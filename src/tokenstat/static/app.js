'use strict';

const esc = (s) => String(s ?? '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;').replace(/'/g, '&#39;');

// ============ API 契约（前端消费，后端必须照此产出）============
// GET /api/summary
//   { generated_at, refresh_sec, pricing_note,
//     periods: { today: P, yesterday: P, week: P, month: P, year: P } }
//   P = { total, cost_usd, by_source: { claude:{total,cost_usd}, codex:{total,cost_usd} } }
// GET /api/daily?days=30           -> { days: [ {date, claude, codex} ] }
// GET /api/breakdown?period=...    -> { period, by_model:[...], by_project:[...] }
// GET /api/audit                   -> { status, meta, sources, ingest_state, issues, ... }
// GET /api/insights                -> { metrics, cards }
// GET /api/session_detail?...      -> { summary, groups, by_date, source_files }
// ===============================================================

let dailyChart = null;
let currentPeriod = 'today';

// ---- 告警设置 ----
function loadAlertConfig() {
  try { return JSON.parse(localStorage.getItem('tokenstat_alert') || '{}'); }
  catch { return {}; }
}
function saveSettings() {
  const cfg = {
    daily_cost: parseFloat(document.getElementById('alertCost').value) || 0,
    daily_tokens: (parseFloat(document.getElementById('alertTokens').value) || 0) * 1e4,
    desktop_notify: document.getElementById('desktopNotify').checked,
  };
  localStorage.setItem('tokenstat_alert', JSON.stringify(cfg));
  toggleSettings();
  refreshAll();
}
function toggleSettings() {
  const m = document.getElementById('settingsModal');
  const isOpen = m.style.display !== 'none';
  if (!isOpen) {
    const cfg = loadAlertConfig();
    document.getElementById('alertCost').value = cfg.daily_cost || '';
    document.getElementById('alertTokens').value = cfg.daily_tokens ? cfg.daily_tokens / 1e4 : '';
    document.getElementById('desktopNotify').checked = !!cfg.desktop_notify;
  }
  m.style.display = isOpen ? 'none' : 'flex';
}
function dismissAlert() {
  sessionStorage.setItem('tokenstat_alert_dismissed', '1');
  document.getElementById('alertBar').style.display = 'none';
  document.body.classList.remove('has-alert');
}
function checkAlert(todayData) {
  if (sessionStorage.getItem('tokenstat_alert_dismissed')) return;
  const cfg = loadAlertConfig();
  const msgs = [];
  if (cfg.daily_cost > 0 && todayData.cost_usd >= cfg.daily_cost)
    msgs.push(`今日费用 ${fmtCost(todayData.cost_usd)} 已达告警阈值 ${fmtCost(cfg.daily_cost)}`);
  if (cfg.daily_tokens > 0 && todayData.total >= cfg.daily_tokens)
    msgs.push(`今日 Token ${fmtCN(todayData.total)} 已达告警阈值 ${fmtCN(cfg.daily_tokens)}`);
  const bar = document.getElementById('alertBar');
  if (msgs.length) {
    const msg = msgs.join(' · ');
    document.getElementById('alertMsg').textContent = msg;
    bar.style.display = 'flex';
    document.body.classList.add('has-alert');
    maybeNotifyAlert(msg);
  } else {
    bar.style.display = 'none';
    document.body.classList.remove('has-alert');
  }
}
let refreshTimer = null;

const fmt = (n) => (n == null ? '0' : Number(n).toLocaleString('en-US'));
const fmtCost = (n) => '$' + (Number(n) || 0).toFixed(2);

function stripZeros(s) {
  return s.replace(/\.0+$/, '').replace(/(\.\d*?)0+$/, '$1');
}

// 中文数字格式：万 / 百万 / 千万 / 亿 / 百亿 / 千亿 / 万亿 / 京 / 垓 … 逐级进位，保留2位小数去尾零
const _CN_UNITS = [
  [1e20, '垓'],
  [1e19, '千京'],
  [1e18, '百京'],
  [1e16, '京'],
  [1e15, '千万亿'],
  [1e14, '百万亿'],
  [1e12, '万亿'],
  [1e11, '千亿'],
  [1e10, '百亿'],
  [1e8, '亿'],
  [1e7, '千万'],
  [1e6, '百万'],
  [1e4, '万'],
];
function fmtCN(n) {
  let v = Math.round(Number(n) || 0);
  const sign = v < 0 ? '-' : '';
  v = Math.abs(v);
  for (const [base, label] of _CN_UNITS) {
    if (v >= base) return sign + stripZeros((v / base).toFixed(2)) + label;
  }
  return sign + v.toLocaleString('en-US');
}

const numCell = (n) => `<td class="num" title="${fmt(n)}">${fmtCN(n)}</td>`;
const pct = (part, whole) => (whole > 0 ? Math.round((part / whole) * 100) : 0);

async function getJSON(url) {
  const res = await fetch(url, { cache: 'no-store' });
  if (!res.ok) throw new Error(`${url} -> HTTP ${res.status}`);
  return res.json();
}

async function postJSON(url, payload) {
  const res = await fetch(url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'X-Tokenstat-Action': 'notify' },
    body: JSON.stringify(payload),
  });
  if (!res.ok) throw new Error(`${url} -> HTTP ${res.status}`);
  return res.json();
}

