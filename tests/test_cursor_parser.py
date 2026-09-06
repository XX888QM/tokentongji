"""Cursor 仪表盘 CSV：列口径、去重键、cookie 安全、不猜本机 bubble。"""
import base64
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from tokenstat.models import parse_iso_utc
from tokenstat.parsers import cursor as cursor_parser


SAMPLE_CSV = (
    "Date,Cloud Agent ID,Automation ID,Kind,Model,Max Mode,"
    "Input (w/ Cache Write),Input (w/o Cache Write),Cache Read,"
    "Output Tokens,Total Tokens,Cost\n"
    '"2026-06-22T13:09:44.478Z","","","free","composer-2.5-fast","No",'
    '"100","76054","723008","8093","807255","0.71"\n'
)


def _jwt(sub: str) -> str:
    header = base64.urlsafe_b64encode(b'{"alg":"none"}').rstrip(b"=").decode()
    payload = base64.urlsafe_b64encode(
        json.dumps({"sub": sub}, separators=(",", ":")).encode()
    ).rstrip(b"=").decode()
    return f"{header}.{payload}.sig-na_ture"


class TestCursorCsv(unittest.TestCase):
    def test_parses_disjoint_input_columns(self):
        recs = cursor_parser.parse_csv(SAMPLE_CSV)
        self.assertEqual(len(recs), 1)
        r = recs[0]
        self.assertEqual(r.source, "cursor")
        self.assertEqual(r.model, "composer-2.5-fast")
        self.assertEqual(r.project, "cursor")
        self.assertEqual(r.input_tokens, 76054)
        self.assertEqual(r.cache_creation_tokens, 100)
        self.assertEqual(r.cache_read_tokens, 723008)
        self.assertEqual(r.output_tokens, 8093)
        self.assertEqual(r.total_tokens, 807255)
        self.assertEqual(r.request_prompt_tokens, 76054 + 723008 + 100)
        ts = parse_iso_utc("2026-06-22T13:09:44.478Z")
        self.assertEqual(r.ts, ts)
        self.assertEqual(r.dedup_key, f"cursor:{ts}:composer-2.5-fast:76054:100:723008:8093:1")

    def test_cache_write_is_not_a_superset(self):
        csv = (
            "Date,Model,Input (w/ Cache Write),Input (w/o Cache Write),Cache Read,Output Tokens\n"
            '"2026-06-22T13:09:44Z","m","500","200","10","5"\n'
        )
        r = cursor_parser.parse_csv(csv)[0]
        self.assertEqual(r.input_tokens, 200)
        self.assertEqual(r.cache_creation_tokens, 500)
        self.assertEqual(r.cache_read_tokens, 10)
        self.assertEqual(r.output_tokens, 5)

    def test_thousands_separator_drops_row(self):
        csv = (
            "Date,Model,Input (w/o Cache Write),Cache Read,Output Tokens\n"
            '"2026-06-22T13:09:44Z","m","1,234","0","5"\n'
        )
        self.assertEqual(cursor_parser.parse_csv(csv), [])

    def test_truncated_row_dropped(self):
        csv = (
            "Date,Model,Input (w/o Cache Write),Cache Read,Output Tokens\n"
            '"2026-06-22T13:09:44Z","m"\n'
        )
        self.assertEqual(cursor_parser.parse_csv(csv), [])

    def test_empty_token_cell_is_zero(self):
        csv = (
            "Date,Model,Input (w/o Cache Write),Cache Read,Output Tokens\n"
            '"2026-06-22T13:09:44Z","m","","0","5"\n'
        )
        r = cursor_parser.parse_csv(csv)[0]
        self.assertEqual(r.input_tokens, 0)
        self.assertEqual(r.output_tokens, 5)

    def test_zero_row_skipped(self):
        csv = (
            "Date,Model,Input (w/o Cache Write),Cache Read,Output Tokens\n"
            '"2026-06-22T13:09:44Z","m","0","0","0"\n'
        )
        self.assertEqual(cursor_parser.parse_csv(csv), [])

    def test_duplicate_fingerprint_gets_occurrence_index(self):
        csv = (
            "Date,Model,Input (w/o Cache Write),Cache Read,Output Tokens\n"
            '"2026-06-22T13:09:44Z","m","10","0","5"\n'
            '"2026-06-22T13:09:44Z","m","10","0","5"\n'
        )
        recs = cursor_parser.parse_csv(csv)
        self.assertEqual(len(recs), 2)
        self.assertTrue(recs[0].dedup_key.endswith(":1"))
        self.assertTrue(recs[1].dedup_key.endswith(":2"))
        self.assertNotEqual(recs[0].dedup_key, recs[1].dedup_key)

    def test_unknown_header_raises(self):
        with self.assertRaises(cursor_parser.CursorParseError):
            cursor_parser.parse_csv("Something,Else\n\"a\",\"b\"\n")

    def test_bom_and_quoted_comma_in_model(self):
        csv = (
            "\ufeffDate,Model,Input (w/o Cache Write),Cache Read,Output Tokens\n"
            '"2026-06-22T13:09:44Z","foo,bar","3","1","2"\n'
        )
        r = cursor_parser.parse_csv(csv)[0]
        self.assertEqual(r.model, "foo,bar")
        self.assertEqual(r.input_tokens, 3)

    def test_cloud_agent_id_becomes_session(self):
        recs = cursor_parser.parse_csv(SAMPLE_CSV)
        self.assertEqual(recs[0].session_id, "")
        csv = (
            "Date,Cloud Agent ID,Model,Input (w/o Cache Write),Cache Read,Output Tokens\n"
            '"2026-06-22T13:09:44Z","agent-9","m","1","0","1"\n'
        )
        self.assertEqual(cursor_parser.parse_csv(csv)[0].session_id, "agent-9")


