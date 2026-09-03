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

    def test_week_is_last_7_days_including_today(self):
        t = date(2026, 6, 6)
        self.assertEqual(aggregate.period_range("week", t), ("2026-05-31", "2026-06-06"))

    def test_month(self):
        t = date(2026, 6, 6)
        self.assertEqual(aggregate.period_range("month", t), ("2026-06-01", "2026-06-06"))

    def test_all_is_cumulative_not_calendar_year(self):
        t = date(2026, 6, 6)
        self.assertEqual(aggregate.period_range("all", t), ("2000-01-01", "2026-06-06"))

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
        # 累计应包含今日数据
        self.assertEqual(s["periods"]["all"]["total"], 3_600_000)
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

    def test_breakdown_totals_reconcile_across_both_tables(self):
        # 权威总额存在，且两表任意逐行 round 后的差异都不影响它（前端两个合计共用此值）
        b = aggregate.breakdown(self.conn, "today", self.pricing)
        self.assertIn("total_cost_usd", b)
        self.assertIn("total_tokens", b)
        self.assertEqual(b["total_tokens"], sum(m["total"] for m in b["by_model"]))
        self.assertEqual(b["total_tokens"], sum(p["total"] for p in b["by_project"]))

    def test_export_rows_are_granular_and_do_not_double_count(self):
        rows = aggregate.export_rows(self.conn, "today", self.pricing)
        self.assertEqual(len(rows), 3)
        self.assertEqual(sum(row["total"] for row in rows), 3_600_000)
        self.assertEqual(rows[0]["model"], "claude-sonnet-4-6")

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
        # reasoning 应并入 output 计费，breakdown cost 应与 summary 一致
        self.assertAlmostEqual(b["by_model"][0]["cost_usd"], 42.0, places=4)

    def test_meta(self):
        m = aggregate.meta(self.conn)
        self.assertEqual(m["total_events"], 3)


