"""로컬 검증용 collector: OTel Collector가 파일로 남긴 span/지표와 로컬 Prometheus로 RCA를 돌린다.

AwsCollector(app-signals/rca/collector.py)와 같은 메서드를 갖는다. Application Signals 지표 대신 span에서
지연·Fault·호출 수를 직접 계산하고, DB 엔진 지표는 로컬 Prometheus(exporter 3종)에서 같은 PromQL로 읽는다.
"""
import collections
import datetime as dt
import json
import os
import sys
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "rca"))

from analysis import (DbEvidence, Dependency, EngineReading, Golden, SpanStat, Stat, parse_identifier,  # noqa: E402
                      scale_cpu, service_name)
from signals import ENGINE_ALIASES, ENGINE_SIGNALS, promql  # noqa: E402

KIND = {1: "INTERNAL", 2: "SERVER", 3: "CLIENT", 4: "PRODUCER", 5: "CONSUMER"}
SERVICES = {"order-api", "inventory-service", "payment-service"}
LOCAL_INSTANCE = {"mysql": "mysql", "postgresql": "postgres", "redis": "valkey"}  # prometheus.yaml의 db_instance


def _val(v):
    for k in ("stringValue", "intValue", "doubleValue", "boolValue"):
        if k in v:
            return int(v[k]) if k == "intValue" else v[k]
    if "arrayValue" in v:
        return [_val(x) for x in v["arrayValue"].get("values", [])]
    return None


def _attrs(lst):
    return {a["key"]: _val(a["value"]) for a in lst or []}


def load_spans(path):
    out = []
    if not os.path.exists(path):
        return out
    with open(path) as f:
        for line in f:
            if not line.strip():
                continue
            doc = json.loads(line)
            for rs in doc.get("resourceSpans", []):
                res = _attrs(rs.get("resource", {}).get("attributes"))
                for ss in rs.get("scopeSpans", []):
                    for s in ss.get("spans", []):
                        start = int(s["startTimeUnixNano"])
                        end = int(s["endTimeUnixNano"])
                        status = s.get("status", {})
                        out.append({
                            "service": res.get("service.name", "?"), "name": s.get("name", ""),
                            "kind": KIND.get(s.get("kind"), str(s.get("kind"))),
                            "trace": s.get("traceId"), "span": s.get("spanId"), "parent": s.get("parentSpanId"),
                            "start": dt.datetime.fromtimestamp(start / 1e9, dt.timezone.utc),
                            "dur_ms": (end - start) / 1e6, "attrs": _attrs(s.get("attributes")),
                            "error": status.get("code") in (2, "STATUS_CODE_ERROR"),
                        })
    return out


def load_metrics(path):
    out = []
    if not os.path.exists(path):
        return out
    with open(path) as f:
        for line in f:
            if not line.strip():
                continue
            doc = json.loads(line)
            for rm in doc.get("resourceMetrics", []):
                res = _attrs(rm.get("resource", {}).get("attributes"))
                for sm in rm.get("scopeMetrics", []):
                    for m in sm.get("metrics", []):
                        for typ in ("histogram", "exponentialHistogram", "sum", "gauge"):
                            for dp in m.get(typ, {}).get("dataPoints", []):
                                out.append({"service": res.get("service.name", "?"), "name": m["name"], "type": typ,
                                            "attrs": _attrs(dp.get("attributes")), "dp": dp,
                                            "time": dt.datetime.fromtimestamp(int(dp.get("timeUnixNano", 0)) / 1e9,
                                                                              dt.timezone.utc)})
    return out


def p99(vals):
    if not vals:
        return None
    v = sorted(vals)
    return v[min(len(v) - 1, int(len(v) * 0.99))]


def http_status(s):
    a = s["attrs"]
    return a.get("http.response.status_code") or a.get("http.status_code")


def operation(s):
    a = s["attrs"]
    if a.get("aws.local.operation"):
        return a["aws.local.operation"]
    method = a.get("http.request.method") or a.get("http.method")
    route = a.get("http.route")
    return f"{method} {route}" if method and route else s["name"]


def remote_service(s):
    a = s["attrs"]
    r = a.get("aws.remote.service") or a.get("db.system") or a.get("server.address") or a.get("net.peer.name") or "?"
    return service_name(str(r))


