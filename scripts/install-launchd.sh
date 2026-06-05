#!/usr/bin/env bash
# 安装 launchd 开机自启：用当前项目绝对路径生成 plist 并加载。
set -euo pipefail

APP_DIR="$(cd "$(dirname "$0")/.." && pwd)"
LABEL="com.tokentongji"
PLIST_DST="$HOME/Library/LaunchAgents/${LABEL}.plist"
TEMPLATE="$APP_DIR/launchd/${LABEL}.plist"

mkdir -p "$HOME/Library/LaunchAgents" "$APP_DIR/data"

# 用真实路径替换模板占位符
sed "s|__APP_DIR__|$APP_DIR|g" "$TEMPLATE" > "$PLIST_DST"

# 若已加载先卸载，再加载
launchctl unload -w "$PLIST_DST" 2>/dev/null || true
launchctl load -w "$PLIST_DST"

echo "已安装并启动：$PLIST_DST"
echo "项目目录：$APP_DIR"
echo "打开： http://127.0.0.1:8787"
