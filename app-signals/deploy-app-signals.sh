#!/usr/bin/env bash
# =============================================================================
# Application Signals 확장 모듈 배포 (deploy-all.sh로 만든 환경 위에)
#
#   1) DB 엔드포인트를 CloudFormation 출력에서 읽음 (${WORKSHOP_NAME}-db, ${WORKSHOP_NAME}-cache)
#   2) Application Signals 활성화 (setup/enable-app-signals.sh)
#   3) 샘플 서비스 이미지 빌드·ECR push (linux/amd64)
#   4) Secret 생성: 서비스별 DB 자격 증명(무작위 암호), 장애 시나리오 토큰, 일회용 마스터 자격 증명
#   5) db-init Job으로 DB·사용자·테이블 생성 → 마스터 자격 증명 Secret 삭제
#   6) order-api · inventory-service · payment-service · traffic-generator 배포
#
# 사용법:
#   export MYSQL_MASTER_PASSWORD=... PG_MASTER_PASSWORD=... MSSQL_MASTER_PASSWORD=...   # database-stack 파라미터 값
#     (루트의 2025년 database-stack.yaml처럼 RDS가 마스터 암호를 관리하면, 스택 출력 *ClusterSecretArn의 비밀에서
#      암호를 직접 꺼내 넣습니다. 이 스크립트는 비밀 값을 조회하지 않습니다)
#   ./app-signals/deploy-app-signals.sh [--skip-enable] [--skip-build] [--skip-init] [--image-tag TAG]
#
# 마스터 사용자 이름 기본값은 database-stack.yaml 기본값(admin / postgres / admin).
# 바꿨다면 MYSQL_MASTER_USER, PG_MASTER_USER, MSSQL_MASTER_USER로 지정합니다.
# =============================================================================
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$HERE/.." && pwd)"
if [ -f "$REPO_ROOT/.deploy-state.env" ]; then
    set -a; source "$REPO_ROOT/.deploy-state.env"; set +a
fi
WORKSHOP_NAME="${WORKSHOP_NAME:-integrated-db-monitoring}"
CLUSTER_NAME="${CLUSTER_NAME:-${WORKSHOP_NAME}-eks}"
: "${AWS_REGION:?AWS_REGION을 설정하세요}"
export WORKSHOP_NAME CLUSTER_NAME AWS_REGION

SKIP_ENABLE=false; SKIP_BUILD=false; SKIP_INIT=false
# 태그 = 커밋 + 서비스 소스 해시. 커밋하지 않은 수정도 새 태그가 되어 롤아웃된다
SRC_HASH="$(cd "$HERE/services" && find . -type f -not -path '*/node_modules/*' -print0 | sort -z | xargs -0 shasum | shasum | cut -c1-8)"
IMAGE_TAG="$(git -C "$REPO_ROOT" rev-parse --short HEAD 2>/dev/null || echo nogit)-${SRC_HASH}"
while [ $# -gt 0 ]; do
    case "$1" in
        --skip-enable) SKIP_ENABLE=true ;;
        --skip-build) SKIP_BUILD=true ;;
        --skip-init) SKIP_INIT=true ;;
        --image-tag) IMAGE_TAG="$2"; shift ;;
        -h|--help) sed -n '2,20p' "$0"; exit 0 ;;
        *) echo "알 수 없는 옵션: $1" >&2; exit 2 ;;
    esac
    shift
done

log() { echo -e "\033[0;34m[app-signals]\033[0m $*"; }
need() { command -v "$1" >/dev/null || { echo "필요한 도구가 없습니다: $1" >&2; exit 1; }; }
for t in aws kubectl docker envsubst openssl; do need "$t"; done

ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"
REGISTRY="${ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com"
export IMAGE_REGISTRY="${REGISTRY}/${WORKSHOP_NAME}" IMAGE_TAG
aws eks update-kubeconfig --name "$CLUSTER_NAME" --region "$AWS_REGION" >/dev/null

# ---------------------------------------------------------------- 1) DB 엔드포인트
output() {
    aws cloudformation describe-stacks --stack-name "$1" --region "$AWS_REGION" \
        --query "Stacks[0].Outputs[?OutputKey=='$2'].OutputValue" --output text
}
MYSQL_HOST="$(output "${WORKSHOP_NAME}-db" MySQLClusterEndpoint)"
PG_HOST="$(output "${WORKSHOP_NAME}-db" PostgreSQLClusterEndpoint)"
MSSQL_HOST="$(output "${WORKSHOP_NAME}-db" SQLServerInstanceEndpoint)"
VALKEY_HOST="$(output "${WORKSHOP_NAME}-cache" ElastiCacheRedisEndpoint)"
VALKEY_PORT="$(output "${WORKSHOP_NAME}-cache" ElastiCacheRedisPort)"
# 캐시 스택은 세대마다 다르다(infrastructure/: TLS 켬·클러스터 모드 끔, 루트 2025: TLS 끔·클러스터 모드 켬). 실제 값을 읽는다
RG_ID="$(aws cloudformation describe-stack-resource --stack-name "${WORKSHOP_NAME}-cache" --region "$AWS_REGION" \
    --logical-resource-id ElastiCacheReplicationGroup --query StackResourceDetail.PhysicalResourceId --output text)"
