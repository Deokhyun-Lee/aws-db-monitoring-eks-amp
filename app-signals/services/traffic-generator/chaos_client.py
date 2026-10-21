"""장애 시나리오를 서비스의 모든 Pod에 한꺼번에 켜고 끈다.

    python chaos_client.py list
    python chaos_client.py start <scenario> [--duration 300]
    python chaos_client.py stop <scenario>
    python chaos_client.py status

시나리오 → 서비스는 아래 SCENARIOS에 고정. 헤드리스 서비스(<service>-pods)의 DNS로 Pod IP를 모두 찾아
각 Pod의 /chaos 엔드포인트를 부른다(상태가 Pod마다 따로이므로). 토큰은 CHAOS_TOKEN 환경 변수.
시작·종료 시각(UTC)을 출력하므로 그대로 rca.py --start/--end에 넣으면 된다.
"""
import argparse
import json
import os
import socket
import sys
import time
import urllib.error
import urllib.request

NAMESPACE = os.environ.get("POD_NAMESPACE", "shop")
SCENARIOS = {
    "mysql-lock": "order-api",
    "cache-stampede": "order-api",
    "pg-slow-query": "inventory-service",
    "app-cpu": "inventory-service",
    "mssql-blocking": "payment-service",
}


def pod_ips(service):
    host = f"{service}-pods.{NAMESPACE}.svc.cluster.local"
    infos = socket.getaddrinfo(host, 8080, proto=socket.IPPROTO_TCP)
    return sorted({i[4][0] for i in infos})


def request(method, url, token=None):
    req = urllib.request.Request(url, method=method, headers={"X-Chaos-Token": token or ""})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def utc_now():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("action", choices=["list", "start", "stop", "status"])
    ap.add_argument("scenario", nargs="?")
    ap.add_argument("--duration", type=int, default=300)
    a = ap.parse_args()
    token = os.environ.get("CHAOS_TOKEN", "")

    if a.action == "list":
        for name, svc in SCENARIOS.items():
            print(f"{name:16s} → {svc}")
        return 0
    if a.action == "status":
        for svc in sorted(set(SCENARIOS.values())):
            for ip in pod_ips(svc):
                code, body = request("GET", f"http://{ip}:8080/chaos")
                active = {k: v["remaining_seconds"] for k, v in body.get("scenarios", {}).items() if v.get("remaining_seconds")}
                print(f"{svc:18s} {ip:15s} {code} active={active or '-'}")
        return 0
    if a.scenario not in SCENARIOS:
        print(f"unknown scenario: {a.scenario}. one of {sorted(SCENARIOS)}", file=sys.stderr)
        return 2
    svc = SCENARIOS[a.scenario]
    ips = pod_ips(svc)
    started = utc_now()
    ok = True
    for ip in ips:
        if a.action == "start":
            code, body = request("POST", f"http://{ip}:8080/chaos/{a.scenario}?duration={a.duration}", token)
        else:
            code, body = request("DELETE", f"http://{ip}:8080/chaos/{a.scenario}", token)
        ok &= code < 300
        print(f"{svc} {ip} → {code} {json.dumps(body, ensure_ascii=False)}")
    if a.action == "start" and ok:
        end = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + a.duration))
        print(f"\n[{a.scenario}] {len(ips)} pod(s), {started} ~ {end} (UTC)")
        print(f"rca: python app-signals/rca/rca.py --environment eks:<클러스터>/{NAMESPACE} --service order-api "
              f"--start {started} --end {end}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
