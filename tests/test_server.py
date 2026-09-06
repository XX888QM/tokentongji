"""回归测试：/api/daily 非法 days 参数 fallback 行为。"""
import io
import json
import os
import tempfile
import time
import unittest
from datetime import date, datetime
from pathlib import Path
from typing import Optional
from unittest.mock import patch

from tokenstat import config, db, ingest, server
from tokenstat.models import UsageRecord
from tokenstat.server import Handler


def _call_get(
    path: str,
    db_path: str,
    headers: Optional[dict] = None,
    client_host: str = "127.0.0.1",
) -> tuple[int, dict]:
    """模拟一次 GET 请求，返回 (status_code, response_body)。"""
    response_code = [None]
    response_body = io.BytesIO()

    with patch("tokenstat.server.config.DB_PATH", db_path):
        with patch.object(Handler, "__init__", lambda *a, **kw: None):
            handler = Handler.__new__(Handler)

        handler.path = path
        handler.headers = {"Host": f"127.0.0.1:{config.PORT}", **(headers or {})}
        handler.client_address = (client_host, 12345)
        handler.wfile = response_body
        handler.send_response = lambda code, msg=None: response_code.__setitem__(0, code)
        handler.send_header = lambda k, v: None
        handler.end_headers = lambda: None
        handler.send_error = lambda code, msg=None: response_code.__setitem__(0, code)

        handler.do_GET()

    body = json.loads(response_body.getvalue()) if response_body.getvalue() else {}
    return response_code[0], body


def _call_get_bytes(
    path: str,
    db_path: str,
    headers: Optional[dict] = None,
    client_host: str = "127.0.0.1",
) -> tuple[int, bytes]:
    """模拟一次非 JSON GET（CSV 导出）。"""
    response_code = [None]
    response_body = io.BytesIO()
    with patch("tokenstat.server.config.DB_PATH", db_path):
        with patch.object(Handler, "__init__", lambda *a, **kw: None):
            handler = Handler.__new__(Handler)
        handler.path = path
        handler.headers = {"Host": f"127.0.0.1:{config.PORT}", **(headers or {})}
        handler.client_address = (client_host, 12345)
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
        "Host": f"127.0.0.1:{config.PORT}",
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
        self.assertIn("hermes 已", " ".join(i["message"] for i in body["issues"]))
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
                            dedup_key="export-1"),
                UsageRecord(ts=int(time.time()), source="codex", model="gpt-5.6-luna",
                            project="/tmp/claude-mem", input_tokens=20, total_tokens=20,
                            category="observer", dedup_key="claude-mem-codex:export-2"),
            ])
        finally:
            conn.close()
        code, body = _call_get_bytes("/api/export?period=today", self._db_path)
        self.assertEqual(code, 200)
        self.assertIn(b"source,display_source,collector,model,project", body)
        self.assertIn(b"codex,codex,,gpt-5.5,/tmp/proj", body)
        self.assertIn(b"codex,claude_mem,claude-mem,gpt-5.6-luna,/tmp/claude-mem", body)

    def test_export_escapes_formula_like_text(self):
        conn = db.get_conn(self._db_path)
        try:
            db.insert_records(conn, [
                UsageRecord(ts=int(time.time()), source="codex", model="=1+1",
                            project="+formula", input_tokens=1, total_tokens=1,
                            dedup_key="formula-export"),
            ])
        finally:
            conn.close()
        _code, body = _call_get_bytes("/api/export?period=today", self._db_path)
        self.assertIn(b"'=1+1,'+formula", body)

        self.assertEqual(server._safe_csv_text("\t=1+1"), "'\t=1+1")

    def test_rejects_untrusted_host_before_reading_api(self):
        code, body = _call_get(
            "/api/summary", self._db_path, headers={"Host": "attacker.example:8787"}
        )
        self.assertEqual(code, 403)
        self.assertEqual(body["error"], "invalid host")

    def test_rejects_remote_client_before_reading_api(self):
        code, body = _call_get("/api/summary", self._db_path, client_host="8.8.8.8")
        self.assertEqual(code, 403)
        self.assertEqual(body["error"], "local requests only")

    def test_rejects_untrusted_host_before_action(self):
        code, body = _call_post(
            "/api/ingest", self._db_path, {},
            headers={"Host": "attacker.example:8787", "X-Tokenstat-Action": "ingest"},
        )
        self.assertEqual(code, 403)
        self.assertEqual(body["error"], "invalid host")

    def test_default_http_port_accepts_host_without_port(self):
        with patch("tokenstat.server.config.PORT", 80):
            code, _body = _call_get("/api/health", self._db_path, headers={"Host": "localhost"})
        self.assertEqual(code, 200)

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


