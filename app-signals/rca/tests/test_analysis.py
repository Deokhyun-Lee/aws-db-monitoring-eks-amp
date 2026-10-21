"""RCA 분석 로직 테스트 (AWS 호출 없음).

각 테스트는 장애 시나리오(scenarios/chaos.sh)에서 기대하는 '지표 모양'을 가짜 collector로 만들고,
diagnose()가 원인을 맞게 짚는지 본다. 값은 시나리오 설계에서 기대하는 방향을 흉내 낸 합성값이며 측정값이 아니다.

    python -m unittest discover -s app-signals/rca/tests -v
"""
import datetime as dt
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from botocore.exceptions import ClientError  # noqa: E402

from analysis import (DbEvidence, Dependency, EngineReading, Golden, SpanStat, Stat, Window,  # noqa: E402
                      Windows, diagnose, hour_bounds, parse_identifier, scale_cpu, service_name, walk)
from collector import AwsCollector  # noqa: E402
from report import markdown, to_json  # noqa: E402

ENV = "eks:test-eks/shop"
T0 = dt.datetime(2026, 10, 22, 5, 0, tzinfo=dt.timezone.utc)
WINDOWS = Windows(baseline=Window(T0, T0 + dt.timedelta(minutes=15)),
                  incident=Window(T0 + dt.timedelta(minutes=15), T0 + dt.timedelta(minutes=30)))


def golden(p99, avg, rpm, fault=(0.0, 0.0), cpu=(None, None), src=""):
    return Golden(latency_p99=Stat(*p99), latency_avg=Stat(*avg), requests=Stat(*rpm), fault_rate=Stat(*fault),
                  error_rate=Stat(0.0, 0.0), cpu=Stat(*cpu), cpu_source=src)


def dep(op, remote, rop, kind, engine=None, host=None, avg=(1.0, 1.0), p99=(2.0, 2.0), calls=(100.0, 100.0),
        fault=(0.0, 0.0), env=None):
    return Dependency(caller_operation=op, remote_service=remote, remote_operation=rop, kind=kind, engine=engine,
                      host=host, environment=env, latency_avg=Stat(*avg), latency_p99=Stat(*p99),
                      calls=Stat(*calls), fault_rate=Stat(*fault))


def reading(key, family, b, i, better="lower", abs_threshold=None, source="amp"):
    return EngineReading(key, key, family, Stat(b, i), better=better, abs_threshold=abs_threshold, source=source)


class FakeCollector:
    def __init__(self, services, evidence=None, spans=None):
        self.services = services          # name -> (Golden, [Dependency])
        self.evidence = evidence or {}    # (engine) -> readings
        self.spans = spans or {}          # service -> [SpanStat]
        self.sources = {"fake": "사용"}

    def golden(self, env, service, op, windows):
        return self.services[service][0]

    def dependencies(self, env, service, op, windows):
        return [d for d in self.services[service][1] if not op or d.caller_operation == op]

    def top_db_spans(self, service, windows):
        return self.spans.get(service, [])

    def xray_root_causes(self, service, windows):
        return []

    def db_evidence(self, d, windows):
        readings = self.evidence.get(d.engine)
        ev = DbEvidence(dependency=d, instances=[f"{d.engine}-writer"], resolved_by="rds-cluster",
                        readings=readings or [])
        if readings is None:
            ev.missing.append("AMP에 엔진 지표 시계열 없음")
        return ev


MYSQL_HOST = "shop-mysql.cluster-abc.ap-northeast-2.rds.amazonaws.com"


