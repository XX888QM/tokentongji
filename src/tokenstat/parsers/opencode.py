"""opencode SQLite 数据库解析器（~/.local/share/opencode/opencode.db）。

opencode 将消息直接存到 SQLite message 表，每条 assistant 消息含：
  tokens.{input, output, reasoning, cache.read, cache.write}
  path.cwd / modelID / providerID / session_id / time_created(ms)

增量策略：记录上次同步到的最大 time_created（毫秒），下次取 >= last_ts_ms 的行。
用 >= 而非 > 是为了不漏掉与游标同毫秒、但写入晚于上轮扫描的消息；
边界行会被重复读到，但 dedup_key(opencode:{id}) 保证 on_conflict='ignore' 挡掉重复，
不会重复计数。无需文件 offset 机制。
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import List, Optional, Tuple

from ..models import CATEGORY_MAIN, SOURCE_OPENCODE, UsageRecord

# ingest_state 表里用这个 key 存断点（fake source_file）
OPENCODE_STATE_KEY = "opencode:db"


def _open_ro(db_path: Path) -> Optional[sqlite3.Connection]:
    """以只读 URI 模式打开 opencode.db，不存在或无法打开返回 None。"""
    if not db_path.is_file():
        return None
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5.0)
        conn.row_factory = sqlite3.Row
        return conn
    except sqlite3.OperationalError:
        return None


def fetch_records(
    db_path: Path, since_ts_ms: int = 0
) -> Tuple[List[UsageRecord], int]:
    """从 opencode.db 读取 since_ts_ms 之后的 assistant token 消息。

    返回 (records, max_ts_ms)，max_ts_ms 供下次增量用。
    """
    conn = _open_ro(db_path)
    if conn is None:
        return [], since_ts_ms

    try:
        cur = conn.execute(
            """
            SELECT id, session_id, time_created, data
            FROM message
            WHERE json_extract(data, '$.role') = 'assistant'
              AND json_extract(data, '$.tokens') IS NOT NULL
              AND time_created >= ?
            ORDER BY time_created ASC
            """,
            (since_ts_ms,),
        )
        rows = cur.fetchall()
    finally:
        conn.close()

    records: List[UsageRecord] = []
    max_ts_ms = since_ts_ms

    for row in rows:
        rec = _parse_row(row, db_path)
        if rec is not None:
            records.append(rec)
            ts_ms = int(row["time_created"])
            if ts_ms > max_ts_ms:
                max_ts_ms = ts_ms

    return records, max_ts_ms


def _parse_row(row: sqlite3.Row, db_path: Path) -> Optional[UsageRecord]:
    """把一条 message 行转成 UsageRecord，解析失败返回 None。"""
    try:
        data = json.loads(row["data"])
    except (json.JSONDecodeError, TypeError):
        return None

    tokens = data.get("tokens")
    if not isinstance(tokens, dict):
        return None

    ts = int(row["time_created"]) // 1000
    if ts <= 0:
        return None

    model = (data.get("modelID") or "unknown").strip()
    cwd = ((data.get("path") or {}).get("cwd") or "").strip()
    session_id = (row["session_id"] or "").strip()

    cache = tokens.get("cache") or {}
    input_tokens = int(tokens.get("input") or 0)
    output_tokens = int(tokens.get("output") or 0)
    reasoning_tokens = int(tokens.get("reasoning") or 0)
    cache_read_tokens = int(cache.get("read") or 0)
    cache_creation_tokens = int(cache.get("write") or 0)
    total_tokens = int(tokens.get("total") or 0)

    if total_tokens == 0:
        total_tokens = (
            input_tokens
            + output_tokens
            + reasoning_tokens
            + cache_read_tokens
            + cache_creation_tokens
        )

    # 全 0 无意义，跳过
    if total_tokens == 0:
        return None

    return UsageRecord(
        ts=ts,
        source=SOURCE_OPENCODE,
        model=model,
        project=cwd,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_read_tokens=cache_read_tokens,
        cache_creation_tokens=cache_creation_tokens,
        reasoning_tokens=reasoning_tokens,
        total_tokens=total_tokens,
        request_prompt_tokens=input_tokens + cache_read_tokens,
        session_id=session_id,
        source_file=str(db_path),
        pos=0,
        category=CATEGORY_MAIN,
        dedup_key=f"opencode:{row['id']}",
    )
