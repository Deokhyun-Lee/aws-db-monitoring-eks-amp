# CloudWatch Application Signals 확장: 앱 → DB까지 이어 보는 RCA

DB 엔진 지표(이 저장소의 exporter → AMP)만으로는 "DB가 바빴다"까지만 보입니다. 이 모듈은 CloudWatch
Application Signals로 **앱의 골든 지표와 트레이스**를 더하고, 둘을 이어서 장애 원인을 앱 오퍼레이션 → 의존성 호출 →
SQL 문장 → DB 엔진 상태까지 한 줄로 짚도록 합니다.

```
사용자 ─▶ order-api ──HTTP──▶ inventory-service ──▶ Aurora PostgreSQL
          (Python)  ──HTTP──▶ payment-service   ──▶ RDS for SQL Server
             │                 (Node.js)
             ├──▶ Aurora MySQL
             └──▶ ElastiCache for Valkey

   ADOT SDK(자동 주입) ─▶ CloudWatch agent(add-on) ─▶ Application Signals 지표·서비스 맵 / X-Ray·Transaction Search
   exporter ─▶ Prometheus ─▶ AMP (기존 경로)            └────────── rca/rca.py 가 둘을 함께 읽음 ──────────┘
```

## 왜 새 서비스를 만들었나

Application Signals는 **요청을 받는 서비스**를 단위로 지연·오류·의존성을 만듭니다. 저장소에 있는 코드를 먼저 봤지만
쓸 만한 것이 없었습니다.

| 기존 코드 | 판단 |
|---|---|
| `helm-charts/db-monitoring/charts/aurora-discovery/app` | 1시간마다 도는 CronJob(AWS API·쿠버네티스 API 호출). 받는 요청이 없어 서비스 맵·DB 의존성이 생기지 않음 |
| `scripts/sql-server-load-test.py` | 일괄 부하 스크립트. 받는 요청이 없고, pyodbc는 ADOT Python 자동 계측 대상이 아님 |
| `scripts/elasticache-load-generator.py` | 일괄 부하 스크립트. 받는 요청이 없음 |

그래서 DB 4종을 모두 부르는 작은 쇼핑 서비스 3개를 만들었습니다. 부하 스크립트의 아이디어(잠금·차단 유발)는
장애 시나리오로 옮겼습니다.

| 서비스 | 언어 | 자동 계측되는 호출 | 핫스팟 |
|---|---|---|---|
| `order-api` | Python (Flask, gunicorn) | Flask, requests, pymysql(Aurora MySQL), redis(Valkey) | `order_counters(id=1)` 행을 주문마다 갱신 |
| `inventory-service` | Python (Flask, gunicorn) | Flask, psycopg2(Aurora PostgreSQL) | `inventory_events` 약 100만 건, 최근 예약 수 조회 |
| `payment-service` | Node.js (Express) | express, http, tedious(RDS for SQL Server) | `accounts(id=1)` 행을 결제마다 갱신 |

SQL Server 서비스만 Node.js인 이유: ADOT Python 배포판이 묶는 계측 패키지에 pymssql이 없고(2026-10 기준
`aws-opentelemetry-distro` 의존성 목록), ADOT Node.js는 `@opentelemetry/instrumentation-tedious`를 포함합니다.
코드에는 OpenTelemetry 의존성이 없습니다. EKS에서는 Pod 템플릿 주석
`instrumentation.opentelemetry.io/inject-python|inject-nodejs: "true"`를 보고 CloudWatch Observability add-on이
ADOT SDK를 주입합니다.

## 장애 시나리오

