"""openclaw 解析器测试：trajectory(model.completed) 与 v3 session 两种格式。"""
import unittest

from tokenstat.parsers import openclaw


class TestTrajectoryFormat(unittest.TestCase):
    def _event(self, **over):
        obj = {
            "type": "model.completed",
            "ts": "2026-05-01T10:00:00Z",
            "runId": "run-1",
            "seq": 3,
            "sessionId": "sess-1",
            "modelId": "claude-opus-4-7",
            "sessionKey": "agent:main:openclaw-weixin:direct:abc",
            "data": {
                "usage": {"input": 100, "output": 50, "cacheRead": 10, "total": 160},
                "promptCache": {"lastCallUsage": {"cacheWrite": 7}},
            },
        }
        obj.update(over)
        return obj

    def test_basic(self):
        r = openclaw.parse_record(self._event(), "/f.trajectory.jsonl", 0)
        self.assertIsNotNone(r)
        self.assertEqual(r.source, "openclaw")
        self.assertEqual(r.input_tokens, 100)
        self.assertEqual(r.output_tokens, 50)
        self.assertEqual(r.cache_read_tokens, 10)
        self.assertEqual(r.cache_creation_tokens, 7)   # 来自 promptCache.lastCallUsage
        self.assertEqual(r.total_tokens, 160 + 7)      # total_raw + cacheWrite
        self.assertIsNone(r.request_prompt_tokens)
        self.assertEqual(r.project, "openclaw-weixin")  # 从 sessionKey 第三段
        self.assertEqual(r.dedup_key, "openclaw:run-1:3")

    def test_subagent_session_key_uses_agent_id(self):
        r = openclaw.parse_record(
            self._event(sessionKey="agent:devops-automator:main:openclaw-weixin"),
            "/f.trajectory.jsonl",
            0,
        )
        self.assertEqual(r.project, "devops-automator")

    def test_wrong_type_skipped(self):
        self.assertIsNone(openclaw.parse_record({"type": "other"}, "/f", 0))

    def test_aborted_skipped(self):
        ev = self._event()
        ev["data"]["aborted"] = True
        self.assertIsNone(openclaw.parse_record(ev, "/f", 0))

    def test_zero_total_skipped(self):
        ev = self._event()
        ev["data"] = {"usage": {"input": 0, "output": 0, "cacheRead": 0, "total": 0}}
        self.assertIsNone(openclaw.parse_record(ev, "/f", 0))

    def test_bad_timestamp_skipped(self):
        self.assertIsNone(openclaw.parse_record(self._event(ts=""), "/f", 0))

    def test_sessionkey_fallback(self):
        r = openclaw.parse_record(self._event(sessionKey=""), "/f", 0)
        self.assertEqual(r.project, "openclaw")

    def test_sessionkey_new_format(self):
        r = openclaw.parse_record(
            self._event(sessionKey="openclaw-weixin:direct/group"), "/f", 0
        )
        self.assertEqual(r.project, "openclaw-weixin")


class TestV3Format(unittest.TestCase):
    def _session(self, sid="sid-1", cwd="/proj"):
        return {"type": "session", "id": sid, "cwd": cwd}

    def _msg(self, mid="m1", inp=100, out=50, total=150, ts_ms=1_700_000_000_000):
        return {
            "type": "message",
            "id": mid,
            "message": {
                "role": "assistant",
                "model": "claude-opus-4-7",
                "usage": {"input": inp, "output": out, "cacheRead": 0,
                          "cacheWrite": 0, "totalTokens": total},
                "timestamp": ts_ms,
            },
        }

    def test_session_sets_ctx_then_message(self):
        ctx = {}
        self.assertIsNone(openclaw.parse_v3_record(self._session("sX", "/home/x"), "/f", 0, ctx))
        self.assertEqual(ctx["session_id"], "sX")
        r = openclaw.parse_v3_record(self._msg(), "/f", 1, ctx)
        self.assertIsNotNone(r)
        self.assertEqual(r.session_id, "sX")
        self.assertEqual(r.project, "/home/x")
        self.assertEqual(r.total_tokens, 150)
        self.assertEqual(r.request_prompt_tokens, 100)
        self.assertEqual(r.dedup_key, "openclaw-v3:m1")
        self.assertEqual(r.ts, 1_700_000_000)  # ms → s

    def test_session_key_overrides_cwd_as_project(self):
        ctx = {}
        session = self._session("sX", "/home/x")
        session["sessionKey"] = "agent:devops-automator:main"
        self.assertIsNone(openclaw.parse_v3_record(session, "/f", 0, ctx))
        r = openclaw.parse_v3_record(self._msg(), "/f", 1, ctx)
        self.assertEqual(r.project, "devops-automator")

    def test_non_assistant_skipped(self):
        ctx = {}
        msg = self._msg()
        msg["message"]["role"] = "user"
        self.assertIsNone(openclaw.parse_v3_record(msg, "/f", 0, ctx))

    def test_zero_total_skipped(self):
        ctx = {}
        self.assertIsNone(openclaw.parse_v3_record(self._msg(total=0), "/f", 0, ctx))

    def test_non_string_model_skipped(self):
        ctx = {}
        msg = self._msg()
        msg["message"]["model"] = 42
        rec = openclaw.parse_v3_record(msg, "/f", 0, ctx)
        self.assertEqual(rec.model, "unknown")


