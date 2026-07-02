import unittest

from tokenstat.models import (
    UsageRecord,
    local_date_of,
    parse_iso_utc,
    project_display,
)


class TestUsageRecord(unittest.TestCase):
    def test_valid_record(self):
        r = UsageRecord(ts=1700000000, source="claude", model="claude-opus-4-7", project="/x")
        self.assertEqual(r.source, "claude")
        self.assertEqual(r.category, "main")

    def test_invalid_source(self):
        with self.assertRaises(ValueError):
            UsageRecord(ts=1, source="gemini", model="m", project="/x")

    def test_invalid_category(self):
        with self.assertRaises(ValueError):
            UsageRecord(ts=1, source="claude", model="m", project="/x", category="weird")

    def test_negative_tokens_rejected(self):
        with self.assertRaises(ValueError):
            UsageRecord(ts=1, source="claude", model="m", project="/x", input_tokens=-5)

    def test_overlong_strings_truncated(self):
        # 损坏/伪造日志塞超长字符串时应被截断，防撑大 SQLite
        r = UsageRecord(ts=1, source="claude", model="m" * 5000,
                        project="/p" * 5000, session_id="s" * 5000)
        self.assertEqual(len(r.model), 512)
        self.assertEqual(len(r.project), 512)
        self.assertEqual(len(r.session_id), 512)

    def test_date_local_is_shanghai(self):
        # 2026-01-01 00:30 UTC -> 北京时间 08:30 同日
        ts = parse_iso_utc("2026-01-01T00:30:00Z")
        r = UsageRecord(ts=ts, source="claude", model="m", project="/x")
        self.assertEqual(r.date_local, "2026-01-01")

    def test_date_local_crosses_midnight(self):
        # 2026-01-01 20:00 UTC -> 北京时间次日 04:00
        ts = parse_iso_utc("2026-01-01T20:00:00Z")
        self.assertEqual(local_date_of(ts), "2026-01-02")


class TestHelpers(unittest.TestCase):
    def test_project_display_basename(self):
        self.assertEqual(project_display("/Users/yunxin/Desktop/开发/token统计"), "token统计")
        self.assertEqual(project_display("/Users/yunxin/Desktop/开发/token统计/"), "token统计")

    def test_project_display_empty(self):
        self.assertEqual(project_display(""), "(unknown)")

    def test_parse_iso_utc_valid(self):
        import calendar

        expected = calendar.timegm((2026, 4, 3, 6, 53, 52, 0, 0, 0))
        self.assertEqual(parse_iso_utc("2026-04-03T06:53:52.013Z"), expected)

    def test_parse_iso_utc_bad(self):
        self.assertEqual(parse_iso_utc("garbage"), 0)
        self.assertEqual(parse_iso_utc(""), 0)


if __name__ == "__main__":
    unittest.main()
