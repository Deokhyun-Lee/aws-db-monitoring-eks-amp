"""order-api: 주문 진입점 서비스.

- Aurora MySQL: 주문 저장. order_counters(id=1) 행을 주문마다 갱신하는 '핫 로우' 패턴
- ElastiCache for Valkey: 상품 조회 캐시 (cache-aside, TTL 30초)
- inventory-service(HTTP): 재고 예약·상품 조회 → Aurora PostgreSQL
- payment-service(HTTP): 결제 → RDS for SQL Server

코드에는 OpenTelemetry 의존성이 없습니다. EKS에서는 CloudWatch Observability add-on이
Deployment의 instrumentation.opentelemetry.io/inject-python 주석을 보고 ADOT Python SDK를 주입하고,
Flask·requests·pymysql·redis 호출이 자동으로 span과 Application Signals 지표가 됩니다.
"""
import json
import logging
import os
import uuid

import pymysql
import redis
import requests
from flask import Flask, jsonify, request
from redis.cluster import RedisCluster
from requests.adapters import HTTPAdapter

from chaos import Chaos
from pool import Pool

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("order-api")

MYSQL = dict(
    host=os.environ["MYSQL_HOST"],
    port=int(os.environ.get("MYSQL_PORT", "3306")),
    user=os.environ["MYSQL_USER"],
    password=os.environ["MYSQL_PASSWORD"],
    database=os.environ.get("MYSQL_DATABASE", "shop"),
    connect_timeout=5,
    # 읽기는 autocommit, 쓰기만 명시적 트랜잭션(conn.begin). 읽기 연결이 스냅샷을 쥔 채 풀에 남지 않게
    autocommit=True,
    # 잠금 대기를 무한정 기다리지 않게: 기본 50초 → 5초 뒤 1205 오류(장애 시나리오에서 Fault로 보임)
    init_command="SET SESSION innodb_lock_wait_timeout = %d" % int(os.environ.get("MYSQL_LOCK_WAIT_TIMEOUT", "5")),
)
INVENTORY_URL = os.environ.get("INVENTORY_URL", "http://inventory-service:8080")
PAYMENT_URL = os.environ.get("PAYMENT_URL", "http://payment-service:8080")
HTTP_TIMEOUT = float(os.environ.get("HTTP_TIMEOUT_SECONDS", "10"))
CACHE_TTL = int(os.environ.get("CACHE_TTL_SECONDS", "30"))

db = Pool(lambda: pymysql.connect(**MYSQL), size=int(os.environ.get("MYSQL_POOL_SIZE", "10")))
# deploy-app-signals.sh가 실제 복제 그룹 설정(전송 중 암호화, 클러스터 모드)을 읽어 VALKEY_TLS·VALKEY_CLUSTER로 넘긴다
_cache_kw = dict(
    host=os.environ["VALKEY_HOST"],
    port=int(os.environ.get("VALKEY_PORT", "6379")),
    ssl=os.environ.get("VALKEY_TLS", "false").lower() == "true",
    socket_timeout=2,
    socket_connect_timeout=2,
    decode_responses=True,
)
cache = RedisCluster(**_cache_kw) if os.environ.get("VALKEY_CLUSTER", "false").lower() == "true" else redis.Redis(**_cache_kw)
http = requests.Session()
# gunicorn 스레드(16)보다 연결 풀이 작으면 연결을 계속 새로 맺는다
http.mount("http://", HTTPAdapter(pool_connections=4, pool_maxsize=32))

app = Flask(__name__)


# ---------------------------------------------------------------- 장애 시나리오
def mysql_lock_worker(stop):
    """order_counters(id=1) 행을 SELECT ... FOR UPDATE로 잡고 몇 초씩 놓지 않는다.
    주문 트랜잭션의 UPDATE order_counters가 잠금을 기다리다 지연되거나 1205(lock wait timeout)로 실패한다."""
    hold = float(os.environ.get("CHAOS_LOCK_HOLD_SECONDS", "8"))
    while not stop.is_set():
        try:
            conn = pymysql.connect(**MYSQL)
        except pymysql.MySQLError:
            log.exception("mysql-lock connect")
            stop.wait(1)
            continue
        try:
            conn.begin()  # autocommit 연결이라 트랜잭션을 열어야 FOR UPDATE 잠금이 유지된다
            with conn.cursor() as cur:
                cur.execute("SELECT value FROM order_counters WHERE id = 1 FOR UPDATE")
                stop.wait(hold)
            conn.commit()
        except Exception:  # noqa: BLE001 - 시나리오 스레드는 계속 돈다
            log.exception("mysql-lock worker")
            stop.wait(1)
        finally:
            conn.close()
        stop.wait(0.3)


