'use strict';

const esc = (s) => String(s ?? '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;').replace(/'/g, '&#39;');

// ============ API 契约（前端消费，后端必须照此产出）============
// GET /api/summary
//   { generated_at, refresh_sec, pricing_note,
//     periods: { today: P, yesterday: P, week: P, month: P, year: P } }
//   P = { total, cost_usd, by_source: { [source]: {total,cost_usd} } }
// GET /api/daily?days=30           -> { sources, days: [ {date, total, [source]: tokens} ] }
// GET /api/breakdown?period=...    -> { period, by_model:[...], by_project:[...] }
// GET /api/audit                   -> { status, meta, sources, ingest_state, issues, ... }
// GET /api/insights                -> { metrics, cards }
// GET /api/top_sessions?period=&limit=  -> { period, sessions:[...] }
// GET /api/session_detail?...      -> { summary, groups, source_files }
// GET /api/rates                   -> { usd_cny }
// POST /api/notify {kind,message}  -> { ok } （本机桌面通知）
// ===============================================================

let dailyChart = null;
let currentPeriod = 'today';
let CNY_RATE = 7.25;
// 请求序号：防止慢响应覆盖新响应（切周期/定时刷新时的竞态）
let breakdownSeq = 0;
let topSessionsSeq = 0;
// 「按项目」分页：页大小跟随「按模型」行数，使两表合计行水平对齐
let projectRows = [];
let projectPage = 0;
let projectPageSize = 8;

const SOURCE_META = {
  claude:   { label: 'Claude',   color: '#f97316' },
  codex:    { label: 'Codex',    color: '#22c55e' },
  opencode: { label: 'Opencode', color: '#a78bfa' },
  openclaw: { label: 'Openclaw', color: '#d67cf2' },
  hermes:   { label: 'Hermes',   color: '#38bdf8' },
};
const SOURCE_ORDER = Object.keys(SOURCE_META);

function metaForSource(source) {
  return SOURCE_META[source] || { label: source, color: '#9b9ba0' };
}

function orderedSources(bySource = {}) {
  const known = SOURCE_ORDER.filter((source) => Object.prototype.hasOwnProperty.call(bySource, source));
  const extra = Object.keys(bySource).filter((source) => !SOURCE_META[source]).sort();
  return known.concat(extra);
}

function sourceRows(bySource = {}, total = 0) {
  return orderedSources(bySource)
    .map((source) => {
      const meta = metaForSource(source);
      const rec = bySource[source] || {};
      return { source, label: meta.label, color: meta.color, total: rec.total || 0, pct: pct(rec.total || 0, total) };
    })
    .filter((row) => row.total > 0);
}

function rgba(hex, alpha) {
  const clean = String(hex || '').replace('#', '');
  if (clean.length !== 6) return `rgba(155,155,160,${alpha})`;
  const n = parseInt(clean, 16);
  return `rgba(${(n >> 16) & 255},${(n >> 8) & 255},${n & 255},${alpha})`;
}

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
}
function checkAlert(todayData) {
  if (sessionStorage.getItem('tokenstat_alert_dismissed')) return;
  const cfg = loadAlertConfig();
  const msgs = [];
  if (cfg.daily_cost > 0 && todayData.cost_usd * CNY_RATE >= cfg.daily_cost)
    msgs.push(`今日费用 ${fmtCost(todayData.cost_usd)} 已达告警阈值 ¥${(cfg.daily_cost).toFixed(2)}`);
  if (cfg.daily_tokens > 0 && todayData.total >= cfg.daily_tokens)
    msgs.push(`今日 Token ${fmtCN(todayData.total)} 已达告警阈值 ${fmtCN(cfg.daily_tokens)}`);
  const bar = document.getElementById('alertBar');
  if (msgs.length) {
    const msg = msgs.join(' · ');
    document.getElementById('alertMsg').textContent = msg;
    bar.style.display = 'flex';
    maybeNotifyAlert(msg);
  } else {
    bar.style.display = 'none';
    }
}
let refreshTimer = null;

