"""Cursor 仪表盘用量 CSV 解析器。

本机 Cursor 没有可入账的增量 token：

- `cursorDiskKV` bubble 的 `tokenCount` 实测几乎全是 `{0,0}`；
- `composerData` 的 `promptTokenBreakdown.totalUsedTokens` 是上下文窗口快照，
  不是逐轮增量，求和会系统性虚高；
- `~/.cursor/ai-tracking/ai-code-tracking.db` 只有模型/时间，没有 token 列。

可靠数字只在 Cursor 网页仪表盘。做法与 agent-walker 的 Cursor collector 相同：
只读 `state.vscdb` 的 `cursorAuth/accessToken`，用 JWT `sub` 拼
`WorkosCursorSessionToken`，`GET` `cursor.com` 的
`/api/dashboard/export-usage-events-csv?strategy=tokens`。不跟随重定向，
cookie 只发往 `cursor.com`。未登录、未装 Cursor、或 `TOKENSTAT_CURSOR=0`
时静默跳过，不发请求。

CSV 按列名解析（Cursor 会插列）。两列 input 是互斥的：
`Input (w/o Cache Write)` 是全价 input，`Input (w/ Cache Write)` 本身就是
cache write，不是超集。没有项目/仓库字段，`project` 固定为 `cursor`。
每行是一次请求，`request_prompt_tokens = input + cache_read + cache_write`。
CSV 没有事件 id：`dedup_key = cursor:{ts}:{model}:{input}:{write}:{read}:{output}:{n}`，
`n` 是同指纹在本批 CSV 里的出现序号，挡住同一秒完全相同的两行。

费用仍走 `pricing.json`。仪表盘 Cost 在订阅套餐里常是 `$0` / `Free` /
`Included`，不能拿来当本账本的权威扣费。`composer-*` 等未进价表的模型按
未知模型走 default，审计会亮出来。接口未公开，401/403 需要在 Cursor 里重新登录。
"""

from __future__ import annotations

import base64
import http.client
import json
import sqlite3
from dataclasses import replace
from pathlib import Path
from typing import Callable, List, Optional

from ..models import CATEGORY_MAIN, SOURCE_CURSOR, UsageRecord, parse_iso_utc

CURSOR_STATE_KEY = "cursor:dashboard-csv"
CSV_HOST = "cursor.com"
CSV_PATH = "/api/dashboard/export-usage-events-csv?strategy=tokens"
CSV_REFERER = "https://www.cursor.com/settings"
CSV_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
ACCESS_TOKEN_SQL = "SELECT value FROM ItemTable WHERE key = 'cursorAuth/accessToken'"
MAX_CSV_BYTES = 32 * 1024 * 1024
_FETCH_TIMEOUT_SEC = 15


class CursorSkip(Exception):
    """未登录或本地 store 不可用，本轮不发请求。"""


class CursorAuthError(Exception):
    """会话过期，需要在 Cursor 里重新登录。"""


class CursorFetchError(Exception):
    """网络、非 200、或意外重定向。"""


class CursorParseError(Exception):
    """CSV 表头对不上，不能静默当成 0 用量。"""


def sanitize_token(raw: str) -> Optional[str]:
    """只接受 JWT 字符集，拒绝 cookie 元字符和 header 注入。"""
    token = raw.strip().strip('"').strip()
    if len(token) < 10:
        return None
    # JWT 标准 base64url 不带 '='；带等号或 cookie 元字符一律不用。
    if not all(ch.isalnum() or ch in "-_." for ch in token):
        return None
    return token


def _jwt_subject(jwt: str) -> Optional[str]:
    parts = jwt.split(".")
    if len(parts) != 3:
        return None
    payload = parts[1]
    pad = "=" * (-len(payload) % 4)
    try:
        claims = json.loads(base64.urlsafe_b64decode(payload + pad))
    except (ValueError, json.JSONDecodeError):
        return None
    sub = claims.get("sub")
    return sub if isinstance(sub, str) else None


def _safe_subject_part(part: str) -> bool:
    if not part:
        return False
    return all(
        not (ch.isascii() and (ord(ch) < 32 or ord(ch) == 127 or ch in ' ",;\\|'))
        for ch in part
    )


