import unittest
from datetime import date

from tokenstat import pricing


class TestNormalization(unittest.TestCase):
    def setUp(self):
        self.p = pricing.load_pricing()

    def test_opus_family(self):
        for m in ("claude-opus-4-5", "claude-opus-4-6", "claude-opus-4-7",
                  "claude-opus-4-8", "claude-opus-5"):
            r = pricing.rates_for_model(m, self.p)
            self.assertEqual(r["input"], 5.0, m)
            self.assertEqual(r["output"], 25.0, m)

    def test_opus_5_has_explicit_entry_and_leads_family_fallback(self):
        # 显式条目，非兜底命中
        self.assertFalse(pricing.is_unknown_model("claude-opus-5", self.p))
        self.assertIn("claude-opus-5", self.p["anthropic"])
        # 未来 opus 版本走家族兜底时应退到 opus-5（最新），而非旧版
        r = pricing.rates_for_model("claude-opus-6", self.p)
        self.assertEqual(r["input"], self.p["anthropic"]["claude-opus-5"]["input"])
        self.assertEqual(r["output"], self.p["anthropic"]["claude-opus-5"]["output"])

    def test_opus_old_pricing(self):
        r = pricing.rates_for_model("claude-opus-4-1", self.p)
        self.assertEqual(r["input"], 15.0)
        self.assertEqual(r["output"], 75.0)

    def test_sonnet_haiku(self):
        self.assertEqual(pricing.rates_for_model("claude-sonnet-4-6", self.p)["input"], 3.0)
        self.assertEqual(pricing.rates_for_model("claude-haiku-4-5", self.p)["input"], 1.0)

    def test_sonnet_5(self):
        # 官方已取消原定 2026-09-01 的涨价，$2/$10 就是标准价：跨 9/1 不得变动
        for when in (date(2026, 8, 31), date(2026, 9, 1), date(2027, 1, 1)):
            r = pricing.rates_for_model("claude-sonnet-5", self.p, priced_at=when)
            self.assertEqual(r["input"], 2.0, when)
            self.assertEqual(r["cache_read"], 0.20, when)
            self.assertEqual(r["output"], 10.0, when)
            self.assertEqual(r["cache_write"], 2.50, when)
            r1h = pricing.rates_for_model(
                "claude-sonnet-5", self.p, cache_window="1h", priced_at=when
            )
            self.assertEqual(r1h["cache_write"], 4.0, when)

    def test_unmatched_sonnet_family_falls_back_to_sonnet_5_not_4_6(self):
        # 未来未收录的 sonnet 变体（不含 claude-sonnet-5 前缀）应通过家族兜底
        # 归到最新的 sonnet-5，而非旧的 sonnet-4-6
        r = pricing.rates_for_model("claude-sonnet-6", self.p, priced_at=date(2026, 9, 1))
        self.assertEqual(r["input"], 2.0)

    def test_region_prefix_and_suffix_stripped(self):
        r = pricing.rates_for_model("us.anthropic.claude-opus-4-8[1m]", self.p)
        self.assertEqual(r["input"], 5.0)

    def test_gpt5_codex_and_auto_review_have_distinct_pricing(self):
        codex = pricing.rates_for_model("gpt-5-codex", self.p)
        review = pricing.rates_for_model("codex-auto-review", self.p)
        self.assertEqual((codex["input"], codex["cache_read"], codex["output"]), (1.25, 0.125, 10.0))
        self.assertEqual((review["input"], review["cache_read"], review["output"]), (1.75, 0.175, 14.0))

    def test_gpt5_versioned(self):
        self.assertEqual(pricing.rates_for_model("gpt-5.4", self.p)["input"], 2.5)

    def test_gpt56_flagship_pricing(self):
        sol = pricing.rates_for_model("gpt-5.6-sol", self.p, priced_at=date(2026, 8, 20))
        self.assertEqual(sol["input"], 5.0)
        self.assertEqual(sol["output"], 30.0)
        for model, short, long in (
            ("gpt-5.6-terra", (2.0, 0.20, 2.50, 12.0), (4.0, 0.40, 5.0, 18.0)),
            ("gpt-5.6-luna", (0.20, 0.02, 0.25, 1.20), (0.40, 0.04, 0.50, 1.80)),
        ):
            rates = pricing.rates_for_model(model, self.p, cache_window="30m")
            long_rates = pricing.rates_for_model(
                model, self.p, cache_window="30m", long_context=True
            )
            self.assertEqual(
                (rates["input"], rates["cache_read"], rates["cache_write"], rates["output"]), short
            )
            self.assertEqual(
                (long_rates["input"], long_rates["cache_read"], long_rates["cache_write"], long_rates["output"]), long
            )

    def test_daybreak_blue_uses_sol_rates(self):
        # OpenAI: gpt-daybreak-blue-latest 是 gpt-5.6-sol 别名，同价，不能掉 default
        for model in ("gpt-daybreak-blue", "gpt-daybreak-blue-latest"):
            self.assertFalse(pricing.is_unknown_model(model, self.p))
            before = pricing.rates_for_model(
                model, self.p, cache_window="30m", priced_at=date(2026, 8, 20)
            )
            after = pricing.rates_for_model(
                model, self.p, cache_window="30m", priced_at=date(2026, 8, 21)
            )
            self.assertEqual(
                (before["input"], before["cache_read"], before["cache_write"], before["output"]),
                (5.0, 0.50, 6.25, 30.0),
            )
            self.assertEqual(
                (after["input"], after["cache_read"], after["cache_write"], after["output"]),
                (4.0, 0.40, 5.0, 20.0),
            )

    def test_gpt56_sol_price_cut_2026_08_21(self):
        # OpenAI 于 2026-08-21 下调 Sol：$5/$30 → $4/$20，历史行必须仍按旧价
        before = pricing.rates_for_model(
            "gpt-5.6-sol", self.p, cache_window="30m", priced_at=date(2026, 8, 20)
        )
        after = pricing.rates_for_model(
            "gpt-5.6-sol", self.p, cache_window="30m", priced_at=date(2026, 8, 21)
        )
        self.assertEqual(
            (before["input"], before["cache_read"], before["cache_write"], before["output"]),
            (5.0, 0.50, 6.25, 30.0),
        )
        self.assertEqual(
            (after["input"], after["cache_read"], after["cache_write"], after["output"]),
            (4.0, 0.40, 5.0, 20.0),
        )
        long_after = pricing.rates_for_model(
            "gpt-5.6-sol", self.p, cache_window="30m", long_context=True,
            priced_at=date(2026, 8, 21),
        )
        self.assertEqual(
            (long_after["input"], long_after["cache_read"], long_after["cache_write"], long_after["output"]),
            (8.0, 0.80, 10.0, 30.0),
        )
        # 降价后长上下文阈值不能丢
        self.assertEqual(
            pricing.long_context_threshold_for_model("gpt-5.6-sol", self.p, date(2026, 8, 21)),
            272000,
        )

    def test_mythos_same_as_fable(self):
        r = pricing.rates_for_model("claude-mythos-5", self.p)
        self.assertEqual(r["input"], 10.0)
        self.assertEqual(r["output"], 50.0)

    def test_fable_5_1_cheaper_cache_read_than_5(self):
        # Fable/Mythos 5.1 的 input/output/cache_write 与 5 代相同，
        # 只有 cache_read 从 0.1x（$1）降到 0.025x（$0.25）
        for m in ("claude-fable-5-1", "claude-mythos-5-1"):
            r = pricing.rates_for_model(m, self.p)
            self.assertEqual((r["input"], r["output"], r["cache_write"]), (10.0, 50.0, 12.50), m)
            self.assertEqual(r["cache_read"], 0.25, m)
        # 5 代精确条目必须保持原样，不能被 5.1 的兜底顺序污染
        old = pricing.rates_for_model("claude-fable-5", self.p)
        self.assertEqual(old["cache_read"], 1.00)
        # 未来未收录的 fable 版本应兜底到最新的 5.1，而非旧的 5
        future = pricing.rates_for_model("claude-fable-6", self.p)
        self.assertEqual(future["cache_read"], 0.25)
        self.assertFalse(pricing.is_unknown_model("claude-fable-5-1", self.p))

    def test_grok_4_6_priced_explicitly(self):
        # grok-4.6 必须走精确价目（cache_read $0.50），不能再兜底套 4.5 的 $0.30
        r = pricing.rates_for_model("grok-4.6", self.p)
        self.assertEqual((r["input"], r["cache_read"], r["output"]), (2.0, 0.50, 6.0))
        long = pricing.rates_for_model("grok-4.6", self.p, long_context=True)
        self.assertEqual((long["input"], long["cache_read"], long["output"]), (4.0, 1.00, 12.0))
        self.assertFalse(pricing.is_unknown_model("grok-4.6", self.p))
        # 家族兜底应指向最新的 4.6，而不是旧版本
        future = pricing.rates_for_model("grok-4.7", self.p)
        self.assertEqual((future["input"], future["cache_read"], future["output"]), (2.0, 0.50, 6.0))

    def test_grok_pricing(self):
        r = pricing.rates_for_model("grok-4.5", self.p)
        self.assertEqual(r["input"], 2.0)
        self.assertEqual(r["output"], 6.0)
        self.assertEqual(r["cache_read"], 0.30)
        long = pricing.rates_for_model("grok-4.5", self.p, long_context=True)
        self.assertEqual((long["input"], long["cache_read"], long["output"]), (4.0, 0.60, 12.0))
        # 家族兜底
        fam = pricing.rates_for_model("grok-99-future", self.p)
        self.assertEqual(fam["input"], 2.0)

    def test_grok_and_deepseek_aliases(self):
        build = pricing.rates_for_model("grok-build-0.1", self.p)
        code_fast = pricing.rates_for_model("grok-code-fast-1", self.p)
        latest = pricing.rates_for_model("grok-latest", self.p)
        multi_agent = pricing.rates_for_model("grok-4.20-multi-agent-0309", self.p)
        self.assertEqual((code_fast["input"], code_fast["cache_read"], code_fast["output"]),
                         (build["input"], build["cache_read"], build["output"]))
        self.assertEqual((latest["input"], latest["cache_read"], latest["output"]), (1.25, 0.20, 2.50))
        self.assertEqual((multi_agent["input"], multi_agent["cache_read"], multi_agent["output"]),
                         (1.25, 0.20, 2.50))
        for alias in ("deepseek-chat", "deepseek-reasoner"):
            r = pricing.rates_for_model(alias, self.p)
            self.assertEqual((r["input"], r["cache_read"], r["output"]), (0.14, 0.0028, 0.28), alias)

    def test_openai_30m_cache_write_and_pro_cache_read(self):
        before = date(2026, 8, 20)
        sol = pricing.rates_for_model("gpt-5.6-sol", self.p, cache_window="30m", priced_at=before)
        self.assertEqual(sol["cache_write"], 6.25)
        long_sol = pricing.rates_for_model(
            "gpt-5.6-sol", self.p, cache_window="30m", long_context=True, priced_at=before
        )
        self.assertEqual(long_sol["cache_write"], 12.50)
        pro = pricing.rates_for_model("gpt-5.4-pro", self.p)
        self.assertEqual(pro["cache_read"], 30.0)
        long_pro = pricing.rates_for_model("gpt-5.4-pro", self.p, long_context=True)
        self.assertEqual(long_pro["cache_read"], 60.0)

    def test_cache_window_1h(self):
        r5 = pricing.rates_for_model("claude-opus-4-7", self.p, cache_window="5m")
        r1 = pricing.rates_for_model("claude-opus-4-7", self.p, cache_window="1h")
        self.assertEqual(r5["cache_write"], 6.25)
        self.assertEqual(r1["cache_write"], 10.0)

    def test_30m_cache_window_falls_back_to_5m_not_zero(self):
        # claude-opus-4-7 没配 cache_write_30m。旧写法里 cw_key 本身就是
        # "cache_write_30m"，兜底条件恒假，永远拿不到值，最后静默算成 0。
        r = pricing.rates_for_model("claude-opus-4-7", self.p, cache_window="30m")
        self.assertEqual(r["cache_write"], 6.25)  # 退到该模型的 5m 价，不是 0
        # grok 系列价表里 cache_write_5m/1h 都显式是 null（xAI 缓存没有独立写入
        # 计费），任何窗口都应该是 0——这是价表本身的意图，不是兜底没生效。
        rg = pricing.rates_for_model("grok-4.6", self.p, cache_window="30m")
        self.assertEqual(rg["cache_write"], 0.0)
        # 有 30m 价目的模型（gpt-5.6-sol）请求 30m 时必须精确命中，不受兜底影响
        # （用降价前的历史日期锚定，避免和 next_pricing 生效日期产生歧义）
        sol = pricing.rates_for_model(
            "gpt-5.6-sol", self.p, cache_window="30m", priced_at=date(2026, 8, 1)
        )
        self.assertEqual(sol["cache_write"], self.p["openai"]["gpt-5.6-sol"]["cache_write_30m"])


