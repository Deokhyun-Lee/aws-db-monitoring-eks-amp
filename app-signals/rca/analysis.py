"""RCA 분석 로직 (AWS 호출 없음 — collector가 모아 온 값만 다룬다).

흐름
  1) 진입 서비스의 골든 지표(지연·트래픽·오류·포화)를 사고 구간과 기준 구간으로 비교
  2) 의존성(다른 서비스·DB)을 지연·Fault 증가량으로 순위 매김
  3) 가장 크게 나빠진 의존성이 계측된 다른 서비스면 그 서비스로 내려가 1~2를 반복 (max_depth까지)
  4) 경로 위의 DB 의존성마다 엔진 지표(잠금·스캔·포화·캐시)를 같은 두 구간으로 비교
  5) 규칙으로 원인 가설을 만들고, 근거 계층 수(앱 지표·의존성 지표·트레이스·엔진 지표)로 확신도를 매김
"""
import datetime as dt
import re
from dataclasses import dataclass, field
from typing import Optional

from signals import ENGINE_ALIASES

# 판단 기준 (README에 같은 값을 적어 둠)
MIN_LATENCY_DELTA_MS = 20.0      # 의존성 평균 지연이 이만큼 이상 늘어야 '나빠짐'
MIN_LATENCY_RATIO = 1.5          # 그리고 기준 대비 1.5배 이상
MIN_FAULT_DELTA_PP = 1.0         # Fault 비율 1%p 이상 증가
CALL_SURGE_RATIO = 3.0           # 호출 수 3배 이상이면 '호출 급증'
ENGINE_WORSE_RATIO = 2.0         # 엔진 지표 2배 이상 나빠짐
APP_SIDE_SHARE = 0.3             # 서비스 지연 증가 중 의존성이 설명하는 몫이 30% 미만이면 앱 자체 의심
CPU_WORSE_RATIO = 1.5


@dataclass
class Window:
    start: object
    end: object

    @property
    def seconds(self):
        return (self.end - self.start).total_seconds()


@dataclass
class Windows:
    baseline: Window
    incident: Window


@dataclass
class Stat:
    baseline: Optional[float] = None
    incident: Optional[float] = None

    @property
    def known(self):
        return self.baseline is not None and self.incident is not None

    @property
    def delta(self):
        return (self.incident - self.baseline) if self.known else None

    @property
    def ratio(self):
        if not self.known:
            return None
        if self.baseline == 0:
            return float("inf") if self.incident > 0 else 1.0
        return self.incident / self.baseline


@dataclass
class Golden:
    latency_p99: Stat = field(default_factory=Stat)    # ms
    latency_avg: Stat = field(default_factory=Stat)    # ms
    requests: Stat = field(default_factory=Stat)       # 분당 요청 수
    fault_rate: Stat = field(default_factory=Stat)     # %
    error_rate: Stat = field(default_factory=Stat)     # %
    cpu: Stat = field(default_factory=Stat)            # 0~1 (Python 런타임 또는 Container Insights)
    cpu_source: str = ""


@dataclass
class Dependency:
    caller_operation: str
    remote_service: str
    remote_operation: str
    kind: str                      # service | db | aws | other
    engine: Optional[str] = None   # mysql | postgresql | mssql | redis
    host: Optional[str] = None
    db_name: Optional[str] = None
    environment: Optional[str] = None
    latency_avg: Stat = field(default_factory=Stat)
    latency_p99: Stat = field(default_factory=Stat)
    calls: Stat = field(default_factory=Stat)          # 분당 호출 수
    fault_rate: Stat = field(default_factory=Stat)

    @property
    def label(self):
        return f"{self.remote_service} {self.remote_operation}".strip()

    @property
    def latency_worse(self):
        s = self.latency_avg
        return s.known and s.delta >= MIN_LATENCY_DELTA_MS and (s.ratio or 0) >= MIN_LATENCY_RATIO

    @property
    def fault_worse(self):
        return self.fault_rate.known and self.fault_rate.delta >= MIN_FAULT_DELTA_PP

    @property
    def call_surge(self):
        return self.calls.known and self.calls.incident >= 1 and (self.calls.ratio or 0) >= CALL_SURGE_RATIO

    @property
    def impact(self):
        """순위 점수: 평균 지연 증가(ms) + Fault 증가(%p) × 50 + 호출 급증 가산."""
        score = max(0.0, self.latency_avg.delta or 0.0) + 50.0 * max(0.0, self.fault_rate.delta or 0.0)
        if self.call_surge:
            score += 25.0 * min(self.calls.ratio, 20.0)
        return score

    @property
    def worse(self):
        return self.latency_worse or self.fault_worse or self.call_surge


