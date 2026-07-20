"""回归测试：/api/daily 非法 days 参数 fallback 行为。"""
import io
import json
import tempfile
import time
import unittest
from datetime import date, datetime
from pathlib import Path
from typing import Optional
from unittest.mock import patch

from tokenstat import db, server
from tokenstat.models import UsageRecord
from tokenstat.server import Handler


def _call_get(path: str, db_path: str) -> tuple[int, dict]:
    """模拟一次 GET 请求，返回 (status_code, response_body)。"""
    response_code = [None]
    response_body = io.BytesIO()

    with patch("tokenstat.server.config.DB_PATH", db_path):
        with patch.object(Handler, "__init__", lambda *a, **kw: None):
            handler = Handler.__new__(Handler)

        handler.path = path
        handler.wfile = response_body
        handler.send_response = lambda code, msg=None: response_code.__setitem__(0, code)
        handler.send_header = lambda k, v: None
        handler.end_headers = lambda: None
        handler.send_error = lambda code, msg=None: response_code.__setitem__(0, code)

        handler.do_GET()

    body = json.loads(response_body.getvalue()) if response_body.getvalue() else {}
    return response_code[0], body


def _call_get_bytes(path: str, db_path: str) -> tuple[int, bytes]:
    """模拟一次非 JSON GET（CSV 导出）。"""
    response_code = [None]
    response_body = io.BytesIO()
    with patch("tokenstat.server.config.DB_PATH", db_path):
        with patch.object(Handler, "__init__", lambda *a, **kw: None):
            handler = Handler.__new__(Handler)
        handler.path = path
        handler.wfile = response_body
        handler.send_response = lambda code, msg=None: response_code.__setitem__(0, code)
        handler.send_header = lambda k, v: None
        handler.end_headers = lambda: None
        handler.send_error = lambda code, msg=None: response_code.__setitem__(0, code)
        handler.do_GET()
    return response_code[0], response_body.getvalue()


def _call_post(
    path: str,
    db_path: str,
    payload,
    headers: Optional[dict] = None,
    client_host: str = "127.0.0.1",
) -> tuple[int, dict]:
    """模拟一次 POST 请求，返回 (status_code, response_body)。"""
    response_code = [None]
    response_body = io.BytesIO()
    request_body = json.dumps(payload).encode("utf-8")
    request_headers = {
        "Content-Length": str(len(request_body)),
        **(headers or {}),
    }

    with patch("tokenstat.server.config.DB_PATH", db_path):
        with patch.object(Handler, "__init__", lambda *a, **kw: None):
            handler = Handler.__new__(Handler)

        handler.path = path
        handler.wfile = response_body
        handler.rfile = io.BytesIO(request_body)
        handler.headers = request_headers
        handler.client_address = (client_host, 12345)
        handler.send_response = lambda code, msg=None: response_code.__setitem__(0, code)
        handler.send_header = lambda k, v: None
        handler.end_headers = lambda: None
        handler.send_error = lambda code, msg=None: response_code.__setitem__(0, code)

        handler.do_POST()

    body = json.loads(response_body.getvalue()) if response_body.getvalue() else {}
    return response_code[0], body