class TestLongContextPricing(unittest.TestCase):
    def setUp(self):
        self.conn = db.get_conn(":memory:")
        db.init_db(self.conn)
        self.pricing = {
            "default": {"input": 1, "output": 1, "cache_read": 1, "cache_write_5m": 1},
            "openai": {
                "gpt-5.4": {
                    "input": 1, "output": 1, "cache_read": 1, "cache_write_5m": 1,
                    "long_context": {
                        "threshold": 200_000,
                        "input": 2,
                        "output": 2,
                        "cache_read": 2,
                    },
                }
            },
        }

    def tearDown(self):
        self.conn.close()

    def test_request_context_is_per_record_and_null_never_uses_token_proxy(self):
        # 两次 150K-token 的短请求不能在聚合后错误升级成长上下文；第三条虽然
        # 本次消耗仅 10K token，但原始请求上下文为 300K，必须按长档计价。
        recs = [
            UsageRecord(
                ts=_ts("2026-07-20"), source="codex", model="gpt-5.4", project="/p",
                input_tokens=150_000, total_tokens=150_000, request_prompt_tokens=150_000, dedup_key="short-1",
            ),
            UsageRecord(
                ts=_ts("2026-07-20") + 1, source="codex", model="gpt-5.4", project="/p",
                input_tokens=150_000, total_tokens=150_000, request_prompt_tokens=150_000, dedup_key="short-2",
            ),
            UsageRecord(
                ts=_ts("2026-07-20") + 2, source="codex", model="gpt-5.4", project="/p",
                input_tokens=10_000, total_tokens=10_000, request_prompt_tokens=300_000, dedup_key="long",
            ),
            # 缺少单次上下文的旧 Codex 差分，即使 input 很大也保持基础档。
            UsageRecord(
                ts=_ts("2026-07-20") + 3, source="codex", model="gpt-5.4", project="/p",
                input_tokens=300_000, total_tokens=300_000, dedup_key="unknown-context",
            ),
        ]
        db.insert_records(self.conn, recs)
        with patch("tokenstat.aggregate._today_local", return_value=date(2026, 7, 20)):
            summary = aggregate.summary(self.conn, self.pricing)["periods"]["today"]
            rows = aggregate.export_rows(self.conn, "today", self.pricing)

        # (150K + 150K + 300K) * $1/M + 10K * $2/M
        self.assertAlmostEqual(summary["cost_usd"], 0.62, places=8)
        self.assertEqual(len(rows), 1)  # 对外导出仍保持来源/模型/项目一行
        self.assertAlmostEqual(rows[0]["cost_usd"], 0.62, places=8)

    def test_historical_threshold_column_exists_when_next_pricing_changes_threshold(self):
        # 模型的长上下文门槛本身随 next_pricing 变了（不只是价格变了）：旧门槛
        # 100000，"今天"生效的新门槛是 200000。历史行落在旧门槛生效期间，必须
        # 按旧门槛 100000 判长上下文——如果 _pricing_context_sql() 只按"今天"的
        # 阈值生成 SQL 列（漏了 100000 这一列），这条历史行会因为找不到对应列
        # 而静默按基础价算，少收钱。
        pricing_dict = {
            "default": {"input": 1, "output": 1, "cache_read": 1, "cache_write_5m": 1},
            "openai": {
                "gpt-hist": {
                    "input": 1, "output": 1, "cache_read": 1, "cache_write_5m": 1,
                    "long_context": {"threshold": 100_000, "input": 5, "output": 5, "cache_read": 5},
                    "next_pricing": {
                        "starts_on": "2026-08-01",
                        "input": 1, "output": 1, "cache_read": 1,
                        "long_context": {"threshold": 200_000, "input": 9, "output": 9, "cache_read": 9},
                    },
                }
            },
        }
        db.insert_records(self.conn, [
            UsageRecord(
                ts=_ts("2026-07-15"), source="codex", model="gpt-hist", project="/p",
                input_tokens=150_000, total_tokens=150_000,
                request_prompt_tokens=150_000, dedup_key="pre-cutover",
            ),
        ])
        with patch("tokenstat.aggregate._today_local", return_value=date(2026, 7, 15)):
            summary = aggregate.summary(self.conn, pricing_dict)["periods"]["today"]

        # 150,000 落在旧门槛(100000)之上 -> 应按旧长上下文价 $5/M 算 = $0.75，
        # 不能因为 SQL 缺列而落到基础价 $1/M（$0.15）。
        self.assertAlmostEqual(summary["cost_usd"], 0.75, places=8)


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

    def test_opencode_reasoning_cost_consistent_in_top_sessions(self):
        """top_sessions cost_usd 应与 summary 口径一致：opencode reasoning 并入 output 计费。"""
        pricing = {
            "default": {"input": 1, "output": 1, "cache_read": 1,
                        "cache_write_5m": 1, "cache_write_1h": 1},
            "anthropic": {}, "openai": {}, "deepseek": {},
        }
        conn = db.get_conn(":memory:")
        db.init_db(conn)
        try:
            db.insert_records(conn, [
                UsageRecord(ts=_ts("2026-06-06"), source="opencode", model="deepseek-v4-pro",
                            project="/tmp/oc", input_tokens=10_000_000, output_tokens=20_000_000,
                            reasoning_tokens=5_000_000, cache_read_tokens=7_000_000,
                            total_tokens=42_000_000, session_id="oc-sess1",
                            dedup_key="oc-top-test"),
            ])
            with patch("tokenstat.aggregate._today_local", return_value=date(2026, 6, 6)):
                sessions = aggregate.top_sessions(conn, "today", pricing, 10)["sessions"]
                s_cost = aggregate.summary(conn, pricing)["periods"]["today"]["cost_usd"]
        finally:
            conn.close()
        self.assertEqual(len(sessions), 1)
        # top_sessions 费用应与 summary 一致（均为 $42）
        self.assertAlmostEqual(sessions[0]["cost_usd"], s_cost, places=4)


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


