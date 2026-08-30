# Token 统计仪表盘

🇨🇳 **简体中文** · [🇹🇼 繁體中文](README.zh-TW.md) · [🇺🇸 English](README.en.md) · [🇯🇵 日本語](README.ja.md) · [🇰🇷 한국어](README.ko.md) · [🇪🇸 Español](README.es.md)

统计本机 **Claude Code**、**Codex**、**OpenCode**、**OpenClaw**、**Hermes**、**Grok** 六类工具的 token 用量，
按天 / 周 / 月 / 累计汇总，桌面浏览器本地 Web 仪表盘实时展示。纯本地日志解析，**不调用任何外部 API（汇率除外）、零第三方依赖**。项目不提供手机端适配。

## 数据来源

| 来源 | 路径 | 取数方式 |
|------|------|---------|
| Claude | `~/.claude/projects/**/*.jsonl` | assistant 的 `message.usage`，按 `message.id` 去重；fallback 的 `usage.iterations` 按真实模型分别统计 |
| Codex | `~/.codex/sessions` + `archived_sessions`；claude-mem 额外读取 `~/.claude-mem/usage/codex-usage-*.jsonl` | 普通会话对 `token_count` 的累积 `total_token_usage` 做相邻差分（防 2x 高估）；claude-mem 的 `codex exec --ephemeral` 用单次 `turn.completed.usage` 精确入账，标记为后台 observer，并在页面拆成 `Codex（直接）` 与 `claude-mem（Codex 额度）` |
| OpenCode | `~/.local/share/opencode/opencode.db` | 直读 SQLite，按消息时间戳增量同步；reasoning token 计入 output |
| OpenClaw | `~/.openclaw/agents/main/sessions/*.jsonl` | 兼容 trajectory 与 v3 两种格式；trajectory 行是 v3 多行的合计，同一 session 两格式并存时整段删 trajectory、保留 v3 明细 |
| Hermes | `~/.hermes/state.db` | 直读累计 session 行并同步覆盖；reasoning 是 output 子集，不重复相加 |
| Grok | `~/.grok/logs/unified.jsonl` | Grok CLI 与 claude-mem API 转录的 `shell.turn.inference_done` 增量 token；model/cwd 优先读事件内容，否则按 sid carry-forward |

Claude 与 Codex 的关键去重、差分和 fork 基线规则均有回归测试。实际统计结果仍取决于各工具日志版本与本机历史数据，建议结合页面“运行审计”检查来源新鲜度和未知模型。

> Grok 统计依赖已有的 `unified.jsonl`。本项目只读取该文件，不负责安装 Grok/claude-mem 的日志转录钩子；日志位于其他路径时请设置 `TOKENSTAT_GROK_LOG`。

## 快速开始

需要 Python 3.9+（标准库即可，**无需 pip install**）。

```bash
git clone https://github.com/XX888QM/tokentongji.git
cd tokentongji

# 1) 建议先全量入库一次（耗时取决于历史日志数量）
PYTHONPATH=src python3 -m tokenstat.ingest

# 2) 启动服务（含后台每 60s 增量 ingest + Web）
PYTHONPATH=src python3 -m tokenstat.server

# 3) 浏览器打开
open http://127.0.0.1:8787
```

## 仪表盘内容

- 顶部观测台：中心环形仪表读出今日总 token 与估算费用，来源分左右两列列出用量与占比；环图只是仪器背景，所有数字均来自实时接口
- 周期卡片：今日 / 近 7 天 / 本月 / 累计 总 token + 估算费用（人民币，后台更新的 USD→CNY 缓存汇率）；Codex 会拆成 `Codex（直接）` 和 `claude-mem（Codex 额度）`
- 数字按万进制单位显示（万 / 亿 / 万亿 / 京 / 垓），悬停看精确值
- 折线图：近 30 天每日 token 趋势，多来源分线，claude-mem 单独成线
- 拆分表：按 model、按项目（cwd）的 token + 费用排行，带合计行，cache token 单列；claude-mem 行明确标记 `claude-mem · Codex`，可切今日 / 近 7 天 / 本月 / 累计
- 运行审计：数据源路径状态、入库进度、口径风险（未知模型、跨来源会话等）
- 审计操作：可立即增量核对、备份 SQLite；原始日志后来删除也不会清掉已经入库的历史数据
- 导出：按当前周期导出来源 / 采集来源（`collector`）/ 模型 / 项目的明细 CSV
- 异常洞察：当日最大贡献模型 / 项目，环比基线对比
- TOP 10 最贵会话，点击展开按模型 / 文件明细
- 每 30s 自动刷新

费用以人民币展示。页面立即使用本机缓存汇率（首次默认 7.25），服务在后台向 `open.er-api.com` 刷新，缓存 1 小时；外部请求失败不会阻塞仪表盘。

### claude-mem 统计口径

claude-mem 不是另一份 Codex 额度：它调用的就是 Codex 额度。为让你看清“它到底用了多少”，页面把物理 Codex 记录拆成两个**展示来源**：`Codex（直接）` 和 `claude-mem（Codex 额度）`。两者相加才是原始 Codex 总量；总览、周期卡、趋势、拆分明细、会话和 CSV 都使用同一套拆分，**不会重复算进总 token 或费用**。运行审计仍按真实物理来源 Codex 判断采集健康。

