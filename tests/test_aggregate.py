import time
import unittest
from datetime import date, datetime
from unittest.mock import patch

from tokenstat import aggregate, db, pricing
from tokenstat.models import _LOCAL_TZ, UsageRecord


def _ts(day: str) -> int:
    return int(datetime.fromisoformat(day).replace(tzinfo=_LOCAL_TZ).timestamp())


class TestPeriodRange(unittest.TestCase):
    def test_today(self):
        t = date(2026, 6, 6)  # 周六
        self.assertEqual(aggregate.period_range("today", t), ("2026-06-06", "2026-06-06"))

    def test_yesterday(self):
        t = date(2026, 6, 6)
        self.assertEqual(aggregate.period_range("yesterday", t), ("2026-06-05", "2026-06-05"))

    def test_week_monday_to_today(self):
        t = date(2026, 6, 6)  # 周六, 本周一=2026-06-01
        self.assertEqual(aggregate.period_range("week", t), ("2026-06-01", "2026-06-06"))

    def test_month(self):
        t = date(2026, 6, 6)
        self.assertEqual(aggregate.period_range("month", t), ("2026-06-01", "2026-06-06"))

    def test_year(self):
        t = date(2026, 6, 6)
        self.assertEqual(aggregate.period_range("year", t), ("2026-01-01", "2026-06-06"))

    def test_bad_period(self):
        with self.assertRaises(ValueError):
            aggregate.period_range("decade")


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
        # 今年应包含今日数据
        self.assertEqual(s["periods"]["year"]["total"], 3_600_000)
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

    def test_daily_includes_all_sources(self):
        conn = db.get_conn(":memory:")
        try:
            db.init_db(conn)
            db.insert_records(conn, [
                UsageRecord(ts=_ts("2026-06-06"), source="claude", model="known",
                            project="/tmp/a", input_tokens=10, total_tokens=10,
                            dedup_key="daily-claude"),
                UsageRecord(ts=_ts("2026-06-06"), source="codex", model="known",
                            project="/tmp/b", input_tokens=20, total_tokens=20,
                            dedup_key="daily-codex"),
                UsageRecord(ts=_ts("2026-06-06"), source="opencode", model="known",
                            project="/tmp/c", input_tokens=30, total_tokens=30,
                            dedup_key="daily-opencode"),
                UsageRecord(ts=_ts("2026-06-06"), source="openclaw", model="known",
                            project="/tmp/d", input_tokens=40, total_tokens=40,
                            dedup_key="daily-openclaw"),
            ])
            with patch("tokenstat.aggregate._today_local", return_value=date(2026, 6, 6)):
                d = aggregate.daily(conn, days=1)
        finally:
            conn.close()

        self.assertEqual(d["sources"], ["claude", "codex", "opencode", "openclaw"])
        day = d["days"][0]
        self.assertEqual(day["claude"], 10)
        self.assertEqual(day["codex"], 20)
        self.assertEqual(day["opencode"], 30)
        self.assertEqual(day["openclaw"], 40)
        self.assertEqual(day["total"], 100)

    def test_opencode_reasoning_counts_as_output(self):
        conn = db.get_conn(":memory:")
        pricing_for_test = {
            "default": {"input": 1, "output": 1, "cache_read": 1,
                        "cache_write_5m": 1, "cache_write_1h": 1},
            "anthropic": {},
            "openai": {},
            "deepseek": {},
        }
        try:
            db.init_db(conn)
            db.insert_records(conn, [
                UsageRecord(ts=_ts("2026-06-06"), source="opencode", model="deepseek-v4-pro",
                            project="/tmp/opencode", input_tokens=10_000_000, output_tokens=20_000_000,
                            reasoning_tokens=5_000_000, cache_read_tokens=7_000_000, total_tokens=42_000_000,
                            dedup_key="opencode-reasoning"),
            ])
            with patch("tokenstat.aggregate._today_local", return_value=date(2026, 6, 6)):
                s = aggregate.summary(conn, pricing_for_test)
                d = aggregate.daily(conn, days=1)
                b = aggregate.breakdown(conn, "today", pricing_for_test)
        finally:
            conn.close()

        self.assertEqual(s["periods"]["today"]["total"], 42_000_000)
        self.assertEqual(s["periods"]["today"]["by_source"]["opencode"]["total"], 42_000_000)
        self.assertAlmostEqual(s["periods"]["today"]["cost_usd"], 42.0, places=4)
        self.assertEqual(d["days"][0]["opencode"], 42_000_000)
        self.assertEqual(b["by_model"][0]["output"], 25_000_000)
        self.assertEqual(b["by_model"][0]["total"], 42_000_000)

    def test_meta(self):
        m = aggregate.meta(self.conn)
        self.assertEqual(m["total_events"], 3)


