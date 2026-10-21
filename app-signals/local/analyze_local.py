"""로컬 검증 결과 분석: (1) Application Signals용 텔레메트리가 나왔는지 (2) 시나리오마다 RCA가 원인을 짚는지.

    python analyze_local.py out/

out/timeline.json(verify.sh가 기록한 구간)과 out/traces.json, out/metrics.json을 읽어
out/summary.md(텔레메트리 점검·시나리오별 결론)와 out/rca-<scenario>.md(보고서 원문)를 쓴다.
"""
import collections
import datetime as dt
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "..", "rca"))

from analysis import Window, Windows, diagnose, walk  # noqa: E402
from local_collector import LocalCollector, load_metrics, load_spans, remote_service  # noqa: E402
from report import markdown  # noqa: E402

EXPECTED_EDGES = [  # (서비스, RemoteService) — Application Signals 의존성으로 보여야 하는 것
    ("order-api", "mysql"), ("order-api", "redis"), ("order-api", "inventory-service"),
    ("order-api", "payment-service"), ("inventory-service", "postgresql"), ("payment-service", "mssql"),
]
EXPECTED_CAUSE = {"mysql-lock": "lock", "pg-slow-query": "scan", "mssql-blocking": "db_wait_unconfirmed",
                  "cache-stampede": "cache_miss", "app-cpu": "app_cpu"}


def t(s):
    return dt.datetime.fromisoformat(s.replace("Z", "+00:00"))


def main(out_dir):
    spans = load_spans(os.path.join(out_dir, "traces.json"))
    metrics = load_metrics(os.path.join(out_dir, "metrics.json"))
    timeline = json.load(open(os.path.join(out_dir, "timeline.json")))
    lines = ["# 로컬 검증 결과", "", f"- span {len(spans)}개, 지표 데이터 포인트 {len(metrics)}개", ""]

    # 1) 텔레메트리 점검
    lines += ["## 1. Application Signals용 텔레메트리", "", "### 의존성 (CLIENT span의 aws.remote.service)", "",
              "| 서비스 | 의존성 | 결과 | RemoteOperation 예 | RemoteResourceIdentifier 예 |", "|---|---|---|---|---|"]
    edges = collections.defaultdict(list)
    for s in spans:
        if s["kind"] == "CLIENT":
            edges[(s["service"], remote_service(s))].append(s)
    ok_all = True
    for svc, remote in EXPECTED_EDGES:
        ss = edges.get((svc, remote), [])
        ok = bool(ss)
        ok_all &= ok
        ops = sorted({str(x["attrs"].get("aws.remote.operation", "")) for x in ss})[:4]
        idents = sorted({str(x["attrs"].get("aws.remote.resource.identifier", "")) for x in ss if x["attrs"].get("aws.remote.resource.identifier")})[:1]
        lines.append(f"| {svc} | {remote} | {'PASS' if ok else 'FAIL'} ({len(ss)}) | {', '.join(ops) or '-'} | {', '.join(idents) or '-'} |")
    others = sorted({k for k in edges if k not in EXPECTED_EDGES})
    if others:
        lines += ["", "그 밖의 CLIENT span 대상: " + ", ".join(f"{a}→{b}({len(edges[(a, b)])})" for a, b in others)]
    names = collections.Counter(m["name"] for m in metrics)
    lines += ["", "### 지표 이름 (Application Signals exporter → Collector)", ""]
    lines += [f"- `{n}`: {c}" for n, c in names.most_common(20)]
    sample = next((m for m in metrics if m["name"].lower() in ("latency", "fault", "error")), None)
    if sample:
        lines += ["", "지표 속성 예: " + ", ".join(f"`{k}`" for k in sorted(sample["attrs"]))]
    server = collections.Counter((s["service"], s["attrs"].get("aws.local.operation") or s["name"]) for s in spans if s["kind"] == "SERVER")
    lines += ["", "### 서비스 오퍼레이션 (SERVER span)", ""]
    lines += [f"- {svc} `{op}`: {c}" for (svc, op), c in server.most_common(12)]

    # 2) 시나리오별 RCA
    base = Window(t(timeline["baseline"][0]), t(timeline["baseline"][1]))
    lines += ["", "## 2. 장애 시나리오별 RCA (로컬 span + 로컬 Prometheus)", "",
              f"기준 구간: {timeline['baseline'][0]} ~ {timeline['baseline'][1]}", "",
              "| 시나리오 | 기대 원인 | RCA 1순위 | 확신도 | 근거 계층 | 결과 |", "|---|---|---|---|---|---|"]
    results = {}
    detail = []
    for sc in timeline["scenarios"]:
        name = sc["name"]
        w = Windows(baseline=base, incident=Window(t(sc["start"]), t(sc["end"])))
        col = LocalCollector(spans, metrics)
        root = walk(col, "local", "order-api", None, w)
        hyps = diagnose(root, w)
        top = hyps[0]
        ok = top.cause == EXPECTED_CAUSE.get(name)
        results[name] = ok
        lines.append(f"| {name} | {EXPECTED_CAUSE.get(name)} | {top.cause} ({top.title}) | {top.confidence} | "
                     f"{', '.join(top.layers)} | {'PASS' if ok else 'FAIL'} |")
        with open(os.path.join(out_dir, f"rca-{name}.md"), "w") as f:
            f.write(markdown(root, hyps, w, col.sources))
        nodes = []
        n = root
        while n:
            nodes.append({"service": n.service, "p99_ms": [n.golden.latency_p99.baseline, n.golden.latency_p99.incident],
                          "fault_pct": [n.golden.fault_rate.baseline, n.golden.fault_rate.incident]})
            n = n.child
        detail.append({"scenario": name, "expected": EXPECTED_CAUSE.get(name), "cause": top.cause, "title": top.title,
                       "confidence": top.confidence, "layers": top.layers, "chain": top.chain,
                       "evidence": top.evidence, "pass": ok, "path": nodes})
    lines += ["", f"텔레메트리 의존성 점검: {'PASS' if ok_all else 'FAIL'}, "
                  f"RCA 시나리오: {sum(results.values())}/{len(results)} PASS", ""]
    with open(os.path.join(out_dir, "results.json"), "w") as f:
        json.dump({"spans": len(spans), "metric_points": len(metrics), "baseline": timeline["baseline"],
                   "edges": {f"{a}->{b}": len(edges.get((a, b), [])) for a, b in EXPECTED_EDGES},
                   "metric_names": dict(names.most_common(20)), "scenarios": detail}, f, ensure_ascii=False, indent=2)
    with open(os.path.join(out_dir, "summary.md"), "w") as f:
        f.write("\n".join(lines))
    print("\n".join(lines))
    return 0 if ok_all and all(results.values()) else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else os.path.join(HERE, "out")))
