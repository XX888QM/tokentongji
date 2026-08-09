"""Web 服务：静态仪表盘 + JSON API + 后台增量 ingest 线程。

单进程：主线程跑 HTTP server，后台守护线程每 INGEST_INTERVAL 秒增量入库。
只手动启动：项目在 ~/Desktop 下，macOS TCC 不让 launchd 进程读桌面文件。
"""

from __future__ import annotations

import json
import platform
import sqlite3
import subprocess
import threading
import time
import urllib.request
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs
import traceback

from . import aggregate, config, db
from . import pricing as pricing_mod
from .models import _LOCAL_TZ
from .parsers import hermes as hermes_parser

# ---- 汇率缓存（1小时 TTL，HTTP 请求只读缓存，外部刷新走后台线程） ----
_RATE_CACHE: dict = {"rate": 7.25, "ts": 0.0, "refreshing": False}
_RATE_TTL = 3600
_RATE_LOCK = threading.Lock()

# 后台自动入库和页面手动核对共用一把锁，避免两个扫描同时写同一个库。
_INGEST_LOCK = threading.Lock()
_INGEST_RUNTIME = {"running": False, "last_started": None, "last_finished": None,
                   "last_result": None, "last_error": None}
_INGEST_RUNTIME_LOCK = threading.Lock()


def _refresh_usd_cny_rate() -> None:
    rate = None
    try:
        req = urllib.request.Request(
            "https://open.er-api.com/v6/latest/USD",
            headers={"User-Agent": "tokenstat/1.0"},
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read(1_000_000))
        rate = float(data["rates"]["CNY"])
        if rate <= 0:
            rate = None
    except Exception:
        pass
    finally:
        with _RATE_LOCK:
            if rate is not None:
                _RATE_CACHE["rate"] = rate
            _RATE_CACHE["ts"] = time.time()
            _RATE_CACHE["refreshing"] = False


def _get_usd_cny_rate() -> float:
    with _RATE_LOCK:
        rate = _RATE_CACHE["rate"]
        stale = time.time() - _RATE_CACHE["ts"] >= _RATE_TTL
        should_refresh = stale and not _RATE_CACHE.get("refreshing", False)
        if should_refresh:
            _RATE_CACHE["refreshing"] = True
    if should_refresh:
        threading.Thread(target=_refresh_usd_cny_rate, daemon=True).start()
    return rate

STATIC_DIR = Path(__file__).resolve().parent / "static"

_CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".woff2": "font/woff2",
    ".woff": "font/woff",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".webp": "image/webp",
}


def _now_local_str() -> str:
    return datetime.now(tz=_LOCAL_TZ).strftime("%Y-%m-%d %H:%M:%S")


def _path_status(path: Path) -> dict:
    return {
        "path": str(path),
        "exists": path.exists(),
        "is_dir": path.is_dir(),
        "is_file": path.is_file(),
    }


def _db_status() -> dict:
    db_path = Path(config.DB_PATH)
    exists = db_path.exists()
    backup_dir = config.DATA_DIR / "backups"
    backups = sorted(backup_dir.glob("tokenstat-*.db"), key=lambda p: p.stat().st_mtime)
    latest_backup = backups[-1] if backups else None
    return {
        "path": str(db_path),
        "exists": exists,
        "size_bytes": db_path.stat().st_size if exists else 0,
        "latest_backup": latest_backup.name if latest_backup else None,
        "latest_backup_at": (
            datetime.fromtimestamp(latest_backup.stat().st_mtime, tz=_LOCAL_TZ).strftime("%Y-%m-%d %H:%M:%S")
            if latest_backup else None
        ),
    }


def _ingest_runtime() -> dict:
    with _INGEST_RUNTIME_LOCK:
        return dict(_INGEST_RUNTIME)


def _run_ingest_with_lock() -> None:
    """由持有 _INGEST_LOCK 的线程调用，记录页面可见的本次核对结果。"""
    from . import ingest

    with _INGEST_RUNTIME_LOCK:
        _INGEST_RUNTIME.update({
            "running": True, "last_started": _now_local_str(), "last_error": None,
        })
    try:
        result = ingest.run_once()
        with _INGEST_RUNTIME_LOCK:
            _INGEST_RUNTIME.update({"last_result": result, "last_finished": _now_local_str()})
        if result.get("records_added"):
            print(
                f"[ingest] 变更 {result['records_added']} 条 "
                f"(扫描 {result.get('files_scanned', 0)} 文件) @ {_now_local_str()}",
                flush=True,
            )
    except Exception as exc:
        print(f"[ingest] 出错: {exc}", flush=True)
        with _INGEST_RUNTIME_LOCK:
            _INGEST_RUNTIME["last_error"] = str(exc)[:300]
    finally:
        with _INGEST_RUNTIME_LOCK:
            _INGEST_RUNTIME["running"] = False
            _INGEST_RUNTIME["last_finished"] = _now_local_str()
        _INGEST_LOCK.release()


