#!/usr/bin/env bash
# =============================================================================
# Application Signals 확장 모듈 정리
#   기본:   shop 네임스페이스(서비스·Secret·Job) 삭제
#   --all:  + SLO 스택, CloudWatch Observability add-on, cloudwatch-agent IAM 역할, ECR 저장소 삭제
# DB 안의 객체(shop / inventory / payments DB와 *_app 사용자)는 남습니다. 기반 스택을 cleanup.sh로 지우면 함께 사라집니다.
# Transaction Search 설정(aws/spans)은 계정 단위라 건드리지 않습니다.
# =============================================================================
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
if [ -f "$REPO_ROOT/.deploy-state.env" ]; then
    set -a; source "$REPO_ROOT/.deploy-state.env"; set +a
fi
WORKSHOP_NAME="${WORKSHOP_NAME:-integrated-db-monitoring}"
CLUSTER_NAME="${CLUSTER_NAME:-${WORKSHOP_NAME}-eks}"
: "${AWS_REGION:?AWS_REGION을 설정하세요}"
ALL=false
[ "${1:-}" = "--all" ] && ALL=true

log() { echo -e "\033[0;34m[app-signals]\033[0m $*"; }

log "shop 네임스페이스 삭제"
kubectl delete namespace shop --ignore-not-found --wait=true

if [ "$ALL" = true ]; then
    if aws cloudformation describe-stacks --stack-name "${WORKSHOP_NAME}-app-signals-slo" --region "$AWS_REGION" >/dev/null 2>&1; then
        log "SLO 스택 삭제"
        aws cloudformation delete-stack --stack-name "${WORKSHOP_NAME}-app-signals-slo" --region "$AWS_REGION"
        aws cloudformation wait stack-delete-complete --stack-name "${WORKSHOP_NAME}-app-signals-slo" --region "$AWS_REGION"
    fi
    log "add-on 삭제: amazon-cloudwatch-observability"
    aws eks delete-addon --cluster-name "$CLUSTER_NAME" --addon-name amazon-cloudwatch-observability \
        --region "$AWS_REGION" >/dev/null 2>&1 || true
    ROLE_NAME="${WORKSHOP_NAME}-cloudwatch-agent-role"
    if aws iam get-role --role-name "$ROLE_NAME" >/dev/null 2>&1; then
        log "IAM 역할 삭제: $ROLE_NAME"
        for policy in CloudWatchAgentServerPolicy AWSXrayWriteOnlyAccess; do
            aws iam detach-role-policy --role-name "$ROLE_NAME" --policy-arn "arn:aws:iam::aws:policy/${policy}" || true
        done
        aws iam delete-role --role-name "$ROLE_NAME"
    fi
    for s in order-api inventory-service payment-service traffic-generator db-init; do
        aws ecr delete-repository --repository-name "${WORKSHOP_NAME}/${s}" --region "$AWS_REGION" --force >/dev/null 2>&1 \
            && log "ECR 삭제: ${WORKSHOP_NAME}/${s}" || true
    done
fi
log "완료"
