import unittest

from tokenstat import pricing


class TestNormalization(unittest.TestCase):
    def setUp(self):
        self.p = pricing.load_pricing()

    def test_opus_family(self):
        for m in ("claude-opus-4-5", "claude-opus-4-6", "claude-opus-4-7", "claude-opus-4-8"):
            r = pricing.rates_for_model(m, self.p)
            self.assertEqual(r["input"], 5.0, m)
            self.assertEqual(r["output"], 25.0, m)

    def test_opus_old_pricing(self):
        r = pricing.rates_for_model("claude-opus-4-1", self.p)
        self.assertEqual(r["input"], 15.0)
        self.assertEqual(r["output"], 75.0)

    def test_sonnet_haiku(self):
        self.assertEqual(pricing.rates_for_model("claude-sonnet-4-6", self.p)["input"], 3.0)
        self.assertEqual(pricing.rates_for_model("claude-haiku-4-5", self.p)["input"], 1.0)

    def test_sonnet_5(self):
        r = pricing.rates_for_model("claude-sonnet-5", self.p)
        self.assertEqual(r["input"], 3.0)
        self.assertEqual(r["output"], 15.0)

    def test_unmatched_sonnet_family_falls_back_to_sonnet_5_not_4_6(self):
        # 未来未收录的 sonnet 变体（不含 claude-sonnet-5 前缀）应通过家族兜底
        # 归到最新的 sonnet-5，而非旧的 sonnet-4-6
        r = pricing.rates_for_model("claude-sonnet-6", self.p)
        self.assertEqual(r["input"], 3.0)

    def test_region_prefix_and_suffix_stripped(self):
        r = pricing.rates_for_model("us.anthropic.claude-opus-4-8[1m]", self.p)
        self.assertEqual(r["input"], 5.0)

    def test_gpt5_codex_uses_codex_specialized_pricing(self):
        for model in ("gpt-5-codex", "codex-auto-review"):
            r = pricing.rates_for_model(model, self.p)
            self.assertEqual(r["input"], 1.75, model)
            self.assertEqual(r["cache_read"], 0.175, model)
            self.assertEqual(r["output"], 14.0, model)

    def test_gpt5_versioned(self):
        self.assertEqual(pricing.rates_for_model("gpt-5.4", self.p)["input"], 2.5)

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