@dataclass
class EngineReading:
    key: str
    label: str
    family: str
    stat: Stat
    better: str = "lower"
    unit: str = ""
    abs_threshold: Optional[float] = None
    source: str = "amp"      # amp | cloudwatch
    scope: str = "instance"  # instance | engine (인스턴스 라벨로 못 찾아 엔진 전체)

    @property
    def worse_factor(self):
        """나빠진 정도(배수). 1이면 변화 없음."""
        s = self.stat
        if not s.known:
            return None
        b, i = s.baseline, s.incident
        if self.better == "higher":
            if i <= 0:
                return float("inf") if b > 0 else 1.0
            return b / i if b > 0 else 1.0
        if b <= 0:
            if self.abs_threshold is not None and i >= self.abs_threshold:
                return float("inf")
            return 1.0
        return i / b

    @property
    def worse(self):
        f = self.worse_factor
        if f is None:
            return False
        if self.abs_threshold is not None and self.better == "lower" and (self.stat.incident or 0) < self.abs_threshold:
            return False
        return f >= ENGINE_WORSE_RATIO


@dataclass
class DbEvidence:
    dependency: Dependency
    instances: list = field(default_factory=list)
    resolved_by: str = ""                  # rds-cluster | rds-instance | elasticache | host-only
    readings: list = field(default_factory=list)
    missing: list = field(default_factory=list)   # 못 가져온 출처 설명

    def families_worse(self):
        out = {}
        for r in self.readings:
            if r.worse:
                out.setdefault(r.family, []).append(r)
        return out


@dataclass
class SpanStat:
    db_system: str
    statement: str
    calls: int
    avg_ms: float
    p99_ms: float
    errors: int = 0


@dataclass
class ServiceNode:
    service: str
    environment: str
    operation: Optional[str]
    golden: Golden
    dependencies: list = field(default_factory=list)
    spans: list = field(default_factory=list)         # SpanStat (Transaction Search)
    xray_root_causes: list = field(default_factory=list)  # (entity, count)
    db_evidence: list = field(default_factory=list)   # DbEvidence (이 서비스가 직접 부르는 DB)
    child: Optional["ServiceNode"] = None
    via: Optional[Dependency] = None                  # child로 내려간 의존성

    def ranked(self):
        return sorted(self.dependencies, key=lambda d: d.impact, reverse=True)

    def top(self):
        worse = [d for d in self.ranked() if d.worse]
        return worse[0] if worse else None


@dataclass
class Hypothesis:
    cause: str            # lock | scan | saturation | cache_miss | load | app_cpu | db_wait_unconfirmed | downstream | unknown
    title: str
    confidence: str       # 높음 | 중간 | 낮음
    layers: list          # 근거 계층 이름
    chain: list           # 앱 → DB 경로 표기
    evidence: list        # 근거 문장
    actions: list
    score: float = 0.0


def parse_identifier(ident):
    """RemoteResourceIdentifier '[db_name|]address[|port]' (구분자 '|', 이스케이프 '^|' '^^') → (db_name, host)."""
    parts, cur, i = [], "", 0
    while i < len(ident):
        c = ident[i]
        if c == "^" and i + 1 < len(ident):
            cur += ident[i + 1]
            i += 2
            continue
        if c == "|":
            parts.append(cur)
            cur = ""
        else:
            cur += c
        i += 1
    parts.append(cur)
    if len(parts) > 1 and parts[-1].isdigit():
        parts.pop()
    if len(parts) >= 2:
        return parts[0], parts[1]
    return None, parts[0] if parts else None


