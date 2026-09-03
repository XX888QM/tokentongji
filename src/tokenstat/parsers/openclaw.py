"""openclaw 日志解析器，支持三种格式：

trajectory（*.trajectory.jsonl）：
  type=="model.completed"，提取 data.usage / promptCache / sessionKey
  dedup_key = "openclaw:{runId}:{seq}"

v3 session（*.jsonl，无 "trajectory"）：
  第一行 type=="session" 作上下文（session_id、cwd）
  type=="message" + role=="assistant" 提取 message.usage
  dedup_key = "openclaw-v3:{msg_id}"

sqlite（~/.openclaw/agents/*/agent/openclaw-agent.sqlite）：
  2026-09 起 jsonl 迁进 transcript_events.event_json，仍是 v3 行。
  复用 parse_v3_record + 同一 dedup_key，on_conflict=ignore 挡住已入库 jsonl。
  不读 trajectory_runtime_events：里面的 model.completed 是合计行，会和
  message 双计。同一 message id 可能被窗口拷贝多次，必须按 id 去重，
  不能用 (session_id, seq)。全表重扫，表很小。
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import List, Optional

from ..models import CATEGORY_MAIN, SOURCE_OPENCLAW, UsageRecord, parse_iso_utc

_EVENT_TYPE = "model.completed"


def _project_from_session_key(session_key: str) -> str:
    """旧键 agent:main:openclaw-weixin:direct:... → openclaw-weixin；
    新键 openclaw-weixin:direct/group → openclaw-weixin。"""
    if not session_key:
        return "openclaw"
    parts = session_key.split(":")
    if len(parts) >= 3 and parts[0] == "agent":
        return parts[2] or "openclaw"
    return parts[0] or "openclaw"


def parse_record(obj: dict, source_file: str, pos: int) -> Optional[UsageRecord]:
    """把一条 jsonl 解析成 UsageRecord；不符合条件返回 None。"""
    if not isinstance(obj, dict):
        return None
    if obj.get("type") != _EVENT_TYPE:
        return None

    data = obj.get("data")
    if not isinstance(data, dict):
        return None

    usage = data.get("usage")
    if not isinstance(usage, dict):
        return None

    # 跳过 aborted 调用（usage 全 0 无意义）
    if data.get("aborted"):
        return None

    ts = parse_iso_utc(obj.get("ts", ""))
    if ts == 0:
        return None

    session_id = obj.get("sessionId") or ""
    run_id = obj.get("runId") or session_id
    seq = int(obj.get("seq") or 0)
    dedup_key = f"openclaw:{run_id}:{seq}"

    model = (obj.get("modelId") or "unknown").strip()
    session_key = obj.get("sessionKey") or ""
    project = _project_from_session_key(session_key)

    input_tokens = int(usage.get("input") or 0)
    output_tokens = int(usage.get("output") or 0)
    cache_read_tokens = int(usage.get("cacheRead") or 0)
    total_raw = int(usage.get("total") or 0)

    # cacheWrite 在 promptCache.lastCallUsage 里（并非每次调用都有）
    prompt_cache = data.get("promptCache") or {}
    last_call = prompt_cache.get("lastCallUsage") or {}
    cache_creation_tokens = int(last_call.get("cacheWrite") or 0)

    total_tokens = total_raw + cache_creation_tokens
    if total_tokens == 0:
        return None

    return UsageRecord(
        ts=ts,
        source=SOURCE_OPENCLAW,
        model=model,
        project=project,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_read_tokens=cache_read_tokens,
        cache_creation_tokens=cache_creation_tokens,
        reasoning_tokens=0,
        total_tokens=total_tokens,
        request_prompt_tokens=input_tokens + cache_read_tokens,
        session_id=session_id,
        source_file=source_file,
        pos=pos,
        category=CATEGORY_MAIN,
        dedup_key=dedup_key,
    )


def parse_v3_record(obj: dict, source_file: str, pos: int, ctx: dict) -> Optional[UsageRecord]:
    """解析 openclaw v3 session 格式（*.jsonl，无 'trajectory'）。

    ctx 跨行持久化 session_id / cwd，由调用方在每个增量批次间传入。
    """
    if not isinstance(obj, dict):
        return None

    obj_type = obj.get("type")

    if obj_type == "session":
        ctx["session_id"] = obj.get("id") or ""
        ctx["cwd"] = obj.get("cwd") or "openclaw"
        return None

    if obj_type != "message":
        return None

    msg = obj.get("message")
    if not isinstance(msg, dict) or msg.get("role") != "assistant":
        return None

    usage = msg.get("usage")
    if not isinstance(usage, dict):
        return None

    total_tokens = int(usage.get("totalTokens") or 0)
    if total_tokens == 0:
        return None

    # message.timestamp 是 epoch ms；外层 timestamp 是 ISO 字符串
    ts_ms = msg.get("timestamp")
    if ts_ms:
        ts = int(ts_ms) // 1000
    else:
        ts = parse_iso_utc(obj.get("timestamp") or "")
    if ts == 0:
        return None

    msg_id = obj.get("id") or str(pos)
    dedup_key = f"openclaw-v3:{msg_id}"

    model = (msg.get("model") or "unknown").strip()
    session_id = ctx.get("session_id") or ""
    project = ctx.get("cwd") or "openclaw"

    return UsageRecord(
        ts=ts,
        source=SOURCE_OPENCLAW,
        model=model,
        project=project,
        input_tokens=int(usage.get("input") or 0),
        output_tokens=int(usage.get("output") or 0),
        cache_read_tokens=int(usage.get("cacheRead") or 0),
        cache_creation_tokens=int(usage.get("cacheWrite") or 0),
        reasoning_tokens=0,
        total_tokens=total_tokens,
        request_prompt_tokens=int(usage.get("input") or 0) + int(usage.get("cacheRead") or 0),
        session_id=session_id,
        source_file=source_file,
        pos=pos,
        category=CATEGORY_MAIN,
        dedup_key=dedup_key,
    )


def _open_ro(db_path: Path) -> Optional[sqlite3.Connection]:
    if not db_path.is_file():
        return None
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5.0)
        conn.row_factory = sqlite3.Row
        return conn
    except sqlite3.OperationalError:
        return None


def fetch_records(db_path: Path) -> List[UsageRecord]:
    """从 openclaw-agent.sqlite 读 v3 transcript，返回可 SUM 的增量行。"""
    conn = _open_ro(db_path)
    if conn is None:
        return []
    try:
        windows: dict[str, str] = {}
        try:
            for row in conn.execute(
                "SELECT session_id, session_key FROM session_windows"
            ):
                sid = (row["session_id"] or "").strip()
                if sid:
                    windows[sid] = _project_from_session_key(row["session_key"] or "")
        except sqlite3.OperationalError as exc:
            # 缺表：没有 session_key，仍可读 transcript。锁/损坏：本轮跳过，避免
            # 用空 windows 把 project 写成 cwd 后 INSERT OR IGNORE 再也改不回来。
            if "no such table" not in str(exc).lower():
                return []
        try:
            rows = conn.execute(
                """
                SELECT session_id, seq, event_json
                FROM transcript_events
                ORDER BY session_id, seq
                """
            ).fetchall()
        except sqlite3.OperationalError:
            return []
    finally:
        conn.close()

    records: List[UsageRecord] = []
    ctx_by_session: dict[str, dict] = {}
    source_file = str(db_path)
    for row in rows:
        sid = (row["session_id"] or "").strip()
        try:
            seq = int(row["seq"] or 0)
        except (TypeError, ValueError):
            seq = 0
        try:
            obj = json.loads(row["event_json"] or "")
        except (json.JSONDecodeError, TypeError, UnicodeDecodeError):
            continue
        if not isinstance(obj, dict):
            continue
        ctx = ctx_by_session.setdefault(
            sid,
            {"session_id": sid, "cwd": windows.get(sid) or "openclaw"},
        )
        try:
            rec = parse_v3_record(obj, source_file, seq, ctx)
        except (TypeError, ValueError):
            continue
        if obj.get("type") == "session" and sid in windows:
            ctx["cwd"] = windows[sid]
        if rec is not None:
            records.append(rec)
    return records
