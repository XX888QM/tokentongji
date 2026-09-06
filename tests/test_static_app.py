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

    def test_claude_mem_display_source_uses_the_same_large_number_format(self):
        check = """
const rows = sourceRows({ codex: { total: 90000000 }, claude_mem: { total: 12438516 } }, 102438516);
const nodes = Object.fromEntries([
  'heroRange', 'heroTotal', 'heroCost', 'heroSplitbar', 'heroSplitLegend'
].map((id) => [id, { innerHTML: '', textContent: '', title: '', hidden: true }]));
global.document = { getElementById(id) { return nodes[id]; } };
renderHero({ total: 102438516, cost_usd: 0, by_display_source: { codex: { total: 90000000 }, claude_mem: { total: 12438516 } } }, '2026-06-06');
console.log(JSON.stringify({
  labels: rows.map((row) => row.label),
  display: sourceTotal(rows[1]),
  badge: sourceBadge({ source: 'codex', collector: 'claude-mem' }),
  hero: nodes.heroSplitLegend.innerHTML,
}));
"""
        rendered = json.loads(self._run_js(check))
        self.assertEqual(rendered["labels"], ["Codex", "claude-mem"])
        self.assertEqual(rendered["display"], "1243.85万")
        self.assertEqual(
            rendered["badge"],
            '<span class="badge claude_mem" data-source="claude_mem">claude-mem</span>',
        )
        self.assertIn('1243.85万', rendered["hero"])
        self.assertNotIn('split-row claude-mem', rendered["hero"])
        self.assertIn('title="12,438,516 tokens"', rendered["hero"])
        self.assertNotIn('<small>tokens</small>', rendered["hero"])

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

    def test_desktop_observatory_semantics(self):
        html = (Path(__file__).parents[1] / "src/tokenstat/static/index.html").read_text()
        self.assertIn("TOKEN OBSERVATORY / 07", html)
        self.assertIn('class="observatory"', html)
        self.assertIn('src="/static/assets/observatory-hud.webp"', html)
        self.assertIn('class="observatory-orbit"', html)
        self.assertIn('id="obsSourcesLeft"', html)
        self.assertIn('id="obsSourcesRight"', html)
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
        self.assertIn('id="ingestNow"', html)
        self.assertIn('id="backupNow"', html)
        self.assertIn('id="exportCsv"', html)
        self.assertNotIn('name="viewport"', html)

    def test_observatory_visual_contract(self):
        css = (Path(__file__).parents[1] / "src/tokenstat/static/styles.css").read_text()
        app = (Path(__file__).parents[1] / "src/tokenstat/static/app.js").read_text()
        self.assertIn("--gold: #e6c77a", css)
        self.assertIn("--signal: #78dce8", css)
        self.assertIn("--hud-cyan: #60d7f4", css)
        self.assertIn(".observatory", css)
        self.assertIn(".observatory-asset", css)
        self.assertIn(".observatory-orbit", css)
        self.assertIn("OBSERVATORY_SOURCE_COLUMNS", app)
        self.assertIn("renderObservatorySources(rows)", app)
        self.assertIn("renderHeroTotal", app)
        self.assertIn("observatoryOrbit", css)
        self.assertIn("observatorySignal", css)
        self.assertIn("min-width: 1180px", css)
        self.assertIn("-apple-system", css)
        self.assertIn("PingFang SC", css)
        self.assertNotIn("Iowan Old Style", css)
        self.assertNotIn("SFMono-Regular", css)
        self.assertNotIn("SFMono-Regular", app)
        self.assertNotIn("Avenir Next", app)
        self.assertIn("font-size: 15px", css)
        self.assertIn("table { width: 100%; border-collapse: collapse; font-size: 16px; }", css)
        self.assertIn("font-size: 14px; }\n\n/* Panels */", css)
        self.assertIn("font-size: 15px; }\n.kv span", css)
        self.assertIn(".insight-body { color: var(--muted); font-size: 15px", css)
        self.assertIn(".hero-sub { margin-bottom: 26px; color: var(--signal); font-size: 14px; }", css)
        self.assertIn(".split-row .label { color: var(--muted); font-size: 16px; }", css)
        self.assertIn(".split-row .value { color: var(--paper); font: 600 15px/1", css)
        self.assertIn(".split-row .pct { color: var(--dim); text-align: right; font: 500 14px/1", css)
        self.assertIn("prefers-reduced-motion", css)
        self.assertNotIn("@media(max-width", css)

    def test_header_health_reflects_audit_status(self):
        check = """
const live = { textContent:'', className:'', style:{} };
global.document = { getElementById(id) { return id === 'liveStatus' ? live : {}; } };
setHeaderHealth('warn');
console.log(JSON.stringify({textContent: live.textContent, className: live.className, cursor: live.style.cursor}));
"""
        self.assertEqual(
            json.loads(self._run_js(check)),
            {"textContent": "需关注", "className": "live warn", "cursor": "pointer"},
        )

    def test_source_filter_uses_display_source_not_physical_source(self):
        check = """
sourceFilter = 'codex';
console.log(JSON.stringify({
  keep: matchesSourceFilter({ source: 'codex', collector: null }),
  dropMem: matchesSourceFilter({ source: 'codex', collector: 'claude-mem' }),
  keepMem: (sourceFilter = 'claude_mem') && matchesSourceFilter({ source: 'codex', collector: 'claude-mem' }),
}));
"""
        self.assertEqual(
            json.loads(self._run_js(check)),
            {"keep": True, "dropMem": False, "keepMem": True},
        )

    def test_chart_is_line_only(self):
        source = (Path(__file__).parents[1] / "src/tokenstat/static/app.js").read_text()
        self.assertIn("fill: false", source)
        self.assertIn("borderWidth: 1.75", source)

    def test_chart_tooltip_shows_total(self):
        check = """
console.log(chartTooltipTotal([
  { parsed: { y: 52939008 } },
  { parsed: { y: 5290439411 } },
  { parsed: { y: 58004220 } }
]));
"""
        self.assertEqual(self._run_js(check).strip(), "总计: 54.01亿 (5,401,382,639)")

    def test_period_and_modal_ux_contract(self):
        source = (Path(__file__).parents[1] / "src/tokenstat/static/app.js").read_text()
        self.assertIn("tokenstat_period", source)
        self.assertIn("aria-pressed", source)
        self.assertIn("e.key === 'Escape'", source)
        self.assertIn("settingsButton", source)
        self.assertIn("/api/export?period=", source)
        self.assertIn("setupMaintenanceActions", source)

    def test_breakdown_has_one_total_and_clear_project_explanation(self):
        root = Path(__file__).parents[1] / "src/tokenstat/static"
        html = (root / "index.html").read_text()
        app = (root / "app.js").read_text()
        self.assertIn("按项目（同一批数据按项目分摊，不重复统计）", html)
        self.assertEqual(html.count("采集来源"), 2)
        self.assertIn("document.querySelector('#modelTable tfoot').innerHTML = '';", app)
        check = """
const nodes = Object.fromEntries([
  '#modelTable tbody', '#modelTable tfoot', '#projectTable tbody', '#projectTable tfoot'
].map((key) => [key, { innerHTML: '' }]));
global.document = {
  querySelector(selector) { return nodes[selector]; },
  getElementById() { return { innerHTML: '', style: {} }; },
};
getJSON = async () => ({
  by_model: [{ source: 'codex', model: 'gpt-test', input: 1, output: 2, cache_read: 3, cache_creation: 4, total: 122, cost_usd: 9.9 }],
  by_project: [{ source: 'codex', project: '项目A', total: 122, cost_usd: 9.9 }],
  total_tokens: 123,
  total_cost_usd: 10,
});
(async () => {
  await loadBreakdown();
  console.log(JSON.stringify({ model: nodes['#modelTable tfoot'].innerHTML, project: nodes['#projectTable tfoot'].innerHTML }));
})();
"""
        totals = json.loads(self._run_js(check))
        self.assertEqual(totals["model"], "")
        self.assertIn("合计", totals["project"])
        self.assertIn("123", totals["project"])


if __name__ == "__main__":
    unittest.main()
