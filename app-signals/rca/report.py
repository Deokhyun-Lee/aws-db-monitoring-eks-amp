"""RCA 결과를 Markdown(사람용)과 JSON(자동화·에이전트용)으로 낸다."""
import dataclasses
import json

from analysis import change, fmt, path
from signals import ENGINE_NAMES


def _iso(t):
    return t.strftime("%Y-%m-%d %H:%M:%SZ")


def markdown(root, hypotheses, windows, sources):
    nodes = path(root)
    lines = [
        f"# RCA 보고서: {root.service}{' ' + root.operation if root.operation else ''}",
        "",
        f"- 환경: `{root.environment}`",
        f"- 사고 구간: {_iso(windows.incident.start)} ~ {_iso(windows.incident.end)}",
        f"- 기준 구간: {_iso(windows.baseline.start)} ~ {_iso(windows.baseline.end)}",
        "- 값은 '기준 → 사고' 순서. p99는 1분 단위 p99의 구간 평균, 평균 지연은 Sum/SampleCount",
        "",
        "## 결론",
        "",
    ]
    for i, h in enumerate(hypotheses, 1):
        lines.append(f"{i}. **[{h.confidence}] {h.title}** — 근거: {', '.join(h.layers) or '없음'}")
        lines.append(f"   - 경로: {' → '.join(h.chain)}")
        for e in h.evidence:
            lines.append(f"   - {e}")
    lines += ["", "## 앱 → DB 경로의 골든 지표", "",
              "| 서비스 | 지연 p99 (ms) | 평균 지연 (ms) | 트래픽 (분당) | Fault (%) | Error (%) | CPU |",
              "|---|---|---|---|---|---|---|"]
    for n in nodes:
        g = n.golden
        cpu = f"{change(g.cpu, 'ratio')} ({g.cpu_source})" if g.cpu.known else "?"
        lines.append(f"| {n.service} {n.operation or ''} | {change(g.latency_p99)} | {change(g.latency_avg)} | "
                     f"{change(g.requests)} | {change(g.fault_rate)} | {change(g.error_rate)} | {cpu} |")
    for n in nodes:
        lines += ["", f"## 의존성 순위: {n.service}", "",
                  "| 순위 | 호출하는 오퍼레이션 | 의존성 | 종류 | 평균 지연 (ms) | p99 (ms) | 호출 (분당) | Fault (%) | 판정 |",
                  "|---|---|---|---|---|---|---|---|---|"]
        for i, d in enumerate(n.ranked()[:8], 1):
            flags = [x for x, on in (("지연↑", d.latency_worse), ("Fault↑", d.fault_worse), ("호출 급증", d.call_surge)) if on]
            kind = ENGINE_NAMES.get(d.engine, d.kind) if d.kind == "db" else d.kind
            lines.append(f"| {i} | {d.caller_operation} | {d.label} | {kind} | {change(d.latency_avg)} | "
                         f"{change(d.latency_p99)} | {change(d.calls)} | {change(d.fault_rate)} | {' '.join(flags) or '-'} |")
        if n.spans:
            lines += ["", f"### 느린 DB 문장 (Transaction Search, 사고 구간): {n.service}", "",
                      "| DB | 문장 | 호출 | 평균 (ms) | p99 (ms) | 오류 |", "|---|---|---|---|---|---|"]
            for s in n.spans[:6]:
                stmt = s.statement.replace("|", "\\|").replace("\n", " ")[:120]
                lines.append(f"| {s.db_system} | `{stmt}` | {s.calls} | {fmt(s.avg_ms)} | {fmt(s.p99_ms)} | {s.errors} |")
        if n.xray_root_causes:
            lines += ["", f"### X-Ray 근본 원인 엔터티 (느리거나 실패한 트레이스): {n.service}", ""]
            lines += [f"- {entity} — {count}건" for entity, count in n.xray_root_causes]
        for ev in n.db_evidence:
            d = ev.dependency
            lines += ["", f"### DB 엔진 지표: {ENGINE_NAMES.get(d.engine, d.engine)} "
                          f"({', '.join(ev.instances) or d.host or '?'}, {ev.resolved_by})", ""]
            if ev.readings:
                lines += ["| 지표 | 종류 | 값 | 나빠진 배수 | 출처 |", "|---|---|---|---|---|"]
                for r in ev.readings:
                    wf = r.worse_factor
                    mark = " **↑**" if r.worse else ""
                    scope = "" if r.scope == "instance" else ", 엔진 전체"
                    lines.append(f"| {r.label} | {r.family} | {change(r.stat, r.unit)} | {fmt(wf)}{mark} | {r.source}{scope} |")
            for m in ev.missing:
                lines.append(f"- 없음: {m}")
    if hypotheses:
        lines += ["", "## 다음 조치 (1순위 가설 기준)", ""]
        lines += [f"- {a}" for a in hypotheses[0].actions]
    lines += ["", "## 데이터 출처", ""]
    lines += [f"- {k}: {v}" for k, v in sources.items()]
    lines += ["", "> 이 보고서는 지표·트레이스의 변화로 만든 가설입니다. 조치 전에 DB에서 직접 확인하세요.", ""]
    return "\n".join(lines)


def to_json(root, hypotheses, windows, sources):
    def conv(o):
        if dataclasses.is_dataclass(o):
            return {k: conv(v) for k, v in dataclasses.asdict(o).items()}
        if isinstance(o, dict):
            return {str(k): conv(v) for k, v in o.items()}
        if isinstance(o, (list, tuple)):
            return [conv(v) for v in o]
        if hasattr(o, "isoformat"):
            return o.isoformat()
        if isinstance(o, float) and o == float("inf"):
            return "inf"
        return o

    return json.dumps({
        "service": root.service, "environment": root.environment, "operation": root.operation,
        "incident": [windows.incident.start.isoformat(), windows.incident.end.isoformat()],
        "baseline": [windows.baseline.start.isoformat(), windows.baseline.end.isoformat()],
        "hypotheses": conv(hypotheses), "path": [conv(dataclasses.replace(n, child=None)) for n in path(root)],
        "sources": dict(sources),
    }, ensure_ascii=False, indent=2)
