"""openclaw trajectory 日志解析器（~/.openclaw/agents/main/sessions/*.trajectory.jsonl）。

只处理 type=="model.completed" 的行，提取：
  data.usage.{input, output, cacheRead}
  data.promptCache.lastCallUsage.cacheWrite（可选）
  顶层: modelId, provider, ts, sessionId, sessionKey, seq

project 从 sessionKey 提取：格式 "agent:main:{type}:..." → 取第三段。
dedup_key = "openclaw:{sessionId}:{seq}"
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
        session_id=session_id,
        source_file=source_file,
        pos=pos,
        category=CATEGORY_MAIN,
        dedup_key=dedup_key,
    )