read -r VALKEY_TLS VALKEY_CLUSTER < <(aws elasticache describe-replication-groups --replication-group-id "$RG_ID" \
    --region "$AWS_REGION" --query 'ReplicationGroups[0].[TransitEncryptionEnabled, ClusterEnabled]' --output text \
    | tr '[:upper:]' '[:lower:]')
for v in MYSQL_HOST PG_HOST MSSQL_HOST VALKEY_HOST; do
    [ -n "${!v}" ] && [ "${!v}" != "None" ] || { echo "$v를 CloudFormation 출력에서 찾지 못했습니다" >&2; exit 1; }
done
log "DB: mysql=${MYSQL_HOST%%.*} postgres=${PG_HOST%%.*} sqlserver=${MSSQL_HOST%%.*} valkey=${VALKEY_HOST%%.*} (tls=${VALKEY_TLS}, cluster=${VALKEY_CLUSTER})"

# ---------------------------------------------------------------- 2) Application Signals
if [ "$SKIP_ENABLE" = false ]; then
    "$HERE/setup/enable-app-signals.sh"
fi

# ---------------------------------------------------------------- 3) 이미지
SERVICES="order-api inventory-service payment-service traffic-generator db-init"
if [ "$SKIP_BUILD" = false ]; then
    aws ecr get-login-password --region "$AWS_REGION" | docker login --username AWS --password-stdin "$REGISTRY" >/dev/null
    for s in $SERVICES; do
        aws ecr describe-repositories --repository-names "${WORKSHOP_NAME}/${s}" --region "$AWS_REGION" >/dev/null 2>&1 || \
            aws ecr create-repository --repository-name "${WORKSHOP_NAME}/${s}" --region "$AWS_REGION" \
                --image-scanning-configuration scanOnPush=true >/dev/null
        log "이미지 빌드·push: ${s}:${IMAGE_TAG}"
        docker buildx build --platform linux/amd64 --push -t "${IMAGE_REGISTRY}/${s}:${IMAGE_TAG}" \
            -f "$HERE/services/${s}/Dockerfile" "$HERE/services"
    done
fi

# ---------------------------------------------------------------- 4) Secret
umask 077
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
kubectl apply -f - >/dev/null <<EOF
apiVersion: v1
kind: Namespace
metadata: { name: shop }
EOF

# 기존 Secret의 값을 다시 쓰고(재실행 시 암호 유지), 없으면 무작위로 만든다
secret_value() {
    kubectl -n shop get secret "$1" -o "jsonpath={.data.$2}" 2>/dev/null | base64 -d 2>/dev/null || true
}
random_password() { openssl rand -base64 48 | tr -dc 'A-Za-z0-9' | head -c 32; }
apply_secret() {  # name, env-file
    kubectl -n shop create secret generic "$1" --from-env-file="$2" --dry-run=client -o yaml | kubectl apply -f - >/dev/null
}

MYSQL_APP_PASSWORD="$(secret_value shop-order-api-db MYSQL_PASSWORD)"; MYSQL_APP_PASSWORD="${MYSQL_APP_PASSWORD:-$(random_password)}"
PG_APP_PASSWORD="$(secret_value shop-inventory-db PG_PASSWORD)"; PG_APP_PASSWORD="${PG_APP_PASSWORD:-$(random_password)}"
MSSQL_APP_PASSWORD="$(secret_value shop-payment-db MSSQL_PASSWORD)"; MSSQL_APP_PASSWORD="${MSSQL_APP_PASSWORD:-$(random_password)}"
CHAOS_TOKEN="$(secret_value shop-chaos CHAOS_TOKEN)"; CHAOS_TOKEN="${CHAOS_TOKEN:-$(random_password)}"

