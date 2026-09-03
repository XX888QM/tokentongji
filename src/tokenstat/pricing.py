"""费用估算：按公开单价把 token 折算成美元。

订阅制（Claude Max / Codex 套餐）下 token 不直接对应扣费，
此处费用**仅供参考**。单价表见 pricing.json（美元 / 每百万 token）。

model 归一化策略（recon 实测约束）：
- 先 lower、剥离区域前缀(us.anthropic./anthropic./openai.)与后缀([1m]/-1m)
- 精确匹配 → 最长前缀匹配 → 家族规则(opus/sonnet/haiku/gpt-5) → default
- 未知 model **fail-loud**（记录到 _UNKNOWN_MODELS）而非静默按 0
"""

from __future__ import annotations

import json
from datetime import date
from functools import lru_cache
from pathlib import Path
from typing import Optional

from . import config

_MILLION = 1_000_000

# 命中过的未知 model，便于排查（不静默漏算）
_UNKNOWN_MODELS: set[str] = set()

_FALLBACK_PRICING = {
    "_meta": {"note": "pricing.json 缺失或损坏，费用按 default 计"},
    "default": {"input": 0.0, "output": 0.0, "cache_read": 0.0, "cache_write_5m": 0.0, "cache_write_1h": 0.0},
    "anthropic": {},
    "openai": {},
    "deepseek": {},
}


