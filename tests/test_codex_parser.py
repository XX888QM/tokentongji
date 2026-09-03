import unittest

from tokenstat.parsers import codex
from tokenstat.parsers.codex import CodexState


def _session_meta(cwd, sid="sess-uuid", forked_from=None):
    payload = {"id": sid, "cwd": cwd}
    if forked_from:
        payload["forked_from_id"] = forked_from
        payload["parent_thread_id"] = forked_from
    return {"type": "session_meta", "payload": payload}


def _turn_context(model, cwd):
    return {"type": "turn_context", "payload": {"model": model, "cwd": cwd}}


def _token_count(
    total, input_t, cached, output, reasoning=0, ts="2026-05-01T10:00:00Z", last=None
):
    info = {
        "total_token_usage": {
            "input_tokens": input_t,
            "cached_input_tokens": cached,
            "output_tokens": output,
            "reasoning_output_tokens": reasoning,
            "total_tokens": total,
        }
    }
    if last is not None:
        info["last_token_usage"] = last
    return {
        "type": "event_msg",
        "timestamp": ts,
        "payload": {
            "type": "token_count",
            "info": info,
            "rate_limits": {"some": "thing"},
        },
    }


def _heartbeat():
    return {"type": "event_msg", "payload": {"type": "token_count", "info": None, "rate_limits": {}}}


