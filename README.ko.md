# Token 사용량 대시보드

[🇨🇳 简体中文](README.md) · [🇹🇼 繁體中文](README.zh-TW.md) · [🇺🇸 English](README.en.md) · [🇯🇵 日本語](README.ja.md) · 🇰🇷 **한국어** · [🇪🇸 Español](README.es.md)

로컬 **Claude Code**, **Codex**, **OpenCode**, **OpenClaw**, **Hermes**, **Grok**의 token 사용량을 일 / 주 / 월 / 누적으로 집계하는 데스크톱용 로컬 Web 대시보드입니다. 로그는 로컬에서 분석하며 **환율을 제외한 외부 API를 호출하지 않고 타사 의존성도 없습니다**. 모바일 화면은 지원하지 않습니다.

## 데이터 소스

| 소스 | 경로 | 수집 방식 |
|---|---|---|
| Claude | `~/.claude/projects/**/*.jsonl` | assistant의 `message.usage`를 읽고 `message.id`로 중복 제거. fallback `usage.iterations`는 실제 모델별로 집계 |
| Codex | `~/.codex/sessions` + `archived_sessions`; claude-mem은 `~/.claude-mem/usage/codex-usage-*.jsonl`도 읽음 | 누적 `total_token_usage`의 인접 차이를 계산. ephemeral `codex exec`는 한 번의 `turn.completed.usage`를 정확히 기록하고 `claude-mem（Codex 할당량）`으로 별도 표시 |
| OpenCode | `~/.local/share/opencode/opencode.db` | SQLite를 직접 읽고 메시지 시간 기준으로 증분 동기화. reasoning token은 output에 포함 |
| OpenClaw | `~/.openclaw/agents/main/sessions/*.jsonl` | trajectory와 v3 형식을 지원하고 두 형식에 중복된 동일 호출 제거 |
| Hermes | `~/.hermes/state.db` | 누적 session 행을 읽고 변경 내용을 덮어써 동기화. reasoning은 output의 일부이므로 중복 합산하지 않음 |
| Grok | `~/.grok/logs/unified.jsonl` | Grok CLI 또는 claude-mem API 전사의 `shell.turn.inference_done` 증분 token을 읽음. 이벤트의 model/cwd를 우선 사용하고 없으면 sid별로 이어받음 |

Claude와 Codex의 핵심 중복 제거, 차분, fork 기준값 규칙에는 회귀 테스트가 있습니다. 실제 결과는 각 도구의 로그 형식과 로컬 기록에 따라 달라질 수 있으므로 대시보드의 실행 감사를 통해 데이터 최신성과 알 수 없는 모델을 확인하세요.

> Grok 통계에는 기존 `unified.jsonl`이 필요합니다. 이 프로젝트는 파일만 읽으며 Grok/claude-mem 로그 전사 hook을 설치하지 않습니다. 파일이 다른 위치에 있으면 `TOKENSTAT_GROK_LOG`를 설정하세요.

## 빠른 시작

Python 3.9 이상이 필요하며 표준 라이브러리만 사용합니다. **`pip install`은 필요하지 않습니다.**

```bash
git clone https://github.com/XX888QM/tokentongji.git
cd tokentongji

# 1) 최초 전체 수집 권장(시간은 로그 기록량에 따라 달라짐)
PYTHONPATH=src python3 -m tokenstat.ingest

# 2) Web 서비스와 60초 간격 백그라운드 증분 수집 시작
PYTHONPATH=src python3 -m tokenstat.server

# 3) 대시보드 열기
open http://127.0.0.1:8787
```

## 대시보드 기능

- 오늘 / 최근 7일 / 이번 달 / 누적 token, CNY 예상 비용, 소스별 비중. Codex는 직접 사용분과 `claude-mem（Codex 할당량）`으로 분리
- 중국식 큰 수 단위(万 / 亿 / 万亿 / 京 / 垓)와 마우스 오버 시 정확한 값
- 최근 30일 소스별 token 추이 차트(claude-mem은 별도 계열)
- 모델 / 프로젝트(cwd)별 token, 비용, cache token, 합계 및 기간 전환. claude-mem 행은 `claude-mem · Codex`로 표시
- 실행 감사: 소스 경로, 수집 진행률, 알 수 없는 모델, 혼합 소스 session
- 이상 분석: 당일 최대 모델 / 프로젝트 기여와 기준값 비교
- 비용 상위 10개 세션과 모델 / 소스 파일 상세 정보
- 30초마다 자동 새로고침

비용은 CNY로 표시합니다. 페이지는 최초 기본값 7.25를 포함한 로컬 캐시 환율을 즉시 사용하며, 서버는 백그라운드에서 `open.er-api.com`의 USD→CNY 환율을 갱신해 1시간 캐시합니다. 외부 요청 실패가 대시보드를 차단하지 않습니다.

### claude-mem 집계 기준

