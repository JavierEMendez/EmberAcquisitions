/* Analyses index — every project and quick analysis in one list.
 *
 * Static file, so Jinja never parses it.
 */
(function () {
  const esc = (s) => String(s == null ? '' : s).replace(/[&<>"']/g,
    (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

  const fmtAcres = (n) => {
    const v = +n || 0;
    return v ? v.toLocaleString('en-US', { maximumFractionDigits: 1 }) + ' ac' : '—';
  };

  // Timestamps are stored as UTC isoformat with no zone marker, so tell the
  // parser it is UTC rather than letting it guess local.
  const fmtWhen = (iso) => {
    if (!iso) return '';
    const d = new Date(/[zZ]|[+-]\d\d:?\d\d$/.test(iso) ? iso : iso + 'Z');
    if (isNaN(d)) return '';
    const days = Math.floor((Date.now() - d.getTime()) / 86400000);
    if (days === 0) return 'today';
    if (days === 1) return 'yesterday';
    if (days < 30) return days + ' days ago';
    return d.toLocaleDateString(undefined, { month: 'short', day: 'numeric', year: 'numeric' });
  };

  const totalAcres = (p) => (p.total_acres != null ? +p.total_acres
    : (p.tracts || []).reduce((a, t) => a + (+t.acres || 0), 0));

  // Searching the same land twice is one finding, not two. Rows collapse on
  // the tracts they hold and their total acreage -- deliberately NOT on the
  // name, because the entries that prompted this were one holding found under
  // three spellings of the same owner ("rancho la laguna", "RANCHO LA LAGUNA
  // LLC", "La Laguna"), all 5 tracts and 1,063.2 ac. Newest wins, and the row
  // says how many runs it stands for so nothing looks silently dropped.
  function collapse(list) {
    const seen = new Map();
    for (const p of list) {                 // already sorted newest-first
      const ids = (p.tracts || []).map(t => String(t.prop_id || ''))
                    .filter(Boolean).sort();
      const key = ids.join('|') + '@' + Math.round(totalAcres(p) * 10);
      const hit = seen.get(key);
      if (hit) hit.runs += 1;
      else seen.set(key, { item: p, runs: 1 });
    }
    return [...seen.values()];
  }

  function row(entry) {
    const p = entry.item, runs = entry.runs;
    const tracts = (p.tracts || []).length;
    const when = fmtWhen(p.updated_at || p.created_at);
    const bits = [tracts + (tracts === 1 ? ' tract' : ' tracts')];
    if (when) bits.push(when);
    if (runs > 1) bits.push(`run ${runs}×`);
    return `<a class="an-row" href="/acquisitions/project/${encodeURIComponent(p.id)}">
      <span class="an-name">${esc(p.name || 'Untitled')}</span>
      <span class="an-meta">${esc(bits.join(' · '))}</span>
      <span class="an-acres">${fmtAcres(totalAcres(p))}</span>
    </a>`;
  }

  function fill(el, entries, emptyMsg) {
    el.innerHTML = entries.length
      ? entries.map(row).join('')
      : `<div class="an-empty">${esc(emptyMsg)}</div>`;
  }

  async function load() {
    const pl = document.getElementById('proj-list');
    const hl = document.getElementById('hist-list');
    try {
      const r = await fetch('/api/acq/projects');
      const d = await r.json();
      if (!r.ok || d.error) throw new Error(d.error || `HTTP ${r.status}`);

      const byNewest = (a, b) =>
        String(b.updated_at || b.created_at || '').localeCompare(
          String(a.updated_at || a.created_at || ''));
      const projects = collapse((d.projects || []).slice().sort(byNewest));
      const history = collapse((d.history || []).slice().sort(byNewest));

      // Counts follow what is shown, not how many rows the table holds --
      // a count of 3 above a single row reads as a rendering bug.
      document.getElementById('proj-count').textContent =
        projects.length ? projects.length : '';
      document.getElementById('hist-count').textContent =
        history.length ? history.length : '';

      fill(pl, projects,
           'No projects yet. Select tracts on the map, then Create Project.');
      fill(hl, history,
           'No searches yet. Acq Analysis on a tract, or Analyse all on an '
           + 'owner, lands here.');
    } catch (e) {
      const msg = `<div class="an-empty">Could not load: ${esc(e.message)}</div>`;
      pl.innerHTML = msg;
      hl.innerHTML = msg;
    }
  }

  load();
})();
