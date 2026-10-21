#!/usr/bin/env bash
# =============================================================================
# 로컬 검증 (Docker): AWS 없이 아래 두 가지를 확인한다
#   1) 샘플 서비스가 ADOT SDK로 Application Signals용 지표·span을 만드는가
#      (서비스·오퍼레이션, DB·서비스 의존성, DB::Connection 식별자)
#   2) 장애 시나리오 5개마다 RCA 로직이 원인을 맞게 짚는가 (로컬 span + exporter 지표)
#
# 순서: 이미지 빌드 → DB·Collector·exporter·서비스 기동 → 기준 트래픽 → 시나리오 5개(각 SCENARIO_SECONDS)
#       → Collector 기록 분석(analyze_local.py) → out/summary.md, out/rca-*.md
# 걸리는 시간: 약 10~15분 (SQL Server 컨테이너는 Apple Silicon에서 에뮬레이션으로 돈다)
# 정리: docker compose -p shop-local down -v
# =============================================================================
set -euo pipefail
cd "$(dirname "$0")"
SVC=../services
BASELINE_SECONDS="${BASELINE_SECONDS:-120}"
SCENARIO_SECONDS="${SCENARIO_SECONDS:-60}"
COOLDOWN_SECONDS="${COOLDOWN_SECONDS:-30}"
RPS="${RPS:-8}"
TOKEN=local-chaos-token
log() { echo -e "\033[0;34m[verify]\033[0m $(date -u +%H:%M:%S) $*"; }
utc() { date -u +%Y-%m-%dT%H:%M:%SZ; }

log "이미지 빌드"
for s in order-api inventory-service payment-service db-init traffic-generator; do
    docker build -q -t "shop-local/$s" -f "$SVC/$s/Dockerfile" "$SVC" >/dev/null
done
docker build -q -t shop-local/order-api-adot --build-arg BASE=shop-local/order-api -f Dockerfile.adot-python . >/dev/null
docker build -q -t shop-local/inventory-service-adot --build-arg BASE=shop-local/inventory-service -f Dockerfile.adot-python . >/dev/null
docker build -q -t shop-local/payment-service-adot --build-arg BASE=shop-local/payment-service -f Dockerfile.adot-node . >/dev/null

# 공개 레지스트리는 익명 동시 pull에 속도 제한이 있어(ECR Public: toomanyrequests) 하나씩, 실패하면 다시 시도한다
log "외부 이미지 내려받기"
for img in $(docker compose config --images | grep -v '^shop-local/'); do
    docker image inspect "$img" >/dev/null 2>&1 && continue
    for i in 1 2 3 4 5; do
        plat=""; case "$img" in mcr.microsoft.com/mssql/*) plat="--platform linux/amd64" ;; esac
        docker pull -q $plat "$img" >/dev/null 2>&1 && break
        log "  $img 다시 시도 ($i)"; sleep $((i * 10))
    done
done

rm -rf out && mkdir -p out
log "기동 (db-init 포함)"
docker compose up -d --wait order-api inventory-service payment-service prometheus mysqld-exporter postgres-exporter redis-exporter

for port in 28080 28081 28082; do
    for _ in $(seq 1 60); do curl -sf "localhost:$port/health" >/dev/null && break; sleep 2; done
done
NET="$(docker inspect -f '{{range $k, $v := .NetworkSettings.Networks}}{{$k}}{{end}}' shop-local-order-api-1)"
docker rm -f shop-local-traffic >/dev/null 2>&1 || true
docker run -d --name shop-local-traffic --network "$NET" -e TARGET_URL=http://order-api:8080 -e RPS="$RPS" \
    shop-local/traffic-generator >/dev/null
log "워밍업 30초"
sleep 30

B0="$(utc)"; log "기준 구간 ${BASELINE_SECONDS}초"; sleep "$BASELINE_SECONDS"; B1="$(utc)"
SCEN_JSON=""
run() {  # 이름, 포트
    log "시나리오 $1 (${SCENARIO_SECONDS}초)"
    curl -sf -X POST -H "X-Chaos-Token: $TOKEN" "localhost:$2/chaos/$1?duration=$SCENARIO_SECONDS" >/dev/null
    local s; s="$(utc)"
    sleep "$SCENARIO_SECONDS"
    local e; e="$(utc)"
    curl -sf -X DELETE -H "X-Chaos-Token: $TOKEN" "localhost:$2/chaos/$1" >/dev/null || true
    SCEN_JSON="${SCEN_JSON:+$SCEN_JSON,}{\"name\":\"$1\",\"start\":\"$s\",\"end\":\"$e\"}"
    log "회복 대기 ${COOLDOWN_SECONDS}초"; sleep "$COOLDOWN_SECONDS"
}
run mysql-lock 28080
run pg-slow-query 28081
run mssql-blocking 28082
run cache-stampede 28080
run app-cpu 28081
printf '{"baseline":["%s","%s"],"scenarios":[%s]}\n' "$B0" "$B1" "$SCEN_JSON" > out/timeline.json

log "트래픽 중지, span·지표 내보내기 대기"
docker logs --tail 40 shop-local-traffic > out/traffic.log 2>&1 || true
docker rm -f shop-local-traffic >/dev/null
sleep 15
docker compose stop order-api inventory-service payment-service >/dev/null
sleep 5
log "분석"
python3 analyze_local.py out
