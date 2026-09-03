# AGENTS.md

This file provides guidance to Codex (Codex.ai/code) when working with code in this repository.

## Project

桌面端本地 Web 仪表盘，统计并汇总 Claude Code / Codex / OpenCode / OpenClaw / Hermes / Grok 六类物理 CLI/Agent 来源的 token 用量（按天/周/月/累计）。claude-mem 的 observer 调用归属实际使用的 Codex 或 Grok，页面会把它们作为统一的独立展示来源拆出。不维护手机端响应式布局。纯本地解析日志文件与 SQLite，Python 3.9+ 标准库即可运行，零第三方依赖（仅费用换算会在后台联网刷新 USD→CNY 缓存汇率）。

## Commands

```bash
# 首次全量入库（之后每次运行都是增量，只处理新增部分）
PYTHONPATH=src python3 -m tokenstat.ingest

# 启动服务：后台线程每 TOKENSTAT_INGEST_INTERVAL 秒自动增量 ingest + Web 服务
PYTHONPATH=src python3 -m tokenstat.server
# → http://127.0.0.1:8787

# 本机 LaunchAgent（避开桌面 TCC：副本跑在 ~/Library/Application Support/tokenstat）
bash scripts/install-launchd.sh
bash scripts/uninstall-launchd.sh

# 全部测试
PYTHONPATH=src python3 -m unittest discover -s tests

# 单个测试文件 / 单个用例
PYTHONPATH=src python3 -m unittest tests.test_pricing
PYTHONPATH=src python3 -m unittest tests.test_pricing.TestNormalization.test_sonnet_5
```

本机开机自启用 LaunchAgent `com.yunxin.tokenstat`（`RunAtLoad` + `KeepAlive`），与 hermes/openclaw 同一套路。launchd 子进程不能读 `~/Desktop`（实测 `open()` 报 `Operation not permitted`），所以 `scripts/install-launchd.sh` 会把 `src/` 拷到 `~/Library/Application Support/tokenstat/`，plist 只指向这份副本。改完仓库代码必须再跑一次安装脚本才会同步到自启进程。**库只在首装（副本里还没有 `data/tokenstat.db`）时从仓库 `data/` 迁一份当种子，之后重跑脚本一律不覆盖**——副本里那份才是真值，仓库 `data/` 是旧快照，无条件 cp 会抹掉自启进程后来采集的历史。"首装"不能只看"副本里有没有库文件"：脚本还会检查 `~/Library/LaunchAgents/com.yunxin.tokenstat.plist` 存不存在（这个 plist 只在脚本跑到最后才会写出）——库不见了但 plist 已经在，说明不是首装、大概率是库被误删/异常丢失，脚本会直接拒绝执行并提示先去 `data/backups/` 找备份，要强行用仓库旧快照重建必须显式带 `--force-reseed`。不要让 WorkingDirectory / PYTHONPATH 再指回桌面。装过自启后 `config.DATA_DIR` 默认也走 Application Support，避免手动启动写出第二套库。

无构建步骤，无 pip 依赖。运行仪表盘只需 Python；`tests/test_static_app.py` 会调用本机 Node.js 执行前端金额格式回归测试。仓库里没有 lint 配置（`.ruff_cache` 只是本地残留缓存，没有 `pyproject.toml`/`ruff.toml`，不要假设 ruff 已接入 CI/流程）。

## Architecture

数据流：6 个独立数据源 → 各自 parser 归一化成 `UsageRecord` → SQLite `usage_events` 表去重入库 → `aggregate.py` 按需查询聚合 → `server.py` 暴露 JSON API → `static/` 纯 JS 前端轮询渲染。

