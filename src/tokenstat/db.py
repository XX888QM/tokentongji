"""SQLite 持久化层：唯一数据源。

设计要点：
- WAL 模式，后台 ingest 线程写、Web 线程并发读。
- 每次操作开独立连接（sqlite 连接很轻），规避跨线程共享坑。
- usage_events 以 dedup_key 唯一去重：
    * Claude：普通消息按 message.id 去重；fallback iterations 使用派生键
    * Codex：dedup_key = f"{file}#{offset}"（每个 token_count 事件差分出一条）
  Claude 用 on_conflict='max' 取 total 更大的完整快照；Codex 用 'ignore' 幂等。
- ingest_state 存断点(offset/inode/size) + Codex carry-forward 上下文(ctx)。
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Iterable, Optional, Sequence

from .models import UsageRecord

_SCHEMA = """
CREATE TABLE IF NOT EXISTS usage_events (
    id                    INTEGER PRIMARY KEY,
    ts                    INTEGER NOT NULL,
    date_local            TEXT    NOT NULL,
    source                TEXT    NOT NULL,
    model                 TEXT    NOT NULL,
    project               TEXT    NOT NULL,
    category              TEXT    NOT NULL DEFAULT 'main',
    input_tokens          INTEGER NOT NULL DEFAULT 0,
    output_tokens         INTEGER NOT NULL DEFAULT 0,
    cache_read_tokens     INTEGER NOT NULL DEFAULT 0,
    cache_creation_tokens INTEGER NOT NULL DEFAULT 0,
    reasoning_tokens      INTEGER NOT NULL DEFAULT 0,
    total_tokens          INTEGER NOT NULL DEFAULT 0,
    session_id            TEXT    NOT NULL DEFAULT '',
    source_file           TEXT    NOT NULL DEFAULT '',
    pos                   INTEGER NOT NULL DEFAULT 0,
    dedup_key             TEXT    NOT NULL UNIQUE
);
CREATE INDEX IF NOT EXISTS idx_events_date   ON usage_events(date_local);
CREATE INDEX IF NOT EXISTS idx_events_source ON usage_events(source);
CREATE INDEX IF NOT EXISTS idx_events_model  ON usage_events(model);
CREATE INDEX IF NOT EXISTS idx_events_proj   ON usage_events(project);
CREATE INDEX IF NOT EXISTS idx_events_cat    ON usage_events(category);

CREATE TABLE IF NOT EXISTS ingest_state (
    source_file TEXT PRIMARY KEY,
    inode       INTEGER NOT NULL,
    offset      INTEGER NOT NULL,
    size        INTEGER NOT NULL DEFAULT 0,
    mtime       REAL    NOT NULL DEFAULT 0,
    ctx         TEXT    NOT NULL DEFAULT '{}'
);
"""


def get_conn(db_path: Path) -> sqlite3.Connection:
    """打开一个配置好的连接（WAL + Row 工厂）。调用方负责关闭。"""
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    conn.execute("PRAGMA busy_timeout=30000;")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    """建表 + 索引（幂等）。"""
    conn.executescript(_SCHEMA)
    conn.commit()


def _row_tuple(r: UsageRecord) -> tuple:
    return (
        r.ts,
        r.date_local,
        r.source,
        r.model,
        r.project,
        r.category,
        r.input_tokens,
        r.output_tokens,
        r.cache_read_tokens,
        r.cache_creation_tokens,
        r.reasoning_tokens,
        r.total_tokens,
        r.session_id,
        r.source_file,
        r.pos,
        r.dedup_key,
    )


_COLS = (
    "ts, date_local, source, model, project, category, "
    "input_tokens, output_tokens, cache_read_tokens, cache_creation_tokens, "
    "reasoning_tokens, total_tokens, session_id, source_file, pos, dedup_key"
)
_PLACEHOLDERS = ",".join(["?"] * 16)

# Claude 旧流式格式：同 message.id 多行递增，整体采用 total 更大的完整快照。
_ON_CONFLICT_MAX = """
ON CONFLICT(dedup_key) DO UPDATE SET
    ts                    = excluded.ts,
    date_local            = excluded.date_local,
    model                 = excluded.model,
    project               = excluded.project,
    category              = excluded.category,
    input_tokens          = excluded.input_tokens,
    output_tokens         = excluded.output_tokens,
    cache_read_tokens     = excluded.cache_read_tokens,
    cache_creation_tokens = excluded.cache_creation_tokens,
    reasoning_tokens      = excluded.reasoning_tokens,
    total_tokens          = excluded.total_tokens,
    session_id            = excluded.session_id,
    source_file           = excluded.source_file,
    pos                   = excluded.pos
WHERE excluded.total_tokens > usage_events.total_tokens
"""

_ON_CONFLICT_REPLACE = """
ON CONFLICT(dedup_key) DO UPDATE SET
    ts                    = excluded.ts,
    date_local            = excluded.date_local,
    model                 = excluded.model,
    project               = excluded.project,
    category              = excluded.category,
    input_tokens          = excluded.input_tokens,
    output_tokens         = excluded.output_tokens,
    cache_read_tokens     = excluded.cache_read_tokens,
    cache_creation_tokens = excluded.cache_creation_tokens,
    reasoning_tokens      = excluded.reasoning_tokens,
    total_tokens          = excluded.total_tokens,
    session_id            = excluded.session_id,
    source_file           = excluded.source_file,
    pos                   = excluded.pos