async function maybeNotifyAlert(message) {
  const cfg = loadAlertConfig();
  if (!cfg.desktop_notify) return;
  const key = 'tokenstat_notify_' + message;
  if (sessionStorage.getItem(key)) return;
  sessionStorage.setItem(key, '1');
  try {
    await postJSON('/api/notify', { kind: 'alert', message });
  } catch (_) {
    // 页面告警仍然有效，本地通知失败不打断刷新。
  }
}

// ---- HERO（今日）----
function renderHero(p, day) {
  const claude = p.by_source.claude || { total: 0, cost_usd: 0 };
  const codex = p.by_source.codex || { total: 0, cost_usd: 0 };
  const total = p.total || 0;

  document.getElementById('heroRange').textContent = `今日烧掉 · ${day}`;
  const heroEl = document.getElementById('heroTotal');
  heroEl.textContent = fmtCN(total);
  heroEl.title = fmt(total) + ' tokens';
  document.getElementById('heroCost').textContent = '估算 ' + fmtCost(p.cost_usd);

  const cw = pct(claude.total, total);
  const xw = 100 - cw;
  document.getElementById('heroSplitbar').innerHTML =
    `<span class="seg-claude" style="width:${cw}%"></span>` +
    `<span class="seg-codex" style="width:${xw}%"></span>`;
  document.getElementById('heroSplitLegend').innerHTML =
    `<div class="split-row"><span class="sw claude"></span><span class="nm">Claude</span>` +
    `<span class="vl">${fmtCN(claude.total)}</span><span class="pc">${cw}%</span></div>` +
    `<div class="split-row"><span class="sw codex"></span><span class="nm">Codex</span>` +
    `<span class="vl">${fmtCN(codex.total)}</span><span class="pc">${xw}%</span></div>`;
}

// ---- 支撑数据卡（今日/本周/本月）----
function statCard(label, p) {
  const claude = p.by_source.claude || { total: 0 };
  const codex = p.by_source.codex || { total: 0 };
  return `
    <div class="stat">
      <div class="s-label">${label}</div>
      <div class="s-total" title="${fmt(p.total)}">${fmtCN(p.total)}</div>
      <div class="s-cost">估算 ${fmtCost(p.cost_usd)}</div>
      <div class="s-split">
        <span class="cv">Claude ${fmtCN(claude.total)}</span>
        <span class="xv">Codex ${fmtCN(codex.total)}</span>
      </div>
    </div>`;
}

