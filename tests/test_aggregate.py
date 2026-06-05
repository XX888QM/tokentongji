import time
import unittest
from datetime import date

from tokenstat import aggregate, db, pricing
from tokenstat.models import UsageRecord


class TestPeriodRange(unittest.TestCase):
    def test_today(self):
        t = date(2026, 6, 6)  # 周六
        self.assertEqual(aggregate.period_range("today", t), ("2026-06-06", "2026-06-06"))

    def test_week_monday_to_today(self):
        t = date(2026, 6, 6)  # 周六, 本周一=2026-06-01
        self.assertEqual(aggregate.period_range("week", t), ("2026-06-01", "2026-06-06"))

    def test_month(self):
        t = date(2026, 6, 6)
        self.assertEqual(aggregate.period_range("month", t), ("2026-06-01", "2026-06-06"))

    def test_bad_period(self):
        with self.assertRaises(ValueError):
            aggregate.period_range("year")


class TestAggregateQueries(unittest.TestCase):
    def setUp(self):
        self.conn = db.get_conn(":memory:")
        db.init_db(self.conn)
        self.pricing = pricing.load_pricing()
        now = int(time.time())
        recs = [
            UsageRecord(ts=now, source="claude", model="claude-opus-4-7",
                        project="/Users/yunxin/projA", input_tokens=1_000_000,
                        output_tokens=0, total_tokens=1_000_000, dedup_key="c1"),
            UsageRecord(ts=now, source="claude", model="claude-sonnet-4-6",
                        project="/Users/yunxin/projB", input_tokens=2_000_000,
                        output_tokens=0, total_tokens=2_000_000, dedup_key="c2"),
            UsageRecord(ts=now, source="codex", model="gpt-5.4",
                        project="/Users/yunxin/projA", input_tokens=500_000,
                        output_tokens=100_000, total_tokens=600_000, dedup_key="x1"),
        ]
        db.insert_records(self.conn, recs, on_conflict="ignore")

    def tearDown(self):
        self.conn.close()

    def test_summary_total(self):
        s = aggregate.summary(self.conn, self.pricing)
        # 今日总量 = 1M + 2M + 0.6M
        self.assertEqual(s["periods"]["today"]["total"], 3_600_000)
        self.assertEqual(s["periods"]["today"]["by_source"]["claude"]["total"], 3_000_000)
        self.assertEqual(s["periods"]["today"]["by_source"]["codex"]["total"], 600_000)

    def test_summary_cost(self):
        s = aggregate.summary(self.conn, self.pricing)
        # claude: 1M opus input($5) + 2M sonnet input($6) = $11; codex: 0.5M*2.5 + 0.1M*15 = $1.25+$1.5=$2.75
        self.assertAlmostEqual(s["periods"]["today"]["by_source"]["claude"]["cost_usd"], 11.0, places=4)
        self.assertAlmostEqual(s["periods"]["today"]["by_source"]["codex"]["cost_usd"], 2.75, places=4)

    def test_breakdown_by_model_and_project(self):
        b = aggregate.breakdown(self.conn, "today", self.pricing)
        self.assertEqual(len(b["by_model"]), 3)
        # 按 total 降序，sonnet 2M 居首
        self.assertEqual(b["by_model"][0]["model"], "claude-sonnet-4-6")
        # 项目 projA 出现两次（claude + codex 不同 source 分行）
        names = {p["project"] for p in b["by_project"]}
        self.assertIn("projA", names)
        self.assertIn("projB", names)

    def test_daily_includes_today(self):
        d = aggregate.daily(self.conn, days=7)
        self.assertEqual(len(d["days"]), 7)
        today = d["days"][-1]
        self.assertEqual(today["claude"], 3_000_000)
        self.assertEqual(today["codex"], 600_000)

    def test_meta(self):
        m = aggregate.meta(self.conn)
        self.assertEqual(m["total_events"], 3)


if __name__ == "__main__":
    unittest.main()