## 启动方式

本机按 LaunchAgent 开机自启（`com.yunxin.tokenstat`，登录即拉起、挂了会重启）。macOS 不让 launchd 读桌面上的仓库，安装脚本会把代码拷到 `~/Library/Application Support/tokenstat/`，服务跑这份副本。数据库只在首装时从项目 `data/` 迁一份过去，之后重跑脚本不会覆盖副本里的库。

```bash
bash scripts/install-launchd.sh    # 安装/更新并立刻启动
# → http://127.0.0.1:8787
bash scripts/uninstall-launchd.sh  # 只卸自启，不删库
```

改完仓库后要再跑一次安装脚本，自启副本才会更新。日志在 `~/Library/Logs/tokenstat/`。也可以在终端手动：`PYTHONPATH=src python3 -m tokenstat.server`（已装自启时会共用 Application Support 里那份库）。

## 数据保留与导出

- 已装本机自启时，活库在 `~/Library/Application Support/tokenstat/data/tokenstat.db`；未装则用项目里的 `data/tokenstat.db`。删除 Claude、Codex 等原始日志不会自动删除数据库中的历史统计。
- 页面“备份数据库”会在对应数据目录下创建 `backups/tokenstat-*.db`；需要重建或清理数据前先备份。
- 页面“立即核对”只做一次增量扫描，不会清库；“导出当前 CSV”输出当前周期的来源、采集来源（`collector`）、模型、项目和费用明细。普通记录的 `collector` 为空，claude-mem 记录为 `claude-mem`。

## 配置（环境变量）

| 变量 | 默认 | 说明 |
|------|------|------|
| `TOKENSTAT_HOST` | 127.0.0.1 | 监听地址 |
| `TOKENSTAT_PORT` | 8787 | Web 端口，须为正整数 |
| `TOKENSTAT_INGEST_INTERVAL` | 60 | 后台 ingest 间隔（秒），须为正整数 |
| `TOKENSTAT_REFRESH` | 30 | 页面自动刷新（秒），须为正整数 |
| `TOKENSTAT_STALE_DAYS` | 3 | 来源无新数据或落后其他来源多少天后告警，须为正整数 |
| `TOKENSTAT_DATA_DIR` | 已装自启则为 `~/Library/Application Support/tokenstat/data`，否则 `./data` | SQLite 与备份目录 |
| `TOKENSTAT_GROK_LOG` | `~/.grok/logs/unified.jsonl` | Grok 统一日志路径 |
| `TOKENSTAT_CLAUDE_MEM_CODEX_USAGE_DIR` | `~/.claude-mem/usage` | claude-mem Codex 单次真实用量 JSONL 目录 |

费用单价见 `src/tokenstat/pricing.json`，可自行调整（美元/百万 token）。本地/自托管模型放 `local` 分区按零费率处理。`codex-auto-review` 按 OpenAI Codex 专项 `gpt-5.3-codex` 公开价格估算；`gpt-5-codex` 使用其自身公开价格。
**注意：订阅制（Claude Max / Codex / Grok 套餐）下 token 不直接对应扣费，费用仅供参考。**

## 测试

需要本机已安装 Node.js（仅用于前端金额格式回归测试，运行仪表盘仍只需 Python）。

```bash
PYTHONPATH=src python3 -m unittest discover -s tests
```

## 常见问题

- 页面只有框架没有数据：先访问 `http://127.0.0.1:8787/api/health`；打不开说明服务未启动或端口被占用。
- 某个来源为空：确认上表中的数据源文件存在，并查看页面“运行审计”。缺失某个工具的数据不会阻止其他来源展示。
- 审计显示“近期无新增”：采集路径正常但最近没有该工具用量，通常无需处理；显示“路径缺失”才表示当前无法继续采集，但既有历史数据仍在。
- 启动时报地址占用：先停止已在跑的服务，或设置新的 `TOKENSTAT_PORT`。

## 架构

```
src/tokenstat/
  config.py      全局配置（数据源路径、端口、间隔）
  models.py      UsageRecord 归一化数据模型
  db.py          SQLite（dedup_key 唯一去重 + 增量断点）
  parsers/
    claude.py    Claude 解析（msg.id 去重 + fallback iterations）
    codex.py     Codex 解析（total 差分 + carry-forward）
    opencode.py  OpenCode 解析（SQLite 直读，增量同步）
    openclaw.py  OpenClaw 解析（trajectory + v3 双格式）
    hermes.py    Hermes 解析（SQLite sessions 表，全表重扫 + 同步覆盖）
    grok.py      Grok 解析（unified.jsonl inference_done + sid carry-forward）
  ingest.py      增量入库（字节 offset 断点续读）
  pricing.py     费用估算 + model 归一化
  pricing.json   单价表（anthropic / openai / deepseek / xai / local）
  aggregate.py   按天/周/月/累计聚合查询（物理来源 + claude-mem 展示来源拆分）
  server.py      http.server（API + 静态 + 汇率 + 后台 ingest 线程）
  static/        index.html / app.js / styles.css / chart.min.js
```

`docs/superpowers/` 保存的是带日期的设计与实施记录，不是当前使用手册；当前行为以本 README、`CLAUDE.md` 和代码/测试为准。
