#!/bin/bash
# health_check.sh — 打 Railway /health；失敗發 TG 通知
# 設計：
#   * 失敗時建立 sentinel 檔，避免每 5 分鐘重複告警
#   * 從失敗轉成功時，發「恢復」通知並清除 sentinel
#
# 從 .env 讀 RAILWAY_API_URL / TG_BOT_TOKEN / TG_CHAT_ID

set -euo pipefail

cd "$(dirname "$0")/.."

# 載入 .env（不覆寫 launchd 已設的環境變數）
if [ -f .env ]; then
    set -a
    # shellcheck disable=SC1091
    source .env
    set +a
fi

: "${RAILWAY_API_URL:?RAILWAY_API_URL 未設}"
: "${TG_BOT_TOKEN:?TG_BOT_TOKEN 未設}"
: "${TG_CHAT_ID:?TG_CHAT_ID 未設}"

URL="${RAILWAY_API_URL%/}/health"
SENTINEL="/tmp/railway_health_failed"

send_tg() {
    local msg="$1"
    curl -sS -X POST \
        "https://api.telegram.org/bot${TG_BOT_TOKEN}/sendMessage" \
        --data-urlencode "chat_id=${TG_CHAT_ID}" \
        --data-urlencode "text=${msg}" \
        > /dev/null 2>&1 || true
}

# 抓 /health，10 秒 timeout
RESP=$(curl -sS --max-time 10 "$URL" 2>&1) || RESP="CURL_FAIL: $RESP"

if echo "$RESP" | grep -q '"status":"ok"'; then
    # 健康 — 如果之前掛過，發恢復通知
    if [ -f "$SENTINEL" ]; then
        send_tg "✅ Railway API 恢復正常
URL: ${URL}"
        rm -f "$SENTINEL"
    fi
    exit 0
fi

# 失敗 — 已發過告警就跳過（避免每 5 分鐘洗版）
if [ -f "$SENTINEL" ]; then
    exit 1
fi

touch "$SENTINEL"
send_tg "🚨 Railway API health check 失敗
URL: ${URL}
回應: ${RESP:0:200}"

exit 1