def _start_ingest() -> bool:
    """请求一次异步核对；已有扫描进行中时不重复启动。"""
    if not _INGEST_LOCK.acquire(blocking=False):
        return False
    with _INGEST_RUNTIME_LOCK:
        _INGEST_RUNTIME.update({
            "running": True, "last_started": _now_local_str(), "last_error": None,
        })
    threading.Thread(target=_run_ingest_with_lock, daemon=True).start()
    return True


def _source_collection_status(source: str, has_path: bool, last_date: str | None) -> dict:
    """区分路径缺失与近期未使用；后者不是采集故障。"""
    if not has_path:
        return {"state": "missing", "message": "采集路径不存在，历史已入库数据仍保留"}
    if not last_date:
        return {"state": "waiting", "message": "路径正常，尚未采集到用量"}
    lag = (datetime.now(tz=_LOCAL_TZ).date() - datetime.fromisoformat(last_date).date()).days
    if lag >= config.STALE_SOURCE_DAYS:
        return {"state": "idle", "message": f"路径正常，已 {lag} 天无新增；近期没用可忽略"}
    return {"state": "active", "message": "路径正常，近期已采集"}


def _source_activity_dates() -> dict[str, str]:
    """补充累计会话来源的最近活动日，不能拿它替代 token 归档日。"""
    ts = hermes_parser.latest_activity_ts(config.HERMES_STATE_DB)
    if not ts:
        return {}
    return {"hermes": datetime.fromtimestamp(ts, tz=_LOCAL_TZ).date().isoformat()}


def _load_audit() -> dict:
    pricing = pricing_mod.load_pricing()
    conn = db.get_conn(config.DB_PATH)
    try:
        return aggregate.audit(conn, pricing, activity_dates=_source_activity_dates())
    finally:
        conn.close()