async function loadSummary() {
  const s = await getJSON('/api/summary');
  const day = (s.generated_at || '----------').slice(0, 10);
  renderHero(s.periods.today, day);
  document.getElementById('stats').innerHTML =
    statCard('昨天', s.periods.yesterday) +
    statCard('本周', s.periods.week) +
    statCard('本月', s.periods.month) +
    statCard('今年', s.periods.year);
  document.getElementById('meta').innerHTML =
    `更新于 ${s.generated_at}<br>每 ${s.refresh_sec}s 自动刷新`;
  document.getElementById('pricingNote').textContent = s.pricing_note || '';
  checkAlert(s.periods.today);
  return s.refresh_sec || 30;
}

// ---- 趋势图 ----
async function loadDaily() {
  const d = await getJSON('/api/daily?days=30');
  const labels = d.days.map((x) => x.date.slice(5));
  const ctx = document.getElementById('dailyChart').getContext('2d');

  const gClaude = ctx.createLinearGradient(0, 0, 0, 320);
  gClaude.addColorStop(0, 'rgba(232,121,79,0.32)');
  gClaude.addColorStop(1, 'rgba(232,121,79,0)');
  const gCodex = ctx.createLinearGradient(0, 0, 0, 320);
  gCodex.addColorStop(0, 'rgba(70,181,150,0.28)');
  gCodex.addColorStop(1, 'rgba(70,181,150,0)');

  const cfg = {
    type: 'line',
    data: {
      labels,
      datasets: [
        { label: 'Claude', data: d.days.map((x) => x.claude), borderColor: '#e8794f', backgroundColor: gClaude, fill: true, tension: 0.35, pointRadius: 0, borderWidth: 2 },
        { label: 'Codex', data: d.days.map((x) => x.codex), borderColor: '#46b596', backgroundColor: gCodex, fill: true, tension: 0.35, pointRadius: 0, borderWidth: 2 },
      ],
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      interaction: { mode: 'index', intersect: false },
      plugins: {
        legend: { display: false },
        tooltip: {
          backgroundColor: '#17120f',
          borderColor: '#2c211b',
          borderWidth: 1,
          titleColor: '#f5ece2',
          bodyColor: '#9c8d7e',
          padding: 10,
          titleFont: { family: "-apple-system, 'PingFang SC', system-ui, sans-serif" },
          bodyFont: { family: "-apple-system, 'PingFang SC', system-ui, sans-serif" },
          callbacks: {
            label: (c) => `  ${c.dataset.label}: ${fmtCN(c.parsed.y)} (${fmt(c.parsed.y)})`,
          },
        },
      },
      scales: {
        x: { grid: { color: 'rgba(44,33,27,0.6)' }, ticks: { color: '#6d5f53', font: { family: "-apple-system, 'PingFang SC', system-ui, sans-serif", size: 10 }, maxTicksLimit: 12 } },
        y: { grid: { color: 'rgba(44,33,27,0.6)' }, ticks: { color: '#6d5f53', font: { family: "-apple-system, 'PingFang SC', system-ui, sans-serif", size: 10 }, callback: (v) => fmtCN(v) } },
      },
    },
  };
  if (dailyChart) {
    dailyChart.data = cfg.data;
    dailyChart.update();
  } else {
    dailyChart = new Chart(ctx, cfg);
  }
}

const badge = (src) => `<span class="badge ${src}">${src}</span>`;
// 模型名去掉末尾日期后缀（如 -20251001），更清爽
const modelDisplay = (m) => (m || '').replace(/-\d{6,8}$/, '');
const shortPath = (p) => {
  const s = String(p || '');
  if (s.length <= 58) return s;
  return '…' + s.slice(-57);
};

function kv(label, value, title = '') {
  return `<div class="kv"><span>${esc(label)}</span><strong title="${esc(title || value)}">${esc(value)}</strong></div>`;
}

function issueBadge(issue) {
  const level = issue.level || 'info';
  return `<div class="issue ${level}">${esc(issue.message || issue)}</div>`;
}

