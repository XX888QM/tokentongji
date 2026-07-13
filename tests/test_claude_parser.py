import unittest

from tokenstat.parsers import claude


def _assistant(msg_id, cwd, model="claude-opus-4-7", out=100, sidechain=False):
    return {
        "type": "assistant",
        "timestamp": "2026-05-01T05:29:41.280Z",
        "cwd": cwd,
        "sessionId": "sess-1",
        "isSidechain": sidechain,
        "message": {
            "id": msg_id,
            "model": model,
            "usage": {
                "input_tokens": 6,
                "output_tokens": out,
                "cache_creation_input_tokens": 26070,
                "cache_read_input_tokens": 18186,
            },
        },
    }


class TestClaudeParser(unittest.TestCase):
    def test_basic_assistant(self):
        rec = claude.parse_record(_assistant("msg_1", "/Users/yunxin/Desktop/proj"), "/f.jsonl", 0)
        self.assertIsNotNone(rec)
        self.assertEqual(rec.source, "claude")
        self.assertEqual(rec.input_tokens, 6)
        self.assertEqual(rec.output_tokens, 100)
        self.assertEqual(rec.cache_creation_tokens, 26070)
        self.assertEqual(rec.cache_read_tokens, 18186)
        self.assertEqual(rec.total_tokens, 6 + 100 + 26070 + 18186)
        self.assertEqual(rec.project, "/Users/yunxin/Desktop/proj")
        self.assertEqual(rec.dedup_key, "msg_1")
        self.assertEqual(rec.category, "main")

    def test_skip_synthetic(self):
        obj = _assistant("msg_2", "/x", model="<synthetic>")
        self.assertIsNone(claude.parse_record(obj, "/f.jsonl", 0))

    def test_skip_non_assistant(self):
        self.assertIsNone(claude.parse_record({"type": "user"}, "/f", 0))
        self.assertIsNone(claude.parse_record({"type": "summary"}, "/f", 0))

    def test_skip_missing_usage(self):
        obj = {"type": "assistant", "message": {"id": "x", "model": "m"}}
        self.assertIsNone(claude.parse_record(obj, "/f", 0))

    def test_skip_missing_msg_id(self):
        obj = _assistant("", "/x")
        obj["message"]["id"] = ""
        self.assertIsNone(claude.parse_record(obj, "/f", 0))

    def test_category_observer(self):
        rec = claude.parse_record(
            _assistant("m", "/Users/yunxin/.claude-mem/observer-sessions"), "/f", 0
        )
        self.assertEqual(rec.category, "observer")

    def test_category_subagent_by_sidechain(self):
        rec = claude.parse_record(_assistant("m", "/proj", sidechain=True), "/f", 0)
        self.assertEqual(rec.category, "subagent")

    def test_category_subagent_by_path(self):
        rec = claude.parse_record(
            _assistant("m", "/proj"), "/a/subagents/workflows/wf/agent-1.jsonl", 0
        )
        self.assertEqual(rec.category, "subagent")

    def test_fallback_iterations_are_separate_model_records(self):
        obj = _assistant("fallback", "/proj", model="claude-opus-4-8", out=90)
        obj["message"]["usage"]["iterations"] = [
            {
                "model": "claude-fable-5",
                "input_tokens": 2,
                "output_tokens": 40,
                "cache_creation_input_tokens": 10,
                "cache_read_input_tokens": 100,
            },
            {
                "model": "claude-opus-4-8",
                "input_tokens": 3,
                "output_tokens": 90,
                "cache_creation_input_tokens": 20,
                "cache_read_input_tokens": 200,
            },
        ]

        recs = claude.parse_records(obj, "/f.jsonl", 0)

        self.assertEqual([r.model for r in recs], ["claude-fable-5", "claude-opus-4-8"])
        self.assertEqual([r.total_tokens for r in recs], [152, 313])
        self.assertEqual(
            [r.dedup_key for r in recs],
            ["fallback:iteration:0", "fallback:iteration:1"],
        )


if __name__ == "__main__":
    unittest.main()
