# Token 使用量ダッシュボード

[🇨🇳 简体中文](README.md) · [🇹🇼 繁體中文](README.zh-TW.md) · [🇺🇸 English](README.en.md) · 🇯🇵 **日本語** · [🇰🇷 한국어](README.ko.md) · [🇪🇸 Español](README.es.md)

ローカル環境の **Claude Code**、**Codex**、**OpenCode**、**OpenClaw**、**Hermes**、**Grok** の token 使用量を、日 / 週 / 月 / 累計で集計するデスクトップ向け Web ダッシュボードです。ログはローカルで解析し、**為替レート以外の外部 API を呼び出さず、サードパーティ依存もありません**。モバイル表示には対応していません。

## データソース

| ソース | パス | 取得方法 |
|---|---|---|
| Claude | `~/.claude/projects/**/*.jsonl` | assistant の `message.usage` を読み、`message.id` で重複排除。fallback の `usage.iterations` は実際のモデル別に集計 |
| Codex | `~/.codex/sessions` + `archived_sessions` | 累積 `total_token_usage` の隣接差分を計算。fork された subagent ファイルの最初のスナップショットは基準値としてのみ使用 |
| OpenCode | `~/.local/share/opencode/opencode.db` | SQLite を直接読み、メッセージ時刻で増分同期。reasoning token は output に含める |
| OpenClaw | `~/.openclaw/agents/main/sessions/*.jsonl` | trajectory と v3 の両形式に対応し、両方に存在する同一呼び出しを重複排除 |
| Hermes | `~/.hermes/state.db` | 累積 session 行を読み、変更を同期上書き。reasoning は output の一部なので二重加算しない |
| Grok | `~/.grok/logs/unified.jsonl` | Grok CLI または claude-mem API 転記の `shell.turn.inference_done` 増分 token を取得。イベント内の model/cwd を優先し、なければ sid ごとに引き継ぐ |

Claude と Codex の主要な重複排除、差分、fork 基準値ルールには回帰テストがあります。実際の統計は各ツールのログ形式とローカル履歴に依存するため、「稼働監査」でデータの鮮度と不明モデルを確認してください。

> Grok の統計には既存の `unified.jsonl` が必要です。本プロジェクトはファイルを読むだけで、Grok/claude-mem のログ転記 hook はインストールしません。別の場所にある場合は `TOKENSTAT_GROK_LOG` を設定してください。

## クイックスタート

Python 3.9 以上が必要です。標準ライブラリのみを使用するため、**`pip install` は不要です**。

```bash
git clone https://github.com/XX888QM/tokentongji.git
cd tokentongji

# 1) 初回は全履歴の取込を推奨（所要時間はログ量による）
PYTHONPATH=src python3 -m tokenstat.ingest

# 2) Web サービスと60秒ごとのバックグラウンド取込を開始
PYTHONPATH=src python3 -m tokenstat.server

# 3) ダッシュボードを開く
open http://127.0.0.1:8787
```

## ダッシュボード機能

- 今日 / 過去 7 日 / 今月 / 累計の token、人民元換算の推定費用、ソース別比率
- 中国語の大数単位（万 / 億 / 万億 / 京 / 垓）で表示し、ホバーで正確な値を確認
- 過去 30 日のソース別 token 推移
- モデル別・プロジェクト別（cwd）の token、費用、cache token、合計と期間切替
- 稼働監査：ソースパス、取込進捗、不明モデル、複数ソース混在 session
- 異常分析：当日の最大モデル / プロジェクト寄与と基準値比較
- 費用上位 10 セッションとモデル / ソースファイル詳細
- 30 秒ごとの自動更新

費用は人民元で表示します。初回はキャッシュ値 7.25 を即時使用し、サーバーがバックグラウンドで `open.er-api.com` から USD→CNY を更新して 1 時間キャッシュします。外部通信の失敗で画面は停止しません。

