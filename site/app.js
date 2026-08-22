// Shared behavior across all pages: tooltips, month toggle, and
// budget-target overrides (persisted in localStorage, no backend).
//
// Each page embeds window.DATA (see build_dashboard.py) and calls a small
// page-specific init at the bottom of its own <script> block, using the
// shared helpers below.

// ---------------------------------------------------------------- tooltip
(function () {
  const tooltip = document.getElementById('tooltip');
  if (!tooltip) return;

  function showTooltip(x, y, title, rows) {
    tooltip.textContent = '';
    const t = document.createElement('div');
    t.className = 't-title';
    t.textContent = title;
    tooltip.appendChild(t);
    rows.forEach(([label, value, color]) => {
      const row = document.createElement('div');
      row.className = 't-row';
      if (color) {
        const key = document.createElement('span');
        key.className = 't-key';
        key.style.background = color;
        row.appendChild(key);
      }
      const lab = document.createElement('span');
      lab.textContent = label;
      const val = document.createElement('span');
      val.className = 't-value';
      val.textContent = value;
      row.appendChild(lab);
      row.appendChild(val);
      tooltip.appendChild(row);
    });
    tooltip.style.display = 'block';
    const vw = window.innerWidth, vh = window.innerHeight;
    const tw = tooltip.offsetWidth || 160, th = tooltip.offsetHeight || 60;
    tooltip.style.left = Math.min(x + 14, vw - tw - 10) + 'px';
    tooltip.style.top = Math.min(y + 14, vh - th - 10) + 'px';
  }
  function hideTooltip() { tooltip.style.display = 'none'; }

  // Event delegation on document, not per-element listeners - charts added
  // or replaced after page load (month-toggle re-renders, new chart types)
  // work automatically, with nothing to re-wire.
  let activeCrosshair = null;

  document.addEventListener('pointermove', e => {
    const el = e.target.closest && e.target.closest('.hit-target');
    if (!el) {
      hideTooltip();
      if (activeCrosshair) { activeCrosshair.style.display = 'none'; activeCrosshair = null; }
      return;
    }
    let rows = [];
    try { rows = JSON.parse(el.dataset.tooltipRows || '[]'); } catch (_) {}
    showTooltip(e.clientX, e.clientY, el.dataset.tooltipTitle || '', rows);
    if (activeCrosshair && (!el.dataset.crosshair || activeCrosshair.id !== el.dataset.crosshair)) {
      activeCrosshair.style.display = 'none';
      activeCrosshair = null;
    }
    if (el.dataset.crosshair) {
      const line = document.getElementById(el.dataset.crosshair);
      if (line) {
        line.style.display = 'block';
        line.setAttribute('x1', el.dataset.cx); line.setAttribute('x2', el.dataset.cx);
        activeCrosshair = line;
      }
    }
  });

  // Trend-chart points carry data-month-idx - clicking one jumps this
  // page's month toggle there, so charts and the toggle stay in sync.
  document.addEventListener('click', e => {
    const el = e.target.closest && e.target.closest('.hit-target');
    if (!el || el.dataset.monthIdx === undefined) return;
    const select = document.getElementById('month-select');
    if (!select) return;
    select.value = el.dataset.monthIdx;
    select.dispatchEvent(new Event('change'));
  });
})();

// ------------------------------------------------------ budget overrides
const OVERRIDE_KEY = 'budget-overrides-v1';

function getOverrides() {
  try { return JSON.parse(localStorage.getItem(OVERRIDE_KEY) || '{}'); }
  catch (_) { return {}; }
}
function setOverride(cat, value) {
  const o = getOverrides();
  if (value === null || value === '' || isNaN(value)) delete o[cat];
  else o[cat] = Number(value);
  localStorage.setItem(OVERRIDE_KEY, JSON.stringify(o));
}
function clearOverrides() { localStorage.removeItem(OVERRIDE_KEY); }
function effectiveTarget(DATA, cat) {
  const o = getOverrides();
  const meta = DATA.categories[cat];
  if (!meta) return null;
  return (cat in o) ? o[cat] : meta.baseTarget;
}

