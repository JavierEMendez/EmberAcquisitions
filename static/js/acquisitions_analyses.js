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

  function row(p) {
    const tracts = (p.tracts || []).length;
    const acres = p.total_acres != null ? p.total_acres
      : (p.tracts || []).reduce((a, t) => a + (+t.acres || 0), 0);
    const when = fmtWhen(p.updated_at || p.created_at);
    const bits = [tracts + (tracts === 1 ? ' tract' : ' tracts')];
    if (when) bits.push(when);
    return `<a class="an-row" href="/acquisitions/project/${encodeURIComponent(p.id)}">
      <span class="an-name">${esc(p.name || 'Untitled')}</span>
      <span class="an-meta">${esc(bits.join(' · '))}</span>
      <span class="an-acres">${fmtAcres(acres)}</span>
    </a>`;
  }

  function fill(el, items, emptyMsg) {
    el.innerHTML = items.length
      ? items.map(row).join('')
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
      const projects = (d.projects || []).slice().sort(byNewest);
      const history = (d.history || []).slice().sort(byNewest);

      document.getElementById('proj-count').textContent =
        projects.length ? projects.length : '';
      document.getElementById('hist-count').textContent =
        history.length ? history.length : '';

      fill(pl, projects,
           'No projects yet. Select tracts on the map, then Create Project.');
      fill(hl, history,
           'No quick analyses yet. Acq Analysis on a tract popup lands here.');
    } catch (e) {
      const msg = `<div class="an-empty">Could not load: ${esc(e.message)}</div>`;
      pl.innerHTML = msg;
      hl.innerHTML = msg;
    }
  }

  load();
})();
