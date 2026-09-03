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

  // ──────────────────────────────────────────────────────────
  // Expose modal handlers on window FIRST — before any other
  // module-level code that could throw (Chart.defaults, INVOICE_D
  // access, etc.). If anything below this point fails, the modal's
  // Cancel and Add-to-archive inline onclick handlers still work.
  // ──────────────────────────────────────────────────────────
  window.invdCloseUploadModal = function() {
    var m = document.getElementById('upload-modal');
    if (m) m.classList.remove('is-open');
  };

  // File-input change handler. Wired via inline `onchange` on the
  // <input autocomplete="off" data-lpignore="true" data-1p-ignore id="upload-file"> so it runs whether or not wire() ever
  // executes. Builds the same preview block handleFileSelection
  // builds later in the file, but standalone so this top section
  // has no forward dependency on anything below.
  window.invdHandleFile = function(input) {
    var file = input && input.files && input.files[0];
    if (!file) return;
    var det  = document.getElementById('upload-detected');
    var modal = document.getElementById('upload-modal');
    var fname = file.name;
    // Detect period by sniffing YYYY-MM-DD / YYYY_MM_DD in filename.
    var m1 = fname.match(/(\d{4})[-_]?(\d{2})[-_]?(\d{2})/);
    var html = '<div class="det-row"><span>File</span><b>' + fname + '</b></div>';
    if (m1) {
      var y  = Number(m1[1]), mo = Number(m1[2]), d = Number(m1[3]);
      var firstHalf = d <= 15;
      var pad = function (n) { return String(n).padStart(2, '0'); };
      var endD = firstHalf ? 15 : new Date(y, mo, 0).getDate();
      var months = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
      var startD = firstHalf ? 1 : 16;
      var label = months[mo - 1] + ' ' + startD + ' – ' + months[mo - 1] + ' ' + endD + ', ' + y;
      html += '<div class="det-row"><span>Detected period</span><b>' + label + '</b></div>';
      html += '<div class="det-row is-new"><span>Status</span><b>New archive entry</b></div>';
    } else {
      html += '<div class="det-row"><span>Detected period</span><b style="color:var(--eax-bad)">Could not parse date from filename</b></div>';
    }
    if (det) {
      det.innerHTML = html;
      det.classList.add('is-shown');
    }
    if (modal) modal.classList.add('is-open');
  };

  window.invdCommitUpload = async function() {
    var input = document.getElementById('upload-file');
    var file  = input && input.files && input.files[0];
    if (!file) { alert('Pick a file first.'); return; }
    var btn = document.getElementById('upload-commit');
    var original = btn ? btn.textContent : 'Add to archive';
    if (btn) { btn.disabled = true; btn.textContent = 'Uploading…'; }
    try {
      var fd = new FormData();
      fd.append('file', file, file.name);
      var r = await fetch('/api/invoice-dashboard/upload', { method: 'POST', body: fd });
      var j = await r.json().catch(function () { return {}; });
      if (!r.ok || !j.ok) {
        alert('Upload failed: ' + (j.error || ('HTTP ' + r.status)));
        if (btn) { btn.disabled = false; btn.textContent = original; }
        return;
      }
      location.assign('/invoice-dashboard?period=' + encodeURIComponent(j.period_key));
    } catch (err) {
      alert('Upload failed: ' + (err && err.message || err));
      if (btn) { btn.disabled = false; btn.textContent = original; }
    }
  };

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
    // Robust against non-ISO inputs: pull the first YYYY-MM-DD we can
    // find anywhere in the string. Flask used to serialize dates as
    // RFC-822 ("Thu, 16 Apr 2026 00:00:00 GMT") which made the naive
    // split('-') return NaN — that crashed buildPeriodArchive() and
    // took the whole IIFE down with it. Server now sends ISO, but
    // belt-and-suspenders here so a future regression doesn't blank
    // the entire dashboard.
    var iso = (dateStr || '').match(/(\d{4})-(\d{2})-(\d{2})/);
    if (!iso) {
      // Try to extract a parseable date by going through Date()
      var dParsed = new Date(dateStr);
      if (!isNaN(dParsed.getTime())) {
        var yy = dParsed.getFullYear();
        var mm = dParsed.getMonth() + 1;
        var dd = dParsed.getDate();
        iso = [null, String(yy), String(mm).padStart(2, '0'), String(dd).padStart(2, '0')];
      }
    }
    if (!iso) {
      // Final fallback — produce a placeholder that won't crash.
      var now = new Date();
      iso = [null, String(now.getFullYear()), String(now.getMonth() + 1).padStart(2, '0'), String(now.getDate()).padStart(2, '0')];
    }
    const y = Number(iso[1]);
    const m = Number(iso[2]);
    const d = Number(iso[3]);
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
    // Source-of-truth: window.INVOICE_ARCHIVE — one entry per row in
    // the server's `invoice_periods` table (one row per upload). The
    // earlier version bucketed D.records by processing_date, which
    // surfaced phantom rail cards for every historical month inside
    // a Stampli export's YTD record set even when only one snapshot
    // was actually uploaded. The rail represents UPLOADS, not slices
    // of an upload's record history.
    const archive = Array.isArray(window.INVOICE_ARCHIVE) ? window.INVOICE_ARCHIVE : [];
    if (!archive.length) return [];
    return archive.map(a => {
      // a = { period_key, period_start, period_end, total_records,
      //       total_amount, ember_count, ccdl_count, … }
      // Map server fields to the in-page shape the rail renderer
      // expects (key / short / year / count / total / ember / ccdl /
      // end / is_current).
      const p = periodOf(a.period_start);
      return {
        key:   a.period_key || p.key,
        start: a.period_start,
        end:   a.period_end || p.end,
        label: p.label, short: p.short, year: p.year,
        count: a.total_records || 0,
        total: a.total_amount || 0,
        ember: a.ember_count || 0,
        ccdl:  a.ccdl_count || 0,
        is_current: (a.period_key === window.INVOICE_PERIOD_KEY),
      };
    }).sort((a, b) => b.start.localeCompare(a.start));
  }
  // Module-level call — wrapped because if it ever throws, the IIFE
  // crashes before DOMContentLoaded is registered and the entire
  // dashboard goes blank. Treat archive-build failure as "no archive."
  let PERIODS = [];
  try { PERIODS = buildPeriodArchive(); }
  catch (e) { console.error('buildPeriodArchive failed', e); PERIODS = []; }
  // Which period is the user currently viewing in the "This Period" tab?
  // Server tells us via INVOICE_PERIOD_KEY; fall back to the newest
  // archive entry or null if there are no uploads yet (empty-state).
  let activePeriodKey = window.INVOICE_PERIOD_KEY
                     || (PERIODS[0] && PERIODS[0].key)
                     || null;

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
    // "Active period" = the records in the currently-loaded snapshot
    // (D) that fall inside the upload's bi-weekly processing window —
    // flagged is_cp_proc by the parser. Previously this read from
    // per-card record buckets, but those no longer exist now that the
    // rail is built from server-side INVOICE_ARCHIVE rather than
    // record-level bucketing of D.
    let e = {count:0,total:0}, c = {count:0,total:0};
    if (!D || !Array.isArray(D.records)) {
      return { ember: e, ccdl: c, combined: { count: 0, total: 0 } };
    }
    D.records.forEach(r => {
      if (!r.is_cp_proc) return;
      const t = (r.entity || '').includes('Ember') ? e : c;
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
  // Charts — Chart.js wired with theme-aware Ember palette.
  //
  // The "Ember Group" series color used to be hardcoded to #13344E
  // (ember-ink-2), which is dark navy. That sits invisibly on the
  // navy-mode dashboard background — the bars literally disappear.
  // We now read the active theme from <html data-theme="..."> and
  // resolve a palette that stays legible on both backgrounds.
  // Charts re-render when the user toggles theme via a
  // MutationObserver on documentElement[data-theme].
  // ──────────────────────────────────────────────────────────
  function isDarkTheme() {
    return document.documentElement.getAttribute('data-theme') === 'dark';
  }
  // Returns the palette for the current theme. Re-resolved on every
  // chart build so theme switches take effect immediately.
  function palette() {
    const dark = isDarkTheme();
    return {
      EMBER:  dark ? '#8FB6D9' : '#13344E',   // legible on navy / dark on paper
      CCDL:   dark ? '#6BC793' : '#1F7A4D',   // matches --eax-good per theme
      PAID:   dark ? '#6BC793' : '#1F7A4D',
      UNPAID: dark ? '#F57346' : '#F25929',   // matches --eax-accent per theme
      HILITE: dark ? '#F57346' : '#F25929',
      MUTED:  dark ? 'rgba(255,255,255,0.30)' : 'rgba(19,52,78,0.45)',
      // Chart text + axis colors. The previous fixed #5B6B7B was OK on
      // paper but rendered as low-contrast murky grey on navy — and
      // the donut/pie legends in particular looked black-ish because
      // they had no explicit color override and inherited it.
      TEXT:   dark ? '#E8EDF2' : '#5B6B7B',
      GRID:   dark ? 'rgba(255,255,255,0.08)' : 'rgba(8,35,59,0.06)',
      TOOLTIP_BG:    dark ? '#08233B' : '#0B1F33',
      TOOLTIP_TEXT:  '#F4ECDD',
    };
  }

  function gridOpts() {
    const p = palette();
    return {
      ticks: { color: p.TEXT, font: { size: 10, family: 'DM Sans, sans-serif' } },
      grid:  { color: p.GRID },
    };
  }
  // Apply Chart.defaults from the current theme. Called on init AND
  // on every theme toggle so legend / tick / tooltip colors track.
  function applyChartDefaults() {
    try {
      const p = palette();
      Chart.defaults.font.family = 'DM Sans, "Plus Jakarta Sans", sans-serif';
      Chart.defaults.color = p.TEXT;
      Chart.defaults.plugins.tooltip.backgroundColor = p.TOOLTIP_BG;
      Chart.defaults.plugins.tooltip.titleColor      = p.TOOLTIP_TEXT;
      Chart.defaults.plugins.tooltip.bodyColor       = p.TOOLTIP_TEXT;
      Chart.defaults.plugins.tooltip.padding         = 10;
      Chart.defaults.plugins.tooltip.cornerRadius    = 6;
    } catch (e) { /* Chart.js unavailable — non-fatal */ }
  }
  applyChartDefaults();
  // Re-render charts when the user flips the theme toggle. We listen
  // on documentElement[data-theme] because that's where the central
  // sidebar toggle writes ('dark' / 'light').
  try {
    new MutationObserver(function () {
      applyChartDefaults();
      refreshPaletteVars();
      // Only rebuild if data is loaded; otherwise nothing to repaint.
      if (D && Array.isArray(D.records) && D.records.length) {
        try { buildCharts();  } catch (_) {}
        try { buildVendorBars('emberVendorBars','Ember Group LLC'); } catch (_) {}
        try { buildVendorBars('ccdlVendorBars', 'CCDL Ventures LLC'); } catch (_) {}
      }
    }).observe(document.documentElement, { attributes: true, attributeFilter: ['data-theme'] });
  } catch (e) { /* no-op */ }

  // The rest of the file references EMBER, CCDL, PAID, UNPAID, HILITE,
  // MUTED as if they were module-level constants — keep that shape, but
  // back them with mutable `let`s that re-resolve on theme change so
  // every chart rebuild picks up the current palette without each call
  // site having to change.
  let EMBER, CCDL, PAID, UNPAID, HILITE, MUTED;
  function refreshPaletteVars() {
    const p = palette();
    EMBER  = p.EMBER;
    CCDL   = p.CCDL;
    PAID   = p.PAID;
    UNPAID = p.UNPAID;
    HILITE = p.HILITE;
    MUTED  = p.MUTED;
  }
  refreshPaletteVars();

  const charts = {};
  function buildOrUpdate(id, config) {
    if (charts[id]) charts[id].destroy();
    const el = document.getElementById(id);
    if (!el) return;
    charts[id] = new Chart(el, config);
  }

  function buildCharts() {
    const p = palette();
    const gls = D.gl_accounts.slice(0, 10);
    // Surface color forms the white-in-light / navy-in-dark slice
    // separator between donut/pie segments. Hardcoded #FFFFFF kept
    // working in dark mode (white lines on colored slices) but made
    // the donut feel slightly more disconnected than necessary —
    // match the surface so it reads as actual gaps either way.
    const SLICE_BORDER = p.TEXT === '#5B6B7B' ? '#FFFFFF' : '#0E2A41';
    buildOrUpdate('glDonut', {
      type: 'doughnut',
      data: {
        labels: gls.map(g => g.gl),
        datasets: [{
          data: gls.map(g => g.total),
          backgroundColor: gls.map(g => GL_COLORS[g.gl] || EMBER),
          borderWidth: 2, borderColor: SLICE_BORDER,
        }],
      },
      options: {
        responsive: true, maintainAspectRatio: false, cutout: '58%',
        plugins: {
          legend: {
            position: 'right',
            labels: {
              color: p.TEXT,
              font: { size: 10 }, boxWidth: 10,
              generateLabels: ch => ch.data.labels.map((l, i) => ({
                text: l + ' · ' + fmtK(ch.data.datasets[0].data[i]),
                fillStyle:   ch.data.datasets[0].backgroundColor[i],
                strokeStyle: ch.data.datasets[0].backgroundColor[i],
                fontColor:   p.TEXT,
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
          borderWidth: 2, borderColor: SLICE_BORDER,
        }],
      },
      options: {
        responsive: true, maintainAspectRatio: false,
        plugins: {
          legend: { position: 'right', labels: { color: p.TEXT, font: { size: 10 }, boxWidth: 10 } },
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
        { label: 'Ember Group',  data: eV, borderColor: EMBER, backgroundColor: EMBER + '1A', tension: .3, fill: true, pointRadius: 5, pointBackgroundColor: EMBER },
        { label: 'CCDL Ventures',data: cV, borderColor: CCDL,  backgroundColor: CCDL  + '1A', tension: .3, fill: true, pointRadius: 5, pointBackgroundColor: CCDL },
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
        borderColor: EMBER, backgroundColor: EMBER + '1F',
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
    // If the user clicks a different rail card we need a different
    // snapshot from the server — D only holds the currently-loaded
    // period's records. Navigate via ?period= so the server-rendered
    // first paint matches the chosen period (and the SSR path reuses
    // the same template). Clicking the already-active card is a
    // no-op.
    if (!key || key === activePeriodKey) return;
    location.assign('/invoice-dashboard?period=' + encodeURIComponent(key));
  }

  // ──────────────────────────────────────────────────────────
  // Upload modal — sniff file name for ISO-date or "YYYY_MM_DD"
  // pattern and report the detected period.
  // ──────────────────────────────────────────────────────────
  function openUploadModal() {
    // Just opens the modal — does NOT reset the preview, because
    // handleFileSelection() may have just populated it. Resetting
    // here would race-erase the just-shown preview rows. The button
    // itself uses an inline onclick to add `is-open` without going
    // through this function, so the empty-state open path is also
    // covered without dropping the preview.
    const m = document.getElementById('upload-modal');
    if (m) m.classList.add('is-open');
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
    // Add-to-archive — wired here AND exposed on window so the
    // template's inline onclick="window.invdCommitUpload()" path also
    // works (defense in depth: if wire() ever fails to run, the
    // inline onclick still does the right thing).
    on('upload-commit', 'click', () => window.invdCommitUpload && window.invdCommitUpload());
    const drop = document.getElementById('upload-drop');
    const fileInput = document.getElementById('upload-file');
    if (!drop || !fileInput) return;        // modal markup absent — bail
    // Click-to-browse is handled natively by the <label for="upload-file">
    // — no JS needed for that path. Drag/drop still wired below.
    drop.addEventListener('dragover', e => { e.preventDefault(); drop.classList.add('dragover'); });
    drop.addEventListener('dragleave', () => drop.classList.remove('dragover'));
    drop.addEventListener('drop', e => {
      e.preventDefault(); drop.classList.remove('dragover');
      if (e.dataTransfer.files && e.dataTransfer.files[0]) handleFileSelection(e.dataTransfer.files[0]);
    });
    fileInput.addEventListener('change', e => {
      if (e.target.files && e.target.files[0]) handleFileSelection(e.target.files[0]);
    });

    // ── Topbar Excel / PDF exports ─────────────────────────────
    // Both buttons were stubbed as <a href="#"> with no handlers since
    // launch (commit 08c171d9). Excel is built from window.INVOICE_D
    // via SheetJS; PDF is window.print() driven by the @media print
    // stylesheet in invoice_dashboard.html.
    on('inv-export-excel', 'click', exportInvoicesExcel);
    on('inv-export-pdf',   'click', exportInvoicesPdf);
  }

  // ──────────────────────────────────────────────────────────
  // EXPORT — Excel (multi-sheet) + PDF (browser print)
  // ──────────────────────────────────────────────────────────
  function _fmtPeriodFilename() {
    // Build a stable "YYYY-MM-DD" stamp from window.INVOICE_D.report_date.
    // Falls back to today's date string if the field is missing.
    const raw = (D && D.report_date) || '';
    // raw is like "May 8, 2026" — Date.parse handles it cross-browser.
    const dt = raw ? new Date(raw) : new Date();
    if (!isFinite(+dt)) return 'invoices';
    const y = dt.getFullYear();
    const m = String(dt.getMonth() + 1).padStart(2, '0');
    const d = String(dt.getDate()).padStart(2, '0');
    return y + '-' + m + '-' + d;
  }

  function exportInvoicesExcel() {
    if (typeof XLSX === 'undefined') {
      alert('Excel library failed to load. Reload the page and try again.');
      return;
    }
    if (!D || !Array.isArray(D.records) || !D.records.length) {
      alert('No invoice data yet — upload a Stampli export first.');
      return;
    }
    const wb = XLSX.utils.book_new();
    const addSheet = (name, rows) => {
      if (!Array.isArray(rows) || rows.length < 2) return;
      // Sheet names cap at 31 chars in xlsx; trim defensively.
      const safe = String(name).slice(0, 31);
      XLSX.utils.book_append_sheet(wb, XLSX.utils.aoa_to_sheet(rows), safe);
    };

    // 1. Invoices — every record, flattened.
    const invHeader = [
      'Entity', 'Vendor', 'Invoice No.', 'Status', 'Paid', 'GL Account',
      'Description', 'Invoice Date', 'Processing Date', 'Due Date', 'Payment Date',
      'Amount ($)', 'Days to Pay', 'Lag (proc - inv days)',
    ];
    const invRows = [invHeader];
    D.records.forEach(r => {
      invRows.push([
        r.entity || '', r.vendor || '', r.invoice_no || '', r.status || '',
        r.is_paid ? 'Yes' : 'No', r.gl || '', r.description || '',
        r.invoice_date || '', r.processing_date || '',
        r.due_date || '', r.payment_date || '',
        Number(r.amount) || 0,
        (r.days_to_pay == null ? '' : Number(r.days_to_pay)),
        (r.lag_days   == null ? '' : Number(r.lag_days)),
      ]);
    });
    addSheet('Invoices', invRows);

    // 2. GL Summary
    if (Array.isArray(D.gl_accounts) && D.gl_accounts.length) {
      const glRows = [['GL Account', 'Invoice Count', 'Total ($)']];
      D.gl_accounts.forEach(g => glRows.push([g.gl || '', g.count || 0, Number(g.total) || 0]));
      addSheet('GL Summary', glRows);
    }

    // 3. Vendor Summary — Ember + CCDL on one sheet (entity column tags rows).
    const venRows = [['Entity', 'Vendor', 'Total ($)']];
    (D.vendor_ember || []).forEach(v => venRows.push(['Ember Group LLC',  v.vendor, Number(v.total) || 0]));
    (D.vendor_ccdl  || []).forEach(v => venRows.push(['CCDL Ventures LLC', v.vendor, Number(v.total) || 0]));
    addSheet('Vendor Summary', venRows);

    // 4. Monthly Roll-up (Invoice basis)
    if (Array.isArray(D.months_inv) && D.months_inv.length) {
      const mRows = [['Month', 'Ember Count', 'Ember Total ($)', 'Ember Paid ($)', 'Ember Not Paid ($)',
                      'CCDL Count', 'CCDL Total ($)', 'CCDL Paid ($)', 'CCDL Not Paid ($)']];
      D.months_inv.forEach(k => {
        const m = (D.monthly_inv || {})[k] || {};
        const e = m.ember || {}, c = m.ccdl || {};
        mRows.push([m.label || k, e.count || 0, e.total || 0, e.paid || 0, e.not_paid || 0,
                                  c.count || 0, c.total || 0, c.paid || 0, c.not_paid || 0]);
      });
      addSheet('Monthly (Invoice Date)', mRows);
    }

    // 5. Monthly Roll-up (Processing basis)
    if (Array.isArray(D.months_proc) && D.months_proc.length) {
      const mRows = [['Month', 'Ember Count', 'Ember Total ($)', 'Ember Paid ($)', 'Ember Not Paid ($)',
                      'CCDL Count', 'CCDL Total ($)', 'CCDL Paid ($)', 'CCDL Not Paid ($)']];
      D.months_proc.forEach(k => {
        const m = (D.monthly_proc || {})[k] || {};
        const e = m.ember || {}, c = m.ccdl || {};
        mRows.push([m.label || k, e.count || 0, e.total || 0, e.paid || 0, e.not_paid || 0,
                                  c.count || 0, c.total || 0, c.paid || 0, c.not_paid || 0]);
      });
      addSheet('Monthly (Processing)', mRows);
    }

    // 6. YTD Totals
    if (D.ytd) {
      const ytdRows = [['Metric', 'Ember Group', 'CCDL Ventures', 'Combined']];
      const tally = (k) => [D.ytd.ember && D.ytd.ember[k], D.ytd.ccdl && D.ytd.ccdl[k], D.ytd.combined && D.ytd.combined[k]];
      ytdRows.push(['Invoice Count', ...tally('count')]);
      ytdRows.push(['Total ($)',     ...tally('total')]);
      ytdRows.push(['Paid ($)',      ...tally('paid')]);
      ytdRows.push(['Not Paid ($)',  ...tally('not_paid')]);
      ytdRows.push(['Avg Days to Pay', ...tally('avg_days_to_pay')]);
      ytdRows.push(['Avg Lag (days)',  ...tally('avg_lag')]);
      addSheet('YTD Totals', ytdRows);
    }

    const stamp = _fmtPeriodFilename();
    XLSX.writeFile(wb, 'Invoice_Dashboard_' + stamp + '.xlsx');
  }

  function exportInvoicesPdf() {
    // Save current title so the print dialog suggests a sensible filename.
    // Browsers default the save name to document.title.
    const stamp = _fmtPeriodFilename();
    const original = document.title;
    document.title = 'Invoice_Dashboard_' + stamp;
    // Defer one frame so the title update flushes before the print dialog opens.
    requestAnimationFrame(() => {
      try { window.print(); }
      finally { setTimeout(() => { document.title = original; }, 500); }
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
