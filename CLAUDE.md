# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

桌面端本地 Web 仪表盘，统计并汇总 Claude Code / Codex / OpenCode / OpenClaw / Hermes / Grok 六类 CLI/Agent 工具的 token 用量（按天/周/月/累计）。不维护手机端响应式布局。纯本地解析日志文件与 SQLite，Python 3.9+ 标准库即可运行，零第三方依赖（仅费用换算会在后台联网刷新 USD→CNY 缓存汇率）。

## Commands

```bash
# 首次全量入库（之后每次运行都是增量，只处理新增部分）
PYTHONPATH=src python3 -m tokenstat.ingest

# 启动服务：后台线程每 TOKENSTAT_INGEST_INTERVAL 秒自动增量 ingest + Web 服务
PYTHONPATH=src python3 -m tokenstat.server
# → http://127.0.0.1:8787

# 全部测试
PYTHONPATH=src python3 -m unittest discover -s tests

# 单个测试文件 / 单个用例
PYTHONPATH=src python3 -m unittest tests.test_pricing
PYTHONPATH=src python3 -m unittest tests.test_pricing.TestNormalization.test_sonnet_5

# 开机自启（launchd）—安装后用 launchctl print 确认服务已注册
bash scripts/install-launchd.sh
bash scripts/uninstall-launchd.sh
```

无构建步骤，无 pip 依赖。运行仪表盘只需 Python；`tests/test_static_app.py` 会调用本机 Node.js 执行前端金额格式回归测试。仓库里没有 lint 配置（`.ruff_cache` 只是本地残留缓存，没有 `pyproject.toml`/`ruff.toml`，不要假设 ruff 已接入 CI/流程）。

## Architecture

数据流：6 个独立数据源 → 各自 parser 归一化成 `UsageRecord` → SQLite `usage_events` 表去重入库 → `aggregate.py` 按需查询聚合 → `server.py` 暴露 JSON API → `static/` 纯 JS 前端轮询渲染。

- **`config.py`** — 唯一配置入口，路径/端口/间隔均可用环境变量覆盖；所有整数配置须为正数。不要在别处硬编码路径。Grok 日志路径可用 `TOKENSTAT_GROK_LOG`。
- **`models.py`** — `UsageRecord`（frozen dataclass）是六来源统一的中间表示，下游（db/aggregate）只认这一种结构，不感知来源差异。所有 token 字段都是"本条增量"，可直接逐条求和，不会重复计数；`request_prompt_tokens` 仅在原始日志能确认一次完整 prompt（含缓存）时保存，供长上下文价判档，不能拿 Codex 累计差分猜。
- **`parsers/{claude,codex,opencode,openclaw,hermes,grok}.py`** — 每来源一个模块，各自的去重键/差分逻辑是 recon 实测出来的坑（细节见各文件顶部 docstring），改动前务必先读：
  - Claude：普通消息用 `dedup_key = message.id`；同一 message 的流式重复行整体采用 total 更大的完整快照。fallback/retry 的 `usage.iterations` 必须拆成真实模型各一条，键统一为 `message.id:iteration:N`；看到 iterations 时删除此前的临时顶层 `message.id` 行，不能只读顶层 usage。
  - Codex：`token_count` 事件给的是**累积总量**，必须做相邻差分，绝不能直接求和或累加 last；且该事件本身不带 model/cwd，靠 `CodexState` 按物理行顺序 carry-forward 最近的 `turn_context`。`dedup_key` 用**文件名**(`Path(source_file).name`)而非完整路径——Codex 会把 session 从 `sessions/` 挪进 `archived_sessions/`，同名文件两路径各解析一遍，键含完整路径就挡不住会系统性重复计数(曾造成约 14.7 亿 token 虚高)。**fork/subagent 两条铁律**：① `session_meta` 带 `forked_from_id` 的文件，首条 `token_count` 继承父会话的累积量（实测上亿），只作差分基线不计增量（`pending_baseline` 标记，持久化进 ctx）；② 同一文件内会交错出现父/子线程的 `session_meta`，但计数器是文件内连续的，**sid 变化绝不能重置差分基线**——这两处曾合计造成约 34.7 亿 token（Codex 的 29%）虚高。另：库里保留了 268 个已从磁盘删除的旧文件的行（约 51 亿 token），无法重扫复核，其中可能含同比例水分。
  - OpenCode：不解析日志文件，直接读它自己的 SQLite（`opencode.db`），按 `time_created`（毫秒）增量同步。
  - OpenClaw：兼容 `*.trajectory.jsonl`（旧格式）与普通 `*.jsonl`（v3 session）两种格式；两套原始键不同。**trajectory 行不是 v3 行的逐条副本，而是若干 v3 行的合计**（实测 72 个配对会话里 59 个能用 v3 前缀和精确还原 trajectory 的累计量），时间戳还差几秒——所以不能按「时间戳 + token 全等」配对去重，那样只命中零星几条，曾残留 615 行 / 1.29 亿 token（约占 OpenClaw 5%）。现按**配对文件整体删除 trajectory 行、保留逐条明细的 v3 行**；没有配套 v3 的孤立 trajectory 是唯一数据源，必须保留。少数会话的 v3 被 `.jsonl.reset.*` 截断过、trajectory 反而更全（实测 3 个会话约 133 万 token），此处宁可少算也不虚高。
  - Hermes：直读 `~/.hermes/state.db` 的 `sessions` 累计行。每轮全表重扫并按 `dedup_key=session id` **同步覆盖真实变化**，相同行不计变更；不能用字段 MAX，否则错误高值无法回调。上游明确 reasoning 是 output 子集，只单独保存用于展示，不再加进 output/total；`parent_session_id` 非空则归 `subagent`。
  - Grok：读 `~/.grok/logs/unified.jsonl` 的 `shell.turn.inference_done`（每 loop 增量，非累积）；`prompt_tokens - cached_prompt_tokens` 为全价 input，`cached_prompt_tokens` 为 cache_read，`completion_tokens` 为 output（含 reasoning 子集）。claude-mem API 转录事件会直接带 model/cwd，优先使用；Grok CLI 事件则靠同 `sid` 的 `model changed` / `session created` carry-forward，字典持久化进 `ingest_state.ctx`。`dedup_key = grok:{sid}:{ts}:{loop_index}`。
