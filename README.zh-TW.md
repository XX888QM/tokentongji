# Token 統計儀表板

[🇨🇳 简体中文](README.md) · 🇹🇼 **繁體中文** · [🇺🇸 English](README.en.md) · [🇯🇵 日本語](README.ja.md) · [🇰🇷 한국어](README.ko.md) · [🇪🇸 Español](README.es.md)

統計本機 **Claude Code**、**Codex**、**OpenCode**、**OpenClaw**、**Hermes**、**Grok** 六類工具的 token 用量，依日 / 週 / 月 / 累計彙總，並在桌面瀏覽器的本機 Web 儀表板即時顯示。純本機日誌解析，**除匯率外不呼叫外部 API、零第三方相依套件**。本專案不提供手機版適配。

## 資料來源

| 來源 | 路徑 | 取數方式 |
|---|---|---|
| Claude | `~/.claude/projects/**/*.jsonl` | 讀取 assistant 的 `message.usage`，依 `message.id` 去重；fallback 的 `usage.iterations` 依真實模型分別統計 |
| Codex | `~/.codex/sessions` + `archived_sessions` | 對累積 `total_token_usage` 做相鄰差分；fork 的 subagent 檔案首筆快照只作基線 |
| OpenCode | `~/.local/share/opencode/opencode.db` | 直接讀取 SQLite，依訊息時間戳增量同步；reasoning token 計入 output |
| OpenClaw | `~/.openclaw/agents/main/sessions/*.jsonl` | 相容 trajectory 與 v3 格式，並移除兩種格式間的完全相同呼叫 |
| Hermes | `~/.hermes/state.db` | 讀取累積 session 資料列並同步覆蓋；reasoning 是 output 子集，不重複相加 |
| Grok | `~/.grok/logs/unified.jsonl` | 讀取 Grok CLI 或 claude-mem API 轉錄的 `shell.turn.inference_done` 增量 token；優先採用事件內的 model/cwd，否則依 sid 延續 |

Claude 與 Codex 的關鍵去重、差分及 fork 基線規則均有回歸測試。實際統計仍取決於各工具的日誌版本與本機歷史資料，建議搭配頁面「運行稽核」檢查來源新鮮度與未知模型。

> Grok 統計依賴既有的 `unified.jsonl`。本專案只讀取該檔案，不負責安裝 Grok/claude-mem 的日誌轉錄 hook；若日誌位於其他路徑，請設定 `TOKENSTAT_GROK_LOG`。

## 快速開始

需要 Python 3.9+，只使用標準函式庫，**不需要 `pip install`**。

```bash
git clone https://github.com/XX888QM/tokentongji.git
cd tokentongji

# 1) 建議先完整匯入一次（耗時取決於歷史日誌量）
PYTHONPATH=src python3 -m tokenstat.ingest

# 2) 啟動服務（含每 60 秒執行的背景增量 ingest）
PYTHONPATH=src python3 -m tokenstat.server

# 3) 開啟儀表板
open http://127.0.0.1:8787
```

## 儀表板內容

- 今日 / 近 7 天 / 本月 / 累計 token 總量、人民幣預估費用與各來源占比
- 以萬進位單位顯示數字（萬 / 億 / 萬億 / 京 / 垓），滑鼠懸停可看精確值
- 近 30 天多來源 token 趨勢折線圖
- 依模型與專案（cwd）拆分 token、費用、快取 token 與合計，可切換期間
- 運行稽核：資料來源路徑、匯入進度、未知模型與跨來源 session
- 異常洞察：當日最大模型 / 專案貢獻與基線比較
- TOP 10 最昂貴工作階段，可展開模型與來源檔案明細
- 每 30 秒自動更新

費用以人民幣顯示。頁面會立即使用本機快取匯率（首次預設 7.25），服務在背景向 `open.er-api.com` 更新 USD→CNY，並快取 1 小時；外部請求失敗不會阻塞儀表板。

## 登入時自動啟動（選用，僅 macOS）

```bash
# 安裝並載入使用目前專案路徑產生的 plist
bash scripts/install-launchd.sh

# 解除安裝
bash scripts/uninstall-launchd.sh
```

日誌：`data/tokenstat.log` / `data/tokenstat.err.log`

安裝腳本只寫入預設連接埠與 `PYTHONPATH`，不會繼承終端機匯出的其他 `TOKENSTAT_*` 變數。若 launchd 需要自訂設定，請修改 plist 範本後重新安裝。部分 macOS 版本的 KeepAlive 自動復活不穩定，必要時請使用前景啟動或其他程序管理器。同一連接埠不要重複啟動服務。

## 設定

| 變數 | 預設值 | 說明 |
|---|---|---|
| `TOKENSTAT_HOST` | 127.0.0.1 | 監聽位址 |
| `TOKENSTAT_PORT` | 8787 | Web 連接埠，必須是正整數 |
| `TOKENSTAT_INGEST_INTERVAL` | 60 | 背景 ingest 間隔（秒），必須為正數 |
| `TOKENSTAT_REFRESH` | 30 | 頁面更新間隔（秒），必須為正數 |
| `TOKENSTAT_STALE_DAYS` | 3 | 來源無新資料或落後其他來源多少天後發出警示 |
| `TOKENSTAT_DATA_DIR` | `./data` | SQLite 與日誌目錄 |
| `TOKENSTAT_GROK_LOG` | `~/.grok/logs/unified.jsonl` | Grok 統一日誌路徑 |

費用單價位於 `src/tokenstat/pricing.json`，單位為美元 / 每百萬 token。本機與自架模型使用零費率的 `local` 分區。`codex-auto-review` 和 `gpt-5-codex` 依 OpenAI Codex 公開的 `gpt-5.3-codex` 價格估算。

**注意：** 使用 Claude Max、Codex 或 Grok 訂閱時，token 用量不等於實際扣款；所有費用僅供參考。

## 測試

Node.js 只用於前端金額格式回歸測試；執行儀表板本身只需要 Python。

```bash
PYTHONPATH=src python3 -m unittest discover -s tests
```

## 常見問題

- 頁面只有框架沒有資料：開啟 `http://127.0.0.1:8787/api/health`；若無法連線，表示服務未啟動或連接埠被占用。
- 某個來源沒有資料：確認上方對應路徑，並查看「運行稽核」。缺少單一來源不會阻止其他來源顯示。
- 啟動時顯示位址被占用：停止現有的手動或 launchd 服務，或設定不同的 `TOKENSTAT_PORT`。

## 架構

```text
src/tokenstat/
  config.py      全域路徑、連接埠與間隔
  models.py      標準化 UsageRecord 模型
  db.py          SQLite 去重與匯入斷點
  parsers/
    claude.py    Claude message-id 去重與 fallback iterations
    codex.py     Codex 累積總量差分與內容延續
    opencode.py  OpenCode SQLite 增量讀取
    openclaw.py  OpenClaw trajectory 與 v3 格式
    hermes.py    Hermes SQLite sessions 全表同步覆蓋
    grok.py      Grok inference_done 與 sid 延續
  ingest.py      依 byte offset 增量匯入
  pricing.py     費用估算與模型名稱標準化
  pricing.json   anthropic / openai / deepseek / xai / local 單價
  aggregate.py   日 / 週 / 月 / 累計查詢
  server.py      HTTP API、靜態資源、匯率與 ingest 執行緒
  static/        index.html / app.js / styles.css / chart.min.js
```

`docs/superpowers/` 保存有日期的設計與實作記錄，不是目前的使用說明。當前行為以主 README、`CLAUDE.md`、程式碼與測試為準。