def normalize_subject(subject: str) -> Optional[str]:
    """`…|user_XXX` 收成 `user_XXX`；其他单管 bridged OAuth 原样保留。"""
    tail = subject.rsplit("|", 1)[-1]
    if (
        tail.startswith("user_")
        and len(tail) > 5
        and all(ch.isalnum() or ch == "_" for ch in tail)
    ):
        return tail
    if subject.count("|") != 1:
        return None
    provider, account = subject.split("|", 1)
    if _safe_subject_part(provider) and _safe_subject_part(account):
        return subject
    return None


def account_id(jwt: str, cli_config: Optional[Path] = None) -> Optional[str]:
    sub = _jwt_subject(jwt)
    if sub:
        normalized = normalize_subject(sub)
        if normalized:
            return normalized
    if cli_config is None or not cli_config.is_file():
        return None
    try:
        cfg = json.loads(cli_config.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, TypeError):
        return None
    if not isinstance(cfg, dict):
        return None
    auth = cfg.get("authInfo")
    raw = auth.get("authId") if isinstance(auth, dict) else None
    if not isinstance(raw, str):
        return None
    return normalize_subject(raw)


def session_cookie(user_id: str, jwt: str) -> str:
    """只编码 `|`，`::` 固定写成 `%3A%3A`；JWT 已是 cookie 安全字符。"""
    return f"WorkosCursorSessionToken={user_id.replace('|', '%7C')}%3A%3A{jwt}"


def _open_ro(db_path: Path) -> Optional[sqlite3.Connection]:
    if not db_path.is_file():
        return None
    uri = str(db_path)
    for query in ("mode=ro", "immutable=1"):
        conn = None
        try:
            conn = sqlite3.connect(f"file:{uri}?{query}", uri=True, timeout=5.0)
            conn.execute("PRAGMA query_only=ON")
            return conn
        except sqlite3.Error:
            if conn is not None:
                conn.close()
            continue
    return None


def read_access_token(state_db: Path) -> Optional[str]:
    """只读 store。文件不在或未登录返回 None；库打不开视为 Skip。"""
    conn = _open_ro(state_db)
    if conn is None:
        return None
    try:
        row = conn.execute(ACCESS_TOKEN_SQL).fetchone()
    except sqlite3.Error:
        return None
    finally:
        conn.close()
    if not row or row[0] is None:
        return None
    value = row[0]
    if not isinstance(value, str):
        return None
    return sanitize_token(value)


def split_csv_line(line: str) -> List[str]:
    """按 RFC 式引号拆一行：`""` 转义，逗号在引号内不拆。"""
    fields: List[str] = []
    current: List[str] = []
    in_quotes = False
    length = len(line)
    idx = 0
    while idx < length:
        ch = line[idx]
        if in_quotes:
            if ch == '"':
                if idx + 1 < length and line[idx + 1] == '"':
                    current.append('"')
                    idx += 2
                    continue
                in_quotes = False
            else:
                current.append(ch)
        elif ch == '"':
            in_quotes = True
        elif ch == ",":
            fields.append("".join(current))
            current = []
        else:
            current.append(ch)
        idx += 1
    fields.append("".join(current))
    return fields


def _parse_required_tokens(cell: str) -> Optional[int]:
    text = cell.strip()
    if text == "":
        return 0
    if not text.isdigit():
        return None
    return int(text)