| 시나리오 | 서비스 | 하는 일 | 앱에서 보이는 것 | DB에서 보이는 것 | RCA 기대 결론 |
|---|---|---|---|---|---|
| `mysql-lock` | order-api | `order_counters` 행을 `SELECT … FOR UPDATE`로 몇 초씩 잡음 | `POST /orders` 지연, 1205 → Fault | InnoDB 행 잠금 대기 | 잠금 경합 |
| `pg-slow-query` | inventory-service | 최근 예약 수 조회를 선행 와일드카드 `LIKE`로 바꿈 | `/stock`·`/reserve` 지연 | 순차 스캔 행 수 | 비효율 쿼리(전체 스캔) |
| `mssql-blocking` | payment-service | `accounts` 행을 잡은 트랜잭션을 몇 초씩 유지 | `POST /payments` 타임아웃 → 상위 502 | suspended 요청·LCK 대기(custom-metrics 필요) | DB 대기(엔진 지표 없으면 '근거 없음') |
| `cache-stampede` | order-api | 상품 캐시 삭제 + TTL 1초 | `GET /products` 지연, inventory 호출 급증 | Valkey 적중률↓, PostgreSQL 처리량↑ | 캐시 미스 폭증 (DB 부하는 결과) |
| `app-cpu` | inventory-service | 요청마다 CPU를 200ms 태움 | 지연↑, Python CPU↑ | 변화 없음 | 앱 CPU 포화 (DB 원인 아님) |

`scenarios/chaos.sh start <시나리오> [초]`가 `traffic-generator` Pod 안에서 헤드리스 서비스로 해당 서비스의 **모든 Pod**에
스위치를 켭니다. 엔드포인트는 `CHAOS_TOKEN`(Secret `shop-chaos`) 없이는 403이고, 최대 1800초 뒤 저절로 꺼집니다.

## RCA 동작 (`rca/rca.py`)

1. **골든 지표**: 시작 서비스의 지연(p99·평균)·트래픽·Fault(5xx)·Error(4xx)·포화(Python 런타임 CPU 또는 Container
   Insights)를 사고 구간과 기준 구간(기본: 바로 앞, 같은 길이)으로 비교
2. **의존성 순위**: `ListServiceDependencies`의 지표 참조로 의존성마다 지연·Fault·호출 수를 비교해 순위
3. **하위 서비스로 내려가기**: 1순위가 계측된 다른 서비스면 그 서비스에서 1~2 반복 (기본 깊이 3)
4. **트레이스**: Transaction Search(`aws/spans`)에서 사고 구간의 느린 DB 문장(`db.statement`)·오류, X-Ray 근본 원인 엔터티
5. **DB 엔진**: 의존성의 `RemoteResourceIdentifier`(`[db|]host[|port]`)로 RDS 클러스터 → writer 인스턴스(ElastiCache는
   복제 그룹 → 멤버)를 찾고, 같은 두 구간의 엔진 지표를 AMP(`db_instance` 라벨)에서 비교. 없으면 CloudWatch 기본 지표
6. **가설과 확신도**: 잠금 → 스캔 → 캐시 → 포화 → 부하 순으로 나빠진 신호를 찾고, 근거 계층
   (앱 골든 지표 · 의존성 지표 · 트레이스 · 엔진 지표) 수로 확신도(높음: 3개 이상이고 엔진 지표 포함, 중간: 2개, 낮음: 1개)

판단 기준(`rca/analysis.py` 상단): 의존성 평균 지연 +20ms 이상이면서 1.5배 이상, Fault +1%p 이상, 호출 3배 이상,
엔진 지표 2배 이상, 서비스 지연 증가 중 의존성이 설명하는 몫이 30% 미만이고 CPU 1.5배 이상이면 앱 자체.

```bash
pip install boto3
python app-signals/rca/rca.py --environment eks:${CLUSTER_NAME}/shop --service order-api \
    --operation "POST /orders" --start 2026-10-22T05:10:00Z --end 2026-10-22T05:20:00Z \
    --amp-workspace-id ${AMP_WORKSPACE_ID} --region ${AWS_REGION} --output rca.md
# --format json 은 자동화·에이전트 입력용
```

