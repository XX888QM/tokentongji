'use strict';

// ============ API 契约（前端消费，后端必须照此产出）============
// GET /api/summary
//   { generated_at, refresh_sec, pricing_note,
//     periods: { today: P, week: P, month: P } }
//   P = { total, cost_usd, by_source: { claude:{total,cost_usd}, codex:{total,cost_usd} } }
// GET /api/daily?days=30
//   { days: [ { date, claude, codex } ... ] }   // claude/codex = 当天总 token
// GET /api/breakdown?period=today|week|month
//   { period,
//     by_model:   [ { model, source, input, output, cache_read, cache_creation, total, cost_usd } ],
//     by_project: [ { project, source, total, cost_usd } ] }
// ===============================================================

let dailyChart = null;
let currentPeriod = 'month';
let refreshTimer = null;

const fmt = (n) => (n == null ? '0' : Number(n).toLocaleString('en-US'));
const fmtCost = (n) => '$' + (Number(n) || 0).toFixed(2);

// 去掉小数末尾多余的 0（1.00 -> 1，8.50 -> 8.5）
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

// 数字单元格：显示万/亿，title 悬停看精确原值
const numCell = (n) => `<td class="num" title="${fmt(n)}">${fmtCN(n)}</td>`;

async function getJSON(url) {
  const res = await fetch(url, { cache: 'no-store' });
  if (!res.ok) throw new Error(`${url} -> HTTP ${res.status}`);
  return res.json();
}

function periodCard(label, p) {
  const claude = p.by_source.claude || { total: 0, cost_usd: 0 };
  const codex = p.by_source.codex || { total: 0, cost_usd: 0 };
  return `
    <div class="card">
      <div class="label">${label}</div>
      <div class="total" title="${fmt(p.total)}">${fmtCN(p.total)}</div>
      <div class="cost">${fmtCost(p.cost_usd)}</div>
      <div class="split">
        <span class="claude-v">Claude ${fmtCN(claude.total)}</span>
        <span class="codex-v">Codex ${fmtCN(codex.total)}</span>
      </div>
    </div>`;
}

async function loadSummary() {
  const s = await getJSON('/api/summary');
  document.getElementById('cards').innerHTML =
    periodCard('今日', s.periods.today) +
    periodCard('本周', s.periods.week) +
    periodCard('本月', s.periods.month) +
    periodCard('今年', s.periods.year);
  document.getElementById('meta').textContent =
    `更新于 ${s.generated_at} · 每 ${s.refresh_sec}s 自动刷新`;
  document.getElementById('pricingNote').textContent = s.pricing_note || '';
  return s.refresh_sec || 30;
}

async function loadDaily() {
  const d = await getJSON('/api/daily?days=30');
  const labels = d.days.map((x) => x.date.slice(5)); // MM-DD
  const claude = d.days.map((x) => x.claude);
  const codex = d.days.map((x) => x.codex);
  const ctx = document.getElementById('dailyChart').getContext('2d');
  const cfg = {
    type: 'line',
    data: {
      labels,
      datasets: [
        { label: 'Claude', data: claude, borderColor: '#d97757', backgroundColor: 'rgba(217,119,87,.12)', fill: true, tension: 0.3, pointRadius: 0 },
        { label: 'Codex', data: codex, borderColor: '#10a37f', backgroundColor: 'rgba(16,163,127,.12)', fill: true, tension: 0.3, pointRadius: 0 },
      ],
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      interaction: { mode: 'index', intersect: false },
      plugins: {
        legend: { display: false },
        tooltip: {
          callbacks: {
            label: (ctx) => `${ctx.dataset.label}: ${fmtCN(ctx.parsed.y)} (${fmt(ctx.parsed.y)})`,
          },
        },
      },
      scales: {
        x: { grid: { color: '#272b36' }, ticks: { color: '#8b91a0', maxTicksLimit: 12 } },
        y: { grid: { color: '#272b36' }, ticks: { color: '#8b91a0', callback: (v) => fmtCN(v) } },
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

function badge(src) {
  return `<span class="badge ${src}">${src}</span>`;
}

async function loadBreakdown() {
  const b = await getJSON(`/api/breakdown?period=${currentPeriod}`);
  const mt = document.querySelector('#modelTable tbody');
  mt.innerHTML = b.by_model
    .map(
      (r) => `<tr>
        <td>${r.model}</td><td>${badge(r.source)}</td>
        ${numCell(r.input)}
        ${numCell(r.output)}
        ${numCell(r.cache_read)}
        ${numCell(r.cache_creation)}
        ${numCell(r.total)}
        <td class="num">${fmtCost(r.cost_usd)}</td>
      </tr>`
    )
    .join('') || '<tr><td colspan="8">暂无数据</td></tr>';

  const pt = document.querySelector('#projectTable tbody');
  pt.innerHTML = b.by_project
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