class TestCost(unittest.TestCase):
    def setUp(self):
        self.p = pricing.load_pricing()

    def test_pure_input(self):
        # 1M input @ opus = $5
        c = pricing.cost_for("claude-opus-4-7", input_tokens=1_000_000, pricing=self.p)
        self.assertAlmostEqual(c, 5.0, places=6)

    def test_mixed(self):
        # opus: 1M in($5) + 1M out($25) + 1M cache_read($0.5) + 1M cache_write_5m($6.25)
        c = pricing.cost_for(
            "claude-opus-4-7",
            input_tokens=1_000_000,
            output_tokens=1_000_000,
            cache_read_tokens=1_000_000,
            cache_creation_tokens=1_000_000,
            pricing=self.p,
        )
        self.assertAlmostEqual(c, 5.0 + 25.0 + 0.5 + 6.25, places=6)

    def test_reasoning_not_double_charged(self):
        # reasoning 是 output 子集，不另计费
        c = pricing.cost_for(
            "gpt-5.4", output_tokens=1_000_000, reasoning_tokens=500_000, pricing=self.p
        )
        self.assertAlmostEqual(c, 15.0, places=6)


class TestUnknownAndFallback(unittest.TestCase):
    def setUp(self):
        pricing.clear_unknown_models()

    def tearDown(self):
        pricing.clear_unknown_models()

    def test_unknown_recorded(self):
        p = pricing.load_pricing()
        pricing.rates_for_model("totally-made-up-model-zzz", p)
        self.assertIn("totally-made-up-model-zzz", pricing.unknown_models())

    def test_clear_unknown_models_resets_process_state(self):
        p = pricing.load_pricing()
        pricing.rates_for_model("one-off-unknown-model", p)
        self.assertIn("one-off-unknown-model", pricing.unknown_models())
        pricing.clear_unknown_models()
        self.assertEqual(pricing.unknown_models(), [])

    def test_is_unknown_model_does_not_use_global_state(self):
        p = pricing.load_pricing()
        pricing.rates_for_model("claude-known-later", {"default": p["default"], "anthropic": {}, "openai": {}})
        local = {"default": p["default"], "anthropic": {"claude-known-later": p["default"]}, "openai": {}}
        self.assertFalse(pricing.is_unknown_model("claude-known-later", local))

    def test_missing_file_fallback(self):
        p = pricing.load_pricing("/nonexistent/pricing.json")
        # 回退结构带 default
        self.assertIn("default", p)