class TestDailyEndpointFallback(unittest.TestCase):
    """回归：/api/daily?days=abc 应返回 200 并 fallback 为 30 天，不得 500。"""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self._db_path = self._tmpdir.name + "/test.db"
        conn = db.get_conn(self._db_path)
        db.init_db(conn)
        conn.close()

    def tearDown(self):
        self._tmpdir.cleanup()

    def test_invalid_days_returns_200_with_30_days(self):
        code, body = _call_get("/api/daily?days=abc", self._db_path)
        self.assertEqual(code, 200)
        self.assertIn("days", body)
        self.assertEqual(len(body["days"]), 30)

    def test_valid_days_returns_correct_length(self):
        code, body = _call_get("/api/daily?days=7", self._db_path)
        self.assertEqual(code, 200)
        self.assertEqual(len(body["days"]), 7)

    def test_audit_endpoint_returns_200(self):
        code, body = _call_get("/api/audit", self._db_path)
        self.assertEqual(code, 200)
        self.assertIn("status", body)
        self.assertIn("ingest_state", body)
        self.assertIn("opencode", body["data_sources"])
        self.assertIn("openclaw", body["data_sources"])
        self.assertIn("grok", body["data_sources"])

    def test_health_endpoint_returns_200(self):
        code, body = _call_get("/api/health", self._db_path)
        self.assertEqual(code, 200)
        self.assertIn("status", body)
        self.assertIn("db", body)

    def test_health_uses_hermes_message_activity_for_staleness(self):
        conn = db.get_conn(self._db_path)
        try:
            today = datetime.now(tz=server._LOCAL_TZ).date()
            base = int(datetime(today.year, today.month, today.day, tzinfo=server._LOCAL_TZ).timestamp())
            db.insert_records(conn, [
                UsageRecord(ts=base, source="claude", model="known", project="/a",
                            input_tokens=1, total_tokens=1, dedup_key="fresh-claude"),
                UsageRecord(ts=base, source="codex", model="known", project="/b",
                            input_tokens=1, total_tokens=1, dedup_key="fresh-codex"),
                UsageRecord(ts=base - 14 * 86400, source="hermes", model="known", project="/h",
                            input_tokens=1, total_tokens=1, dedup_key="old-hermes"),
            ])
        finally:
            conn.close()
        with patch("tokenstat.aggregate._today_local", return_value=today):
            with patch("tokenstat.server.hermes_parser.latest_activity_ts", return_value=base):
                code, body = _call_get("/api/health", self._db_path)
                audit_code, audit = _call_get("/api/audit", self._db_path)
        self.assertEqual(code, 200)
        hermes = next(source for source in body["sources"] if source["source"] == "hermes")
        self.assertEqual(hermes["activity_last_date"], today.isoformat())
        self.assertNotIn("hermes 已", " ".join(i["message"] for i in body["issues"]))
        self.assertEqual(audit_code, 200)
        audit_hermes = next(source for source in audit["sources"] if source["source"] == "hermes")
        self.assertEqual(audit_hermes["collection"]["state"], "active")

    def test_insights_endpoint_returns_200(self):
        code, body = _call_get("/api/insights", self._db_path)
        self.assertEqual(code, 200)
        self.assertIn("cards", body)

    def test_rates_endpoint_returns_cache_before_background_refresh(self):
        refresh_targets = []

        class DeferredThread:
            def __init__(self, target, daemon):
                self.target = target
                self.daemon = daemon

            def start(self):
                refresh_targets.append(self.target)

        with patch.dict(server._RATE_CACHE, {"rate": 7.25, "ts": 0.0}, clear=True):
            with patch("tokenstat.server.threading.Thread", DeferredThread):
                with patch("tokenstat.server.urllib.request.urlopen") as urlopen:
                    code, body = _call_get("/api/rates", self._db_path)

        self.assertEqual(code, 200)
        self.assertEqual(body, {"usd_cny": 7.25})
        self.assertEqual(len(refresh_targets), 1)
        urlopen.assert_not_called()

    def test_session_detail_requires_session_id(self):
        code, body = _call_get("/api/session_detail?period=today", self._db_path)
        self.assertEqual(code, 400)
        self.assertEqual(body["error"], "bad session_id")

    def test_session_detail_success(self):
        conn = db.get_conn(self._db_path)
        try:
            db.insert_records(conn, [
                UsageRecord(ts=int(time.time()), source="codex", model="gpt-5.5",
                            project="/tmp/proj", input_tokens=10, total_tokens=10,
                            session_id="sid-ok", dedup_key="sid-ok-1")
            ])
        finally:
            conn.close()
        code, body = _call_get("/api/session_detail?period=today&session_id=sid-ok", self._db_path)
        self.assertEqual(code, 200)
        self.assertEqual(body["session_id"], "sid-ok")
        self.assertEqual(body["summary"]["total"], 10)

    def test_export_endpoint_returns_current_period_csv(self):
        conn = db.get_conn(self._db_path)
        try:
            db.insert_records(conn, [
                UsageRecord(ts=int(time.time()), source="codex", model="gpt-5.5",
                            project="/tmp/proj", input_tokens=10, total_tokens=10,
                            dedup_key="export-1")
            ])
        finally:
            conn.close()
        code, body = _call_get_bytes("/api/export?period=today", self._db_path)
        self.assertEqual(code, 200)
        self.assertIn(b"source,model,project", body)
        self.assertIn(b"codex,gpt-5.5,/tmp/proj", body)

    def test_ingest_endpoint_requires_its_own_action_header(self):
        code, body = _call_post("/api/ingest", self._db_path, {}, headers={"X-Tokenstat-Action": "notify"})
        self.assertEqual(code, 403)
        self.assertEqual(body["error"], "missing action header")

    def test_ingest_endpoint_starts_single_background_check(self):
        with patch("tokenstat.server._start_ingest", return_value=True) as start:
            code, body = _call_post("/api/ingest", self._db_path, {}, headers={"X-Tokenstat-Action": "ingest"})
        self.assertEqual(code, 200)
        self.assertTrue(body["started"])
        start.assert_called_once()

    def test_backup_endpoint_creates_a_separate_database_file(self):
        with patch("tokenstat.server.config.DATA_DIR", Path(self._tmpdir.name)):
            code, body = _call_post("/api/backup", self._db_path, {}, headers={"X-Tokenstat-Action": "backup"})
        self.assertEqual(code, 200)
        self.assertTrue(body["file"].endswith(".db"))
        self.assertTrue((Path(self._tmpdir.name) / "backups" / body["file"]).is_file())

    def test_get_notify_is_not_allowed(self):
        code, _body = _call_get("/api/notify?kind=alert&message=x", self._db_path)
        self.assertEqual(code, 404)

    def test_notify_requires_action_header(self):
        code, body = _call_post(
            "/api/notify",
            self._db_path,
            {"kind": "alert", "message": "hello"},
        )
        self.assertEqual(code, 403)
        self.assertEqual(body["error"], "missing action header")

    def test_notify_rejects_non_local_client(self):
        code, body = _call_post(
            "/api/notify",
            self._db_path,
            {"kind": "alert", "message": "hello"},
            headers={"X-Tokenstat-Action": "notify"},
            client_host="192.168.1.2",
        )
        self.assertEqual(code, 403)
        self.assertEqual(body["error"], "local requests only")

    def test_notify_rejects_bad_payload_shape(self):
        code, body = _call_post(
            "/api/notify",
            self._db_path,
            ["not", "a", "dict"],
            headers={"X-Tokenstat-Action": "notify"},
        )
        self.assertEqual(code, 400)
        self.assertEqual(body["error"], "bad payload")

    def test_notify_rejects_non_string_message(self):
        code, body = _call_post(
            "/api/notify",
            self._db_path,
            {"kind": "alert", "message": {"bad": "shape"}},
            headers={"X-Tokenstat-Action": "notify"},
        )
        self.assertEqual(code, 400)
        self.assertEqual(body["error"], "bad payload")

    def test_notify_rejects_bad_kind_and_empty_message(self):
        headers = {"X-Tokenstat-Action": "notify"}
        code, body = _call_post(
            "/api/notify",
            self._db_path,
            {"kind": "other", "message": "hello"},
            headers=headers,
        )
        self.assertEqual(code, 400)
        self.assertEqual(body["error"], "bad kind")
        code, body = _call_post(
            "/api/notify",
            self._db_path,
            {"kind": "alert", "message": ""},
            headers=headers,
        )
        self.assertEqual(code, 400)
        self.assertEqual(body["error"], "empty message")

    def test_notify_uses_subprocess_without_shell(self):
        headers = {"X-Tokenstat-Action": "notify"}
        with patch("tokenstat.server.platform.system", return_value="Darwin"):
            with patch("tokenstat.server.subprocess.run") as run:
                run.return_value.returncode = 0
                run.return_value.stderr = ""
                code, body = _call_post(
                    "/api/notify",
                    self._db_path,
                    {"kind": "alert", "message": "hello"},
                    headers=headers,
                )
        self.assertEqual(code, 200)
        self.assertTrue(body["ok"])
        self.assertEqual(run.call_args.kwargs["timeout"], 3)
        self.assertTrue(run.call_args.kwargs["capture_output"])
        self.assertNotIn("shell", run.call_args.kwargs)

    def test_notify_command_failure_returns_500(self):
        headers = {"X-Tokenstat-Action": "notify"}
        with patch("tokenstat.server.platform.system", return_value="Darwin"):
            with patch("tokenstat.server.subprocess.run") as run:
                run.return_value.returncode = 1
                run.return_value.stderr = "osascript failed"
                code, body = _call_post(
                    "/api/notify",
                    self._db_path,
                    {"kind": "alert", "message": "hello"},
                    headers=headers,
                )
        self.assertEqual(code, 500)
        self.assertFalse(body["ok"])
        self.assertIn("osascript failed", body["error"])


if __name__ == "__main__":
    unittest.main()