// ------------------------------------------------------------- formatting
function fmtMoney(x) {
  if (x === null || x === undefined) return '—';
  const sign = x < 0 ? '-' : '';
  return sign + '$' + Math.abs(Math.round(x)).toLocaleString();
}
function fmtPct(x) {
  return (x === null || x === undefined) ? '—' : Math.round(x * 100) + '%';
}

const STATUS_HEX = { good: '#0ca30c', warning: '#fab219', critical: '#d03b3b', neutral: '#898781' };
const STATUS_TEXT = { good: 'on pace', warning: 'watch', critical: 'over pace', neutral: '—' };

function statusForRatio(ratio) {
  if (ratio === null || ratio === undefined) return 'neutral';
  if (ratio <= 1.0) return 'good';
  if (ratio <= 1.2) return 'warning';
  return 'critical';
}

// --------------------------------------------------------- month helpers
function paceForMonth(DATA, monthIdx) {
  return (monthIdx === DATA.months.length - 1) ? DATA.livePace : 1.0;
}

function categoryResult(DATA, cat, monthIdx) {
  const meta = DATA.categories[cat];
  const month = DATA.months[monthIdx];
  const target = effectiveTarget(DATA, cat);
  const actualThisMonth = (DATA.byMonth[month].expenseByCat[cat]) || 0;
  let ytd = 0;
  for (let i = 0; i <= monthIdx; i++) ytd += (DATA.byMonth[DATA.months[i]].expenseByCat[cat]) || 0;
  const pace = paceForMonth(DATA, monthIdx);
  const nMonths = monthIdx + 1;
  let status = 'neutral', ratio = null, note = '';
  if (meta.cadence === 'fixed') {
    if (target) {
      ratio = actualThisMonth / target;
      if (actualThisMonth === 0) {
        note = 'not yet paid';
        status = 'warning';
      } else if (ratio >= 0.95) {
        note = 'paid ' + fmtMoney(actualThisMonth);
        status = 'good';
      } else {
        // Something was paid but well short of the target - a partial
        // payment reads identically to "fully paid" if this isn't checked.
        note = 'partial: ' + fmtMoney(actualThisMonth) + ' of ' + fmtMoney(target);
        status = 'warning';
      }
    }
  } else if (meta.cadence === 'variable') {
    if (target) {
      const expected = target * pace;
      ratio = expected ? actualThisMonth / expected : null;
      note = fmtMoney(actualThisMonth) + ' vs ' + fmtMoney(expected) + ' expected';
      status = statusForRatio(ratio);
    }
  } else if (meta.cadence === 'lumpy') {
    if (target) {
      const annualExpected = target * nMonths;
      ratio = annualExpected ? ytd / annualExpected : null;
      note = fmtMoney(ytd) + ' YTD vs ' + fmtMoney(annualExpected) + ' expected (' + nMonths + ' mo)';
      status = statusForRatio(ratio);
    }
  }
  return { category: cat, target, actualThisMonth, ytd, status, ratio, note, cadence: meta.cadence };
}

function poolResult(DATA, monthIdx, group) {
  // Lumpy-cadence categories are excluded from the paced pool (SPEC §5:
  // "a trip is one decision, not a monthly overrun") - they're judged on
  // their own annualized YTD comparison instead (see categoryResult).
  // Pooling them here would make one early-month lumpy purchase (a flight,
  // a move-in furniture run) read as "over pace" for the whole pool, which
  // is exactly the false alarm lumpy cadence exists to prevent.
  const cats = DATA.order[group].filter(cat => DATA.categories[cat].cadence !== 'lumpy');
  let target = 0, actual = 0;
  const month = DATA.months[monthIdx];
  cats.forEach(cat => {
    const t = effectiveTarget(DATA, cat);
    if (t) target += t;
    actual += (DATA.byMonth[month].expenseByCat[cat]) || 0;
  });
  const pace = paceForMonth(DATA, monthIdx);
  const expected = target * pace;
  const ratio = expected ? actual / expected : null;
  return { target, actual, expected, ratio, status: statusForRatio(ratio) };
}

