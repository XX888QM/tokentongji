"""回归测试：/api/daily 非法 days 参数 fallback 行为。"""
import io
import json
import tempfile
import time
import unittest
from typing import Optional
from unittest.mock import patch

from tokenstat import db
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

    def test_health_endpoint_returns_200(self):
        code, body = _call_get("/api/health", self._db_path)
        self.assertEqual(code, 200)
        self.assertIn("status", body)
        self.assertIn("db", body)

    def test_insights_endpoint_returns_200(self):
        code, body = _call_get("/api/insights", self._db_path)
        self.assertEqual(code, 200)
        self.assertIn("cards", body)

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