class TestSqliteFetch(unittest.TestCase):
    def setUp(self):
        import sqlite3
        import tempfile
        from pathlib import Path

        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "openclaw-agent.sqlite"
        self.conn = sqlite3.connect(str(self.db_path))
        self.conn.execute(
            "CREATE TABLE transcript_events (session_id TEXT, seq INTEGER, event_json TEXT, created_at INTEGER, PRIMARY KEY(session_id, seq))"
        )
        self.conn.execute(
            "CREATE TABLE session_windows (session_id TEXT, session_key TEXT)"
        )

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def _add(self, sid, seq, obj, key="openclaw-weixin:direct"):
        import json

        self.conn.execute(
            "INSERT INTO transcript_events VALUES (?,?,?,?)",
            (sid, seq, json.dumps(obj), 1_700_000_000_000 + seq),
        )
        self.conn.execute(
            "INSERT OR IGNORE INTO session_windows VALUES (?,?)",
            (sid, key),
        )
        self.conn.commit()

    def test_fetches_assistant_usage_and_skips_zero(self):
        sid = "sid-sql"
        self._add(sid, 0, {"type": "session", "id": sid, "cwd": "/workspace"})
        self._add(
            sid,
            1,
            {
                "type": "message",
                "id": "m-keep",
                "message": {
                    "role": "assistant",
                    "model": "grok-4.6",
                    "usage": {
                        "input": 10,
                        "output": 5,
                        "cacheRead": 2,
                        "cacheWrite": 0,
                        "totalTokens": 17,
                    },
                    "timestamp": 1_700_000_001_000,
                },
            },
        )
        self._add(
            sid,
            2,
            {
                "type": "message",
                "id": "m-zero",
                "message": {
                    "role": "assistant",
                    "model": "grok-4.6",
                    "usage": {"input": 0, "output": 0, "totalTokens": 0},
                    "timestamp": 1_700_000_002_000,
                },
            },
        )
        recs = openclaw.fetch_records(self.db_path)
        self.assertEqual(len(recs), 1)
        self.assertEqual(recs[0].dedup_key, "openclaw-v3:m-keep")
        self.assertEqual(recs[0].project, "openclaw-weixin")
        self.assertEqual(recs[0].session_id, sid)
        self.assertEqual(recs[0].total_tokens, 17)

    def test_copied_window_same_message_id_dedups(self):
        msg = {
            "type": "message",
            "id": "copied",
            "message": {
                "role": "assistant",
                "model": "grok-4.6",
                "usage": {"input": 1, "output": 1, "totalTokens": 2},
                "timestamp": 1_700_000_003_000,
            },
        }
        self._add("win-a", 0, msg, "main")
        self._add("win-b", 0, msg, "main")
        recs = openclaw.fetch_records(self.db_path)
        self.assertEqual(len(recs), 2)
        self.assertEqual({r.dedup_key for r in recs}, {"openclaw-v3:copied"})

    def test_missing_db_returns_empty(self):
        from pathlib import Path

        self.assertEqual(openclaw.fetch_records(Path(self.tmp.name) / "nope.sqlite"), [])

    def test_non_object_event_json_skipped(self):
        sid = "sid-bad"
        self._add(sid, 0, {"type": "session", "id": sid, "cwd": "/workspace"})
        self.conn.execute(
            "INSERT INTO transcript_events VALUES (?,?,?,?)",
            (sid, 3, "null", 3),
        )
        self.conn.execute(
            "INSERT INTO transcript_events VALUES (?,?,?,?)",
            (sid, 4, "[]", 4),
        )
        self._add(
            sid,
            5,
            {
                "type": "message",
                "id": "after-junk",
                "message": {
                    "role": "assistant",
                    "model": "grok-4.6",
                    "usage": {"input": 3, "output": 1, "totalTokens": 4},
                    "timestamp": 1_700_000_005_000,
                },
            },
        )
        recs = openclaw.fetch_records(self.db_path)
        keys = {r.dedup_key for r in recs}
        self.assertIn("openclaw-v3:after-junk", keys)

    def test_bad_record_does_not_block_following_usage(self):
        sid = "sid-bad-field"
        self._add(sid, 0, {"type": "session", "id": sid, "cwd": "/workspace"})
        bad = {
            "type": "message",
            "id": "bad-model",
            "message": {
                "role": "assistant",
                "model": 42,
                "usage": {"input": 1, "output": 1, "totalTokens": 2},
                "timestamp": 1_700_000_001_000,
            },
        }
        self._add(sid, 1, bad)
        self._add(
            sid,
            2,
            {
                "type": "message",
                "id": "after-bad",
                "message": {
                    "role": "assistant",
                    "model": "grok-4.6",
                    "usage": {"input": 5, "output": 2, "totalTokens": 7},
                    "timestamp": 1_700_000_002_000,
                },
            },
        )

        recs = openclaw.fetch_records(self.db_path)
        self.assertEqual(
            [r.dedup_key for r in recs], ["openclaw-v3:bad-model", "openclaw-v3:after-bad"]
        )


if __name__ == "__main__":
    unittest.main()
