"""장애 시나리오 스위치 (RCA 실습용).

POST   /chaos/<name>?duration=300   시나리오 시작 (헤더 X-Chaos-Token 필요, 최대 1800초 뒤 자동 종료)
DELETE /chaos/<name>                 시나리오 종료
GET    /chaos                        상태

CHAOS_TOKEN 환경 변수가 비어 있으면 엔드포인트는 모두 403입니다. 상태는 Pod(프로세스)마다 따로이므로
여러 Pod에 같이 걸려면 traffic-generator의 chaos_client.py(헤드리스 서비스로 모든 Pod 호출)를 씁니다.
gunicorn은 워커 1개 + 스레드로 띄워 한 Pod 안에서는 상태가 하나입니다.
"""
import hmac
import logging
import os
import threading
import time

from flask import jsonify, request

log = logging.getLogger("chaos")
MAX_DURATION = 1800


class Chaos:
    def __init__(self, service, scenarios):
        self.service = service
        self.scenarios = scenarios  # name -> {"desc", "worker"(stop_event), "on_start"(), "on_stop"()}
        self.token = os.environ.get("CHAOS_TOKEN", "")
        self._lock = threading.Lock()
        self._state = {}  # name -> {"until", "stop": Event, "timer": Timer}

    # -- 상태
    def active(self, name):
        with self._lock:
            st = self._state.get(name)
            return bool(st) and st["until"] > time.time()

    def status(self):
        now = time.time()
        with self._lock:
            running = {n: round(st["until"] - now) for n, st in self._state.items() if st["until"] > now}
        return {
            "service": self.service,
            "scenarios": {n: {"desc": s.get("desc", ""), "remaining_seconds": running.get(n)}
                          for n, s in self.scenarios.items()},
        }

    # -- 제어
    def start(self, name, duration):
        spec = self.scenarios[name]
        duration = max(1, min(int(duration), MAX_DURATION))
        stop = threading.Event()
        # 타이머는 자기가 시작한 회차만 끈다(같은 시나리오를 다시 켜도 이전 타이머가 새 회차를 끄지 않게)
        timer = threading.Timer(duration, self.stop, args=(name, stop))
        timer.daemon = True
        with self._lock:
            prev = self._state.pop(name, None)
            self._state[name] = {"until": time.time() + duration, "stop": stop, "timer": timer}
        if prev:
            prev["stop"].set()
            prev["timer"].cancel()
        if spec.get("on_start"):
            spec["on_start"]()
        if spec.get("worker"):
            threading.Thread(target=spec["worker"], args=(stop,), name=f"chaos-{name}", daemon=True).start()
        timer.start()
        log.warning("chaos %s started for %ss", name, duration)
        return duration

    def stop(self, name, only=None):
        with self._lock:
            st = self._state.get(name)
            if not st or (only is not None and st["stop"] is not only):
                return False
            del self._state[name]
        st["stop"].set()
        st["timer"].cancel()
        spec = self.scenarios.get(name, {})
        if spec.get("on_stop"):
            spec["on_stop"]()
        log.warning("chaos %s stopped", name)
        return True

    # -- HTTP
    def _authorized(self):
        given = request.headers.get("X-Chaos-Token", "")
        return bool(self.token) and hmac.compare_digest(given, self.token)

    def register(self, app):
        @app.get("/chaos")
        def chaos_status():
            return jsonify(self.status())

        @app.post("/chaos/<name>")
        def chaos_start(name):
            if not self._authorized():
                return jsonify(error="forbidden"), 403
            if name not in self.scenarios:
                return jsonify(error="unknown_scenario", available=sorted(self.scenarios)), 404
            seconds = self.start(name, request.args.get("duration", 300))
            return jsonify(service=self.service, scenario=name, duration_seconds=seconds,
                           started_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())), 202

        @app.delete("/chaos/<name>")
        def chaos_stop(name):
            if not self._authorized():
                return jsonify(error="forbidden"), 403
            return jsonify(service=self.service, scenario=name, stopped=self.stop(name))
