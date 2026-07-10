# Token 统计仪表盘

统计本机 **Claude Code**、**Codex**、**OpenCode**、**OpenClaw**、**Hermes**、**Grok** 六类工具的 token 用量，
按天 / 周 / 月 / 累计汇总，本地 Web 仪表盘实时展示。纯本地日志解析，**不调用任何外部 API（汇率除外）、零第三方依赖**。

## 数据来源

| 来源 | 路径 | 取数方式 |
|------|------|---------|
| Claude | `~/.claude/projects/**/*.jsonl` | assistant 的 `message.usage`，按 `message.id` 去重（防 2.6~3x 高估） |
| Codex | `~/.codex/sessions` + `archived_sessions` | `token_count` 的累积 `total_token_usage` 做相邻差分（防 2x 高估）；fork 出的 subagent 文件首条快照只作基线（防父会话重复计数） |
| OpenCode | `~/.local/share/opencode/opencode.db` | 直读 SQLite，按消息时间戳增量同步；reasoning token 计入 output |
| OpenClaw | `~/.openclaw/agents/main/sessions/*.jsonl` | 兼容 `*.trajectory.jsonl`（`model.completed`）与 v3 session（assistant message usage）两种格式 |
| Hermes | `~/.hermes/state.db` | 直读 SQLite `sessions` 表（按 session 原地累积的总量，非逐条事件），全表重扫 + `dedup_key` 取最大值幂等同步 |
| Grok | `~/.grok/logs/unified.jsonl` | `shell.turn.inference_done` 增量 token；model/cwd 靠同 sid 的 `model changed` / `session created` carry-forward |

Claude 与 Codex 两边数据已用独立脚本交叉对账，**0.000% 误差**。

## 快速开始

需要 Python 3.9+（标准库即可，**无需 pip install**）。

```bash
git clone https://github.com/XX888QM/tokentongji.git
cd tokentongji

# 1) 先全量入库一次（首次约 1 秒）
PYTHONPATH=src python3 -m tokenstat.ingest

# 2) 启动服务（含后台每 60s 增量 ingest + Web）
PYTHONPATH=src python3 -m tokenstat.server

# 3) 浏览器打开
open http://127.0.0.1:8787
```

## 仪表盘内容

- 顶部卡片：今日 / 近 7 天 / 本月 / 累计 总 token + 估算费用（人民币，实时 USD→CNY 汇率），多来源占比分列
- 数字按万进制单位显示（万 / 亿 / 万亿 / 京 / 垓），悬停看精确值
- 折线图：近 30 天每日 token 趋势，多来源分线
- 拆分表：按 model、按项目（cwd）的 token + 费用排行，带合计行，cache token 单列，可切今日 / 近 7 天 / 本月 / 累计
- 运行审计：数据源路径状态、入库进度、口径风险（未知模型、跨来源会话等）
- 异常洞察：当日最大贡献模型 / 项目，环比基线对比
- TOP 10 最贵会话，点击展开按模型 / 文件明细
- 每 30s 自动刷新

费用以人民币展示，汇率经 `open.er-api.com` 实时获取（1 小时缓存，失败回退 7.25）。

## 开机自启（launchd）

```bash
# 安装（自动用当前项目路径生成 plist 并加载）
bash scripts/install-launchd.sh

# 卸载
bash scripts/uninstall-launchd.sh
```

日志：`data/tokenstat.log` / `data/tokenstat.err.log`

## 配置（环境变量）

| 变量 | 默认 | 说明 |
|------|------|------|
| `TOKENSTAT_HOST` | 127.0.0.1 | 监听地址 |
| `TOKENSTAT_PORT` | 8787 | Web 端口 |
| `TOKENSTAT_INGEST_INTERVAL` | 60 | 后台 ingest 间隔（秒） |
| `TOKENSTAT_REFRESH` | 30 | 页面自动刷新（秒） |
| `TOKENSTAT_DATA_DIR` | `./data` | SQLite 与日志目录 |
| `TOKENSTAT_GROK_LOG` | `~/.grok/logs/unified.jsonl` | Grok 统一日志路径 |

费用单价见 `src/tokenstat/pricing.json`，可自行调整（美元/百万 token）。本地/自托管模型放 `local` 分区按零费率处理。`codex-auto-review` 和 `gpt-5-codex` 按 OpenAI Codex 专项 `gpt-5.3-codex` 公开价格估算。
**注意：订阅制（Claude Max / Codex / Grok 套餐）下 token 不直接对应扣费，费用仅供参考。**

## 测试

```bash
PYTHONPATH=src python3 -m unittest discover -s tests
```

## 架构

```
src/tokenstat/
  config.py      全局配置（数据源路径、端口、间隔）
  models.py      UsageRecord 归一化数据模型
  db.py          SQLite（dedup_key 唯一去重 + 增量断点）
  parsers/
    claude.py    Claude 解析（msg.id 去重）
    codex.py     Codex 解析（total 差分 + carry-forward）
    opencode.py  OpenCode 解析（SQLite 直读，增量同步）
    openclaw.py  OpenClaw 解析（trajectory + v3 双格式）
    hermes.py    Hermes 解析（SQLite sessions 表，全表重扫 + MAX 幂等）
    grok.py      Grok 解析（unified.jsonl inference_done + sid carry-forward）
  ingest.py      增量入库（字节 offset 断点续读）
  pricing.py     费用估算 + model 归一化
  pricing.json   单价表（anthropic / openai / deepseek / xai / local）
  aggregate.py   按天/周/月/累计聚合查询
  server.py      http.server（API + 静态 + 汇率 + 后台 ingest 线程）
  static/        index.html / app.js / styles.css / chart.min.js
```