class TestLongContextThresholds(unittest.TestCase):
    def test_includes_both_base_and_next_pricing_thresholds(self):
        # 构造一个"涨价/降价顺带改了长上下文门槛"的模型：基础阈值 100000，
        # next_pricing 改成 200000。旧写法 long_context_thresholds() 不接受
        # priced_at、永远只按"今天"生效价取阈值，会漏掉另一个阈值对应的 SQL
        # 列，导致按历史阈值本该判长上下文的行找不到列、静默按基础价算。
        p = {
            "default": {"input": 1.0, "output": 1.0, "cache_read": 0.1,
                        "cache_write_5m": None, "cache_write_1h": None},
            "anthropic": {
                "model-a": {
                    "input": 1.0, "output": 1.0, "cache_read": 0.1,
                    "cache_write_5m": None, "cache_write_1h": None,
                    "long_context": {"threshold": 100000, "input": 2.0, "output": 2.0},
                    "next_pricing": {
                        "starts_on": "2026-01-01",
                        "input": 1.5, "output": 1.5,
                        "long_context": {"threshold": 200000, "input": 3.0, "output": 3.0},
                    },
                },
            },
            "openai": {}, "deepseek": {}, "xai": {}, "local": {},
        }
        thresholds = pricing.long_context_thresholds(p)
        self.assertIn(100000, thresholds)
        self.assertIn(200000, thresholds)

        # 历史日期（next_pricing 生效前）应判到旧阈值 100000
        old = pricing.long_context_threshold_for_model("model-a", p, date(2025, 6, 1))
        self.assertEqual(old, 100000)
        # 生效后应判到新阈值 200000
        new = pricing.long_context_threshold_for_model("model-a", p, date(2026, 6, 1))
        self.assertEqual(new, 200000)

    def test_no_next_pricing_returns_single_threshold(self):
        p = {
            "default": {"input": 1.0, "output": 1.0, "cache_read": 0.1,
                        "cache_write_5m": None, "cache_write_1h": None},
            "anthropic": {
                "model-b": {
                    "input": 1.0, "output": 1.0, "cache_read": 0.1,
                    "cache_write_5m": None, "cache_write_1h": None,
                    "long_context": {"threshold": 50000, "input": 2.0, "output": 2.0},
                },
            },
            "openai": {}, "deepseek": {}, "xai": {}, "local": {},
        }
        self.assertEqual(pricing.long_context_thresholds(p), (50000,))

    def test_real_pricing_json_thresholds_are_consistent(self):
        # 冒烟测试：真实价表里跑一遍，不该抛异常，且当前生产价表里新旧阈值
        # 恰好没变过，历史/今天判到的阈值应该一致。
        p = pricing.load_pricing()
        thresholds = pricing.long_context_thresholds(p)
        self.assertTrue(all(isinstance(t, int) for t in thresholds))
        for model in ("grok-4.6", "gpt-5.6-sol"):
            today_th = pricing.long_context_threshold_for_model(model, p)
            old_th = pricing.long_context_threshold_for_model(model, p, date(2025, 1, 1))
            self.assertEqual(today_th, old_th)
            self.assertIn(today_th, thresholds)


if __name__ == "__main__":
    unittest.main()
