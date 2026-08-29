/* ==========================================================================
   Acquisitions GIS — project analysis page
   Gross acreage through the constraints and the infrastructure deduction to
   net saleable acres, then a lot count off that.
   ========================================================================== */
(function () {
  'use strict';

  const $ = (id) => document.getElementById(id);
  const PID = window.ACQ_PROJECT_ID;

  function escapeHtml(s) {
    return String(s ?? '').replace(/[&<>"']/g,
      c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  }
  function fmtNum(n, dp) {
    if (n === null || n === undefined || isNaN(n)) return '—';
    return Number(n).toLocaleString('en-US', {
      minimumFractionDigits: dp || 0, maximumFractionDigits: dp || 0,
    });
  }

  let statusTimer = null;
  function setStatus(msg, isError) {
    const el = $('acq-status');
    if (!el) return;
    if (!msg) { el.classList.remove('show'); return; }
    el.innerHTML = msg;
    el.classList.toggle('error', !!isError);
    el.classList.add('show');
    clearTimeout(statusTimer);
    if (!isError) statusTimer = setTimeout(() => el.classList.remove('show'), 5000);
  }

  function readAssumptions() {
    const g = (id, dflt) => { const el = $(id); return el ? el.checked : dflt; };
    let pct = parseFloat($('infra-pct').value);
    if (!isFinite(pct)) pct = 30;
    return {
      flood:        g('no-flood', true),
      wetlands:     g('no-wetlands', true),
      transmission: g('no-transmission', true),
      streams:      g('no-streams', true),
      pipelines:    g('no-pipelines', true),
      infrastructure_pct: Math.max(0, Math.min(pct, 90)),
    };
  }

  function applyAssumptions(na) {
    if (!na) return;
    [['no-flood', 'flood'], ['no-wetlands', 'wetlands'],
     ['no-transmission', 'transmission'], ['no-streams', 'streams'],
     ['no-pipelines', 'pipelines']].forEach(([id, key]) => {
      const el = $(id);
      if (el && na[key] !== undefined) el.checked = na[key] !== false;
    });
    if (na.infrastructure_pct !== undefined) $('infra-pct').value = na.infrastructure_pct;
  }

  // ── Rendering ───────────────────────────────────────────────────────────
  function renderLadder(a) {
    if (!a) {
      $('acqp-ladder').innerHTML =
        '<div class="acq-empty">Run the analysis to build the ladder.</div>';
      return;
    }
    const rows = (a.netout_detail || []).map(n => {
      // A constraint you deliberately kept and a layer that failed to load
      // both read as "not deducted" and mean opposite things — one of them
      // silently overstates the site. Say which is which.
      const note = n.error
        ? `<span class="note err" title="${escapeHtml(n.reason || 'the service did not respond')}">layer unavailable — not deducted</span>`
        : (n.applied ? '' : '<span class="note">kept by override</span>');
      return `<div class="acqp-rung ${n.applied ? '' : 'kept'}">
          <span>${escapeHtml(n.label)} ${note}</span>
          <span class="num">${n.applied ? '−' : ''}${n.error ? '?' : fmtNum(n.acres, 1)} ac</span>
        </div>`;
    }).join('');

    const density = parseFloat($('acqp-density').value) || 4.5;
    const lots = Math.round((a.net_saleable_acres || 0) * density);

    $('acqp-ladder').innerHTML = `
      <div class="acqp-rung head"><span>Gross acreage</span>
        <span class="num">${fmtNum(a.gross_acres, 1)} ac</span></div>
      ${rows}
      <div class="acqp-rung sub"><span>Net developable</span>
        <span class="num">${fmtNum(a.net_developable_acres, 1)} ac
        <span class="note">${fmtNum(a.net_developable_pct, 1)}% of gross</span></span></div>
      <div class="acqp-rung">
        <span>Infrastructure &amp; landscaping (${fmtNum(a.infrastructure_pct, 0)}%)
          <span class="note">roads, plans, detention, landscaping</span></span>
        <span class="num">−${fmtNum(a.infrastructure_acres, 1)} ac</span></div>
      <div class="acqp-rung total"><span>Net saleable</span>
        <span class="num">${fmtNum(a.net_saleable_acres, 1)} ac
        <span class="note">${fmtNum(a.net_saleable_pct, 1)}% of gross</span></span></div>

      <div class="acqp-kpis">
        <div class="acqp-kpi"><div class="k">Tracts</div>
          <div class="v">${fmtNum(a.tract_count)}</div></div>
        <div class="acqp-kpi"><div class="k">Net developable</div>
          <div class="v">${fmtNum(a.net_developable_acres, 0)}</div></div>
        <div class="acqp-kpi accent"><div class="k">Net saleable</div>
          <div class="v">${fmtNum(a.net_saleable_acres, 0)}</div></div>
        <div class="acqp-kpi accent"><div class="k">Lots @ ${density}/ac</div>
          <div class="v">${fmtNum(lots)}</div></div>
      </div>

      <div style="font-size:11.5px;color:var(--eax-subtle);margin-top:10px">
        Constraints are subtracted as a union, so where they overlap the acreage
        is counted once. Switching one off returns less than its own footprint
        for the same reason.
        ${a.computed_at ? ' Computed ' + escapeHtml(String(a.computed_at).slice(0, 19).replace('T', ' ')) + '.' : ''}
      </div>`;
  }

  function renderTracts(proj) {
    const tracts = proj.tracts || [];
    $('acqp-tracts').innerHTML = tracts.length ? tracts.map((t, i) => `
      <div class="acqp-rung">
        <span>${i + 1}. ${escapeHtml(t.owner_name || '(unknown owner)')}
          <span class="note">${escapeHtml(t.prop_id || '')}${t.county ? ' · ' + escapeHtml(t.county) : ''}</span>
        </span>
        <span class="num">${fmtNum(t.acres, 1)} ac</span>
      </div>`).join('')
      + `<div class="acqp-rung sub"><span>Total</span>
         <span class="num">${fmtNum(proj.total_acres, 1)} ac</span></div>`
      : '<div class="acq-empty">This project has no tracts yet.</div>';
  }

  // ── Load + run ──────────────────────────────────────────────────────────
  let project = null;
  let analysis = null;   // latest run, which is not project.analysis_cache after run()

  async function load() {
    try {
      const r = await fetch(`/api/acq/projects/${encodeURIComponent(PID)}`,
                            { credentials: 'same-origin' });
      const d = await r.json();
      if (!r.ok || d.error) throw new Error(d.error || ('HTTP ' + r.status));
      project = d.project;
      $('acqp-name').textContent = project.name || 'Untitled project';
      $('acqp-meta').textContent =
        `${fmtNum((project.tracts || []).length)} tract`
        + `${(project.tracts || []).length === 1 ? '' : 's'} · `
        + `${fmtNum(project.total_acres, 1)} gross acres`;
      applyAssumptions(project.netout_assumptions);
      renderTracts(project);
      analysis = project.analysis_cache;
      renderLadder(analysis);
    } catch (e) {
      $('acqp-name').textContent = 'Could not load this project';
      $('acqp-meta').textContent = e.message;
    }
  }

  async function run() {
    const btn = $('acqp-run');
    btn.disabled = true;
    btn.textContent = 'Running…';
    setStatus('Querying FEMA, USFWS, USGS and the RRC — this can take up to a minute.');
    try {
      // Persist the assumptions first so the run uses them and a reload
      // repeats the same basis.
      await fetch(`/api/acq/projects/${encodeURIComponent(PID)}`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        credentials: 'same-origin',
        body: JSON.stringify({ netout_assumptions: readAssumptions() }),
      });
      const r = await fetch(`/api/acq/projects/${encodeURIComponent(PID)}/analyze`,
                            { method: 'POST', credentials: 'same-origin' });
      const d = await r.json();
      if (!r.ok || d.error) throw new Error(d.error || ('HTTP ' + r.status));
      analysis = d.analysis;
      renderLadder(analysis);

      const failed = (d.analysis.netout_detail || []).filter(n => n.error);
      if (failed.length) {
        setStatus(`Analysis complete, but ${failed.length} layer`
          + `${failed.length === 1 ? '' : 's'} did not load `
          + `(${failed.map(f => escapeHtml(f.label)).join(', ')}). `
          + 'Those acres are NOT deducted, so net developable is overstated.', true);
      } else {
        setStatus('Analysis complete.');
      }
    } catch (e) {
      setStatus('Analysis failed: ' + escapeHtml(e.message), true);
    } finally {
      btn.disabled = false;
      btn.textContent = 'Run analysis';
    }
  }

  $('acqp-run').addEventListener('click', run);
  // Density is a display-side multiplier on saleable acres, so it re-renders
  // without needing another round trip to the GIS services.
  $('acqp-density').addEventListener('input', () => {
    if (analysis) renderLadder(analysis);
  });

  load();
})();