const fmt = (n) => (n == null ? '0' : Number(n).toLocaleString('en-US'));
const fmtCost = (n) => '¥' + ((Number(n) || 0) * CNY_RATE).toFixed(2);

function stripZeros(s) {
  return s.replace(/\.0+$/, '').replace(/(\.\d*?)0+$/, '$1');
}

// 中文数字格式：万进制真单位（万/亿/万亿/京/垓），数值部分可多位（如 144亿、5400万），保留2位小数去尾零
const _CN_UNITS = [
  [1e20, '垓'],
  [1e16, '京'],
  [1e12, '万亿'],
  [1e8, '亿'],
  [1e4, '万'],
];
function fmtCN(n) {
  let v = Math.round(Number(n) || 0);
  const sign = v < 0 ? '-' : '';
  v = Math.abs(v);
  for (let i = 0; i < _CN_UNITS.length; i++) {
    const [base, label] = _CN_UNITS[i];
    if (v >= base) {
      const s = stripZeros((v / base).toFixed(2));
      // 边界：四舍五入后进位满一个上级单位（相邻单位差 1e4），改用上级单位
      // 避免显示「10000万」而非「1亿」
      if (parseFloat(s) >= 1e4 && i > 0) {
        const [upBase, upLabel] = _CN_UNITS[i - 1];
        return sign + stripZeros((v / upBase).toFixed(2)) + upLabel;
      }
      return sign + s + label;
    }
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
  // 按「日期」去重：message 内嵌实时金额，若含金额则每次刷新键都变会重复弹窗
  const key = 'tokenstat_notify_' + new Date().toISOString().slice(0, 10);
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
  const src = p.by_source || {};
  const total = p.total || 0;
  const rows = sourceRows(src, total);

  document.getElementById('heroRange').textContent = `今日消耗 · ${day}`;
  const heroEl = document.getElementById('heroTotal');
  heroEl.textContent = fmtCN(total);
  heroEl.title = fmt(total) + ' tokens';
  document.getElementById('heroCost').textContent = fmtCost(p.cost_usd);

  let remaining = 100;
  document.getElementById('heroSplitbar').innerHTML = rows.map((row, index) => {
    const width = index === rows.length - 1 ? Math.max(0, remaining) : row.pct;
    remaining -= width;
    return `<span style="width:${width}%;background:${row.color}"></span>`;
  }).join('');

  document.getElementById('heroSplitLegend').innerHTML = rows
    .map((row) =>
      `<div class="split-row"><span class="dot" style="background:${row.color}"></span><span class="label">${esc(row.label)}</span>` +
      `<span class="value">${fmtCN(row.total)}</span><span class="pct">${row.pct}%</span></div>`
    ).join('');
}

// ---- 支撑数据卡（昨天/近7天/本月）----
function statCard(label, p) {
  const split = sourceRows(p.by_source || {}, p.total || 0)
    .map((row) => `<span style="color:${row.color}">${esc(row.label)} ${fmtCN(row.total)}</span>`)
    .join('');
  return `
    <div class="stat-card">
      <div class="s-label">${label}</div>
      <div class="s-value" title="${fmt(p.total)}">${fmtCN(p.total)}</div>
      <div class="s-cost">${fmtCost(p.cost_usd)}</div>
      <div class="s-split">${split}</div>
    </div>`;
}

async function loadSummary() {
  const s = await getJSON('/api/summary');
  const day = (s.generated_at || '----------').slice(0, 10);
  renderHero(s.periods.today, day);
  document.getElementById('stats').innerHTML =
    statCard('昨天', s.periods.yesterday) +
    statCard('近7天', s.periods.week) +
    statCard('本月', s.periods.month) +
    statCard('累计', s.periods.all);
  document.getElementById('meta').textContent =
    `更新于 ${s.generated_at} · 每 ${s.refresh_sec}s`;
  document.getElementById('pricingNote').textContent = s.pricing_note || '';
  checkAlert(s.periods.today);
  return s.refresh_sec || 30;
}

// ---- 趋势图 ----
async function loadDaily() {
  const d = await getJSON('/api/daily?days=30');
  const labels = d.days.map((x) => x.date.slice(5));
  const ctx = document.getElementById('dailyChart').getContext('2d');
  const sources = (d.sources && d.sources.length ? d.sources : SOURCE_ORDER)
    .filter((source) => d.days.some((day) => (day[source] || 0) > 0));
  const chartSources = sources.length ? sources : ['claude', 'codex'];
  const legend = document.getElementById('dailyLegend');
  if (legend) {
    legend.innerHTML = chartSources.map((source) => {
      const meta = metaForSource(source);
      return `<span><span class="dot" style="background:${meta.color}"></span>${esc(meta.label)}</span>`;
    }).join('');
  }
  const datasets = chartSources.map((source) => {
    const meta = metaForSource(source);
    const gradient = ctx.createLinearGradient(0, 0, 0, 260);
    gradient.addColorStop(0, rgba(meta.color, 0.18));
    gradient.addColorStop(1, rgba(meta.color, 0));
    return {
      label: meta.label,
      data: d.days.map((x) => x[source] || 0),
      borderColor: meta.color,
      backgroundColor: gradient,
      fill: true,
      tension: 0.35,
      pointRadius: 0,
      borderWidth: 2,
    };
  });

  const cfg = {
    type: 'line',
    data: {
      labels,
      datasets,
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      interaction: { mode: 'index', intersect: false },
      plugins: {
        legend: { display: false },
        tooltip: {
          backgroundColor: '#141618',
          borderColor: '#1e2024',
          borderWidth: 1,
          titleColor: '#ededef',
          bodyColor: '#9b9ba0',
          padding: 10,
          titleFont: { family: "Inter, 'PingFang SC', sans-serif" },
          bodyFont: { family: "Inter, 'PingFang SC', sans-serif" },
          callbacks: {
            label: (c) => `  ${c.dataset.label}: ${fmtCN(c.parsed.y)} (${fmt(c.parsed.y)})`,
          },
        },
      },
      scales: {
        x: { grid: { color: 'rgba(30,32,36,0.8)' }, ticks: { color: '#5f6065', font: { family: "Inter, 'PingFang SC', sans-serif", size: 12 }, maxTicksLimit: 12 } },
        y: { grid: { color: 'rgba(30,32,36,0.8)' }, ticks: { color: '#5f6065', font: { family: "Inter, 'PingFang SC', sans-serif", size: 12 }, callback: (v) => fmtCN(v) } },
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
  const my = ++topSessionsSeq;
  const b = await getJSON(`/api/top_sessions?period=${currentPeriod}&limit=10`);
  if (my !== topSessionsSeq) return;  // 已有更新的请求，丢弃这次迟到响应
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
  const my = ++breakdownSeq;
  const b = await getJSON(`/api/breakdown?period=${currentPeriod}`);
  if (my !== breakdownSeq) return;  // 已有更新的请求，丢弃这次迟到响应
  const mRows = b.by_model;
  const mTotalTokens = mRows.reduce((s, r) => s + (r.total || 0), 0);
  const mTotalCost   = mRows.reduce((s, r) => s + (r.cost_usd || 0), 0);
  const mInput       = mRows.reduce((s, r) => s + (r.input || 0), 0);
  const mOutput      = mRows.reduce((s, r) => s + (r.output || 0), 0);
  const mCR          = mRows.reduce((s, r) => s + (r.cache_read || 0), 0);
  const mCC          = mRows.reduce((s, r) => s + (r.cache_creation || 0), 0);
  document.querySelector('#modelTable tbody').innerHTML =
    mRows.map((r) => `<tr>
        <td>${esc(modelDisplay(r.model))}</td><td>${badge(esc(r.source))}</td>
        ${numCell(r.input)}${numCell(r.output)}${numCell(r.cache_read)}${numCell(r.cache_creation)}${numCell(r.total)}
        <td class="num">${fmtCost(r.cost_usd)}</td>
      </tr>`).join('') || '<tr><td colspan="8">暂无数据</td></tr>';
  document.querySelector('#modelTable tfoot').innerHTML = mRows.length
    ? `<tr class="tfoot-total">
        <td colspan="2">合计</td>
        ${numCell(mInput)}${numCell(mOutput)}${numCell(mCR)}${numCell(mCC)}${numCell(mTotalTokens)}
        <td class="num">${fmtCost(mTotalCost)}</td>
      </tr>` : '';

  projectRows = b.by_project;
  // 每页行数 = 「按模型」行数，让两表的「合计」行水平对齐（下限 6 防病态分页）
  projectPageSize = Math.max(mRows.length, 6);
  renderProjectPage();
}

// 渲染「按项目」当前页；合计行始终是全量合计（非本页），刷新不重置页码
function renderProjectPage() {
  const rows = projectRows;
  const total = rows.length;
  const pages = Math.max(1, Math.ceil(total / projectPageSize));
  projectPage = Math.min(Math.max(projectPage, 0), pages - 1);
  const start = projectPage * projectPageSize;
  document.querySelector('#projectTable tbody').innerHTML =
    rows.slice(start, start + projectPageSize).map((r) => `<tr>
        <td>${esc(r.project) || '(未知)'}</td><td>${badge(esc(r.source))}</td>
        ${numCell(r.total)}
        <td class="num">${fmtCost(r.cost_usd)}</td>
      </tr>`).join('') || '<tr><td colspan="4">暂无数据</td></tr>';
  const tTokens = rows.reduce((s, r) => s + (r.total || 0), 0);
  const tCost   = rows.reduce((s, r) => s + (r.cost_usd || 0), 0);
  document.querySelector('#projectTable tfoot').innerHTML = total
    ? `<tr class="tfoot-total">
        <td colspan="2">合计</td>
        ${numCell(tTokens)}
        <td class="num">${fmtCost(tCost)}</td>
      </tr>` : '';
  const pager = document.getElementById('projectPager');
  if (total > projectPageSize) {
    pager.innerHTML =
      `<button class="pager-btn" onclick="projectGoPage(-1)"${projectPage === 0 ? ' disabled' : ''}>‹ 上一页</button>` +
      `<span class="pager-info">第 ${projectPage + 1} / ${pages} 页 · 共 ${total} 项</span>` +
      `<button class="pager-btn" onclick="projectGoPage(1)"${projectPage >= pages - 1 ? ' disabled' : ''}>下一页 ›</button>`;
    pager.style.display = 'flex';
  } else {
    pager.innerHTML = '';
    pager.style.display = 'none';
  }
}

function projectGoPage(delta) {
  projectPage += delta;
  renderProjectPage();
}

async function refreshAll() {
  try {
    const sec = await loadSummary();
    await Promise.all([loadRates(), loadDaily(), loadBreakdown(), loadTopSessions(), loadAudit(), loadInsights()]);
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
    projectPage = 0;  // 切周期回到第 1 页（定时刷新不重置，避免打断翻页浏览）
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

async function loadRates() {
  try {
    const r = await getJSON('/api/rates');
    if (r.usd_cny > 0) CNY_RATE = r.usd_cny;
  } catch (_) {}
}

async function main() {
  setupPeriodToggle();
  setupTopSessionDrilldown();
  await loadRates();
  const sec = await refreshAll();
  if (refreshTimer) clearInterval(refreshTimer);
  refreshTimer = setInterval(refreshAll, (sec || 30) * 1000);
}

main();
