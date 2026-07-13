import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tokenstat import db, ingest
from tokenstat.models import UsageRecord


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

    def test_iterations_are_ingested_without_top_level_double_count(self):
        f = Path(self.tmp.name) / "fallback.jsonl"
        obj = _assistant("fallback", 90, model="claude-opus-4-8")
        obj["message"]["usage"]["iterations"] = [
            {"model": "claude-fable-5", "input_tokens": 2, "output_tokens": 40,
             "cache_creation_input_tokens": 10, "cache_read_input_tokens": 100},
            {"model": "claude-opus-4-8", "input_tokens": 3, "output_tokens": 90,
             "cache_creation_input_tokens": 20, "cache_read_input_tokens": 200},
        ]
        _w(f, [obj])
        ingest._ingest_file(self.conn, f, "claude", "gpt-5.5")
        rows = self.conn.execute(
            "SELECT model, total_tokens FROM usage_events ORDER BY dedup_key"
        ).fetchall()
        self.assertEqual([(r["model"], r["total_tokens"]) for r in rows], [
            ("claude-fable-5", 152),
            ("claude-opus-4-8", 313),
        ])

    def test_fallback_removes_larger_temporary_top_level_row(self):
        f = Path(self.tmp.name) / "fallback-stream.jsonl"
        temporary = _assistant("fallback", 200, model="claude-fable-5")
        final = _assistant("fallback", 100, model="claude-opus-4-8")
        final["message"]["usage"]["iterations"] = [
            {"model": "claude-fable-5", "input_tokens": 0, "output_tokens": 210,
             "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0},
            {"model": "claude-opus-4-8", "input_tokens": 0, "output_tokens": 100,
             "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0},
        ]
        _w(f, [temporary, final])
        ingest._ingest_file(self.conn, f, "claude", "gpt-5.5")
        rows = self.conn.execute(
            "SELECT dedup_key, model, total_tokens FROM usage_events ORDER BY dedup_key"
        ).fetchall()
        self.assertEqual([tuple(r) for r in rows], [
            ("fallback:iteration:0", "claude-fable-5", 210),
            ("fallback:iteration:1", "claude-opus-4-8", 100),
        ])


class TestDbUpserts(unittest.TestCase):
    def setUp(self):
        self.conn = db.get_conn(":memory:")
        db.init_db(self.conn)

    def tearDown(self):
        self.conn.close()

    def test_max_keeps_one_coherent_larger_snapshot(self):
        older = UsageRecord(ts=1, source="claude", model="old", project="/p",
                            input_tokens=10, output_tokens=100, total_tokens=110,
                            dedup_key="same")
        newer = UsageRecord(ts=2, source="claude", model="new", project="/p",
                            input_tokens=20, output_tokens=95, total_tokens=115,
                            dedup_key="same")
        db.insert_records(self.conn, [older], on_conflict="max")
        db.insert_records(self.conn, [newer], on_conflict="max")
        row = self.conn.execute(
            "SELECT ts, model, input_tokens, output_tokens, total_tokens FROM usage_events"
        ).fetchone()
        self.assertEqual(tuple(row), (2, "new", 20, 95, 115))

    def test_replace_updates_lower_value_and_ignores_unchanged_row(self):
        wrong = UsageRecord(ts=1, source="hermes", model="m", project="/p",
                            output_tokens=70, total_tokens=70, dedup_key="hermes:s")
        correct = UsageRecord(ts=1, source="hermes", model="m", project="/p",
                              output_tokens=50, reasoning_tokens=20, total_tokens=50,
                              dedup_key="hermes:s")
        self.assertEqual(db.insert_records(self.conn, [wrong], on_conflict="replace"), 1)
        self.assertEqual(db.insert_records(self.conn, [correct], on_conflict="replace"), 1)
        self.assertEqual(db.insert_records(self.conn, [correct], on_conflict="replace"), 0)
        row = self.conn.execute(
            "SELECT output_tokens, reasoning_tokens, total_tokens FROM usage_events"
        ).fetchone()
        self.assertEqual(tuple(row), (50, 20, 50))

    def test_openclaw_cross_format_duplicate_is_deleted(self):
        common = dict(ts=100, source="openclaw", model="gpt-5.4", project="/p",
                      input_tokens=10, output_tokens=5, cache_read_tokens=20,
                      total_tokens=35, session_id="s")
        db.insert_records(self.conn, [
            UsageRecord(**common, source_file="/tmp/s.trajectory.jsonl", dedup_key="trajectory"),
            UsageRecord(**common, source_file="/tmp/s.jsonl", dedup_key="v3"),
            UsageRecord(**{**common, "ts": 101, "total_tokens": 36},
                        source_file="/tmp/s.trajectory.jsonl", dedup_key="unique"),
        ])
        self.assertEqual(db.delete_openclaw_cross_format_duplicates(self.conn), 1)
        keys = [r[0] for r in self.conn.execute(
            "SELECT dedup_key FROM usage_events ORDER BY dedup_key"
        )]
        self.assertEqual(keys, ["unique", "v3"])


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

    def test_codex_archived_copy_not_double_counted(self):
        # 同一份 session 内容存在于 sessions/ 和 archived_sessions/ 两个路径，
        # 各自从头解析一遍，dedup_key 基于文件名 → 第二份被 DB 去重挡掉，不重复计数
        active = Path(self.tmp.name) / "sessions"
        archived = Path(self.tmp.name) / "archived_sessions"
        active.mkdir()
        archived.mkdir()
        content = [
            {"type": "session_meta", "payload": {"id": "u1", "cwd": "/real"}},
            {"type": "turn_context", "payload": {"model": "gpt-5.4", "cwd": "/real"}},
            _tc(1000, 600, 200, 400),
        ]
        _w(active / "roll-uuid.jsonl", content)
        _w(archived / "roll-uuid.jsonl", content)
        ingest._ingest_file(self.conn, active / "roll-uuid.jsonl", "codex", "gpt-5.5")
        ingest._ingest_file(self.conn, archived / "roll-uuid.jsonl", "codex", "gpt-5.5")
        c, i, o, cr = self._sum()
        self.assertEqual(c, 1)     # 只 1 条，不是 2
        self.assertEqual(o, 400)   # 不是 800

    def test_codex_model_fallback(self):
        f = Path(self.tmp.name) / "rollout-y.jsonl"
        _w(f, [
            {"type": "session_meta", "payload": {"id": "u2", "cwd": "/c"}},
            _tc(100, 60, 0, 40),
        ])
        ingest._ingest_file(self.conn, f, "codex", "gpt-5.5")
        row = self.conn.execute("SELECT model FROM usage_events").fetchone()
        self.assertEqual(row["model"], "gpt-5.5")


class TestOpenclaWV3Ingest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = db.get_conn(":memory:")
        db.init_db(self.conn)

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def _rows(self):
        cur = self.conn.execute(
            "SELECT COUNT(*) c, COALESCE(SUM(total_tokens),0) t, "
            "MAX(session_id) sid, MAX(project) proj FROM usage_events"
        )
        return cur.fetchone()

    def _v3_session(self, session_id="sess-abc", cwd="/home/proj"):
        return {"type": "session", "version": 3, "id": session_id,
                "timestamp": "2026-05-01T10:00:00.000Z", "cwd": cwd}

    def _v3_msg(self, msg_id, inp, out, total, model="gpt-5.4", ts_ms=1777000000000):
        return {
            "type": "message",
            "id": msg_id,
            "parentId": None,
            "timestamp": "2026-05-01T10:00:01.000Z",
            "message": {
                "role": "assistant",
                "model": model,
                "usage": {"input": inp, "output": out, "cacheRead": 0,
                          "cacheWrite": 0, "totalTokens": total},
                "timestamp": ts_ms,
            },
        }

    def test_v3_basic_ingestion(self):
        f = Path(self.tmp.name) / "mysess.jsonl"
        _w(f, [
            self._v3_session("sid-1", "/tmp/myproject"),
            self._v3_msg("m1", 100, 50, 150),
            self._v3_msg("m2", 200, 80, 280),
        ])
        ingest._ingest_openclaw_v3_file(self.conn, f)
        r = self._rows()
        self.assertEqual(r["c"], 2)
        self.assertEqual(r["t"], 430)
        self.assertEqual(r["sid"], "sid-1")
        self.assertEqual(r["proj"], "/tmp/myproject")

    def test_v3_zero_token_messages_skipped(self):
        f = Path(self.tmp.name) / "zero.jsonl"
        _w(f, [
            self._v3_session(),
            self._v3_msg("z1", 0, 0, 0),
            self._v3_msg("z2", 10, 5, 15),
        ])
        ingest._ingest_openclaw_v3_file(self.conn, f)
        r = self._rows()
        self.assertEqual(r["c"], 1)   # z1 跳过，只有 z2

    def test_v3_incremental_no_recount(self):
        f = Path(self.tmp.name) / "inc.jsonl"
        _w(f, [self._v3_session(), self._v3_msg("a1", 10, 5, 15)])
        ingest._ingest_openclaw_v3_file(self.conn, f)
        self.assertEqual(self._rows()["c"], 1)
        # 追加新消息，断点续读
        _w(f, [self._v3_msg("a2", 20, 10, 30)])
        ingest._ingest_openclaw_v3_file(self.conn, f)
        r = self._rows()
        self.assertEqual(r["c"], 2)
        self.assertEqual(r["t"], 45)

    def test_v3_dedup_same_msg_id(self):
        f = Path(self.tmp.name) / "dup.jsonl"
        _w(f, [
            self._v3_session(),
            self._v3_msg("dup1", 10, 5, 15),
            self._v3_msg("dup1", 10, 5, 15),  # 同 id
        ])
        ingest._ingest_openclaw_v3_file(self.conn, f)
        self.assertEqual(self._rows()["c"], 1)


class TestHermesIngest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "state.db"
        self.conn = db.get_conn(":memory:")
        db.init_db(self.conn)

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def _make_db(self):
        import sqlite3
        c = sqlite3.connect(str(self.db_path))
        c.execute(
            "CREATE TABLE sessions (id TEXT PRIMARY KEY, model TEXT, cwd TEXT, "
            "started_at REAL, parent_session_id TEXT, input_tokens INTEGER DEFAULT 0, "
            "output_tokens INTEGER DEFAULT 0, cache_read_tokens INTEGER DEFAULT 0, "
            "cache_write_tokens INTEGER DEFAULT 0, reasoning_tokens INTEGER DEFAULT 0)"
        )
        c.commit()
        c.close()

    def _upsert(self, **kw):
        import sqlite3
        c = sqlite3.connect(str(self.db_path))
        c.execute(
            "INSERT INTO sessions (id, model, cwd, started_at, input_tokens, output_tokens) "
            "VALUES (:id,:model,:cwd,:started_at,:input_tokens,:output_tokens) "
            "ON CONFLICT(id) DO UPDATE SET input_tokens=:input_tokens, output_tokens=:output_tokens",
            kw,
        )
        c.commit()
        c.close()

    def _sum(self):
        cur = self.conn.execute(
            "SELECT COUNT(*) c, COALESCE(SUM(input_tokens),0) i, COALESCE(SUM(output_tokens),0) o "
            "FROM usage_events WHERE source='hermes'"
        )
        r = cur.fetchone()
        return r["c"], r["i"], r["o"]

    def test_growing_session_rescanned_without_duplication(self):
        # 长会话 token 数随进行增长；全表重扫 + dedup_key=session id + on_conflict='max'
        # 应该只留 1 行且反映最新累计值，不会重复计数
        self._make_db()
        with patch("tokenstat.ingest.config.HERMES_STATE_DB", self.db_path):
            self._upsert(id="s1", model="gpt-5.5", cwd="/p", started_at=1700000000,
                         input_tokens=100, output_tokens=50)
            self.assertEqual(ingest._ingest_hermes(self.conn), 1)
            self.assertEqual(self._sum(), (1, 100, 50))

            self.assertEqual(ingest._ingest_hermes(self.conn), 0)

            self._upsert(id="s1", model="gpt-5.5", cwd="/p", started_at=1700000000,
                         input_tokens=300, output_tokens=120)  # 同一会话继续增长
            self.assertEqual(ingest._ingest_hermes(self.conn), 1)
            self.assertEqual(self._sum(), (1, 300, 120))  # 仍 1 行，取最新值，不是叠加

    def test_missing_db_returns_zero(self):
        with patch("tokenstat.ingest.config.HERMES_STATE_DB", Path(self.tmp.name) / "nope.db"):
            self.assertEqual(ingest._ingest_hermes(self.conn), 0)


class TestGrokIngest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = db.get_conn(":memory:")
        db.init_db(self.conn)
        self.log = Path(self.tmp.name) / "unified.jsonl"

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def _sum(self):
        cur = self.conn.execute(
            "SELECT COUNT(*) c, COALESCE(SUM(input_tokens),0) i, "
            "COALESCE(SUM(output_tokens),0) o, COALESCE(SUM(cache_read_tokens),0) cr "
            "FROM usage_events WHERE source='grok'"
        )
        r = cur.fetchone()
        return r["c"], r["i"], r["o"], r["cr"]

    def test_incremental_and_carry_forward(self):
        lines = [
            {
                "ts": "2026-07-09T10:00:00.000Z",
                "sid": "s1",
                "msg": "model changed",
                "ctx": {"model": "grok-4.5"},
            },
            {
                "ts": "2026-07-09T10:00:01.000Z",
                "sid": "s1",
                "msg": "session created",
                "ctx": {"cwd": "/proj/a"},
            },
            {
                "ts": "2026-07-09T10:00:05.000Z",
                "sid": "s1",
                "msg": "shell.turn.inference_done",
                "ctx": {
                    "loop_index": 0,
                    "prompt_tokens": 1000,
                    "cached_prompt_tokens": 200,
                    "completion_tokens": 40,
                    "reasoning_tokens": 20,
                },
            },
        ]
        _w(self.log, lines)
        with patch("tokenstat.ingest.config.GROK_LOG_PATH", self.log):
            n = ingest._ingest_grok(self.conn)
            self.assertEqual(n, 1)
            self.assertEqual(self._sum(), (1, 800, 40, 200))

            # 无新增
            self.assertEqual(ingest._ingest_grok(self.conn), 0)

            # 追加第二条；model/cwd 应从 ctx 延续
            _w(
                self.log,
                [
                    {
                        "ts": "2026-07-09T10:01:00.000Z",
                        "sid": "s1",
                        "msg": "shell.turn.inference_done",
                        "ctx": {
                            "loop_index": 1,
                            "prompt_tokens": 500,
                            "cached_prompt_tokens": 100,
                            "completion_tokens": 10,
                            "reasoning_tokens": 5,
                        },
                    }
                ],
            )
            self.assertEqual(ingest._ingest_grok(self.conn), 1)
            self.assertEqual(self._sum(), (2, 1200, 50, 300))

            cur = self.conn.execute(
                "SELECT model, project FROM usage_events WHERE source='grok' ORDER BY pos"
            )
            rows = cur.fetchall()
            self.assertEqual(rows[0]["model"], "grok-4.5")
            self.assertEqual(rows[0]["project"], "/proj/a")
            self.assertEqual(rows[1]["model"], "grok-4.5")
            self.assertEqual(rows[1]["project"], "/proj/a")

    def test_missing_log_returns_zero(self):
        with patch("tokenstat.ingest.config.GROK_LOG_PATH", Path(self.tmp.name) / "nope.jsonl"):
            self.assertEqual(ingest._ingest_grok(self.conn), 0)


if __name__ == "__main__":
    unittest.main()
