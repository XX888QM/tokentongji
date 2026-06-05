# Token 统计仪表盘

统计本机 **Claude Code** 与 **Codex** 的 token 用量，按天 / 周 / 月汇总，
本地 Web 仪表盘实时展示。纯本地日志解析，**不调用任何外部 API、零第三方依赖**。

## 数据来源

| 来源 | 路径 | 取数方式 |
|------|------|---------|
| Claude | `~/.claude/projects/**/*.jsonl` | assistant 的 `message.usage`，按 `message.id` 去重（防 2.6~3x 高估） |
| Codex | `~/.codex/sessions` + `archived_sessions` | `token_count` 的累积 `total_token_usage` 做相邻差分（防 2x 高估） |

两边数据已用独立脚本交叉对账，**0.000% 误差**。

## 快速开始

需要 Python 3.9+（标准库即可，**无需 pip install**）。

```bash
git clone https://github.com/<your-account>/tokentongji.git
cd tokentongji

# 1) 先全量入库一次（首次约 9 秒）
PYTHONPATH=src python3 -m tokenstat.ingest

# 2) 启动服务（含后台每 60s 增量 ingest + Web）
PYTHONPATH=src python3 -m tokenstat.server

# 3) 浏览器打开
open http://127.0.0.1:8787
```

## 仪表盘内容

- 顶部卡片：今日 / 本周 / 本月 / 今年 总 token + 估算费用，Claude/Codex 分列
- 数字按 万 / 百万 / 千万 / 亿 显示，悬停看精确值
- 折线图：近 30 天每日 token 趋势（Claude vs Codex）
- 拆分表：按 model、按项目（cwd）的 token + 费用排行，cache token 单列，可切今日/本周/本月/今年
- 每 30s 自动刷新

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
| `TOKENSTAT_PORT` | 8787 | Web 端口 |
| `TOKENSTAT_INGEST_INTERVAL` | 60 | 后台 ingest 间隔（秒） |
| `TOKENSTAT_REFRESH` | 30 | 页面自动刷新（秒） |
| `TOKENSTAT_DATA_DIR` | `./data` | SQLite 与日志目录 |

费用单价见 `src/tokenstat/pricing.json`，可自行调整（美元/百万 token）。
**注意：订阅制（Claude Max / Codex 套餐）下 token 不直接对应扣费，费用仅供参考。**

## 测试

```bash
PYTHONPATH=src python3 -m unittest discover -s tests
```

## 架构

```
src/tokenstat/
  config.py      全局配置
  models.py      UsageRecord 归一化数据模型
  db.py          SQLite（dedup_key 唯一去重 + 增量断点）
  parsers/
    claude.py    Claude 解析（msg.id 去重）
    codex.py     Codex 解析（total 差分 + carry-forward）
  ingest.py      增量入库（字节 offset 断点续读）
  pricing.py     费用估算 + model 归一化
  pricing.json   单价表
  aggregate.py   按天/周/月聚合查询
  server.py      http.server（API + 静态 + 后台 ingest 线程）
  static/        index.html / app.js / styles.css / chart.min.js
```