// Baseline is checked per-category (SPEC §5), not pooled - a fixed rent
// payment and a paced groceries budget don't share one meaningful "expected
// by day N" figure. This sums the raw dollars for reference but grounds its
// verdict in each category's own cadence-appropriate status, not a blend.
function baselineSummary(DATA, monthIdx) {
  const cats = DATA.order.baseline;
  let target = 0, actual = 0, onTrack = 0, withTarget = 0;
  cats.forEach(cat => {
    const r = categoryResult(DATA, cat, monthIdx);
    if (r.target !== null) {
      target += r.target;
      withTarget++;
      if (r.status === 'good') onTrack++;
    }
    actual += r.actualThisMonth;
  });
  return { target, actual, onTrack, withTarget };
}

// ----------------------------------------------------------- DOM renderers
function renderMeterRow(rootEl, result) {
  const color = STATUS_HEX[result.status];
  const badge = rootEl.querySelector('.m-badge');
  if (badge) {
    badge.textContent = result.target !== null ? STATUS_TEXT[result.status] : '';
    badge.className = 'status-badge ' + result.status;
    badge.style.display = result.target !== null ? '' : 'none';
  }
  const pctFill = Math.min(100, (result.ratio || 0) * 100);
  const fillEl = rootEl.querySelector('.m-fill');
  fillEl.style.width = pctFill + '%';
  fillEl.style.background = color;
  rootEl.querySelector('.m-track').style.setProperty('--fill-color', color);
  rootEl.querySelector('.m-note').textContent = result.note || 'no target set — type one in';
  const input = rootEl.querySelector('.target-input');
  if (input && document.activeElement !== input) input.value = result.target ?? '';
}

function renderPoolRow(rootEl, result) {
  const color = STATUS_HEX[result.status];
  const badge = rootEl.querySelector('.m-badge');
  if (badge) { badge.textContent = STATUS_TEXT[result.status]; badge.className = 'status-badge ' + result.status; }
  const pctFill = Math.min(100, (result.ratio || 0) * 100);
  rootEl.querySelector('.m-fill').style.width = pctFill + '%';
  rootEl.querySelector('.m-fill').style.background = color;
  rootEl.querySelector('.m-track').style.setProperty('--fill-color', color);
  rootEl.querySelector('.m-target-val').textContent = fmtMoney(result.target);
  rootEl.querySelector('.m-note').textContent =
    fmtMoney(result.actual) + ' vs ' + fmtMoney(result.expected) + ' expected';
}

function renderBarList(containerEl, entries, color, fmt, opts) {
  // entries: [[catKey, label, value], ...]. opts: {onClick(catKey), selectedKey}
  opts = opts || {};
  containerEl.textContent = '';
  if (!entries.length) {
    const p = document.createElement('p');
    p.className = 'card-sub';
    p.textContent = 'No data this month.';
    containerEl.appendChild(p);
    return;
  }
  const max = Math.max(...entries.map(e => e[2]));
  entries.forEach(([catKey, label, val]) => {
    const row = document.createElement('div');
    row.className = 'jsbar-row';
    if (opts.onClick) row.classList.add('jsbar-row--clickable');
    if (opts.selectedKey) row.classList.add(catKey === opts.selectedKey ? 'jsbar-row--selected' : 'jsbar-row--dimmed');
    const lab = document.createElement('div');
    lab.className = 'jsbar-label';
    lab.textContent = label;
    const track = document.createElement('div');
    track.className = 'jsbar-track';
    const fill = document.createElement('div');
    fill.className = 'jsbar-fill';
    fill.style.width = (max ? (val / max) * 100 : 0) + '%';
    fill.style.background = color;
    track.appendChild(fill);
    const val_ = document.createElement('div');
    val_.className = 'jsbar-value';
    val_.textContent = fmt(val);
    row.appendChild(lab); row.appendChild(track); row.appendChild(val_);
    if (opts.onClick) row.addEventListener('click', () => opts.onClick(catKey));
    containerEl.appendChild(row);
  });
}

