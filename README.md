# AWS Workshop: Integrated Database Monitoring with EKS, AMP, and AMG

이 워크숍은 Amazon EKS, Amazon Managed Service for Prometheus (AMP), Amazon Managed Grafana (AMG)를 사용하여 AWS 데이터베이스에 대한 통합 모니터링 솔루션을 구성하는 과정을 안내합니다.

## 아키텍처 개요

```
┌─────────────────────────────────────────────────────────────────────────────┐
│ Amazon VPC                                                                    │
│                                                                               │
│  ┌─────────────┐     ┌──────────────────────────────────────────┐            │
│  │ Amazon Aurora│     │ Amazon EKS                               │            │
│  │  (MySQL)    │     │                                          │            │
│  │ Writer ─────┼────▶│ MySQL Exporter ×3    ┐                   │            │
│  │ Reader ×2   │     │                      │                   │            │
│  └─────────────┘     │                      ▼                   │            │
│                       │               ┌──────────┐              │            │
│  ┌─────────────┐     │               │   ADOT   │    ┌───────────────────┐  │
│  │ Amazon Aurora│     │               │Collector │───▶│ Amazon Managed    │  │
│  │(PostgreSQL) │     │               │          │    │ Prometheus (AMP)  │  │
│  │ Writer ─────┼────▶│ PG Exporter×3 ┘          │    └────────┬──────────┘  │
│  │ Reader ×2   │     │                           │             │             │
│  └─────────────┘     │ Redis Exporter ──────────┘             │             │
│                       │ MSSQL Exporter ─────────┘              ▼             │
│  ┌─────────────┐     │                                  ┌──────────────┐    │
│  │  RDS SQL    │     └──────────────────────────────────┘│ Amazon       │    │
│  │  Server     ├─────▶ MS SQL Exporter                   │ Managed      │    │
│  └─────────────┘                                         │ Grafana(AMG) │    │
│                                                          └──────┬───────┘    │
│  ┌─────────────┐                                                │            │
│  │ ElastiCache │                                                ▼            │
│  │  (Valkey)   ├─────▶ Redis Exporter              Dashboard + Alerting     │
│  └─────────────┘                                                             │
│                                         Amazon CloudWatch ──────▶ AMG        │
└─────────────────────────────────────────────────────────────────────────────┘
```

### 지원 데이터베이스
| 데이터베이스 | Exporter | 포트 | 비고 |
|---|---|---|---|
| Aurora MySQL | mysqld_exporter | 9104 | Writer 1 + Reader 2 |
| Aurora PostgreSQL | postgres_exporter | 9187 | Writer 1 + Reader 2 |
| RDS SQL Server | mssql_exporter | 4000 | 단일 인스턴스 |
| ElastiCache (Valkey) | redis_exporter | 9121 | Valkey 호환 |

### 메트릭 수집 흐름
1. **PULL**: ADOT Collector가 주기적으로 각 Exporter의 `/metrics` 엔드포인트를 스크래핑 (30초 주기)
2. **PUSH**: ADOT Collector가 수집된 메트릭을 AMP Remote Write API로 전송
3. **Visualize**: AMG가 AMP + CloudWatch 데이터소스에서 메트릭 조회 및 시각화

## Prerequisites

워크숍 시작 전 아래 도구가 설치되어 있어야 합니다:
- AWS CLI (version 2.0.0 이상)
- kubectl (version 1.23.0 이상)
- Helm (version 3.8.0 이상)
- eksctl (version 0.123.0 이상)
- jq (JSON 처리용)
- envsubst (gettext 패키지)

### Quick Installation Guide
```bash
# Helm 설치 (macOS)
brew install helm

# Helm 설치 (Linux)
curl -fsSL -o get_helm.sh https://raw.githubusercontent.com/helm/helm/main/scripts/get-helm-3
chmod 700 get_helm.sh
./get_helm.sh

# jq 설치
# macOS
brew install jq
# Linux
sudo yum install -y jq  # Amazon Linux
sudo apt-get install -y jq  # Ubuntu

# 설치 확인
helm version
kubectl version --client
eksctl version
aws --version
jq --version
```

## Workshop Modules