class TestStaleSourceDetection(unittest.TestCase):
    """audit() 应捕捉「某来源静默停更」——落后库内最新日期 >= STALE_SOURCE_DAYS 天。"""

    def _pricing(self):
        return {"default": {"input": 1, "output": 1, "cache_read": 1,
                            "cache_write_5m": 1, "cache_write_1h": 1},
                "anthropic": {}, "openai": {}}

    def test_stale_single_source_is_info_not_warn(self):
        # 单来源落后（用户没用该工具）只作 info，不触发「需关注」
        conn = db.get_conn(":memory:")
        try:
            db.init_db(conn)
            db.insert_records(conn, [
                UsageRecord(ts=_ts("2026-06-20"), source="claude", model="known",
                            project="/tmp/a", input_tokens=10, total_tokens=10, dedup_key="fresh"),
                UsageRecord(ts=_ts("2026-06-20"), source="codex", model="known",
                            project="/tmp/b", input_tokens=10, total_tokens=10, dedup_key="fresh2"),
                UsageRecord(ts=_ts("2026-06-06"), source="opencode", model="known",
                            project="/tmp/c", input_tokens=10, total_tokens=10, dedup_key="stale"),
            ])
            with patch("tokenstat.aggregate._today_local", return_value=date(2026, 6, 20)):
                a = aggregate.audit(conn, self._pricing())
        finally:
            conn.close()
        stale = [i for i in a["issues"] if "opencode 已 14 天无新数据" in i["message"]]
        self.assertEqual(len(stale), 1)
        self.assertEqual(stale[0]["level"], "info")  # info，非 warn（不触发「需关注」）
        self.assertNotIn("claude 已", " ".join(i["message"] for i in a["issues"]))

    def test_all_fresh_no_stale_warn(self):
        conn = db.get_conn(":memory:")
        try:
            db.init_db(conn)
            db.insert_records(conn, [
                UsageRecord(ts=_ts("2026-06-20"), source="claude", model="known",
                            project="/tmp/a", input_tokens=10, total_tokens=10, dedup_key="c"),
                UsageRecord(ts=_ts("2026-06-19"), source="codex", model="known",
                            project="/tmp/b", input_tokens=10, total_tokens=10, dedup_key="x"),
            ])
            with patch("tokenstat.aggregate._today_local", return_value=date(2026, 6, 20)):
                a = aggregate.audit(conn, self._pricing())
        finally:
            conn.close()
        msgs = " ".join(i["message"] for i in a["issues"])
        self.assertNotIn("无新数据", msgs)  # 差 1 天 < 阈值 3

    def test_activity_date_overrides_stale_check_without_moving_token_date(self):
        conn = db.get_conn(":memory:")
        try:
            db.init_db(conn)
            db.insert_records(conn, [
                UsageRecord(ts=_ts("2026-06-20"), source="claude", model="known",
                            project="/tmp/a", input_tokens=10, total_tokens=10, dedup_key="fresh-c"),
                UsageRecord(ts=_ts("2026-06-20"), source="codex", model="known",
                            project="/tmp/b", input_tokens=10, total_tokens=10, dedup_key="fresh-x"),
                UsageRecord(ts=_ts("2026-06-06"), source="hermes", model="known",
                            project="/tmp/h", input_tokens=10, total_tokens=10, dedup_key="old-h"),
            ])
            with patch("tokenstat.aggregate._today_local", return_value=date(2026, 6, 20)):
                a = aggregate.audit(
                    conn, self._pricing(), activity_dates={"hermes": "2026-06-20"}
                )
        finally:
            conn.close()
        hermes = next(source for source in a["sources"] if source["source"] == "hermes")
        self.assertEqual(hermes["last_date"], "2026-06-06")
        self.assertEqual(hermes["activity_last_date"], "2026-06-20")
        self.assertNotIn("hermes 已", " ".join(i["message"] for i in a["issues"]))

    def test_all_sources_stale_against_today_warns(self):
        conn = db.get_conn(":memory:")
        try:
            db.init_db(conn)
            db.insert_records(conn, [
                UsageRecord(ts=_ts("2026-01-01"), source="claude", model="known",
                            project="/tmp/a", input_tokens=10, total_tokens=10, dedup_key="old-c"),
                UsageRecord(ts=_ts("2026-01-01"), source="codex", model="known",
                            project="/tmp/b", input_tokens=10, total_tokens=10, dedup_key="old-x"),
            ])
            with patch("tokenstat.aggregate._today_local", return_value=date(2026, 1, 10)):
                a = aggregate.audit(conn, self._pricing())
        finally:
            conn.close()
        msgs = " ".join(i["message"] for i in a["issues"])
        self.assertIn("全部来源已 9 天无新数据", msgs)
        self.assertEqual(a["status"], "warn")


