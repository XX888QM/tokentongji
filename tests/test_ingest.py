import json
import sqlite3
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


def _claude_mem_usage(event_id="thread-1"):
    return {
        "type": "claude_mem.codex_usage",
        "schema_version": 1,
        "timestamp": "2026-07-27T08:58:18.000Z",
        "event_id": event_id,
        "model": "gpt-5.6-luna",
        "project": "/project",
        "session_id": "session-1",
        "usage": {
            "input_tokens": 100,
            "cached_input_tokens": 25,
            "cache_write_input_tokens": 0,
            "output_tokens": 20,
            "reasoning_output_tokens": 10,
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

    def test_old_snapshot_after_fallback_does_not_reinsert_base_record(self):
        final = Path(self.tmp.name) / "final.jsonl"
        old_copy = Path(self.tmp.name) / "old-copy.jsonl"
        obj = _assistant("fallback", 200)
        obj["message"]["usage"]["iterations"] = [
            {"model": "claude-fable-5", "input_tokens": 0, "output_tokens": 100},
            {"model": "claude-opus-4-8", "input_tokens": 0, "output_tokens": 200},
        ]
        _w(final, [obj])
        _w(old_copy, [_assistant("fallback", 100)])

        ingest._ingest_file(self.conn, final, "claude", "gpt-5.5")
        ingest._ingest_file(self.conn, old_copy, "claude", "gpt-5.5")

        rows = self.conn.execute(
            "SELECT dedup_key, total_tokens FROM usage_events ORDER BY dedup_key"
        ).fetchall()
        self.assertEqual([tuple(r) for r in rows], [
            ("fallback:iteration:0", 100),
            ("fallback:iteration:1", 200),
        ])

    def test_bad_token_field_does_not_block_later_records(self):
        f = Path(self.tmp.name) / "bad-field.jsonl"
        bad = _assistant("bad", 10)
        bad["message"]["usage"]["input_tokens"] = "oops"
        _w(f, [bad, _assistant("good", 20)])

        self.assertEqual(ingest._ingest_file(self.conn, f, "claude", "gpt-5.5"), 1)
        self.assertEqual(self._rows(), (1, 20))

    def test_bad_model_type_does_not_block_later_records(self):
        f = Path(self.tmp.name) / "bad-model.jsonl"
        bad = _assistant("bad", 10)
        bad["message"]["model"] = {"bad": "type"}
        _w(f, [bad, _assistant("good", 20)])

        self.assertEqual(ingest._ingest_file(self.conn, f, "claude", "gpt-5.5"), 1)
        self.assertEqual(self._rows(), (1, 20))

    def test_overflow_token_field_does_not_block_later_records(self):
        f = Path(self.tmp.name) / "overflow.jsonl"
        bad = _assistant("bad", 10)
        bad["message"]["usage"]["input_tokens"] = float("inf")
        _w(f, [bad, _assistant("good", 20)])

        self.assertEqual(ingest._ingest_file(self.conn, f, "claude", "gpt-5.5"), 1)
        self.assertEqual(self._rows(), (1, 20))


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

    def test_openclaw_paired_trajectory_rows_all_deleted(self):
        # trajectory 行是若干 v3 行的合计，时间戳和 token 都对不上逐条 v3，
        # 因此配对会话的 trajectory 行要整段删除，不能只删 token 全等的那条。
        common = dict(source="openclaw", model="gpt-5.4", project="/p", session_id="s")
        db.insert_records(self.conn, [
            UsageRecord(**common, ts=100, input_tokens=10, output_tokens=5,
                        cache_read_tokens=20, total_tokens=35,
                        source_file="/tmp/s.jsonl", dedup_key="v3-a"),
            UsageRecord(**common, ts=110, input_tokens=7, output_tokens=3,
                        cache_read_tokens=30, total_tokens=40,
                        source_file="/tmp/s.jsonl", dedup_key="v3-b"),
            # 合计行：ts 晚几秒，token = 上面两条之和，逐条比对永远匹配不上
            UsageRecord(**common, ts=119, input_tokens=17, output_tokens=8,
                        cache_read_tokens=50, total_tokens=75,
                        source_file="/tmp/s.trajectory.jsonl", dedup_key="traj-sum"),
        ])
        self.assertEqual(
            db.delete_openclaw_cross_format_duplicates(self.conn, ["/tmp/s.jsonl"]), 1
        )
        keys = [r[0] for r in self.conn.execute(
            "SELECT dedup_key FROM usage_events ORDER BY dedup_key"
        )]
        self.assertEqual(keys, ["v3-a", "v3-b"])

    def test_openclaw_unpaired_trajectory_is_kept(self):
        # 没有配套 v3 文件的 trajectory 是唯一数据来源，绝不能删
        db.insert_records(self.conn, [
            UsageRecord(ts=100, source="openclaw", model="gpt-5.4", project="/p",
                        input_tokens=10, output_tokens=5, total_tokens=15,
                        source_file="/tmp/lonely.trajectory.jsonl", dedup_key="solo"),
        ])
        self.assertEqual(db.delete_openclaw_cross_format_duplicates(self.conn, []), 0)
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM usage_events").fetchone()[0], 1
        )

    def test_openclaw_trajectory_survives_after_v3_reset(self):
        # v3 文件早先产生过历史行，之后被 openclaw 侧 .jsonl.reset.* 重命名/停更
        # （不再出现在本轮 glob 到的 active_v3_paths 里）。此后 trajectory 侧新写
        # 入的行必须保留，不能因为"历史上配对过"就被永远继续删下去。
        common = dict(source="openclaw", model="gpt-5.4", project="/p", session_id="s")
        db.insert_records(self.conn, [
            UsageRecord(**common, ts=100, input_tokens=10, output_tokens=5,
                        total_tokens=15, source_file="/tmp/reset.jsonl",
                        dedup_key="v3-old"),
        ])
        # 第一轮：v3 文件还在，配对生效，trajectory 行被删（沿用既有行为）
        db.insert_records(self.conn, [
            UsageRecord(**common, ts=110, input_tokens=8, output_tokens=4,
                        total_tokens=12, source_file="/tmp/reset.trajectory.jsonl",
                        dedup_key="traj-1"),
        ])
        self.assertEqual(
            db.delete_openclaw_cross_format_duplicates(self.conn, ["/tmp/reset.jsonl"]), 1
        )

        # 第二轮：v3 文件已被 reset（不再出现在 active_v3_paths），trajectory 侧
        # 又写入了新一轮真实用量——这条必须保留，不能被"历史上配对过"误杀。
        db.insert_records(self.conn, [
            UsageRecord(**common, ts=200, input_tokens=100, output_tokens=99,
                        total_tokens=1998, source_file="/tmp/reset.trajectory.jsonl",
                        dedup_key="traj-2"),
        ])
        self.assertEqual(db.delete_openclaw_cross_format_duplicates(self.conn, []), 0)
        keys = [r[0] for r in self.conn.execute(
            "SELECT dedup_key FROM usage_events ORDER BY dedup_key"
        )]
        self.assertEqual(keys, ["traj-2", "v3-old"])


class TestRequestPromptMigration(unittest.TestCase):
    def setUp(self):
        self.conn = db.get_conn(":memory:")
        # 模拟升级前的实际表结构：没有 request_prompt_tokens 列。
        self.conn.executescript(db._SCHEMA.replace("    request_prompt_tokens INTEGER,\n", ""))
        for source in ("grok", "openclaw", "opencode", "codex", "hermes"):
            self.conn.execute(
                """
                INSERT INTO usage_events (
                    ts, date_local, source, model, project, input_tokens,
                    cache_read_tokens, dedup_key
                ) VALUES (1, '2026-07-20', ?, 'm', '/p', 100, 25, ?)
                """,
                (source, f"legacy-{source}"),
            )
        self.conn.commit()

    def tearDown(self):
        self.conn.close()

    def test_migrates_only_confirmed_single_request_sources_once(self):
        db.init_db(self.conn)
        rows = {
            row["source"]: row["request_prompt_tokens"]
            for row in self.conn.execute(
                "SELECT source, request_prompt_tokens FROM usage_events"
            )
        }
        self.assertEqual(rows["grok"], 125)
        self.assertEqual(rows["openclaw"], 125)
        self.assertEqual(rows["opencode"], 125)
        self.assertIsNone(rows["codex"])
        self.assertIsNone(rows["hermes"])

        # 第二次启动不应把之后刻意保留 NULL 的记录再次猜测回填。
        self.conn.execute(
            "UPDATE usage_events SET request_prompt_tokens = NULL WHERE source = 'grok'"
        )
        self.conn.commit()
        db.init_db(self.conn)
        value = self.conn.execute(
            "SELECT request_prompt_tokens FROM usage_events WHERE source = 'grok'"
        ).fetchone()[0]
        self.assertIsNone(value)


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

    def test_claude_mem_usage_spool_is_discovered_and_deduplicated(self):
        usage_dir = Path(self.tmp.name) / "usage"
        usage_dir.mkdir()
        spool = usage_dir / "codex-usage-2026-07-27.jsonl"
        _w(spool, [_claude_mem_usage()])
        with patch("tokenstat.ingest.config.CLAUDE_MEM_CODEX_USAGE_DIR", usage_dir):
            self.assertEqual(list(ingest.claude_mem_codex_usage_files()), [spool])
            self.assertEqual(ingest._ingest_file(self.conn, spool, "codex", "gpt-5.5"), 1)
            self.assertEqual(ingest._ingest_file(self.conn, spool, "codex", "gpt-5.5"), 0)
        row = self.conn.execute(
            "SELECT category, input_tokens, cache_read_tokens, output_tokens, total_tokens "
            "FROM usage_events"
        ).fetchone()
        self.assertEqual(tuple(row), ("observer", 75, 25, 20, 120))

    def test_file_rebuild_resets_offset_and_carry_forward_baseline(self):
        # docstring 明确写"inode 变 -> 文件重建或被截断，从头重读"，但没有任何
        # 用例真的模拟过文件重建。用真实场景验证：文件被删掉重建成一个全新的
        # 小会话后，新会话的用量必须被正常计入，不能因为旧 prev_total 基线
        # （上百万）继续拿来做差分，把新会话的小 total 一减直接变成 0（被
        # max(0, ...) 吞掉，系统性漏计）。
        f = Path(self.tmp.name) / "codex-session.jsonl"
        # cwd 故意撑得很长：保证旧 offset 明显大于重建后小文件的体积，不依赖
        # "unlink 重建后 inode 一定变"这个平台相关行为，直接、确定地命中
        # _should_read 的 size < offset 分支（同样是文档说的"文件重建/截断"）。
        padded_cwd = "/" + ("x" * 4000)
        _w(f, [
            {"type": "turn_context", "payload": {"model": "gpt-5.5", "cwd": padded_cwd}},
            _tc(1_000_000, 900_000, 100_000, 100_000),
        ], mode="wb")
        added1 = ingest._ingest_file(self.conn, f, "codex", "gpt-5.5")
        self.assertEqual(added1, 1)

        # 同路径原地重写成一个全新的小会话（截断变小，触发 _should_read 重置）
        _w(f, [
            {"type": "turn_context", "payload": {"model": "gpt-5.5", "cwd": "/c"}},
            _tc(200, 150, 0, 50),
        ], mode="wb")
        added2 = ingest._ingest_file(self.conn, f, "codex", "gpt-5.5")
        self.assertEqual(added2, 1)  # 新会话的用量不能被旧基线吞掉

        rows = self.conn.execute(
            "SELECT total_tokens FROM usage_events ORDER BY id"
        ).fetchall()
        self.assertEqual([r["total_tokens"] for r in rows], [1_000_000, 200])

    def test_oversized_line_skipped_but_offset_advances_and_next_line_ingested(self):
        # docstring 写"单行 >50MB 跳过"，但没有用例验证被跳过的巨行之后，offset
        # 是否正确前进（不前进的话下一轮会在同一行卡死，永远读不到它之后的
        # 内容）。用 patch 把阈值调小，不用真造 50MB 的行拖慢测试。
        f = Path(self.tmp.name) / "huge-line.jsonl"
        with open(f, "wb") as fh:
            fh.write(b"x" * 2000 + b"\n")  # 超过下面 patch 的 1000 字节阈值
        _w(f, [
            {"type": "turn_context", "payload": {"model": "gpt-5.5", "cwd": "/c"}},
            _tc(500, 300, 0, 200),
        ])
        with patch.object(ingest, "MAX_LINE_BYTES", 1000):
            added = ingest._ingest_file(self.conn, f, "codex", "gpt-5.5")
        self.assertEqual(added, 1)  # 巨行被跳过，正常行照常入库
        row = self.conn.execute("SELECT total_tokens FROM usage_events").fetchone()
        self.assertEqual(row["total_tokens"], 500)

        # offset 必须已经前进过巨行：重新 ingest 不应再产出任何新行
        with patch.object(ingest, "MAX_LINE_BYTES", 1000):
            added2 = ingest._ingest_file(self.conn, f, "codex", "gpt-5.5")
        self.assertEqual(added2, 0)


class TestShouldRead(unittest.TestCase):
    """_should_read() 的 inode/size/mtime 判断分支，纯函数直接测，不依赖真实
    文件系统的 inode 分配行为（更快、更确定）。"""

    def test_no_prior_state_reads_from_start(self):
        self.assertEqual(
            ingest._should_read(None, inode=5, size=100, mtime=1.0), (0, True)
        )

    def test_inode_change_resets_from_start(self):
        # 文件被重建（同路径换了新文件）：inode 变了，必须从头读、重置 ctx
        state = {"inode": 1, "offset": 500, "size": 500, "mtime": 10.0, "ctx": {}}
        self.assertEqual(
            ingest._should_read(state, inode=2, size=50, mtime=11.0), (0, True)
        )

    def test_size_smaller_than_offset_resets_from_start(self):
        # 同 inode 但文件被截断变小（size < 已读 offset）：也必须从头重读
        state = {"inode": 1, "offset": 500, "size": 500, "mtime": 10.0, "ctx": {}}
        self.assertEqual(
            ingest._should_read(state, inode=1, size=100, mtime=11.0), (0, True)
        )

    def test_no_change_returns_same_offset_without_reset(self):
        state = {"inode": 1, "offset": 500, "size": 500, "mtime": 10.0, "ctx": {}}
        self.assertEqual(
            ingest._should_read(state, inode=1, size=500, mtime=10.0), (500, False)
        )

    def test_appended_data_continues_without_reset(self):
        state = {"inode": 1, "offset": 500, "size": 500, "mtime": 10.0, "ctx": {}}
        self.assertEqual(
            ingest._should_read(state, inode=1, size=800, mtime=12.0), (500, False)
        )


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


class TestOpenclawSqliteIngest(unittest.TestCase):
    def setUp(self):
        import sqlite3

        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        agent_dir = self.root / "main" / "agent"
        agent_dir.mkdir(parents=True)
        self.db_path = agent_dir / "openclaw-agent.sqlite"
        src = sqlite3.connect(str(self.db_path))
        src.execute(
            "CREATE TABLE transcript_events (session_id TEXT, seq INTEGER, event_json TEXT, created_at INTEGER, PRIMARY KEY(session_id, seq))"
        )
        src.execute("CREATE TABLE session_windows (session_id TEXT, session_key TEXT)")
        src.execute(
            "INSERT INTO session_windows VALUES (?,?)",
            ("sid-1", "openclaw-weixin:direct"),
        )
        src.execute(
            "INSERT INTO transcript_events VALUES (?,?,?,?)",
            (
                "sid-1",
                0,
                json.dumps({"type": "session", "id": "sid-1", "cwd": "/workspace"}),
                1,
            ),
        )
        src.execute(
            "INSERT INTO transcript_events VALUES (?,?,?,?)",
            (
                "sid-1",
                1,
                json.dumps({
                    "type": "message",
                    "id": "sql-new",
                    "message": {
                        "role": "assistant",
                        "model": "grok-4.6",
                        "usage": {
                            "input": 100,
                            "output": 20,
                            "cacheRead": 5,
                            "cacheWrite": 0,
                            "totalTokens": 125,
                        },
                        "timestamp": 1_777_000_000_000,
                    },
                }),
                2,
            ),
        )
        src.commit()
        src.close()
        self.conn = db.get_conn(":memory:")
        db.init_db(self.conn)

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def test_sqlite_ingest_and_ignore_existing_v3_key(self):
        db.insert_records(
            self.conn,
            [
                UsageRecord(
                    ts=100,
                    source="openclaw",
                    model="grok-4.6",
                    project="old",
                    input_tokens=100,
                    output_tokens=20,
                    cache_read_tokens=5,
                    total_tokens=125,
                    session_id="sid-1",
                    source_file="/old.jsonl",
                    pos=1,
                    dedup_key="openclaw-v3:sql-new",
                )
            ],
        )
        with patch("tokenstat.config.OPENCLAW_AGENTS_DIR", self.root):
            added = ingest._ingest_openclaw_sqlite(self.conn)
        self.assertEqual(added, 0)
        n = self.conn.execute(
            "SELECT COUNT(*) AS c FROM usage_events WHERE source='openclaw'"
        ).fetchone()["c"]
        self.assertEqual(n, 1)

    def test_sqlite_ingest_inserts_new_rows(self):
        with patch("tokenstat.config.OPENCLAW_AGENTS_DIR", self.root):
            added = ingest._ingest_openclaw_sqlite(self.conn)
        self.assertEqual(added, 1)
        row = self.conn.execute(
            "SELECT project, total_tokens, model FROM usage_events"
        ).fetchone()
        self.assertEqual(row["project"], "openclaw-weixin")
        self.assertEqual(row["total_tokens"], 125)
        self.assertEqual(row["model"], "grok-4.6")

    def test_sqlite_ingest_replaces_matching_old_trajectory_session(self):
        db.insert_records(
            self.conn,
            [
                UsageRecord(
                    ts=100,
                    source="openclaw",
                    model="grok-4.6",
                    project="old",
                    input_tokens=100,
                    output_tokens=20,
                    cache_read_tokens=5,
                    total_tokens=125,
                    session_id="sid-1",
                    source_file="/old.trajectory.jsonl",
                    pos=1,
                    dedup_key="openclaw:old-run:1",
                )
            ],
        )
        with patch("tokenstat.config.OPENCLAW_AGENTS_DIR", self.root):
            self.assertEqual(ingest._ingest_openclaw_sqlite(self.conn), 1)
        rows = self.conn.execute(
            "SELECT source_file, total_tokens FROM usage_events ORDER BY dedup_key"
        ).fetchall()
        self.assertEqual([tuple(r) for r in rows], [(str(self.db_path), 125)])

    def test_sqlite_partial_session_keeps_old_trajectory(self):
        db.insert_records(
            self.conn,
            [
                UsageRecord(
                    ts=100, source="openclaw", model="grok-4.6", project="old",
                    total_tokens=250, session_id="sid-1", source_file="/old.trajectory.jsonl",
                    pos=1, dedup_key="openclaw:old-run:1",
                )
            ],
        )
        with patch("tokenstat.config.OPENCLAW_AGENTS_DIR", self.root):
            self.assertEqual(ingest._ingest_openclaw_sqlite(self.conn), 0)
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) AS c FROM usage_events").fetchone()["c"], 1
        )

    def test_sqlite_new_records_after_old_prefix_replace_trajectory(self):
        db.insert_records(
            self.conn,
            [
                UsageRecord(
                    ts=100, source="openclaw", model="grok-4.6", project="old",
                    total_tokens=125, session_id="sid-1", source_file="/old.trajectory.jsonl",
                    pos=1, dedup_key="openclaw:old-run:1",
                )
            ],
        )
        import sqlite3
        src = sqlite3.connect(str(self.db_path))
        src.execute(
            "INSERT INTO transcript_events VALUES (?,?,?,?)",
            ("sid-1", 2, json.dumps({
                "type": "message", "id": "sql-later", "message": {
                    "role": "assistant", "model": "grok-4.6",
                    "usage": {"input": 5, "output": 2, "totalTokens": 7},
                    "timestamp": 1_777_000_001_000,
                },
            }), 3),
        )
        src.commit()
        src.close()
        with patch("tokenstat.config.OPENCLAW_AGENTS_DIR", self.root):
            self.assertEqual(ingest._ingest_openclaw_sqlite(self.conn), 2)
        self.assertEqual(
            self.conn.execute("SELECT SUM(total_tokens) AS t FROM usage_events").fetchone()["t"], 132
        )


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

    def test_bad_usage_field_does_not_block_later_grok_record(self):
        bad = {
            "ts": "2026-07-09T10:00:00.000Z", "sid": "s1",
            "msg": "shell.turn.inference_done",
            "ctx": {"loop_index": 0, "prompt_tokens": 2**64, "completion_tokens": 1},
        }
        good = {
            "ts": "2026-07-09T10:00:01.000Z", "sid": "s1",
            "msg": "shell.turn.inference_done",
            "ctx": {"loop_index": 1, "prompt_tokens": 10, "completion_tokens": 2},
        }
        _w(self.log, [bad, good])
        with patch("tokenstat.ingest.config.GROK_LOG_PATH", self.log):
            self.assertEqual(ingest._ingest_grok(self.conn), 1)
        self.assertEqual(self._sum(), (1, 10, 2, 0))


