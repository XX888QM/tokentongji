"""Hermes 解析器（~/.hermes/state.db，sessions 表）。

契约（recon 实测）：
- sessions 表按 session 存「当前累计」token 总量(不是逐条增量事件)，且随会话
  进行只会递增。不能用增量游标跳过已读过的 session_id——那样长会话后续增长的
  部分会被永久漏计。改用「全表重扫 + dedup_key=session id + ON CONFLICT MAX」，
  等价于始终写入该 session 的最新累计值，幂等且不会漏计增长。
- reasoning_tokens 是 output_tokens 的展示子集，不另加到总量或费用。
- cwd 列作为 project；source 列是发起平台(cli/telegram/weixin/...)不是 LLM
  供应商，不用于分源，统一挂 source="hermes"。
- parent_session_id 非空 → 视为子会话/委派(delegation)，category=subagent。
- 早于 token 埋点上线的历史 session（无 usage 字段，只有旧格式 session 文件）
  无法恢复，直接跳过——这类文件在 messages 里不含任何 usage/token_count 字段。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import List, Optional

from ..models import CATEGORY_MAIN, CATEGORY_SUBAGENT, SOURCE_HERMES, UsageRecord


def _open_ro(db_path: Path) -> Optional[sqlite3.Connection]:
    """以只读 URI 模式打开 state.db，不存在或无法打开返回 None。"""
    if not db_path.is_file():
        return None
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5.0)
        conn.row_factory = sqlite3.Row
        return conn
    except sqlite3.OperationalError:
        return None


def latest_activity_ts(db_path: Path) -> Optional[int]:
    """返回最新消息时间；用于来源新鲜度，不改变累计 token 的归档日期。"""
    conn = _open_ro(db_path)
    if conn is None:
        return None
    try:
        row = conn.execute("SELECT MAX(timestamp) AS ts FROM messages").fetchone()
    except sqlite3.DatabaseError:
        return None
    finally:
        conn.close()
    try:
        ts = int(float(row["ts"] or 0))
    except (TypeError, ValueError):
        return None
    return ts if ts > 0 else None


def fetch_records(db_path: Path) -> List[UsageRecord]:
    """全量重扫 sessions 表；调用方靠 dedup_key + on_conflict='max' 保证幂等。"""
    conn = _open_ro(db_path)
    if conn is None:
        return []
    try:
        cur = conn.execute(
            "SELECT id, model, cwd, started_at, parent_session_id, "
            "input_tokens, output_tokens, cache_read_tokens, "
            "cache_write_tokens, reasoning_tokens "
            "FROM sessions"
        )
        rows = cur.fetchall()
    finally:
        conn.close()

    records = []
    for row in rows:
        rec = _parse_row(row, db_path)
        if rec is not None:
            records.append(rec)
    return records


def _parse_row(row: sqlite3.Row, db_path: Path) -> Optional[UsageRecord]:
    ts = int(row["started_at"] or 0)
    if ts <= 0:
        return None

    input_tokens = int(row["input_tokens"] or 0)
    cache_read_tokens = int(row["cache_read_tokens"] or 0)
    cache_creation_tokens = int(row["cache_write_tokens"] or 0)
    output_tokens = int(row["output_tokens"] or 0)
    reasoning_tokens = int(row["reasoning_tokens"] or 0)

    total_tokens = input_tokens + output_tokens + cache_read_tokens + cache_creation_tokens
    if total_tokens == 0:
        return None  # 刚创建、还没产生任何 token 的会话

    model = (row["model"] or "unknown").strip()
    cwd = (row["cwd"] or "").strip()
    category = CATEGORY_SUBAGENT if row["parent_session_id"] else CATEGORY_MAIN

    return UsageRecord(
        ts=ts,
        source=SOURCE_HERMES,
        model=model,
        project=cwd or "hermes",
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_read_tokens=cache_read_tokens,
        cache_creation_tokens=cache_creation_tokens,
        reasoning_tokens=reasoning_tokens,
        total_tokens=total_tokens,
        session_id=row["id"] or "",
        source_file=str(db_path),
        pos=0,
        category=category,
        dedup_key=f"hermes:{row['id']}",
    )