async function loadAudit() {
  const a = await getJSON('/api/audit');
  const status = document.getElementById('auditStatus');
  status.textContent = a.status === 'ok' ? '正常' : '需关注';
  status.className = 'status-pill ' + (a.status || 'ok');
  const sources = [];
  for (const s of (a.sources || [])) {
    sources.push(kv(s.source, `${fmtCN(s.total)} / ${fmt(s.records)} 条`));
  }
  document.getElementById('auditSources').innerHTML =
    sources.join('') || '<div class="empty">暂无数据源记录</div>';
  const ingest = a.ingest_state || {};
  document.getElementById('auditIngest').innerHTML =
    kv('事件总数', fmt(a.meta?.total_events || 0)) +
    kv('日期范围', (a.meta?.date_range || []).filter(Boolean).join(' → ') || '暂无') +
    kv('跟踪文件', fmt(ingest.files || 0)) +
    kv('最近入库', ingest.latest_mtime_local || '暂无') +
    kv('DB 大小', fmtCN(a.db?.size_bytes || 0) + 'B', fmt(a.db?.size_bytes || 0) + ' bytes');
  const issueHtml = (a.issues || []).map(issueBadge).join('');
  const unknown = (a.unknown_models || []).slice(0, 5)
    .map((m) => `<div class="issue warn">未知模型：${esc(modelDisplay(m))}</div>`).join('');
  document.getElementById('auditIssues').innerHTML =
    issueHtml + unknown || '<div class="issue ok">未发现明显口径风险</div>';
}

async function loadInsights() {
  const data = await getJSON('/api/insights');
  document.getElementById('insightDate').textContent = data.date || '今日';
  document.getElementById('insightCards').innerHTML =
    (data.cards || []).map((c) => `
      <div class="insight-card ${esc(c.level || 'info')}">
        <div class="insight-title">${esc(c.title)}</div>
        <div class="insight-body">${esc(c.body)}</div>
      </div>`).join('') || '<div class="empty">暂无可分析数据</div>';
}

async function loadTopSessions() {
  const b = await getJSON(`/api/top_sessions?period=${currentPeriod}&limit=10`);
  document.querySelector('#topSessionsTable tbody').innerHTML =
    b.sessions.map((r, i) => `<tr>
      <td class="num">${i + 1}</td>
      <td><button class="session-link" data-session="${esc(r.session_id)}">${esc(r.date)}</button></td>
      <td>${esc(r.project) || '(未知)'}</td>
      <td>${esc(modelDisplay(r.model))}</td>
      <td>${badge(esc(r.source))}</td>
      ${numCell(r.total)}
      <td class="num">${fmtCost(r.cost_usd)}</td>
    </tr>`).join('') || '<tr><td colspan="7">暂无数据</td></tr>';
}

