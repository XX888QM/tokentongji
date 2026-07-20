"""Grok CLI（Grok Build TUI）用量解析器。

数据源（recon 实测，2026-07-09）：
- 主源：`~/.grok/logs/unified.jsonl`（全局统一日志，非 per-session）。
- 真正带 token 的事件：`msg == "shell.turn.inference_done"`，ctx 含
  prompt_tokens / cached_prompt_tokens / completion_tokens / reasoning_tokens。
- Grok CLI 的 model/cwd 靠同 sid 的 `model changed` / `session created` carry-forward；
  claude-mem API 转录事件可在 inference_done ctx 内直接提供，并优先使用。
- 每条 inference_done 是**本轮 loop 的增量**（非累积总量），直接入库求和即可。
- reasoning_tokens 是 completion 子集（实测 reason ≤ completion），与 Codex 同口径：
  output=completion，reasoning 仅展示不另计费。
- input = prompt - cached（全价输入）；cache_read = cached。
- dedup_key = `grok:{sid}:{ts}:{loop_index}`（实测无碰撞）。
- 跨增量批次：sid→model / sid→cwd 字典持久化进 ingest_state.ctx。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from ..models import CATEGORY_MAIN, SOURCE_GROK, UsageRecord, parse_iso_utc

DEFAULT_MODEL = "grok-4.5"


@dataclass
class GrokState:
    """跨行 / 跨批次 carry-forward：按 session id 记住最近 model 与 cwd。"""

    models: dict[str, str] = field(default_factory=dict)
    cwds: dict[str, str] = field(default_factory=dict)
    default_model: str = DEFAULT_MODEL

    def to_ctx(self) -> dict:
        return {
            "models": dict(self.models),
            "cwds": dict(self.cwds),
            "default_model": self.default_model,
        }

    @classmethod
    def from_ctx(cls, ctx: Optional[dict], default_model: str = DEFAULT_MODEL) -> "GrokState":
        ctx = ctx or {}
        models = ctx.get("models") or {}
        cwds = ctx.get("cwds") or {}
        # 只保留 str→str，防损坏 ctx
        clean_models = {
            str(k): str(v) for k, v in models.items() if k and v
        }
        clean_cwds = {
            str(k): str(v) for k, v in cwds.items() if k and v
        }
        dm = str(ctx.get("default_model") or default_model or DEFAULT_MODEL)
        return cls(models=clean_models, cwds=clean_cwds, default_model=dm)


def _int(v: Any) -> int:
    try:
        n = int(v or 0)
    except (TypeError, ValueError):
        return 0
    return n if n >= 0 else 0


def process_record(
    obj: dict,
    source_file: str,
    pos: int,
    state: GrokState,
) -> Optional[UsageRecord]:
    """处理 unified.jsonl 一行；非用量事件更新 state 后返回 None。"""
    if not isinstance(obj, dict):
        return None

    msg = obj.get("msg") or ""
    sid = obj.get("sid") or ""
    ctx = obj.get("ctx") if isinstance(obj.get("ctx"), dict) else {}

    if msg == "model changed" and sid:
        model = ctx.get("model") or ctx.get("model_id")
        if model:
            state.models[sid] = str(model)
        return None

    if msg == "session created" and sid:
        cwd = ctx.get("cwd")
        if cwd:
            state.cwds[sid] = str(cwd)
        return None

    if msg != "shell.turn.inference_done":
        return None

    prompt = _int(ctx.get("prompt_tokens"))
    cached = _int(ctx.get("cached_prompt_tokens"))
    completion = _int(ctx.get("completion_tokens"))
    reasoning = _int(ctx.get("reasoning_tokens"))

    # 脏数据防护：cache 不应超过 prompt；reasoning 是 completion 子集
    if cached > prompt:
        cached = prompt
    if reasoning > completion:
        reasoning = completion

    input_tokens = prompt - cached
    if input_tokens == 0 and cached == 0 and completion == 0:
        return None

    ts_raw = obj.get("ts") or ""
    ts = parse_iso_utc(ts_raw)
    if ts == 0:
        return None

    loop_index = ctx.get("loop_index")
    if loop_index is None:
        loop_index = 0
    dedup_key = f"grok:{sid}:{ts_raw}:{loop_index}"

    model = str(ctx.get("model") or state.models.get(sid) or state.default_model)
    project = str(ctx.get("cwd") or state.cwds.get(sid) or "grok")
    total = input_tokens + cached + completion

    return UsageRecord(
        ts=ts,
        source=SOURCE_GROK,
        model=model,
        project=project,
        input_tokens=input_tokens,
        output_tokens=completion,
        cache_read_tokens=cached,
        cache_creation_tokens=0,
        reasoning_tokens=reasoning,
        total_tokens=total,
        request_prompt_tokens=prompt,
        session_id=sid,
        source_file=source_file,
        pos=pos,
        category=CATEGORY_MAIN,
        dedup_key=dedup_key,
    )