_PORT = re.compile(r":\d+$")
_K8S_DNS = re.compile(r"^([a-z0-9](?:[-a-z0-9]*[a-z0-9])?)\.[a-z0-9-]+\.svc(?:\.cluster\.local)?\.?$")


def service_name(remote):
    """HTTP 호출의 원격 서비스 이름 → 하위 서비스 이름. ADOT는 URL의 host:port를 그대로 남기므로
    (실제 EKS 트레이스: '<svc>.<ns>.svc.cluster.local:8080') 포트와 쿠버네티스 DNS 접미사를 뗀다."""
    host = _PORT.sub("", remote or "")
    m = _K8S_DNS.match(host)
    return m.group(1) if m else host


def hour_bounds(start, end):
    """ListServiceDependencies는 StartTime·EndTime을 가장 가까운 정시로 반올림한다(API 문서).
    짧은 구간이 같은 정시로 접히지 않게 시작은 내림, 끝은 올림한 정시로 넓혀 목록만 받는다(지표는 정확한 구간으로 조회)."""
    s = start.replace(minute=0, second=0, microsecond=0)
    e = end.replace(minute=0, second=0, microsecond=0)
    if e < end:
        e += dt.timedelta(hours=1)
    if e <= s:
        e = s + dt.timedelta(hours=1)
    return s, e


def scale_cpu(baseline_vals, incident_vals, percent=None):
    """두 구간의 CPU 값을 같은 배율로 0~1로 맞춘다. percent=True면 항상 /100,
    None이면 두 구간을 합친 최댓값이 1.5를 넘을 때만 백분율로 본다(구간마다 따로 판단하면 배율이 갈린다)."""
    allv = [v for v in baseline_vals + incident_vals if v is not None]
    if not allv:
        return None, None
    div = 100.0 if (percent or (percent is None and max(allv) > 1.5)) else 1.0
    mean = lambda v: (sum(v) / len(v) / div) if v else None  # noqa: E731
    return mean(baseline_vals), mean(incident_vals)


# ---------------------------------------------------------------- 탐색
def walk(collector, environment, service, operation, windows, max_depth=3, _depth=0, _seen=None):
    """서비스 노드를 만들고, 가장 크게 나빠진 의존성이 다른 서비스면 따라 내려간다."""
    seen = _seen or set()
    seen.add(service)
    golden = collector.golden(environment, service, operation, windows)
    if operation and not golden.requests.incident:
        # 호출하는 쪽의 RemoteOperation(예: GET /stock)과 하위 서비스의 오퍼레이션(GET /stock/<sku>)이
        # 다를 수 있다. 그 이름으로 데이터가 없으면 서비스 전체로 본다
        operation = None
        golden = collector.golden(environment, service, None, windows)
    node = ServiceNode(service=service, environment=environment, operation=operation, golden=golden)
    node.dependencies = collector.dependencies(environment, service, operation, windows)
    node.spans = collector.top_db_spans(service, windows)
    node.xray_root_causes = collector.xray_root_causes(service, windows)
    # DB 근거는 엔진·호스트마다 한 번 (같은 DB에 오퍼레이션이 여럿이면 가장 크게 나빠진 호출로 대표)
    by_db = {}
    for dep in node.dependencies:
        slower = (dep.latency_avg.ratio or 0) >= MIN_LATENCY_RATIO
        if dep.kind == "db" and (dep.worse or slower or dep.engine == "redis"):
            key = (dep.engine, dep.host)
            if key not in by_db or (dep.impact, dep.calls.incident or 0) > (by_db[key].impact, by_db[key].calls.incident or 0):
                by_db[key] = dep
    node.db_evidence = [collector.db_evidence(dep, windows) for dep in by_db.values()]
    top = node.top()
    if top and top.kind == "service" and _depth + 1 < max_depth and top.remote_service not in seen:
        node.via = top
        node.child = walk(collector, top.environment or environment, top.remote_service, top.remote_operation or None,
                          windows, max_depth, _depth + 1, seen)
    return node