class TestTopSessions(unittest.TestCase):
    """回归：同 session_id 跨 source/model/project 时展示最大 token 分组的属性。"""

    def setUp(self):
        self.conn = db.get_conn(":memory:")
        db.init_db(self.conn)
        self.pricing = {'default': {'input': 1, 'output': 1, 'cache_read': 1,
                                    'cache_write_5m': 1, 'cache_write_1h': 1},
                        'anthropic': {}, 'openai': {}}
        now = int(time.time())
        db.insert_records(self.conn, [
            UsageRecord(ts=now, source='claude', model='model-small',
                        project='/tmp/project-small', input_tokens=1,
                        total_tokens=1, session_id='sid1', dedup_key='r1'),
            UsageRecord(ts=now, source='codex', model='model-big',
                        project='/tmp/project-big', input_tokens=100,
                        total_tokens=100, session_id='sid1', dedup_key='r2'),
        ])

    def tearDown(self):
        self.conn.close()

    def test_dominant_row_wins(self):
        sessions = aggregate.top_sessions(self.conn, 'today', self.pricing, 10)['sessions']
        self.assertEqual(len(sessions), 1)
        s = sessions[0]
        self.assertEqual(s['model'], 'model-big')
        self.assertEqual(s['source'], 'codex')
        self.assertEqual(s['project'], 'project-big')
        self.assertEqual(s['total'], 101)


class TestAuditAndInsights(unittest.TestCase):
    def setUp(self):
        self.conn = db.get_conn(":memory:")
        db.init_db(self.conn)
        self.pricing = {'default': {'input': 1, 'output': 1, 'cache_read': 1,
                                    'cache_write_5m': 1, 'cache_write_1h': 1},
                        'anthropic': {}, 'openai': {}}
        db.insert_records(self.conn, [
            UsageRecord(ts=_ts("2026-06-05"), source="claude", model="known",
                        project="/tmp/a", input_tokens=10, total_tokens=10,
                        session_id="s1", dedup_key="a1"),
            UsageRecord(ts=_ts("2026-06-06"), source="codex", model="known",
                        project="/tmp/b", input_tokens=100, output_tokens=20,
                        total_tokens=120, session_id="s2", dedup_key="b1"),
        ])
        db.set_ingest_state(
            self.conn,
            "/tmp/source.jsonl",
            inode=1,
            offset=10,
            size=10,
            mtime=float(_ts("2026-06-06")),
        )

    def tearDown(self):
        self.conn.close()

    def test_audit_returns_sources_and_ingest_state(self):
        a = aggregate.audit(self.conn, self.pricing)
        self.assertEqual(a["meta"]["total_events"], 2)
        self.assertEqual(a["ingest_state"]["files"], 1)
        sources = {s["source"]: s for s in a["sources"]}
        self.assertEqual(sources["claude"]["total"], 10)
        self.assertEqual(sources["codex"]["total"], 120)

    def test_audit_unknown_models_are_limited_to_current_db(self):
        from tokenstat import pricing as pricing_mod

        pricing_mod.clear_unknown_models()
        pricing_mod.rates_for_model("old-unknown-model", self.pricing)
        self.assertIn("old-unknown-model", pricing_mod.unknown_models())
        current_pricing = {
            "default": self.pricing["default"],
            "anthropic": {},
            "openai": {"known": self.pricing["default"]},
        }
        a = aggregate.audit(self.conn, current_pricing)
        self.assertNotIn("old-unknown-model", a["unknown_models"])
        self.assertEqual(a["unknown_models"], [])
        self.assertIn("old-unknown-model", pricing_mod.unknown_models())

    def test_session_detail_aggregates_groups_and_files(self):
        with patch("tokenstat.aggregate._today_local", return_value=date(2026, 6, 6)):
            d = aggregate.session_detail(self.conn, "s2", "today", self.pricing)
        self.assertEqual(d["summary"]["total"], 120)
        self.assertEqual(d["summary"]["records"], 1)
        self.assertEqual(d["groups"][0]["project"], "b")
        self.assertEqual(d["source_files"][0]["records"], 1)

    def test_insights_explains_today_contributors(self):
        with patch("tokenstat.aggregate._today_local", return_value=date(2026, 6, 6)):
            data = aggregate.insights(self.conn, self.pricing)
        self.assertEqual(data["metrics"]["today_total"], 120)
        bodies = " ".join(card["body"] for card in data["cards"])
        self.assertIn("b / codex", bodies)
        self.assertIn("codex / known", bodies)

    def test_insights_uses_week_baseline_without_empty_yesterday_card(self):
        conn = db.get_conn(":memory:")
        try:
            db.init_db(conn)
            db.insert_records(conn, [
                UsageRecord(ts=_ts("2026-06-04"), source="codex", model="known",
                            project="/tmp/past", input_tokens=100,
                            total_tokens=100, dedup_key="past"),
                UsageRecord(ts=_ts("2026-06-06"), source="codex", model="known",
                            project="/tmp/today", input_tokens=200,
                            total_tokens=200, dedup_key="today"),
            ])
            with patch("tokenstat.aggregate._today_local", return_value=date(2026, 6, 6)):
                data = aggregate.insights(conn, self.pricing)
        finally:
            conn.close()

        titles = [card["title"] for card in data["cards"]]
        self.assertNotIn("昨日基线为空", titles)
        self.assertIn("近 7 日基线", titles)


if __name__ == "__main__":
    unittest.main()
