'use strict';
/**
 * payment-service: 결제 서비스 (RDS for SQL Server).
 *
 * - accounts(id=1): 결제마다 잔액을 갱신하는 '핫 로우'
 * - payments: 결제 기록
 * - mssql-blocking 시나리오: accounts(id=1)을 잡은 트랜잭션을 몇 초씩 유지 → 결제 UPDATE가 막혀
 *   requestTimeout(기본 5초)에 걸려 실패(Fault)
 *
 * 코드에는 OpenTelemetry 의존성이 없습니다. EKS에서는 instrumentation.opentelemetry.io/inject-nodejs 주석으로
 * ADOT Node.js SDK가 주입되고 express·http·tedious(mssql 드라이버) 호출이 자동 계측됩니다.
 * ADOT Node.js는 CommonJS만 자동 계측하므로 ESM(import)으로 바꾸지 않습니다.
 */
const crypto = require('crypto');
const express = require('express');
const sql = require('mssql');

const config = {
  server: process.env.MSSQL_HOST,
  port: parseInt(process.env.MSSQL_PORT || '1433', 10),
  database: process.env.MSSQL_DATABASE || 'payments',
  user: process.env.MSSQL_USER,
  password: process.env.MSSQL_PASSWORD,
  requestTimeout: parseInt(process.env.MSSQL_REQUEST_TIMEOUT_MS || '5000', 10),
  connectionTimeout: 10000,
  pool: { max: parseInt(process.env.MSSQL_POOL_SIZE || '10', 10), min: 0, idleTimeoutMillis: 30000 },
  options: {
    encrypt: (process.env.MSSQL_ENCRYPT || 'true') === 'true',
    // RDS 인증서 번들을 이미지에 넣지 않은 실습용 설정. 운영에서는 RDS CA를 신뢰 저장소에 넣고 false로 둡니다
    trustServerCertificate: (process.env.MSSQL_TRUST_SERVER_CERT || 'true') === 'true',
  },
};

let poolPromise = null;
function getPool() {
  if (!poolPromise) {
    const pool = new sql.ConnectionPool(config);
    // 리스너가 없으면 풀의 'error' 이벤트가 프로세스를 죽일 수 있다 (mssql README 권장)
    pool.on('error', (err) => console.error('mssql pool error', err.message));
    poolPromise = pool.connect().catch((err) => {
      poolPromise = null;
      throw err;
    });
  }
  return poolPromise;
}

// ---------------------------------------------------------------- 장애 시나리오
const MAX_DURATION = 1800;
const chaosToken = process.env.CHAOS_TOKEN || '';
const scenarios = {
  'mssql-blocking': {
    desc: 'accounts(id=1) 잠금을 쥔 트랜잭션 유지 → 결제 UPDATE 차단·타임아웃',
    worker: mssqlBlockingWorker,
  },
};
const running = {}; // name -> { until, stopped, timer }

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function mssqlBlockingWorker(state) {
  const holdMs = parseInt(process.env.CHAOS_LOCK_HOLD_SECONDS || '8', 10) * 1000;
  while (!state.stopped) {
    let tx = null;
    try {
      const pool = await getPool();
      tx = new sql.Transaction(pool);
      await tx.begin();
      await new sql.Request(tx).query('UPDATE accounts SET balance = balance WHERE id = 1');
      await sleep(holdMs);
      await tx.commit();
    } catch (err) {
      console.error('mssql-blocking worker', err.message);
      if (tx) await tx.rollback().catch(() => {});
      await sleep(1000);
    }
    await sleep(300);
  }
}

function chaosActive(name) {
  const st = running[name];
  return Boolean(st) && st.until > Date.now();
}

function chaosStop(name) {
  const st = running[name];
  if (!st) return false;
  st.stopped = true;
  clearTimeout(st.timer);
  delete running[name];
  console.warn(`chaos ${name} stopped`);
  return true;
}

function chaosStart(name, seconds) {
  chaosStop(name);
  const duration = Math.max(1, Math.min(parseInt(seconds || '300', 10), MAX_DURATION));
  const state = { until: Date.now() + duration * 1000, stopped: false };
  state.timer = setTimeout(() => chaosStop(name), duration * 1000);
  running[name] = state;
  scenarios[name].worker(state);
  console.warn(`chaos ${name} started for ${duration}s`);
  return duration;
}

function authorized(req) {
  const given = Buffer.from(req.get('X-Chaos-Token') || '');
  const want = Buffer.from(chaosToken);
  return want.length > 0 && given.length === want.length && crypto.timingSafeEqual(given, want);
}

// ---------------------------------------------------------------- API
const app = express();
app.use(express.json());

app.get('/health', (req, res) => res.json({ status: 'ok' }));

app.post('/payments', async (req, res) => {
  const orderId = String(req.body.order_id || '');
  const amount = Number(req.body.amount || 0);
  let tx = null;
  try {
    const pool = await getPool();
    tx = new sql.Transaction(pool);
    await tx.begin();
    await new sql.Request(tx)
      .input('amount', sql.Decimal(18, 2), amount)
      .query('UPDATE accounts SET balance = balance + @amount, updated_at = SYSUTCDATETIME() WHERE id = 1');
    const result = await new sql.Request(tx)
      .input('order_id', sql.VarChar(36), orderId)
      .input('amount', sql.Decimal(12, 2), amount)
      .query("INSERT INTO payments (order_id, amount, status) OUTPUT INSERTED.id VALUES (@order_id, @amount, 'CAPTURED')");
    await tx.commit();
    res.status(201).json({ payment_id: String(result.recordset[0].id), status: 'CAPTURED' });
  } catch (err) {
    if (tx) await tx.rollback().catch(() => {});
    console.warn('payment failed', err.code, err.message);
    res.status(500).json({ error: 'payment_failed', code: err.code || null });
  }
});

app.get('/chaos', (req, res) => {
  const now = Date.now();
  const out = {};
  for (const [name, spec] of Object.entries(scenarios)) {
    out[name] = {
      desc: spec.desc,
      remaining_seconds: chaosActive(name) ? Math.round((running[name].until - now) / 1000) : null,
    };
  }
  res.json({ service: 'payment-service', scenarios: out });
});

app.post('/chaos/:name', (req, res) => {
  if (!authorized(req)) return res.status(403).json({ error: 'forbidden' });
  const { name } = req.params;
  if (!scenarios[name]) return res.status(404).json({ error: 'unknown_scenario', available: Object.keys(scenarios) });
  const seconds = chaosStart(name, req.query.duration);
  return res.status(202).json({
    service: 'payment-service', scenario: name, duration_seconds: seconds, started_at: new Date().toISOString(),
  });
});

app.delete('/chaos/:name', (req, res) => {
  if (!authorized(req)) return res.status(403).json({ error: 'forbidden' });
  return res.json({ service: 'payment-service', scenario: req.params.name, stopped: chaosStop(req.params.name) });
});

const port = parseInt(process.env.PORT || '8080', 10);
const server = app.listen(port, () => console.log(`payment-service listening on ${port}`));
// PID 1의 Node는 SIGTERM을 기본 처리하지 않아 롤아웃마다 종료 유예 시간을 다 쓴다
process.on('SIGTERM', () => {
  for (const name of Object.keys(running)) chaosStop(name);
  server.close(() => process.exit(0));
  setTimeout(() => process.exit(0), 5000).unref();
});
