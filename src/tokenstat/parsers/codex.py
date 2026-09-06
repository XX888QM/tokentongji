"""Codex CLI 会话日志解析器（~/.codex/sessions + archived_sessions）。

契约（recon 实测）：
- envelope: session_meta / turn_context / event_msg(内含 payload.type==token_count) / response_item。
- carry-forward：token_count 不带 model/cwd，只能靠物理行顺序携带最近的
  turn_context.model / turn_context.cwd（cwd 优先 turn_context，session_meta.cwd 可能被改写）。
- token 取数：对累积 total_token_usage 做相邻**差分**得每轮增量，按事件时间戳分桶。
  绝不对 total 求和、绝不累加 last（last 含完整 context、事件成对重发 → 2x 高估）。
- fork/subagent（Codex Desktop）：session_meta 带 forked_from_id 的文件，首条
  token_count **继承父会话的累积量**（实测上亿），只能作差分基线不能计增量。
  之后凡 total 仍落在父会话轨迹上的 token_count 整段作基线推进、不产出行；
  只跳首条会把父会话后续 replay 再计一遍（8/12 曾约 2x）。找不到父文件时
  仍只跳首条。与父轨迹零交集的独立 subagent 从 0 正常计数。
- 同一文件内会交错出现父/子线程的 session_meta，但 total_token_usage 是文件内
  **连续**计数器 → sid 变化绝不能重置差分基线（重置 = 整段累积量重计一遍）。
- 跳过 info is None（rate-limit 心跳）。
- model 缺失回退 config.toml 默认（默认 gpt-5.5），再缺标 unknown，不丢弃。
- input_tokens 含 cached → 归一化时拆出 fresh_input = input - cached。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from .. import config
from ..models import (
    CATEGORY_MAIN, CATEGORY_OBSERVER, SOURCE_CODEX, SQLITE_INT_MAX, UsageRecord, parse_iso_utc,
)

_FIELDS = (
    "input_tokens",
    "cached_input_tokens",
    "output_tokens",
    "reasoning_output_tokens",
    "total_tokens",
)
_DEFAULT_MODEL_FALLBACK = "gpt-5.5"
_CONFIG_PATH = Path.home() / ".codex" / "config.toml"
_CLAUDE_MEM_USAGE_TYPE = "claude_mem.codex_usage"


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


def load_parent_totals(forked_from_id: str) -> list:
    """按 forked_from_id 找父 session，抽出 token_count 的累积 total 序列。"""
    if not isinstance(forked_from_id, str) or not forked_from_id:
        return []
    needle = forked_from_id.strip()
    if not needle:
        return []
    for root in config.CODEX_SESSION_DIRS:
        if not root.is_dir():
            continue
        matches = list(root.rglob(f"*{needle}*.jsonl"))
        if not matches:
            continue
        path = matches[0]
        totals = []
        try:
            with open(path, "r", encoding="utf-8") as fh:
                for line in fh:
                    try:
                        obj = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    payload = obj.get("payload") if isinstance(obj, dict) else None
                    if not isinstance(payload, dict) or payload.get("type") != "token_count":
                        continue
                    info = payload.get("info")
                    if not isinstance(info, dict):
                        continue
                    tot = info.get("total_token_usage")
                    if not isinstance(tot, dict):
                        continue
                    try:
                        totals.append(int(tot.get("total_tokens") or 0))
                    except (TypeError, ValueError):
                        continue
        except OSError:
            return []
        return totals
    return []


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
    # fork 文件：下一条 token_count 是继承来的父会话累积量，只作基线不计增量
    pending_baseline: bool = False
    parent_totals: list = field(default_factory=list)
    replay_index: int = -1
    skipping_replay: bool = False

    def to_ctx(self) -> dict:
        """导出可持久化上下文（不含 default_model，那是运行期注入）。"""
        return {
            "cur_model": self.cur_model,
            "cur_cwd": self.cur_cwd,
            "session_id": self.session_id,
            "prev_total": dict(self.prev_total),
            "pending_baseline": self.pending_baseline,
            "parent_totals": list(self.parent_totals),
            "replay_index": self.replay_index,
            "skipping_replay": self.skipping_replay,
        }

    @classmethod
    def from_ctx(cls, ctx: Optional[dict], default_model: str) -> "CodexState":
        if not isinstance(ctx, dict):
            ctx = {}
        prev = ctx.get("prev_total") if isinstance(ctx.get("prev_total"), dict) else {}
        prev_total = {k: int(prev.get(k, 0) or 0) for k in _FIELDS}
        raw_totals = ctx.get("parent_totals") if isinstance(ctx.get("parent_totals"), list) else []
        parent_totals = []
        for value in raw_totals:
            try:
                parent_totals.append(int(value))
            except (TypeError, ValueError):
                continue
        return cls(
            cur_model=ctx.get("cur_model"),
            cur_cwd=ctx.get("cur_cwd"),
            session_id=ctx.get("session_id", "") or "",
            prev_total=prev_total,
            default_model=default_model or _DEFAULT_MODEL_FALLBACK,
            pending_baseline=bool(ctx.get("pending_baseline")),
            parent_totals=parent_totals,
            replay_index=int(ctx.get("replay_index") or -1),
            skipping_replay=bool(ctx.get("skipping_replay")),
        )


def process_record(
    obj: dict, source_file: str, pos: int, state: CodexState
) -> Optional[UsageRecord]:
    """处理一条 jsonl，更新 state；若产出增量则返回 UsageRecord，否则 None。"""
    if not isinstance(obj, dict):
        return None
    if obj.get("type") == _CLAUDE_MEM_USAGE_TYPE:
        return _process_claude_mem_usage(obj, source_file, pos)
    env_type = obj.get("type")
    payload = obj.get("payload")
    if not isinstance(payload, dict):
        payload = {}

    if env_type == "session_meta":
        sid = payload.get("id")
        if isinstance(sid, str) and sid and sid != state.session_id:
            # 只更新归属 sid，**不重置差分基线**：Codex Desktop 会把父/子线程的
            # session_meta 交错写进同一文件，而 total_token_usage 是文件内连续
            # 计数器，重置基线会把整段累积量再计一遍（实测 ~29% 虚高）。
            state.session_id = sid
        if payload.get("forked_from_id") or payload.get("parent_thread_id"):
            # fork 出的 subagent 文件：首条 token_count 继承父会话累积量，
            # 只能作基线。仅在尚未见过任何 token_count 时生效（文件中段的
            # fork meta 不影响已建立的连续基线）。
            if all(v == 0 for v in state.prev_total.values()):
                state.pending_baseline = True
                parent_id = payload.get("forked_from_id") or payload.get("parent_thread_id")
                if not state.parent_totals and isinstance(parent_id, str):
                    state.parent_totals = load_parent_totals(parent_id)
        cwd = payload.get("cwd")
        if isinstance(cwd, str) and cwd and state.cur_cwd is None:
            # session_meta.cwd 仅作初值；后续 turn_context.cwd 更权威会覆盖
            state.cur_cwd = cwd
        return None

    if env_type == "turn_context":
        m = payload.get("model")
        if not m:
            cm = payload.get("collaboration_mode") or {}
            m = (cm.get("settings") or {}).get("model")
        if isinstance(m, str) and m:
            state.cur_model = m
        cwd = payload.get("cwd")
        if isinstance(cwd, str) and cwd:
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

    # total_token_usage 是累计计数，仍用于稳定差分；last_token_usage 才是本次
    # 请求的完整 prompt。字段不全时宁可不标记，不能把累计差分误当 prompt。
    request_prompt_tokens = None
    last = info.get("last_token_usage")
    if isinstance(last, dict) and {
        "input_tokens", "cached_input_tokens"
    }.issubset(last):
        try:
            last_input = int(last.get("input_tokens") or 0)
            last_cached = int(last.get("cached_input_tokens") or 0)
        except (TypeError, ValueError):
            pass
        else:
            if 0 <= last_cached <= last_input <= SQLITE_INT_MAX:
                request_prompt_tokens = last_input

    cur = {k: int(tot.get(k, 0) or 0) for k in _FIELDS}
    if any(value < 0 or value > SQLITE_INT_MAX for value in cur.values()):
        return None
    ts = parse_iso_utc(obj.get("timestamp", ""))
    if state.pending_baseline:
        state.pending_baseline = False
        if all(v == 0 for v in state.prev_total.values()):
            if state.parent_totals:
                try:
                    state.replay_index = state.parent_totals.index(cur["total_tokens"])
                except ValueError:
                    state.replay_index = -1
                if state.replay_index >= 0:
                    state.prev_total = cur
                    state.skipping_replay = True
                    return None
                # 与父轨迹零交集：独立 subagent，从 0 正常计数
            else:
                # 找不到父文件：沿用只跳首条，避免把继承的上亿当增量
                state.prev_total = cur
                return None
    if state.skipping_replay and state.parent_totals:
        nxt = state.replay_index + 1
        if nxt < len(state.parent_totals) and state.parent_totals[nxt] == cur["total_tokens"]:
            state.replay_index = nxt
            state.prev_total = cur
            return None
        state.skipping_replay = False
    if ts == 0:
        return None
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
        request_prompt_tokens=request_prompt_tokens,
        session_id=state.session_id,
        source_file=source_file,
        pos=pos,
        category=CATEGORY_MAIN,
        # 用文件名(自带全局唯一 UUID+时间戳)而非完整路径做去重键：
        # Codex 把 session 从 sessions/ 挪进 archived_sessions/ 后，同一份内容
        # 在两个路径各解析一遍，若键含完整路径就挡不住 → 系统性重复计数。
        dedup_key=f"{Path(source_file).name}#{pos}",
    )


def _process_claude_mem_usage(
    obj: dict, source_file: str, pos: int
) -> Optional[UsageRecord]:
    """解析 claude-mem 对 `codex exec --json` 的单次真实 usage 记录。

    `input_tokens` 已包含 `cached_input_tokens`，因此二者拆成全价输入与
    cache_read。`cache_write_input_tokens` 原样留在来源 JSONL 中，但 Codex CLI
    尚未定义其是否已包含在 input_tokens 内；这里不把它再计一次，避免虚高。
    """
    if obj.get("schema_version") != 1:
        return None
    event_id = obj.get("event_id")
    if not isinstance(event_id, str) or not event_id or len(event_id) > 512:
        return None
    usage = obj.get("usage")
    if not isinstance(usage, dict):
        return None

    input_total = _nonnegative_int(usage.get("input_tokens"))
    cached = _nonnegative_int(usage.get("cached_input_tokens"))
    cache_write = _nonnegative_int(usage.get("cache_write_input_tokens"))
    output = _nonnegative_int(usage.get("output_tokens"))
    reasoning = _nonnegative_int(usage.get("reasoning_output_tokens"))
    if None in (input_total, cached, cache_write, output, reasoning):
        return None
    if cached > input_total or reasoning > output:
        return None

    ts = parse_iso_utc(obj.get("timestamp", ""))
    if ts == 0:
        return None
    model = obj.get("model")
    project = obj.get("project")
    session_id = obj.get("session_id")
    model = model.strip() if isinstance(model, str) and model.strip() else "unknown"
    project = project.strip() if isinstance(project, str) and project.strip() else "unknown"
    session_id = session_id.strip() if isinstance(session_id, str) else ""

    return UsageRecord(
        ts=ts,
        source=SOURCE_CODEX,
        model=model,
        project=project,
        input_tokens=input_total - cached,
        output_tokens=output,
        cache_read_tokens=cached,
        # Do not double count cache_write_input_tokens until Codex documents
        # whether it is already included in input_tokens. The raw spool keeps it.
        cache_creation_tokens=0,
        reasoning_tokens=reasoning,
        total_tokens=input_total + output,
        request_prompt_tokens=input_total,
        session_id=session_id,
        source_file=source_file,
        pos=pos,
        category=CATEGORY_OBSERVER,
        dedup_key=f"claude-mem-codex:{event_id}",
    )


def _nonnegative_int(value) -> Optional[int]:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value