def cache_stampede_start():
    """상품 캐시를 비운다. 시나리오가 켜진 동안에는 TTL을 1초로 써서 대부분의 조회가 캐시 미스가 된다."""
    deleted = 0
    for key in cache.scan_iter(match="product:*", count=500):
        cache.delete(key)
        deleted += 1
    log.info("cache-stampede: deleted %d product keys", deleted)


chaos = Chaos(
    service="order-api",
    scenarios={
        "mysql-lock": {"worker": mysql_lock_worker,
                       "desc": "order_counters 행 잠금 유지 → 주문 UPDATE 잠금 대기·1205"},
        "cache-stampede": {"on_start": cache_stampede_start,
                           "desc": "상품 캐시 삭제 + TTL 1초 → 캐시 미스 폭증, inventory·PostgreSQL 부하"},
    },
)
chaos.register(app)


# ---------------------------------------------------------------- API
@app.get("/health")
def health():
    return jsonify(status="ok")


@app.get("/products/<sku>")
def get_product(sku):
    key = f"product:{sku}"
    try:
        cached = cache.get(key)
    except redis.RedisError:
        log.warning("valkey get failed", exc_info=True)
        cached = None
    if cached:
        return jsonify(json.loads(cached) | {"cache": "hit"})

    r = http.get(f"{INVENTORY_URL}/stock/{sku}", timeout=HTTP_TIMEOUT)
    if r.status_code == 404:
        return jsonify(error="not_found"), 404
    if not r.ok:
        return jsonify(error="inventory_unavailable", upstream=r.status_code), 502
    data = r.json()
    ttl = 1 if chaos.active("cache-stampede") else CACHE_TTL
    try:
        cache.setex(key, ttl, json.dumps(data))
    except redis.RedisError:
        log.warning("valkey set failed", exc_info=True)
    return jsonify(data | {"cache": "miss"})


@app.post("/orders")
def create_order():
    body = request.get_json(silent=True) or {}
    sku = body.get("sku", "SKU-0001")
    qty = int(body.get("qty", 1))

    # 1) 재고 예약 (inventory-service → Aurora PostgreSQL)
    r = http.post(f"{INVENTORY_URL}/reserve", json={"sku": sku, "qty": qty}, timeout=HTTP_TIMEOUT)
    if r.status_code in (404, 409):
        return jsonify(error=r.json().get("error", "reserve_rejected")), r.status_code
    if not r.ok:
        return jsonify(error="inventory_unavailable", upstream=r.status_code), 502
    amount = round(float(r.json()["price"]) * qty, 2)

    # 2) 주문 저장 (Aurora MySQL). order_counters는 주문마다 갱신되는 핫 로우
    # 실습용 단순화: 주문 저장·결제가 실패해도 재고 예약을 되돌리지 않는다(재고 1e9라 영향 없음)
    order_id = str(uuid.uuid4())
    try:
        with db.connection() as conn, conn.cursor() as cur:
            conn.begin()
            cur.execute("UPDATE order_counters SET value = value + 1 WHERE id = 1")
            cur.execute(
                "INSERT INTO orders (id, sku, qty, amount, status) VALUES (%s, %s, %s, %s, 'PENDING')",
                (order_id, sku, qty, amount),
            )
            conn.commit()
    except pymysql.MySQLError as e:
        log.warning("order insert failed: %s", e)
        return jsonify(error="order_store_failed", code=e.args[0] if e.args else None), 500

    # 3) 결제 (payment-service → RDS for SQL Server)
    p = http.post(f"{PAYMENT_URL}/payments", json={"order_id": order_id, "amount": amount}, timeout=HTTP_TIMEOUT)
    status = "PAID" if p.ok else "PAYMENT_FAILED"
    with db.connection() as conn, conn.cursor() as cur:
        cur.execute("UPDATE orders SET status = %s WHERE id = %s", (status, order_id))
    if not p.ok:
        return jsonify(order_id=order_id, status=status, upstream=p.status_code), 502
    return jsonify(order_id=order_id, status=status, amount=amount), 201


@app.get("/orders/<order_id>")
def get_order(order_id):
    with db.connection() as conn, conn.cursor(pymysql.cursors.DictCursor) as cur:
        cur.execute("SELECT id, sku, qty, amount, status, created_at FROM orders WHERE id = %s", (order_id,))
        row = cur.fetchone()
    if not row:
        return jsonify(error="not_found"), 404
    row["amount"] = float(row["amount"])
    row["created_at"] = row["created_at"].isoformat()
    return jsonify(row)
