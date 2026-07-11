import json
import subprocess
import unittest
from pathlib import Path


class TestStaticApp(unittest.TestCase):
    def _run_js(self, check):
        app = Path(__file__).parents[1] / "src/tokenstat/static/app.js"
        source = app.read_text().removesuffix("main();\n")
        return subprocess.run(
            ["node", "-e", source + check],
            check=True,
            capture_output=True,
            text=True,
        ).stdout

    def test_cost_uses_chinese_large_number_units(self):
        check = """
CNY_RATE = 1;
console.log(JSON.stringify([9999.99, 10000, 156665.68, 1000000, 10000000, 100000000].map(fmtCost)));
"""
        self.assertEqual(
            json.loads(self._run_js(check)),
            ["¥9999.99", "¥1万", "¥15.67万", "¥100万", "¥1000万", "¥1亿"],
        )

    def test_alert_day_uses_shanghai_date(self):
        check = """
console.log(JSON.stringify([
  localDateKey(new Date('2026-07-11T15:59:00Z')),
  localDateKey(new Date('2026-07-11T16:01:00Z'))
]));
"""
        self.assertEqual(json.loads(self._run_js(check)), ["2026-07-11", "2026-07-12"])

    def test_stale_session_detail_response_is_ignored(self):
        check = """
const target = { hidden: true, innerHTML: '' };
const close = { addEventListener() {} };
global.document = { getElementById(id) { return id === 'sessionDetail' ? target : close; } };
const pending = {};
getJSON = (url) => new Promise((resolve) => { pending[url.includes('session_id=A') ? 'A' : 'B'] = resolve; });
const data = (id) => ({ session_id:id, summary:{}, groups:[], source_files:[] });
(async () => {
  const a = loadSessionDetail('A');
  const b = loadSessionDetail('B');
  pending.B(data('B'));
  await b;
  pending.A(data('A'));
  await a;
  console.log(target.innerHTML.includes('B'));
})();
"""
        self.assertEqual(self._run_js(check).strip(), "true")

    def test_mobile_specific_markup_and_css_are_absent(self):
        root = Path(__file__).parents[1] / "src/tokenstat/static"
        self.assertNotIn('name="viewport"', (root / "index.html").read_text())
        self.assertNotIn("@media(max-width", (root / "styles.css").read_text())

    def test_desktop_redesign_semantics(self):
        html = (Path(__file__).parents[1] / "src/tokenstat/static/index.html").read_text()
        self.assertIn("TOKEN LEDGER / 06", html)
        self.assertIn('id="liveStatus"', html)
        self.assertIn('aria-live="polite"', html)
        self.assertIn('href="#overview"', html)
        self.assertIn('href="#audit"', html)
        self.assertIn('href="#breakdown"', html)
        self.assertIn('href="#trend"', html)
        self.assertIn('href="#sessions"', html)
        self.assertIn('role="dialog"', html)
        self.assertIn('aria-labelledby="settingsTitle"', html)
        self.assertIn('for="alertCost"', html)
        self.assertIn('for="alertTokens"', html)
        self.assertIn('for="desktopNotify"', html)
        self.assertNotIn('name="viewport"', html)

    def test_night_ledger_visual_contract(self):
        css = (Path(__file__).parents[1] / "src/tokenstat/static/styles.css").read_text()
        self.assertIn("--gold: #e6c77a", css)
        self.assertIn("--signal: #78dce8", css)
        self.assertIn("min-width: 1180px", css)
        self.assertIn("Iowan Old Style", css)
        self.assertIn("SFMono-Regular", css)
        self.assertIn("prefers-reduced-motion", css)
        self.assertNotIn("@media(max-width", css)


if __name__ == "__main__":
    unittest.main()