class TestIngestStateAndForkReset(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = db.get_conn(":memory:")
        db.init_db(self.conn)

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def test_bad_ctx_forces_full_rescan(self):
        path = "/tmp/broken.jsonl"
        db.set_ingest_state(self.conn, path, inode=1, offset=999, size=999, mtime=1.0, ctx={"ok": 1})
        self.conn.execute("UPDATE ingest_state SET ctx = ? WHERE source_file = ?", ("not-json", path))
        self.conn.commit()
        self.assertIsNone(db.get_ingest_state(self.conn, path))
        self.conn.execute("UPDATE ingest_state SET ctx = ? WHERE source_file = ?", ("[]", path))
        self.conn.commit()
        self.assertIsNone(db.get_ingest_state(self.conn, path))

    def test_reset_codex_fork_sessions_deletes_only_fork_files(self):
        root = Path(self.tmp.name)
        parent = root / "rollout-parent.jsonl"
        child = root / "rollout-child.jsonl"
        _w(parent, [{"type": "session_meta", "payload": {"id": "parent", "cwd": "/p"}}], mode="wb")
        _w(
            child,
            [{"type": "session_meta", "payload": {"id": "child", "cwd": "/c", "forked_from_id": "parent"}}],
            mode="wb",
        )
        db.insert_records(self.conn, [
            UsageRecord(ts=1, source="codex", model="gpt-5.5", project="/p",
                        total_tokens=100, source_file=str(parent), dedup_key="parent-1"),
            UsageRecord(ts=1, source="codex", model="gpt-5.5", project="/c",
                        total_tokens=200, source_file=str(child), dedup_key="child-1"),
        ])
        db.set_ingest_state(self.conn, str(child), inode=1, offset=10, size=10, mtime=1.0, ctx={})
        with patch("tokenstat.ingest.codex_files", return_value=[parent, child]):
            result = ingest.reset_codex_fork_sessions(self.conn)
        self.assertEqual(result["files"], 1)
        self.assertEqual(result["deleted_events"], 1)
        keys = [r[0] for r in self.conn.execute("SELECT dedup_key FROM usage_events")]
        self.assertEqual(keys, ["parent-1"])
        self.assertIsNone(db.get_ingest_state(self.conn, str(child)))

    def test_reset_detects_fork_meta_after_parent_meta(self):
        root = Path(self.tmp.name)
        mixed = root / "rollout-mixed.jsonl"
        _w(
            mixed,
            [
                {"type": "session_meta", "payload": {"id": "parent", "cwd": "/p"}},
                {"type": "session_meta", "payload": {"id": "child", "cwd": "/c", "forked_from_id": "parent"}},
            ],
            mode="wb",
        )
        db.insert_records(self.conn, [
            UsageRecord(ts=1, source="codex", model="gpt-5.5", project="/c",
                        total_tokens=200, source_file=str(mixed), dedup_key="mixed-1"),
        ])
        with patch("tokenstat.ingest.codex_files", return_value=[mixed]):
            result = ingest.reset_codex_fork_sessions(self.conn)
        self.assertEqual(result["files"], 1)
        self.assertEqual(result["deleted_events"], 1)


class TestCursorIngest(unittest.TestCase):
    def setUp(self):
        self.conn = db.get_conn(":memory:")
        db.init_db(self.conn)

    def tearDown(self):
        self.conn.close()

    def _rec(self, key="cursor:1:m:1:0:0:1:1"):
        return UsageRecord(
            ts=1750597784,
            source="cursor",
            model="composer-2.5-fast",
            project="cursor",
            input_tokens=1,
            output_tokens=1,
            total_tokens=2,
            request_prompt_tokens=1,
            dedup_key=key,
        )

    def test_disabled_skips_without_fetch(self):
        with patch("tokenstat.ingest.config.CURSOR_ENABLED", False):
            with patch("tokenstat.ingest.cursor_parser.fetch_records") as fetch:
                self.assertEqual(ingest._ingest_cursor(self.conn), 0)
                fetch.assert_not_called()

    def test_auth_error_backs_off(self):
        with patch("tokenstat.ingest.config.CURSOR_ENABLED", True):
            with patch("tokenstat.ingest.config.CURSOR_REFRESH_SEC", 600):
                with patch(
                    "tokenstat.ingest.cursor_parser.fetch_records",
                    side_effect=ingest.cursor_parser.CursorAuthError("expired"),
                ) as fetch:
                    with self.assertRaises(ingest.cursor_parser.CursorAuthError):
                        ingest._ingest_cursor(self.conn)
                    self.assertEqual(ingest._ingest_cursor(self.conn), 0)
                    self.assertEqual(fetch.call_count, 1)

    def test_fetch_error_backs_off(self):
        with patch("tokenstat.ingest.config.CURSOR_ENABLED", True):
            with patch("tokenstat.ingest.config.CURSOR_REFRESH_SEC", 600):
                with patch(
                    "tokenstat.ingest.cursor_parser.fetch_records",
                    side_effect=ingest.cursor_parser.CursorFetchError("HTTP 503"),
                ) as fetch:
                    with self.assertRaises(ingest.cursor_parser.CursorFetchError):
                        ingest._ingest_cursor(self.conn)
                    self.assertEqual(ingest._ingest_cursor(self.conn), 0)
                    self.assertEqual(fetch.call_count, 1)

    def test_skip_does_not_throttle(self):
        with patch("tokenstat.ingest.config.CURSOR_ENABLED", True):
            with patch(
                "tokenstat.ingest.cursor_parser.fetch_records",
                side_effect=ingest.cursor_parser.CursorSkip("signed out"),
            ):
                self.assertEqual(ingest._ingest_cursor(self.conn), 0)
        self.assertIsNone(db.get_ingest_state(self.conn, "cursor:dashboard-csv"))

    def test_success_is_throttled_then_deduped(self):
        rec = self._rec()
        with patch("tokenstat.ingest.config.CURSOR_ENABLED", True):
            with patch("tokenstat.ingest.config.CURSOR_REFRESH_SEC", 600):
                with patch(
                    "tokenstat.ingest.cursor_parser.fetch_records",
                    return_value=[rec],
                ) as fetch:
                    self.assertEqual(ingest._ingest_cursor(self.conn), 1)
                    self.assertEqual(ingest._ingest_cursor(self.conn), 0)
                    self.assertEqual(fetch.call_count, 1)
                    fetch.return_value = [rec, self._rec("cursor:2:m:2:0:0:2:1")]
                    with patch("tokenstat.ingest.time.time", return_value=1_800_000_000):
                        self.assertEqual(ingest._ingest_cursor(self.conn), 1)
                    self.assertEqual(fetch.call_count, 2)
        count = self.conn.execute(
            "SELECT COUNT(*) FROM usage_events WHERE source='cursor'"
        ).fetchone()[0]
        self.assertEqual(count, 2)


class TestIngestLock(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.data = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_db_open_failure_releases_lock(self):
        with patch("tokenstat.config.DATA_DIR", self.data):
            with patch("tokenstat.ingest.db.get_conn", side_effect=sqlite3.Error("boom")):
                with self.assertRaises(sqlite3.Error):
                    ingest.run_once()
                with self.assertRaises(sqlite3.Error):
                    ingest.run_once()


if __name__ == "__main__":
    unittest.main()