class TestCursorAuthHelpers(unittest.TestCase):
    def test_sanitize_token_rejects_cookie_metacharacters(self):
        self.assertEqual(
            cursor_parser.sanitize_token("eyJhbGci.eyJzdWIi.sig-na_ture"),
            "eyJhbGci.eyJzdWIi.sig-na_ture",
        )
        self.assertEqual(cursor_parser.sanitize_token(' "abcdefghij" '), "abcdefghij")
        self.assertIsNone(cursor_parser.sanitize_token("abcdefghij\r\nInjected: 1"))
        self.assertIsNone(cursor_parser.sanitize_token("abcdefghij; evil=1"))
        self.assertIsNone(cursor_parser.sanitize_token("abcdefghij=padding"))
        self.assertIsNone(cursor_parser.sanitize_token("short"))

    def test_normalize_subject(self):
        self.assertEqual(cursor_parser.normalize_subject("auth0|user_01ABC"), "user_01ABC")
        self.assertEqual(cursor_parser.normalize_subject("user_01ABC"), "user_01ABC")
        self.assertEqual(
            cursor_parser.normalize_subject("google-oauth2|209269195"),
            "google-oauth2|209269195",
        )
        self.assertEqual(
            cursor_parser.normalize_subject("okta|user@company.com"),
            "okta|user@company.com",
        )
        self.assertIsNone(cursor_parser.normalize_subject("weird-value"))
        self.assertIsNone(cursor_parser.normalize_subject("a|b|c"))
        self.assertIsNone(cursor_parser.normalize_subject("google-oauth2|123\r\nInjected: 1"))
        self.assertIsNone(cursor_parser.normalize_subject("google-oauth2|123; evil=1"))

    def test_session_cookie_encodes_pipe_only(self):
        cookie = cursor_parser.session_cookie("google-oauth2|123", "abc.def.ghi")
        self.assertEqual(
            cookie,
            "WorkosCursorSessionToken=google-oauth2%7C123%3A%3Aabc.def.ghi",
        )

    def test_account_id_prefers_jwt_sub(self):
        token = _jwt("google-oauth2|209269195")
        self.assertEqual(cursor_parser.account_id(token), "google-oauth2|209269195")

    def test_fetch_records_from_injected_csv(self):
        recs = cursor_parser.fetch_records(Path("/nope"), csv_text=SAMPLE_CSV)
        self.assertEqual(len(recs), 1)
        self.assertEqual(recs[0].source, "cursor")

    def test_signed_out_store_is_skip(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        db_path = Path(tmp.name) / "state.vscdb"
        conn = sqlite3.connect(str(db_path))
        conn.execute("CREATE TABLE ItemTable (key TEXT PRIMARY KEY, value TEXT)")
        conn.commit()
        conn.close()
        with self.assertRaises(cursor_parser.CursorSkip):
            cursor_parser.fetch_records(db_path)

    def test_fetch_fn_receives_cookie_not_raw_jwt_in_exception(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        db_path = Path(tmp.name) / "state.vscdb"
        token = _jwt("google-oauth2|209269195")
        conn = sqlite3.connect(str(db_path))
        conn.execute("CREATE TABLE ItemTable (key TEXT PRIMARY KEY, value TEXT)")
        conn.execute("INSERT INTO ItemTable VALUES (?, ?)", ("cursorAuth/accessToken", token))
        conn.commit()
        conn.close()
        seen = {}

        def fake_fetch(cookie: str) -> str:
            seen["cookie"] = cookie
            return SAMPLE_CSV

        recs = cursor_parser.fetch_records(db_path, fetch_fn=fake_fetch)
        self.assertEqual(len(recs), 1)
        self.assertIn("%7C", seen["cookie"])
        self.assertIn("%3A%3A", seen["cookie"])
        self.assertTrue(seen["cookie"].startswith("WorkosCursorSessionToken="))


if __name__ == "__main__":
    unittest.main()
