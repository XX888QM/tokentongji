# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

本地 Web 仪表盘，统计并汇总 Claude Code / Codex / OpenCode / OpenClaw / Hermes 五类 CLI/Agent 工具的 token 用量（按天/周/月/累计）。纯本地解析日志文件与 SQLite，Python 3.9+ 标准库即可运行，零第三方依赖（仅费用换算实时联网拉 USD→CNY 汇率）。

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

# 开机自启（launchd）—见下方"已知问题"，本机当前禁用，改手动启动
bash scripts/install-launchd.sh
bash scripts/uninstall-launchd.sh
```

无构建步骤，无 pip 依赖。仓库里没有 lint 配置（`.ruff_cache` 只是本地残留缓存，没有 `pyproject.toml`/`ruff.toml`，不要假设 ruff 已接入 CI/流程）。

## Architecture

数据流：5 个独立数据源 → 各自 parser 归一化成 `UsageRecord` → SQLite `usage_events` 表去重入库 → `aggregate.py` 按需查询聚合 → `server.py` 暴露 JSON API → `static/` 纯 JS 前端轮询渲染。

- **`config.py`** — 唯一配置入口，路径/端口/间隔均可用环境变量覆盖；不要在别处硬编码路径。
- **`models.py`** — `UsageRecord`（frozen dataclass）是五来源统一的中间表示，下游（db/aggregate）只认这一种结构，不感知来源差异。所有 token 字段都是"本条增量"，可直接逐条求和，不会重复计数。
- **`parsers/{claude,codex,opencode,openclaw,hermes}.py`** — 每来源一个模块，各自的去重键/差分逻辑是 recon 实测出来的坑（细节见各文件顶部 docstring），改动前务必先读：
  - Claude：`dedup_key = message.id`；同一 message 会拆成多行（thinking/text/tool_use）重复携带 usage，靠 `db.py` 里 `ON CONFLICT ... MAX` 在入库时取 output 最大值去重，否则会 2.6~3x 高估。
  - Codex：`token_count` 事件给的是**累积总量**，必须做相邻差分，绝不能直接求和或累加 last；且该事件本身不带 model/cwd，靠 `CodexState` 按物理行顺序 carry-forward 最近的 `turn_context`。`dedup_key` 用**文件名**(`Path(source_file).name`)而非完整路径——Codex 会把 session 从 `sessions/` 挪进 `archived_sessions/`，同名文件两路径各解析一遍，键含完整路径就挡不住会系统性重复计数(曾造成约 14.7 亿 token 虚高)。
  - OpenCode：不解析日志文件，直接读它自己的 SQLite（`opencode.db`），按 `time_created`（毫秒）增量同步。
  - OpenClaw：兼容 `*.trajectory.jsonl`（旧格式）与普通 `*.jsonl`（v3 session）两种格式，dedup_key 前缀不同。
  - Hermes：直读 `~/.hermes/state.db` 的 `sessions` 表，该表每行是**按 session 原地更新的累积总量**（不是逐条增量事件），且无可靠的"最后更新时间"列，所以不能像 OpenCode 那样用增量游标——长会话后续增长的部分会被永久跳过。改用**全表重扫 + `dedup_key=session id` + `ON CONFLICT MAX`**，语义上等价于每次都覆盖成该 session 的最新累计值，幂等且不会漏计增长。`reasoning_tokens` 在 parser 内直接折进 `output_tokens`（避免占用 `aggregate.py` 里只认 OpenCode 的 reasoning 特判分支）；`parent_session_id` 非空则归 `subagent` 分类（委派/子会话）。
- **`ingest.py`** — 增量入库编排层，对基于文件的来源按字节 offset + inode 做断点续读；`_should_read()` 处理文件被截断/重建（inode 变化）的情况，单行 >50MB 直接跳过防止撑爆内存。Codex 的 carry-forward 上下文持久化进 `ingest_state.ctx`，跨批次延续。
- **`db.py`** — 唯一持久化层，WAL 模式，每次操作开独立连接（sqlite3 连接开销很低，用这个规避跨线程共享连接的坑）。`usage_events.dedup_key` 唯一约束是幂等入库的关键。
- **`pricing.py` + `pricing.json`** — model 名归一化（剥离区域前缀/后缀）→ 精确匹配 → 最长前缀匹配 → 家族兜底（`_family_rates()`，同系列新版本未命中时退到该系列已知最新价）→ default。**新模型上线时**要同步改两处：`pricing.json` 加价目条目，以及对应 family 的 `_family_rates()` 里 `pick()` 参数顺序（最新版本放最前面），否则未来的新版本会退到旧价格而不是最新价格。未知 model 会被记录到 `_UNKNOWN_MODELS`（fail-loud，不静默按 0 计费）。
- **`aggregate.py`** — 所有仪表盘用到的聚合都在这，一律按 `date_local`（Asia/Shanghai 本地日）分桶。`audit()` 是数据质量自检，检测缺失来源、未知模型、跨来源/模型/项目的"混合会话"、以及**源陈旧**（某来源落后库内最新日期 ≥ `config.STALE_SOURCE_DAYS` 天，默认 3，环境变量 `TOKENSTAT_STALE_DAYS`，用来捕捉某数据源静默停更）。`insights()` 额外产出"后台消耗占比"卡片区分 observer/subagent 与主交互。
- **`server.py`** — 单进程 `ThreadingHTTPServer`：主线程处理 HTTP 请求，后台 daemon 线程按 `TOKENSTAT_INGEST_INTERVAL` 定时增量 ingest。API 路由手写分发在 `do_GET`/`do_POST` 里，没有框架。`/api/notify` 仅接受本机请求 + 自定义 header + 白名单 kind，用于触发 osascript 桌面通知，改这块要留意命令注入面（当前对 message 做了转义 + 长度截断）。

## 关键约定

- **Token 归一化口径**：`input_tokens` 是已剔除缓存命中的全价输入；`cache_read_tokens`/`cache_creation_tokens` 分开算；reasoning token 是否并入 output 因来源而异（Codex 已含在 output 里；OpenCode 单独存字段，只在展示/计费时并入，见 `aggregate._row_output`；Hermes 在 parser 内已折进 `output_tokens`，入库前就统一了口径）。改计费或展示逻辑前先确认没有破坏这个口径。
- **时区固定 Asia/Shanghai**（UTC+8，无夏令时）。`models.py` 里同时实现了 zoneinfo 优先 + 固定偏移兜底两套，保证在缺 tzdata 的环境也能零依赖运行。
- **费用是参考估算，不是真实扣费**——订阅制（Claude Max / Codex 套餐）下 token 不直接对应扣费，改 UI 文案时不要弱化这个免责声明。
- **launchd 开机自启在当前 macOS 版本上有已知的 KeepAlive 复活失效问题**：首次 `bootstrap` 能跑起来，但进程退出后不会自动重启，且无诊断日志可查。目前采用手动启动（见上方 Commands），`launchd/` 目录下的文件保留但不是默认使用路径。
