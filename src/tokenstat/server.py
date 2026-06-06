"""Web 服务：静态仪表盘 + JSON API + 后台增量 ingest 线程。

单进程：主线程跑 HTTP server，后台守护线程每 INGEST_INTERVAL 秒增量入库。
launchd KeepAlive 负责保活。
"""

from __future__ import annotations

import json
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

from . import aggregate, config, db
from . import pricing as pricing_mod
from .models import _LOCAL_TZ

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
        if not str(safe).startswith(str(STATIC_DIR.resolve())) or not safe.is_file():
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
                days = int(qs.get("days", ["30"])[0])
                self._api_daily(days)
            elif path == "/api/breakdown":
                period = qs.get("period", ["month"])[0]
                self._api_breakdown(period)
            elif path == "/api/meta":
                self._api_meta()
            else:
                self.send_error(404, "Not Found")
        except Exception as e:  # 任何 API 异常都回 JSON，不让连接挂死
            self._send_json({"error": str(e)}, status=500)

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