1. [Module 1: Environment Setup](#module-1-environment-setup)
2. [Module 2: Networking and Database Infrastructure](#module-2-networking-and-database-infrastructure)
3. [Module 3: EKS Cluster Setup (EKS 1.30)](#module-3-eks-cluster-setup-eks-130)
4. [Module 4: Amazon Managed Prometheus Setup](#module-4-amazon-managed-prometheus-setup)
5. [Module 5: Amazon Managed Grafana Setup](#module-5-amazon-managed-grafana-setup)
6. [Module 6: Deploy Monitoring Stack with Helm](#module-6-deploy-monitoring-stack-with-helm)
7. [Module 7: Dashboard Configuration (MySQL + PostgreSQL + SQL Server + CloudWatch)](#module-7-dashboard-configuration)
8. [Module 8: Monitoring Best Practices](#module-8-monitoring-best-practices)

---

## Module 1: Environment Setup

### Step 1: Set up environment variables
```bash
# 환경 변수 설정
export WORKSHOP_NAME=integrated-db-monitoring
export AWS_REGION=$(aws configure get region)
export VPC_CIDR="10.0.0.0/16"
export ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)

# 설정 확인
echo "Workshop Name: $WORKSHOP_NAME"
echo "AWS Region: $AWS_REGION"
echo "AWS Account ID: $ACCOUNT_ID"
echo "Using VPC CIDR: $VPC_CIDR"
```

### Step 2: Clone the workshop repository
```bash
git clone https://github.com/Deokhyun-Lee/aws-db-monitoring-eks-amp.git
cd aws-db-monitoring-eks-amp
```

---

## Module 2: Networking and Database Infrastructure

### Step 1: Deploy networking stack
```bash
aws cloudformation create-stack \
  --stack-name ${WORKSHOP_NAME}-network \
  --template-body file://networking-stack.yaml \
  --parameters \
    ParameterKey=EnvironmentName,ParameterValue=${WORKSHOP_NAME} \
    ParameterKey=VpcCidr,ParameterValue=${VPC_CIDR} \
  --capabilities CAPABILITY_IAM

# 완료 대기
aws cloudformation wait stack-create-complete \
  --stack-name ${WORKSHOP_NAME}-network
```

### Step 2: Store networking outputs
```bash
export VPC_ID=$(aws cloudformation describe-stacks \
  --stack-name ${WORKSHOP_NAME}-network \
  --query 'Stacks[0].Outputs[?OutputKey==`VpcId`].OutputValue' \
  --output text)

export PUBLIC_SUBNETS=$(aws cloudformation describe-stacks \
  --stack-name ${WORKSHOP_NAME}-network \
  --query 'Stacks[0].Outputs[?OutputKey==`PublicSubnets`].OutputValue' \
  --output text)

export PRIVATE_SUBNETS=$(aws cloudformation describe-stacks \
  --stack-name ${WORKSHOP_NAME}-network \
  --query 'Stacks[0].Outputs[?OutputKey==`PrivateSubnets`].OutputValue' \
  --output text)

export DATABASE_SUBNETS=$(aws cloudformation describe-stacks \
  --stack-name ${WORKSHOP_NAME}-network \
  --query 'Stacks[0].Outputs[?OutputKey==`DatabaseSubnets`].OutputValue' \
  --output text)

export PRIVATE_SUBNET_CIDRS=$(aws cloudformation describe-stacks \
  --stack-name ${WORKSHOP_NAME}-network \
  --query 'Stacks[0].Outputs[?OutputKey==`PrivateSubnetCidrs`].OutputValue' \
  --output text)
```

### Step 3: Deploy database stack (Aurora MySQL 3개 + Aurora PostgreSQL 3개 + SQL Server)
```bash
aws cloudformation create-stack \
  --stack-name ${WORKSHOP_NAME}-db \
  --template-body file://database-stack.yaml \
  --parameters \
    ParameterKey=EnvironmentName,ParameterValue=${WORKSHOP_NAME} \
    ParameterKey=VpcId,ParameterValue=$VPC_ID \
    ParameterKey=DatabaseSubnets,ParameterValue=\"$DATABASE_SUBNETS\" \
    ParameterKey=PrivateSubnetCidrs,ParameterValue=\"$PRIVATE_SUBNET_CIDRS\" \
    ParameterKey=PublicSubnets,ParameterValue=\"$PUBLIC_SUBNETS\" \
    ParameterKey=SQLServerMasterUsername,ParameterValue=admin \
    ParameterKey=SQLServerMasterPassword=[REDACTED_PASSWORD]AdminAdmin123! \
  --capabilities CAPABILITY_IAM

# 완료 대기 (15-20분 소요)
aws cloudformation wait stack-create-complete \
  --stack-name ${WORKSHOP_NAME}-db
```

### Step 4: Store database endpoints
```bash
# Aurora MySQL (Writer + Reader 2)
export MYSQL_ENDPOINT1=$(aws cloudformation describe-stacks \
  --stack-name ${WORKSHOP_NAME}-db \
  --query 'Stacks[0].Outputs[?OutputKey==`MySQLInstance1Endpoint`].OutputValue' \
  --output text)

export MYSQL_ENDPOINT2=$(aws cloudformation describe-stacks \
  --stack-name ${WORKSHOP_NAME}-db \
  --query 'Stacks[0].Outputs[?OutputKey==`MySQLInstance2Endpoint`].OutputValue' \
  --output text)

export MYSQL_ENDPOINT3=$(aws cloudformation describe-stacks \
  --stack-name ${WORKSHOP_NAME}-db \
  --query 'Stacks[0].Outputs[?OutputKey==`MySQLInstance3Endpoint`].OutputValue' \
  --output text)

# Aurora PostgreSQL (Writer + Reader 2)
export POSTGRESQL_ENDPOINT1=$(aws cloudformation describe-stacks \
  --stack-name ${WORKSHOP_NAME}-db \
  --query 'Stacks[0].Outputs[?OutputKey==`PostgreSQLInstance1Endpoint`].OutputValue' \
  --output text)

export POSTGRESQL_ENDPOINT2=$(aws cloudformation describe-stacks \
  --stack-name ${WORKSHOP_NAME}-db \
  --query 'Stacks[0].Outputs[?OutputKey==`PostgreSQLInstance2Endpoint`].OutputValue' \
  --output text)

export POSTGRESQL_ENDPOINT3=$(aws cloudformation describe-stacks \
  --stack-name ${WORKSHOP_NAME}-db \
  --query 'Stacks[0].Outputs[?OutputKey==`PostgreSQLInstance3Endpoint`].OutputValue' \
  --output text)

# SQL Server
export SQLSERVER_ENDPOINT=$(aws cloudformation describe-stacks \
  --stack-name ${WORKSHOP_NAME}-db \
  --query 'Stacks[0].Outputs[?OutputKey==`SQLServerInstanceEndpoint`].OutputValue' \
  --output text)

echo "MySQL Endpoints: $MYSQL_ENDPOINT1, $MYSQL_ENDPOINT2, $MYSQL_ENDPOINT3"
echo "PostgreSQL Endpoints: $POSTGRESQL_ENDPOINT1, $POSTGRESQL_ENDPOINT2, $POSTGRESQL_ENDPOINT3"
echo "SQL Server Endpoint: $SQLSERVER_ENDPOINT"
```

### Step 5: Deploy cache stack (ElastiCache Valkey)
```bash
aws cloudformation create-stack \
  --stack-name ${WORKSHOP_NAME}-cache \
  --template-body file://cache-stack.yaml \
  --parameters \
    ParameterKey=EnvironmentName,ParameterValue=${WORKSHOP_NAME} \
    ParameterKey=VpcId,ParameterValue=$VPC_ID \
    ParameterKey=DatabaseSubnets,ParameterValue=\"$DATABASE_SUBNETS\" \
    ParameterKey=PrivateSubnetCidrs,ParameterValue=\"$PRIVATE_SUBNET_CIDRS\" \
  --capabilities CAPABILITY_IAM

aws cloudformation wait stack-create-complete \
  --stack-name ${WORKSHOP_NAME}-cache

export ELASTICACHE_VALKEY_ENDPOINT=$(aws cloudformation describe-stacks \
  --stack-name ${WORKSHOP_NAME}-cache \
  --query 'Stacks[0].Outputs[?OutputKey==`ElastiCacheRedisEndpoint`].OutputValue' \
  --output text)

export ELASTICACHE_VALKEY_PORT=$(aws cloudformation describe-stacks \
  --stack-name ${WORKSHOP_NAME}-cache \
  --query 'Stacks[0].Outputs[?OutputKey==`ElastiCacheRedisPort`].OutputValue' \
  --output text)

echo "ElastiCache Valkey: $ELASTICACHE_VALKEY_ENDPOINT:$ELASTICACHE_VALKEY_PORT"
```

---

## Module 3: EKS Cluster Setup (EKS 1.30)

```bash
# 서브넷 ID 추출
export PRIVATE_SUBNET_1=$(echo $PRIVATE_SUBNETS | cut -d',' -f1)
export PRIVATE_SUBNET_2=$(echo $PRIVATE_SUBNETS | cut -d',' -f2)
export PRIVATE_SUBNET_3=$(echo $PRIVATE_SUBNETS | cut -d',' -f3)
export CLUSTER_NAME="${WORKSHOP_NAME}-eks"

# EKS 클러스터 생성 (15-20분)
envsubst < eks-cluster.yaml > eks-cluster-resolved.yaml
eksctl create cluster -f eks-cluster-resolved.yaml

# OIDC Provider 설정
eksctl utils associate-iam-oidc-provider \
  --region $AWS_REGION \
  --cluster ${CLUSTER_NAME} \
  --approve

# OIDC Provider URL 저장
export OIDC_PROVIDER=$(aws eks describe-cluster \
  --name ${CLUSTER_NAME} \
  --query "cluster.identity.oidc.issuer" \
  --output text | sed -e "s/^https:\/\///")

# 클러스터 확인
kubectl get nodes
```

---

## Module 4: Amazon Managed Prometheus Setup

```bash
# AMP 워크스페이스 생성
aws amp create-workspace \
  --alias ${WORKSHOP_NAME}-amp \
  --tags Name=${WORKSHOP_NAME}-amp

# 워크스페이스 정보 저장
export AMP_WORKSPACE_ID=$(aws amp list-workspaces \
  --alias ${WORKSHOP_NAME}-amp \
  --query 'workspaces[0].workspaceId' \
  --output text)

export AMP_ENDPOINT="https://aps-workspaces.${AWS_REGION}.amazonaws.com/workspaces/${AMP_WORKSPACE_ID}/api/v1/remote_write"

# ADOT Collector용 IAM 역할 생성
cat > adot-trust-policy.json << EOF
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Principal": {
        "Federated": "arn:aws:iam::${ACCOUNT_ID}:oidc-provider/${OIDC_PROVIDER}"
      },
      "Action": "sts:AssumeRoleWithWebIdentity",
      "Condition": {
        "StringEquals": {
          "${OIDC_PROVIDER}:sub": "system:serviceaccount:observability:adot-collector",
          "${OIDC_PROVIDER}:aud": "sts.amazonaws.com"
        }
      }
    }
  ]
}
EOF

aws iam create-role \
  --role-name ${WORKSHOP_NAME}-adot-collector-role \
  --assume-role-policy-document file://adot-trust-policy.json

aws iam attach-role-policy \
  --role-name ${WORKSHOP_NAME}-adot-collector-role \
  --policy-arn arn:aws:iam::aws:policy/AmazonPrometheusRemoteWriteAccess

export ADOT_ROLE_ARN=$(aws iam get-role \
  --role-name ${WORKSHOP_NAME}-adot-collector-role \
  --query 'Role.Arn' \
  --output text)
```

---

## Module 5: Amazon Managed Grafana Setup

### Step 1: AMG 워크스페이스 생성 (CLI)
```bash
# AMG 워크스페이스 생성
aws grafana create-workspace \
  --workspace-name ${WORKSHOP_NAME}-grafana \
  --account-access-type CURRENT_ACCOUNT \
  --authentication-providers AWS_SSO \
  --permission-type SERVICE_MANAGED \
  --workspace-data-sources PROMETHEUS CLOUDWATCH \
  --workspace-role-arn arn:aws:iam::${ACCOUNT_ID}:role/${WORKSHOP_NAME}-grafana-role

# 워크스페이스 ID 저장
export AMG_WORKSPACE_ID=$(aws grafana list-workspaces \
  --query "workspaces[?name=='${WORKSHOP_NAME}-grafana'].id" \
  --output text)

export AMG_ENDPOINT=$(aws grafana describe-workspace \
  --workspace-id ${AMG_WORKSPACE_ID} \
  --query 'workspace.endpoint' \
  --output text)

echo "Grafana URL: https://${AMG_ENDPOINT}"
```

### Step 2: 데이터소스 설정
AMG 콘솔에서 아래 데이터소스를 추가합니다:

1. **Amazon Managed Prometheus**
   - Type: Prometheus
   - URL: `https://aps-workspaces.${AWS_REGION}.amazonaws.com/workspaces/${AMP_WORKSPACE_ID}`
   - Auth: SigV4 Authentication
   - Default Region: ${AWS_REGION}

2. **Amazon CloudWatch**
   - Type: CloudWatch
   - Default Region: ${AWS_REGION}
   - Auth: AWS SDK Default (워크스페이스 IAM 역할 사용)

---

## Module 6: Deploy Monitoring Stack with Helm

### Step 1: Namespace 및 Secrets 생성
```bash
# observability 네임스페이스 생성
kubectl create namespace observability

# MySQL Exporter 인증 정보
kubectl create secret generic mysql-exporter-credentials \
  -n observability \
  --from-literal=datasource="exporter:ExporterPass123!@tcp(${MYSQL_ENDPOINT1}:3306)/"

# PostgreSQL Exporter 인증 정보
kubectl create secret generic postgresql-exporter-credentials \
  -n observability \
  --from-literal=username=exporter \
  --from-literal=password=[REDACTED_PASSWORD]

# MSSQL Exporter 인증 정보
kubectl create secret generic mssql-exporter-credentials \
  -n observability \
  --from-literal=username=exporter \
  --from-literal=password=[REDACTED_PASSWORD]
```

### Step 2: ADOT Collector 배포
```bash
# 환경변수 치환 후 ADOT Collector 배포
envsubst < adot-collector-config.yaml | kubectl apply -f -

# 배포 확인
kubectl get pods -n observability -l app=adot-collector
kubectl logs -n observability -l app=adot-collector --tail=20
```

### Step 3: DB Discovery를 통한 Exporter 자동 배포
```bash
# DB Discovery 스크립트 실행 (단일 실행)
chmod +x db-discovery.sh
./db-discovery.sh --once

# 또는 지속적 디스커버리 (백그라운드)
nohup ./db-discovery.sh &

# 배포된 Exporter 확인
kubectl get deployments -n observability
kubectl get pods -n observability
```

### Step 4: MS SQL Exporter 수동 배포 (대안)
DB Discovery를 사용하지 않을 경우, Helm으로 직접 배포:
```bash
# Helm 차트 설치
helm install mssql-exporter ./helm-charts/mssql-exporter \
  -n observability \
  -f mssql-exporter-values.yaml \
  --set env[0].value=${SQLSERVER_ENDPOINT}
```

### Step 5: Redis Exporter 수동 배포 (대안)
```bash
helm install redis-exporter ./helm-charts/redis-exporter \
  -n observability \
  -f redis-exporter-values.yaml \
  --set env[0].value="redis://${ELASTICACHE_VALKEY_ENDPOINT}:${ELASTICACHE_VALKEY_PORT}"
```

### Step 6: 배포 상태 확인
```bash
# 모든 Exporter Pod 상태 확인
kubectl get pods -n observability -o wide

# 각 Exporter의 메트릭 수집 확인
kubectl port-forward -n observability svc/mysql-exporter 9104:9104 &
curl -s http://localhost:9104/metrics | head -20

kubectl port-forward -n observability svc/postgresql-exporter 9187:9187 &
curl -s http://localhost:9187/metrics | head -20

kubectl port-forward -n observability svc/mssql-exporter 4000:4000 &
curl -s http://localhost:4000/metrics | head -20

kubectl port-forward -n observability svc/redis-exporter 9121:9121 &
curl -s http://localhost:9121/metrics | head -20

# ADOT Collector가 AMP로 전송 중인지 확인
kubectl logs -n observability -l app=adot-collector --tail=50 | grep -i "remote_write"
```

---

## Module 7: Dashboard Configuration

### Prometheus 기반 대시보드 (Exporter 메트릭)
AMG에서 다음 대시보드를 Import합니다:

| 대시보드 | Grafana ID | 설명 |
|---|---|---|
| MySQL Overview | 14057 | MySQL Exporter 메트릭 |
| PostgreSQL Overview | 9628 | PostgreSQL Exporter 메트릭 |
| SQL Server | 커스텀 | MSSQL Exporter 메트릭 |
| Redis/Valkey | 11835 | Redis Exporter 메트릭 |

### CloudWatch 기반 대시보드
CloudWatch 메트릭을 활용한 인프라 레벨 대시보드:

```bash
# CloudWatch 대시보드 JSON Import
# AMG UI → Dashboards → Import → Upload JSON
# 파일: dashboards/cloudwatch-dashboard.json
```

CloudWatch 대시보드에서 제공하는 메트릭:
- **Aurora MySQL/PostgreSQL**: CPUUtilization, FreeableMemory, ReadIOPS, WriteIOPS, DatabaseConnections, ReadLatency, WriteLatency, AuroraReplicaLag
- **RDS SQL Server**: CPUUtilization, FreeableMemory, FreeStorageSpace, ReadIOPS, WriteIOPS
- **ElastiCache**: CPUUtilization, EngineCPUUtilization, BytesUsedForCache, CacheHits, CacheMisses

---

## Module 8: Monitoring Best Practices

### 알림 설정 권장사항
| 메트릭 | Warning | Critical | 설명 |
|---|---|---|---|
| CPU Utilization | > 70% (5분) | > 90% (5분) | 인스턴스 스케일업 고려 |
| Freeable Memory | < 2GB | < 500MB | 메모리 부족 경고 |
| Database Connections | > 80% max | > 95% max | 커넥션 풀 확인 |
| Replica Lag | > 100ms | > 1000ms | 복제 지연 |
| Redis Memory | > 80% maxmemory | > 95% | Eviction 시작점 |
| MSSQL Wait Time | > 5000ms | > 10000ms | 성능 저하 |

### 확장 고려사항
- ADOT Collector: 50개 이상의 인스턴스 모니터링 시 replica 증가 권장
- Scrape interval: 프로덕션은 30초, 개발은 60초 권장
- AMP 보존 기간: 기본 150일, 비용 최적화 필요시 단축

---

## 파일 구조

```
aws-db-monitoring-eks-amp/
├── README.md                          # 이 문서
├── networking-stack.yaml              # VPC/서브넷 CloudFormation
├── database-stack.yaml                # DB 인프라 CloudFormation (Aurora×6 + SQLServer)
├── cache-stack.yaml                   # ElastiCache CloudFormation
├── eks-cluster.yaml                   # EKS 클러스터 설정
├── adot-collector-config.yaml         # ADOT Collector 통합 설정
├── db-discovery.sh                    # Aurora + RDS 통합 디스커버리 스크립트
├── helm-charts/
│   ├── mssql-exporter/
│   │   └── values.yaml                # MS SQL Exporter Helm values
│   └── redis-exporter/
│       └── values.yaml                # Redis Exporter Helm values
├── dashboards/
│   ├── cloudwatch-dashboard.json      # CloudWatch 메트릭 Grafana 대시보드
│   ├── mysql-dashboard.json           # MySQL Exporter 대시보드
│   └── postgresql-dashboard.json      # PostgreSQL Exporter 대시보드
├── app-signals/                       # (확장) CloudWatch Application Signals + 앱→DB RCA, app-signals/README.md
├── deploy-all.sh                      # 원클릭 전체 배포 스크립트
├── cleanup.sh                         # 전체 리소스 정리 스크립트
└── Makefile                           # make 명령으로 쉬운 배포
```

---

## Cleanup

모든 리소스를 삭제하려면:
```bash
# 또는 make cleanup
./cleanup.sh
```

---

## License

This workshop is provided under the MIT License.

## Contributors

- Deokhyun Lee (deokhyun@amazon.com) - Database Specialist SA, AWS Korea
