"""AWS에서 RCA 근거를 모은다. 모든 호출은 읽기 전용이다.

출처                         API
  골든 지표                    CloudWatch GetMetricData (ApplicationSignals 네임스페이스, ContainerInsights)
  의존성 목록·지표 참조        Application Signals ListServiceDependencies
  느린 DB 문장                 CloudWatch Logs Insights on aws/spans (Transaction Search)
  트레이스 근본 원인           X-Ray GetTraceSummaries (ResponseTimeRootCauses·FaultRootCauses)
  DB 엔드포인트 → 인스턴스      RDS DescribeDBClusters/Instances, ElastiCache DescribeReplicationGroups
  DB 엔진 지표                 AMP query_range (SigV4) → 없으면 CloudWatch AWS/RDS·AWS/ElastiCache

출처가 꺼져 있거나 권한이 없으면 건너뛰고 sources에 이유를 남긴다(보고서의 '확인하지 못한 것').
"""
import collections
import json
import time
import urllib.parse
import urllib.request

import boto3
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest
from botocore.exceptions import BotoCoreError, ClientError

from analysis import (DbEvidence, Dependency, EngineReading, Golden, SpanStat, Stat, hour_bounds, parse_identifier,
                      scale_cpu, service_name)
from signals import CW_FALLBACK, ENGINE_ALIASES, ENGINE_SIGNALS, promql

NS = "ApplicationSignals"
ERRORS = (ClientError, BotoCoreError, OSError, ValueError, KeyError)


def _dims(d):
    return [{"Name": k, "Value": v} for k, v in d.items()]