function renderTxnTable(containerEl, txns, labelFor, opts) {
  opts = opts || {};
  containerEl.textContent = '';
  if (!txns.length) {
    const p = document.createElement('p');
    p.className = 'card-sub';
    p.textContent = opts.emptyText || 'No transactions this month.';
    containerEl.appendChild(p);
    return;
  }
  const table = document.createElement('table');
  table.className = 'data-table';
  const monthTh = opts.showMonth ? '<th>Month</th>' : '';
  table.innerHTML = `<thead><tr><th>Date</th>${monthTh}<th>Description</th><th>Category</th><th class="num">Amount</th></tr></thead>`;
  const tbody = document.createElement('tbody');
  txns.forEach(t => {
    const tr = document.createElement('tr');
    const d = document.createElement('td'); d.textContent = t.date; tr.appendChild(d);
    if (opts.showMonth) {
      const mo = document.createElement('td'); mo.textContent = opts.monthLabels[t.month] || t.month; tr.appendChild(mo);
    }
    const desc = document.createElement('td'); desc.textContent = t.description; tr.appendChild(desc);
    const cat = document.createElement('td');
    const tag = document.createElement('span'); tag.className = 'cat-tag'; tag.textContent = labelFor(t.category);
    cat.appendChild(tag); tr.appendChild(cat);
    const amt = document.createElement('td'); amt.className = 'num'; amt.textContent = fmtMoney(t.amount);
    tr.appendChild(amt);
    tbody.appendChild(tr);
  });
  table.appendChild(tbody);
  containerEl.appendChild(table);
}

// -------------------------------------------------------- search & deltas
function searchTransactions(DATA, txnKey, query) {
  const q = query.trim().toLowerCase();
  if (!q) return null;
  const results = [];
  DATA.months.forEach(month => {
    (DATA.byMonth[month][txnKey] || []).forEach(t => {
      const hay = (t.description + ' ' + fmtMoney(t.amount) + ' ' + t.amount).toLowerCase();
      if (hay.includes(q)) results.push(Object.assign({ month }, t));
    });
  });
  results.sort((a, b) => (a.date < b.date ? 1 : -1));
  return results;
}

function computeDelta(curr, prev, higherIsBetter) {
  if (curr === null || curr === undefined || prev === null || prev === undefined || prev === 0) return null;
  const pct = (curr - prev) / Math.abs(prev);
  return { pct, good: higherIsBetter ? pct >= 0 : pct <= 0 };
}

function renderDelta(elId, curr, prev, opts) {
  const el = document.getElementById(elId);
  if (!el) return;
  opts = opts || {};
  if (opts.points) {
    // percentage-point delta (for rates like savings rate), not relative %
    if (curr === null || prev === null || prev === undefined) {
      el.textContent = 'first month tracked'; el.className = 'tile-delta neutral'; return;
    }
    const diff = Math.round((curr - prev) * 100);
    if (diff === 0) { el.textContent = 'flat vs last month'; el.className = 'tile-delta neutral'; return; }
    const good = opts.higherIsBetter ? diff > 0 : diff < 0;
    el.textContent = (diff > 0 ? '+' : '') + diff + 'pp vs last month';
    el.className = 'tile-delta ' + (good ? 'good' : 'bad');
    return;
  }
  const delta = computeDelta(curr, prev, opts.higherIsBetter !== false);
  if (!delta) { el.textContent = prev === undefined ? '' : 'first month tracked'; el.className = 'tile-delta neutral'; return; }
  const arrow = delta.pct >= 0 ? '↑' : '↓';
  el.textContent = arrow + ' ' + Math.abs(Math.round(delta.pct * 100)) + '% vs last month';
  el.className = 'tile-delta ' + (delta.good ? 'good' : 'bad');
}

