"""全局配置：数据源路径、端口、刷新间隔、数据库位置。

所有可调项集中在此，避免散落硬编码。可被环境变量覆盖。
"""

from __future__ import annotations

import os
from pathlib import Path

def _env_int(name: str, default: int) -> int:
    val = os.environ.get(name)
    if val is None:
        return default
    try:
        parsed = int(val)
    except ValueError:
        raise ValueError(f"环境变量 {name} 须为整数，收到 {val!r}")
    if parsed <= 0:
        raise ValueError(f"环境变量 {name} 须为正整数，收到 {val!r}")
    return parsed

HOME = Path.home()

# ---- 数据源 ----
CLAUDE_PROJECTS_DIR = HOME / ".claude" / "projects"
CODEX_SESSION_DIRS = (
    HOME / ".codex" / "sessions",
    HOME / ".codex" / "archived_sessions",
)
OPENCODE_DB_PATH = HOME / ".local" / "share" / "opencode" / "opencode.db"
OPENCLAW_SESSION_DIR = HOME / ".openclaw" / "agents" / "main" / "sessions"
HERMES_STATE_DB = HOME / ".hermes" / "state.db"
# Grok CLI 统一日志（shell.turn.inference_done 带真实 token）
GROK_LOG_PATH = Path(os.environ.get("TOKENSTAT_GROK_LOG", str(HOME / ".grok" / "logs" / "unified.jsonl")))

# ---- 数据库 ----
DATA_DIR = Path(os.environ.get("TOKENSTAT_DATA_DIR", str(Path(__file__).resolve().parent.parent.parent / "data")))
DB_PATH = DATA_DIR / "tokenstat.db"

# ---- Web 服务 ----
HOST = os.environ.get("TOKENSTAT_HOST", "127.0.0.1")
PORT = _env_int("TOKENSTAT_PORT", 8787)

# ---- 后台 ingest ----
INGEST_INTERVAL_SEC = _env_int("TOKENSTAT_INGEST_INTERVAL", 60)

# ---- 数据质量：某来源落后最新数据源多少天算「陈旧/停更」 ----
STALE_SOURCE_DAYS = _env_int("TOKENSTAT_STALE_DAYS", 3)

# ---- 前端自动刷新（秒）----
DASHBOARD_REFRESH_SEC = _env_int("TOKENSTAT_REFRESH", 30)

# ---- 时区（仅记录，实际换算在 models.py）----
LOCAL_TZ_NAME = "Asia/Shanghai"

# ---- 单价表 ----
PRICING_PATH = Path(__file__).resolve().parent / "pricing.json"


def ensure_data_dir() -> None:
    """确保数据目录存在。"""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
