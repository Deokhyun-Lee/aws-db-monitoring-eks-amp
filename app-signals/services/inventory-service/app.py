"""inventory-service: 상품·재고 서비스 (Aurora PostgreSQL).

- products: 상품 50개(SKU-0001~SKU-0050), 재고
- inventory_events: 재고 변동 이력. db-init이 약 100만 건을 미리 넣고 (sku, created_at) 인덱스를 둔다
- 조회·예약 때마다 '최근 5분 예약 건수'를 인덱스로 센다. pg-slow-query 시나리오는 이 조회를
  선행 와일드카드 LIKE로 바꿔 인덱스를 못 쓰는 전체 스캔으로 만든다

코드에는 OpenTelemetry 의존성이 없습니다(EKS에서 ADOT Python SDK 자동 주입).
"""
import logging
import os
import time

import psycopg2
from flask import Flask, jsonify, request

from chaos import Chaos
from pool import Pool

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("inventory-service")

PG = dict(
    host=os.environ["PG_HOST"],
    port=int(os.environ.get("PG_PORT", "5432")),
    user=os.environ["PG_USER"],
    password=os.environ["PG_PASSWORD"],
    dbname=os.environ.get("PG_DATABASE", "inventory"),
    connect_timeout=5,
    sslmode=os.environ.get("PG_SSLMODE", "require"),
    # 장애 시나리오의 전체 스캔이 끝없이 쌓이지 않도록 문장 단위 상한
    options="-c statement_timeout=%d" % int(os.environ.get("PG_STATEMENT_TIMEOUT_MS", "8000")),
)
CPU_BURN_MS = int(os.environ.get("CHAOS_CPU_BURN_MS", "200"))

db = Pool(lambda: psycopg2.connect(**PG), size=int(os.environ.get("PG_POOL_SIZE", "10")))
app = Flask(__name__)

chaos = Chaos(
    service="inventory-service",
    scenarios={
        "pg-slow-query": {"desc": "최근 예약 건수 조회를 선행 와일드카드 LIKE로 → inventory_events 전체 스캔"},
        "app-cpu": {"desc": "요청마다 CPU를 %dms 태움 → 서비스 지연, DB 지표는 평소와 같음" % CPU_BURN_MS},
    },
)
chaos.register(app)


def burn_cpu_if_chaos():
    if chaos.active("app-cpu"):
        end = time.perf_counter() + CPU_BURN_MS / 1000.0
        x = 0
        while time.perf_counter() < end:
            x = (x * 1103515245 + 12345) % 2147483648


def recent_reservations(cur, sku):
    if chaos.active("pg-slow-query"):
        # 선행 와일드카드라 (sku, created_at) 인덱스를 쓸 수 없어 inventory_events 전체를 읽는다
        cur.execute("SELECT count(*) FROM inventory_events WHERE note LIKE %s", (f"%{sku}%",))
    else:
        cur.execute(
            "SELECT count(*) FROM inventory_events WHERE sku = %s AND created_at > now() - interval '5 minutes'",
            (sku,),
        )
    return cur.fetchone()[0]


@app.get("/health")
def health():
    return jsonify(status="ok")


@app.get("/stock/<sku>")
def get_stock(sku):
    burn_cpu_if_chaos()
    with db.connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT sku, name, price, stock FROM products WHERE sku = %s", (sku,))
        row = cur.fetchone()
        if not row:
            conn.rollback()
            return jsonify(error="not_found"), 404
        recent = recent_reservations(cur, sku)
        conn.rollback()  # 읽기 전용 트랜잭션 정리
    return jsonify(sku=row[0], name=row[1], price=float(row[2]), stock=int(row[3]), recent_reservations=recent)


@app.post("/reserve")
def reserve():
    burn_cpu_if_chaos()
    body = request.get_json(silent=True) or {}
    sku = body.get("sku", "SKU-0001")
    qty = int(body.get("qty", 1))
    try:
        with db.connection() as conn, conn.cursor() as cur:
            # 이력 조회를 행 잠금(UPDATE) 앞에 둔다. 뒤에 두면 전체 스캔 시나리오에서 products 행 잠금을 쥔 채
            # 스캔해 실제 잠금 대기가 생기고, 원인이 '스캔'이 아니라 '잠금'처럼 보인다
            recent = recent_reservations(cur, sku)
            cur.execute(
                "UPDATE products SET stock = stock - %s WHERE sku = %s AND stock >= %s RETURNING price, stock",
                (qty, sku, qty),
            )
            row = cur.fetchone()
            if not row:
                conn.rollback()
                return jsonify(error="out_of_stock"), 409
            cur.execute(
                "INSERT INTO inventory_events (sku, qty, note) VALUES (%s, %s, %s)",
                (sku, -qty, f"reserve {sku}"),
            )
            conn.commit()
    except psycopg2.Error as e:
        log.warning("reserve failed: %s", e.pgerror or e)
        return jsonify(error="reserve_failed", pgcode=e.pgcode), 500
    return jsonify(sku=sku, price=float(row[0]), stock=int(row[1]), recent_reservations=recent)
