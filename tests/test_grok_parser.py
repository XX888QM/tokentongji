"""Grok unified.jsonl 解析器测试。"""

import unittest

from tokenstat.parsers import grok


def _model_changed(sid="s1", model="grok-4.5", ts="2026-07-09T10:00:00.000Z"):
    return {
        "ts": ts,
        "src": "shell",
        "sid": sid,
        "msg": "model changed",
        "ctx": {"model": model},
    }


def _session_created(sid="s1", cwd="/Users/yunxin/Desktop/proj", ts="2026-07-09T10:00:01.000Z"):
    return {
        "ts": ts,
        "src": "shell",
        "sid": sid,
        "msg": "session created",
        "ctx": {"cwd": cwd},
    }


def _inference_done(
    sid="s1",
    prompt=1000,
    cached=400,
    comp=50,
    reason=30,
    loop=1,
    ts="2026-07-09T10:00:05.000Z",
):
    return {
        "ts": ts,
        "src": "shell",
        "sid": sid,
        "msg": "shell.turn.inference_done",
        "ctx": {
            "loop_index": loop,
            "prompt_tokens": prompt,
            "cached_prompt_tokens": cached,
            "completion_tokens": comp,
            "reasoning_tokens": reason,
            "tokens_per_sec": 10.0,
        },
    }


class TestGrokParser(unittest.TestCase):
    def test_carry_forward_model_and_cwd(self):
        state = grok.GrokState()
        self.assertIsNone(grok.process_record(_model_changed(), "/u.jsonl", 0, state))
        self.assertIsNone(grok.process_record(_session_created(), "/u.jsonl", 10, state))
        r = grok.process_record(_inference_done(), "/u.jsonl", 20, state)
        self.assertIsNotNone(r)
        self.assertEqual(r.source, "grok")
        self.assertEqual(r.model, "grok-4.5")
        self.assertEqual(r.project, "/Users/yunxin/Desktop/proj")
        self.assertEqual(r.input_tokens, 600)  # 1000 - 400
        self.assertEqual(r.cache_read_tokens, 400)
        self.assertEqual(r.output_tokens, 50)
        self.assertEqual(r.reasoning_tokens, 30)
        self.assertEqual(r.total_tokens, 600 + 400 + 50)
        self.assertEqual(r.session_id, "s1")
        self.assertEqual(r.dedup_key, "grok:s1:2026-07-09T10:00:05.000Z:1")

    def test_default_model_when_missing(self):
        state = grok.GrokState()
        r = grok.process_record(_inference_done(), "/u.jsonl", 0, state)
        self.assertEqual(r.model, "grok-4.5")
        self.assertEqual(r.project, "grok")

    def test_usage_event_can_supply_model_and_project(self):
        state = grok.GrokState()
        event = _inference_done()
        event["ctx"].update({"model": "grok-4.3", "cwd": "claude-mem"})
        r = grok.process_record(event, "/u.jsonl", 0, state)
        self.assertEqual(r.model, "grok-4.3")
        self.assertEqual(r.project, "claude-mem")

    def test_skip_non_usage_and_zero(self):
        state = grok.GrokState()
        self.assertIsNone(
            grok.process_record({"msg": "shell.turn.inference_start", "sid": "s"}, "/u", 0, state)
        )
        self.assertIsNone(
            grok.process_record(_inference_done(prompt=0, cached=0, comp=0, reason=0), "/u", 0, state)
        )

    def test_clamp_dirty_cache_and_reason(self):
        state = grok.GrokState()
        r = grok.process_record(
            _inference_done(prompt=100, cached=200, comp=10, reason=99),
            "/u",
            0,
            state,
        )
        self.assertEqual(r.cache_read_tokens, 100)
        self.assertEqual(r.input_tokens, 0)
        self.assertEqual(r.reasoning_tokens, 10)

    def test_state_roundtrip(self):
        state = grok.GrokState()
        grok.process_record(_model_changed(sid="a", model="grok-4.3"), "/u", 0, state)
        grok.process_record(_session_created(sid="a", cwd="/tmp/x"), "/u", 1, state)
        restored = grok.GrokState.from_ctx(state.to_ctx())
        r = grok.process_record(
            _inference_done(sid="a", ts="2026-07-09T11:00:00.000Z"),
            "/u",
            2,
            restored,
        )
        self.assertEqual(r.model, "grok-4.3")
        self.assertEqual(r.project, "/tmp/x")

    def test_bad_timestamp_skipped(self):
        state = grok.GrokState()
        ev = _inference_done()
        ev["ts"] = "not-a-date"
        self.assertIsNone(grok.process_record(ev, "/u", 0, state))


if __name__ == "__main__":
    unittest.main()