function populateMonthSelect(selectEl, DATA, onChange) {
  DATA.months.forEach((m, i) => {
    const opt = document.createElement('option');
    opt.value = i;
    opt.textContent = DATA.monthLabels[m];
    selectEl.appendChild(opt);
  });
  selectEl.value = DATA.months.length - 1;
  selectEl.addEventListener('change', () => onChange(Number(selectEl.value)));
}

// ------------------------------------------------------- day-level charts
function daysInMonth(monthStr) {
  const [y, m] = monthStr.split('-').map(Number);
  return new Date(y, m, 0).getDate();
}
function totalMonthlyTarget(DATA, categoryKeys) {
  const cats = categoryKeys || Object.keys(DATA.categories);
  let sum = 0;
  cats.forEach(cat => {
    const t = effectiveTarget(DATA, cat);
    if (t) sum += t;
  });
  return sum;
}

function renderTopPurchases(containerEl, txns, labelFor, n) {
  const top = [...txns].sort((a, b) => b.amount - a.amount).slice(0, n || 5);
  renderTxnTable(containerEl, top, labelFor, { emptyText: 'No transactions this month.' });
}

// Every budgeted category (baseline + discretionary), % of target spent so
// far this month, sorted worst-first. Deliberately NOT pace-adjusted, unlike
// the Budget page's meters (categoryResult) - just actual / target, so it
// reads as "how much of this month's budget is gone," not a pacing verdict.
function renderCategoryPercentList(containerEl, DATA, monthIdx, labelFor) {
  const cats = [...DATA.order.baseline, ...DATA.order.discretionary];
  const month = DATA.months[monthIdx];
  const results = cats.map(cat => {
    const target = effectiveTarget(DATA, cat);
    const actual = (DATA.byMonth[month].expenseByCat[cat]) || 0;
    const ratio = target ? actual / target : null;
    return { category: cat, target, actual, ratio, status: statusForRatio(ratio) };
  }).filter(r => r.target !== null);
  results.sort((a, b) => (b.target || 0) - (a.target || 0));

  containerEl.textContent = '';
  if (!results.length) {
    const p = document.createElement('p');
    p.className = 'card-sub';
    p.textContent = 'No categories have a target set.';
    containerEl.appendChild(p);
    return;
  }
  results.forEach(r => {
    const row = document.createElement('div');
    row.className = 'jsbar-row';
    const lab = document.createElement('div');
    lab.className = 'jsbar-label';
    lab.textContent = (labelFor && labelFor[r.category]) || r.category;
    const track = document.createElement('div');
    track.className = 'jsbar-track';
    const fill = document.createElement('div');
    fill.className = 'jsbar-fill';
    fill.style.width = Math.min(100, (r.ratio || 0) * 100) + '%';
    fill.style.background = STATUS_HEX[r.status];
    track.appendChild(fill);
    const val = document.createElement('div');
    val.className = 'jsbar-value';
    val.textContent = r.ratio !== null ? Math.round(r.ratio * 100) + '%' : '—';
    row.appendChild(lab); row.appendChild(track); row.appendChild(val);
    containerEl.appendChild(row);
  });
}