@lru_cache(maxsize=2)
def load_pricing(path: Optional[str] = None) -> dict:
    """读取 pricing.json（带缓存）。缺失/损坏回退到安全默认。"""
    p = Path(path) if path else config.PRICING_PATH
    try:
        with open(p, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if "default" not in data:
            raise ValueError("pricing.json 缺少 default 字段")
        return data
    except (FileNotFoundError, json.JSONDecodeError, ValueError):
        return _FALLBACK_PRICING


def _clean_model(model: str) -> str:
    m = (model or "").strip().lower()
    for prefix in ("us.anthropic.", "anthropic.", "openai.", "us.", "eu."):
        if m.startswith(prefix):
            m = m[len(prefix):]
    for suffix in ("[1m]", "-1m", ":1m"):
        if m.endswith(suffix):
            m = m[: -len(suffix)]
    return m


def _merged_models(pricing: dict) -> dict:
    out = {}
    for section in ("anthropic", "openai", "deepseek", "xai", "local"):
        out.update(pricing.get(section, {}))
    return out


def _raw_for_model(model: str, pricing: dict) -> Optional[dict]:
    """按既有归一化规则找到原始价目；未知模型返回 None。"""
    models = _merged_models(pricing)
    clean = _clean_model(model)
    if clean in models:
        return models[clean]

    best = ""
    for cand in models:
        if clean.startswith(cand) and len(cand) > len(best):
            best = cand
    if best:
        return models[best]
    return _family_rates(clean, models, pricing.get("default", {}))


def _family_rates(clean: str, models: dict, default: dict) -> Optional[dict]:
    """已知家族的兜底：版本号没精确命中时按家族归档。"""
    def pick(*keys):
        for k in keys:
            if k in models:
                return models[k]
        return None

    if clean.startswith("claude-fable") or clean.startswith("claude-mythos"):
        return pick("claude-fable-5-1", "claude-mythos-5-1", "claude-fable-5", "claude-mythos-5")
    if clean.startswith("claude-opus-4-1"):
        return pick("claude-opus-4-1")
    if clean.startswith(("claude-opus-4-0", "claude-opus-4-20")):
        return pick("claude-opus-4-0")
    if clean.startswith("claude-sonnet-4-20"):
        return pick("claude-sonnet-4-0")
    if clean.startswith("claude-opus"):
        return pick("claude-opus-5", "claude-opus-4-8", "claude-opus-4-7")
    if clean.startswith("claude-sonnet"):
        return pick("claude-sonnet-5", "claude-sonnet-4-6", "claude-sonnet-4-5")
    if clean.startswith("claude-haiku"):
        return pick("claude-haiku-4-5")
    if clean.startswith("gpt-5-codex") or clean == "codex-auto-review":
        return pick("gpt-5.3-codex", "gpt-5-codex")
    if clean.startswith("gpt-5.6-sol") or clean.startswith("gpt-daybreak-blue"):
        return pick("gpt-daybreak-blue", "gpt-5.6-sol", "gpt-5.5")
    if clean.startswith("gpt-5.6-terra"):
        return pick("gpt-5.6-terra", "gpt-5.4")
    if clean.startswith("gpt-5.6-luna"):
        return pick("gpt-5.6-luna")
    if clean == "gpt-5":
        return pick("gpt-5")
    if clean.startswith("gpt-5"):
        # gpt-5.x 未精确命中时：优先新 flagship，再退基础款
        return pick("gpt-5.6-sol", "gpt-5.5", "gpt-5.4", "gpt-5")
    if clean.startswith("grok"):
        return pick("grok-4.6", "grok-4.5", "grok-4.3", "grok-build-0.1")
    return None


def _effective_raw(raw: dict, priced_at: Optional[date]) -> dict:
    """应用按日期生效的后续价，未指定日期时按当天价格展示。"""
    next_pricing = raw.get("next_pricing") or {}
    starts_on = next_pricing.get("starts_on")
    when = priced_at or date.today()
    if starts_on and when.isoformat() >= starts_on:
        return {**raw, **next_pricing}
    return raw


def long_context_threshold_for_model(
    model: str, pricing: dict, priced_at: Optional[date] = None
) -> Optional[int]:
    """返回该模型按单次请求升到长上下文价的阈值。"""
    raw = _raw_for_model(model, pricing)
    if raw is None:
        return None
    rule = _effective_raw(raw, priced_at).get("long_context") or {}
    threshold = rule.get("threshold")
    try:
        return int(threshold) if threshold is not None else None
    except (TypeError, ValueError):
        return None


def long_context_thresholds(pricing: dict) -> tuple[int, ...]:
    """返回价表内所有长上下文阈值（含 next_pricing 可能引入的历史/未来阈值），
    供聚合层按请求分桶生成 SQL 列。

    不能只取"今天"生效的阈值：如果某模型的 next_pricing.long_context.threshold
    与基础 long_context.threshold 不同，只收今天的那个会导致另一个阈值在 SQL 里
    没有对应列，聚合层按历史计价日查到的阈值就会落空、静默按基础价算（不区分
    高估/低估）。这里直接扫每个模型的 raw 和 next_pricing 两份 long_context，
    把两边的阈值都收进来，不依赖 priced_at。
    """
    thresholds = set()
    for raw in _merged_models(pricing).values():
        for section in (raw, raw.get("next_pricing") or {}):
            threshold = (section.get("long_context") or {}).get("threshold")
            if threshold is None:
                continue
            try:
                thresholds.add(int(threshold))
            except (TypeError, ValueError):
                continue
    return tuple(sorted(thresholds))


def rates_for_model(
    model: str,
    pricing: dict,
    cache_window: str = "5m",
    long_context: bool = False,
    priced_at: Optional[date] = None,
) -> dict:
    """返回归一化单价 {input, output, cache_read, cache_write}。

    cache_window: '5m'(默认)、'1h' 或 '30m'，决定 cache 写入用哪档价。
    """
    default = pricing.get("default", {})
    raw = _raw_for_model(model, pricing)

    if raw is None:
        if model and model != "<synthetic>":
            _UNKNOWN_MODELS.add(model)
        raw = default

    raw = _effective_raw(raw, priced_at)
    if long_context and raw.get("long_context"):
        raw = {**raw, **raw["long_context"]}

    cw_key = {
        "1h": "cache_write_1h",
        "30m": "cache_write_30m",
    }.get(cache_window, "cache_write_5m")
    cache_write = raw.get(cw_key)
    if cache_write is None:
        # 请求的档位这个模型没定义时，退到该模型确实定义了的档位，而不是静默
        # 按 0 算——之前的写法在 cache_window="30m" 时 cw_key 本身就是
        # "cache_write_30m"，"cw_key != cache_write_30m" 恒假，这条兜底永远不
        # 会执行，导致没配 30m 价目的模型（现在除 3 个 GPT-5.6 系列外全部）在
        # 30m 场景下缓存写入直接算成 0。
        for fallback_key in ("cache_write_5m", "cache_write_30m", "cache_write_1h"):
            if fallback_key == cw_key:
                continue
            cache_write = raw.get(fallback_key)
            if cache_write is not None:
                break
    return {
        "input": raw.get("input", 0.0) or 0.0,
        "output": raw.get("output", 0.0) or 0.0,
        "cache_read": raw.get("cache_read", 0.0) or 0.0,
        "cache_write": cache_write or 0.0,
    }


def is_unknown_model(model: str, pricing: dict) -> bool:
    """判断模型是否缺少明确价格规则；不写入全局 unknown 状态。"""
    if not model or model == "<synthetic>":
        return False
    return _raw_for_model(model, pricing) is None


def cost_for(
    model: str,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cache_read_tokens: int = 0,
    cache_creation_tokens: int = 0,
    reasoning_tokens: int = 0,
    pricing: Optional[dict] = None,
    cache_window: str = "5m",
    long_context: bool = False,
    priced_at: Optional[date] = None,
) -> float:
    """按单价把各类 token 折算为美元。

    归一化口径（两来源统一）：
    - input_tokens = 全价输入（已剔除缓存）。
    - cache_read_tokens = 缓存命中，低价。
    - cache_creation_tokens = 缓存写入（Claude 或来源已明确标出的 OpenAI 写入）。
    - output_tokens 应为计费输出；若来源把 reasoning 单列，调用方需先并入 output。
      reasoning_tokens 仅展示、不另计费。
    """
    if pricing is None:
        pricing = load_pricing()
    r = rates_for_model(
        model,
        pricing,
        cache_window,
        long_context=long_context,
        priced_at=priced_at,
    )
    total = (
        input_tokens * r["input"]
        + output_tokens * r["output"]
        + cache_read_tokens * r["cache_read"]
        + cache_creation_tokens * r["cache_write"]
    )
    return total / _MILLION


def pricing_note(pricing: Optional[dict] = None) -> str:
    if pricing is None:
        pricing = load_pricing()
    return pricing.get("_meta", {}).get("note", "")


def unknown_models() -> list[str]:
    return sorted(_UNKNOWN_MODELS)


def clear_unknown_models() -> None:
    """清空进程内未知模型记录，主要用于测试与诊断前重置状态。"""
    _UNKNOWN_MODELS.clear()
