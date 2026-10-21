"""order-api에 일정한 요청을 보내 Application Signals 지표·트레이스의 바탕이 되는 트래픽을 만든다.

환경 변수
  TARGET_URL   기본 http://order-api:8080
  RPS          초당 요청 수 (기본 5)
  MIX          products:60,orders:35,lookup:5 (가중치)
  SKU_COUNT    SKU-0001 ~ SKU-<n> (기본 50, db-init의 상품 수와 같음)
30초마다 상태 코드별 건수와 p50·p99 지연을 로그로 남긴다. 이 생성기 자체는 계측하지 않는다.
"""
import collections
import logging
import os
import random
import signal
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import requests

logging.basicConfig(level="INFO", format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("traffic")

TARGET = os.environ.get("TARGET_URL", "http://order-api:8080").rstrip("/")
RPS = float(os.environ.get("RPS", "5"))
SKU_COUNT = int(os.environ.get("SKU_COUNT", "50"))
MIX = [(name, int(w)) for name, w in (p.split(":") for p in os.environ.get("MIX", "products:60,orders:35,lookup:5").split(","))]
TIMEOUT = float(os.environ.get("TIMEOUT_SECONDS", "15"))

session = requests.Session()
recent_orders = collections.deque(maxlen=200)
stats_lock = threading.Lock()
stats = collections.Counter()
latencies = []


def sku():
    # 일부 SKU에 요청이 몰리는 분포 (핫 상품)
    n = min(SKU_COUNT, int(random.paretovariate(1.2)))
    return f"SKU-{max(1, n):04d}"


def call(kind):
    t0 = time.perf_counter()
    try:
        if kind == "products":
            r = session.get(f"{TARGET}/products/{sku()}", timeout=TIMEOUT)
        elif kind == "orders":
            r = session.post(f"{TARGET}/orders", json={"sku": sku(), "qty": random.randint(1, 3)}, timeout=TIMEOUT)
            if r.status_code == 201:
                recent_orders.append(r.json()["order_id"])
        else:
            oid = random.choice(recent_orders) if recent_orders else "00000000-0000-0000-0000-000000000000"
            r = session.get(f"{TARGET}/orders/{oid}", timeout=TIMEOUT)
        code = str(r.status_code)
    except requests.RequestException as e:
        code = type(e).__name__
    ms = (time.perf_counter() - t0) * 1000
    with stats_lock:
        stats[f"{kind}:{code}"] += 1
        latencies.append(ms)


def report():
    while True:
        time.sleep(30)
        with stats_lock:
            snap, lat = dict(stats), sorted(latencies)
            stats.clear()
            latencies.clear()
        if lat:
            p50 = lat[len(lat) // 2]
            p99 = lat[min(len(lat) - 1, int(len(lat) * 0.99))]
            log.info("last 30s: %s | p50=%.0fms p99=%.0fms", snap, p50, p99)


def main():
    kinds = [k for k, _ in MIX]
    weights = [w for _, w in MIX]
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    threading.Thread(target=report, daemon=True).start()
    log.info("target=%s rps=%s mix=%s", TARGET, RPS, MIX)
    interval = 1.0 / RPS
    workers = max(8, int(RPS * 4))
    # 응답이 느려져도 대기열을 무한히 쌓지 않는다. 남는 자리가 없으면 그 요청은 버리고 'dropped'로 센다
    # (쌓아 두면 장애가 끝난 뒤 한꺼번에 쏟아져 다음 기준 구간을 오염시킨다)
    slots = threading.BoundedSemaphore(workers)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        next_at = time.perf_counter()
        while True:
            kind = random.choices(kinds, weights)[0]
            if slots.acquire(blocking=False):
                pool.submit(call, kind).add_done_callback(lambda _: slots.release())
            else:
                with stats_lock:
                    stats[f"{kind}:dropped"] += 1
            next_at += interval
            time.sleep(max(0.0, next_at - time.perf_counter()))


if __name__ == "__main__":
    main()