필요한 권한(읽기 전용): `cloudwatch:GetMetricData`, `application-signals:ListServices`·`ListServiceDependencies`,
`logs:StartQuery`·`logs:GetQueryResults`(aws/spans), `xray:GetTraceSummaries`, `rds:DescribeDBClusters`·`DescribeDBInstances`,
`elasticache:DescribeReplicationGroups`, `aps:QueryMetrics`. 권한이 없거나 꺼진 출처는 건너뛰고 보고서의 '데이터 출처'에 적습니다.

## 배포

전제: `deploy-all.sh`로 만든 VPC·DB·EKS·AMP·AMG가 있고, `kubectl`·`docker buildx`·`envsubst`·`openssl`이 있음.

```bash
# database-stack에 넣은 마스터 암호 (db-init Job이 서비스별 DB·최소 권한 사용자를 만들 때만 쓰고 바로 지움)
export MYSQL_MASTER_PASSWORD=... PG_MASTER_PASSWORD=... MSSQL_MASTER_PASSWORD=...
./app-signals/deploy-app-signals.sh
```

루트의 2025년 `database-stack.yaml`처럼 RDS가 Aurora 마스터 암호를 관리(`ManageMasterUserPassword`)하는 환경이면
스택 출력 `MySQLClusterSecretArn`·`PostgreSQLClusterSecretArn`의 비밀에서 암호를 꺼내 환경 변수로 넣습니다.
스크립트는 비밀 값을 직접 조회하지 않습니다. Valkey는 복제 그룹의 실제 설정(전송 중 암호화, 클러스터 모드)을 읽어
`VALKEY_TLS`·`VALKEY_CLUSTER`로 넘깁니다(`infrastructure/`는 TLS 켬·클러스터 모드 끔, 루트 2025 스택은 반대).

| 단계 | 하는 일 |
|---|---|
| 1 | CloudFormation 출력에서 DB 엔드포인트 읽기 |
| 2 | `setup/enable-app-signals.sh`: cloudwatch-agent IRSA 역할(CloudWatchAgentServerPolicy + AWSXrayWriteOnlyAccess) → `amazon-cloudwatch-observability` add-on → `application-signals start-discovery` → Transaction Search(색인 1%, `SPAN_INDEXING_PERCENT`로 조정) |
| 3 | 이미지 5개 빌드·ECR push (`linux/amd64`) |
| 4 | Secret: 서비스별 DB 자격 증명(무작위 32자), 장애 시나리오 토큰 |
| 5 | `db-init` Job (shop / inventory / payments DB, `*_app` 사용자, 테이블, 시드) → 마스터 자격 증명 Secret 삭제 |
| 6 | 서비스 3개(각 2 Pod) + traffic-generator(초당 5건) |

이후:

```bash
./app-signals/scenarios/chaos.sh start mysql-lock 600     # 끝나면 rca.py에 넣을 시작·끝 시각을 출력
# SLO는 서비스가 지표를 10분 이상 낸 뒤에 (그 전에는 Application Signals가 거부)
aws cloudformation deploy --stack-name ${WORKSHOP_NAME}-app-signals-slo \
    --template-file app-signals/slo/app-signals-slo-cfn.yaml \
    --parameter-overrides ClusterName=${CLUSTER_NAME} AlarmTopicArn=<alerting 모듈의 Warning 주제 ARN>
./app-signals/cleanup-app-signals.sh        # shop 네임스페이스만
./app-signals/cleanup-app-signals.sh --all  # + SLO 스택, add-on, IAM 역할, ECR
```

대시보드: `dashboards/grafana-rca-app-to-db.json`을 AMG에 Import합니다. 데이터 소스는 변수(`cw`, `amp`)로 고르고
`environment`·`cluster` 값만 맞추면 됩니다. 위에서 아래로 ① 골든 지표 → ② 의존성 → ③ DB 엔진 → ④ 포화 순서입니다.

## 로컬 검증 (AWS 없이)

