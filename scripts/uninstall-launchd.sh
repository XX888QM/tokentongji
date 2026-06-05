#!/usr/bin/env bash
# 卸载 launchd 开机自启。
set -euo pipefail

LABEL="com.tokentongji"
PLIST_DST="$HOME/Library/LaunchAgents/${LABEL}.plist"

if [ -f "$PLIST_DST" ]; then
    launchctl unload -w "$PLIST_DST" 2>/dev/null || true
    rm -f "$PLIST_DST"
    echo "已卸载：$PLIST_DST"
else
    echo "未找到：$PLIST_DST（可能未安装）"
fi