// Running total for the selected month vs. a straight-line pace toward the
// sum of every category's monthly target - same math as the budget meters,
// as an actual chart instead of a single percentage. categoryKeys restricts
// both the spend and the pace target to that set (e.g. discretionary only).
function renderCumulativeChart(containerEl, DATA, monthIdx, categoryKeys) {
  const month = DATA.months[monthIdx];
  const inSet = categoryKeys ? new Set(categoryKeys) : null;
  const txns = DATA.byMonth[month].expenseTxns.filter(t => !inSet || inSet.has(t.category));
  const nDays = daysInMonth(month);
  const dayTotals = new Array(nDays + 1).fill(0);
  txns.forEach(t => { dayTotals[Number(t.date.split('-')[2])] += t.amount; });

  const isCurrent = monthIdx === DATA.months.length - 1;
  const lastDay = isCurrent ? DATA.dayOfMonth : nDays;
  const cumActual = [];
  let running = 0;
  for (let d = 1; d <= lastDay; d++) { running += dayTotals[d]; cumActual.push(running); }

  const target = totalMonthlyTarget(DATA, categoryKeys);
  const cumPace = [];
  for (let d = 1; d <= nDays; d++) cumPace.push(target * d / nDays);

  const W = 760, H = 260, PAD_L = 52, PAD_R = 64, PAD_T = 16, PAD_B = 28;
  const plotW = W - PAD_L - PAD_R, plotH = H - PAD_T - PAD_B;
  const maxVal = (Math.max(target, cumActual[cumActual.length - 1] || 0) * 1.15) || 1;
  const xAt = i => PAD_L + (plotW * i / Math.max(1, nDays - 1));
  const yAt = v => PAD_T + plotH - (v / maxVal * plotH);
  const esc = s => String(s).replace(/"/g, '&quot;');

  let svg = '';
  for (let step = 0; step <= 4; step++) {
    const v = maxVal * step / 4;
    const y = yAt(v);
    svg += `<line x1="${PAD_L}" y1="${y.toFixed(1)}" x2="${W - PAD_R}" y2="${y.toFixed(1)}" stroke="var(--gridline)" stroke-width="1"/>`;
    svg += `<text x="${PAD_L - 8}" y="${(y + 4).toFixed(1)}" text-anchor="end" class="axis-label">${esc(fmtMoney(v))}</text>`;
  }
  const paceD = 'M ' + cumPace.map((v, i) => `${xAt(i).toFixed(1)} ${yAt(v).toFixed(1)}`).join(' L ');
  svg += `<path d="${paceD}" fill="none" stroke="var(--text-muted)" stroke-width="2" stroke-dasharray="5 4" stroke-linecap="round"/>`;
  if (cumActual.length > 1) {
    const actualD = 'M ' + cumActual.map((v, i) => `${xAt(i).toFixed(1)} ${yAt(v).toFixed(1)}`).join(' L ');
    svg += `<path d="${actualD}" fill="none" stroke="var(--series-2)" stroke-width="2" stroke-linejoin="round" stroke-linecap="round"/>`;
  }
  if (cumActual.length) {
    const lx = xAt(cumActual.length - 1), ly = yAt(cumActual[cumActual.length - 1]);
    svg += `<circle cx="${lx.toFixed(1)}" cy="${ly.toFixed(1)}" r="4" fill="var(--series-2)" stroke="var(--surface-1)" stroke-width="2"/>`;
    svg += `<text x="${(lx + 6).toFixed(1)}" y="${(ly + 4).toFixed(1)}" class="end-label" fill="var(--series-2)">${esc(fmtMoney(cumActual[cumActual.length - 1]))}</text>`;
  }
  const plx = xAt(nDays - 1), ply = yAt(target);
  svg += `<text x="${(plx + 6).toFixed(1)}" y="${(ply + 4).toFixed(1)}" class="end-label" fill="var(--text-muted)">${esc(fmtMoney(target))}</text>`;
  for (let d = 1; d <= nDays; d++) {
    if (d === 1 || d === nDays || d % 5 === 0) {
      svg += `<text x="${xAt(d - 1).toFixed(1)}" y="${H - 8}" text-anchor="middle" class="axis-label">${d}</text>`;
    }
  }
  const colW = plotW / nDays;
  for (let d = 1; d <= nDays; d++) {
    const x = xAt(d - 1);
    const rows = [];
    if (d <= lastDay) rows.push(['Cumulative spend', fmtMoney(cumActual[d - 1]), 'var(--series-2)']);
    rows.push(['Pace (even spend)', fmtMoney(cumPace[d - 1]), 'var(--text-muted)']);
    svg += `<rect class="hit-target" data-tooltip-title="Day ${d}" data-tooltip-rows="${esc(JSON.stringify(rows))}" `
         + `x="${(x - colW / 2).toFixed(1)}" y="${PAD_T}" width="${colW.toFixed(1)}" height="${plotH}" fill="transparent"/>`;
  }
  containerEl.innerHTML = `<svg viewBox="0 0 ${W} ${H}" class="chart-svg">${svg}</svg>`;
}

