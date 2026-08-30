#!/usr/bin/env bash
# 安装本机 LaunchAgent：把 src/库拷到 ~/Library/Application Support/tokenstat
# （避开桌面 TCC），再 bootstrap 开机自启。须在终端里跑，才能读 Desktop。
set -euo pipefail

APP_DIR="$(cd "$(dirname "$0")/.." && pwd)"
LABEL="com.yunxin.tokenstat"
SUPPORT_DIR="$HOME/Library/Application Support/tokenstat"
PLIST_DST="$HOME/Library/LaunchAgents/${LABEL}.plist"
TEMPLATE="$APP_DIR/launchd/${LABEL}.plist"
LOG_DIR="$HOME/Library/Logs/tokenstat"
USER_ID="$(id -u)"
SERVICE="gui/${USER_ID}/${LABEL}"

mkdir -p "$HOME/Library/LaunchAgents" "$SUPPORT_DIR/src" "$SUPPORT_DIR/data" "$LOG_DIR"

# 同步代码（不含测试/缓存）
rsync -a --delete \
  --exclude '__pycache__/' \
  --exclude '*.pyc' \
  "$APP_DIR/src/" "$SUPPORT_DIR/src/"

cat > "$SUPPORT_DIR/start.sh" <<EOF
#!/bin/bash
export PYTHONPATH="$SUPPORT_DIR/src"
export TOKENSTAT_DATA_DIR="$SUPPORT_DIR/data"
export TOKENSTAT_HOST="127.0.0.1"
export TOKENSTAT_PORT="8787"
exec /usr/bin/python3 -m tokenstat.server
EOF
chmod 755 "$SUPPORT_DIR/start.sh"

# 先停占用 8787 的本服务，再 checkpoint + 拷库，避免 WAL 对半切。
if [[ -f "$APP_DIR/data/tokenstat.pid" ]]; then
  old_pid="$(cat "$APP_DIR/data/tokenstat.pid" 2>/dev/null || true)"
  if [[ -n "${old_pid:-}" ]] && ps -p "$old_pid" >/dev/null 2>&1; then
    kill "$old_pid" 2>/dev/null || true
    sleep 1
  fi
fi
if pgrep -f "python3 -m tokenstat.server" >/dev/null 2>&1; then
  pkill -f "python3 -m tokenstat.server" || true
  sleep 1
fi

# 只在首装（运行副本还没有库）时把桌面库迁过去当种子。
# 运行副本一旦建立，它才是唯一真值：桌面 data/ 那份是旧快照，
# 无条件 cp 会把自启进程后来采集到的历史整段抹掉（不可逆）。
if [[ -f "$APP_DIR/data/tokenstat.db" && ! -f "$SUPPORT_DIR/data/tokenstat.db" ]]; then
  sqlite3 "$APP_DIR/data/tokenstat.db" "PRAGMA wal_checkpoint(TRUNCATE);" >/dev/null 2>&1 || true
  cp "$APP_DIR/data/tokenstat.db" "$SUPPORT_DIR/data/tokenstat.db"
  rm -f "$SUPPORT_DIR/data/tokenstat.db-wal" "$SUPPORT_DIR/data/tokenstat.db-shm"
  echo "首装：已用仓库 data/tokenstat.db 作为运行副本的初始库。"
elif [[ -f "$SUPPORT_DIR/data/tokenstat.db" ]]; then
  echo "运行副本已有库，保留不覆盖：$SUPPORT_DIR/data/tokenstat.db"
fi

sed -e "s|__SUPPORT_DIR__|$SUPPORT_DIR|g" -e "s|__HOME__|$HOME|g" "$TEMPLATE" > "$PLIST_DST"

launchctl bootout "$SERVICE" 2>/dev/null || true
launchctl bootstrap "gui/${USER_ID}" "$PLIST_DST"
launchctl kickstart -k "$SERVICE"
launchctl print "$SERVICE" >/dev/null

echo "已安装并启动：$PLIST_DST"
echo "运行副本：$SUPPORT_DIR"
echo "打开： http://127.0.0.1:8787"
echo "改完桌面仓库代码后，再跑一次本脚本才会同步到自启副本。"