class TestRuntimeHealthIssues(unittest.TestCase):
    """/api/audit、/api/health 应把后台核对出错、备份太久没做也计入健康判定，
    不用等某个来源连续好几天没数据才报警。"""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self._db_path = self._tmpdir.name + "/test.db"
        conn = db.get_conn(self._db_path)
        db.init_db(conn)
        conn.close()
        self._data_dir = Path(self._tmpdir.name) / "data"
        self._data_dir.mkdir()

    def tearDown(self):
        self._tmpdir.cleanup()

    def test_ingest_last_error_surfaces_as_warn_issue(self):
        with patch("tokenstat.server.config.DATA_DIR", self._data_dir), \
             patch.dict(server._INGEST_RUNTIME, {"last_error": "database is locked"}):
            code, body = _call_get("/api/audit", self._db_path)
        self.assertEqual(code, 200)
        self.assertEqual(body["status"], "warn")
        self.assertTrue(any("后台核对出错" in i["message"] for i in body["issues"]))

    def test_ingest_last_error_also_surfaces_on_health_endpoint(self):
        with patch("tokenstat.server.config.DATA_DIR", self._data_dir), \
             patch.dict(server._INGEST_RUNTIME, {"last_error": "boom"}):
            code, body = _call_get("/api/health", self._db_path)
        self.assertEqual(code, 200)
        self.assertEqual(body["status"], "warn")
        self.assertTrue(any("后台核对出错" in i["message"] for i in body["issues"]))

    def test_no_backup_ever_warns(self):
        with patch("tokenstat.server.config.DATA_DIR", self._data_dir):
            code, body = _call_get("/api/audit", self._db_path)
        self.assertEqual(code, 200)
        self.assertEqual(body["status"], "warn")
        self.assertTrue(any("还没做过数据库备份" in i["message"] for i in body["issues"]))

    def test_recent_backup_does_not_warn(self):
        backups = self._data_dir / "backups"
        backups.mkdir()
        (backups / "tokenstat-recent.db").write_bytes(b"x")
        with patch("tokenstat.server.config.DATA_DIR", self._data_dir):
            code, body = _call_get("/api/audit", self._db_path)
        self.assertEqual(code, 200)
        self.assertFalse(any("备份" in i["message"] for i in body["issues"]))

    def test_old_backup_warns(self):
        backups = self._data_dir / "backups"
        backups.mkdir()
        old_file = backups / "tokenstat-old.db"
        old_file.write_bytes(b"x")
        old_ts = time.time() - (config.BACKUP_STALE_DAYS + 1) * 86400
        os.utime(old_file, (old_ts, old_ts))
        with patch("tokenstat.server.config.DATA_DIR", self._data_dir):
            code, body = _call_get("/api/audit", self._db_path)
        self.assertEqual(code, 200)
        self.assertEqual(body["status"], "warn")
        self.assertTrue(any("天没备份" in i["message"] for i in body["issues"]))