class ScenarioTests(unittest.TestCase):
    def run_rca(self, collector, service="order-api", op=None):
        root = walk(collector, ENV, service, op, WINDOWS)
        hyps = diagnose(root, WINDOWS)
        # 보고서가 깨지지 않는지도 함께 본다
        self.assertIn("## 결론", markdown(root, hyps, WINDOWS, collector.sources))
        self.assertIn('"hypotheses"', to_json(root, hyps, WINDOWS, collector.sources))
        return root, hyps

    def test_mysql_lock(self):
        c = FakeCollector(
            services={"order-api": (golden((120, 5200), (60, 2400), (300, 290), fault=(0.0, 14.0)), [
                dep("POST /orders", "mysql", "UPDATE", "db", "mysql", MYSQL_HOST, avg=(3, 2300), p99=(8, 5000),
                    fault=(0, 12)),
                dep("POST /orders", "inventory-service", "POST /reserve", "service", avg=(12, 14), env=ENV),
                dep("GET /products/<sku>", "redis", "GET", "db", "redis", "master.shop.cache.amazonaws.com"),
            ])},
            evidence={"mysql": [reading("row_lock_waits", "lock", 0.01, 1.9, abs_threshold=0.2),
                                reading("threads_running", "saturation", 3, 9)],
                      "redis": [reading("hit_ratio", "cache", 0.95, 0.94, better="higher")]},
            spans={"order-api": [SpanStat("mysql", "SELECT value FROM order_counters WHERE id = 1 FOR UPDATE", 40, 7900, 8000),
                                 SpanStat("mysql", "UPDATE order_counters SET value = value + 1 WHERE id = 1", 900, 2300, 5000, 110)]},
        )
        _, hyps = self.run_rca(c)
        self.assertEqual(hyps[0].cause, "lock")
        self.assertEqual(hyps[0].confidence, "높음")
        self.assertIn("트레이스", hyps[0].layers)
        self.assertTrue(any("FOR UPDATE" in e for e in hyps[0].evidence))

    def test_pg_slow_query_through_inventory(self):
        c = FakeCollector(
            services={
                "order-api": (golden((150, 900), (70, 420), (300, 300)), [
                    dep("POST /orders", "inventory-service", "POST /reserve", "service", avg=(15, 380), env=ENV),
                    dep("POST /orders", "mysql", "UPDATE", "db", "mysql", MYSQL_HOST)]),
                "inventory-service": (golden((30, 800), (12, 380), (300, 300)), [
                    dep("POST /reserve", "postgresql", "SELECT", "db", "postgresql", "inv.cluster-x.rds.amazonaws.com",
                        avg=(2, 360), p99=(5, 700))]),
            },
            evidence={"postgresql": [reading("seq_tup_read", "scan", 1000, 900000), reading("tps", "load", 50, 52)],
                      "mysql": [reading("row_lock_waits", "lock", 0.0, 0.0, abs_threshold=0.2)]},
        )
        root, hyps = self.run_rca(c)
        self.assertEqual(root.child.service, "inventory-service")
        self.assertEqual(hyps[0].cause, "scan")
        self.assertIn("inventory-service", " ".join(hyps[0].chain))

    def test_mssql_blocking_without_engine_metrics(self):
        c = FakeCollector(
            services={
                "order-api": (golden((150, 5400), (70, 2600), (300, 300), fault=(0, 30)), [
                    dep("POST /orders", "payment-service", "POST /payments", "service", avg=(20, 2500),
                        fault=(0, 30), env=ENV)]),
                "payment-service": (golden((25, 5000), (10, 2400), (100, 100), fault=(0, 32)), [
                    dep("POST /payments", "mssql", "UPDATE", "db", "mssql", "pay.xxxx.rds.amazonaws.com",
                        avg=(4, 2400), p99=(9, 5000), fault=(0, 32))]),
            },
            evidence={"mssql": None},   # custom-metrics 미적용 → 엔진 지표 없음
            spans={"payment-service": [SpanStat("mssql", "UPDATE accounts SET balance = balance + @amount ...", 500, 2400, 5000, 160)]},
        )
        root, hyps = self.run_rca(c)
        self.assertEqual(root.child.service, "payment-service")
        self.assertEqual(hyps[0].cause, "db_wait_unconfirmed")
        self.assertEqual(hyps[0].confidence, "중간")
        self.assertTrue(any("엔진 지표 없음" in e for e in hyps[0].evidence))

    def test_cache_stampede(self):
        c = FakeCollector(
            services={
                "order-api": (golden((40, 600), (8, 210), (180, 180)), [
                    dep("GET /products/<sku>", "redis", "GET", "db", "redis", "master.shop.cache.amazonaws.com",
                        avg=(1, 1.2)),
                    dep("GET /products/<sku>", "inventory-service", "GET /stock/<sku>", "service",
                        avg=(14, 200), calls=(10, 175), env=ENV)]),
                "inventory-service": (golden((30, 400), (12, 190), (10, 175)), [
                    dep("GET /stock/<sku>", "postgresql", "SELECT", "db", "postgresql", "inv.cluster-x.rds.amazonaws.com",
                        avg=(2, 30), calls=(20, 350))]),
            },
            evidence={"redis": [reading("hit_ratio", "cache", 0.96, 0.08, better="higher"),
                                reading("misses", "cache", 0.3, 2.9)],
                      "postgresql": [reading("tps", "load", 40, 260), reading("seq_tup_read", "scan", 100, 120)]},
        )
        _, hyps = self.run_rca(c)
        self.assertEqual(hyps[0].cause, "cache_miss")
        load = [h for h in hyps if h.cause == "load"]
        self.assertTrue(load and "결과" in load[0].title)

    def test_app_cpu_is_not_db(self):
        c = FakeCollector(
            services={
                "order-api": (golden((150, 700), (70, 330), (300, 300)), [
                    dep("POST /orders", "inventory-service", "POST /reserve", "service", avg=(15, 280), env=ENV)]),
                "inventory-service": (golden((30, 600), (12, 270), (300, 300), cpu=(0.15, 0.95), src="Python 런타임"), [
                    dep("POST /reserve", "postgresql", "UPDATE", "db", "postgresql", "inv.cluster-x.rds.amazonaws.com",
                        avg=(2, 2.3)),
                    dep("POST /reserve", "postgresql", "SELECT", "db", "postgresql", "inv.cluster-x.rds.amazonaws.com",
                        avg=(2, 2.1))]),
            },
            evidence={"postgresql": [reading("seq_tup_read", "scan", 1000, 1100)]},
        )
        _, hyps = self.run_rca(c)
        self.assertEqual(hyps[0].cause, "app_cpu")
        self.assertIn("inventory-service", " ".join(hyps[0].chain))

    def test_no_change(self):
        c = FakeCollector(services={"order-api": (golden((120, 125), (60, 61), (300, 300)), [
            dep("POST /orders", "mysql", "UPDATE", "db", "mysql", MYSQL_HOST)])})
        _, hyps = self.run_rca(c)
        self.assertEqual(hyps[0].cause, "unknown")