`local/verify.sh`가 DB 4종·exporter 3종·Prometheus·OTel Collector를 컨테이너로 띄우고, 서비스에 **실제 ADOT SDK**를
얹어(EKS 주입과 같은 방식) 기준 트래픽 → 시나리오 5개를 차례로 돌린 뒤 `local/analyze_local.py`로 두 가지를 확인합니다.

1. 서비스가 Application Signals용 텔레메트리(서비스·오퍼레이션, DB·서비스 의존성, `DB::Connection` 식별자)를 만드는가
2. 시나리오마다 RCA가 기대한 원인을 1순위로 짚는가 (Application Signals 지표 대신 span으로 계산, 엔진 지표는 로컬 Prometheus)

결과는 `local/out/summary.md`, 시나리오별 보고서는 `local/out/rca-<시나리오>.md`에 남습니다.
가장 최근 실행 결과는 아래 [검증 결과](#검증-결과)에 적었습니다.

## 검증 결과

로컬 Docker 1회 실행 (2026-10-21, Apple Silicon, SQL Server는 amd64 에뮬레이션). 기준 구간 20:27:49~20:29:49 UTC, 시나리오마다 60초. span 39,092개, 지표 데이터 포인트 10,146개.

**텔레메트리**: 기대한 의존성 6개 모두 CLIENT span의 `aws.remote.service`로 확인 `order-api → mysql` 5,414건, `order-api → redis` 3,234건, `order-api → inventory-service` 2,115건, `order-api → payment-service` 1,712건, `inventory-service → postgresql` 5,956건, `payment-service → mssql` 5,124건. DB 식별자는 `shop|mysql|3306` 형식(`[db|]host[|port]`), 지표 이름은 `Latency`·`Fault`·`Error`.

| 시나리오 | RCA 1순위 | 확신도 | 근거 계층 | order-api p99 (ms, 기준 → 사고) | 기대와 일치 |
|---|---|---|---|---|---|
| mysql-lock | 잠금 경합 | 높음 | 앱 골든 지표, 의존성 지표, 트레이스, 엔진 지표 | 51 → 5,625 | 예 |
| pg-slow-query | 비효율 쿼리(전체 스캔) | 높음 | 앱 골든 지표, 의존성 지표, 트레이스, 엔진 지표 | 51 → 83 | 예 |
| mssql-blocking | DB 호출 대기(엔진 지표 근거 없음) | 중간 | 앱 골든 지표, 의존성 지표, 트레이스 | 51 → 5,037 | 예 |
| cache-stampede | 캐시 미스 폭증 | 높음 | 의존성 지표, 트레이스, 엔진 지표 | 51 → 35 | 예 |
| app-cpu | 앱 CPU 포화(DB 원인 아님) | 중간 | 앱 골든 지표, 의존성 지표(변화 없음), 런타임 CPU | 51 → 305 | 예 |

기대한 원인을 1순위로 짚은 시나리오 5/5.

경과: 첫 실행에서는 pg-slow-query가 '원인 미확정'으로 실패했습니다(4/5). 호출하는 쪽의 RemoteOperation(`GET /stock`)과 하위 서비스의 오퍼레이션(`GET /stock/<sku>`)이 달라 하위 서비스 데이터를 못 찾은 것이 원인이었습니다. 이를 고치고(이름으로 데이터가 없으면 서비스 전체로), 같은 DB 근거의 중복 제거, 로컬 시드 규모(30만 → 100만 건, EKS 기본값과 같게), 코드 검토 지적 사항(CPU 단위, 트랜잭션 정리, Valkey TLS·클러스터 모드 감지 등)을 반영한 뒤 새 데이터로 다시 돌린 결과가 위 표입니다.

로컬 검증은 Application Signals 지표 대신 span으로 지연·Fault·호출 수를 계산하고(`local/local_collector.py`), SQL Server 엔진 지표는 없습니다(EKS 기본 경로와 같음). 1회 실행 값이며 반복 측정이 아닙니다.

## 실제 배포에서 확인한 것 (별도 환경)

이 모듈의 서비스·스크립트는 아직 EKS에 배포해 보지 않았습니다. 대신 같은 `amazon-cloudwatch-observability` add-on
(v6.7.0-eksbuild.1)으로 Python 서비스 2개(주문 → 재고 → Aurora PostgreSQL)를 계측한 별도 배포(2026-10-12, 서울)에서
X-Ray 트레이스와 Application Signals 응답을 보고, 이 모듈이 기대는 가정을 맞춰 봤습니다.

| 가정 | 실제 값 | 결과 |
|---|---|---|
| Environment는 `eks:<클러스터>/<네임스페이스>` | `eks:<클러스터>/dbaops-demo` | 맞음 |
| 서비스 지표 | `ApplicationSignals` 네임스페이스 `Latency`·`Error`·`Fault`, 차원 [Environment, Service] | 맞음 |
| DB 호출의 원격 서비스·오퍼레이션 | `aws.remote.service=postgresql`, `aws.remote.operation=SELECT` | 맞음 |
| DB 식별자 (`DB::Connection`) | `<db>\|<Aurora 클러스터 엔드포인트>\|5432` → `[db\|]host[\|port]` 형식 | 맞음 |
| 서비스 간 호출의 원격 서비스 이름 | URL의 host:port 그대로 (`<svc>.<ns>.svc.cluster.local:8080`) | 포트만 떼던 처리로는 못 맞춤 → 고침 |

고친 것(`rca/collector.py`, `rca/analysis.py`의 `service_name`): 원격 서비스 이름에서 포트와 쿠버네티스 DNS 접미사를 떼고,
`ListServices`의 계측된 서비스 이름과 같으면 하위 서비스로 따라 내려갑니다. 이전에는 `ListServiceDependencies`가 그 호출을
계측된 서비스(`Type=Service` 또는 `RemoteEnvironment` 차원)로 묶어 줄 때만 내려갔습니다. 실제 응답이 어느 쪽인지는 확인하지
못해서 둘 다 받게 했습니다. 로컬 검증의 span 39,092개에 새 처리를 적용해도 원격 이름은 하나도 바뀌지 않았습니다(로컬은
짧은 이름 `inventory-service:8080`을 씀).

그 배포에서도 확인하지 못한 것: ADOT Node.js(tedious) 계측, `ListServiceDependencies` 응답, Transaction Search(`aws/spans`,
그 배포는 X-Ray 저장 경로를 유지), 이 모듈의 `rca.py`를 실제 API로 돌린 결과.

## DB 운영 에이전트와의 관계

화면과 LLM 조사는 [blait/DBAOps-Agent](https://github.com/blait/DBAOps-Agent)(Streamlit UI · Amazon Bedrock AgentCore ·
MCP 도구)가 맡고, 이 모듈은 그 앞단을 채웁니다.

| | DBAOps-Agent | 이 모듈 (`app-signals/`) |
|---|---|---|
| 맡는 일 | 자연어 질문에 답하는 조사·리포트 화면 | 앱 신호를 내는 데모 서비스, 장애 재현, 규칙 기반 RCA |
| RCA 방식 | 모델이 질문에 맞춰 도구와 순서를 고름 | 고정 절차(골든 지표 → 의존성 → SQL 문장 → 엔진 지표)와 확신도 |
| 결과 | 대화·차트 | Markdown, JSON(`--format json`) |

잇는 방법(제안, 아직 구현하지 않음): `rca.py --format json` 결과를 에이전트의 근거 입력으로 넘겨, 같은 구간을 다시 조사할 때
출발점과 반증 대상으로 씁니다. 규칙이 놓친 원인은 에이전트가 찾고, 모델의 추정은 규칙의 수치 근거로 견제하려는 것입니다.

## 제한과 확인할 점

- **이 모듈을 EKS에 배포해 보지는 않았습니다.** 위 '실제 배포에서 확인한 것' 밖의 부분(Node.js 계측,
  `ListServiceDependencies` 응답 모양, Transaction Search 필드 이름)은 AWS 문서·ADOT 소스 기준으로 썼습니다.
- add-on이 주입하는 ADOT SDK 버전은 add-on 버전을 따릅니다. 로컬 검증은 PyPI·npm의 최신 ADOT로 했습니다.
- SQL Server 엔진의 잠금 근거(suspended 요청, LCK 대기)는 `custom-metrics` 모듈의 collector가 있어야 나옵니다.
  없으면 RCA는 트레이스(타임아웃·느린 UPDATE)까지만 근거로 듭니다.
- 엔진 지표는 exporter 시계열의 `db_instance` 라벨이 RDS 인스턴스 ID와 같다고 가정합니다(디스커버리 모듈의 라벨).
  다르면 `--instance-label`로 바꾸거나, 엔진 전체 값으로 대신한다는 표시가 붙습니다.
- 기본 배포 경로의 exporter 이슈(검토 메모 참고)가 풀려야 AMP에 엔진 지표가 쌓입니다. 그 전에는 CloudWatch 기본 지표로 대신합니다.
- 기본 경로의 postgres_exporter는 `postgres` DB에 접속합니다(`helm-charts/postgres-exporter/values.yaml`). 그래서 `inventory` DB의
  테이블 단위 지표(`pg_stat_user_tables_seq_tup_read`)는 보이지 않고, 클러스터 전체인 `pg_stat_database_tup_returned`만 스캔 근거로 남습니다.
  테이블 단위까지 보려면 exporter에 `--auto-discover-databases`를 켜거나 `inventory` DSN을 더합니다(로컬 검증은 `inventory`에 접속).
- X-Ray 트레이스 요약은 Transaction Search 색인 비율(기본 1%)에 따라 몇 건 안 될 수 있어, 보고서의 '데이터 출처'에 건수를 함께 적습니다.
- 로컬 검증에서 ADOT Node.js의 ServiceEvents 내보내기가 기본 주소(`localhost:4316`)로 가며 연결 오류를 남겼습니다. span·Application Signals 지표
  내보내기와는 별개이고 결과에 영향이 없었습니다. EKS에서는 add-on이 CloudWatch agent 주소를 설정합니다.
- 요금: Application Signals, Container Insights(enhanced observability), X-Ray span 수집과 Transaction Search 색인,
  CloudWatch Logs(`aws/spans`)가 새로 붙습니다. 색인 비율 1%까지는 추가 요금이 없습니다(AWS 문서). 실습 후 `cleanup-app-signals.sh --all`.

## 파일

| 경로 | 내용 |
|---|---|
| `deploy-app-signals.sh` · `cleanup-app-signals.sh` | 배포·정리 |
| `setup/enable-app-signals.sh` | IRSA 역할, add-on, 서비스 검색, Transaction Search |
| `services/` | order-api · inventory-service · payment-service · traffic-generator(+ chaos_client) · db-init, `python-common/`(연결 풀·장애 스위치·gunicorn 설정). 빌드 컨텍스트는 `services/` |
| `k8s/` | 서비스 매니페스트(계측 주석), db-init Job |
| `scenarios/chaos.sh` | 장애 시나리오 켜기·끄기 |
| `rca/` | `rca.py`(CLI) · `collector.py`(AWS 읽기) · `analysis.py`(판단 로직) · `signals.py`(엔진 지표 정의) · `report.py` · `tests/` |
| `slo/app-signals-slo-cfn.yaml` | SLO 3개 + order-api Fault 경보(선택, 기존 SNS 주제로) |
| `dashboards/grafana-rca-app-to-db.json` | AMG 대시보드 |
| `local/` | 로컬 검증(docker compose, Collector·Prometheus 설정, 분석 스크립트) |
