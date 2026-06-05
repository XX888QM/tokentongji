import json
import tempfile
import unittest
from pathlib import Path

from tokenstat import db, ingest


def _w(path: Path, objs, mode="ab"):
    with open(path, mode) as fh:
        for o in objs:
            fh.write((json.dumps(o) + "\n").encode("utf-8"))


def _assistant(msg_id, out, model="claude-opus-4-7"):
    return {
        "type": "assistant",
        "timestamp": "2026-05-01T05:29:41.280Z",
        "cwd": "/Users/yunxin/Desktop/proj",
        "sessionId": "s1",
        "message": {
            "id": msg_id,
            "model": model,
            "usage": {
                "input_tokens": 10,
                "output_tokens": out,
                "cache_creation_input_tokens": 0,
                "cache_read_input_tokens": 0,
            },
        },
    }


def _tc(total, inp, cached, out, ts="2026-05-01T10:00:00Z"):
    return {
        "type": "event_msg",
        "timestamp": ts,
        "payload": {
            "type": "token_count",
            "info": {
                "total_token_usage": {
                    "input_tokens": inp,
                    "cached_input_tokens": cached,
                    "output_tokens": out,
                    "reasoning_output_tokens": 0,
                    "total_tokens": total,
                }
            },
        },
    }


class TestClaudeIngest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = db.get_conn(":memory:")
        db.init_db(self.conn)

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def _rows(self):
        cur = self.conn.execute(
            "SELECT COUNT(*) c, COALESCE(SUM(output_tokens),0) s FROM usage_events"
        )
        r = cur.fetchone()
        return r["c"], r["s"]

    def test_dedup_same_msg_id_not_doubled(self):
        f = Path(self.tmp.name) / "a.jsonl"
        # 同一 msg_1 拆 2 行(usage 相同) + msg_2 一行
        _w(f, [_assistant("msg_1", 100), _assistant("msg_1", 100), _assistant("msg_2", 50)])
        ingest._ingest_file(self.conn, f, "claude", "gpt-5.5")
        count, out_sum = self._rows()
        self.assertEqual(count, 2)          # 去重后只 2 行
        self.assertEqual(out_sum, 150)      # 不是 250

    def test_streaming_takes_max_output(self):
        f = Path(self.tmp.name) / "b.jsonl"
        # 旧流式：同 msg.id output 递增
        _w(f, [_assistant("m", 39), _assistant("m", 134)])
        ingest._ingest_file(self.conn, f, "claude", "gpt-5.5")
        count, out_sum = self._rows()
        self.assertEqual(count, 1)
        self.assertEqual(out_sum, 134)      # 取最大

    def test_incremental_append_no_recount(self):
        f = Path(self.tmp.name) / "c.jsonl"
        _w(f, [_assistant("m1", 10)])
        ingest._ingest_file(self.conn, f, "claude", "gpt-5.5")
        self.assertEqual(self._rows(), (1, 10))
        # 追加新行，再次 ingest（断点续读，不重复旧行）
        _w(f, [_assistant("m2", 20)])
        ingest._ingest_file(self.conn, f, "claude", "gpt-5.5")
        self.assertEqual(self._rows(), (2, 30))

    def test_partial_last_line_not_consumed(self):
        f = Path(self.tmp.name) / "d.jsonl"
        _w(f, [_assistant("m1", 10)])
        # 写一个没有换行的残行（模拟正在写入）
        with open(f, "ab") as fh:
            fh.write(json.dumps(_assistant("m2", 20)).encode("utf-8"))
        ingest._ingest_file(self.conn, f, "claude", "gpt-5.5")
        self.assertEqual(self._rows(), (1, 10))  # 残行不消费
        # 补上换行后再 ingest
        with open(f, "ab") as fh:
            fh.write(b"\n")
        ingest._ingest_file(self.conn, f, "claude", "gpt-5.5")
        self.assertEqual(self._rows(), (2, 30))


class TestCodexIngest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = db.get_conn(":memory:")
        db.init_db(self.conn)

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def _sum(self):
        cur = self.conn.execute(
            "SELECT COUNT(*) c, COALESCE(SUM(input_tokens),0) i, "
            "COALESCE(SUM(output_tokens),0) o, COALESCE(SUM(cache_read_tokens),0) cr "
            "FROM usage_events"
        )
        r = cur.fetchone()
        return r["c"], r["i"], r["o"], r["cr"]

    def test_codex_delta_and_incremental(self):
        f = Path(self.tmp.name) / "rollout-x.jsonl"
        _w(f, [
            {"type": "session_meta", "payload": {"id": "u1", "cwd": "/meta"}},
            {"type": "turn_context", "payload": {"model": "gpt-5.4", "cwd": "/real"}},
            _tc(1000, 600, 200, 400),
        ])
        ingest._ingest_file(self.conn, f, "codex", "gpt-5.5")
        c, i, o, cr = self._sum()
        self.assertEqual(c, 1)
        self.assertEqual(i, 400)   # fresh = 600-200
        self.assertEqual(o, 400)
        self.assertEqual(cr, 200)

        # 追加累积快照，断点续读 + ctx 延续差分
        _w(f, [_tc(1500, 800, 300, 700)])
        ingest._ingest_file(self.conn, f, "codex", "gpt-5.5")
        c, i, o, cr = self._sum()
        self.assertEqual(c, 2)
        self.assertEqual(o, 700)   # 400 + 300
        self.assertEqual(cr, 300)  # 200 + 100
        self.assertEqual(i, 500)   # 400 + 100

    def test_codex_model_fallback(self):
        f = Path(self.tmp.name) / "rollout-y.jsonl"
        _w(f, [
            {"type": "session_meta", "payload": {"id": "u2", "cwd": "/c"}},
            _tc(100, 60, 0, 40),
        ])
        ingest._ingest_file(self.conn, f, "codex", "gpt-5.5")
        row = self.conn.execute("SELECT model FROM usage_events").fetchone()
        self.assertEqual(row["model"], "gpt-5.5")


if __name__ == "__main__":
    unittest.main()