class ReviewRegressionTests(unittest.TestCase):
    def test_pg_scan_with_more_locks_is_still_scan(self):
        # 느린 쿼리로 동시 실행이 늘어 보유 잠금 수도 오르지만, 원인은 잠금이 아니라 스캔이어야 한다
        c = FakeCollector(
            services={"inventory-service": (golden((30, 800), (12, 380), (300, 300)), [
                dep("POST /reserve", "postgresql", "SELECT", "db", "postgresql", "inv.cluster-x.rds.amazonaws.com",
                    avg=(2, 360), p99=(5, 700))])},
            evidence={"postgresql": [reading("locks", "saturation", 20, 90), reading("seq_tup_read", "scan", 1000, 900000)]})
        hyps = diagnose(walk(c, ENV, "inventory-service", None, WINDOWS), WINDOWS)
        self.assertEqual(hyps[0].cause, "scan")

    def test_child_operation_fallback(self):
        # 호출하는 쪽의 RemoteOperation(GET /stock)으로 하위 서비스 데이터가 없으면 서비스 전체로 본다
        class OpCollector(FakeCollector):
            def golden(self, env, service, op, windows):
                if service == "inventory-service" and op == "GET /stock":
                    return Golden()
                return super().golden(env, service, op, windows)
        c = OpCollector(services={
            "order-api": (golden((40, 300), (8, 120), (180, 180)), [
                dep("GET /products/<sku>", "inventory-service", "GET /stock", "service", avg=(4, 110), env=ENV)]),
            "inventory-service": (golden((10, 250), (4, 100), (180, 180)), [
                dep("GET /stock/<sku>", "postgresql", "SELECT", "db", "postgresql", "inv.cluster-x.rds.amazonaws.com",
                    avg=(2, 95))])},
            evidence={"postgresql": [reading("seq_tup_read", "scan", 100, 50000)]})
        root = walk(c, ENV, "order-api", None, WINDOWS)
        self.assertIsNone(root.child.operation)
        self.assertEqual(diagnose(root, WINDOWS)[0].cause, "scan")

    def test_hour_bounds(self):
        s, e = hour_bounds(dt.datetime(2026, 10, 22, 5, 40, tzinfo=dt.timezone.utc),
                           dt.datetime(2026, 10, 22, 5, 59, tzinfo=dt.timezone.utc))
        self.assertEqual((s.hour, e.hour), (5, 6))
        s, e = hour_bounds(dt.datetime(2026, 10, 22, 5, 0, tzinfo=dt.timezone.utc),
                           dt.datetime(2026, 10, 22, 7, 0, tzinfo=dt.timezone.utc))
        self.assertEqual((s.hour, e.hour), (5, 7))

    def test_scale_cpu_same_factor(self):
        # 기준 1.2%, 사고 35% (백분율로 온 값) → 둘 다 /100. 구간마다 따로 판단하면 배수가 뒤집힌다
        b, i = scale_cpu([1.2, 1.2], [35.0, 35.0])
        self.assertAlmostEqual(b, 0.012)
        self.assertAlmostEqual(i, 0.35)
        b, i = scale_cpu([0.02], [0.4])        # 비율로 온 값은 그대로
        self.assertAlmostEqual(i / b, 20.0)
        b, i = scale_cpu([12.0], [30.0], percent=True)
        self.assertAlmostEqual(i, 0.30)


