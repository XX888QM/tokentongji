# Token 统计仪表盘

统计本机 **Claude Code**、**Codex**、**OpenCode**、**OpenClaw**、**Hermes**、**Grok** 六类工具的 token 用量，
按天 / 周 / 月 / 累计汇总，桌面浏览器本地 Web 仪表盘实时展示。纯本地日志解析，**不调用任何外部 API（汇率除外）、零第三方依赖**。项目不提供手机端适配。

## 数据来源

| 来源 | 路径 | 取数方式 |
|------|------|---------|
| Claude | `~/.claude/projects/**/*.jsonl` | assistant 的 `message.usage`，按 `message.id` 去重；fallback 的 `usage.iterations` 按真实模型分别统计 |
| Codex | `~/.codex/sessions` + `archived_sessions` | `token_count` 的累积 `total_token_usage` 做相邻差分（防 2x 高估）；fork 出的 subagent 文件首条快照只作基线（防父会话重复计数） |
| OpenCode | `~/.local/share/opencode/opencode.db` | 直读 SQLite，按消息时间戳增量同步；reasoning token 计入 output |
| OpenClaw | `~/.openclaw/agents/main/sessions/*.jsonl` | 兼容 trajectory 与 v3 两种格式；同一 session 的完全相同调用跨格式去重 |
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

- 顶部卡片：今日 / 近 7 天 / 本月 / 累计 总 token + 估算费用（人民币，后台更新的 USD→CNY 缓存汇率），多来源占比分列
- 数字按万进制单位显示（万 / 亿 / 万亿 / 京 / 垓），悬停看精确值
- 折线图：近 30 天每日 token 趋势，多来源分线
- 拆分表：按 model、按项目（cwd）的 token + 费用排行，带合计行，cache token 单列，可切今日 / 近 7 天 / 本月 / 累计
- 运行审计：数据源路径状态、入库进度、口径风险（未知模型、跨来源会话等）
- 异常洞察：当日最大贡献模型 / 项目，环比基线对比
- TOP 10 最贵会话，点击展开按模型 / 文件明细
- 每 30s 自动刷新

费用以人民币展示。页面立即使用本机缓存汇率（首次默认 7.25），服务在后台向 `open.er-api.com` 刷新，缓存 1 小时；外部请求失败不会阻塞仪表盘。

## 开机自启（launchd，可选，仅 macOS）

```bash
# 安装（自动用当前项目路径生成 plist 并加载）
bash scripts/install-launchd.sh

# 卸载
bash scripts/uninstall-launchd.sh
```

日志：`data/tokenstat.log` / `data/tokenstat.err.log`

安装脚本只写入默认端口和 `PYTHONPATH`，不会继承当前终端里导出的其他 `TOKENSTAT_*` 变量；需要自定义 launchd 配置时，请修改 plist 模板后重新安装。部分 macOS 版本上 KeepAlive 退出后自动复活不稳定，遇到问题请使用上面的前台启动方式或自行选择进程管理器。同一端口已有手动服务时不要重复安装启动。

## 配置（环境变量）

| 变量 | 默认 | 说明 |
|------|------|------|
| `TOKENSTAT_HOST` | 127.0.0.1 | 监听地址 |
| `TOKENSTAT_PORT` | 8787 | Web 端口，须为正整数 |
| `TOKENSTAT_INGEST_INTERVAL` | 60 | 后台 ingest 间隔（秒），须为正整数 |
| `TOKENSTAT_REFRESH` | 30 | 页面自动刷新（秒），须为正整数 |
| `TOKENSTAT_STALE_DAYS` | 3 | 来源无新数据或落后其他来源多少天后告警，须为正整数 |
| `TOKENSTAT_DATA_DIR` | `./data` | SQLite 与日志目录 |
| `TOKENSTAT_GROK_LOG` | `~/.grok/logs/unified.jsonl` | Grok 统一日志路径 |

费用单价见 `src/tokenstat/pricing.json`，可自行调整（美元/百万 token）。本地/自托管模型放 `local` 分区按零费率处理。`codex-auto-review` 和 `gpt-5-codex` 按 OpenAI Codex 专项 `gpt-5.3-codex` 公开价格估算。
**注意：订阅制（Claude Max / Codex / Grok 套餐）下 token 不直接对应扣费，费用仅供参考。**

## 测试

需要本机已安装 Node.js（仅用于前端金额格式回归测试，运行仪表盘仍只需 Python）。

```bash
PYTHONPATH=src python3 -m unittest discover -s tests
```

## 常见问题

- 页面只有框架没有数据：先访问 `http://127.0.0.1:8787/api/health`；打不开说明服务未启动或端口被占用。
- 某个来源为空：确认上表中的数据源文件存在，并查看页面“运行审计”。缺失某个工具的数据不会阻止其他来源展示。
- 启动时报地址占用：先停止已有的手动或 launchd 服务，或设置新的 `TOKENSTAT_PORT`。

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
  aggregate.py   按天/周/月/累计聚合查询
  server.py      http.server（API + 静态 + 汇率 + 后台 ingest 线程）
  static/        index.html / app.js / styles.css / chart.min.js
```

`docs/superpowers/` 保存的是带日期的设计与实施记录，不是当前使用手册；当前行为以本 README、`CLAUDE.md` 和代码/测试为准。