class AwsCollector:
    def __init__(self, region, amp_workspace_id=None, instance_label="db_instance", spans_log_group="aws/spans",
                 session=None):
        self.region = region
        self.session = session or boto3.Session(region_name=region)
        c = self.session.client
        self.cw, self.appsig, self.logs, self.xray = c("cloudwatch"), c("application-signals"), c("logs"), c("xray")
        self.rds, self.ec = c("rds"), c("elasticache")
        self.amp_ws = amp_workspace_id
        self.label = instance_label
        self.spans_lg = spans_log_group
        self.sources = collections.OrderedDict()   # 출처 → 상태 문장
        self._known = {}                           # environment → 계측된 서비스 이름

    def _ok(self, name, msg="사용"):
        self.sources.setdefault(name, msg)

    def _fail(self, name, err):
        self.sources[name] = f"사용 못 함: {err}"

    # ---------------------------------------------------------------- CloudWatch 공통
    def _series(self, specs, window):
        """specs: {key: (namespace, metric, dims(dict), stat)} → {key: [1분 값…]}"""
        keys = list(specs)
        out = {k: [] for k in keys}
        for off in range(0, len(keys), 500):
            chunk = keys[off:off + 500]
            queries = [{"Id": f"q{off + i}", "ReturnData": True,
                        "MetricStat": {"Metric": {"Namespace": specs[k][0], "MetricName": specs[k][1],
                                                  "Dimensions": _dims(specs[k][2])},
                                       "Period": 60, "Stat": specs[k][3]}} for i, k in enumerate(chunk)]
            token = None
            while True:
                kw = dict(MetricDataQueries=queries, StartTime=window.start, EndTime=window.end,
                          ScanBy="TimestampAscending")
                if token:
                    kw["NextToken"] = token
                resp = self.cw.get_metric_data(**kw)
                for r in resp["MetricDataResults"]:
                    out[keys[int(r["Id"][1:])]].extend(r["Values"])
                token = resp.get("NextToken")
                if not token:
                    break
        return out

    @staticmethod
    def _mean(v):
        return sum(v) / len(v) if v else None

    # ---------------------------------------------------------------- 골든 지표
    def golden(self, environment, service, operation, windows):
        dims = {"Environment": environment, "Service": service}
        if operation:
            dims["Operation"] = operation
        specs = {
            "lat_sum": (NS, "Latency", dims, "Sum"), "lat_n": (NS, "Latency", dims, "SampleCount"),
            "lat_p99": (NS, "Latency", dims, "p99"), "fault": (NS, "Fault", dims, "Sum"),
            "error": (NS, "Error", dims, "Sum"),
            "cpu_py": (NS, "PythonProcessCpuUtilization", {"Environment": environment, "Service": service}, "Average"),
        }
        if environment.startswith("eks:") and "/" in environment:
            cluster, namespace = environment[4:].split("/", 1)
            specs["cpu_ci"] = ("ContainerInsights", "pod_cpu_utilization",
                               {"ClusterName": cluster, "Namespace": namespace, "PodName": service}, "Average")
        g = Golden()
        try:
            raw = {w: self._series(specs, getattr(windows, w)) for w in ("baseline", "incident")}
            for wname in ("baseline", "incident"):
                w, s = getattr(windows, wname), raw[wname]
                n, minutes = sum(s["lat_n"]), max(1.0, w.seconds / 60)
                setattr(g.latency_avg, wname, sum(s["lat_sum"]) / n if n else None)
                setattr(g.latency_p99, wname, self._mean(s["lat_p99"]))
                setattr(g.requests, wname, n / minutes)
                setattr(g.fault_rate, wname, 100.0 * sum(s["fault"]) / n if n else None)
                setattr(g.error_rate, wname, 100.0 * sum(s["error"]) / n if n else None)
            # CPU: 두 구간을 같은 배율로. Container Insights pod_cpu_utilization은 백분율(노드 대비)
            if raw["baseline"]["cpu_py"] or raw["incident"]["cpu_py"]:
                g.cpu.baseline, g.cpu.incident = scale_cpu(raw["baseline"]["cpu_py"], raw["incident"]["cpu_py"])
                g.cpu_source = "Python 런타임"
            elif raw["baseline"].get("cpu_ci") or raw["incident"].get("cpu_ci"):
                g.cpu.baseline, g.cpu.incident = scale_cpu(raw["baseline"].get("cpu_ci", []),
                                                           raw["incident"].get("cpu_ci", []), percent=True)
                g.cpu_source = "Container Insights (노드 대비)"
            self._ok("Application Signals 지표")
        except ERRORS as e:
            self._fail("Application Signals 지표", e)
        return g

    # ---------------------------------------------------------------- 의존성
    def instrumented_services(self, environment, windows):
        """같은 Environment의 계측된 서비스 이름 (ListServices). 실패하면 빈 집합 — 의존성 판별은 차원만으로 한다."""
        if environment in self._known:
            return self._known[environment]
        names = set()
        try:
            start, end = hour_bounds(windows.baseline.start, windows.incident.end)
            kw = dict(StartTime=start, EndTime=end)
            while True:
                resp = self.appsig.list_services(**kw)
                for s in resp.get("ServiceSummaries", []):
                    k = s.get("KeyAttributes", {})
                    kinds = {m.get("InstrumentationType") for m in s.get("AttributeMaps", [])} - {None}
                    if k.get("Type") == "Service" and k.get("Environment") == environment and \
                            kinds != {"UNINSTRUMENTED"}:
                        names.add(k.get("Name"))
                if not resp.get("NextToken"):
                    break
                kw["NextToken"] = resp["NextToken"]
            self._ok("Application Signals 서비스 목록")
        except ERRORS as e:
            self._fail("Application Signals 서비스 목록", e)
        self._known[environment] = names
        return names

    def dependencies(self, environment, service, operation, windows):
        items = []
        try:
            token = None
            while True:
                list_start, list_end = hour_bounds(windows.baseline.start, windows.incident.end)
                kw = dict(StartTime=list_start, EndTime=list_end,
                          KeyAttributes={"Type": "Service", "Name": service, "Environment": environment})
                if token:
                    kw["NextToken"] = token
                resp = self.appsig.list_service_dependencies(**kw)
                items.extend(resp.get("ServiceDependencies", []))
                token = resp.get("NextToken")
                if not token:
                    break
            self._ok("Application Signals 의존성")
        except ERRORS as e:
            self._fail("Application Signals 의존성", e)
            return []

        known = self.instrumented_services(environment, windows)
        deps, specs = [], {}
        for idx, it in enumerate(items):
            if operation and it.get("OperationName") != operation:
                continue
            refs = {m["MetricName"]: m for m in it.get("MetricReferences", [])}
            if "Latency" not in refs:
                continue
            key = it.get("DependencyKeyAttributes", {})
            dims = {d["Name"]: d["Value"] for d in refs["Latency"].get("Dimensions", [])}
            remote = dims.get("RemoteService") or key.get("Name") or key.get("Identifier") or "UnknownRemoteService"
            ident = dims.get("RemoteResourceIdentifier") or key.get("Identifier")
            rtype = dims.get("RemoteResourceType") or key.get("ResourceType")
            engine = ENGINE_ALIASES.get(remote.lower())
            if key.get("Type") == "Service" or (not engine and (dims.get("RemoteEnvironment")
                                                                 or service_name(remote) in known)):
                # 같은 EKS 클러스터의 계측된 서비스는 RemoteEnvironment 차원이 붙는다(AWS 문서). 원격 이름이
                # host:port로만 와도(미계측 원격 서비스로 잡힌 경우) 계측된 서비스 이름과 같으면 따라 내려간다
                kind = "service"
            elif engine or rtype == "DB::Connection":
                kind = "db"
            elif remote.startswith("AWS::"):
                kind = "aws"
            else:
                kind = "other"
            if kind == "service":
                # 'inventory-service:8080'·'<svc>.<ns>.svc.cluster.local:8080' → 하위 서비스 조회용 이름
                remote = (key.get("Type") == "Service" and key.get("Name")) or service_name(remote)
            db_name, host = parse_identifier(ident) if (ident and kind == "db") else (None, None)
            dep = Dependency(caller_operation=it.get("OperationName", ""), remote_service=remote,
                             remote_operation=it.get("DependencyOperationName") or dims.get("RemoteOperation", ""),
                             kind=kind, engine=engine, host=host, db_name=db_name,
                             environment=key.get("Environment") or dims.get("RemoteEnvironment"))
            deps.append(dep)
            ns = refs["Latency"]["Namespace"]
            specs[(len(deps) - 1, "sum")] = (ns, "Latency", dims, "Sum")
            specs[(len(deps) - 1, "n")] = (ns, "Latency", dims, "SampleCount")
            specs[(len(deps) - 1, "p99")] = (ns, "Latency", dims, "p99")
            if "Fault" in refs:
                fd = {d["Name"]: d["Value"] for d in refs["Fault"].get("Dimensions", [])}
                specs[(len(deps) - 1, "fault")] = (refs["Fault"]["Namespace"], "Fault", fd, "Sum")
        try:
            for wname in ("baseline", "incident"):
                w = getattr(windows, wname)
                s = self._series(specs, w)
                for i, dep in enumerate(deps):
                    n = sum(s.get((i, "n"), []))
                    setattr(dep.latency_avg, wname, sum(s[(i, "sum")]) / n if n else None)
                    setattr(dep.latency_p99, wname, self._mean(s[(i, "p99")]))
                    setattr(dep.calls, wname, n / max(1.0, w.seconds / 60))
                    if (i, "fault") in s:
                        setattr(dep.fault_rate, wname, 100.0 * sum(s[(i, "fault")]) / n if n else None)
        except ERRORS as e:
            self._fail("Application Signals 의존성 지표", e)
        return deps

    # ---------------------------------------------------------------- Transaction Search
    def _insights(self, query, window):
        qid = self.logs.start_query(logGroupName=self.spans_lg, startTime=int(window.start.timestamp()),
                                    endTime=int(window.end.timestamp()), queryString=query, limit=20)["queryId"]
        deadline = time.time() + 90
        while time.time() < deadline:
            r = self.logs.get_query_results(queryId=qid)
            if r["status"] in ("Complete", "Failed", "Cancelled", "Timeout"):
                if r["status"] != "Complete":
                    raise ValueError(f"Logs Insights {r['status']}")
                return [{f["field"]: f["value"] for f in row} for row in r["results"]]
            time.sleep(1.5)
        raise ValueError("Logs Insights 시간 초과")

    def top_db_spans(self, service, windows):
        name = "Transaction Search(aws/spans)"
        if self.sources.get(name, "").startswith("사용 못 함"):
            return []
        svc = service.replace('"', "")
        where = (f'(attributes.aws.local.service = "{svc}" or resource.attributes.service.name = "{svc}")'
                 " and ispresent(attributes.db.system)")
        try:
            rows = self._insights(
                f"filter {where}\n| stats count(*) as calls, avg(durationNano) as avg_ns, pct(durationNano, 99) as p99_ns"
                " by attributes.db.system, attributes.db.statement\n| sort p99_ns desc\n| limit 10", windows.incident)
            errs = self._insights(
                f"filter {where} and status.code like /(?i)error/\n| stats count(*) as errors"
                " by attributes.db.system, attributes.db.statement", windows.incident)
            self._ok(name)
        except ERRORS as e:
            self._fail(name, e)
            return []
        err_by = {(r.get("attributes.db.system"), r.get("attributes.db.statement")): int(float(r.get("errors", 0)))
                  for r in errs}
        out = []
        for r in rows:
            key = (r.get("attributes.db.system"), r.get("attributes.db.statement"))
            out.append(SpanStat(db_system=key[0] or "?", statement=key[1] or "(statement 없음)",
                                calls=int(float(r.get("calls", 0))), avg_ms=float(r.get("avg_ns", 0)) / 1e6,
                                p99_ms=float(r.get("p99_ns", 0)) / 1e6, errors=err_by.get(key, 0)))
        return out

    # ---------------------------------------------------------------- X-Ray
    def xray_root_causes(self, service, windows, pages=5):
        counts = collections.Counter()
        traces = 0
        try:
            token, n = None, 0
            while n < pages:
                kw = dict(StartTime=windows.incident.start, EndTime=windows.incident.end, Sampling=False,
                          FilterExpression=f'service("{service}") {{ fault = true OR responsetime > 1 }}')
                if token:
                    kw["NextToken"] = token
                resp = self.xray.get_trace_summaries(**kw)
                traces += len(resp.get("TraceSummaries", []))
                for s in resp.get("TraceSummaries", []):
                    for rc in s.get("ResponseTimeRootCauses", []) + s.get("FaultRootCauses", []):
                        for svc in rc.get("Services", []):
                            entity = " → ".join(e.get("Name", "?") for e in svc.get("EntityPath", [])) or svc.get("Name", "?")
                            counts[f"{svc.get('Name', '?')}: {entity}"] += 1
                token, n = resp.get("NextToken"), n + 1
                if not token:
                    break
            # 표본 수를 함께 남긴다: Transaction Search 색인 비율(기본 1%)에 따라 몇 건 안 될 수 있어 다른 근거와 같은 무게로 읽으면 안 된다
            self.sources[f"X-Ray 트레이스 요약 ({service})"] = f"느리거나 실패한 트레이스 {traces}건"
        except ERRORS as e:
            self._fail("X-Ray 트레이스 요약", e)
        return counts.most_common(5)

    # ---------------------------------------------------------------- DB
    def _resolve(self, engine, host):
        if not host:
            return [], "unknown"
        label = host.split(".")[0]
        try:
            if engine == "redis":
                for page in self.ec.get_paginator("describe_replication_groups").paginate():
                    for g in page["ReplicationGroups"]:
                        addrs = {(g.get("ConfigurationEndpoint") or {}).get("Address")}
                        for ng in g.get("NodeGroups", []):
                            addrs |= {(ng.get("PrimaryEndpoint") or {}).get("Address"),
                                      (ng.get("ReaderEndpoint") or {}).get("Address")}
                            addrs |= {(m.get("ReadEndpoint") or {}).get("Address") for m in ng.get("NodeGroupMembers", [])}
                        if host in addrs:
                            self._ok("RDS·ElastiCache 메타데이터")
                            return list(g.get("MemberClusters", [])), "elasticache"
                return [label], "host-only"
            if ".cluster-" in host:
                cl = self.rds.describe_db_clusters(DBClusterIdentifier=label)["DBClusters"][0]
                members = cl.get("DBClusterMembers", [])
                want_reader = ".cluster-ro-" in host
                ids = [m["DBInstanceIdentifier"] for m in members if m.get("IsClusterWriter") != want_reader]
                self._ok("RDS·ElastiCache 메타데이터")
                return ids or [label], "rds-cluster"
            self.rds.describe_db_instances(DBInstanceIdentifier=label)
            self._ok("RDS·ElastiCache 메타데이터")
            return [label], "rds-instance"
        except ERRORS as e:
            self._fail("RDS·ElastiCache 메타데이터", e)
            return [label], "host-only"

    def _amp_values(self, query, window, step=60):
        url = f"https://aps-workspaces.{self.region}.amazonaws.com/workspaces/{self.amp_ws}/api/v1/query_range"
        body = urllib.parse.urlencode({"query": query, "start": window.start.timestamp(),
                                       "end": window.end.timestamp(), "step": step})
        req = AWSRequest(method="POST", url=url, data=body,
                         headers={"Content-Type": "application/x-www-form-urlencoded"})
        SigV4Auth(self.session.get_credentials().get_frozen_credentials(), "aps", self.region).add_auth(req)
        http_req = urllib.request.Request(url, data=body.encode(), headers=dict(req.headers.items()), method="POST")
        with urllib.request.urlopen(http_req, timeout=30) as resp:
            data = json.load(resp)
        vals = []
        for series in data.get("data", {}).get("result", []):
            for _, v in series.get("values", []):
                if v not in ("NaN", "+Inf", "-Inf"):
                    vals.append(float(v))
        return vals

    @staticmethod
    def _agg(vals, how):
        if not vals:
            return None
        return {"max": max, "min": min}.get(how, lambda v: sum(v) / len(v))(vals)

    def _amp_readings(self, engine, instances, windows):
        sigs = ENGINE_SIGNALS[engine]
        for scope, values in (("instance", instances), ("engine", None)):
            if scope == "instance" and not instances:
                continue
            readings = []
            for s in sigs:
                q = promql(s, self.label, values)
                stat = Stat(self._agg(self._amp_values(q, windows.baseline), s.agg),
                            self._agg(self._amp_values(q, windows.incident), s.agg))
                readings.append(EngineReading(s.key, s.label, s.family, stat, s.better, s.unit, s.abs_threshold,
                                              source="amp", scope=scope))
            if any(r.stat.known for r in readings):
                return readings
        return []

    def _cw_readings(self, engine, instances, windows):
        ns, dim, sigs = CW_FALLBACK[engine]
        specs = {(s.key, inst): (ns, s.metric, {dim: inst}, s.stat) for s in sigs for inst in instances}
        readings = []
        if not specs:
            return readings
        per = {}
        for wname in ("baseline", "incident"):
            per[wname] = self._series(specs, getattr(windows, wname))
        for s in sigs:
            stat = Stat()
            for wname in ("baseline", "incident"):
                vals = [self._mean(per[wname][(s.key, inst)]) for inst in instances]
                vals = [v for v in vals if v is not None]
                if vals:
                    setattr(stat, wname, (min if s.better == "higher" else max)(vals))
            readings.append(EngineReading(s.key, s.label, s.family, stat, s.better, s.unit, None,
                                          source="cloudwatch", scope="instance"))
        return readings

    def db_evidence(self, dep, windows):
        ev = DbEvidence(dependency=dep)
        if not dep.engine:
            ev.missing.append(f"엔진을 알 수 없는 DB 의존성({dep.remote_service})")
            return ev
        ev.instances, ev.resolved_by = self._resolve(dep.engine, dep.host)
        readings = []
        if self.amp_ws:
            try:
                readings = self._amp_readings(dep.engine, ev.instances, windows)
                self._ok("AMP 엔진 지표")
                if not readings:
                    ev.missing.append(f"AMP에 {dep.engine} 엔진 지표 시계열 없음(exporter 수집 상태·'{self.label}' 라벨 확인)")
            except ERRORS as e:
                self._fail("AMP 엔진 지표", e)
                ev.missing.append(f"AMP 조회 실패: {e}")
        else:
            ev.missing.append("AMP 워크스페이스 미지정(--amp-workspace-id)")
        if not readings and ev.instances:
            try:
                readings = self._cw_readings(dep.engine, ev.instances, windows)
                self._ok("CloudWatch DB 지표(대체)")
            except ERRORS as e:
                self._fail("CloudWatch DB 지표(대체)", e)
        ev.readings = readings
        return ev