## ログイン時に起動（任意、macOS のみ）

```bash
# 現在のプロジェクトパスで plist を生成して読み込む
bash scripts/install-launchd.sh

# アンインストール
bash scripts/uninstall-launchd.sh
```

ログ：`data/tokenstat.log` / `data/tokenstat.err.log`

インストーラーが設定するのは既定ポートと `PYTHONPATH` だけで、シェルから export した他の `TOKENSTAT_*` 変数は引き継ぎません。launchd の設定を変更する場合は plist テンプレートを編集して再インストールしてください。一部の macOS では KeepAlive の再起動が不安定なため、必要に応じてフォアグラウンド起動や別のプロセスマネージャーを使用してください。同じポートでサービスを二重起動しないでください。

## 設定

| 変数 | 既定値 | 説明 |
|---|---|---|
| `TOKENSTAT_HOST` | 127.0.0.1 | リッスンアドレス |
| `TOKENSTAT_PORT` | 8787 | Web ポート。正の整数が必要 |
| `TOKENSTAT_INGEST_INTERVAL` | 60 | バックグラウンド取込間隔（秒）。正の値が必要 |
| `TOKENSTAT_REFRESH` | 30 | 画面更新間隔（秒）。正の値が必要 |
| `TOKENSTAT_STALE_DAYS` | 3 | ソースに新規データがない、または他ソースより遅れている場合に警告する日数 |
| `TOKENSTAT_DATA_DIR` | `./data` | SQLite とログのディレクトリ |
| `TOKENSTAT_GROK_LOG` | `~/.grok/logs/unified.jsonl` | Grok 統合ログのパス |

料金は `src/tokenstat/pricing.json` に USD / 100万 token で定義されています。ローカル / 自己ホストモデルはゼロ料金の `local` セクションを使用します。`codex-auto-review` と `gpt-5-codex` は OpenAI Codex の公開 `gpt-5.3-codex` 価格で推定します。

**注意：** Claude Max、Codex、Grok のサブスクリプションでは、token 使用量がそのまま請求額になるわけではありません。費用は参考値です。

## テスト

Node.js はフロントエンドの金額表示回帰テストにのみ必要です。ダッシュボードの実行には Python だけが必要です。

```bash
PYTHONPATH=src python3 -m unittest discover -s tests
```

## トラブルシューティング

- 枠だけ表示されデータがない：`http://127.0.0.1:8787/api/health` を開いてください。応答しない場合はサービス停止またはポート競合です。
- 特定ソースが空：上記のパスと「稼働監査」を確認してください。1つのソースがなくても他のソースは表示されます。
- アドレス使用中エラー：既存の手動 / launchd サービスを停止するか、別の `TOKENSTAT_PORT` を設定してください。

## アーキテクチャ

```text
src/tokenstat/
  config.py      パス、ポート、間隔の設定
  models.py      正規化 UsageRecord モデル
  db.py          SQLite 重複排除と取込チェックポイント
  parsers/
    claude.py    Claude message-id 重複排除と fallback iterations
    codex.py     Codex 累積値の差分とコンテキスト引継ぎ
    opencode.py  OpenCode SQLite の増分読取
    openclaw.py  OpenClaw trajectory / v3 形式
    hermes.py    Hermes SQLite sessions の全表同期上書き
    grok.py      Grok inference_done と sid 引継ぎ
  ingest.py      byte offset による増分取込
  pricing.py     費用推定とモデル名正規化
  pricing.json   anthropic / openai / deepseek / xai / local 料金
  aggregate.py   日 / 週 / 月 / 累計クエリ
  server.py      HTTP API、静的ファイル、為替、取込スレッド
  static/        index.html / app.js / styles.css / chart.min.js
```

`docs/superpowers/` は日付付きの設計・実装記録であり、現在の利用ガイドではありません。現在の動作はメイン README、`CLAUDE.md`、コード、テストを基準にしてください。
