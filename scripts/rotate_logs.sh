#!/bin/bash
# rotate_logs.sh — 清除 log/ 內超過 N 天的舊 daily log
# 用法：scripts/rotate_logs.sh [days]
#   days 預設 7
#
# 只處理檔名形如 *.log.20260516（daily rotation 留下的歷史檔）。
# launchd / uploader 持續寫入的 .stdout.log / .stderr.log 不會被清。

set -euo pipefail

DAYS="${1:-7}"
LOG_DIR="$(cd "$(dirname "$0")/../log" && pwd)"

cd "$LOG_DIR"

DELETED=0
while IFS= read -r -d '' f; do
    rm -f -- "$f"
    DELETED=$((DELETED + 1))
done < <(find . -maxdepth 1 -type f -name "*.log.[0-9]*" -mtime "+${DAYS}" -print0)

echo "[rotate_logs] $(date '+%Y-%m-%d %H:%M:%S')  清掉 ${DELETED} 個 ${DAYS} 天前的 log"