- **`config.py`** — 唯一配置入口，路径/端口/间隔均可用环境变量覆盖；所有整数配置须为正数。不要在别处硬编码路径。Grok 日志路径可用 `TOKENSTAT_GROK_LOG`，claude-mem Codex usage spool 路径可用 `TOKENSTAT_CLAUDE_MEM_CODEX_USAGE_DIR`，claude-mem Grok observer 日志可用 `TOKENSTAT_CLAUDE_MEM_GROK_LOG`。
- **`models.py`** — `UsageRecord`（frozen dataclass）是六来源统一的中间表示，下游（db/aggregate）只认这一种结构，不感知来源差异。所有 token 字段都是"本条增量"，可直接逐条求和，不会重复计数；`request_prompt_tokens` 仅在原始日志能确认一次完整 prompt（含缓存）时保存，供长上下文价判档，不能拿 Codex 累计差分猜。
- **`parsers/{Codex,codex,opencode,openclaw,hermes,grok}.py`** — 每来源一个模块，各自的去重键/差分逻辑是 recon 实测出来的坑（细节见各文件顶部 docstring），改动前务必先读：
  - Codex：普通消息用 `dedup_key = message.id`；同一 message 的流式重复行整体采用 total 更大的完整快照。fallback/retry 的 `usage.iterations` 必须拆成真实模型各一条，键统一为 `message.id:iteration:N`；看到 iterations 时删除此前的临时顶层 `message.id` 行，不能只读顶层 usage。
  - Codex：普通 `token_count` 事件给的是**累积总量**，必须做相邻差分，绝不能直接求和或累加 last；且该事件本身不带 model/cwd，靠 `CodexState` 按物理行顺序 carry-forward 最近的 `turn_context`。`dedup_key` 用**文件名**(`Path(source_file).name`)而非完整路径——Codex 会把 session 从 `sessions/` 挪进 `archived_sessions/`，同名文件两路径各解析一遍，键含完整路径就挡不住会系统性重复计数(曾造成约 14.7 亿 token 虚高)。**fork/subagent 两条铁律**：① `session_meta` 带 `forked_from_id` 的文件，首条 `token_count` 继承父会话的累积量（实测上亿），只作差分基线不计增量（`pending_baseline` 标记，持久化进 ctx）；② 同一文件内会交错出现父/子线程的 `session_meta`，但计数器是文件内连续的，**sid 变化绝不能重置差分基线**——这两处曾合计造成约 34.7 亿 token（Codex 的 29%）虚高。Codex-mem 的 ephemeral Codex 调用没有 session 文件，改读其单次 `turn.completed.usage` spool，归为 `observer`；`input_tokens` 含缓存命中，reasoning 是 output 子集，`cache_write_input_tokens` 暂只保留原始值而不重复计入估算。另：库里保留了 268 个已从磁盘删除的旧文件的行（约 51 亿 token），无法重扫复核，其中可能含同比例水分。
  - OpenCode：不解析日志文件，直接读它自己的 SQLite（`opencode.db`），按 `time_created`（毫秒）增量同步。opencode 写消息行是先插入 tokens 全 0 的占位行、流式结束后再原地 UPDATE 成真实值，`time_created` 全程不变——`fetch_records()` 的水位线因此只在本批所有行都解析成功时才推进到最新 ts，一旦某行解析失败（含全零占位行），该批次后续行一律不再推进水位线，保证下一轮重新扫到它；否则占位行之后被补上真实值就会被 `time_created >= 水位线` 永久排除在外（曾实测复现丢数据）。
  - OpenClaw：兼容 `*.trajectory.jsonl`（旧格式）与普通 `*.jsonl`（v3 session）两种格式；两套原始键不同。**trajectory 行不是 v3 行的逐条副本，而是若干 v3 行的合计**（实测 72 个配对会话里 59 个能用 v3 前缀和精确还原 trajectory 的累计量），时间戳还差几秒——所以不能按「时间戳 + token 全等」配对去重，那样只命中零星几条，曾残留 615 行 / 1.29 亿 token（约占 OpenClaw 5%）。现按**配对文件整体删除 trajectory 行、保留逐条明细的 v3 行**；没有配套 v3 的孤立 trajectory 是唯一数据源，必须保留。少数会话的 v3 被 `.jsonl.reset.*` 截断过、trajectory 反而更全（实测 3 个会话约 133 万 token），此处宁可少算也不虚高。配对判定必须用「v3 文件本轮是否还在磁盘上」（`ingest.py` 每轮 glob 到的 `active_v3_paths`），不能用「`usage_events` 里历史上是否出现过该 v3 路径」——用历史判定的话，v3 一旦被 `.jsonl.reset.*` 改名/停更，其历史行会永远留在库里让判定恒真，之后 trajectory 侧任何新行都会被无限期静默删掉（曾实测复现，比文档原先估计的一次性损失严重得多）。
  - Hermes：直读 `~/.hermes/state.db` 的 `sessions` 累计行。每轮全表重扫并按 `dedup_key=session id` **同步覆盖真实变化**，相同行不计变更；不能用字段 MAX，否则错误高值无法回调。上游明确 reasoning 是 output 子集，只单独保存用于展示，不再加进 output/total；`parent_session_id` 非空则归 `subagent`。
  - Grok：读 `~/.grok/logs/unified.jsonl` 和 `~/.claude-mem/observer-grok-home/logs/unified.jsonl` 的 `shell.turn.inference_done`（每 loop 增量，非累积）；`prompt_tokens - cached_prompt_tokens` 为全价 input，`cached_prompt_tokens` 为 cache_read，`completion_tokens` 为 output（含 reasoning 子集）。model/cwd 靠同 `sid` 的 `model changed` / `session created` carry-forward，字典持久化进 `ingest_state.ctx`。普通日志键为 `grok:{sid}:{ts}:{loop_index}`；claude-mem 隔离日志加 `claude-mem-grok:` 前缀并归为 `observer`。
