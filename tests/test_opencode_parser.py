"""opencode 解析器测试：SQLite 直读 + 增量水位 + reasoning 归 output。"""
import json
import sqlite3
import tempfile
import unittest
import unittest.mock
from pathlib import Path

from tokenstat.parsers import opencode


def _make_db(path: Path, rows):
    """建一个最小 opencode message 表；rows = [(id, session_id, time_created_ms, data_dict)]。"""
    conn = sqlite3.connect(str(path))
    conn.execute(
        "CREATE TABLE message (id TEXT PRIMARY KEY, session_id TEXT, "
        "time_created INTEGER, data TEXT)"
    )
    conn.executemany(
        "INSERT INTO message (id, session_id, time_created, data) VALUES (?,?,?,?)",
        [(i, sid, ts, json.dumps(d)) for (i, sid, ts, d) in rows],
    )
    conn.commit()
    conn.close()


def _assistant(inp=100, out=50, reasoning=0, cread=0, cwrite=0, total=None, model="grok-2", cwd="/proj"):
    tokens = {"input": inp, "output": out, "reasoning": reasoning,
              "cache": {"read": cread, "write": cwrite}}
    if total is not None:
        tokens["total"] = total
    return {"role": "assistant", "tokens": tokens, "modelID": model, "path": {"cwd": cwd}}