def path(node):
    out = []
    while node:
        out.append(node)
        node = node.child
    return out


# ---------------------------------------------------------------- 진단
ACTIONS = {
    "lock": [
        "잠금을 오래 쥔 세션 찾기 — MySQL: performance_schema.data_lock_waits·information_schema.innodb_trx, "
        "PostgreSQL: pg_locks + pg_stat_activity(wait_event_type=Lock), SQL Server: sys.dm_exec_requests의 blocking_session_id",
        "트레이스에서 가장 오래 걸린 문장(특히 SELECT … FOR UPDATE, 긴 트랜잭션)을 낸 코드 경로 확인",
        "핫 로우 갱신 분산(카운터 샤딩·시퀀스로 대체)과 트랜잭션 범위 축소, 잠금 대기 시간 상한 점검",
    ],
    "scan": [
        "느린 문장의 실행 계획 확인 — PostgreSQL: EXPLAIN (ANALYZE, BUFFERS), MySQL: EXPLAIN ANALYZE",
        "선행 와일드카드 LIKE·함수로 감싼 컬럼처럼 인덱스를 못 쓰는 조건 수정, 필요한 인덱스 추가",
        "최근 배포에서 바뀐 쿼리인지 트레이스의 statement로 대조",
    ],
    "saturation": [
        "연결 풀 크기와 DB max_connections 대조, 앱 Pod 수 증가로 연결이 불어났는지 확인",
        "실행 중 세션의 대기 이벤트 확인 후 오래 걸리는 쿼리부터 정리",
    ],
    "cache_miss": [
        "캐시 무효화·TTL 변경·키 삭제 작업이 있었는지 확인(배포·운영 작업 기록)",
        "캐시 미스 폭주 보호: 요청 병합(single-flight), TTL 지터, 오래된 값 제공(stale-while-revalidate)",
        "캐시 뒤 DB의 처리량 여유 확인",
    ],
    "load": [
        "호출량이 늘어난 상위 서비스·오퍼레이션 확인(트래픽 증가인지 재시도 폭증인지)",
        "재시도 정책과 타임아웃 점검, 필요하면 읽기를 reader 엔드포인트로 분산",
    ],
    "app_cpu": [
        "DB 의존성 지연은 그대로라 DB가 원인이 아닙니다 — 서비스 Pod CPU·스로틀링과 최근 배포 변경 확인",
        "CPU limit·HPA 설정과 핫 코드 경로 프로파일링",
    ],
    "db_wait_unconfirmed": [
        "DB 엔진 지표가 없어 원인을 확정하지 못했습니다 — exporter 수집 상태와 instance 라벨 확인",
        "SQL Server라면 custom-metrics 모듈(suspended 요청·LCK 대기)을 적용하면 잠금 여부를 지표로 확인할 수 있습니다",
        "지연이 클라이언트 타임아웃 값에 붙어 있으면 잠금·차단 대기를 먼저 의심",
    ],
    "downstream": [
        "하위 서비스의 의존성(DB·외부 호출)과 배포 이력 확인",
    ],
    "unknown": [
        "구간을 넓혀 다시 실행하거나(--minutes), 기준 구간을 바꿔(--baseline-minutes) 비교",
    ],
}

TITLES = {
    "lock": "잠금 경합",
    "scan": "비효율 쿼리(전체 스캔)",
    "saturation": "DB 연결·세션 포화",
    "cache_miss": "캐시 미스 폭증으로 하위 DB 부하",
    "load": "호출량 증가로 DB 부하",
    "app_cpu": "애플리케이션 CPU 포화 (DB 원인 아님)",
    "db_wait_unconfirmed": "DB 호출 대기 — 엔진 지표 근거 없음",
    "downstream": "하위 서비스 장애",
    "unknown": "원인 미확정",
}