def _claude_mem_usage(event_id="thread-1"):
    return {
        "type": "claude_mem.codex_usage",
        "schema_version": 1,
        "timestamp": "2026-07-27T08:58:18.000Z",
        "event_id": event_id,
        "model": "gpt-5.6-luna",
        "project": "/Users/yunxin/Desktop/开发/claude-mem",
        "session_id": "session-1",
        "usage": {
            "input_tokens": 22738,
            "cached_input_tokens": 6912,
            "cache_write_input_tokens": 0,
            "output_tokens": 46,
            "reasoning_output_tokens": 39,
        },
    }


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

    def test_last_token_usage_marks_real_request_prompt(self):
        codex.process_record(_turn_context("gpt-5.4", "/c"), "/f", 0, self.state)
        rec = codex.process_record(
            _token_count(
                1000,
                600,
                200,
                400,
                last={"input_tokens": 300_000, "cached_input_tokens": 280_000},
            ),
            "/f",
            1,
            self.state,
        )
        self.assertEqual(rec.request_prompt_tokens, 300_000)

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

    def test_fork_first_snapshot_is_baseline_not_delta(self):
        # fork 文件：首条 token_count 继承父会话累积量（上亿级），只作基线
        codex.process_record(_session_meta("/c", sid="child", forked_from="parent"), "/f", 0, self.state)
        codex.process_record(_turn_context("gpt-5.4", "/c"), "/f", 1, self.state)
        inherited = codex.process_record(
            _token_count(135_000_000, 134_000_000, 132_000_000, 1_000_000), "/f", 2, self.state
        )
        self.assertIsNone(inherited)  # 父会话的量不重复计
        # 之后的差分基于继承基线，正常计增量
        rec = codex.process_record(
            _token_count(135_000_500, 134_000_300, 132_000_100, 1_000_200), "/f", 3, self.state
        )
        self.assertEqual(rec.output_tokens, 200)
        self.assertEqual(rec.cache_read_tokens, 100)
        self.assertEqual(rec.total_tokens, 500)

    def test_midfile_sid_change_keeps_baseline(self):
        # 同一文件内交错出现父线程 meta：sid 变化不重置基线（计数器连续）
        codex.process_record(_session_meta("/c", sid="child", forked_from="parent"), "/f", 0, self.state)
        codex.process_record(_turn_context("gpt-5.4", "/c"), "/f", 1, self.state)
        codex.process_record(_token_count(1000, 600, 200, 400), "/f", 2, self.state)  # 基线
        codex.process_record(_token_count(1500, 800, 300, 700), "/f", 3, self.state)
        # 中段插入父线程 session_meta（无 fork 标记）
        codex.process_record(_session_meta("/c", sid="parent"), "/f", 4, self.state)
        rec = codex.process_record(_token_count(1800, 1000, 400, 800), "/f", 5, self.state)
        self.assertIsNotNone(rec)
        self.assertEqual(rec.total_tokens, 300)  # 1800-1500，而非从 0 重计 1800
        self.assertEqual(rec.session_id, "parent")  # 归属跟着 sid 走
        # 中段再出现带 fork 标记的 meta：基线已建立，不触发 baseline 跳过
        codex.process_record(_session_meta("/c", sid="child2", forked_from="parent"), "/f", 6, self.state)
        rec2 = codex.process_record(_token_count(2000, 1100, 450, 900), "/f", 7, self.state)
        self.assertIsNotNone(rec2)
        self.assertEqual(rec2.total_tokens, 200)

    def test_pending_baseline_ctx_roundtrip(self):
        # fork meta 与首条 token_count 被 ingest 分在两个批次时，标记必须跨批次存活
        codex.process_record(_session_meta("/c", sid="child", forked_from="parent"), "/f", 0, self.state)
        s2 = CodexState.from_ctx(self.state.to_ctx(), default_model="gpt-5.5")
        self.assertTrue(s2.pending_baseline)
        inherited = codex.process_record(_token_count(135_000_000, 134_000_000, 132_000_000, 1_000_000), "/f", 1, s2)
        self.assertIsNone(inherited)

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

    def test_claude_mem_ephemeral_usage_is_an_observer_record(self):
        rec = codex.process_record(_claude_mem_usage(), "/usage/codex-usage-2026-07-27.jsonl", 0, self.state)
        self.assertIsNotNone(rec)
        self.assertEqual(rec.source, "codex")
        self.assertEqual(rec.category, "observer")
        self.assertEqual(rec.input_tokens, 15826)  # 22738 - 6912 cached
        self.assertEqual(rec.cache_read_tokens, 6912)
        self.assertEqual(rec.output_tokens, 46)
        self.assertEqual(rec.reasoning_tokens, 39)  # output 子集，不重复计入 total
        self.assertEqual(rec.total_tokens, 22784)
        self.assertEqual(rec.request_prompt_tokens, 22738)
        self.assertEqual(rec.dedup_key, "claude-mem-codex:thread-1")

    def test_compaction_input_field_drop_recovered_via_total_anchor(self):
        # docstring 明确说明 compaction 时 input 子字段会回落，不能逐字段相减，
        # 要锚定单调的 total_tokens 反推。这里构造一次真实的 compaction：原始
        # input 字段从 800_000 掉到 300_000（上下文被压缩摘要），但 total 仍从
        # 1_500_000 涨到 1_580_000（+80_000），output 从 500_000 涨到 530_000
        # （+30_000）。若有人"简化"成直接 max(0, cur.input - prev.input)，
        # 会把这轮新增的 input 算成 0，白白漏掉 50_000 token。
        codex.process_record(_turn_context("gpt-5.4", "/c"), "/f", 0, self.state)
        codex.process_record(_token_count(1_500_000, 1_000_000, 200_000, 500_000), "/f", 1, self.state)
        rec = codex.process_record(_token_count(1_580_000, 700_000, 150_000, 530_000), "/f", 2, self.state)
        self.assertIsNotNone(rec)
        self.assertEqual(rec.output_tokens, 30_000)          # 530_000 - 500_000
        self.assertEqual(rec.total_tokens, 80_000)            # 锚定 total 的真实增量
        # total-锚定法：d_input_total = d_total - d_output = 80_000 - 30_000 = 50_000
        # cached 字段同样被 compaction 拉低(200_000 -> 150_000)，差值 floor 到 0
        self.assertEqual(rec.cache_read_tokens, 0)
        self.assertEqual(rec.input_tokens, 50_000)            # fresh_input，不是被漏计的 0
        self.assertEqual(rec.input_tokens + rec.cache_read_tokens + rec.output_tokens, rec.total_tokens)

    def test_claude_mem_cache_write_input_tokens_not_double_counted(self):
        # docstring/注释都明确说 cache_write_input_tokens 原样留在来源 JSONL，
        # 不重复计入 total/cache_creation，但现有测试全部固定传 0，没验证过
        # 非零值真的不会被算进去。
        obj = _claude_mem_usage("cache-write-nonzero")
        obj["usage"]["cache_write_input_tokens"] = 5000
        rec = codex.process_record(obj, "/usage/codex.jsonl", 0, self.state)
        self.assertIsNotNone(rec)
        self.assertEqual(rec.cache_creation_tokens, 0)
        self.assertEqual(rec.total_tokens, 22738 + 46)  # 不含 cache_write_input_tokens

    def test_claude_mem_usage_rejects_invalid_cache_or_reasoning_counts(self):
        bad_cache = _claude_mem_usage("bad-cache")
        bad_cache["usage"]["cached_input_tokens"] = 22739
        self.assertIsNone(codex.process_record(bad_cache, "/usage/codex.jsonl", 0, self.state))
        bad_reasoning = _claude_mem_usage("bad-reasoning")
        bad_reasoning["usage"]["reasoning_output_tokens"] = 47
        self.assertIsNone(codex.process_record(bad_reasoning, "/usage/codex.jsonl", 0, self.state))


if __name__ == "__main__":
    unittest.main()
