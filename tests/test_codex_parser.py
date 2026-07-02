import unittest

from tokenstat.parsers import codex
from tokenstat.parsers.codex import CodexState


def _session_meta(cwd, sid="sess-uuid"):
    return {"type": "session_meta", "payload": {"id": sid, "cwd": cwd}}


def _turn_context(model, cwd):
    return {"type": "turn_context", "payload": {"model": model, "cwd": cwd}}


def _token_count(total, input_t, cached, output, reasoning=0, ts="2026-05-01T10:00:00Z"):
    return {
        "type": "event_msg",
        "timestamp": ts,
        "payload": {
            "type": "token_count",
            "info": {
                "total_token_usage": {
                    "input_tokens": input_t,
                    "cached_input_tokens": cached,
                    "output_tokens": output,
                    "reasoning_output_tokens": reasoning,
                    "total_tokens": total,
                }
            },
            "rate_limits": {"some": "thing"},
        },
    }


def _heartbeat():
    return {"type": "event_msg", "payload": {"type": "token_count", "info": None, "rate_limits": {}}}


class TestCodexParser(unittest.TestCase):
    def setUp(self):
        self.state = CodexState.from_ctx({}, default_model="gpt-5.5")

    def test_carry_forward_and_first_delta(self):
        self.assertIsNone(codex.process_record(_session_meta("/meta/cwd"), "/f", 0, self.state))
        self.assertIsNone(codex.process_record(_turn_context("gpt-5.4", "/real/cwd"), "/f", 1, self.state))
        # 首条 token_count：total=1000, input=600(含 cached 200), output=400
        rec = codex.process_record(_token_count(1000, 600, 200, 400), "/f", 2, self.state)
        self.assertIsNotNone(rec)
        self.assertEqual(rec.model, "gpt-5.4")           # 来自 turn_context
        self.assertEqual(rec.project, "/real/cwd")        # turn_context.cwd 覆盖 meta.cwd
        self.assertEqual(rec.cache_read_tokens, 200)      # cached
        self.assertEqual(rec.input_tokens, 400)           # fresh = 600 - 200
        self.assertEqual(rec.output_tokens, 400)
        self.assertEqual(rec.total_tokens, 400 + 200 + 400)
        self.assertEqual(rec.dedup_key, "f#2")  # 基于文件名(basename)，非完整路径

    def test_dedup_key_path_independent(self):
        # 同一文件被归档到另一目录后，dedup_key 必须相同，否则会重复计数
        s1 = CodexState.from_ctx({}, default_model="gpt-5.5")
        s2 = CodexState.from_ctx({}, default_model="gpt-5.5")
        codex.process_record(_turn_context("gpt-5.4", "/c"), "/sessions/roll.jsonl", 0, s1)
        r1 = codex.process_record(_token_count(1000, 600, 200, 400), "/sessions/roll.jsonl", 88, s1)
        codex.process_record(_turn_context("gpt-5.4", "/c"), "/archived_sessions/roll.jsonl", 0, s2)
        r2 = codex.process_record(_token_count(1000, 600, 200, 400), "/archived_sessions/roll.jsonl", 88, s2)
        self.assertEqual(r1.dedup_key, r2.dedup_key)  # 同名文件不同路径 → 同键 → DB 层去重

    def test_cumulative_delta(self):
        codex.process_record(_turn_context("gpt-5.4", "/c"), "/f", 0, self.state)
        codex.process_record(_token_count(1000, 600, 200, 400), "/f", 1, self.state)
        # 第二条累积：total=1500, input=800(cached 300), output=700
        rec = codex.process_record(_token_count(1500, 800, 300, 700), "/f", 2, self.state)
        self.assertEqual(rec.output_tokens, 700 - 400)    # delta output = 300
        self.assertEqual(rec.cache_read_tokens, 300 - 200)  # delta cached = 100
        self.assertEqual(rec.input_tokens, (800 - 600) - (300 - 200))  # fresh delta = 100

    def test_duplicate_snapshot_skipped(self):
        codex.process_record(_turn_context("gpt-5.4", "/c"), "/f", 0, self.state)
        codex.process_record(_token_count(1000, 600, 200, 400), "/f", 1, self.state)
        # 成对重发：同样的 total → 零增量 → 跳过
        dup = codex.process_record(_token_count(1000, 600, 200, 400), "/f", 2, self.state)
        self.assertIsNone(dup)

    def test_heartbeat_skipped(self):
        codex.process_record(_turn_context("gpt-5.4", "/c"), "/f", 0, self.state)
        self.assertIsNone(codex.process_record(_heartbeat(), "/f", 1, self.state))

    def test_model_fallback_default(self):
        # 没有 turn_context，model 回退 default
        codex.process_record(_session_meta("/c"), "/f", 0, self.state)
        rec = codex.process_record(_token_count(100, 60, 0, 40), "/f", 1, self.state)
        self.assertEqual(rec.model, "gpt-5.5")

    def test_ctx_roundtrip_cross_batch(self):
        codex.process_record(_turn_context("gpt-5.4", "/c"), "/f", 0, self.state)
        codex.process_record(_token_count(1000, 600, 200, 400), "/f", 1, self.state)
        ctx = self.state.to_ctx()
        # 模拟下一批次：从持久化 ctx 恢复
        s2 = CodexState.from_ctx(ctx, default_model="gpt-5.5")
        self.assertEqual(s2.cur_model, "gpt-5.4")
        self.assertEqual(s2.prev_total["total_tokens"], 1000)
        rec = codex.process_record(_token_count(1500, 800, 200, 700), "/f", 2, s2)
        self.assertEqual(rec.output_tokens, 300)  # 差分基于恢复的 prev_total

    def test_sum_of_deltas_equals_final_total(self):
        # 验证差分法总量正确：多条累积快照的增量和 == 最后 total
        codex.process_record(_turn_context("gpt-5.4", "/c"), "/f", 0, self.state)
        snaps = [(300, 200, 0, 100), (800, 500, 100, 300), (1500, 900, 200, 600)]
        recs = []
        for i, (tot, inp, cached, out) in enumerate(snaps, start=1):
            r = codex.process_record(_token_count(tot, inp, cached, out), "/f", i, self.state)
            if r:
                recs.append(r)
        total_in = sum(r.input_tokens for r in recs)
        total_cached = sum(r.cache_read_tokens for r in recs)
        total_out = sum(r.output_tokens for r in recs)
        # 最后快照 input=900(cached 200) output=600 → fresh=700
        self.assertEqual(total_in, 700)
        self.assertEqual(total_cached, 200)
        self.assertEqual(total_out, 600)


if __name__ == "__main__":
    unittest.main()
