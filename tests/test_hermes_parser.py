"""Hermes 解析器测试：sessions 表全量重扫 + reasoning 子集口径 + 子会话分类。"""
import sqlite3
import tempfile
import unittest
from pathlib import Path

from tokenstat.parsers import hermes


def _make_db(path: Path, rows):
    """建一个最小 sessions 表；rows 为 dict 列表，缺的列用 schema 默认值。"""
    conn = sqlite3.connect(str(path))
    conn.execute(
        "CREATE TABLE sessions (id TEXT PRIMARY KEY, model TEXT, cwd TEXT, "
        "started_at REAL, parent_session_id TEXT, "
        "input_tokens INTEGER DEFAULT 0, output_tokens INTEGER DEFAULT 0, "
        "cache_read_tokens INTEGER DEFAULT 0, cache_write_tokens INTEGER DEFAULT 0, "
        "reasoning_tokens INTEGER DEFAULT 0)"
    )
    for r in rows:
        conn.execute(
            "INSERT INTO sessions (id, model, cwd, started_at, parent_session_id, "
            "input_tokens, output_tokens, cache_read_tokens, cache_write_tokens, reasoning_tokens) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (r.get("id"), r.get("model"), r.get("cwd"), r.get("started_at"),
             r.get("parent_session_id"), r.get("input_tokens", 0), r.get("output_tokens", 0),
             r.get("cache_read_tokens", 0), r.get("cache_write_tokens", 0),
             r.get("reasoning_tokens", 0)),
        )
    conn.commit()
    conn.close()


class TestHermesParser(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "state.db"

    def tearDown(self):
        self.tmp.cleanup()

    def test_reasoning_is_output_subset_and_not_added_twice(self):
        _make_db(self.db, [{
            "id": "s1", "model": "gpt-5.5", "cwd": "/proj", "started_at": 1700000000,
            "input_tokens": 100, "output_tokens": 50, "cache_read_tokens": 10,
            "cache_write_tokens": 5, "reasoning_tokens": 20,
        }])
        recs = hermes.fetch_records(self.db)
        self.assertEqual(len(recs), 1)
        r = recs[0]
        self.assertEqual(r.source, "hermes")
        self.assertEqual(r.input_tokens, 100)
        self.assertEqual(r.output_tokens, 50)
        self.assertEqual(r.reasoning_tokens, 20)
        self.assertEqual(r.cache_read_tokens, 10)
        self.assertEqual(r.cache_creation_tokens, 5)
        self.assertEqual(r.total_tokens, 100 + 50 + 10 + 5)
        self.assertEqual(r.project, "/proj")
        self.assertEqual(r.session_id, "s1")
        self.assertEqual(r.dedup_key, "hermes:s1")
        self.assertEqual(r.category, "main")

    def test_parent_session_id_marks_subagent(self):
        _make_db(self.db, [{
            "id": "child1", "model": "gpt-5.4", "cwd": "/p", "started_at": 1700000000,
            "parent_session_id": "parent1", "input_tokens": 10, "output_tokens": 5,
        }])
        r = hermes.fetch_records(self.db)[0]
        self.assertEqual(r.category, "subagent")

    def test_zero_token_session_skipped(self):
        _make_db(self.db, [
            {"id": "empty", "model": "gpt-5.5", "cwd": "/p", "started_at": 1700000000},
            {"id": "real", "model": "gpt-5.5", "cwd": "/p", "started_at": 1700000001,
             "input_tokens": 5, "output_tokens": 3},
        ])
        recs = hermes.fetch_records(self.db)
        self.assertEqual([r.session_id for r in recs], ["real"])

    def test_missing_started_at_skipped(self):
        _make_db(self.db, [{"id": "s1", "model": "gpt-5.5", "input_tokens": 10, "output_tokens": 5}])
        self.assertEqual(hermes.fetch_records(self.db), [])

    def test_missing_cwd_falls_back_to_hermes(self):
        _make_db(self.db, [{"id": "s1", "model": "gpt-5.5", "started_at": 1700000000,
                            "input_tokens": 10, "output_tokens": 5}])
        r = hermes.fetch_records(self.db)[0]
        self.assertEqual(r.project, "hermes")

    def test_missing_db_returns_empty(self):
        self.assertEqual(hermes.fetch_records(Path(self.tmp.name) / "nope.db"), [])

    def test_source_file_is_db_path(self):
        _make_db(self.db, [{"id": "s1", "model": "gpt-5.5", "started_at": 1700000000,
                            "input_tokens": 10, "output_tokens": 5}])
        r = hermes.fetch_records(self.db)[0]
        self.assertEqual(r.source_file, str(self.db))


if __name__ == "__main__":
    unittest.main()
