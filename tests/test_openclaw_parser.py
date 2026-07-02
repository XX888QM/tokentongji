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
        self.assertEqual(r.project, "openclaw-weixin")  # 从 sessionKey 第三段
        self.assertEqual(r.dedup_key, "openclaw:run-1:3")

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
        self.assertEqual(r.dedup_key, "openclaw-v3:m1")
        self.assertEqual(r.ts, 1_700_000_000)  # ms → s

    def test_non_assistant_skipped(self):
        ctx = {}
        msg = self._msg()
        msg["message"]["role"] = "user"
        self.assertIsNone(openclaw.parse_v3_record(msg, "/f", 0, ctx))

    def test_zero_total_skipped(self):
        ctx = {}
        self.assertIsNone(openclaw.parse_v3_record(self._msg(total=0), "/f", 0, ctx))


if __name__ == "__main__":
    unittest.main()