cat > "$WORK/order-api.env" <<EOF
MYSQL_HOST=${MYSQL_HOST}
MYSQL_USER=shop_app
MYSQL_PASSWORD=${MYSQL_APP_PASSWORD}
MYSQL_DATABASE=shop
VALKEY_HOST=${VALKEY_HOST}
VALKEY_PORT=${VALKEY_PORT}
VALKEY_TLS=${VALKEY_TLS}
VALKEY_CLUSTER=${VALKEY_CLUSTER}
EOF
cat > "$WORK/inventory.env" <<EOF
PG_HOST=${PG_HOST}
PG_USER=inventory_app
PG_PASSWORD=${PG_APP_PASSWORD}
PG_DATABASE=inventory
EOF
cat > "$WORK/payment.env" <<EOF
MSSQL_HOST=${MSSQL_HOST}
MSSQL_USER=payment_app
MSSQL_PASSWORD=${MSSQL_APP_PASSWORD}
MSSQL_DATABASE=payments
EOF
echo "CHAOS_TOKEN=${CHAOS_TOKEN}" > "$WORK/chaos.env"
apply_secret shop-order-api-db "$WORK/order-api.env"
apply_secret shop-inventory-db "$WORK/inventory.env"
apply_secret shop-payment-db "$WORK/payment.env"
apply_secret shop-chaos "$WORK/chaos.env"
log "Secret 적용: shop-order-api-db, shop-inventory-db, shop-payment-db, shop-chaos"

# ---------------------------------------------------------------- 5) db-init
if [ "$SKIP_INIT" = false ]; then
    : "${MYSQL_MASTER_PASSWORD:?MYSQL_MASTER_PASSWORD를 설정하세요 (database-stack의 MySQLMasterPassword)}"
    : "${PG_MASTER_PASSWORD:?PG_MASTER_PASSWORD를 설정하세요 (database-stack의 PostgreSQLMasterPassword)}"
    : "${MSSQL_MASTER_PASSWORD:?MSSQL_MASTER_PASSWORD를 설정하세요 (database-stack의 SQLServerMasterPassword)}"
    cat > "$WORK/master.env" <<EOF
MYSQL_HOST=${MYSQL_HOST}
MYSQL_MASTER_USER=${MYSQL_MASTER_USER:-admin}
MYSQL_MASTER_PASSWORD=${MYSQL_MASTER_PASSWORD}
MYSQL_APP_PASSWORD=${MYSQL_APP_PASSWORD}
PG_HOST=${PG_HOST}
PG_MASTER_USER=${PG_MASTER_USER:-postgres}
PG_MASTER_PASSWORD=${PG_MASTER_PASSWORD}
PG_APP_PASSWORD=${PG_APP_PASSWORD}
MSSQL_HOST=${MSSQL_HOST}
MSSQL_MASTER_USER=${MSSQL_MASTER_USER:-admin}
MSSQL_MASTER_PASSWORD=${MSSQL_MASTER_PASSWORD}
MSSQL_APP_PASSWORD=${MSSQL_APP_PASSWORD}
EOF
    apply_secret shop-db-master "$WORK/master.env"
    kubectl -n shop delete job shop-db-init --ignore-not-found >/dev/null
    envsubst '${IMAGE_REGISTRY} ${IMAGE_TAG}' < "$HERE/k8s/db-init-job.yaml" | kubectl apply -f - >/dev/null
    log "db-init 실행 중 (inventory_events 시드 100만 건 포함, 수 분 걸릴 수 있음)"
    status="0/0"
    for _ in $(seq 1 180); do   # 최대 15분. backoffLimit(2)까지 실패하면 바로 멈춘다
        status="$(kubectl -n shop get job shop-db-init -o jsonpath='{.status.succeeded}/{.status.failed}')"
        succeeded="${status%%/*}"; failed="${status#*/}"
        [ "${succeeded:-0}" = "1" ] && break
        [ "${failed:-0}" -gt 2 ] && break
        sleep 5
    done
    if [ "${status%%/*}" != "1" ]; then
        kubectl -n shop logs job/shop-db-init --tail=50 || true
        kubectl -n shop delete secret shop-db-master --ignore-not-found >/dev/null
        echo "db-init 실패 (succeeded/failed=${status})" >&2; exit 1
    fi
    kubectl -n shop delete secret shop-db-master >/dev/null
    log "db-init 완료, 마스터 자격 증명 Secret 삭제"
fi

# ---------------------------------------------------------------- 6) 서비스
envsubst '${IMAGE_REGISTRY} ${IMAGE_TAG}' < "$HERE/k8s/shop.yaml" | kubectl apply -f - >/dev/null
for d in order-api inventory-service payment-service traffic-generator; do
    kubectl -n shop rollout status "deployment/$d" --timeout=300s
done

cat <<EOF

배포 완료. 5~10분 뒤 CloudWatch 콘솔 → Application Signals → Services에서 order-api 등이 보입니다.
  서비스 맵:      CloudWatch → Application Signals → Application Map
  장애 시나리오:  ./app-signals/scenarios/chaos.sh start mysql-lock 600
  RCA:            python app-signals/rca/rca.py --environment eks:${CLUSTER_NAME}/shop --service order-api --minutes 15
  대시보드:       app-signals/dashboards/grafana-rca-app-to-db.json 을 AMG에 Import
  SLO:            트래픽이 10분 이상 흐른 뒤 app-signals/slo/app-signals-slo-cfn.yaml 배포 (README 참고)
EOF
