"""Claude Code 会话日志解析器（~/.claude/projects/**/*.jsonl）。

契约（recon 实测）：
- 只认 type=="assistant" 且 message.usage 是 dict 的记录。
- 普通消息去重键 = message.id；同一 message 的流式多行由 DB 采用 total 更大的完整快照。
- fallback/retry 的 usage.iterations 按真实模型拆成多条，不能只统计顶层最终模型。
- 跳过 model=="<synthetic>"（本地合成、usage 全 0、无 requestId）。
- project = 顶层 cwd 绝对路径（不是 message 内、不是目录名）。
- category：observer(cwd 含 .claude-mem/observer-sessions) / subagent(isSidechain 或
  路径含 /subagents/workflows/) / main。
- token 字段：input_tokens / output_tokens / cache_creation_input_tokens /
  cache_read_input_tokens；total = 四者之和（reasoning=0）。
"""

from __future__ import annotations

from dataclasses import replace
from typing import Optional

from ..models import (
    CATEGORY_MAIN,
    CATEGORY_OBSERVER,
    CATEGORY_SUBAGENT,
    SOURCE_CLAUDE,
    UsageRecord,
    parse_iso_utc,
)

SYNTHETIC_MODEL = "<synthetic>"
_OBSERVER_MARK = "/.claude-mem/observer-sessions"
_SUBAGENT_MARK = "/subagents/workflows/"


def _category(cwd: str, is_sidechain: bool, source_file: str) -> str:
    if cwd and _OBSERVER_MARK in cwd:
        return CATEGORY_OBSERVER
    if is_sidechain or (_SUBAGENT_MARK in (source_file or "")):
        return CATEGORY_SUBAGENT
    return CATEGORY_MAIN


def parse_record(obj: dict, source_file: str, pos: int) -> Optional[UsageRecord]:
    """把一条 jsonl 解析成 UsageRecord；不符合条件返回 None。"""
    if not isinstance(obj, dict):
        return None
    if obj.get("type") != "assistant":
        return None
    msg = obj.get("message")
    if not isinstance(msg, dict):
        return None
    usage = msg.get("usage")
    if not isinstance(usage, dict):
        return None

    model = msg.get("model") or ""
    if model == SYNTHETIC_MODEL:
        return None

    msg_id = msg.get("id") or ""
    if not msg_id:
        # 无 id 无法可靠去重，跳过（实测仅 synthetic 缺 id）
        return None

    input_tokens = int(usage.get("input_tokens", 0) or 0)
    output_tokens = int(usage.get("output_tokens", 0) or 0)
    cache_creation = int(usage.get("cache_creation_input_tokens", 0) or 0)
    cache_read = int(usage.get("cache_read_input_tokens", 0) or 0)
    total = input_tokens + output_tokens + cache_creation + cache_read

    cwd = obj.get("cwd") or "unknown"
    is_sidechain = bool(obj.get("isSidechain", False))
    category = _category(cwd, is_sidechain, source_file)
    ts = parse_iso_utc(obj.get("timestamp", ""))
    if ts == 0:
        return None
    session_id = obj.get("sessionId") or ""

    return UsageRecord(
        ts=ts,
        source=SOURCE_CLAUDE,
        model=model,
        project=cwd,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_read_tokens=cache_read,
        cache_creation_tokens=cache_creation,
        reasoning_tokens=0,
        total_tokens=total,
        session_id=session_id,
        source_file=source_file,
        pos=pos,
        category=category,
        dedup_key=msg_id,
    )


def parse_records(obj: dict, source_file: str, pos: int) -> list[UsageRecord]:
    """解析单行；fallback/retry 的每次 iteration 各保留一条真实模型用量。"""
    base = parse_record(obj, source_file, pos)
    if base is None:
        return []
    usage = obj["message"]["usage"]
    iterations = usage.get("iterations")
    if not isinstance(iterations, list) or len(iterations) <= 1:
        return [base]

    records = []
    for index, item in enumerate(iterations):
        if not isinstance(item, dict):
            continue
        input_tokens = int(item.get("input_tokens", 0) or 0)
        output_tokens = int(item.get("output_tokens", 0) or 0)
        cache_creation = int(item.get("cache_creation_input_tokens", 0) or 0)
        cache_read = int(item.get("cache_read_input_tokens", 0) or 0)
        records.append(replace(
            base,
            model=item.get("model") or base.model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_creation_tokens=cache_creation,
            cache_read_tokens=cache_read,
            total_tokens=input_tokens + output_tokens + cache_creation + cache_read,
            dedup_key=f"{base.dedup_key}:iteration:{index}",
        ))
    return records or [base]