class TestIngestToApi(unittest.TestCase):
    """原始日志经 run_once 入库后，接口仍保持 claude-mem 拆分且不重复计数。"""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        root = Path(self._tmpdir.name)
        self._db_path = str(root / "data" / "test.db")
        self._sessions = root / "sessions"
        self._sessions.mkdir()
        self._usage_dir = root / "usage"
        self._usage_dir.mkdir()
        self._grok_observer_log = root / "claude-mem-grok.jsonl"
        self._config_patch = patch.multiple(
            "tokenstat.config",
            DATA_DIR=root / "data",
            DB_PATH=Path(self._db_path),
            CLAUDE_PROJECTS_DIR=root / "claude",
            CODEX_SESSION_DIRS=(self._sessions, root / "archived_sessions"),
            CLAUDE_MEM_CODEX_USAGE_DIR=self._usage_dir,
            OPENCODE_DB_PATH=root / "opencode.db",
            OPENCLAW_SESSION_DIR=root / "openclaw",
            OPENCLAW_AGENTS_DIR=root / "openclaw-agents",
            HERMES_STATE_DB=root / "hermes.db",
            GROK_LOG_PATH=root / "grok.jsonl",
            CLAUDE_MEM_GROK_LOG_PATH=self._grok_observer_log,
        )
        self._config_patch.start()

    def tearDown(self):
        self._config_patch.stop()
        self._tmpdir.cleanup()

    def test_raw_logs_to_api_preserve_display_totals_and_skip_bad_line(self):
        direct = self._sessions / "rollout-direct.jsonl"
        direct.write_text(
            "\n".join([
                json.dumps({"type": "session_meta", "payload": {"id": "direct-session", "cwd": "/tmp/direct"}}),
                "{not valid json",
                json.dumps({"type": "turn_context", "payload": {"model": "gpt-5.6-luna", "cwd": "/tmp/direct"}}),
                json.dumps({
                    "type": "event_msg", "timestamp": "2026-07-27T10:00:00Z",
                    "payload": {"type": "token_count", "info": {"total_token_usage": {
                        "input_tokens": 600, "cached_input_tokens": 200,
                        "output_tokens": 400, "reasoning_output_tokens": 0, "total_tokens": 1000,
                    }}},
                }),
            ]) + "\n",
            encoding="utf-8",
        )
        (self._usage_dir / "codex-usage-2026-07-27.jsonl").write_text(
            json.dumps({
                "type": "claude_mem.codex_usage", "schema_version": 1,
                "timestamp": "2026-07-27T10:01:00Z", "event_id": "mem-one",
                "model": "gpt-5.6-luna", "project": "/tmp/claude-mem", "session_id": "mem-session",
                "usage": {
                    "input_tokens": 100, "cached_input_tokens": 25, "cache_write_input_tokens": 0,
                    "output_tokens": 20, "reasoning_output_tokens": 10,
                },
            }) + "\n",
            encoding="utf-8",
        )
        self._grok_observer_log.write_text(
            "\n".join([
                json.dumps({
                    "ts": "2026-07-27T10:02:00Z", "sid": "mem-grok-session",
                    "msg": "model changed", "ctx": {"model": "grok-4.6"},
                }),
                json.dumps({
                    "ts": "2026-07-27T10:02:01Z", "sid": "mem-grok-session",
                    "msg": "session created", "ctx": {
                        "cwd": "/tmp/.claude-mem/observer-sessions/grok-test",
                    },
                }),
                json.dumps({
                    "ts": "2026-07-27T10:02:05Z", "sid": "mem-grok-session",
                    "msg": "shell.turn.inference_done", "ctx": {
                        "loop_index": 0, "prompt_tokens": 900,
                        "cached_prompt_tokens": 200, "completion_tokens": 100,
                        "reasoning_tokens": 50,
                    },
                }),
            ]) + "\n",
            encoding="utf-8",
        )

        with patch("tokenstat.ingest.codex_parser.read_default_model", return_value="gpt-5.6-luna"):
            self.assertEqual(ingest.run_once()["records_added"], 3)
            self.assertEqual(ingest.run_once()["records_added"], 0)

        with patch("tokenstat.aggregate._today_local", return_value=date(2026, 7, 27)):
            summary_code, summary = _call_get("/api/summary", self._db_path)
            daily_code, daily = _call_get("/api/daily?days=1", self._db_path)
            breakdown_code, breakdown = _call_get("/api/breakdown?period=today", self._db_path)
            export_code, exported = _call_get_bytes("/api/export?period=today", self._db_path)

        self.assertEqual((summary_code, daily_code, breakdown_code, export_code), (200, 200, 200, 200))
        today = summary["periods"]["today"]
        self.assertEqual(today["total"], 2120)
        self.assertEqual(today["by_source"]["codex"]["total"], 1120)
        self.assertEqual(today["by_source"]["grok"]["total"], 1000)
        self.assertEqual(today["by_display_source"]["codex"]["total"], 1000)
        self.assertEqual(today["by_display_source"]["claude_mem"]["total"], 1120)
        self.assertEqual(sum(row["total"] for row in today["by_display_source"].values()), today["total"])
        self.assertEqual(daily["days"][0]["total"], 2120)
        self.assertEqual(daily["days"][0]["codex"], 1000)
        self.assertEqual(daily["days"][0]["claude_mem"], 1120)
        self.assertEqual(breakdown["total_tokens"], 2120)
        self.assertEqual(
            {(row["collector"], row["total"]) for row in breakdown["by_model"]},
            {(None, 1000), ("claude-mem", 120), ("claude-mem", 1000)},
        )
        self.assertIn(b"codex,codex,,gpt-5.6-luna,/tmp/direct", exported)
        self.assertIn(b"codex,claude_mem,claude-mem,gpt-5.6-luna,/tmp/claude-mem", exported)
        self.assertIn(b"grok,claude_mem,claude-mem,grok-4.6,claude-mem", exported)


class TestEnsureUsableDb(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.data = self.root / "data"
        self.data.mkdir()
        (self.data / "backups").mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def test_restores_latest_backup_when_live_db_empty(self):
        live = self.data / "tokenstat.db"
        backup = self.data / "backups" / "tokenstat-20260906-120000-000000.db"
        conn = db.get_conn(backup)
        db.init_db(conn)
        db.insert_records(conn, [
            UsageRecord(ts=1, source="codex", model="gpt-5.5", project="/p",
                        total_tokens=9, dedup_key="seed"),
        ])
        conn.close()
        live.write_bytes(b"")
        with patch("tokenstat.server.config.DATA_DIR", self.data):
            with patch("tokenstat.server.config.DB_PATH", live):
                self.assertTrue(server._ensure_usable_db())
        check = db.get_conn(live)
        try:
            n = check.execute("SELECT COUNT(*) FROM usage_events").fetchone()[0]
        finally:
            check.close()
        self.assertEqual(n, 1)

    def test_refuses_empty_ledger_when_launch_agent_exists(self):
        live = self.data / "tokenstat.db"
        agents = self.root / "Library" / "LaunchAgents"
        agents.mkdir(parents=True)
        (agents / "com.yunxin.tokenstat.plist").write_text("x")
        with patch("tokenstat.server.config.DATA_DIR", self.data):
            with patch("tokenstat.server.config.DB_PATH", live):
                with patch("tokenstat.server.Path.home", return_value=self.root):
                    self.assertFalse(server._ensure_usable_db())


if __name__ == "__main__":
    unittest.main()