def parse_csv(text: str) -> List[UsageRecord]:
    """把仪表盘 CSV 收成增量 UsageRecord。表头不对会抛 CursorParseError。"""
    if text.startswith("\ufeff"):
        text = text.lstrip("\ufeff")
    lines = text.splitlines()
    if not lines:
        raise CursorParseError("empty Cursor usage CSV")
    header = split_csv_line(lines[0])
    columns = [cell.strip() for cell in header]
    index = {name: i for i, name in enumerate(columns)}

    required = (
        "Date",
        "Model",
        "Input (w/o Cache Write)",
        "Cache Read",
        "Output Tokens",
    )
    missing = [name for name in required if name not in index]
    if missing:
        raise CursorParseError(f"unrecognized Cursor usage CSV header: missing {missing}")

    date_idx = index["Date"]
    model_idx = index["Model"]
    input_idx = index["Input (w/o Cache Write)"]
    cache_read_idx = index["Cache Read"]
    output_idx = index["Output Tokens"]
    cache_write_idx = index.get("Input (w/ Cache Write)")
    session_idx = index.get("Cloud Agent ID")

    required_max = max(
        date_idx,
        model_idx,
        input_idx,
        cache_read_idx,
        output_idx,
        cache_write_idx if cache_write_idx is not None else 0,
    )

    parsed: List[tuple[str, UsageRecord]] = []
    for row_no, line in enumerate(lines[1:], start=2):
        if not line.strip():
            continue
        fields = split_csv_line(line)
        if len(fields) <= required_max:
            continue
        ts = parse_iso_utc(fields[date_idx].strip())
        if ts <= 0:
            continue
        input_tokens = _parse_required_tokens(fields[input_idx])
        output_tokens = _parse_required_tokens(fields[output_idx])
        cache_read = _parse_required_tokens(fields[cache_read_idx])
        if cache_write_idx is None:
            cache_write = 0
        else:
            cache_write = _parse_required_tokens(fields[cache_write_idx])
        if None in (input_tokens, output_tokens, cache_read, cache_write):
            continue
        if input_tokens + output_tokens + cache_read + cache_write <= 0:
            continue
        model = fields[model_idx].strip() or "unknown"
        session_id = ""
        if session_idx is not None and session_idx < len(fields):
            session_id = fields[session_idx].strip()
        fingerprint = (
            f"{ts}:{model}:{input_tokens}:{cache_write}:{cache_read}:{output_tokens}"
        )
        rec = UsageRecord(
            ts=ts,
            source=SOURCE_CURSOR,
            model=model,
            project="cursor",
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_read_tokens=cache_read,
            cache_creation_tokens=cache_write,
            total_tokens=input_tokens + output_tokens + cache_read + cache_write,
            request_prompt_tokens=input_tokens + cache_read + cache_write,
            session_id=session_id,
            source_file=CURSOR_STATE_KEY,
            pos=row_no,
            category=CATEGORY_MAIN,
            dedup_key="",
        )
        parsed.append((fingerprint, rec))

    counts: dict[str, int] = {}
    records: List[UsageRecord] = []
    for fingerprint, rec in parsed:
        counts[fingerprint] = counts.get(fingerprint, 0) + 1
        records.append(replace(rec, dedup_key=f"cursor:{fingerprint}:{counts[fingerprint]}"))
    return records


def fetch_csv(cookie: str, timeout: int = _FETCH_TIMEOUT_SEC) -> str:
    """只连 cursor.com，不跟随重定向，不把 cookie 带到别的主机。"""
    conn = http.client.HTTPSConnection(CSV_HOST, timeout=timeout)
    try:
        conn.request(
            "GET",
            CSV_PATH,
            headers={
                "Cookie": cookie,
                "Referer": CSV_REFERER,
                "User-Agent": CSV_USER_AGENT,
                "Accept": "text/csv, */*",
                "Accept-Encoding": "identity",
            },
        )
        resp = conn.getresponse()
        status = resp.status
        if status in (401, 403):
            raise CursorAuthError("Cursor 会话已过期，请在 Cursor 里重新登录")
        if 300 <= status < 400:
            raise CursorFetchError(f"unexpected redirect HTTP {status} from Cursor usage endpoint")
        if status != 200:
            raise CursorFetchError(f"HTTP {status} from Cursor usage endpoint")
        chunks: List[bytes] = []
        total = 0
        while True:
            chunk = resp.read(65536)
            if not chunk:
                break
            total += len(chunk)
            if total > MAX_CSV_BYTES:
                raise CursorFetchError("Cursor usage CSV exceeded size cap")
            chunks.append(chunk)
    finally:
        conn.close()
    try:
        return b"".join(chunks).decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise CursorFetchError("Cursor usage CSV was not UTF-8") from exc


def fetch_records(
    state_db: Path,
    *,
    cli_config: Optional[Path] = None,
    csv_text: Optional[str] = None,
    fetch_fn: Optional[Callable[[str], str]] = None,
) -> List[UsageRecord]:
    """读本机登录态并拉 CSV。csv_text / fetch_fn 只给测试注入。"""
    if csv_text is not None:
        return parse_csv(csv_text)
    if not state_db.is_file():
        raise CursorSkip("Cursor state.vscdb not found")
    jwt = read_access_token(state_db)
    if jwt is None:
        raise CursorSkip("Cursor signed out")
    user_id = account_id(jwt, cli_config)
    if user_id is None:
        raise CursorAuthError("Cursor session token has no usable account id")
    cookie = session_cookie(user_id, jwt)
    body = (fetch_fn or fetch_csv)(cookie)
    return parse_csv(body)