class IdentifierTests(unittest.TestCase):
    def test_db_host_port(self):
        self.assertEqual(parse_identifier("shop|db.cluster-1.rds.amazonaws.com|3306"),
                         ("shop", "db.cluster-1.rds.amazonaws.com"))

    def test_host_port(self):
        self.assertEqual(parse_identifier("master.c.cache.amazonaws.com|6379"), (None, "master.c.cache.amazonaws.com"))

    def test_escaped(self):
        self.assertEqual(parse_identifier("we^|ird|h.example|5432"), ("we|ird", "h.example"))

    def test_service_name(self):
        # ADOT는 HTTP 호출의 원격 서비스를 URL의 host:port로 남긴다 (실제 EKS 트레이스에서 FQDN:8080 확인)
        for remote, want in [("inventory-service:8080", "inventory-service"),
                             ("inventory-service.shop.svc.cluster.local:8080", "inventory-service"),
                             ("inventory-service.shop.svc:8080", "inventory-service"),
                             ("api.example.com:443", "api.example.com"), ("postgresql", "postgresql")]:
            self.assertEqual(service_name(remote), want, remote)


class _Client:
    """AwsCollector가 부르는 boto3 클라이언트 흉내 (부른 메서드만)."""

    def __init__(self, **methods):
        self.__dict__.update(methods)


class CollectorDependencyTests(unittest.TestCase):
    """ListServiceDependencies 응답 → Dependency.kind / remote_service (하위 서비스로 내려갈지)."""

    REMOTE = "inventory-service.shop.svc.cluster.local:8080"

    def collector(self, list_services):
        dims = [{"Name": "Environment", "Value": ENV}, {"Name": "Service", "Value": "order-api"},
                {"Name": "Operation", "Value": "POST /orders"}, {"Name": "RemoteService", "Value": self.REMOTE},
                {"Name": "RemoteOperation", "Value": "POST /reserve"}]
        item = {"OperationName": "POST /orders", "DependencyOperationName": "POST /reserve",
                "DependencyKeyAttributes": {"Type": "RemoteService", "Name": self.REMOTE},
                "MetricReferences": [{"Namespace": "ApplicationSignals", "MetricName": "Latency", "Dimensions": dims}]}
        appsig = _Client(list_service_dependencies=lambda **kw: {"ServiceDependencies": [item]},
                         list_services=list_services)

        def metric_data(**kw):
            return {"MetricDataResults": [{"Id": q["Id"], "Values": []} for q in kw["MetricDataQueries"]]}

        clients = {"application-signals": appsig, "cloudwatch": _Client(get_metric_data=metric_data)}
        session = _Client(client=lambda name: clients.get(name, _Client()))
        return AwsCollector("ap-northeast-2", session=session)

    def test_unlinked_remote_matches_instrumented_service(self):
        summaries = [{"KeyAttributes": {"Type": "Service", "Name": "inventory-service", "Environment": ENV},
                      "AttributeMaps": [{"InstrumentationType": "INSTRUMENTED"}]},
                     {"KeyAttributes": {"Type": "Service", "Name": "inventory-service", "Environment": "eks:x/y"}}]
        c = self.collector(lambda **kw: {"ServiceSummaries": summaries})
        (d,) = c.dependencies(ENV, "order-api", "POST /orders", WINDOWS)
        self.assertEqual((d.kind, d.remote_service, d.remote_operation), ("service", "inventory-service", "POST /reserve"))

    def test_without_service_list_it_stays_other(self):
        def denied(**kw):
            raise ClientError({"Error": {"Code": "AccessDeniedException", "Message": "no"}}, "ListServices")

        c = self.collector(denied)
        (d,) = c.dependencies(ENV, "order-api", "POST /orders", WINDOWS)
        self.assertEqual(d.kind, "other")
        self.assertIn("사용 못 함", c.sources["Application Signals 서비스 목록"])


if __name__ == "__main__":
    unittest.main()
