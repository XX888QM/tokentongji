"""归一化的 token 使用记录数据模型。

两个解析器（Claude / Codex）都产出同构的 ``UsageRecord``，
下游入库与聚合只认这一种结构。记录不可变（frozen dataclass）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta

# Asia/Shanghai 自 1991 年起无夏令时，固定 UTC+8。
# 优先用 zoneinfo（更规范），缺失则回退固定偏移——保证零依赖可运行。
try:  # pragma: no cover - 依环境而定
    from zoneinfo import ZoneInfo

    _LOCAL_TZ = ZoneInfo("Asia/Shanghai")
except Exception:  # pragma: no cover
    _LOCAL_TZ = timezone(timedelta(hours=8))


SOURCE_CLAUDE = "claude"
SOURCE_CODEX = "codex"
SOURCE_OPENCODE = "opencode"
SOURCE_OPENCLAW = "openclaw"
VALID_SOURCES = (SOURCE_CLAUDE, SOURCE_CODEX, SOURCE_OPENCODE, SOURCE_OPENCLAW)

CATEGORY_MAIN = "main"
CATEGORY_SUBAGENT = "subagent"
CATEGORY_OBSERVER = "observer"
VALID_CATEGORIES = (CATEGORY_MAIN, CATEGORY_SUBAGENT, CATEGORY_OBSERVER)


@dataclass(frozen=True)
class UsageRecord:
    """单条 token 使用增量记录。

    所有 token 字段都是「本条增量」，下游可直接逐条求和，不会重复计数：
    - Claude：每个 message.id 一条（已按 message.id 去重，取 output 最大）。
    - Codex：每个 token_count 事件对累积 total 做差分得到的本轮增量。

    归一化口径：
    - input_tokens = 全价输入（已剔除缓存命中部分）。
    - cache_read_tokens = 缓存命中输入。
    - cache_creation_tokens = 缓存写入（仅 Claude）。
    - output_tokens 已含 reasoning；reasoning_tokens 仅作展示。
    - total_tokens = input + output + cache_read + cache_creation。
    """

    ts: int  # epoch 秒（UTC）
    source: str  # "claude" | "codex"
    model: str
    project: str  # 完整 cwd 绝对路径（分组键）
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0
    reasoning_tokens: int = 0
    total_tokens: int = 0
    session_id: str = ""
    source_file: str = ""
    pos: int = 0  # 记录在文件内的字节偏移（调试/溯源用）
    category: str = CATEGORY_MAIN  # main | subagent | observer
    dedup_key: str = ""  # 幂等键：claude=message.id；codex=f"{file}#{pos}"

    def __post_init__(self) -> None:
        if self.source not in VALID_SOURCES:
            raise ValueError(f"未知 source: {self.source!r}，应为 {VALID_SOURCES}")
        if self.category not in VALID_CATEGORIES:
            raise ValueError(f"未知 category: {self.category!r}，应为 {VALID_CATEGORIES}")
        # 负数 token 视为脏数据，直接拒绝而不是静默吞掉
        for name in (
            "input_tokens",
            "output_tokens",
            "cache_read_tokens",
            "cache_creation_tokens",
            "reasoning_tokens",
            "total_tokens",
        ):
            value = getattr(self, name)
            if not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} 必须为非负整数，收到 {value!r}")

    @property
    def date_local(self) -> str:
        """按本地时区（Asia/Shanghai）得到的 YYYY-MM-DD，用于按天/周/月分桶。"""
        dt = datetime.fromtimestamp(self.ts, tz=timezone.utc).astimezone(_LOCAL_TZ)
        return dt.strftime("%Y-%m-%d")

    @property
    def computed_total(self) -> int:
        """若来源未给 total，则按可计费字段加和（不含 cache_read，避免与 input 重复语义时失真）。

        实际入库用 total_tokens 字段；此属性仅作兜底/校验。
        """
        if self.total_tokens > 0:
            return self.total_tokens
        return (
            self.input_tokens
            + self.output_tokens
            + self.cache_creation_tokens
            + self.cache_read_tokens
            + self.reasoning_tokens
        )


def local_date_of(ts: int) -> str:
    """工具函数：把 epoch 秒转成本地 YYYY-MM-DD。"""
    dt = datetime.fromtimestamp(ts, tz=timezone.utc).astimezone(_LOCAL_TZ)
    return dt.strftime("%Y-%m-%d")


def project_display(cwd: str) -> str:
    """项目显示名 = cwd 末段目录名，保留中文与大小写原样。"""
    if not cwd:
        return "(unknown)"
    name = cwd.rstrip("/").rsplit("/", 1)[-1]
    return name or cwd


def parse_iso_utc(ts: str) -> int:
    """把 ISO UTC（带 Z）时间戳转 epoch 秒。无法解析返回 0。"""
    if not ts:
        return 0
    try:
        clean = ts.replace("Z", "+00:00")
        dt = datetime.fromisoformat(clean)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp())
    except (ValueError, TypeError):
        return 0
