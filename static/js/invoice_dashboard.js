/* Invoice Dashboard — controller logic.
   Ported from the original Stampli-export HTML, restyled for the Ember
   theme tokens. Three things changed beyond visuals:

   1. Date-basis pills, tab nav, and search/filter wiring driven through
      [data-…] hooks instead of inline onclick — matches our convention
      from operations.js / capital.js so the markup stays readable.
   2. Bi-weekly Period Archive — records get auto-binned into half-month
      slots from their processing_date; each slot is a snapshot the user
      can step back to. The current slot is whatever the latest upload
      covers (Apr 16 – Apr 30, 2026 here).
   3. Upload modal sniffs the dropped file's name / contents to identify
      its period, previews the detected dates before commit, and adds it
      to the archive on save.

   The original chart logic is intact — we only swap color tokens. */

(function () {
  'use strict';
  const D = window.INVOICE_D;
  const GL_COLORS = window.INVOICE_GL_COLORS;

  // ──────────────────────────────────────────────────────────
  // Number / text formatters
  // ──────────────────────────────────────────────────────────
  const fmt  = n => (n == null) ? '—' :
    '$' + parseFloat(n).toLocaleString('en-US',
      { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  const fmtK = n => {
    if (!n) return '$0';
    const a = Math.abs(n);
    if (a >= 1_000_000) return '$' + (n / 1_000_000).toFixed(1) + 'M';
    if (a >= 1_000)     return '$' + (n / 1_000).toFixed(0)     + 'K';
    return fmt(n);
  };
  const pct = (a, b) => b ? (a / b * 100).toFixed(1) + '%' : '—';

  function statusChip(s) {
    const map = {
      'Paid Invoice':                          'status-paid',
      'Authorized for Payment':                'status-auth',
      'Approved and Pending AP Authorization': 'status-approved',
      'Invoice Details Have Been Registered.': 'status-registered',
      'Pending Approval':                      'status-pending',
      'Canceled Invoice':                      'status-canceled',
    };
    const cls = map[s] || 'status-pending';
    return `<span class="status-chip ${cls}">${s}</span>`;
  }
  const entityChip = e => e.includes('Ember')
    ? '<span class="entity-chip ember">Ember</span>'
    : '<span class="entity-chip ccdl">CCDL</span>';
  const glChip = gl => gl === 'Not Assigned'
    ? '<span class="gl-chip unassigned">Not Assigned</span>'
    : `<span class="gl-chip" title="${gl}">${gl}</span>`;

  // ──────────────────────────────────────────────────────────
  // Bi-weekly period bucketing — every uploaded file lands in a
  // half-month slot (1–15 or 16–EOM) keyed by processing_date.
  // ──────────────────────────────────────────────────────────
  const MONTH_NAMES = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
  function periodOf(dateStr) {
    // 2026-04-22 → {start:'2026-04-16', end:'2026-04-30', key, label, short}
    const [y, m, d] = dateStr.split('-').map(Number);
    const dom = d;
    const firstHalf = dom <= 15;
    const startD = firstHalf ? 1 : 16;
    const endD   = firstHalf ? 15 : new Date(y, m, 0).getDate();
    const pad = n => String(n).padStart(2, '0');
    const start = `${y}-${pad(m)}-${pad(startD)}`;
    const end   = `${y}-${pad(m)}-${pad(endD)}`;
    const key   = `${y}-${pad(m)}-${firstHalf ? 'A' : 'B'}`;
    const mo    = MONTH_NAMES[m - 1];
    const label = `${mo} ${startD} – ${mo} ${endD}, ${y}`;
    const short = `${mo.toUpperCase()} ${startD}–${endD}`;
    return { start, end, key, label, short, year: y };
  }
  function buildPeriodArchive() {
    // Empty-state guard: D.records is undefined when no uploads exist.
    // Without this, .forEach throws at IIFE load time and kills the
    // entire script — including the DOMContentLoaded handler that
    // wires the file input's change event. That manifests as "the
    // file picker opens but selecting a file does nothing."
    if (!D || !Array.isArray(D.records)) return [];
    const buckets = {};
    D.records.forEach(r => {
      const p = periodOf(r.processing_date);
      if (!buckets[p.key]) buckets[p.key] = { ...p, records: [], ember: 0, ccdl: 0, total: 0, count: 0 };
      const b = buckets[p.key];
      b.records.push(r);
      b.count += 1;
      b.total += r.amount || 0;
      if (r.entity.includes('Ember')) b.ember += 1; else b.ccdl += 1;
    });
    // Sort newest → oldest
    const all = Object.values(buckets).sort((a, b) => b.start.localeCompare(a.start));
    // The "current" period is whichever bucket holds the cp_proc records.
    const cpKey = (() => {
      const sample = D.records.find(r => r.is_cp_proc);
      return sample ? periodOf(sample.processing_date).key : (all[0] && all[0].key);
    })();
    all.forEach(p => p.is_current = (p.key === cpKey));
    return all;
  }
  const PERIODS = buildPeriodArchive();
  // Which period is the user currently viewing in the "This Period" tab?
  // Empty-state → null (no period selected yet, no records loaded).
  let activePeriodKey = (PERIODS.find(p => p.is_current) || PERIODS[0] || {}).key || null;

  // ──────────────────────────────────────────────────────────
  // Date basis (Invoice Date / Processing Date)
  // ──────────────────────────────────────────────────────────
  let basis = 'proc';
  const monthKey      = r => basis === 'inv' ? r.inv_month     : r.proc_month;
  const activeMonths  = () => basis === 'inv' ? D.months_inv   : D.months_proc;
  const activeMthly   = () => basis === 'inv' ? D.monthly_inv  : D.monthly_proc;
  const activeCPSt    = () => basis === 'inv' ? D.cp_inv       : D.cp_proc;
  const basisLabel    = () => basis === 'inv' ? 'Invoice Date' : 'Processing Date';
  const isCP = r => {
    // True when this record falls inside the *active* archive period.
    // Falls back to source flags for the latest period.
    const ap = PERIODS.find(p => p.key === activePeriodKey);
    if (!ap) return false;
    const d = basis === 'inv' ? r.invoice_date : r.processing_date;
    return d >= ap.start && d <= ap.end;
  };

  function setDateBasis(b) {
    basis = b;
    document.querySelectorAll('[data-basis]').forEach(el => {
      el.classList.toggle('is-active', el.dataset.basis === b);
    });
    const lbl = basisLabel();
    const isProc = b === 'proc';
    ['ov-basis-badge','monthly-tab-badge','cp-chart-badge','ember-chart-badge','ccdl-chart-badge'].forEach(id => {
      const el = document.getElementById(id);
      if (!el) return;
      el.textContent = lbl;
      el.className = 'ct-badge' + (isProc ? ' proc' : '');
    });
    syncCpBanner();
    updateKPIs(); updateCharts(); buildMonthlyTable(); buildPeriodTable();
    rebuildMonthFilter();
    renderAllTables();
  }

  // ──────────────────────────────────────────────────────────
  // Tab nav
  // ──────────────────────────────────────────────────────────
  function showTab(id) {
    document.querySelectorAll('.inv-panel').forEach(p => p.classList.remove('is-active'));
    document.querySelectorAll('.inv-tab').forEach(t => t.classList.remove('is-active'));
    const panel = document.getElementById('panel-' + id);
    const tab   = document.querySelector(`.inv-tab[data-tab="${id}"]`);
    if (panel) panel.classList.add('is-active');
    if (tab)   tab.classList.add('is-active');
  }

  // ──────────────────────────────────────────────────────────
  // KPI rendering — pulls from D.ytd + active period state.
  // ──────────────────────────────────────────────────────────
  function activePeriodStats() {
    const ap = PERIODS.find(p => p.key === activePeriodKey);
    if (!ap) return { ember:{count:0,total:0}, ccdl:{count:0,total:0}, combined:{count:0,total:0} };
    let e = {count:0,total:0}, c = {count:0,total:0};
    ap.records.forEach(r => {
      const t = r.entity.includes('Ember') ? e : c;
      t.count += 1; t.total += (r.amount || 0);
    });
    return { ember: e, ccdl: c, combined: { count: e.count + c.count, total: e.total + c.total } };
  }

  function updateKPIs() {
    const y  = D.ytd;
    const cp = activePeriodStats();

    const set = (id, v) => { const el = document.getElementById(id); if (el) el.textContent = v; };

    set('o-count',     y.combined.count);
    set('o-total',     fmtK(y.combined.total));
    set('o-paid',      fmtK(y.combined.paid));
    set('o-paid-pct',  pct(y.combined.paid, y.combined.total) + ' of total');
    set('o-notpaid',   fmtK(y.combined.not_paid));
    set('o-notpaid-pct', pct(y.combined.not_paid, y.combined.total) + ' of total');
    set('o-dtp',       y.combined.avg_days_to_pay ? y.combined.avg_days_to_pay + ' days' : '—');
    set('o-lag',       y.combined.avg_lag ? y.combined.avg_lag + ' days' : '—');
    set('o-cp-count',  '+' + cp.combined.count + ' this period');
    set('o-cp-total',  '+' + fmtK(cp.combined.total) + ' this period');

    ['ember','ccdl'].forEach((k, i) => {
      const p = i === 0 ? 'e' : 'c';
      set(p + '-count',          D.ytd[k].count);
      set(p + '-total',          fmt(D.ytd[k].total));
      set(p + '-paid',           fmt(D.ytd[k].paid));
      set(p + '-notpaid',        fmt(D.ytd[k].not_paid));
      set(p + '-dtp',            D.ytd[k].avg_days_to_pay ? D.ytd[k].avg_days_to_pay + ' d' : '—');
      set(p + '-cp',             cp[k].count + ' inv · ' + fmtK(cp[k].total));
      set(p + '2-count',         D.ytd[k].count);
      set(p + '2-total',         fmt(D.ytd[k].total));
      set(p + '2-paid',          fmt(D.ytd[k].paid));
      set(p + '2-paid-pct',      pct(D.ytd[k].paid, D.ytd[k].total)     + ' of total');
      set(p + '2-notpaid',       fmt(D.ytd[k].not_paid));
      set(p + '2-notpaid-pct',   pct(D.ytd[k].not_paid, D.ytd[k].total) + ' of total');
    });

    set('cp-count', cp.combined.count);
    set('cp-total', fmtK(cp.combined.total));
    set('cp-ember', cp.ember.count + ' inv');
    set('cp-ccdl',  cp.ccdl.count  + ' inv');
  }

  function syncCpBanner() {
    const ap = PERIODS.find(p => p.key === activePeriodKey);
    if (!ap) return;
    const isCurrent = ap.is_current;
    const titleEl = document.getElementById('cp-banner-title');
    const subEl   = document.getElementById('cp-banner-sub');
    const tagEl   = document.getElementById('cp-banner-eyebrow');
    if (tagEl)   tagEl.textContent = isCurrent ? 'Current Update' : 'Archive Snapshot';
    if (titleEl) titleEl.textContent = ap.label;
    if (subEl)   subEl.textContent = (basis === 'inv'
      ? 'Invoices with invoice date in this period'
      : 'Invoices with processing date in this period');
  }

  // ──────────────────────────────────────────────────────────
  // Charts — Chart.js wired with Ember palette
  // ──────────────────────────────────────────────────────────
  const EMBER = '#13344E';   // ember-ink-2
  const CCDL  = '#1F7A4D';   // ember-good
  const PAID  = '#1F7A4D';
  const UNPAID = '#F25929';  // ember-orange
  const HILITE = '#F25929';
  const MUTED  = 'rgba(19,52,78,0.45)';

  // Soft gridlines that don't fight the paper bg
  function gridOpts() {
    return {
      ticks: { color: '#5B6B7B', font: { size: 10, family: 'DM Sans, sans-serif' } },
      grid:  { color: 'rgba(8,35,59,0.06)' },
    };
  }
  Chart.defaults.font.family = 'DM Sans, "Plus Jakarta Sans", sans-serif';
  Chart.defaults.color = '#5B6B7B';
  Chart.defaults.plugins.tooltip.backgroundColor = '#0B1F33';
  Chart.defaults.plugins.tooltip.titleColor = '#F4ECDD';
  Chart.defaults.plugins.tooltip.bodyColor  = '#F4ECDD';
  Chart.defaults.plugins.tooltip.padding = 10;
  Chart.defaults.plugins.tooltip.cornerRadius = 6;

  const charts = {};
  function buildOrUpdate(id, config) {
    if (charts[id]) charts[id].destroy();
    const el = document.getElementById(id);
    if (!el) return;
    charts[id] = new Chart(el, config);
  }

  function buildCharts() {
    const gls = D.gl_accounts.slice(0, 10);
    buildOrUpdate('glDonut', {
      type: 'doughnut',
      data: {
        labels: gls.map(g => g.gl),
        datasets: [{
          data: gls.map(g => g.total),
          backgroundColor: gls.map(g => GL_COLORS[g.gl] || EMBER),
          borderWidth: 2, borderColor: '#FFFFFF',
        }],
      },
      options: {
        responsive: true, maintainAspectRatio: false, cutout: '58%',
        plugins: {
          legend: {
            position: 'right',
            labels: {
              font: { size: 10 }, boxWidth: 10,
              generateLabels: ch => ch.data.labels.map((l, i) => ({
                text: l + ' · ' + fmtK(ch.data.datasets[0].data[i]),
                fillStyle:   ch.data.datasets[0].backgroundColor[i],
                strokeStyle: ch.data.datasets[0].backgroundColor[i],
                index: i,
              })),
            },
          },
          tooltip: { callbacks: { label: c => ' ' + fmt(c.raw) } },
        },
      },
    });

    buildOrUpdate('glBar', {
      type: 'bar',
      data: {
        labels: D.gl_accounts.map(g => g.gl),
        datasets: [{
          label: 'Total',
          data: D.gl_accounts.map(g => g.total),
          backgroundColor: D.gl_accounts.map(g => GL_COLORS[g.gl] || EMBER),
          borderRadius: 4,
        }],
      },
      options: {
        indexAxis: 'y', responsive: true, maintainAspectRatio: false,
        plugins: {
          legend: { display: false },
          tooltip: { callbacks: { label: c => ' ' + fmt(c.raw) } },
        },
        scales: { x: { ...gridOpts(), ticks: { ...gridOpts().ticks, callback: v => fmtK(v) } }, y: gridOpts() },
      },
    });

    buildOrUpdate('glPie', {
      type: 'pie',
      data: {
        labels: D.gl_accounts.map(g => g.gl),
        datasets: [{
          data: D.gl_accounts.map(g => g.total),
          backgroundColor: D.gl_accounts.map(g => GL_COLORS[g.gl] || EMBER),
          borderWidth: 2, borderColor: '#FFFFFF',
        }],
      },
      options: {
        responsive: true, maintainAspectRatio: false,
        plugins: {
          legend: { position: 'right', labels: { font: { size: 10 }, boxWidth: 10 } },
          tooltip: { callbacks: { label: c => ' ' + fmt(c.raw) } },
        },
      },
    });
    updateCharts();
  }

  function updateCharts() {
    const months = activeMonths();
    const mthly  = activeMthly();
    const labels = months.map(m => mthly[m].label);
    const eV = months.map(m => mthly[m].ember.total);
    const cV = months.map(m => mthly[m].ccdl.total);
    // Highlight the month containing the *active* period (not just current).
    const ap = PERIODS.find(p => p.key === activePeriodKey);
    const cpMonth = ap ? ap.start.slice(0, 7) : null;

    buildOrUpdate('monthlyBar', {
      type: 'bar',
      data: { labels, datasets: [
        { label: 'Ember Group',  data: eV, backgroundColor: EMBER, borderRadius: 4 },
        { label: 'CCDL Ventures',data: cV, backgroundColor: CCDL,  borderRadius: 4 },
      ]},
      options: {
        responsive: true, maintainAspectRatio: false,
        plugins: {
          legend: { position: 'top', labels: { boxWidth: 10, font: { size: 11 } } },
          tooltip: { callbacks: { label: c => ' ' + c.dataset.label + ': ' + fmt(c.raw) } },
        },
        scales: { y: { ...gridOpts(), ticks: { ...gridOpts().ticks, callback: v => fmtK(v) } }, x: gridOpts() },
      },
    });
    buildOrUpdate('monthlyLine', {
      type: 'line',
      data: { labels, datasets: [
        { label: 'Ember Group',  data: eV, borderColor: EMBER, backgroundColor: 'rgba(19,52,78,0.10)', tension: .3, fill: true, pointRadius: 5, pointBackgroundColor: EMBER },
        { label: 'CCDL Ventures',data: cV, borderColor: CCDL,  backgroundColor: 'rgba(31,122,77,0.10)', tension: .3, fill: true, pointRadius: 5, pointBackgroundColor: CCDL },
      ]},
      options: {
        responsive: true, maintainAspectRatio: false,
        plugins: {
          legend: { position: 'top', labels: { boxWidth: 10, font: { size: 11 } } },
          tooltip: { callbacks: { label: c => ' ' + c.dataset.label + ': ' + fmt(c.raw) } },
        },
        scales: { y: { ...gridOpts(), ticks: { ...gridOpts().ticks, callback: v => fmtK(v) } }, x: gridOpts() },
      },
    });
    buildOrUpdate('emberMonthly', {
      type: 'bar',
      data: { labels, datasets: [
        { label: 'Paid Invoice', data: months.map(m => mthly[m].ember.paid),     backgroundColor: PAID,   borderRadius: 4 },
        { label: 'Not Yet Paid', data: months.map(m => mthly[m].ember.not_paid), backgroundColor: UNPAID, borderRadius: 4 },
      ]},
      options: {
        responsive: true, maintainAspectRatio: false,
        plugins: {
          legend: { position: 'top', labels: { boxWidth: 10, font: { size: 11 } } },
          tooltip: { callbacks: { label: c => ' ' + c.dataset.label + ': ' + fmt(c.raw) } },
        },
        scales: { x: { ...gridOpts(), stacked: true }, y: { ...gridOpts(), stacked: true, ticks: { ...gridOpts().ticks, callback: v => fmtK(v) } } },
      },
    });
    buildOrUpdate('ccdlMonthly', {
      type: 'bar',
      data: { labels, datasets: [
        { label: 'Paid Invoice', data: months.map(m => mthly[m].ccdl.paid),     backgroundColor: PAID,   borderRadius: 4 },
        { label: 'Not Yet Paid', data: months.map(m => mthly[m].ccdl.not_paid), backgroundColor: UNPAID, borderRadius: 4 },
      ]},
      options: {
        responsive: true, maintainAspectRatio: false,
        plugins: {
          legend: { position: 'top', labels: { boxWidth: 10, font: { size: 11 } } },
          tooltip: { callbacks: { label: c => ' ' + c.dataset.label + ': ' + fmt(c.raw) } },
        },
        scales: { x: { ...gridOpts(), stacked: true }, y: { ...gridOpts(), stacked: true, ticks: { ...gridOpts().ticks, callback: v => fmtK(v) } } },
      },
    });
    const pvC = months.map(m => m === cpMonth ? HILITE : MUTED);
    buildOrUpdate('cpCompare', {
      type: 'bar',
      data: { labels, datasets: [{
        label: 'Combined',
        data: months.map(m => mthly[m].ember.total + mthly[m].ccdl.total),
        backgroundColor: pvC, borderRadius: 4,
      }]},
      options: {
        responsive: true, maintainAspectRatio: false,
        plugins: { legend: { display: false }, tooltip: { callbacks: { label: c => ' ' + fmt(c.raw) } } },
        scales: { y: { ...gridOpts(), ticks: { ...gridOpts().ticks, callback: v => fmtK(v) } }, x: gridOpts() },
      },
    });
    let run = 0;
    const rd = months.map(m => { run += mthly[m].ember.total + mthly[m].ccdl.total; return run; });
    buildOrUpdate('cpRunning', {
      type: 'line',
      data: { labels, datasets: [{
        label: 'YTD Cumulative', data: rd,
        borderColor: EMBER, backgroundColor: 'rgba(19,52,78,0.12)',
        tension: .3, fill: true, pointRadius: 5, pointBackgroundColor: EMBER,
      }]},
      options: {
        responsive: true, maintainAspectRatio: false,
        plugins: { legend: { display: false }, tooltip: { callbacks: { label: c => ' ' + fmt(c.raw) } } },
        scales: { y: { ...gridOpts(), ticks: { ...gridOpts().ticks, callback: v => fmtK(v) } }, x: gridOpts() },
      },
    });
  }

  // ──────────────────────────────────────────────────────────
  // Monthly table
  // ──────────────────────────────────────────────────────────
  function buildMonthlyTable() {
    const months = activeMonths();
    const mthly  = activeMthly();
    const ap = PERIODS.find(p => p.key === activePeriodKey);
    const cpMonth = ap ? ap.start.slice(0, 7) : null;
    let html = `<thead><tr>
      <th>Month</th><th>Entity</th>
      <th class="r">Invoices</th><th class="r">Total</th>
      <th class="r">Paid Invoice</th><th class="r">Not Yet Paid</th>
    </tr></thead><tbody>`;
    const agg = {
      e: { count:0, total:0, paid:0, not_paid:0 },
      c: { count:0, total:0, paid:0, not_paid:0 },
    };
    months.forEach(m => {
      const md = mthly[m];
      const isCp = m === cpMonth;
      const hdr = isCp ? ' class="ent-hdr is-cp"' : ' class="ent-hdr"';
      if (md.ember.count > 0 || md.ccdl.count > 0) {
        html += `<tr${hdr}><td colspan="6">${md.label}${isCp ? ' · Contains active period' : ''}</td></tr>`;
      }
      if (md.ember.count > 0) {
        html += `<tr><td></td><td>Ember Group LLC</td><td>${md.ember.count}</td><td>${fmt(md.ember.total)}</td><td>${fmt(md.ember.paid)}</td><td>${fmt(md.ember.not_paid)}</td></tr>`;
        agg.e.count += md.ember.count; agg.e.total += md.ember.total;
        agg.e.paid += md.ember.paid; agg.e.not_paid += md.ember.not_paid;
      }
      if (md.ccdl.count > 0) {
        html += `<tr><td></td><td>CCDL Ventures LLC</td><td>${md.ccdl.count}</td><td>${fmt(md.ccdl.total)}</td><td>${fmt(md.ccdl.paid)}</td><td>${fmt(md.ccdl.not_paid)}</td></tr>`;
        agg.c.count += md.ccdl.count; agg.c.total += md.ccdl.total;
        agg.c.paid += md.ccdl.paid; agg.c.not_paid += md.ccdl.not_paid;
      }
    });
    html += `<tr class="total-row"><td colspan="2">YTD — Ember Group</td><td>${agg.e.count}</td><td>${fmt(agg.e.total)}</td><td>${fmt(agg.e.paid)}</td><td>${fmt(agg.e.not_paid)}</td></tr>`;
    html += `<tr class="total-row"><td colspan="2">YTD — CCDL Ventures</td><td>${agg.c.count}</td><td>${fmt(agg.c.total)}</td><td>${fmt(agg.c.paid)}</td><td>${fmt(agg.c.not_paid)}</td></tr>`;
    html += `<tr class="grand-row"><td colspan="2">GRAND TOTAL</td><td>${agg.e.count + agg.c.count}</td><td>${fmt(agg.e.total + agg.c.total)}</td><td class="paid">${fmt(agg.e.paid + agg.c.paid)}</td><td class="unpaid">${fmt(agg.e.not_paid + agg.c.not_paid)}</td></tr>`;
    document.getElementById('monTable').innerHTML = html + '</tbody>';
  }

  // ──────────────────────────────────────────────────────────
  // This Period table (active period only)
  // ──────────────────────────────────────────────────────────
  function buildPeriodTable() {
    const cur = D.records.filter(r => isCP(r));
    document.getElementById('cpTbody').innerHTML = cur.map(r => {
      const dt = r.dates_differ ? '<span class="diff-tag" title="Invoice and processing months differ">⚠</span>' : '';
      return `<tr class="cp-row${r.dates_differ ? ' diff-row' : ''}">
        <td>${entityChip(r.entity)}</td>
        <td class="date-cell">${r.invoice_date}</td>
        <td class="date-cell">${r.processing_date}${dt}</td>
        <td>${r.vendor}</td>
        <td>${glChip(r.gl)}</td>
        <td class="desc-cell" title="${r.description || ''}">${r.description || '—'}</td>
        <td>${r.invoice_no}</td>
        <td>${statusChip(r.status)}</td>
        <td class="amount-cell">${fmt(r.amount)}</td>
      </tr>`;
    }).join('');
  }

  // ──────────────────────────────────────────────────────────
  // Tables (GL / Ember / CCDL / All)
  // ──────────────────────────────────────────────────────────
  const diffTag = r => r.dates_differ ? '<span class="diff-tag" title="Months differ">⚠</span>' : '';
  const newTag  = r => isCP(r) ? '<span class="new-tag">NEW</span>' : '';
  const rc      = r => (r.dates_differ ? 'diff-row ' : '') + (isCP(r) ? 'cp-row' : '');

  function rowAll(r) {
    const c = rc(r);
    return `<tr${c ? ` class="${c}"` : ''}>
      <td class="date-cell">${r.invoice_date}</td>
      <td class="date-cell">${r.processing_date}${diffTag(r)}</td>
      <td>${entityChip(r.entity)}</td>
      <td>${r.vendor}</td>
      <td>${glChip(r.gl)}</td>
      <td class="desc-cell" title="${r.description || ''}">${r.description || '—'}</td>
      <td>${r.invoice_no}${newTag(r)}</td>
      <td>${statusChip(r.status)}</td>
      <td class="date-cell">${r.payment_date || '—'}</td>
      <td class="amount-cell">${fmt(r.amount)}</td>
    </tr>`;
  }
  function rowEnt(r) {
    const c = rc(r);
    return `<tr${c ? ` class="${c}"` : ''}>
      <td class="date-cell">${r.invoice_date}</td>
      <td class="date-cell">${r.processing_date}${diffTag(r)}</td>
      <td>${r.vendor}</td>
      <td>${glChip(r.gl)}</td>
      <td class="desc-cell" title="${r.description || ''}">${r.description || '—'}</td>
      <td>${r.invoice_no}${newTag(r)}</td>
      <td>${statusChip(r.status)}</td>
      <td class="date-cell">${r.payment_date || '—'}</td>
      <td class="amount-cell">${fmt(r.amount)}</td>
    </tr>`;
  }
  function rowGl(r) {
    const c = rc(r);
    return `<tr${c ? ` class="${c}"` : ''}>
      <td class="date-cell">${r.invoice_date}</td>
      <td class="date-cell">${r.processing_date}${diffTag(r)}</td>
      <td>${entityChip(r.entity)}</td>
      <td>${r.vendor}</td>
      <td>${glChip(r.gl)}</td>
      <td class="desc-cell" title="${r.description || ''}">${r.description || '—'}</td>
      <td>${r.invoice_no}${newTag(r)}</td>
      <td>${statusChip(r.status)}</td>
      <td class="amount-cell">${fmt(r.amount)}</td>
    </tr>`;
  }

  let allRows, emberRows, ccdlRows;
  const renderTbl = (id, rows, fn) => {
    document.getElementById(id).innerHTML = rows.map(fn).join('');
  };
  function renderAllTables() {
    renderTbl('glTbody',    allRows,   rowGl);
    renderTbl('emberTbody', emberRows, rowEnt);
    renderTbl('ccdlTbody',  ccdlRows,  rowEnt);
    renderTbl('allTbody',   allRows,   rowAll);
    buildPeriodTable();
    document.getElementById('gl-count').textContent    = allRows.length   + ' invoices';
    document.getElementById('ember-count').textContent = emberRows.length + ' invoices';
    document.getElementById('ccdl-count').textContent  = ccdlRows.length  + ' invoices';
    document.getElementById('all-count').textContent   = allRows.length   + ' invoices';
  }
  function buildTables() {
    allRows   = D.records;
    emberRows = D.records.filter(r => r.entity === 'Ember Group LLC');
    ccdlRows  = D.records.filter(r => r.entity === 'CCDL Ventures LLC');
    renderAllTables();
  }

  function filterGlTable() {
    const s   = document.getElementById('gl-search').value.toLowerCase();
    const gl  = document.getElementById('gl-gl-filter').value;
    const ent = document.getElementById('gl-ent-filter').value;
    const f = allRows.filter(r =>
      (!s || r.vendor.toLowerCase().includes(s) || r.invoice_no.toLowerCase().includes(s)
          || r.description.toLowerCase().includes(s) || r.gl.toLowerCase().includes(s))
      && (!gl || r.gl === gl)
      && (!ent || r.entity === ent));
    renderTbl('glTbody', f, rowGl);
    document.getElementById('gl-count').textContent = f.length + ' of ' + allRows.length;
  }
  function filterEntityTbl(which) {
    const base = which === 'ember' ? emberRows : ccdlRows;
    const s   = document.getElementById(which + '-search').value.toLowerCase();
    const gl  = document.getElementById(which + '-gl-filter').value;
    const sta = document.getElementById(which + '-status-filter').value;
    const f = base.filter(r =>
      (!s || r.vendor.toLowerCase().includes(s) || r.invoice_no.toLowerCase().includes(s)
          || r.description.toLowerCase().includes(s) || r.gl.toLowerCase().includes(s))
      && (!gl || r.gl === gl)
      && (!sta || r.status === sta));
    renderTbl(which + 'Tbody', f, rowEnt);
    document.getElementById(which + '-count').textContent = f.length + ' of ' + base.length;
  }
  function filterAllTbl() {
    const s   = document.getElementById('all-search').value.toLowerCase();
    const ent = document.getElementById('all-ent-filter').value;
    const mon = document.getElementById('all-month-filter').value;
    const gl  = document.getElementById('all-gl-filter').value;
    const sta = document.getElementById('all-status-filter').value;
    const f = allRows.filter(r =>
      (!s || r.vendor.toLowerCase().includes(s) || r.invoice_no.toLowerCase().includes(s)
          || r.description.toLowerCase().includes(s) || r.gl.toLowerCase().includes(s)
          || r.entity.toLowerCase().includes(s))
      && (!ent || r.entity === ent)
      && (!mon || (basis === 'inv' ? r.inv_month : r.proc_month) === mon)
      && (!gl  || r.gl === gl)
      && (!sta || r.status === sta));
    renderTbl('allTbody', f, rowAll);
    document.getElementById('all-count').textContent = f.length + ' of ' + allRows.length;
  }
  function rebuildMonthFilter() {
    const sel = document.getElementById('all-month-filter');
    const months = activeMonths();
    const mthly  = activeMthly();
    sel.innerHTML = '<option value="">All Months</option>'
      + months.map(m => `<option value="${m}">${mthly[m].label}</option>`).join('');
  }
  function populateDropdowns() {
    const gls = [...new Set(D.records.map(r => r.gl))].sort();
    const statuses = [...new Set(D.records.map(r => r.status))].sort();
    ['gl-gl-filter','all-gl-filter','ember-gl-filter','ccdl-gl-filter'].forEach(id => {
      const el = document.getElementById(id); if (!el) return;
      gls.forEach(g => {
        const o = document.createElement('option');
        o.value = g; o.textContent = g; el.appendChild(o);
      });
    });
    ['all-status-filter','ember-status-filter','ccdl-status-filter'].forEach(id => {
      const el = document.getElementById(id); if (!el) return;
      statuses.forEach(s => {
        const o = document.createElement('option');
        o.value = s; o.textContent = s; el.appendChild(o);
      });
    });
  }

  const sortState = {};
  function sortTbl(tbodyId, col) {
    const tbody = document.getElementById(tbodyId);
    const rows  = Array.from(tbody.querySelectorAll('tr'));
    const key   = tbodyId + '_' + col;
    sortState[key] = !sortState[key];
    const asc = sortState[key];
    rows.sort((a, b) => {
      let av = (a.cells[col]?.textContent || '').trim().replace(/[$,⚠]/g, '');
      let bv = (b.cells[col]?.textContent || '').trim().replace(/[$,⚠]/g, '');
      const an = parseFloat(av), bn = parseFloat(bv);
      if (!isNaN(an) && !isNaN(bn)) return asc ? an - bn : bn - an;
      return asc ? av.localeCompare(bv) : bv.localeCompare(av);
    });
    rows.forEach(r => tbody.appendChild(r));
  }

  function buildVendorBars(id, entity) {
    const rows = D.records.filter(r => r.entity === entity);
    const map = {};
    rows.forEach(r => {
      if (!map[r.vendor]) map[r.vendor] = { paid: 0, unpaid: 0 };
      if (r.is_paid) map[r.vendor].paid   += (r.amount || 0);
      else           map[r.vendor].unpaid += (r.amount || 0);
    });
    const vendors = Object.entries(map)
      .map(([vendor, v]) => ({ vendor, paid: v.paid, unpaid: v.unpaid, total: v.paid + v.unpaid }))
      .sort((a, b) => b.total - a.total).slice(0, 10);
    const max = Math.max(...vendors.map(v => v.total));
    const legend = `<div class="vbar-legend">
      <div class="vbar-legend-item"><div class="vbar-legend-dot" style="background:var(--eax-good)"></div>Paid</div>
      <div class="vbar-legend-item"><div class="vbar-legend-dot" style="background:var(--eax-accent)"></div>Not Paid</div>
    </div>`;
    document.getElementById(id).innerHTML = legend + vendors.map(v => {
      const pw = (v.paid / max * 100).toFixed(1);
      const uw = (v.unpaid / max * 100).toFixed(1);
      const pLbl = v.paid   > max * 0.09 ? fmtK(v.paid)   : '';
      const uLbl = v.unpaid > max * 0.09 ? fmtK(v.unpaid) : '';
      return `<div class="vbar-row">
        <div class="vbar-label" title="${v.vendor}">${v.vendor}</div>
        <div class="vbar-track">
          ${v.paid   > 0 ? `<div class="vbar-fill vbar-paid"   style="width:${pw}%" title="Paid: ${fmt(v.paid)}">${pLbl}</div>`     : ''}
          ${v.unpaid > 0 ? `<div class="vbar-fill vbar-unpaid" style="width:${uw}%" title="Not Paid: ${fmt(v.unpaid)}">${uLbl}</div>` : ''}
        </div>
        <div class="vbar-total">${fmtK(v.total)}</div>
      </div>`;
    }).join('');
  }

  function buildGlCards() {
    document.getElementById('glCards').innerHTML = D.gl_accounts.map(g => {
      const clr = GL_COLORS[g.gl] || EMBER;
      const isNA = g.gl === 'Not Assigned';
      return `<div class="gl-card${isNA ? ' is-unassigned' : ''}" style="border-left-color:${isNA ? 'var(--eax-accent)' : clr}">
        <div class="gl-card-name">${g.gl}</div>
        <div class="gl-card-total" style="${isNA ? '' : `color:${clr}`}">${fmt(g.total)}</div>
        <div class="gl-card-count">${g.count} invoice${g.count !== 1 ? 's' : ''}</div>
      </div>`;
    }).join('');
  }

  // ──────────────────────────────────────────────────────────
  // Period Archive rail
  // ──────────────────────────────────────────────────────────
  function renderPeriodArchive() {
    const row = document.getElementById('arc-row');
    // Only show year separators when the archive actually spans years —
    // with a single year (the common case for the first ~12 months),
    // the rail reads cleaner without them.
    const distinctYears = new Set(PERIODS.map(p => p.year));
    const showYearHeaders = distinctYears.size > 1;
    let html = '';
    let lastYear = null;
    PERIODS.forEach(p => {
      if (showYearHeaders && p.year !== lastYear) {
        html += `<div class="arc-rail-year">${p.year}</div>`;
        lastYear = p.year;
      }
      const isActive = p.key === activePeriodKey;
      html += `<div class="arc-card${isActive ? ' is-active' : ''}" data-period="${p.key}" role="button" tabindex="0">
        ${p.is_current ? '<span class="arc-tag">Latest</span>' : ''}
        <div class="arc-dates">${p.short}<span class="arc-year">${p.year}</span></div>
        <div class="arc-meta">Upload &middot; ${formatUploadDate(p.end)}</div>
        <div class="arc-stats">
          <span class="arc-amount">${fmtK(p.total)}</span>
          <span class="arc-count">${p.count} inv &middot; ${p.ember}E / ${p.ccdl}C</span>
        </div>
      </div>`;
    });
    row.innerHTML = html;
    row.querySelectorAll('.arc-card').forEach(btn => {
      btn.addEventListener('click', () => setActivePeriod(btn.dataset.period));
      btn.addEventListener('keydown', (e) => {
        if (e.key === 'Enter' || e.key === ' ') {
          e.preventDefault();
          setActivePeriod(btn.dataset.period);
        }
      });
    });
  }
  function formatUploadDate(yyyymmdd) {
    const [y, m, d] = yyyymmdd.split('-').map(Number);
    return `${MONTH_NAMES[m - 1]} ${d}, ${y}`;
  }
  function setActivePeriod(key) {
    activePeriodKey = key;
    document.querySelectorAll('.arc-card').forEach(c => {
      c.classList.toggle('is-active', c.dataset.period === key);
    });
    syncCpBanner();
    updateKPIs();
    updateCharts();
    buildMonthlyTable();
    renderAllTables();
  }

  // ──────────────────────────────────────────────────────────
  // Upload modal — sniff file name for ISO-date or "YYYY_MM_DD"
  // pattern and report the detected period.
  // ──────────────────────────────────────────────────────────
  function openUploadModal() {
    document.getElementById('upload-modal').classList.add('is-open');
    document.getElementById('upload-detected').classList.remove('is-shown');
  }
  function closeUploadModal() {
    document.getElementById('upload-modal').classList.remove('is-open');
  }
  function detectPeriodFromFilename(name) {
    // Matches "2026_05_08", "2026-05-08", "20260508".
    const m1 = name.match(/(\d{4})[-_]?(\d{2})[-_]?(\d{2})/);
    if (!m1) return null;
    const [_, y, mm, d] = m1;
    return periodOf(`${y}-${mm}-${d}`);
  }
  function handleFileSelection(file) {
    const det = document.getElementById('upload-detected');
    const fname = file.name;
    const p = detectPeriodFromFilename(fname);
    let html = `<div class="det-row"><span>File</span><b>${fname}</b></div>`;
    if (p) {
      const exists = PERIODS.some(pp => pp.key === p.key);
      html += `<div class="det-row"><span>Detected period</span><b>${p.label}</b></div>`;
      html += `<div class="det-row is-new"><span>Status</span><b>${exists ? 'Will replace existing snapshot' : 'New archive entry'}</b></div>`;
    } else {
      html += `<div class="det-row"><span>Detected period</span><b style="color:var(--eax-bad)">Could not parse date from filename</b></div>`;
      html += `<div class="det-row"><span>Tip</span><b>Include YYYY_MM_DD (e.g. <code>2026_05_08</code>) in the filename.</b></div>`;
    }
    if (det) {
      det.innerHTML = html;
      det.classList.add('is-shown');
    }
    // Open the preview modal so the user can review the filename
    // sniff result and confirm with "Add to archive" — the file
    // picker has already happened by this point.
    openUploadModal();
  }

  // ──────────────────────────────────────────────────────────
  // Wiring
  //
  // Defensive: tiny `on(id, evt, fn)` helper that no-ops when the
  // element isn't in the DOM. In empty-state mode (no invoice data
  // yet), most of the dashboard markup isn't rendered — but the
  // Upload modal IS, and the Upload button on the archive rail IS,
  // and the previous unguarded getElementById().addEventListener
  // chain threw on the first missing element (gl-search) which
  // aborted wire() before it ever reached the upload button bind.
  // That's why clicking "Upload bi-weekly file" did nothing.
  // ──────────────────────────────────────────────────────────
  function on(id, evt, fn) {
    const el = document.getElementById(id);
    if (el) el.addEventListener(evt, fn);
  }
  function wire() {
    // Basis pills
    document.querySelectorAll('[data-basis]').forEach(el => {
      el.addEventListener('click', () => setDateBasis(el.dataset.basis));
    });
    // Tab nav
    document.querySelectorAll('.inv-tab').forEach(t => {
      t.addEventListener('click', () => showTab(t.dataset.tab));
    });
    // Table sort headers
    document.querySelectorAll('th[data-sort]').forEach(th => {
      th.addEventListener('click', () => {
        const [tbodyId, col] = th.dataset.sort.split(':');
        sortTbl(tbodyId, Number(col));
      });
    });
    // Filters — guarded individually so the empty-state page (which
    // omits the filter inputs) doesn't break the rest of the wiring.
    on('gl-search',     'input',  filterGlTable);
    on('gl-gl-filter',  'change', filterGlTable);
    on('gl-ent-filter', 'change', filterGlTable);
    ['ember','ccdl'].forEach(w => {
      on(w + '-search',        'input',  () => filterEntityTbl(w));
      on(w + '-gl-filter',     'change', () => filterEntityTbl(w));
      on(w + '-status-filter', 'change', () => filterEntityTbl(w));
    });
    on('all-search', 'input', filterAllTbl);
    ['all-ent-filter','all-month-filter','all-gl-filter','all-status-filter'].forEach(id => {
      on(id, 'change', filterAllTbl);
    });

    // Upload flow — the rail's "Upload bi-weekly file" trigger is now
    // a <label for="upload-file">, so the browser handles opening the
    // OS file picker natively (no JS required). After file selection,
    // the file input's change handler (wired further down) fires
    // handleFileSelection() which populates + opens the preview modal.
    // #open-upload is a topbar trigger placeholder — bind via JS if/
    // when it ever exists.
    const uploadInput = document.getElementById('upload-file');
    document.querySelectorAll('#open-upload').forEach(b => {
      b.addEventListener('click', () => {
        if (uploadInput) uploadInput.click();
      });
    });
    on('upload-close',  'click', closeUploadModal);
    on('upload-cancel', 'click', closeUploadModal);
    on('upload-modal',  'click', (e) => {
      if (e.target.id === 'upload-modal') closeUploadModal();
    });
    const drop = document.getElementById('upload-drop');
    const fileInput = document.getElementById('upload-file');
    if (!drop || !fileInput) return;        // modal markup absent — bail
    drop.addEventListener('click', () => fileInput.click());
    drop.addEventListener('dragover', e => { e.preventDefault(); drop.classList.add('dragover'); });
    drop.addEventListener('dragleave', () => drop.classList.remove('dragover'));
    drop.addEventListener('drop', e => {
      e.preventDefault(); drop.classList.remove('dragover');
      if (e.dataTransfer.files && e.dataTransfer.files[0]) handleFileSelection(e.dataTransfer.files[0]);
    });
    fileInput.addEventListener('change', e => {
      if (e.target.files && e.target.files[0]) handleFileSelection(e.target.files[0]);
    });
  }

  // ──────────────────────────────────────────────────────────
  // INIT
  //
  // wire() and renderPeriodArchive() are always safe to run — they
  // either bind handlers on the always-present rail/modal markup, or
  // no-op against missing elements. The data-render functions below
  // require D.records / D.ytd / etc., so they get skipped in the
  // empty-state where INVOICE_D is `{}` (no uploads yet).
  // ──────────────────────────────────────────────────────────
  const _HAS_DATA = !!(D && Array.isArray(D.records) && D.records.length);
  document.addEventListener('DOMContentLoaded', () => {
    wire();
    renderPeriodArchive();
    if (!_HAS_DATA) return;
    populateDropdowns();
    rebuildMonthFilter();
    buildTables();
    buildGlCards();
    buildVendorBars('emberVendorBars','Ember Group LLC');
    buildVendorBars('ccdlVendorBars', 'CCDL Ventures LLC');
    updateKPIs();
    buildCharts();
    buildMonthlyTable();
    syncCpBanner();
  });
})();