claude-mem이 쓰는 것은 Codex 할당량이며 추가 Codex 사용량이 아닙니다. 대시보드는 물리 Codex를 `Codex（직접）`과 `claude-mem（Codex 할당량）`이라는 두 **표시 소스**로 나눕니다. 두 값을 합친 것이 물리 Codex 사용량이며 총 token과 비용에 중복 합산되지 않습니다. 소스 비중, 기간 카드, 추이, 상세, 세션, CSV는 같은 분리를 사용하고 실행 감사는 물리 Codex를 확인합니다.

## 수동 시작(launchd 없음)

서비스는 터미널에서 수동으로 시작하며 로그는 `data/tokenstat.log` / `data/tokenstat.err.log`에 기록됩니다.

프로젝트가 `~/Desktop` 아래에 있으므로 macOS TCC는 launchd 백그라운드 프로세스의 Desktop 파일 읽기를 막습니다(`Operation not permitted`, `EX_CONFIG` 78로 종료). 터미널 시작은 터미널 앱의 권한을 상속합니다. 프로젝트를 `~/Desktop` 밖으로 옮기거나 Python에 전체 디스크 접근 권한을 주기 전에는 자동 시작을 추가하지 마세요.

## 설정

| 변수 | 기본값 | 설명 |
|---|---|---|
| `TOKENSTAT_HOST` | 127.0.0.1 | 수신 주소 |
| `TOKENSTAT_PORT` | 8787 | Web 포트, 양의 정수여야 함 |
| `TOKENSTAT_INGEST_INTERVAL` | 60 | 백그라운드 수집 간격(초), 양수여야 함 |
| `TOKENSTAT_REFRESH` | 30 | 화면 새로고침 간격(초), 양수여야 함 |
| `TOKENSTAT_STALE_DAYS` | 3 | 소스에 새 데이터가 없거나 다른 소스보다 뒤처질 때 경고할 일수 |
| `TOKENSTAT_DATA_DIR` | `./data` | SQLite 및 로그 디렉터리 |
| `TOKENSTAT_GROK_LOG` | `~/.grok/logs/unified.jsonl` | Grok 통합 로그 경로 |
| `TOKENSTAT_CLAUDE_MEM_CODEX_USAGE_DIR` | `~/.claude-mem/usage` | claude-mem Codex 단일 사용량 JSONL 디렉터리 |

가격은 `src/tokenstat/pricing.json`에 USD / 백만 token 단위로 정의됩니다. 로컬 및 자체 호스팅 모델은 0원 요금의 `local` 섹션을 사용합니다. `codex-auto-review`와 `gpt-5-codex`는 공개된 OpenAI Codex `gpt-5.3-codex` 가격으로 추정합니다.

**주의:** Claude Max, Codex, Grok 구독에서는 token 사용량이 실제 청구액과 직접 일치하지 않습니다. 모든 비용은 참고용 추정치입니다.

## 테스트

Node.js는 프런트엔드 금액 형식 회귀 테스트에만 필요합니다. 대시보드 실행에는 Python만 필요합니다.

```bash
PYTHONPATH=src python3 -m unittest discover -s tests
```

## 문제 해결

- 화면 틀만 보이고 데이터가 없음: `http://127.0.0.1:8787/api/health`를 여세요. 응답하지 않으면 서비스가 중지됐거나 포트를 사용 중입니다.
- 특정 소스가 비어 있음: 위의 해당 경로와 실행 감사를 확인하세요. 한 소스가 없어도 다른 소스는 표시됩니다.
- 주소 사용 중 오류: 기존 수동 서비스를 중지하거나 다른 `TOKENSTAT_PORT`를 설정하세요.

## 구조

```text
src/tokenstat/
  config.py      경로, 포트, 간격 설정
  models.py      정규화된 UsageRecord 모델
  db.py          SQLite 중복 제거 및 수집 체크포인트
  parsers/
    claude.py    Claude message-id 중복 제거 및 fallback iterations
    codex.py     Codex 누적값 차분 및 컨텍스트 이어받기
    opencode.py  OpenCode SQLite 증분 읽기
    openclaw.py  OpenClaw trajectory / v3 형식
    hermes.py    Hermes SQLite sessions 전체 동기화 덮어쓰기
    grok.py      Grok inference_done 및 sid 이어받기
  ingest.py      byte offset 기반 증분 수집
  pricing.py     비용 추정 및 모델명 정규화
  pricing.json   anthropic / openai / deepseek / xai / local 가격
  aggregate.py   일 / 주 / 월 / 누적 조회
  server.py      HTTP API, 정적 파일, 환율, 수집 스레드
  static/        index.html / app.js / styles.css / chart.min.js
```

`docs/superpowers/`에는 날짜가 표시된 설계 및 구현 기록이 있으며 현재 사용 설명서가 아닙니다. 현재 동작은 기본 README, `CLAUDE.md`, 코드, 테스트를 기준으로 합니다.