class LocalCollector:
    def __init__(self, spans, metrics, prom_url="http://localhost:29090"):
        self.spans = spans
        self.metrics = metrics
        self.prom = prom_url
        self.by_id = {s["span"]: s for s in spans}
        self.sources = collections.OrderedDict([("로컬 span (ADOT SDK → OTel Collector 파일)", f"{len(spans)}개")])

    def _in(self, s, w):
        return w.start <= s["start"] < w.end

    def _caller_op(self, s):
        if s["attrs"].get("aws.local.operation"):
            return s["attrs"]["aws.local.operation"]
        p = self.by_id.get(s["parent"])
        while p is not None and p["kind"] != "SERVER":
            p = self.by_id.get(p["parent"])
        return operation(p) if p else "InternalOperation"

    def golden(self, environment, service, op, windows):
        g = Golden()
        for wname in ("baseline", "incident"):
            w = getattr(windows, wname)
            ss = [s for s in self.spans if s["service"] == service and s["kind"] == "SERVER" and self._in(s, w)
                  and (op is None or operation(s) == op)]
            durs = [s["dur_ms"] for s in ss]
            n = len(ss)
            setattr(g.latency_avg, wname, sum(durs) / n if n else None)
            setattr(g.latency_p99, wname, p99(durs))
            setattr(g.requests, wname, n / max(w.seconds / 60, 1 / 60))
            faults = sum(1 for s in ss if s["error"] or (http_status(s) or 0) >= 500)
            errors = sum(1 for s in ss if 400 <= (http_status(s) or 0) < 500)
            setattr(g.fault_rate, wname, 100.0 * faults / n if n else None)
            setattr(g.error_rate, wname, 100.0 * errors / n if n else None)
        # 프로세스 단위 CPU (CloudWatch에서는 PythonProcessCpuUtilization). system.*은 호스트 전체라 쓰지 않는다
        name = "process.cpu.utilization"
        vals = {}
        for wname in ("baseline", "incident"):
            w = getattr(windows, wname)
            vals[wname] = [m["dp"]["asDouble"] for m in self.metrics
                           if m["service"] == service and m["name"] == name and w.start <= m["time"] < w.end
                           and m["dp"].get("asDouble") is not None]
        if vals["baseline"] or vals["incident"]:
            g.cpu.baseline, g.cpu.incident = scale_cpu(vals["baseline"], vals["incident"])
            g.cpu_source = f"{name} (코어 수로 나눈 값)"
        return g

    def dependencies(self, environment, service, op, windows):
        groups = collections.OrderedDict()
        for wname in ("baseline", "incident"):
            w = getattr(windows, wname)
            for s in self.spans:
                if s["service"] != service or s["kind"] != "CLIENT" or not self._in(s, w):
                    continue
                caller = self._caller_op(s)
                if op and caller != op:
                    continue
                rsvc = remote_service(s)
                rop = s["attrs"].get("aws.remote.operation") or s["name"]
                groups.setdefault((caller, rsvc, rop), {"baseline": [], "incident": [], "sample": s})[wname].append(s)
        deps = []
        for (caller, rsvc, rop), g in groups.items():
            a = g["sample"]["attrs"]
            engine = ENGINE_ALIASES.get(str(rsvc).lower()) or ENGINE_ALIASES.get(str(a.get("db.system", "")).lower())
            kind = "service" if rsvc in SERVICES else ("db" if engine else "other")
            ident = a.get("aws.remote.resource.identifier")
            db_name, host = parse_identifier(ident) if ident else (a.get("db.name"), a.get("server.address") or a.get("net.peer.name"))
            d = Dependency(caller_operation=caller, remote_service=rsvc, remote_operation=rop, kind=kind,
                           engine=engine, host=host, db_name=db_name)
            for wname in ("baseline", "incident"):
                ss = g[wname]
                w = getattr(windows, wname)
                durs = [s["dur_ms"] for s in ss]
                n = len(ss)
                setattr(d.latency_avg, wname, sum(durs) / n if n else None)
                setattr(d.latency_p99, wname, p99(durs))
                setattr(d.calls, wname, n / max(w.seconds / 60, 1 / 60))
                setattr(d.fault_rate, wname, 100.0 * sum(1 for s in ss if s["error"]) / n if n else None)
            deps.append(d)
        return deps

    def top_db_spans(self, service, windows):
        grp = collections.defaultdict(list)
        for s in self.spans:
            if s["service"] == service and s["kind"] == "CLIENT" and s["attrs"].get("db.system") and self._in(s, windows.incident):
                grp[(s["attrs"]["db.system"], s["attrs"].get("db.statement") or s["name"])].append(s)
        out = [SpanStat(db_system=k[0], statement=str(k[1]), calls=len(v), avg_ms=sum(x["dur_ms"] for x in v) / len(v),
                        p99_ms=p99([x["dur_ms"] for x in v]), errors=sum(1 for x in v if x["error"]))
               for k, v in grp.items()]
        return sorted(out, key=lambda s: s.p99_ms, reverse=True)[:10]

    def xray_root_causes(self, service, windows):
        return []

    def _prom(self, query, w):
        qs = urllib.parse.urlencode({"query": query, "start": w.start.timestamp(), "end": w.end.timestamp(), "step": 5})
        with urllib.request.urlopen(f"{self.prom}/api/v1/query_range?{qs}", timeout=20) as r:
            data = json.load(r)
        return [float(v) for series in data["data"]["result"] for _, v in series["values"] if v not in ("NaN", "+Inf", "-Inf")]

    def db_evidence(self, dep, windows):
        ev = DbEvidence(dependency=dep)
        inst = LOCAL_INSTANCE.get(dep.engine)
        if not inst:
            ev.missing.append(f"로컬 검증에 {dep.engine} 엔진 exporter 없음 (EKS 기본 경로와 같음: custom-metrics 모듈 필요)")
            return ev
        ev.instances, ev.resolved_by = [inst], "local"
        for sig in ENGINE_SIGNALS[dep.engine]:
            q = promql(sig, "db_instance", [inst])
            vals = {}
            for wname in ("baseline", "incident"):
                v = self._prom(q, getattr(windows, wname))
                agg = {"max": max, "min": min}.get(sig.agg, lambda x: sum(x) / len(x))
                vals[wname] = agg(v) if v else None
            ev.readings.append(EngineReading(sig.key, sig.label, sig.family, Stat(vals["baseline"], vals["incident"]),
                                             sig.better, sig.unit, sig.abs_threshold, source="prometheus(로컬)"))
        self.sources.setdefault("로컬 Prometheus (exporter 3종)", "사용")
        return ev