class TestCategoryCard(unittest.TestCase):
    """insights() 应给出「后台消耗占比」卡片，区分 observer/subagent 与主交互。"""

    def _pricing(self):
        return {"default": {"input": 1, "output": 1, "cache_read": 1,
                            "cache_write_5m": 1, "cache_write_1h": 1},
                "anthropic": {}, "openai": {}}

    def test_background_ratio_card(self):
        conn = db.get_conn(":memory:")
        try:
            db.init_db(conn)
            db.insert_records(conn, [
                UsageRecord(ts=_ts("2026-06-06"), source="claude", model="known",
                            project="/tmp/a", input_tokens=70, total_tokens=70,
                            category="main", dedup_key="m"),
                UsageRecord(ts=_ts("2026-06-06"), source="claude", model="known",
                            project="/tmp/b", input_tokens=30, total_tokens=30,
                            category="observer", dedup_key="o"),
            ])
            with patch("tokenstat.aggregate._today_local", return_value=date(2026, 6, 6)):
                data = aggregate.insights(conn, self._pricing())
        finally:
            conn.close()
        card = next(c for c in data["cards"] if c["title"] == "后台消耗占比")
        self.assertIn("30.0%", card["body"])  # observer 30 / 总 100

    def test_all_main_card(self):
        conn = db.get_conn(":memory:")
        try:
            db.init_db(conn)
            db.insert_records(conn, [
                UsageRecord(ts=_ts("2026-06-06"), source="claude", model="known",
                            project="/tmp/a", input_tokens=100, total_tokens=100,
                            category="main", dedup_key="m"),
            ])
            with patch("tokenstat.aggregate._today_local", return_value=date(2026, 6, 6)):
                data = aggregate.insights(conn, self._pricing())
        finally:
            conn.close()
        card = next(c for c in data["cards"] if c["title"] == "后台消耗占比")
        self.assertIn("全部为主交互", card["body"])


class TestClaudeMemSummary(unittest.TestCase):
    def test_claude_mem_is_a_display_source_without_double_counting(self):
        conn = db.get_conn(":memory:")
        try:
            db.init_db(conn)
            db.insert_records(conn, [
                UsageRecord(ts=_ts("2026-06-06"), source="codex", model="gpt-5.6-luna",
                            project="/tmp/project", input_tokens=100, cache_read_tokens=200,
                            output_tokens=300, total_tokens=600, category="observer",
                            session_id="claude-mem-session",
                            source_file="/tmp/claude-mem/usage/codex-usage.jsonl",
                            dedup_key="claude-mem-codex:one"),
                UsageRecord(ts=_ts("2026-06-06"), source="codex", model="gpt-5.6-luna",
                            project="/tmp/project", input_tokens=900, total_tokens=900,
                            session_id="direct-codex-session",
                            category="observer", source_file="/tmp/other.jsonl",
                            dedup_key="other-observer:one"),
            ])
            with patch("tokenstat.aggregate._today_local", return_value=date(2026, 6, 6)):
                data = aggregate.summary(conn, self._pricing())
                daily = aggregate.daily(conn, days=1)
                breakdown = aggregate.breakdown(conn, "today", self._pricing())
                exported = aggregate.export_rows(conn, "today", self._pricing())
                sessions = aggregate.top_sessions(conn, "today", self._pricing())["sessions"]
                detail = aggregate.session_detail(conn, "claude-mem-session", "today", self._pricing())
        finally:
            conn.close()
        self.assertEqual(data["periods"]["today"]["claude_mem"], {"total": 600, "records": 1})
        self.assertEqual(data["periods"]["today"]["by_source"]["codex"]["total"], 1500)
        self.assertEqual(
            data["periods"]["today"]["by_display_source"],
            {"codex": {"total": 900, "cost_usd": 0.0009}, "claude_mem": {"total": 600, "cost_usd": 0.0006}},
        )
        self.assertEqual(sum(row["total"] for row in data["periods"]["today"]["by_display_source"].values()), 1500)
        self.assertEqual(daily["sources"], ["codex", "claude_mem"])
        self.assertEqual(daily["days"][0]["codex"], 900)
        self.assertEqual(daily["days"][0]["claude_mem"], 600)
        self.assertEqual(daily["days"][0]["total"], 1500)
        self.assertEqual(breakdown["total_tokens"], 1500)
        self.assertEqual(
            [(row["collector"], row["total"]) for row in breakdown["by_model"]],
            [(None, 900), ("claude-mem", 600)],
        )
        self.assertEqual(
            [(row["collector"], row["total"]) for row in exported],
            [(None, 900), ("claude-mem", 600)],
        )
        self.assertEqual(sum(row["total"] for row in exported), 1500)
        mem_session = next(row for row in sessions if row["session_id"] == "claude-mem-session")
        self.assertEqual(mem_session["collector"], "claude-mem")
        self.assertEqual(detail["groups"][0]["collector"], "claude-mem")
        self.assertEqual(
            [(row["collector"], row["total"]) for row in breakdown["by_project"]],
            [(None, 900), ("claude-mem", 600)],
        )

    @staticmethod
    def _pricing():
        return {"default": {"input": 1, "output": 1, "cache_read": 1,
                            "cache_write_5m": 1, "cache_write_1h": 1},
                "anthropic": {}, "openai": {}}


if __name__ == "__main__":
    unittest.main()
