#!/usr/bin/env bash
# =============================================================================
# 장애 시나리오 켜기·끄기 (traffic-generator Pod 안의 chaos_client.py 실행)
#
#   ./chaos.sh list
#   ./chaos.sh start <scenario> [seconds=600]
#   ./chaos.sh stop  <scenario>
#   ./chaos.sh status
#
# 시나리오                 서비스              DB에서 보일 증거                         앱에서 보일 증거
#   mysql-lock          order-api           InnoDB row lock waits·current waits      POST /orders 지연, 1205 → Fault
#   cache-stampede      order-api           Valkey keyspace misses↑, PG 조회↑        GET /products 지연, inventory 호출↑
#   pg-slow-query       inventory-service   pg seq_tup_read↑ (inventory_events)     /stock·/reserve 지연, PG SELECT 지연
#   mssql-blocking      payment-service     suspended 요청·LCK 대기(custom-metrics) POST /payments 타임아웃 → 상위 502
#   app-cpu             inventory-service   (변화 없음 — DB가 원인이 아닌 경우)     지연↑, Python CPU↑, DB 의존성 지연은 그대로
# =============================================================================
set -euo pipefail
ACTION="${1:-list}"
SCENARIO="${2:-}"
SECONDS_ARG="${3:-600}"

case "$ACTION" in
    start) ARGS=(start "$SCENARIO" --duration "$SECONDS_ARG") ;;
    stop) ARGS=(stop "$SCENARIO") ;;
    list|status) ARGS=("$ACTION") ;;
    *) sed -n '2,20p' "$0"; exit 2 ;;
esac
kubectl -n shop exec deploy/traffic-generator -- python chaos_client.py "${ARGS[@]}"
