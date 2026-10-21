"""샘플 서비스용 DB 객체를 만든다 (여러 번 실행해도 같은 결과).

마스터 계정(일회용 Secret으로 받음)으로 서비스별 DB·사용자·테이블을 만들고, 서비스는 최소 권한 사용자로만 접속한다.

  Aurora MySQL      DB shop       사용자 shop_app        orders, order_counters
  Aurora PostgreSQL DB inventory  사용자 inventory_app   products(50), inventory_events(~100만 건 시드)
  RDS SQL Server    DB payments   사용자 payment_app     accounts, payments

필요한 환경 변수: *_HOST, *_MASTER_USER, *_MASTER_PASSWORD, *_APP_PASSWORD (MYSQL_/PG_/MSSQL_)
암호에는 영문자·숫자만 허용한다(SQL Server CREATE LOGIN은 매개변수 바인딩을 받지 않아 문자열로 넣기 때문).
"""
import logging
import os
import re
import sys
import time

import psycopg2
import pymssql
import pymysql

logging.basicConfig(level="INFO", format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("db-init")

PRODUCTS = int(os.environ.get("PRODUCT_COUNT", "50"))
EVENT_SEED_ROWS = int(os.environ.get("EVENT_SEED_ROWS", "1000000"))
SAFE = re.compile(r"^[A-Za-z0-9]{12,128}$")


def env(name):
    v = os.environ.get(name)
    if not v:
        sys.exit(f"missing environment variable: {name}")
    return v


def app_password(name):
    v = env(name)
    if not SAFE.fullmatch(v):
        sys.exit(f"{name} must be 12-128 alphanumeric characters")
    return v


def retry(fn, what, attempts=10):
    for i in range(attempts):
        try:
            return fn()
        except Exception as e:  # noqa: BLE001 - 네트워크·DNS 준비 대기
            log.warning("%s not ready (%s/%s): %s", what, i + 1, attempts, e)
            time.sleep(min(30, 2 ** i))
    sys.exit(f"{what}: giving up")


def init_mysql():
    pw = app_password("MYSQL_APP_PASSWORD")
    conn = retry(lambda: pymysql.connect(host=env("MYSQL_HOST"), port=int(os.environ.get("MYSQL_PORT", "3306")),
                                         user=env("MYSQL_MASTER_USER"), password=env("MYSQL_MASTER_PASSWORD"),
                                         autocommit=True, connect_timeout=10), "mysql")
    with conn.cursor() as cur:
        cur.execute("CREATE DATABASE IF NOT EXISTS shop")
        cur.execute("CREATE USER IF NOT EXISTS 'shop_app'@'%%' IDENTIFIED BY %s", (pw,))
        cur.execute("ALTER USER 'shop_app'@'%%' IDENTIFIED BY %s", (pw,))
        cur.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON shop.* TO 'shop_app'@'%'")
        cur.execute("""
            CREATE TABLE IF NOT EXISTS shop.orders (
              id CHAR(36) PRIMARY KEY,
              sku VARCHAR(32) NOT NULL,
              qty INT NOT NULL,
              amount DECIMAL(12,2) NOT NULL,
              status VARCHAR(20) NOT NULL,
              created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
              updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
              KEY idx_orders_created (created_at)
            ) ENGINE=InnoDB""")
        cur.execute("CREATE TABLE IF NOT EXISTS shop.order_counters (id INT PRIMARY KEY, value BIGINT NOT NULL) ENGINE=InnoDB")
        cur.execute("INSERT IGNORE INTO shop.order_counters (id, value) VALUES (1, 0)")
    conn.close()
    log.info("mysql: shop ready")


def init_postgres():
    pw = app_password("PG_APP_PASSWORD")
    common = dict(host=env("PG_HOST"), port=int(os.environ.get("PG_PORT", "5432")), user=env("PG_MASTER_USER"),
                  password=env("PG_MASTER_PASSWORD"), connect_timeout=10, sslmode=os.environ.get("PG_SSLMODE", "require"))
    conn = retry(lambda: psycopg2.connect(dbname=os.environ.get("PG_MASTER_DATABASE", "postgres"), **common), "postgres")
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute("SELECT 1 FROM pg_database WHERE datname = 'inventory'")
        if not cur.fetchone():
            cur.execute("CREATE DATABASE inventory")
        cur.execute("SELECT 1 FROM pg_roles WHERE rolname = 'inventory_app'")
        verb = "ALTER" if cur.fetchone() else "CREATE"
        cur.execute(f"{verb} ROLE inventory_app WITH LOGIN PASSWORD %s", (pw,))
    conn.close()

    conn = psycopg2.connect(dbname="inventory", **common)
    with conn, conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS products (
              sku varchar(32) PRIMARY KEY,
              name text NOT NULL,
              price numeric(12,2) NOT NULL,
              stock bigint NOT NULL)""")
        cur.execute("""
            CREATE TABLE IF NOT EXISTS inventory_events (
              id bigserial PRIMARY KEY,
              sku varchar(32) NOT NULL,
              qty int NOT NULL,
              note text NOT NULL,
              created_at timestamptz NOT NULL DEFAULT now())""")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_inventory_events_sku_created ON inventory_events (sku, created_at)")
        cur.execute("""
            INSERT INTO products (sku, name, price, stock)
            SELECT format('SKU-%%s', lpad(i::text, 4, '0')), format('product %%s', i), (10 + i %% 90)::numeric, 1000000000
            FROM generate_series(1, %s) AS i
            ON CONFLICT (sku) DO NOTHING""", (PRODUCTS,))
        cur.execute("SELECT count(*) FROM inventory_events")
        have = cur.fetchone()[0]
        if have < EVENT_SEED_ROWS:
            # 시드 이력. note는 md5 문자열이라 장애 시나리오의 LIKE '%SKU-…%'는 일치하는 행 없이 전체를 읽게 된다
            cur.execute("""
                INSERT INTO inventory_events (sku, qty, note, created_at)
                SELECT format('SKU-%%s', lpad((1 + i %% %s)::text, 4, '0')), 1, md5(i::text) || ' restock',
                       now() - make_interval(secs => i)
                FROM generate_series(1, %s) AS i""", (PRODUCTS, EVENT_SEED_ROWS - have))
        cur.execute("GRANT CONNECT ON DATABASE inventory TO inventory_app")
        cur.execute("GRANT SELECT, INSERT, UPDATE ON products, inventory_events TO inventory_app")
        cur.execute("GRANT USAGE, SELECT ON SEQUENCE inventory_events_id_seq TO inventory_app")
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute("ANALYZE products")
        cur.execute("ANALYZE inventory_events")
    conn.close()
    log.info("postgres: inventory ready (events seeded to >= %d rows)", EVENT_SEED_ROWS)


def init_mssql():
    pw = app_password("MSSQL_APP_PASSWORD")
    connect = dict(server=env("MSSQL_HOST"), port=int(os.environ.get("MSSQL_PORT", "1433")),
                   user=env("MSSQL_MASTER_USER"), password=env("MSSQL_MASTER_PASSWORD"), login_timeout=10, autocommit=True)
    conn = retry(lambda: pymssql.connect(database="master", **connect), "sqlserver")
    cur = conn.cursor()
    cur.execute("IF DB_ID('payments') IS NULL CREATE DATABASE payments")
    # CREATE/ALTER LOGIN은 매개변수를 받지 않는다. 암호는 SAFE(영문자·숫자)만 통과시켰다
    cur.execute(f"""
        IF NOT EXISTS (SELECT 1 FROM sys.server_principals WHERE name = 'payment_app')
            CREATE LOGIN payment_app WITH PASSWORD = '{pw}', CHECK_POLICY = OFF;
        ELSE
            ALTER LOGIN payment_app WITH PASSWORD = '{pw}';""")
    conn.close()

    conn = pymssql.connect(database="payments", **connect)
    cur = conn.cursor()
    cur.execute("""
        IF NOT EXISTS (SELECT 1 FROM sys.database_principals WHERE name = 'payment_app')
            CREATE USER payment_app FOR LOGIN payment_app;
        ALTER ROLE db_datareader ADD MEMBER payment_app;
        ALTER ROLE db_datawriter ADD MEMBER payment_app;""")
    cur.execute("""
        IF OBJECT_ID('dbo.accounts') IS NULL
            CREATE TABLE dbo.accounts (
              id INT PRIMARY KEY,
              balance DECIMAL(18,2) NOT NULL,
              updated_at DATETIME2 NOT NULL DEFAULT SYSUTCDATETIME());
        IF NOT EXISTS (SELECT 1 FROM dbo.accounts WHERE id = 1)
            INSERT INTO dbo.accounts (id, balance) VALUES (1, 0);
        IF OBJECT_ID('dbo.payments') IS NULL
            CREATE TABLE dbo.payments (
              id BIGINT IDENTITY PRIMARY KEY,
              order_id VARCHAR(36) NOT NULL,
              amount DECIMAL(12,2) NOT NULL,
              status VARCHAR(20) NOT NULL,
              created_at DATETIME2 NOT NULL DEFAULT SYSUTCDATETIME());""")
    conn.close()
    log.info("sqlserver: payments ready")


if __name__ == "__main__":
    targets = os.environ.get("TARGETS", "mysql,postgres,mssql").split(",")
    for t in targets:
        {"mysql": init_mysql, "postgres": init_postgres, "mssql": init_mssql}[t.strip()]()
    log.info("done: %s", targets)
