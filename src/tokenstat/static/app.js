'use strict';

// ============ API 契约（前端消费，后端必须照此产出）============
// GET /api/summary
//   { generated_at, refresh_sec, pricing_note,
//     periods: { today: P, week: P, month: P, year: P } }
//   P = { total, cost_usd, by_source: { claude:{total,cost_usd}, codex:{total,cost_usd} } }
// GET /api/daily?days=30           -> { days: [ {date, claude, codex} ] }
// GET /api/breakdown?period=...    -> { period, by_model:[...], by_project:[...] }
// ===============================================================

let dailyChart = null;
let currentPeriod = 'month';
let refreshTimer = null;

const fmt = (n) => (n == null ? '0' : Number(n).toLocaleString('en-US'));
const fmtCost = (n) => '$' + (Number(n) || 0).toFixed(2);

function stripZeros(s) {
  return s.replace(/\.0+$/, '').replace(/(\.\d*?)0+$/, '$1');
}

// 中文数字格式：万 / 百万 / 千万 / 亿 逐级进位，保留2位小数去尾零
const _CN_UNITS = [
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

// ---- HERO（今年）----
function renderHero(p, year) {
  const claude = p.by_source.claude || { total: 0, cost_usd: 0 };
  const codex = p.by_source.codex || { total: 0, cost_usd: 0 };
  const total = p.total || 0;

  document.getElementById('heroRange').textContent = `今年累计烧掉 · ${year}`;
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
  const year = (s.generated_at || '----').slice(0, 4);
  renderHero(s.periods.year, year);
  document.getElementById('stats').innerHTML =
    statCard('今日', s.periods.today) +
    statCard('本周', s.periods.week) +
    statCard('本月', s.periods.month);
  document.getElementById('meta').innerHTML =
    `更新于 ${s.generated_at}<br>每 ${s.refresh_sec}s 自动刷新`;
  document.getElementById('pricingNote').textContent = s.pricing_note || '';
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

async function loadBreakdown() {
  const b = await getJSON(`/api/breakdown?period=${currentPeriod}`);
  document.querySelector('#modelTable tbody').innerHTML =
    b.by_model
      .map(
        (r) => `<tr>
          <td>${modelDisplay(r.model)}</td><td>${badge(r.source)}</td>
          ${numCell(r.input)}${numCell(r.output)}${numCell(r.cache_read)}${numCell(r.cache_creation)}${numCell(r.total)}
          <td class="num">${fmtCost(r.cost_usd)}</td>
        </tr>`
      )
      .join('') || '<tr><td colspan="8">暂无数据</td></tr>';

  document.querySelector('#projectTable tbody').innerHTML =
    b.by_project
      .map(
        (r) => `<tr>
          <td>${r.project || '(未知)'}</td><td>${badge(r.source)}</td>
          ${numCell(r.total)}
          <td class="num">${fmtCost(r.cost_usd)}</td>
        </tr>`
      )
      .join('') || '<tr><td colspan="4">暂无数据</td></tr>';
}

async function refreshAll() {
  try {
    const sec = await loadSummary();
    await Promise.all([loadDaily(), loadBreakdown()]);
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
  });
}

async function main() {
  setupPeriodToggle();
  const sec = await refreshAll();
  if (refreshTimer) clearInterval(refreshTimer);
  refreshTimer = setInterval(refreshAll, (sec || 30) * 1000);
}

main();
