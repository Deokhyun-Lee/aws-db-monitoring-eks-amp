#!/usr/bin/env bash
# =============================================================================
# CloudWatch Application Signals 활성화 (EKS)
#   1) cloudwatch-agent 서비스 어카운트용 IRSA 역할
#      (CloudWatchAgentServerPolicy + AWSXrayWriteOnlyAccess, AWS 문서 권장 조합)
#   2) amazon-cloudwatch-observability EKS add-on 설치/갱신
#      (Application Signals + Container Insights, 주석 붙은 Pod에 ADOT SDK 자동 주입)
#   3) Application Signals 서비스 검색 시작 (계정·리전당 한 번, 서비스 연결 역할 생성)
#   4) Transaction Search: X-Ray span을 CloudWatch Logs(aws/spans)로 → RCA에서 SQL 문장 단위 분석
#
# 필요한 값: CLUSTER_NAME, AWS_REGION (없으면 저장소 루트의 .deploy-state.env에서 읽음)
# 선택:      TRANSACTION_SEARCH=false (4단계 건너뜀), SPAN_INDEXING_PERCENT (기본 1, 1%까지 추가 요금 없음)
# 참고:      https://docs.aws.amazon.com/AmazonCloudWatch/latest/monitoring/CloudWatch-Application-Signals-Enable-EKS.html
# =============================================================================
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
if [ -f "$REPO_ROOT/.deploy-state.env" ]; then
    set -a; source "$REPO_ROOT/.deploy-state.env"; set +a
fi
: "${CLUSTER_NAME:?CLUSTER_NAME을 설정하세요 (deploy-all.sh 실행 후에는 .deploy-state.env에 있음)}"
: "${AWS_REGION:?AWS_REGION을 설정하세요}"
WORKSHOP_NAME="${WORKSHOP_NAME:-integrated-db-monitoring}"
TRANSACTION_SEARCH="${TRANSACTION_SEARCH:-true}"
SPAN_INDEXING_PERCENT="${SPAN_INDEXING_PERCENT:-1}"
ADDON=amazon-cloudwatch-observability
ROLE_NAME="${WORKSHOP_NAME}-cloudwatch-agent-role"

ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"
log() { echo -e "\033[0;34m[app-signals]\033[0m $*"; }

# ---------------------------------------------------------------- 1) IRSA 역할
OIDC_PROVIDER="$(aws eks describe-cluster --name "$CLUSTER_NAME" --region "$AWS_REGION" \
    --query 'cluster.identity.oidc.issuer' --output text | sed 's|^https://||')"
TRUST_FILE="$(mktemp)"
trap 'rm -f "$TRUST_FILE"' EXIT
cat > "$TRUST_FILE" <<EOF
{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Principal": {"Federated": "arn:aws:iam::${ACCOUNT_ID}:oidc-provider/${OIDC_PROVIDER}"},
    "Action": "sts:AssumeRoleWithWebIdentity",
    "Condition": {"StringEquals": {
      "${OIDC_PROVIDER}:sub": "system:serviceaccount:amazon-cloudwatch:cloudwatch-agent",
      "${OIDC_PROVIDER}:aud": "sts.amazonaws.com"
    }}
  }]
}
EOF
if aws iam get-role --role-name "$ROLE_NAME" >/dev/null 2>&1; then
    log "IAM 역할 갱신: $ROLE_NAME"
    aws iam update-assume-role-policy --role-name "$ROLE_NAME" --policy-document "file://$TRUST_FILE"
else
    log "IAM 역할 생성: $ROLE_NAME"
    aws iam create-role --role-name "$ROLE_NAME" --assume-role-policy-document "file://$TRUST_FILE" >/dev/null
fi
for policy in CloudWatchAgentServerPolicy AWSXrayWriteOnlyAccess; do
    aws iam attach-role-policy --role-name "$ROLE_NAME" --policy-arn "arn:aws:iam::aws:policy/${policy}"
done
ROLE_ARN="$(aws iam get-role --role-name "$ROLE_NAME" --query Role.Arn --output text)"

# ---------------------------------------------------------------- 2) EKS add-on
if aws eks describe-addon --cluster-name "$CLUSTER_NAME" --addon-name "$ADDON" --region "$AWS_REGION" >/dev/null 2>&1; then
    log "add-on 갱신: $ADDON"
    aws eks update-addon --cluster-name "$CLUSTER_NAME" --addon-name "$ADDON" --region "$AWS_REGION" \
        --service-account-role-arn "$ROLE_ARN" --resolve-conflicts PRESERVE >/dev/null
else
    log "add-on 설치: $ADDON (Container Insights도 함께 켜짐)"
    aws eks create-addon --cluster-name "$CLUSTER_NAME" --addon-name "$ADDON" --region "$AWS_REGION" \
        --service-account-role-arn "$ROLE_ARN" --resolve-conflicts OVERWRITE >/dev/null
fi
aws eks wait addon-active --cluster-name "$CLUSTER_NAME" --addon-name "$ADDON" --region "$AWS_REGION"
log "add-on ACTIVE: $(aws eks describe-addon --cluster-name "$CLUSTER_NAME" --addon-name "$ADDON" \
    --region "$AWS_REGION" --query addon.addonVersion --output text)"

# ---------------------------------------------------------------- 3) 서비스 검색
log "Application Signals 서비스 검색 시작 (이미 켜져 있으면 그대로)"
aws application-signals start-discovery --region "$AWS_REGION" >/dev/null

# ---------------------------------------------------------------- 4) Transaction Search
if [ "$TRANSACTION_SEARCH" = "true" ]; then
    log "Transaction Search 설정 (span → aws/spans, 색인 비율 ${SPAN_INDEXING_PERCENT}%)"
    aws logs put-resource-policy --region "$AWS_REGION" --policy-name TransactionSearchXRayAccess \
        --policy-document "{
          \"Version\": \"2012-10-17\",
          \"Statement\": [{
            \"Sid\": \"TransactionSearchXRayAccess\",
            \"Effect\": \"Allow\",
            \"Principal\": {\"Service\": \"xray.amazonaws.com\"},
            \"Action\": \"logs:PutLogEvents\",
            \"Resource\": [
              \"arn:aws:logs:${AWS_REGION}:${ACCOUNT_ID}:log-group:aws/spans:*\",
              \"arn:aws:logs:${AWS_REGION}:${ACCOUNT_ID}:log-group:/aws/application-signals/data:*\"
            ],
            \"Condition\": {
              \"ArnLike\": {\"aws:SourceArn\": \"arn:aws:xray:${AWS_REGION}:${ACCOUNT_ID}:*\"},
              \"StringEquals\": {\"aws:SourceAccount\": \"${ACCOUNT_ID}\"}
            }
          }]
        }" >/dev/null
    aws xray update-trace-segment-destination --region "$AWS_REGION" --destination CloudWatchLogs >/dev/null
    aws xray update-indexing-rule --region "$AWS_REGION" --name Default \
        --rule "{\"Probabilistic\": {\"DesiredSamplingPercentage\": ${SPAN_INDEXING_PERCENT}}}" >/dev/null
    log "확인: aws xray get-trace-segment-destination --region $AWS_REGION  (Status가 ACTIVE가 될 때까지 최대 10분)"
fi

log "완료. 확인: kubectl -n amazon-cloudwatch get pods"
log "주석(instrumentation.opentelemetry.io/inject-*)이 붙은 워크로드는 다음 재시작부터 계측됩니다."