- **`ingest.py`** — 增量入库编排层，对基于文件的来源按字节 offset + inode 做断点续读；`_should_read()` 处理文件被截断/重建（inode 变化）的情况，单行 >50MB 直接跳过防止撑爆内存。Codex / Grok 的 carry-forward 上下文持久化进 `ingest_state.ctx`，跨批次延续。
- **`db.py`** — 唯一持久化层，WAL 模式，每次操作开独立连接（sqlite3 连接开销很低，用这个规避跨线程共享连接的坑）。`usage_events.dedup_key` 唯一约束是幂等入库的关键。
- **`pricing.py` + `pricing.json`** — model 名归一化（剥离区域前缀/后缀）→ 精确匹配 → 最长前缀匹配 → 家族兜底（`_family_rates()`，同系列新版本未命中时退到该系列已知最新价）→ default。价表可用 `next_pricing.starts_on` 按历史日期生效（涨价、降价都走它：起始日填**过去**的日期即可让该日之前的历史行保持旧价，如 `gpt-5.6-sol` 的 2026-08-21 降价）；有 `long_context` 的模型必须按单次完整 prompt 分档，缺可靠口径的历史行保持基础档，不能把多行总和误判成长上下文。`long_context_thresholds()` 给聚合层生成 SQL 分桶列时，会同时扫每个模型的基础 `long_context` 和 `next_pricing.long_context` 两份阈值、取并集——不能只按"今天"生效的那个，否则 `next_pricing` 一旦真的改了阈值本身（不只是改价格），另一个阈值没有对应列，历史行会因为查不到列静默降级成基础价。**新模型上线时**要同步改两处：`pricing.json` 加价目条目，以及对应 family 的 `_family_rates()` 里 `pick()` 参数顺序（最新版本放最前面），否则未来的新版本会退到旧价格而不是最新价格。未知 model 会被记录到 `_UNKNOWN_MODELS`（fail-loud，不静默按 0 计费）。分区含 `anthropic` / `openai` / `deepseek` / `xai` / `local`。
- **`aggregate.py`** — 所有仪表盘用到的聚合都在这，一律按 `date_local`（Asia/Shanghai 本地日）分桶。`by_source` 永远是审计用的物理来源，Codex/Grok 仍包含各自的 claude-mem 调用；`by_display_source` 才是页面、趋势、明细、会话和 CSV 的统一展示口径，把这些 observer 统一拆为 `claude_mem`（virtual）。所有展示来源相加必须等于物理来源总数，绝不能再把 `claude_mem` 加到总数。`audit()` 同时检查最新来源距今天的绝对陈旧天数，以及单个来源落后最新来源的相对天数，避免全部采集一起停摆时假绿；另外还检查价目表 `_meta.verified_date` 是否超过 `config.PRICING_STALE_DAYS` 没核实过、算未知 model 的估算费用和占总用量的百分比（`unknown_models_detail`/`unknown_models_summary`，未知 model 走 default 价目本身不为 0，可以直接折算）、以及跨来源/模型/项目的"混合会话"在近 90 天窗口里的合计占比（`mixed_sessions_summary`，区分是零星几条还是已经影响不少数据）。`insights()` 的 `cache_savings_usd` 复用 `_grouped()` 按模型分组算出的行，逐行按各自单价算缓存命中省了多少钱，不能对全表一次性 SUM（不同模型单价不同）。
- **`server.py`** — 单进程 `ThreadingHTTPServer`：主线程处理 HTTP 请求，后台 daemon 线程按 `TOKENSTAT_INGEST_INTERVAL` 定时增量 ingest。汇率 API 只同步返回缓存值，外部 USD→CNY 请求由单个后台线程刷新，不能重新放回 HTTP 请求链阻塞首屏。API 路由手写分发在 `do_GET`/`do_POST` 里，没有框架。`/api/notify`、`/api/ingest`、`/api/backup` 都仅接受本机请求 + 对应自定义 header；手动核对和后台核对共用锁，不能并发写库。`_append_runtime_issues()` 把后台 ingest 最近一次异常（`_INGEST_RUNTIME["last_error"]`）和数据库多久没备份（超过 `config.BACKUP_STALE_DAYS`，复用 `_db_status()` 已经算好的 `latest_backup_at`，不重新扫一遍文件系统）也计入 `/api/audit`、`/api/health` 的 `status`/`issues`，不用等某个来源静默好几天才报警。CSV 导出使用 `/api/export`，不改库；其 `collector` 列为空表示普通记录，值为 `Codex-mem` 表示这条展示来源来自 Codex-mem。
- **桌面视觉系统**：`static/` 采用 Token Observatory HUD 方向。`static/assets/observatory-hud.webp` 是本地生成的主观测环图（源图 `observatory-hud.png` 一并留在仓库里，需要换图或重压时从它出）：固定底图提供仪器背景，裁切后的中心副本可做低速旋转；总量、费用、来源名称、来源占比和来源数值必须继续由 `app.js` 用实时接口数据渲染，不能把图片里的装饰当成统计数据。动效须遵循 `prefers-reduced-motion`，总量只在真实接口数值变化时插值，来源信号线只使用对应的真实展示来源色。冷青/蓝色用于仪器框架与状态，来源色仍遵循 `SOURCE_META`；标题、正文和数字统一使用系统字体（macOS 苹方优先），正文基准字号 15px，统计表格 16px，审计、图表和辅助信息不低于 13px，数字仅用 `tabular-nums` 对齐。页面最小宽度 1180px，不添加 viewport、移动端媒体查询或外部字体依赖。顶部区段导航、周期记忆、健康状态联动和设置弹窗键盘操作属于既定用户体验，不要在样式重构时删掉。

