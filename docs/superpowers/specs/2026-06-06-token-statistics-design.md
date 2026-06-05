# Token 统计仪表盘 — 设计文档

日期：2026-06-06
作者：yunxin（大哥）+ Claude Code

## 1. 目标

统计本机 **Claude Code** 与 **Codex** 的 token 用量，按**每天 / 每周 / 每月**汇总，
并以本地 Web 仪表盘实时展示。纯本地日志解析，不调用任何外部 API。

## 2. 已确认的需求决策

| 决策点 | 选择 |
|--------|------|
| 交互形式 | **只要 Web 仪表盘**（不做 CLI） |
| 数据更新 | 后台增量汇总落库（SQLite） |
| 统计维度 | 按 model / 按项目目录 / cache token 单列 / 估算费用($) 全要 |
| 实时性 | 后台线程每 ~60s 增量入库，网页每 ~30s 自动刷新，体感实时 |
| 历史归档 | 统计 `~/.codex/archived_sessions` 与全部 `~/.claude/projects`，月度才完整 |
| 端口 | 默认 `8787`（可配置） |
| 时区 | 按 `Asia/Shanghai` 本地日期分桶 |
| 费用口径 | 按公开单价折算，**订阅制下仅供参考** |

## 3. 数据源（已实测）

### Claude
- 位置：`~/.claude/projects/**/*.jsonl`（约 1100+ 会话文件）
- 每条 `type=assistant` 记录的 `message.usage`：
  - `input_tokens` / `output_tokens`
  - `cache_creation_input_tokens` / `cache_read_input_tokens`
  - （`cache_creation.ephemeral_1h/5m_input_tokens` 为细分）
- `message.model`（如 `claude-opus-4-7`）— 干净
- `timestamp`（ISO UTC）
- 每条 usage 即单条增量，**逐条求和**即可
- project：从会话所在目录名（`-Users-yunxin-Desktop-xxx` → 路径）或记录内 `cwd` 字段还原

### Codex
- 位置：`~/.codex/sessions/**/*.jsonl` + `~/.codex/archived_sessions/**/*.jsonl`
- 首行 `payload.type=session_meta`：携带 `cwd`、`cli_version`、`model_provider`、`originator`
- `payload.type=token_count` 事件：
  - `info.last_token_usage`（**逐轮增量**）：`input_tokens` / `cached_input_tokens` / `output_tokens` / `reasoning_output_tokens` / `total_tokens`
  - `info.total_token_usage`（累计，用于校验）
  - **`info=null` 的是 rate-limit 心跳，必须跳过**
- model：尽力解析（GPT-5 系；缺失则标 `codex`）
- token 归属：先读 `session_meta` 拿 cwd/model 上下文，再把后续 `token_count` 增量归到该 session

## 4. 架构（单进程）

一个 Python 进程，launchd `KeepAlive` 保活：

```
┌─────────────────────────────────────────────┐
│  Python 进程 (launchd 保活)                    │
│  ┌────────────────┐    ┌───────────────────┐ │
│  │ 后台 ingest 线程 │───▶│   SQLite (唯一源)  │ │
│  │ 每 60s 增量扫日志 │    │  usage_events 表   │ │
│  └────────────────┘    └─────────▲─────────┘ │
│  ┌────────────────────────────────┴────────┐ │
│  │ http.server: /api/* (JSON) + 静态仪表盘   │ │
│  └──────────────────────────────────────────┘│
└─────────────────────────────────────────────┘
        ▲ 浏览器 localhost:8787 每 30s 自动刷新
```

单进程理由：后台线程入库 + 同进程读 DB，launchd 只守一个进程，不用两个定时任务。

## 5. 技术栈

- **Python 3 标准库**：`sqlite3` + `http.server` + `json` + `zoneinfo`，**零第三方依赖**
- 前端：单页 HTML + **Chart.js 本地内置**（不走 CDN）
- 配置 / 单价：JSON

## 6. 数据模型

归一化记录 `UsageRecord`（不可变 dataclass）：

| 字段 | 说明 |
|------|------|
| `ts` | epoch 秒（UTC） |
| `source` | `claude` \| `codex` |
| `model` | 模型名 |
| `project` | 项目（cwd 末段 / 解码目录名） |
| `input_tokens` | 输入 |
| `output_tokens` | 输出 |
| `cache_read_tokens` | 缓存读 |
| `cache_creation_tokens` | 缓存写 |
| `reasoning_tokens` | 推理（Codex） |
| `total_tokens` | 合计（缺失则按字段加和） |
| `session_id` | 会话 id |
| `source_file` | 来源文件绝对路径 |
| `line_no` | 文件内行号（用于幂等去重） |

SQLite 表：
- `usage_events`：上述字段 + `date_local`（派生，便于按天聚合）；唯一索引 `(source_file, line_no)`
- `ingest_state`：`source_file` PK、`inode`、`offset`（已读字节）、`mtime`

## 7. 增量入库（幂等）

- `ingest_state` 记每文件 `inode + offset`，每轮只读新增字节
- 文件截断/轮转（当前 size < 旧 offset 或 inode 变化）→ 从头重读
- `usage_events` 对 `(source_file, line_no)` 唯一索引，重跑不重复计数（`INSERT OR IGNORE`）

## 8. 聚合口径

- 按 `Asia/Shanghai` 本地日期分桶（避免 UTC 错位）
- 日 = 本地自然日；周 = 周一~周日；月 = 本地自然月
- 费用 = `Σ tokens_type × 单价_type`（input/output/cache_read/cache_write 分价），`pricing.json` 可配
- 标注订阅制仅供参考

## 9. 仪表盘内容

- 顶部卡片：今日 / 本周 / 本月 总 token + 估算费用
- 折线图：近 30 天每日 token 趋势（Claude vs Codex 两条线）
- 拆分表：按 model、按项目 的 token + 费用排行
- cache token 单列展示

## 10. 项目结构（多小文件，单文件 <300 行）

```
src/tokenstat/
  config.py  db.py  models.py  ingest.py  pricing.py  pricing.json  aggregate.py  server.py
  parsers/claude.py  parsers/codex.py
  static/index.html  app.js  chart.min.js  styles.css
launchd/com.yunxin.tokenstat.plist
tests/  (claude/codex 解析、ingest 幂等、aggregate、pricing；目标 80%+ 覆盖)
```

## 11. 测试与验收

- TDD：先写解析/聚合/幂等测试（真实日志片段做 fixture）
- 验收：launchd 跑起来 → 浏览器开 localhost:8787 看到真实日/周/月数字 → 与手动 grep 抽样核对一致

## 12. 非目标（YAGNI）

- 不做 CLI
- 不调外部 API、不联网拉价（单价写死可配）
- 不做多用户 / 鉴权（本地个人工具）
- 不做实时秒级 tail（60s 增量足够）