class Handler(BaseHTTPRequestHandler):
    # 静音默认日志（避免刷屏），错误仍打印
    def log_message(self, fmt, *args):  # noqa: N802
        pass

    def _send_json(self, payload: dict, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_static(self, rel: str) -> None:
        # 防目录穿越
        safe = (STATIC_DIR / rel).resolve()
        if not safe.is_relative_to(STATIC_DIR.resolve()) or not safe.is_file():
            self.send_error(404, "Not Found")
            return
        body = safe.read_bytes()
        ctype = _CONTENT_TYPES.get(safe.suffix, "application/octet-stream")
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path
        qs = parse_qs(parsed.query)

        try:
            if path == "/" or path == "/index.html":
                self._send_static("index.html")
            elif path.startswith("/static/"):
                self._send_static(path[len("/static/"):])
            elif path == "/api/summary":
                self._api_summary()
            elif path == "/api/daily":
                try:
                    days = int(qs.get("days", ["30"])[0])
                except ValueError:
                    days = 30
                self._api_daily(days)
            elif path == "/api/breakdown":
                period = qs.get("period", ["month"])[0]
                self._api_breakdown(period)
            elif path == "/api/audit":
                self._api_audit()
            elif path == "/api/health":
                self._api_health()
            elif path == "/api/insights":
                self._api_insights()
            elif path == "/api/top_sessions":
                period = qs.get("period", ["today"])[0]
                limit_raw = qs.get("limit", ["10"])[0]
                self._api_top_sessions(period, limit_raw)
            elif path == "/api/session_detail":
                session_id = qs.get("session_id", [""])[0]
                period = qs.get("period", ["today"])[0]
                self._api_session_detail(session_id, period)
            elif path == "/api/export":
                period = qs.get("period", ["today"])[0]
                self._api_export(period)
            elif path == "/api/rates":
                self._send_json({"usd_cny": _get_usd_cny_rate()})
            else:
                self.send_error(404, "Not Found")
        except Exception:  # 任何 API 异常都回 JSON，不让连接挂死
            print(f"[server] API 错误: {traceback.format_exc()}", flush=True)
            self._send_json({"error": "internal server error"}, status=500)

    def do_POST(self):  # noqa: N802
        parsed = urlparse(self.path)
        try:
            if parsed.path not in ("/api/notify", "/api/ingest", "/api/backup"):
                self.send_error(404, "Not Found")
                return
            raw_len = self.headers.get("Content-Length", "0")
            try:
                length = max(0, min(int(raw_len), 2048))
            except ValueError:
                length = 0
            body = self.rfile.read(length) if length else b"{}"
            try:
                payload = json.loads(body.decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError):
                payload = {}
            if not isinstance(payload, dict):
                self._send_json({"error": "bad payload"}, status=400)
                return
            if parsed.path == "/api/notify":
                self._api_notify(payload.get("kind", "alert"), payload.get("message", ""))
            elif parsed.path == "/api/ingest":
                self._api_ingest()
            else:
                self._api_backup()
        except Exception:
            print(f"[server] API 错误: {traceback.format_exc()}", flush=True)
            self._send_json({"error": "internal server error"}, status=500)

    # ---- API ----
    def _conn(self):
        return db.get_conn(config.DB_PATH)

    def _api_summary(self):
        pricing = pricing_mod.load_pricing()
        conn = self._conn()
        try:
            data = aggregate.summary(conn, pricing)
        finally:
            conn.close()
        data["generated_at"] = _now_local_str()
        data["refresh_sec"] = config.DASHBOARD_REFRESH_SEC
        self._send_json(data)

    def _api_daily(self, days: int):
        days = max(1, min(days, 365))
        conn = self._conn()
        try:
            data = aggregate.daily(conn, days)
        finally:
            conn.close()
        self._send_json(data)

    def _api_breakdown(self, period: str):
        if period not in ("today", "week", "month", "all"):
            self._send_json({"error": f"bad period: {period}"}, status=400)
            return
        conn = self._conn()
        try:
            data = aggregate.breakdown(conn, period)
        finally:
            conn.close()
        self._send_json(data)

    def _api_audit(self):
        data = _load_audit()
        data["generated_at"] = _now_local_str()
        data["db"] = _db_status()
        data["data_sources"] = {
            "claude": _path_status(config.CLAUDE_PROJECTS_DIR),
            "codex": [
                *(_path_status(path) for path in config.CODEX_SESSION_DIRS),
                _path_status(config.CLAUDE_MEM_CODEX_USAGE_DIR),
            ],
            "opencode": _path_status(config.OPENCODE_DB_PATH),
            "openclaw": _path_status(config.OPENCLAW_SESSION_DIR),
            "hermes": _path_status(config.HERMES_STATE_DB),
            "grok": _path_status(config.GROK_LOG_PATH),
        }
        path_available = {
            "claude": data["data_sources"]["claude"]["exists"],
            "codex": any(item["exists"] for item in data["data_sources"]["codex"]),
            "opencode": data["data_sources"]["opencode"]["exists"],
            "openclaw": data["data_sources"]["openclaw"]["exists"],
            "hermes": data["data_sources"]["hermes"]["exists"],
            "grok": data["data_sources"]["grok"]["exists"],
        }
        for source in data["sources"]:
            activity_date = source.get("activity_last_date") or source.get("last_date")
            source["collection"] = _source_collection_status(
                source["source"], path_available.get(source["source"], False), activity_date
            )
            if source["source"] == "hermes" and activity_date != source.get("last_date"):
                source["collection"]["message"] = (
                    f"最近消息 {activity_date}；累计 token 仍按会话开始日归档"
                )
        data["runtime"] = _ingest_runtime()
        data["retention_note"] = "已入库数据独立保存在本机 SQLite；删除原始日志不会删除历史统计。"
        self._send_json(data)

    def _api_health(self):
        audit = _load_audit()
        payload = {
            "status": audit["status"],
            "generated_at": _now_local_str(),
            "db": _db_status(),
            "sources": audit["sources"],
            "ingest_state": audit["ingest_state"],
            "issues": audit["issues"],
            "runtime": _ingest_runtime(),
        }
        self._send_json(payload)

    def _api_insights(self):
        pricing = pricing_mod.load_pricing()
        conn = self._conn()
        try:
            data = aggregate.insights(conn, pricing)
        finally:
            conn.close()
        data["generated_at"] = _now_local_str()
        self._send_json(data)

    def _api_export(self, period: str):
        if period not in ("today", "week", "month", "all"):
            self._send_json({"error": f"bad period: {period}"}, status=400)
            return
        pricing = pricing_mod.load_pricing()
        conn = self._conn()
        try:
            rows = aggregate.export_rows(conn, period, pricing)
        finally:
            conn.close()
        import csv
        import io

        output = io.StringIO(newline="")
        writer = csv.writer(output)
        writer.writerow(["period", "source", "collector", "model", "project", "input_tokens", "output_tokens",
                         "cache_read_tokens", "cache_creation_tokens", "total_tokens", "cost_usd"])
        for row in rows:
            writer.writerow([period, row["source"], row["collector"] or "", row["model"], row["project"], row["input"], row["output"],
                             row["cache_read"], row["cache_creation"], row["total"], row["cost_usd"]])
        body = ("\ufeff" + output.getvalue()).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/csv; charset=utf-8")
        self.send_header("Content-Disposition", f'attachment; filename="tokenstat-{period}.csv"')
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _require_local_action(self, action: str) -> bool:
        client_host = self.client_address[0] if getattr(self, "client_address", None) else ""
        if client_host not in ("127.0.0.1", "::1", "localhost"):
            self._send_json({"ok": False, "error": "local requests only"}, status=403)
            return False
        if self.headers.get("X-Tokenstat-Action") != action:
            self._send_json({"ok": False, "error": "missing action header"}, status=403)
            return False
        return True

    def _api_ingest(self):
        if not self._require_local_action("ingest"):
            return
        started = _start_ingest()
        self._send_json({"ok": True, "started": started, "runtime": _ingest_runtime()})

    def _api_backup(self):
        if not self._require_local_action("backup"):
            return
        source = Path(config.DB_PATH)
        if not source.is_file():
            self._send_json({"ok": False, "error": "database not found"}, status=404)
            return
        name = f"tokenstat-{datetime.now(tz=_LOCAL_TZ).strftime('%Y%m%d-%H%M%S-%f')}.db"
        destination = config.DATA_DIR / "backups" / name
        try:
            db.backup_database(source, destination)
        except (OSError, sqlite3.Error) as exc:
            self._send_json({"ok": False, "error": str(exc)[:160]}, status=500)
            return
        self._send_json({"ok": True, "file": name, "size_bytes": destination.stat().st_size})

    def _api_top_sessions(self, period: str, limit_raw: str):
        if period not in ("today", "week", "month", "all"):
            self._send_json({"error": f"bad period: {period}"}, status=400)
            return
        try:
            limit = max(1, min(int(limit_raw), 50))
        except ValueError:
            limit = 10
        pricing = pricing_mod.load_pricing()
        conn = self._conn()
        try:
            data = aggregate.top_sessions(conn, period, pricing, limit)
        finally:
            conn.close()
        self._send_json(data)

    def _api_session_detail(self, session_id: str, period: str):
        if period not in ("today", "week", "month", "all"):
            self._send_json({"error": f"bad period: {period}"}, status=400)
            return
        if not session_id or len(session_id) > 128:
            self._send_json({"error": "bad session_id"}, status=400)
            return
        pricing = pricing_mod.load_pricing()
        conn = self._conn()
        try:
            data = aggregate.session_detail(conn, session_id, period, pricing)
        finally:
            conn.close()
        self._send_json(data)

    def _api_notify(self, kind: str, message: str):
        if not isinstance(kind, str) or not isinstance(message, str):
            self._send_json({"ok": False, "error": "bad payload"}, status=400)
            return
        if not self._require_local_action("notify"):
            return
        if kind != "alert":
            self._send_json({"ok": False, "error": "bad kind"}, status=400)
            return
        msg = (message or "").strip()
        if not msg:
            self._send_json({"ok": False, "error": "empty message"}, status=400)
            return
        msg = " ".join(msg[:180].split())
        if platform.system() != "Darwin":
            self._send_json({"ok": False, "error": "notifications only supported on macOS"}, status=501)
            return
        safe_msg = msg.replace("\\", "\\\\").replace('"', '\\"')
        script = f'display notification "{safe_msg}" with title "Token 统计告警"'
        try:
            result = subprocess.run(
                ["osascript", "-e", script],
                check=False,
                capture_output=True,
                text=True,
                timeout=3,
            )
        except (OSError, subprocess.TimeoutExpired, ValueError) as exc:
            # ValueError: message 含 NUL 字节时 subprocess 会抛「embedded null byte」
            self._send_json({"ok": False, "error": str(exc)}, status=500)
            return
        if result.returncode != 0:
            err = (result.stderr or "notification command failed").strip()[:160]
            self._send_json({"ok": False, "error": err}, status=500)
            return
        self._send_json({"ok": True})


def _ingest_loop(stop_event: threading.Event) -> None:
    """后台增量入库循环。延迟导入 ingest 以避免循环依赖。"""
    while not stop_event.is_set():
        if _INGEST_LOCK.acquire(blocking=False):
            _run_ingest_with_lock()
        stop_event.wait(config.INGEST_INTERVAL_SEC)


def serve() -> None:
    config.ensure_data_dir()
    conn = db.get_conn(config.DB_PATH)
    db.init_db(conn)
    conn.close()

    stop_event = threading.Event()
    t = threading.Thread(target=_ingest_loop, args=(stop_event,), daemon=True)
    t.start()

    httpd = ThreadingHTTPServer((config.HOST, config.PORT), Handler)
    print(
        f"[server] http://{config.HOST}:{config.PORT} 启动；"
        f"ingest 每 {config.INGEST_INTERVAL_SEC}s，页面每 {config.DASHBOARD_REFRESH_SEC}s 刷新",
        flush=True,
    )
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop_event.set()
        httpd.server_close()


if __name__ == "__main__":
    serve()