- **`ingest.py`** — 增量入库编排层，对基于文件的来源按字节 offset + inode 做断点续读；`_should_read()` 处理文件被截断/重建（inode 变化）的情况，单行 >50MB 直接跳过防止撑爆内存。Codex / Grok 的 carry-forward 上下文持久化进 `ingest_state.ctx`，跨批次延续。
- **`db.py`** — 唯一持久化层，WAL 模式，每次操作开独立连接（sqlite3 连接开销很低，用这个规避跨线程共享连接的坑）。`usage_events.dedup_key` 唯一约束是幂等入库的关键。
- **`pricing.py` + `pricing.json`** — model 名归一化（剥离区域前缀/后缀）→ 精确匹配 → 最长前缀匹配 → 家族兜底（`_family_rates()`，同系列新版本未命中时退到该系列已知最新价）→ default。价表可用 `next_pricing.starts_on` 按历史日期生效；有 `long_context` 的模型必须按单次完整 prompt 分档，缺可靠口径的历史行保持基础档，不能把多行总和误判成长上下文。**新模型上线时**要同步改两处：`pricing.json` 加价目条目，以及对应 family 的 `_family_rates()` 里 `pick()` 参数顺序（最新版本放最前面），否则未来的新版本会退到旧价格而不是最新价格。未知 model 会被记录到 `_UNKNOWN_MODELS`（fail-loud，不静默按 0 计费）。分区含 `anthropic` / `openai` / `deepseek` / `xai` / `local`。
- **`aggregate.py`** — 所有仪表盘用到的聚合都在这，一律按 `date_local`（Asia/Shanghai 本地日）分桶。`audit()` 同时检查最新来源距今天的绝对陈旧天数，以及单个来源落后最新来源的相对天数，避免全部采集一起停摆时假绿。
- **`server.py`** — 单进程 `ThreadingHTTPServer`：主线程处理 HTTP 请求，后台 daemon 线程按 `TOKENSTAT_INGEST_INTERVAL` 定时增量 ingest。汇率 API 只同步返回缓存值，外部 USD→CNY 请求由单个后台线程刷新，不能重新放回 HTTP 请求链阻塞首屏。API 路由手写分发在 `do_GET`/`do_POST` 里，没有框架。`/api/notify`、`/api/ingest`、`/api/backup` 都仅接受本机请求 + 对应自定义 header；手动核对和后台核对共用锁，不能并发写库。CSV 导出使用 `/api/export`，不改库。
- **桌面视觉系统**：`static/` 采用 Night Ledger × Signal Room 方向。暖金只表示金额/核心总量，冷青表示运行状态；标题、正文和数字统一使用系统字体（macOS 苹方优先），正文基准字号 15px，统计表格 16px，审计、图表和辅助信息不低于 13px，数字仅用 `tabular-nums` 对齐。页面最小宽度 1180px，不添加 viewport、移动端媒体查询或外部字体依赖。顶部区段导航、周期记忆、健康状态联动和设置弹窗键盘操作属于既定用户体验，不要在样式重构时删掉。

## 关键约定

- **Token 归一化口径**：`input_tokens` 是已剔除缓存命中的全价输入；`cache_read_tokens`/`cache_creation_tokens` 分开算；reasoning token 是否并入 output 因来源而异（Codex / Grok / Hermes 已是 output 子集，不重复相加；OpenCode 单独存字段，只在展示/计费时并入，见 `aggregate._row_output`）。改计费或展示逻辑前先确认没有破坏这个口径。
- **时区固定 Asia/Shanghai**（UTC+8，无夏令时）。`models.py` 里同时实现了 zoneinfo 优先 + 固定偏移兜底两套，保证在缺 tzdata 的环境也能零依赖运行。
- **费用是参考估算，不是真实扣费**——订阅制（Claude Max / Codex / Grok 套餐）下 token 不直接对应扣费，改 UI 文案时不要弱化这个免责声明。
- **历史保留与备份**：原始日志只用于新增采集；已经写入 `data/tokenstat.db` 的历史记录不会因日志删除而自动清除。页面备份会复制到 `data/backups/`，重建或清理前先备份。`launchd` 安装脚本用 `bootstrap` / `kickstart` 并立即 `print` 注册状态；若服务未注册，优先查看 `data/tokenstat.err.log`。
