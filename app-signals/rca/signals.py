"""DB 엔진 증거로 볼 지표 정의.

1순위는 AMP(이 저장소의 exporter가 내는 Prometheus 지표, 알림 규칙·대시보드와 같은 이름)이고,
AMP가 없거나 해당 인스턴스 시계열이 없으면 CloudWatch 기본 지표로 대신합니다.

family(증거의 종류)
  lock        잠금 경합·차단           scan        전체 스캔·비효율 쿼리
  saturation  연결·스레드·세션 포화     cache       캐시 적중률
  load        처리량(쿼리 수)          io          디스크 읽기·버퍼
  memory      메모리·eviction          latency     DB 쪽 지연(CloudWatch)
better: 값이 커질수록 나쁘면 "lower", 작아질수록 나쁘면 "higher"(적중률·PLE)
"""
from dataclasses import dataclass


@dataclass(frozen=True)
class Signal:
    key: str
    label: str
    family: str
    query: str               # PromQL. __M__ 자리에 인스턴스 라벨 매처('db_instance=~"a|b",')가 들어감
    agg: str = "avg"         # 구간 값을 낼 때: avg | max | min
    better: str = "lower"
    unit: str = ""
    abs_threshold: float = None   # 기준 구간이 0에 가까울 때 '나빠짐'으로 볼 절대값


ENGINE_SIGNALS = {
    "mysql": [
        Signal("row_lock_waits", "InnoDB 행 잠금 대기", "lock",
               "sum(rate(mysql_global_status_innodb_row_lock_waits{__M__}[1m]))", unit="회/s", abs_threshold=0.2),
        Signal("row_lock_current", "현재 행 잠금 대기 수", "lock",
               "max(mysql_global_status_innodb_row_lock_current_waits{__M__})", agg="max", abs_threshold=1),
        Signal("row_lock_time", "행 잠금 대기 시간", "lock",
               "sum(rate(mysql_global_status_innodb_row_lock_time{__M__}[1m]))", unit="ms/s", abs_threshold=100),
        Signal("threads_running", "실행 중 스레드", "saturation",
               "max(mysql_global_status_threads_running{__M__})", agg="max"),
        Signal("conn_util", "연결 사용률", "saturation",
               "max(mysql_global_status_threads_connected{__M__}) / max(mysql_global_variables_max_connections{__M__})",
               agg="max", unit="ratio", abs_threshold=0.8),
        Signal("slow_queries", "slow query", "scan",
               "sum(rate(mysql_global_status_slow_queries{__M__}[1m]))", unit="회/s", abs_threshold=0.5),
        Signal("rows_read", "InnoDB rows read", "scan",
               "sum(rate(mysql_global_status_innodb_rows_read{__M__}[1m]))", unit="행/s"),
        Signal("qps", "쿼리 처리량", "load", "sum(rate(mysql_global_status_queries{__M__}[1m]))", unit="회/s"),
    ],
    "postgresql": [
        Signal("active", "active 세션", "saturation",
               'sum(pg_stat_activity_count{__M__state="active"})', agg="max"),
        Signal("seq_tup_read", "순차 스캔으로 읽은 행", "scan",
               "sum(rate(pg_stat_user_tables_seq_tup_read{__M__}[1m]))", unit="행/s"),
        Signal("seq_scan", "순차 스캔 횟수", "scan", "sum(rate(pg_stat_user_tables_seq_scan{__M__}[1m]))", unit="회/s"),
        Signal("tup_returned", "반환 행", "scan", "sum(rate(pg_stat_database_tup_returned{__M__}[1m]))", unit="행/s"),
        Signal("blks_read", "디스크 블록 읽기", "io", "sum(rate(pg_stat_database_blks_read{__M__}[1m]))", unit="블록/s"),
        # pg_locks_count는 모든 쿼리의 AccessShareLock까지 세므로 경합이 아니라 동시성 지표로 둔다
        Signal("locks", "보유 잠금 수(동시성)", "saturation", "sum(pg_locks_count{__M__})", agg="max"),
        Signal("deadlocks", "deadlock", "lock", "sum(rate(pg_stat_database_deadlocks{__M__}[5m]))", unit="회/s",
               abs_threshold=0.01),
        Signal("tps", "커밋 처리량", "load", "sum(rate(pg_stat_database_xact_commit{__M__}[1m]))", unit="회/s"),
    ],
    "mssql": [
        # 아래 두 개는 custom-metrics 모듈(mssql-custom-collectors.yaml)을 적용해야 수집됩니다
        Signal("suspended", "suspended 요청 (차단 대기)", "lock",
               'sum(mssql_active_requests_by_status{__M__status="suspended"})', agg="max", abs_threshold=1),
        Signal("lck_wait", "잠금(LCK_M_*) 대기 시간", "lock",
               'sum(rate(mssql_wait_stats_wait_time_ms{__M__wait_category="Lock"}[1m]))', unit="ms/s", abs_threshold=100),
        Signal("deadlocks", "deadlock", "lock", "sum(rate(mssql_deadlocks{__M__}[5m]))", unit="회/s",
               abs_threshold=0.01),
        Signal("batch_requests", "batch requests", "load", "sum(rate(mssql_batch_requests{__M__}[1m]))", unit="회/s"),
        Signal("connections", "연결 수", "saturation", "sum(mssql_connections{__M__})", agg="max"),
        Signal("ple", "page life expectancy", "io", "min(mssql_page_life_expectancy{__M__})", agg="min",
               better="higher", unit="s"),
    ],
    "redis": [
        Signal("hit_ratio", "캐시 적중률", "cache",
               "sum(rate(redis_keyspace_hits_total{__M__}[1m])) / (sum(rate(redis_keyspace_hits_total{__M__}[1m]))"
               " + sum(rate(redis_keyspace_misses_total{__M__}[1m])))", better="higher", unit="ratio"),
        Signal("misses", "캐시 미스", "cache", "sum(rate(redis_keyspace_misses_total{__M__}[1m]))", unit="회/s"),
        Signal("evictions", "eviction", "memory", "sum(rate(redis_evicted_keys_total{__M__}[1m]))", unit="회/s",
               abs_threshold=1),
        Signal("clients", "연결 클라이언트", "saturation", "max(redis_connected_clients{__M__})", agg="max"),
        Signal("blocked", "blocked 클라이언트", "saturation", "max(redis_blocked_clients{__M__})", agg="max",
               abs_threshold=1),
        Signal("mem_util", "메모리 사용률", "memory",
               "max(redis_memory_used_bytes{__M__}) / max(redis_memory_max_bytes{__M__})", agg="max", unit="ratio",
               abs_threshold=0.9),
    ],
}


