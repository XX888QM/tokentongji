"""回归测试：/api/daily 非法 days 参数 fallback 行为。"""
import io
import json
import tempfile
import unittest
from unittest.mock import patch

from tokenstat import db
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


if __name__ == "__main__":
    unittest.main()
