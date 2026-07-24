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
        r = pricing.rates_for_model("claude-sonnet-5", self.p, priced_at=date(2026, 8, 31))
        self.assertEqual(r["input"], 2.0)
        self.assertEqual(r["cache_read"], 0.20)
        self.assertEqual(r["output"], 10.0)
        self.assertEqual(r["cache_write"], 2.50)
        r = pricing.rates_for_model(
            "claude-sonnet-5", self.p, cache_window="1h", priced_at=date(2026, 8, 31)
        )
        self.assertEqual(r["cache_write"], 4.0)
        r = pricing.rates_for_model("claude-sonnet-5", self.p, priced_at=date(2026, 9, 1))
        self.assertEqual(r["input"], 3.0)
        self.assertEqual(r["cache_read"], 0.30)
        self.assertEqual(r["output"], 15.0)
        self.assertEqual(r["cache_write"], 3.75)
        r = pricing.rates_for_model(
            "claude-sonnet-5", self.p, cache_window="1h", priced_at=date(2026, 9, 1)
        )
        self.assertEqual(r["cache_write"], 6.0)

    def test_unmatched_sonnet_family_falls_back_to_sonnet_5_not_4_6(self):
        # 未来未收录的 sonnet 变体（不含 claude-sonnet-5 前缀）应通过家族兜底
        # 归到最新的 sonnet-5，而非旧的 sonnet-4-6
        r = pricing.rates_for_model("claude-sonnet-6", self.p, priced_at=date(2026, 9, 1))
        self.assertEqual(r["input"], 3.0)

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
        sol = pricing.rates_for_model("gpt-5.6-sol", self.p)
        terra = pricing.rates_for_model("gpt-5.6-terra", self.p)
        luna = pricing.rates_for_model("gpt-5.6-luna", self.p)
        self.assertEqual(sol["input"], 5.0)
        self.assertEqual(sol["output"], 30.0)
        self.assertEqual(terra["input"], 2.5)
        self.assertEqual(terra["output"], 15.0)
        self.assertEqual(luna["input"], 1.0)
        self.assertEqual(luna["output"], 6.0)

    def test_mythos_same_as_fable(self):
        r = pricing.rates_for_model("claude-mythos-5", self.p)
        self.assertEqual(r["input"], 10.0)
        self.assertEqual(r["output"], 50.0)

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
        sol = pricing.rates_for_model("gpt-5.6-sol", self.p, cache_window="30m")
        self.assertEqual(sol["cache_write"], 6.25)
        long_sol = pricing.rates_for_model("gpt-5.6-sol", self.p, cache_window="30m", long_context=True)
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


if __name__ == "__main__":
    unittest.main()
