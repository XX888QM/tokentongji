"""Codex CLI 会话日志解析器（~/.codex/sessions + archived_sessions）。

契约（recon 实测）：
- envelope: session_meta / turn_context / event_msg(内含 payload.type==token_count) / response_item。
- carry-forward：token_count 不带 model/cwd，只能靠物理行顺序携带最近的
  turn_context.model / turn_context.cwd（cwd 优先 turn_context，session_meta.cwd 可能被改写）。
- token 取数：对累积 total_token_usage 做相邻**差分**得每轮增量，按事件时间戳分桶。
  绝不对 total 求和、绝不累加 last（last 含完整 context、事件成对重发 → 2x 高估）。
- 跳过 info is None（rate-limit 心跳）。
- model 缺失回退 config.toml 默认（默认 gpt-5.5），再缺标 unknown，不丢弃。
- input_tokens 含 cached → 归一化时拆出 fresh_input = input - cached。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from ..models import CATEGORY_MAIN, SOURCE_CODEX, UsageRecord, parse_iso_utc

_FIELDS = (
    "input_tokens",
    "cached_input_tokens",
    "output_tokens",
    "reasoning_output_tokens",
    "total_tokens",
)
_DEFAULT_MODEL_FALLBACK = "gpt-5.5"
_CONFIG_PATH = Path.home() / ".codex" / "config.toml"


def read_default_model(config_path: Path = _CONFIG_PATH) -> str:
    """从 ~/.codex/config.toml 读顶层 model；读不到回退 gpt-5.5。"""
    try:
        with open(config_path, "r", encoding="utf-8") as fh:
            for line in fh:
                s = line.strip()
                if s.startswith("["):  # 进入 section，顶层 model 应在之前
                    break
                m = re.match(r'^model\s*=\s*"([^"]+)"', s)
                if m:
                    return m.group(1)
    except OSError:
        pass
    return _DEFAULT_MODEL_FALLBACK


def _zero_total() -> dict:
    return {k: 0 for k in _FIELDS}


@dataclass
class CodexState:
    """单文件解析的流式游标（可变累加器，非领域数据）。"""

    cur_model: Optional[str] = None
    cur_cwd: Optional[str] = None
    session_id: str = ""
    prev_total: dict = field(default_factory=_zero_total)
    default_model: str = _DEFAULT_MODEL_FALLBACK

    def to_ctx(self) -> dict:
        """导出可持久化上下文（不含 default_model，那是运行期注入）。"""
        return {
            "cur_model": self.cur_model,
            "cur_cwd": self.cur_cwd,
            "session_id": self.session_id,
            "prev_total": dict(self.prev_total),
        }

    @classmethod
    def from_ctx(cls, ctx: Optional[dict], default_model: str) -> "CodexState":
        ctx = ctx or {}
        prev = ctx.get("prev_total") or {}
        prev_total = {k: int(prev.get(k, 0) or 0) for k in _FIELDS}
        return cls(
            cur_model=ctx.get("cur_model"),
            cur_cwd=ctx.get("cur_cwd"),
            session_id=ctx.get("session_id", "") or "",
            prev_total=prev_total,
            default_model=default_model or _DEFAULT_MODEL_FALLBACK,
        )


def process_record(
    obj: dict, source_file: str, pos: int, state: CodexState
) -> Optional[UsageRecord]:
    """处理一条 jsonl，更新 state；若产出增量则返回 UsageRecord，否则 None。"""
    if not isinstance(obj, dict):
        return None
    env_type = obj.get("type")
    payload = obj.get("payload")
    if not isinstance(payload, dict):
        payload = {}

    if env_type == "session_meta":
        cwd = payload.get("cwd")
        if cwd and state.cur_cwd is None:
            # session_meta.cwd 仅作初值；后续 turn_context.cwd 更权威会覆盖
            state.cur_cwd = cwd
        sid = payload.get("id")
        if sid:
            state.session_id = sid
        return None

    if env_type == "turn_context":
        m = payload.get("model")
        if not m:
            cm = payload.get("collaboration_mode") or {}
            m = (cm.get("settings") or {}).get("model")
        if m:
            state.cur_model = m
        cwd = payload.get("cwd")
        if cwd:
            state.cur_cwd = cwd
        return None

    if payload.get("type") != "token_count":
        return None

    info = payload.get("info")
    if not info or not isinstance(info, dict):
        return None  # rate-limit 心跳，跳过
    tot = info.get("total_token_usage")
    if not isinstance(tot, dict):
        return None

    cur = {k: int(tot.get(k, 0) or 0) for k in _FIELDS}
    prev = state.prev_total
    state.prev_total = cur

    # 以单调的 total_tokens 为锚算总增量（input 子字段在 compaction 时会回落，
    # 不能逐字段相减，否则少算输入增量 ~0.16%）。total = input + output。
    d_total = max(0, cur["total_tokens"] - prev["total_tokens"])
    if d_total == 0:
        return None  # 重复快照（成对重发）或无新增

    d_output = max(0, cur["output_tokens"] - prev["output_tokens"])
    d_reasoning = max(0, cur["reasoning_output_tokens"] - prev["reasoning_output_tokens"])
    # 输入总增量 = 总增量 - 输出增量（锚定单调 total，跨 compaction 也准）
    d_input_total = max(0, d_total - d_output)
    d_cached = max(0, cur["cached_input_tokens"] - prev["cached_input_tokens"])
    d_cached = min(d_cached, d_input_total)  # cached ⊆ 输入增量
    fresh_input = max(0, d_input_total - d_cached)
    total_norm = fresh_input + d_cached + d_output  # == d_total

    model = state.cur_model or state.default_model or "unknown"
    cwd = state.cur_cwd or "unknown"
    ts = parse_iso_utc(obj.get("timestamp", ""))

    return UsageRecord(
        ts=ts,
        source=SOURCE_CODEX,
        model=model,
        project=cwd,
        input_tokens=fresh_input,
        output_tokens=d_output,
        cache_read_tokens=d_cached,
        cache_creation_tokens=0,
        reasoning_tokens=d_reasoning,
        total_tokens=total_norm,
        session_id=state.session_id,
        source_file=source_file,
        pos=pos,
        category=CATEGORY_MAIN,
        dedup_key=f"{source_file}#{pos}",
    )