WHERE excluded.ts != usage_events.ts
   OR excluded.model != usage_events.model
   OR excluded.project != usage_events.project
   OR excluded.category != usage_events.category
   OR excluded.input_tokens != usage_events.input_tokens
   OR excluded.output_tokens != usage_events.output_tokens
   OR excluded.cache_read_tokens != usage_events.cache_read_tokens
   OR excluded.cache_creation_tokens != usage_events.cache_creation_tokens
   OR excluded.reasoning_tokens != usage_events.reasoning_tokens
   OR excluded.total_tokens != usage_events.total_tokens
   OR excluded.session_id != usage_events.session_id
"""


def insert_records(
    conn: sqlite3.Connection,
    records: Iterable[UsageRecord],
    on_conflict: str = "ignore",
) -> int:
    """批量写入，按 dedup_key 去重幂等。返回实际新增或更新行数。

    on_conflict: 'ignore'（默认）| 'max'（Claude，取完整大快照）| 'replace'（Hermes 累计行）。
    """
    rows: Sequence[tuple] = [_row_tuple(r) for r in records]
    if not rows:
        return 0
    if on_conflict == "max":
        sql = (
            f"INSERT INTO usage_events ({_COLS}) VALUES ({_PLACEHOLDERS}) "
            + _ON_CONFLICT_MAX
        )
    elif on_conflict == "replace":
        sql = (
            f"INSERT INTO usage_events ({_COLS}) VALUES ({_PLACEHOLDERS}) "
            + _ON_CONFLICT_REPLACE
        )
    else:
        sql = f"INSERT OR IGNORE INTO usage_events ({_COLS}) VALUES ({_PLACEHOLDERS})"
    before = conn.total_changes
    conn.executemany(sql, rows)
    conn.commit()
    # 带 WHERE 的 UPSERT 只在数据真实变化时计数，重复全表扫描返回 0。
    return conn.total_changes - before


def delete_openclaw_cross_format_duplicates(conn: sqlite3.Connection) -> int:
    """删除同一 session 的 trajectory/v3 完全相同调用，保留 v3 行。"""
    before = conn.total_changes
    conn.execute(
        """
        DELETE FROM usage_events
        WHERE id IN (
            SELECT t.id
            FROM usage_events AS t
            JOIN usage_events AS v
              ON v.source = 'openclaw'
             AND v.source_file = substr(t.source_file, 1, length(t.source_file) - 17) || '.jsonl'
             AND v.ts = t.ts
             AND v.model = t.model
             AND v.input_tokens = t.input_tokens
             AND v.output_tokens = t.output_tokens
             AND v.cache_read_tokens = t.cache_read_tokens
             AND v.cache_creation_tokens = t.cache_creation_tokens
             AND v.total_tokens = t.total_tokens
            WHERE t.source = 'openclaw'
              AND t.source_file LIKE '%.trajectory.jsonl'
        )
        """
    )
    conn.commit()
    return conn.total_changes - before


def delete_dedup_keys(conn: sqlite3.Connection, source: str, keys: Iterable[str]) -> int:
    """删除来源内指定旧去重键，用于日志从临时格式升级为最终格式。"""
    rows = [(source, key) for key in keys]
    if not rows:
        return 0
    before = conn.total_changes
    conn.executemany(
        "DELETE FROM usage_events WHERE source = ? AND dedup_key = ?",
        rows,
    )
    conn.commit()
    return conn.total_changes - before


def get_ingest_state(conn: sqlite3.Connection, source_file: str) -> Optional[dict]:
    """返回 {inode, offset, size, mtime, ctx(dict)}；无记录返回 None。"""
    cur = conn.execute(
        "SELECT inode, offset, size, mtime, ctx FROM ingest_state WHERE source_file = ?",
        (source_file,),
    )
    row = cur.fetchone()
    if row is None:
        return None
    try:
        ctx = json.loads(row["ctx"]) if row["ctx"] else {}
    except (json.JSONDecodeError, TypeError):
        ctx = {}
    return {
        "inode": int(row["inode"]),
        "offset": int(row["offset"]),
        "size": int(row["size"]),
        "mtime": float(row["mtime"]),
        "ctx": ctx,
    }


def set_ingest_state(
    conn: sqlite3.Connection,
    source_file: str,
    inode: int,
    offset: int,
    size: int,
    mtime: float,
    ctx: Optional[dict] = None,
) -> None:
    """更新某文件读取断点 + carry-forward 上下文。"""
    ctx_json = json.dumps(ctx or {}, ensure_ascii=False)
    conn.execute(
        """
        INSERT INTO ingest_state (source_file, inode, offset, size, mtime, ctx)
        VALUES (?,?,?,?,?,?)
        ON CONFLICT(source_file) DO UPDATE SET
            inode = excluded.inode,
            offset = excluded.offset,
            size = excluded.size,
            mtime = excluded.mtime,
            ctx = excluded.ctx
        """,
        (source_file, inode, offset, size, mtime, ctx_json),
    )
    conn.commit()