def fmt(v, unit=""):
    if v is None:
        return "?"
    if v == float("inf"):
        return "∞"
    if unit == "ratio":
        return f"{v * 100:.1f}%"
    if abs(v) >= 100:
        s = f"{v:,.0f}"
    elif abs(v) >= 10:
        s = f"{v:.1f}"
    else:
        s = f"{v:.2f}"
    return f"{s}{unit if unit not in ('ratio',) else ''}"


def change(stat, unit=""):
    return f"{fmt(stat.baseline, unit)} → {fmt(stat.incident, unit)}"


def _span_for(node, dep):
    """의존성과 같은 DB 시스템의 문장 중 p99가 가장 큰 것."""
    cand = [s for s in node.spans if dep.engine and _engine_of(s.db_system) == dep.engine]
    return max(cand, key=lambda s: s.p99_ms) if cand else None


def _engine_of(db_system):
    return ENGINE_ALIASES.get((db_system or "").lower())


def _confidence(layers):
    n = len(layers)
    if n >= 3 and "엔진 지표" in layers:
        return "높음"
    if n >= 2:
        return "중간"
    return "낮음"


def diagnose(root, windows=None):
    nodes = path(root)
    terminal = nodes[-1]
    hypotheses = []
    chain = []
    for n in nodes:
        g = n.golden
        chain.append(f"{n.service}{' ' + n.operation if n.operation else ''} "
                     f"(p99 {change(g.latency_p99, 'ms')}, Fault {change(g.fault_rate, '%')})")

    # 앱 계층 근거: 진입 서비스의 골든 지표가 나빠졌는가
    rg = root.golden
    app_worse = ((rg.latency_p99.known and (rg.latency_p99.ratio or 0) >= MIN_LATENCY_RATIO)
                 or (rg.fault_rate.known and rg.fault_rate.delta >= MIN_FAULT_DELTA_PP))

    # 1) 경로 위 DB 의존성 각각에 대해 엔진 근거로 가설
    for n in nodes:
        for ev in n.db_evidence:
            dep = ev.dependency
            worse = ev.families_worse()
            layers = ["앱 골든 지표"] if app_worse else []
            evidence = []
            if dep.worse:
                layers.append("의존성 지표")
                evidence.append(f"{n.service} → {dep.label}: 평균 지연 {change(dep.latency_avg, 'ms')}, "
                                f"Fault {change(dep.fault_rate, '%')}, 호출 {change(dep.calls, '/분')}")
            span = _span_for(n, dep)
            if span and (span.p99_ms >= 2 * max(dep.latency_avg.baseline or 0, 1) or span.errors):
                layers.append("트레이스")
                evidence.append(f"가장 느린 {span.db_system} 문장(p99 {fmt(span.p99_ms, 'ms')}, 오류 {span.errors}건): "
                                f"`{span.statement[:160]}`")
            cause = None
            for fam, c in (("lock", "lock"), ("scan", "scan"), ("cache", "cache_miss"),
                           ("saturation", "saturation"), ("load", "load")):
                if fam in worse:
                    cause = c
                    layers.append("엔진 지표")
                    for r in worse[fam][:3]:
                        scope = "" if r.scope == "instance" else " (인스턴스 구분 없이 엔진 전체)"
                        evidence.append(f"{r.label}: {change(r.stat, r.unit)} [{r.source}{scope}]")
                    break
            if cause is None and dep.worse:
                cause = "db_wait_unconfirmed"
                if ev.missing:
                    evidence.append("엔진 지표 없음: " + "; ".join(ev.missing))
                if any(r.stat.known for r in ev.readings):
                    evidence.append("수집된 엔진 지표는 기준 구간과 비슷함(잠금·스캔·포화 신호 없음) → 네트워크·연결 풀 대기 등 클라이언트 쪽도 확인")
            if cause is None:
                continue
            if cause == "cache_miss":
                # 캐시가 나빠졌을 때 같은 서비스에서 호출이 급증한 의존성을 함께 짚는다
                surged = [d for d in n.dependencies if d.call_surge and d.engine != dep.engine]
                for d in surged[:2]:
                    evidence.append(f"캐시 뒤 호출 급증: {n.service} → {d.label} 호출 {change(d.calls, '/분')}")
                if surged and "의존성 지표" not in layers:
                    layers.append("의존성 지표")
            target = (f"{dep.remote_service} {dep.remote_operation} on "
                      f"{', '.join(ev.instances) or dep.host or '?'}").strip()
            score = dep.impact + (500 if "엔진 지표" in layers else 0) + 100 * len(layers)
            hypotheses.append(Hypothesis(
                cause=cause, title=TITLES[cause], confidence=_confidence(layers), layers=layers,
                chain=chain[: nodes.index(n) + 1] + [target], evidence=evidence, actions=ACTIONS[cause], score=score))

    # 2) 앱 자체 CPU: 서비스 지연은 늘었는데 의존성이 설명하지 못하고 CPU가 오름
    for n in nodes:
        g = n.golden
        if not (g.latency_avg.known and g.latency_avg.delta and g.latency_avg.delta > MIN_LATENCY_DELTA_MS):
            continue
        explained = max([max(0.0, d.latency_avg.delta or 0.0) for d in n.dependencies] or [0.0])
        share = explained / g.latency_avg.delta
        # 배율(노드 대비 %·코어 수로 나눈 값)이 출처마다 달라 절대값 대신 기준 대비 배수만 본다
        cpu_up = g.cpu.known and (g.cpu.ratio or 0) >= CPU_WORSE_RATIO
        if share < APP_SIDE_SHARE and cpu_up:
            layers = (["앱 골든 지표"] if app_worse else []) + ["의존성 지표(변화 없음)", "런타임 CPU"]
            evidence = [
                f"{n.service} 평균 지연 {change(g.latency_avg, 'ms')}인데 의존성 지연 증가는 최대 {fmt(explained, 'ms')}"
                f" (설명 비율 {share * 100:.0f}%)",
                f"CPU({g.cpu_source}) {change(g.cpu, 'ratio')}",
            ]
            hypotheses.append(Hypothesis(
                cause="app_cpu", title=TITLES["app_cpu"], confidence=_confidence(layers), layers=layers,
                chain=chain[: nodes.index(n) + 1], evidence=evidence, actions=ACTIONS["app_cpu"],
                score=g.latency_avg.delta + 300))

    # 3) 하위 서비스 Fault를 그대로 받은 경우(하위에서 원인을 못 찾았을 때만)
    if not hypotheses and len(nodes) > 1 and root.via and root.via.fault_worse:
        hypotheses.append(Hypothesis(
            cause="downstream", title=TITLES["downstream"], confidence="낮음", layers=["의존성 지표"],
            chain=chain, evidence=[f"{root.service} → {root.via.label}: Fault {change(root.via.fault_rate, '%')}"],
            actions=ACTIONS["downstream"], score=root.via.impact))

    # 캐시 미스가 있으면 그 아래 DB의 부하 증가는 결과로 본다
    if any(h.cause == "cache_miss" for h in hypotheses):
        for h in hypotheses:
            if h.cause == "load":
                h.score -= 1000
                h.title += " (캐시 미스의 결과로 보임)"

    if not hypotheses:
        top = terminal.top()
        ev = [f"가장 크게 나빠진 의존성: {top.label} (평균 지연 {change(top.latency_avg, 'ms')})"] if top else \
             ["나빠진 의존성을 찾지 못했습니다"]
        hypotheses.append(Hypothesis(cause="unknown", title=TITLES["unknown"], confidence="낮음",
                                     layers=["앱 골든 지표"] if app_worse else [], chain=chain, evidence=ev,
                                     actions=ACTIONS["unknown"]))
    hypotheses.sort(key=lambda h: ({"높음": 2, "중간": 1, "낮음": 0}[h.confidence], h.score), reverse=True)
    return hypotheses
