"""Web 服务：静态仪表盘 + JSON API + 后台增量 ingest 线程。

单进程：主线程跑 HTTP server，后台守护线程每 INGEST_INTERVAL 秒增量入库。
launchd KeepAlive 负责保活。
"""

from __future__ import annotations

import json
import platform
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

# ---- 汇率缓存（1小时 TTL，失败降级到上次缓存值） ----
_RATE_CACHE: dict = {"rate": 7.25, "ts": 0.0}
_RATE_TTL = 3600
_RATE_LOCK = threading.Lock()


def _get_usd_cny_rate() -> float:
    with _RATE_LOCK:
        now = time.time()
        if now - _RATE_CACHE["ts"] < _RATE_TTL:
            return _RATE_CACHE["rate"]
    try:
        req = urllib.request.Request(
            "https://open.er-api.com/v6/latest/USD",
            headers={"User-Agent": "tokenstat/1.0"},
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read())
        rate = float(data["rates"]["CNY"])
        with _RATE_LOCK:
            _RATE_CACHE["rate"] = rate
            _RATE_CACHE["ts"] = time.time()
        return rate
    except Exception:
        return _RATE_CACHE["rate"]

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
    return {
        "path": str(db_path),
        "exists": exists,
        "size_bytes": db_path.stat().st_size if exists else 0,
    }


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
            elif path == "/api/meta":
                self._api_meta()
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
            if parsed.path != "/api/notify":
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
            self._api_notify(payload.get("kind", "alert"), payload.get("message", ""))
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
        if period not in ("today", "week", "month", "year"):
            self._send_json({"error": f"bad period: {period}"}, status=400)
            return
        conn = self._conn()
        try:
            data = aggregate.breakdown(conn, period)
        finally:
            conn.close()
        self._send_json(data)

    def _api_meta(self):
        conn = self._conn()
        try:
            data = aggregate.meta(conn)
        finally:
            conn.close()
        self._send_json(data)

    def _api_audit(self):
        pricing = pricing_mod.load_pricing()
        conn = self._conn()
        try:
            data = aggregate.audit(conn, pricing)
        finally:
            conn.close()
        data["generated_at"] = _now_local_str()
        data["db"] = _db_status()
        data["data_sources"] = {
            "claude": _path_status(config.CLAUDE_PROJECTS_DIR),
            "codex": [_path_status(path) for path in config.CODEX_SESSION_DIRS],
            "opencode": _path_status(config.OPENCODE_DB_PATH),
            "openclaw": _path_status(config.OPENCLAW_SESSION_DIR),
        }
        self._send_json(data)

    def _api_health(self):
        pricing = pricing_mod.load_pricing()
        conn = self._conn()
        try:
            audit = aggregate.audit(conn, pricing)
        finally:
            conn.close()
        payload = {
            "status": audit["status"],
            "generated_at": _now_local_str(),
            "db": _db_status(),
            "sources": audit["sources"],
            "ingest_state": audit["ingest_state"],
            "issues": audit["issues"],
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

    def _api_top_sessions(self, period: str, limit_raw: str):
        if period not in ("today", "week", "month", "year"):
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
        if period not in ("today", "week", "month", "year"):
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
        client_host = self.client_address[0] if getattr(self, "client_address", None) else ""
        if client_host not in ("127.0.0.1", "::1", "localhost"):
            self._send_json({"ok": False, "error": "local requests only"}, status=403)
            return
        if self.headers.get("X-Tokenstat-Action") != "notify":
            self._send_json({"ok": False, "error": "missing action header"}, status=403)
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
        except (OSError, subprocess.TimeoutExpired) as exc:
            self._send_json({"ok": False, "error": str(exc)}, status=500)
            return
        if result.returncode != 0:
            err = (result.stderr or "notification command failed").strip()[:160]
            self._send_json({"ok": False, "error": err}, status=500)
            return
        self._send_json({"ok": True})


def _ingest_loop(stop_event: threading.Event) -> None:
    """后台增量入库循环。延迟导入 ingest 以避免循环依赖。"""
    from . import ingest

    while not stop_event.is_set():
        try:
            result = ingest.run_once()
            if result.get("records_added"):
                print(
                    f"[ingest] +{result['records_added']} 条 "
                    f"(扫描 {result.get('files_scanned', 0)} 文件) @ {_now_local_str()}",
                    flush=True,
                )
        except Exception as e:  # ingest 出错不能拖垮服务
            print(f"[ingest] 出错: {e}", flush=True)
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