## 关键约定

- **Token 归一化口径**：`input_tokens` 是已剔除缓存命中的全价输入；`cache_read_tokens`/`cache_creation_tokens` 分开算；reasoning token 是否并入 output 因来源而异（Codex / Grok / Hermes 已是 output 子集，不重复相加；OpenCode 单独存字段，只在展示/计费时并入，见 `aggregate._row_output`）。改计费或展示逻辑前先确认没有破坏这个口径。
- **claude-mem 展示口径**：它是物理 Codex/Grok 内的 virtual display source，不是第七个物理来源。接口的 `by_source.codex` / `by_source.grok` 用于审计；页面统一消费 `by_display_source` 和带 `collector` 的行。任何新增页面/导出都必须复用同一个分类条件，不能在前端自行猜或把展示来源重复加回物理总数。
- **时区固定 Asia/Shanghai**（UTC+8，无夏令时）。`models.py` 里同时实现了 zoneinfo 优先 + 固定偏移兜底两套，保证在缺 tzdata 的环境也能零依赖运行。
- **费用是参考估算，不是真实扣费**——订阅制（Codex Max / Codex / Grok 套餐）下 token 不直接对应扣费，改 UI 文案时不要弱化这个免责声明。
- **历史保留与备份**：原始日志只用于新增采集；已经写入 `data/tokenstat.db` 的历史记录不会因日志删除而自动清除。页面备份会复制到 `data/backups/`，重建或清理前先备份。服务异常先看 `data/tokenstat.err.log`。
