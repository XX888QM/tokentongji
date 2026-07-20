"""openclaw 日志解析器，支持两种格式：

trajectory（*.trajectory.jsonl）：
  type=="model.completed"，提取 data.usage / promptCache / sessionKey
  dedup_key = "openclaw:{runId}:{seq}"

v3 session（*.jsonl，无 "trajectory"）：
  第一行 type=="session" 作上下文（session_id、cwd）
  type=="message" + role=="assistant" 提取 message.usage
  dedup_key = "openclaw-v3:{msg_id}"
"""

from __future__ import annotations

from typing import Optional

from ..models import CATEGORY_MAIN, SOURCE_OPENCLAW, UsageRecord, parse_iso_utc

_EVENT_TYPE = "model.completed"


def _project_from_session_key(session_key: str) -> str:
    """agent:main:openclaw-weixin:direct:... → 'openclaw-weixin'"""
    if not session_key:
        return "openclaw"
    parts = session_key.split(":", 3)
    if len(parts) >= 3:
        return parts[2] or "openclaw"
    return "openclaw"


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