@dataclass(frozen=True)
class CwSignal:
    key: str
    label: str
    family: str
    metric: str
    stat: str = "Average"
    better: str = "lower"
    unit: str = ""


# AMP 대신 쓰는 CloudWatch 기본 지표: (네임스페이스, 차원 이름, 지표 목록)
CW_FALLBACK = {
    "mysql": ("AWS/RDS", "DBInstanceIdentifier", [
        CwSignal("row_lock_time", "행 잠금 시간", "lock", "RowLockTime", unit="ms"),
        CwSignal("blocked_tx", "차단된 트랜잭션", "lock", "BlockedTransactions", unit="회/s"),
        CwSignal("deadlocks", "deadlock", "lock", "Deadlocks", unit="회/s"),
        CwSignal("dml_latency", "DML 지연", "latency", "DMLLatency", unit="ms"),
        CwSignal("select_latency", "SELECT 지연", "latency", "SelectLatency", unit="ms"),
        CwSignal("connections", "연결 수", "saturation", "DatabaseConnections", stat="Maximum"),
        CwSignal("cpu", "CPU", "saturation", "CPUUtilization", unit="%"),
    ]),
    "postgresql": ("AWS/RDS", "DBInstanceIdentifier", [
        CwSignal("deadlocks", "deadlock", "lock", "Deadlocks", unit="회/s"),
        CwSignal("read_iops", "읽기 IOPS", "io", "ReadIOPS"),
        CwSignal("read_latency", "읽기 지연", "latency", "ReadLatency", unit="s"),
        CwSignal("buffer_hit", "버퍼 캐시 적중률", "cache", "BufferCacheHitRatio", better="higher", unit="%"),
        CwSignal("connections", "연결 수", "saturation", "DatabaseConnections", stat="Maximum"),
        CwSignal("cpu", "CPU", "saturation", "CPUUtilization", unit="%"),
    ]),
    "mssql": ("AWS/RDS", "DBInstanceIdentifier", [
        CwSignal("read_latency", "읽기 지연", "latency", "ReadLatency", unit="s"),
        CwSignal("write_latency", "쓰기 지연", "latency", "WriteLatency", unit="s"),
        CwSignal("connections", "연결 수", "saturation", "DatabaseConnections", stat="Maximum"),
        CwSignal("cpu", "CPU", "saturation", "CPUUtilization", unit="%"),
    ]),
    "redis": ("AWS/ElastiCache", "CacheClusterId", [
        CwSignal("hit_rate", "캐시 적중률", "cache", "CacheHitRate", better="higher", unit="%"),
        CwSignal("misses", "캐시 미스", "cache", "CacheMisses", stat="Sum"),
        CwSignal("evictions", "eviction", "memory", "Evictions", stat="Sum"),
        CwSignal("engine_cpu", "엔진 CPU", "saturation", "EngineCPUUtilization", unit="%"),
        CwSignal("connections", "연결 수", "saturation", "CurrConnections", stat="Maximum"),
        CwSignal("mem_util", "메모리 사용률", "memory", "DatabaseMemoryUsagePercentage", unit="%"),
    ]),
}

# Application Signals의 RemoteService(= span의 db.system) → 엔진
ENGINE_ALIASES = {
    "mysql": "mysql", "mariadb": "mysql", "aurora-mysql": "mysql",
    "postgresql": "postgresql", "postgres": "postgresql", "aurora-postgresql": "postgresql",
    "mssql": "mssql", "sqlserver": "mssql", "microsoft.sql_server": "mssql",
    "redis": "redis", "valkey": "redis", "elasticache": "redis",
}

ENGINE_NAMES = {"mysql": "Aurora MySQL", "postgresql": "Aurora PostgreSQL", "mssql": "RDS for SQL Server",
                "redis": "ElastiCache for Valkey"}


def selector(label, values):
    """__M__ 자리에 넣을 매처. 값이 없으면 빈 문자열(엔진 전체)."""
    if not values:
        return ""
    alt = "|".join(v.replace(".", r"\\.") for v in values)
    return f'{label}=~"{alt}",'


def promql(signal, label=None, values=None):
    return signal.query.replace("__M__", selector(label, values) if label else "")
