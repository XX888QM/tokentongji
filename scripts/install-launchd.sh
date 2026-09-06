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
FORCE_RESEED=false
if [[ "${1:-}" == "--force-reseed" ]]; then
  FORCE_RESEED=true
fi

mkdir -p "$HOME/Library/LaunchAgents" "$SUPPORT_DIR/src" "$SUPPORT_DIR/data" "$LOG_DIR"

# 同步代码（不含测试/缓存/系统垃圾文件）
rsync -a --delete \
  --exclude '__pycache__/' \
  --exclude '*.pyc' \
  --exclude '.DS_Store' \
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

# 先卸 LaunchAgent，防 KeepAlive 在检查缺库时抢先拉起空账本。
launchctl bootout "$SERVICE" 2>/dev/null || true

# 再停占用 8787 的手动服务，再 checkpoint + 拷库，避免 WAL 对半切。
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
#
db_has_events() {
  local db="$1"
  [[ -f "$db" ]] || return 1
  local size
  size="$(stat -f%z "$db" 2>/dev/null || echo 0)"
  [[ "${size:-0}" -ge 100 ]] || return 1
  local n
  n="$(sqlite3 "$db" "SELECT COUNT(*) FROM usage_events;" 2>/dev/null || echo 0)"
  [[ "${n:-0}" -gt 0 ]]
}

# "首装"判断不能只看运行副本有没有库文件——那分不清"从没装过"和"已经跑了
# 很久、库因异常退出/误删/磁盘问题不见了但目录还在"这两种情况。用 $PLIST_DST
# 存不存在做第二重信号：它只在本脚本成功跑到最后才会被写出，所以"$PLIST_DST
# 已存在但库不见了"说明这不是首装，很可能是数据丢失，不能悄悄拿旧快照顶上
# 掩盖过去，得先停下来让人确认。0 字节 / 0 事件也按缺库处理。
if ! db_has_events "$SUPPORT_DIR/data/tokenstat.db"; then
  if [[ -f "$PLIST_DST" && "$FORCE_RESEED" != true ]]; then
    echo "警告：LaunchAgent 之前已装过（$PLIST_DST 存在），但运行库 $SUPPORT_DIR/data/tokenstat.db 不见了。" >&2
    echo "这不是首装，运行副本的库大概率是被误删/异常丢失，不是从没装过。" >&2
    echo "为避免用仓库里可能是几个月前的旧快照悄悄覆盖、掩盖数据丢失，这里停止执行。" >&2
    echo "重建前建议先看看 $SUPPORT_DIR/data/backups/ 里有没有更新的备份可以手动恢复。" >&2
    echo "确认要用仓库 data/tokenstat.db 当种子重建，请带上 --force-reseed 重跑本脚本。" >&2
    exit 1
  fi
  seed=""
  latest_backup="$(ls -t "$SUPPORT_DIR/data/backups"/tokenstat-*.db 2>/dev/null | head -n 1 || true)"
  if [[ -n "${latest_backup:-}" ]]; then
    seed="$latest_backup"
  elif [[ -f "$APP_DIR/data/tokenstat.db" ]]; then
    seed="$APP_DIR/data/tokenstat.db"
  fi
  if [[ -z "$seed" ]]; then
    echo "错误：运行库、备份和仓库种子库都不存在，拒绝启动空账本。" >&2
    echo "请先从 $SUPPORT_DIR/data/backups/ 恢复数据库后再运行。" >&2
    exit 1
  fi
  sqlite3 "$seed" "PRAGMA wal_checkpoint(TRUNCATE);" >/dev/null 2>&1 || true
  cp "$seed" "$SUPPORT_DIR/data/tokenstat.db"
  rm -f "$SUPPORT_DIR/data/tokenstat.db-wal" "$SUPPORT_DIR/data/tokenstat.db-shm"
  if [[ -f "$PLIST_DST" ]]; then
    echo "已用 --force-reseed 确认：用 $seed 重建了运行副本的库。"
  else
    echo "首装：已用 $seed 作为运行副本的初始库。"
  fi
else
  echo "运行副本已有库，保留不覆盖：$SUPPORT_DIR/data/tokenstat.db"
fi

sed -e "s|__SUPPORT_DIR__|$SUPPORT_DIR|g" -e "s|__HOME__|$HOME|g" "$TEMPLATE" > "$PLIST_DST"

launchctl bootstrap "gui/${USER_ID}" "$PLIST_DST"
launchctl kickstart -k "$SERVICE"
launchctl print "$SERVICE" >/dev/null

echo "已安装并启动：$PLIST_DST"
echo "运行副本：$SUPPORT_DIR"
echo "打开： http://127.0.0.1:8787"
echo "改完桌面仓库代码后，再跑一次本脚本才会同步到自启副本。"
