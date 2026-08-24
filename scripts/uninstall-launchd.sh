#!/usr/bin/env bash
# 卸掉本机 LaunchAgent。不删 ~/Library/Application Support/tokenstat 里的库。
set -euo pipefail

LABEL="com.yunxin.tokenstat"
PLIST_DST="$HOME/Library/LaunchAgents/${LABEL}.plist"
USER_ID="$(id -u)"
SERVICE="gui/${USER_ID}/${LABEL}"

launchctl bootout "$SERVICE" 2>/dev/null || true
rm -f "$PLIST_DST"

echo "已卸载：$SERVICE"
echo "数据仍在：$HOME/Library/Application Support/tokenstat/data"
