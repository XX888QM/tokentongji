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
    out.update(pricing.get("anthropic", {}))
    out.update(pricing.get("openai", {}))
    return out


def _family_rates(clean: str, models: dict, default: dict) -> Optional[dict]:
    """已知家族的兜底：版本号没精确命中时按家族归档。"""
    def pick(*keys):
        for k in keys:
            if k in models:
                return models[k]
        return None

    if clean.startswith("claude-opus-4-1") or clean.startswith("claude-opus-4-0"):
        return pick("claude-opus-4-1")
    if clean.startswith("claude-opus"):
        return pick("claude-opus-4-8", "claude-opus-4-7")
    if clean.startswith("claude-sonnet"):
        return pick("claude-sonnet-4-6", "claude-sonnet-4-5")
    if clean.startswith("claude-haiku"):
        return pick("claude-haiku-4-5")
    if clean.startswith("gpt-5-codex") or clean == "gpt-5":
        return pick("gpt-5", "gpt-5-codex")
    if clean.startswith("gpt-5"):
        # gpt-5.x 未精确命中时退到最接近的基础款
        return pick("gpt-5", "gpt-5.4")
    return None


def rates_for_model(model: str, pricing: dict, cache_window: str = "5m") -> dict:
    """返回归一化单价 {input, output, cache_read, cache_write}。

    cache_window: '5m'(默认) 或 '1h'，决定 cache 写入用哪档价。
    """
    models = _merged_models(pricing)
    default = pricing.get("default", {})
    clean = _clean_model(model)

    raw = None
    if clean in models:
        raw = models[clean]
    else:
        # 最长前缀匹配
        best = ""
        for cand in models:
            if clean.startswith(cand) and len(cand) > len(best):
                best = cand
        if best:
            raw = models[best]
        else:
            raw = _family_rates(clean, models, default)

    if raw is None:
        if model and model != "<synthetic>":
            _UNKNOWN_MODELS.add(model)
        raw = default

    cw_key = "cache_write_1h" if cache_window == "1h" else "cache_write_5m"
    return {
        "input": raw.get("input", 0.0) or 0.0,
        "output": raw.get("output", 0.0) or 0.0,
        "cache_read": raw.get("cache_read", 0.0) or 0.0,
        "cache_write": raw.get(cw_key, 0.0) or 0.0,
    }


def cost_for(
    model: str,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cache_read_tokens: int = 0,
    cache_creation_tokens: int = 0,
    reasoning_tokens: int = 0,
    pricing: Optional[dict] = None,
    cache_window: str = "5m",
) -> float:
    """按单价把各类 token 折算为美元。

    归一化口径（两来源统一）：
    - input_tokens = 全价输入（已剔除缓存）。
    - cache_read_tokens = 缓存命中，低价。
    - cache_creation_tokens = 缓存写入（仅 Claude）。
    - output_tokens 已含 reasoning，reasoning_tokens 仅展示、不另计费。
    """
    if pricing is None:
        pricing = load_pricing()
    r = rates_for_model(model, pricing, cache_window)
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