async function loadSessionDetail(sessionId) {
  const target = document.getElementById('sessionDetail');
  target.hidden = false;
  target.innerHTML = '<div class="empty">加载会话明细中…</div>';
  try {
    const d = await getJSON(`/api/session_detail?period=${currentPeriod}&session_id=${encodeURIComponent(sessionId)}`);
    const summary = d.summary || {};
    const groupRows = (d.groups || []).map((g) => `
      <tr>
        <td>${badge(esc(g.source))}</td>
        <td>${esc(modelDisplay(g.model))}</td>
        <td title="${esc(g.cwd)}">${esc(g.project)}</td>
        ${numCell(g.input)}${numCell(g.output)}${numCell(g.cache_read)}${numCell(g.cache_creation)}${numCell(g.total)}
        <td class="num">${fmtCost(g.cost_usd)}</td>
      </tr>`).join('') || '<tr><td colspan="8">暂无分组</td></tr>';
    const fileRows = (d.source_files || []).map((f) => `
      <tr>
        <td title="${esc(f.source_file)}">${esc(shortPath(f.source_file))}</td>
        ${numCell(f.total)}
        <td class="num">${fmt(f.records)}</td>
      </tr>`).join('') || '<tr><td colspan="3">暂无文件</td></tr>';
    target.innerHTML = `
      <div class="session-head">
        <div>
          <div class="session-title">会话明细</div>
          <div class="session-id" title="${esc(d.session_id)}">${esc(d.session_id)}</div>
        </div>
        <button class="mini-btn" id="closeSessionDetail">关闭</button>
      </div>
      <div class="session-summary">
        ${kv('总量', fmtCN(summary.total || 0), fmt(summary.total || 0))}
        ${kv('费用', fmtCost(summary.cost_usd || 0))}
        ${kv('记录', fmt(summary.records || 0))}
        ${kv('日期', `${summary.first_date || '-'} → ${summary.last_date || '-'}`)}
      </div>
      <div class="session-tables">
        <div class="table-block">
          <h3>模型 / 项目拆分</h3>
          <table><thead><tr><th>源</th><th>模型</th><th>项目</th><th class="num">输入</th><th class="num">输出</th><th class="num">缓存读</th><th class="num">缓存写</th><th class="num">合计</th><th class="num">费用</th></tr></thead><tbody>${groupRows}</tbody></table>
        </div>
        <div class="table-block">
          <h3>来源文件</h3>
          <table><thead><tr><th>文件</th><th class="num">合计</th><th class="num">记录</th></tr></thead><tbody>${fileRows}</tbody></table>
        </div>
      </div>`;
    document.getElementById('closeSessionDetail').addEventListener('click', () => {
      target.hidden = true;
      target.innerHTML = '';
    });
  } catch (e) {
    target.innerHTML = `<div class="issue warn">会话明细加载失败：${esc(e.message)}</div>`;
  }
}

async function loadBreakdown() {
  const b = await getJSON(`/api/breakdown?period=${currentPeriod}`);
  document.querySelector('#modelTable tbody').innerHTML =
    b.by_model
      .map(
        (r) => `<tr>
          <td>${esc(modelDisplay(r.model))}</td><td>${badge(esc(r.source))}</td>
          ${numCell(r.input)}${numCell(r.output)}${numCell(r.cache_read)}${numCell(r.cache_creation)}${numCell(r.total)}
          <td class="num">${fmtCost(r.cost_usd)}</td>
        </tr>`
      )
      .join('') || '<tr><td colspan="8">暂无数据</td></tr>';

  document.querySelector('#projectTable tbody').innerHTML =
    b.by_project
      .map(
        (r) => `<tr>
          <td>${esc(r.project) || '(未知)'}</td><td>${badge(esc(r.source))}</td>
          ${numCell(r.total)}
          <td class="num">${fmtCost(r.cost_usd)}</td>
        </tr>`
      )
      .join('') || '<tr><td colspan="4">暂无数据</td></tr>';
}

async function refreshAll() {
  try {
    const sec = await loadSummary();
    await Promise.all([loadDaily(), loadBreakdown(), loadTopSessions(), loadAudit(), loadInsights()]);
    return sec;
  } catch (e) {
    document.getElementById('meta').textContent = '加载失败: ' + e.message;
    return 30;
  }
}

function setupPeriodToggle() {
  document.getElementById('periodToggle').addEventListener('click', (e) => {
    const btn = e.target.closest('button');
    if (!btn) return;
    document.querySelectorAll('#periodToggle button').forEach((b) => b.classList.remove('active'));
    btn.classList.add('active');
    currentPeriod = btn.dataset.period;
    loadBreakdown();
    loadTopSessions();
    document.getElementById('sessionDetail').hidden = true;
    document.getElementById('sessionDetail').innerHTML = '';
  });
}

function setupTopSessionDrilldown() {
  document.getElementById('topSessionsTable').addEventListener('click', (e) => {
    const btn = e.target.closest('[data-session]');
    if (!btn) return;
    loadSessionDetail(btn.dataset.session);
  });
}

async function main() {
  setupPeriodToggle();
  setupTopSessionDrilldown();
  const sec = await refreshAll();
  if (refreshTimer) clearInterval(refreshTimer);
  refreshTimer = setInterval(refreshAll, (sec || 30) * 1000);
}

main();