class TestOpencodeParser(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "opencode.db"

    def tearDown(self):
        self.tmp.cleanup()

    def test_basic_parse_and_fields(self):
        _make_db(self.db, [
            ("m1", "s1", 1_700_000_000_000, _assistant(inp=100, out=50, reasoning=20, cread=10, cwrite=5)),
        ])
        recs, max_ts = self.assertRecords(1)
        r = recs[0]
        self.assertEqual(r.source, "opencode")
        self.assertEqual(r.input_tokens, 100)
        self.assertEqual(r.output_tokens, 50)
        self.assertEqual(r.reasoning_tokens, 20)
        self.assertEqual(r.cache_read_tokens, 10)
        self.assertEqual(r.cache_creation_tokens, 5)
        self.assertEqual(r.request_prompt_tokens, 110)
        # 无 total 字段时按各分量求和
        self.assertEqual(r.total_tokens, 100 + 50 + 20 + 10 + 5)
        self.assertEqual(r.ts, 1_700_000_000)  # ms → s
        self.assertEqual(r.dedup_key, "opencode:m1")
        self.assertEqual(max_ts, 1_700_000_000_000)

    def test_role_and_zero_filtered(self):
        _make_db(self.db, [
            ("u1", "s1", 1_700_000_000_000, {"role": "user", "tokens": {"input": 1}}),
            ("z1", "s1", 1_700_000_000_001, _assistant(inp=0, out=0, reasoning=0, cread=0, cwrite=0)),
            ("m1", "s1", 1_700_000_000_002, _assistant(inp=5, out=3)),
        ])
        # user 角色被 SQL 过滤；全 0 消息被跳过；只剩 m1
        recs, _ = self.assertRecords(1)
        self.assertEqual(recs[0].dedup_key, "opencode:m1")

    def test_incremental_watermark_ge(self):
        # 用 >= 水位：与游标同毫秒的行会被再读到，但靠 dedup_key 去重（此处只验证不漏）
        _make_db(self.db, [
            ("a", "s1", 1000, _assistant(inp=10, out=5)),
            ("b", "s1", 2000, _assistant(inp=20, out=5)),
        ])
        recs, max_ts = opencode.fetch_records(self.db, since_ts_ms=0)
        self.assertEqual(len(recs), 2)
        self.assertEqual(max_ts, 2000)
        # 下轮以 max_ts 为起点（>=），边界行 b 会被重复取到（交由 DB dedup 挡）
        recs2, _ = opencode.fetch_records(self.db, since_ts_ms=2000)
        self.assertEqual([r.dedup_key for r in recs2], ["opencode:b"])

    def test_watermark_frozen_at_first_unparsed_row_so_late_update_is_not_missed(self):
        # 真实 opencode 写入模式：占位行(tokens 全 0, ts 较小)先出现，之后一条已
        # 完成的行(ts 较大)紧跟其后。水位线绝不能被后者推过前者——否则占位行
        # 之后被原地 UPDATE 成真实 token 值时(time_created 不变)，会被永久漏读。
        _make_db(self.db, [
            ("pending", "s1", 1000, _assistant(inp=0, out=0, reasoning=0, cread=0, cwrite=0)),
            ("done", "s1", 2000, _assistant(inp=20, out=10)),
        ])
        recs, max_ts = opencode.fetch_records(self.db, since_ts_ms=0)
        self.assertEqual([r.dedup_key for r in recs], ["opencode:done"])
        # 水位线必须冻结在 pending 之前，不能被 done 的 ts=2000 推过去
        self.assertEqual(max_ts, 0)

        # opencode 把 pending 原地 UPDATE 成真实 token 值，time_created 不变
        conn = sqlite3.connect(str(self.db))
        conn.execute(
            "UPDATE message SET data = ? WHERE id = ?",
            (json.dumps(_assistant(inp=30, out=15)), "pending"),
        )
        conn.commit()
        conn.close()

        # 下一轮从冻结的水位线重新扫：pending 现在能读到真实值；done 被重复读到，
        # 交由 DB 层 dedup_key 去重，不会重复计数
        recs2, max_ts2 = opencode.fetch_records(self.db, since_ts_ms=max_ts)
        keys = sorted(r.dedup_key for r in recs2)
        self.assertEqual(keys, ["opencode:done", "opencode:pending"])
        pending_rec = next(r for r in recs2 if r.dedup_key == "opencode:pending")
        self.assertEqual(pending_rec.input_tokens, 30)
        self.assertEqual(max_ts2, 2000)

    def test_explicit_total_respected(self):
        _make_db(self.db, [
            ("m1", "s1", 1000, _assistant(inp=100, out=50, total=999)),
        ])
        recs, _ = opencode.fetch_records(self.db, 0)
        self.assertEqual(recs[0].total_tokens, 999)

    def test_locked_db_returns_empty_instead_of_raising(self):
        # opencode 正在写事务持锁时，_open_ro 的 connect() 本身不会报错
        # （只读连接、SQLite 懒加锁），真正的锁冲突在第一次 execute() 才暴露。
        # 之前 fetch_records() 只包了 connect()，execute() 没有 except，会让
        # sqlite3.OperationalError 一路冒出去，级联影响同批次排在后面的来源。
        # 用 mock 直接让 execute() 抛错，而不是真起一个持锁连接等 busy_timeout
        # （那样每条用例要空等 5 秒，拖慢整个套件）。
        _make_db(self.db, [("m1", "s1", 1000, _assistant(inp=10, out=5))])
        real_connect = sqlite3.connect

        class _LockedOnExecute:
            """sqlite3.Connection 是 C 类型，实例属性只读，包一层代理来伪造
            "connect 成功、第一次 execute 才炸"这个真实时序。"""

            def __init__(self, real):
                self._real = real

            def execute(self, *a, **kw):
                raise sqlite3.OperationalError("database is locked")

            def close(self):
                self._real.close()

        def fake_connect(*args, **kwargs):
            return _LockedOnExecute(real_connect(*args, **kwargs))

        with unittest.mock.patch.object(opencode.sqlite3, "connect", side_effect=fake_connect):
            recs, max_ts = opencode.fetch_records(self.db, since_ts_ms=0)
        self.assertEqual(recs, [])
        self.assertEqual(max_ts, 0)  # 原样返回起点，下一轮重试

    def test_missing_db_returns_empty(self):
        recs, max_ts = opencode.fetch_records(Path(self.tmp.name) / "nope.db", 42)
        self.assertEqual(recs, [])
        self.assertEqual(max_ts, 42)

    def assertRecords(self, n):
        recs, max_ts = opencode.fetch_records(self.db, 0)
        self.assertEqual(len(recs), n)
        return recs, max_ts


if __name__ == "__main__":
    unittest.main()
