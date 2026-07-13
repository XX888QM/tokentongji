# Desktop Dashboard Redesign Implementation Plan

> **已完成的历史实施计划**：复选框保留当时的执行模板格式，不代表当前仍有待办。当前行为与维护规则以 `README.md`、`CLAUDE.md` 和测试为准。

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将 Token 统计仪表盘重做为 Night Ledger × Signal Room 桌面数据工作台，同时保留现有 API、DOM 数据绑定和所有交互。

**Architecture:** 继续使用零构建的原生 HTML/CSS/JavaScript。`index.html` 只负责语义结构和稳定 DOM ID，`styles.css` 负责完整视觉系统，`app.js` 仅调整健康状态和 Chart.js 呈现；后端与数据库不改。

**Tech Stack:** Python 3.9+ 标准库、HTML5、CSS3、原生 JavaScript、仓库内 Chart.js、Node.js 静态回归测试。

## Global Constraints

- 只支持桌面浏览器，页面最小内容宽度为 `1180px`。
- 不添加 viewport、移动端媒体查询、主题切换或响应式布局。
- 不引入 npm、外部字体、图标库、CSS 预处理器或 UI 框架。
- 保留现有 API 路径、数据字段、关键 DOM ID 和事件行为。
- 增加区段导航、周期记忆和模态框键盘交互，不增加新业务功能。
- 暖金 `#e6c77a` 只承担金额和核心指标；冷青 `#78dce8` 承担运行状态。
- 所有新交互具备键盘 focus 样式，并尊重 `prefers-reduced-motion`。
- 工作区已有用户批准但未提交的统计修复；不得回滚或覆盖这些改动。

## File Structure

- `src/tokenstat/static/index.html`：桌面语义骨架、分区标题、模态框可访问性。
- `src/tokenstat/static/styles.css`：Night Ledger × Signal Room 视觉系统和所有桌面布局。
- `src/tokenstat/static/app.js`：健康状态联动、来源渲染标记、Chart.js 视觉参数。
- `tests/test_static_app.py`：无浏览器构建环境下的 HTML/CSS/JS 契约回归。
- `CLAUDE.md`：记录视觉约束和桌面端维护规则。

---

### Task 1: Desktop Semantic Shell

**Files:**
- Modify: `tests/test_static_app.py`
- Modify: `src/tokenstat/static/index.html`

**Interfaces:**
- Consumes: 现有 DOM ID：`meta`、`heroTotal`、`heroCost`、`auditStatus`、`periodToggle`、`settingsModal`。
- Produces: `liveStatus`、`settingsTitle`、`settingsButton`、五个桌面区段锚点，以及与输入控件关联的三个 `<label for="...">`。

- [ ] **Step 1: Write the failing semantic contract test**

Add to `TestStaticApp`:

```python
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
```

- [ ] **Step 2: Run the test and verify RED**

Run:

```bash
PYTHONPATH=src python3 -m unittest tests.test_static_app.TestStaticApp.test_desktop_redesign_semantics
```

Expected: FAIL because the ledger wordmark, `liveStatus`, dialog role, and label associations do not exist.

- [ ] **Step 3: Replace the header and modal markup minimally**

Use this header structure while preserving `meta` and the settings action:

```html
<header class="header">
  <div class="brand-lockup">
    <span class="brand-code">TOKEN LEDGER / 06</span>
    <h1>Token 统计</h1>
  </div>
  <div class="header-right">
    <div class="header-meta">
      <span class="live" id="liveStatus" aria-live="polite">连接中</span>
      <span id="meta">加载中…</span>
    </div>
    <button class="btn-ghost" id="settingsButton" type="button" onclick="toggleSettings()">告警设置</button>
  </div>
</header>
<nav class="section-nav" aria-label="页面区段">
  <a href="#overview">总览</a><a href="#audit">审计</a><a href="#breakdown">明细</a><a href="#trend">趋势</a><a href="#sessions">会话</a>
</nav>
```

Add `id="overview"` to the hero, and add `id="audit"`、`id="breakdown"`、`id="trend"`、`id="sessions"` to the corresponding existing panels.

Add short section eyebrows above the existing H2 headings without changing their IDs or child data containers:

```html
<div class="section-title">
  <span class="section-index">01</span>
  <div><span class="eyebrow">SYSTEM INTEGRITY</span><h2>运行审计</h2></div>
</div>
```

Use this modal shell and keep the existing buttons/handlers:

```html
<div class="modal-overlay" id="settingsModal" style="display:none">
  <div class="modal" role="dialog" aria-modal="true" aria-labelledby="settingsTitle">
    <span class="eyebrow">THRESHOLDS</span>
    <h3 id="settingsTitle">用量告警设置</h3>
    <div class="modal-row"><label for="alertCost">今日费用超过 ¥</label><input type="number" id="alertCost" min="0" step="0.1" placeholder="不告警"></div>
    <div class="modal-row"><label for="alertTokens">今日 Token 超过（万）</label><input type="number" id="alertTokens" min="0" step="1" placeholder="不告警"></div>
    <div class="modal-row"><label for="desktopNotify">启用桌面通知</label><input type="checkbox" id="desktopNotify"></div>
    <div class="modal-actions">
      <button class="btn" type="button" onclick="toggleSettings()">取消</button>
      <button class="btn primary" type="button" onclick="saveSettings()">保存</button>
    </div>
  </div>
</div>
```

- [ ] **Step 4: Run the semantic test and verify GREEN**

Run the Step 2 command.

Expected: `Ran 1 test ... OK`.

- [ ] **Step 5: Commit the semantic shell**

```bash
git add src/tokenstat/static/index.html tests/test_static_app.py
git commit -m "feat: add ledger dashboard semantic shell"
```

---

### Task 2: Night Ledger Visual System

**Files:**
- Modify: `tests/test_static_app.py`
- Modify: `src/tokenstat/static/styles.css`

**Interfaces:**
- Consumes: semantic classes from Task 1 and all existing data-table/modal classes.
- Produces: CSS variables `--gold`, `--signal`, `--ink`, `--paper`, desktop `min-width:1180px`, and reduced-motion handling.

- [ ] **Step 1: Write the failing CSS contract test**

```python
def test_night_ledger_visual_contract(self):
    css = (Path(__file__).parents[1] / "src/tokenstat/static/styles.css").read_text()
    self.assertIn("--gold: #e6c77a", css)
    self.assertIn("--signal: #78dce8", css)
    self.assertIn("min-width: 1180px", css)
    self.assertIn("Iowan Old Style", css)
    self.assertIn("SFMono-Regular", css)
    self.assertIn("prefers-reduced-motion", css)
    self.assertNotIn("@media(max-width", css)
```

- [ ] **Step 2: Run the test and verify RED**

```bash
PYTHONPATH=src python3 -m unittest tests.test_static_app.TestStaticApp.test_night_ledger_visual_contract
```

Expected: FAIL on the missing ledger tokens and desktop minimum width.

- [ ] **Step 3: Replace the root visual tokens and page foundation**

The top of `styles.css` must use these exact responsibilities:

```css
:root {
  --ink: #080909;
  --surface: #0d0f10;
  --surface-raised: #121516;
  --line: #272827;
  --line-soft: #1b1d1d;
  --paper: #f1eee7;
  --muted: #9b9a94;
  --dim: #646660;
  --gold: #e6c77a;
  --signal: #78dce8;
  --ok: #8ccf9b;
  --warn: #ef8b5d;
  --title-font: 'Iowan Old Style', 'Songti SC', Georgia, serif;
  --body-font: 'Avenir Next', 'PingFang SC', sans-serif;
  --number-font: 'SFMono-Regular', Menlo, monospace;
}

* { box-sizing: border-box; margin: 0; padding: 0; }
html { min-width: 1180px; background: var(--ink); }
body {
  min-width: 1180px;
  color: var(--paper);
  background-color: var(--ink);
  background-image:
    linear-gradient(rgba(255,255,255,.018) 1px, transparent 1px),
    radial-gradient(circle at 78% 0%, rgba(120,220,232,.055), transparent 34%);
  background-size: 100% 28px, auto;
  font-family: var(--body-font);
}
.app { width: min(1500px, calc(100% - 64px)); margin: 0 auto; padding: 28px 0 64px; }
html { scroll-behavior:smooth; scroll-padding-top:74px; }
```

- [ ] **Step 4: Implement the desktop layout and component rules**

Use flat ledger sections rather than generic floating cards:

```css
.header { display:flex; justify-content:space-between; align-items:end; padding:18px 0 22px; border-bottom:1px solid var(--line); }
.section-nav { position:sticky; top:0; z-index:20; display:flex; gap:24px; padding:11px 0; border-bottom:1px solid var(--line); background:rgba(8,9,9,.92); backdrop-filter:blur(14px); }
.section-nav a { color:var(--muted); font:600 11px/1 var(--number-font); letter-spacing:.08em; text-decoration:none; }
.section-nav a:hover,.section-nav a:focus-visible { color:var(--gold); }
.brand-code,.eyebrow,.section-index { color:var(--signal); font:600 10px/1 var(--number-font); letter-spacing:.18em; }
.brand-lockup h1,.section-title h2 { font-family:var(--title-font); font-weight:500; letter-spacing:-.025em; }
.hero { display:grid; grid-template-columns:minmax(0,1.62fr) minmax(360px,.9fr); border-bottom:1px solid var(--line); }
.hero-card { min-height:310px; padding:38px 0; background:none; border:0; border-radius:0; }
.hero-card + .hero-card { padding-left:34px; border-left:1px solid var(--line); }
.hero-number { color:var(--gold); font:800 clamp(64px,7vw,112px)/.94 var(--number-font); letter-spacing:-.075em; }
.stats { display:grid; grid-template-columns:repeat(4,1fr); border-bottom:1px solid var(--line); }
.stat-card { min-width:0; padding:22px 24px; border-right:1px solid var(--line); background:none; border-radius:0; }
.stat-card:last-child { border-right:0; }
.panel { margin-top:18px; padding:24px 26px; border:1px solid var(--line); border-radius:6px; background:rgba(13,15,16,.88); }
.tables { display:grid; grid-template-columns:minmax(0,1.65fr) minmax(360px,1fr); gap:26px; }
table { width:100%; border-collapse:collapse; font-size:14px; }
th { color:var(--dim); font:600 10px/1.2 var(--number-font); letter-spacing:.1em; }
td.num,.hero-number,.s-value { font-family:var(--number-font); font-variant-numeric:tabular-nums; }
tbody tr:hover { background:rgba(230,199,122,.045); }
.btn:focus-visible,.btn-ghost:focus-visible,.period-toggle button:focus-visible,.session-link:focus-visible { outline:2px solid var(--signal); outline-offset:3px; }
@media (prefers-reduced-motion: reduce) { html { scroll-behavior:auto; } *,*::before,*::after { animation-duration:.01ms !important; transition-duration:.01ms !important; } }
```

Complete the same visual language for `.audit-grid`, `.insight-cards`, `.chart-wrap`, `.session-detail`, `.modal`, badges, pager, alert and footer. Preserve every existing selector required by `app.js`.

- [ ] **Step 5: Run CSS test and all static tests**

```bash
PYTHONPATH=src python3 -m unittest tests.test_static_app
```

Expected: all `TestStaticApp` tests pass.

- [ ] **Step 6: Commit the visual system**

```bash
git add src/tokenstat/static/styles.css tests/test_static_app.py
git commit -m "feat: apply night ledger visual system"
```

---

### Task 3: Runtime Health, Period Memory, Modal UX and Chart Styling

**Files:**
- Modify: `tests/test_static_app.py`
- Modify: `src/tokenstat/static/app.js`

**Interfaces:**
- Consumes: `#liveStatus` from Task 1 and `/api/audit` response `{status: "ok"|"warn"}`.
- Produces: `setHeaderHealth(status)`, persisted `tokenstat_period`, accessible period state, modal focus lifecycle, and a line-only Chart.js dataset style.

- [ ] **Step 1: Write failing JavaScript behavior tests**

Add a Node-backed test using the existing `_run_js` helper:

```python
def test_header_health_reflects_audit_status(self):
    check = """
const live = { textContent:'', className:'' };
global.document = { getElementById(id) { return id === 'liveStatus' ? live : {}; } };
setHeaderHealth('warn');
console.log(JSON.stringify(live));
"""
    self.assertEqual(
        json.loads(self._run_js(check)),
        {"textContent": "需关注", "className": "live warn"},
    )

def test_chart_is_line_only(self):
    source = (Path(__file__).parents[1] / "src/tokenstat/static/app.js").read_text()
    self.assertIn("fill: false", source)
    self.assertIn("borderWidth: 1.75", source)

def test_period_and_modal_ux_contract(self):
    source = (Path(__file__).parents[1] / "src/tokenstat/static/app.js").read_text()
    self.assertIn("tokenstat_period", source)
    self.assertIn("aria-pressed", source)
    self.assertIn("e.key === 'Escape'", source)
    self.assertIn("settingsButton", source)
```

- [ ] **Step 2: Run tests and verify RED**

```bash
PYTHONPATH=src python3 -m unittest \
  tests.test_static_app.TestStaticApp.test_header_health_reflects_audit_status \
  tests.test_static_app.TestStaticApp.test_chart_is_line_only
```

Expected: FAIL because `setHeaderHealth` does not exist and datasets still use gradient fill.

- [ ] **Step 3: Implement header status binding**

Add near the other rendering helpers:

```javascript
function setHeaderHealth(status) {
  const live = document.getElementById('liveStatus');
  if (!live) return;
  const warn = status !== 'ok';
  live.textContent = warn ? '需关注' : '运行正常';
  live.className = 'live ' + (warn ? 'warn' : 'ok');
}
```

Call `setHeaderHealth(a.status)` inside `loadAudit()` immediately after receiving `/api/audit`.

- [ ] **Step 4: Persist the selected period and expose its accessible state**

Initialize from session storage with an allowlist:

```javascript
const VALID_PERIODS = new Set(['today', 'week', 'month', 'all']);
const savedPeriod = sessionStorage.getItem('tokenstat_period');
if (VALID_PERIODS.has(savedPeriod)) currentPeriod = savedPeriod;

function renderPeriodState() {
  document.querySelectorAll('#periodToggle button').forEach((button) => {
    const active = button.dataset.period === currentPeriod;
    button.classList.toggle('active', active);
    button.setAttribute('aria-pressed', String(active));
  });
}
```

Call `renderPeriodState()` during setup and after a valid period click; save the value with `sessionStorage.setItem('tokenstat_period', currentPeriod)`.

- [ ] **Step 5: Add modal keyboard and focus behavior**

Track and restore focus without adding a modal library:

```javascript
let settingsReturnFocus = null;

function closeSettings() {
  const modal = document.getElementById('settingsModal');
  modal.style.display = 'none';
  settingsReturnFocus?.focus();
}

document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape' && document.getElementById('settingsModal').style.display !== 'none') closeSettings();
});
```

When opening, assign `settingsReturnFocus = document.getElementById('settingsButton')`, show the modal, then focus `#alertCost`. Add one overlay click handler that closes only when `e.target === settingsModal`.

- [ ] **Step 6: Make chart styling line-only**

Replace each dataset's fill/line configuration with:

```javascript
return {
  label: meta.label,
  data: d.days.map((x) => x[source] || 0),
  borderColor: meta.color,
  backgroundColor: rgba(meta.color, 0.08),
  fill: false,
  tension: 0.28,
  pointRadius: 0,
  pointHoverRadius: 3,
  borderWidth: 1.75,
};
```

Set chart grid colors to `rgba(241,238,231,0.055)` and tick colors to `#646660`.

- [ ] **Step 7: Run JavaScript and static tests**

```bash
node --check src/tokenstat/static/app.js
PYTHONPATH=src python3 -m unittest tests.test_static_app
```

Expected: syntax exit 0 and all static tests pass.

- [ ] **Step 8: Commit runtime visual behavior**

```bash
git add src/tokenstat/static/app.js tests/test_static_app.py
git commit -m "feat: connect dashboard health and chart styling"
```

---

### Task 4: Documentation and Full Desktop QA

**Files:**
- Modify: `CLAUDE.md`
- Verify: `src/tokenstat/static/index.html`
- Verify: `src/tokenstat/static/styles.css`
- Verify: `src/tokenstat/static/app.js`

**Interfaces:**
- Consumes: completed desktop dashboard.
- Produces: documented visual maintenance contract and fresh QA evidence.

- [ ] **Step 1: Document the visual contract**

Add under frontend architecture guidance in `CLAUDE.md`:

```markdown
- **桌面视觉系统**：`static/` 采用 Night Ledger × Signal Room 方向。暖金只表示金额/核心总量，冷青表示运行状态；标题、正文、数字分别使用本机衬线/无衬线/等宽回退链。页面最小宽度 1180px，不添加 viewport、移动端媒体查询或外部字体依赖。
```

- [ ] **Step 2: Run the full automated verification gate**

```bash
PYTHONPATH=src python3 -m unittest discover -s tests
python3 -m compileall -q src tests
node --check src/tokenstat/static/app.js
git diff --check
```

Expected: all tests pass and every command exits 0.

- [ ] **Step 3: Verify live APIs before browser QA**

```bash
curl -fsS http://127.0.0.1:8787/api/summary >/tmp/tokenstat-summary-redesign.json
curl -fsS http://127.0.0.1:8787/api/audit >/tmp/tokenstat-audit-redesign.json
```

Expected: both commands exit 0 and return JSON.

- [ ] **Step 4: Run desktop Browser QA**

Flow under test:

```text
http://127.0.0.1:8787/ -> first meaningful screen renders ->
period changes to 累计 -> session detail opens/closes -> settings opens/cancels
```

Verify:

```text
- URL and title are correct.
- DOM snapshot contains 今日消耗、运行审计、拆分明细、TOP 10 最贵会话.
- No framework/error overlay.
- Console error/warn log is empty except explained health warnings rendered as data.
- documentElement.scrollWidth equals documentElement.clientWidth at the normal desktop viewport.
- Accumulated period receives active state.
- Reload preserves the accumulated period and its aria-pressed state.
- Section navigation reaches the requested anchor.
- Session detail and settings modal become visible, then close.
- Settings closes with Escape and focus returns to the settings button.
- Capture one viewport screenshot under /tmp, not in the repository.
```

- [ ] **Step 5: Commit documentation**

```bash
git add CLAUDE.md
git commit -m "docs: record desktop dashboard visual contract"
```

- [ ] **Step 6: Review final worktree without staging unrelated files**

```bash
git status --short
git diff --stat HEAD~4..HEAD
```

Expected: `.superpowers/` mockup state and any pre-existing untracked runtime helper remain uncommitted unless explicitly approved.
