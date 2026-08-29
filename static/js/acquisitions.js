/* ==========================================================================
   Acquisitions GIS — map page
   Vanilla JS, no build step, matching the rest of the portal.
   All endpoints namespace under /api/acq/ (bare /api/projects belongs to MPC
   Underwriting in this app).
   ========================================================================== */
(function () {
  'use strict';

  const $ = (id) => document.getElementById(id);

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

  function fmtMoney(n) {
    if (n === null || n === undefined || isNaN(n)) return '—';
    const r = Math.round(n), neg = r < 0;
    const s = '$' + Math.abs(r).toLocaleString('en-US');
    return neg ? '(' + s + ')' : s;   // accounting convention, as elsewhere
  }

  // ── Status line ─────────────────────────────────────────────────────────
  let statusTimer = null;
  function setStatus(msg, isError) {
    const el = $('acq-status');
    if (!el) return;
    if (!msg) { el.classList.remove('show'); return; }
    el.innerHTML = msg;
    el.classList.toggle('error', !!isError);
    el.classList.add('show');
    clearTimeout(statusTimer);
    if (!isError) statusTimer = setTimeout(() => el.classList.remove('show'), 4500);
  }

  // ── Map ─────────────────────────────────────────────────────────────────
  // setView BEFORE anything is added. Adding a layer to a map with no view
  // throws inside Leaflet's projection ("layerPointToLatLng of undefined"),
  // and a fitBounds later in the function is too late to prevent it.
  const map = L.map('acq-map', { zoomControl: true, preferCanvas: true })
    .setView([30.05, -95.75], 10);

  // Esri and OSM tiles, all key-free. CARTO's raster tiles now stamp
  // "API KEY REQUIRED" across every tile, so they are not usable here.
  // Imagery is the default: for land screening, what the ground actually looks
  // like beats a road map almost every time.
  const ESRI_ATTR = 'Tiles &copy; Esri';
  const basemaps = {
    'Imagery': L.tileLayer(
      'https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}',
      { maxZoom: 19, attribution: ESRI_ATTR }),
    'Topographic': L.tileLayer(
      'https://server.arcgisonline.com/ArcGIS/rest/services/World_Topo_Map/MapServer/tile/{z}/{y}/{x}',
      { maxZoom: 19, attribution: ESRI_ATTR }),
    'Streets': L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png',
      { maxZoom: 19, attribution: '&copy; OpenStreetMap contributors' }),
  };
  basemaps['Imagery'].addTo(map);

  // Place and boundary labels over the imagery - without them aerial tiles are
  // hard to navigate, since nothing is named.
  const labels = L.tileLayer(
    'https://server.arcgisonline.com/ArcGIS/rest/services/Reference/World_Boundaries_and_Places/MapServer/tile/{z}/{y}/{x}',
    { maxZoom: 19, opacity: 0.9 }).addTo(map);

  L.control.layers(basemaps, { 'Place labels': labels },
                   { position: 'bottomleft', collapsed: true }).addTo(map);

  const overlays = {
    tracts:  L.geoJSON(null, { style: tractStyle, onEachFeature: onTract }).addTo(map),
    radius:  L.geoJSON(null, { style: { color: '#F25929', weight: 2, dashArray: '6 5', fill: false } }).addTo(map),
    focused: L.geoJSON(null, { style: { color: '#00B8D4', weight: 3, fillColor: '#00B8D4', fillOpacity: 0.25 } }).addTo(map),
  };

  // Fill can be switched off so boundaries stay readable over the imagery
  // and over whichever constraint layers are on.
  let fillOn = true;
  function tractStyle() {
    return {
      color: '#F25929',
      weight: 1.6,
      fillColor: '#F25929',
      fillOpacity: fillOn ? 0.18 : 0,
    };
  }

  // ── Ancillary layers ────────────────────────────────────────────────────
  const LAYER_DEFS = [
    { key: 'flood',          label: 'Floodplain (100-yr)',  color: '#2E86C1' },
    { key: 'wetlands',       label: 'Wetlands (NWI)',       color: '#1ABC9C' },
    { key: 'streams',        label: 'Streams',              color: '#5DADE2' },
    { key: 'pipelines',      label: 'Pipelines',            color: '#B7950B' },
    { key: 'transmission',   label: 'Transmission lines',   color: '#8E44AD' },
    { key: 'wells',          label: 'Oil / gas wells',      color: '#616A6B' },
    { key: 'counties',       label: 'Counties',             color: '#34495E' },
    { key: 'etj',            label: 'City ETJ',             color: '#E67E22' },
    { key: 'schools',        label: 'School districts',     color: '#16A085' },
    { key: 'water_dist',     label: 'Water districts',      color: '#2980B9' },
    { key: 'muds',           label: 'MUDs only',            color: '#3498DB' },
    { key: 'ccn',            label: 'Water CCN',            color: '#48C9B0' },
    { key: 'electric',       label: 'Electric utility',     color: '#F1C40F' },
    { key: 'txdot_projects', label: 'TxDOT projects',       color: '#C0392B' },
  ];

  const layerGroups = {};
  const layerLoaded = {};
  const layerInflight = {};

  function buildLayerPanel() {
    const body = $('acq-layers-body');
    body.innerHTML = LAYER_DEFS.map(d => `
      <label class="acq-layer" data-key="${d.key}">
        <input type="checkbox" data-layer="${d.key}">
        <span class="swatch" style="background:${d.color}"></span>
        <span>${escapeHtml(d.label)}</span>
        <span class="count" id="acq-count-${d.key}"></span>
      </label>`).join('');

    // Nothing is enabled on load. Turning on fourteen live GIS services the
    // moment the page opens is slow and mostly not what you came for.
    body.querySelectorAll('input[data-layer]').forEach(cb => {
      cb.addEventListener('change', () => toggleLayer(cb.dataset.layer, cb.checked));
    });
  }

  function toggleLayer(key, on) {
    const def = LAYER_DEFS.find(d => d.key === key);
    if (!def) return;
    if (!layerGroups[key]) {
      layerGroups[key] = L.geoJSON(null, {
        style: { color: def.color, weight: 1.4, fillColor: def.color, fillOpacity: 0.16 },
        pointToLayer: (f, latlng) => L.circleMarker(latlng, {
          radius: 3, color: def.color, fillColor: def.color, fillOpacity: 0.8, weight: 1,
        }),
      });
    }
    const grp = layerGroups[key];
    if (!on) { map.removeLayer(grp); return; }
    grp.addTo(map);
    loadLayer(key);
  }

  async function loadLayer(key) {
    const b = map.getBounds();
    const cur = { w: b.getWest(), s: b.getSouth(), e: b.getEast(), n: b.getNorth() };
    const prev = layerLoaded[key];
    // Skip a refetch when the view has barely moved and we already have data.
    if (prev && Math.abs(prev.w - cur.w) < 0.01 && Math.abs(prev.s - cur.s) < 0.01 &&
        Math.abs(prev.e - cur.e) < 0.01 && Math.abs(prev.n - cur.n) < 0.01 &&
        layerGroups[key].getLayers().length) return;

    if (cur.e - cur.w > 5 || cur.n - cur.s > 5) {
      setStatus('Zoom in to load layers — the current view is too wide.', true);
      return;
    }
    if (layerInflight[key]) { try { layerInflight[key].abort(); } catch (e) {} }
    const ctrl = new AbortController();
    layerInflight[key] = ctrl;

    const countEl = $('acq-count-' + key);
    if (countEl) countEl.textContent = '…';
    try {
      const url = `/api/acq/load-layer/${encodeURIComponent(key)}`
                + `?minx=${cur.w}&miny=${cur.s}&maxx=${cur.e}&maxy=${cur.n}`;
      const r = await fetch(url, { signal: ctrl.signal, credentials: 'same-origin' });
      const d = await r.json();
      if (!r.ok || d.error) throw new Error(d.error || ('HTTP ' + r.status));
      layerGroups[key].clearLayers();
      if (d.fc) layerGroups[key].addData(d.fc);
      layerLoaded[key] = cur;
      if (countEl) countEl.textContent = d.count ? fmtNum(d.count) : '0';
    } catch (e) {
      if (e.name === 'AbortError') return;
      if (countEl) countEl.textContent = '!';
      setStatus(`Layer <b>${escapeHtml(key)}</b> failed: ${escapeHtml(e.message)}`, true);
    } finally {
      if (layerInflight[key] === ctrl) delete layerInflight[key];
    }
  }

  // Refresh whatever is switched on after the view settles.
  let moveTimer = null;
  map.on('moveend', () => {
    clearTimeout(moveTimer);
    moveTimer = setTimeout(() => {
      Object.keys(layerGroups).forEach(k => {
        if (map.hasLayer(layerGroups[k])) loadLayer(k);
      });
    }, 450);
  });

  // Pin keeps the panel from collapsing on small screens.
  let panelPinned = false;
  $('acq-layers-pin').addEventListener('click', () => {
    panelPinned = !panelPinned;
    $('acq-layers-pin').classList.toggle('pinned', panelPinned);
    $('acq-layers-pin').title = panelPinned ? 'Unpin this panel' : 'Keep this panel open';
  });

  // ── Parcel interaction ──────────────────────────────────────────────────
  function tractProps(f) {
    const p = f.properties || {};
    return {
      pid: p.Prop_ID || p.prop_id || '',
      owner: p.OWNER_NAME || p.owner_name || '(unknown owner)',
      acres: p.Acres ?? p.GIS_AREA ?? p.gis_area ?? null,
      county: p._county || p.county_name || '',
      situs: p.SITUS_ADDR || '',
      market: p.MARKET_VAL ?? p.total_market_val ?? null,
    };
  }

  function onTract(f, layer) {
    const t = tractProps(f);
    layer.bindPopup(`
      <div class="acq-popup-owner">${escapeHtml(t.owner)}</div>
      <div class="acq-popup-meta">
        ${fmtNum(t.acres, 1)} ac${t.county ? ' · ' + escapeHtml(t.county) : ''}<br>
        ${escapeHtml(t.situs || 'no street address')}<br>
        ${t.market ? fmtMoney(t.market) + ' market value' : ''}
      </div>
      <div class="acq-popup-actions">
        <button data-acq-owner="${escapeHtml(t.owner)}">Search by owner</button>
        <button data-acq-focus="${escapeHtml(t.pid)}">Focus layers</button>
        <button data-acq-add="${escapeHtml(t.pid)}">Add to project</button>
      </div>`);
  }

  // Popup buttons are created after the fact, so delegate from the container.
  document.addEventListener('click', (ev) => {
    const owner = ev.target.closest?.('[data-acq-owner]');
    if (owner) { ownerSearch(owner.dataset.acqOwner); return; }
    const focus = ev.target.closest?.('[data-acq-focus]');
    if (focus) { focusTract(focus.dataset.acqFocus); return; }
    const add = ev.target.closest?.('[data-acq-add]');
    if (add) { addToProject(add.dataset.acqAdd); return; }
  });

  function focusTract(pid) {
    let target = null;
    overlays.tracts.eachLayer(l => {
      if (String(tractProps(l.feature).pid) === String(pid)) target = l;
    });
    if (!target) return;
    // Fill off on entry — the whole point of focusing is to read the
    // constraint layers underneath this parcel.
    fillOn = false;
    overlays.tracts.setStyle(tractStyle);
    overlays.focused.clearLayers();
    overlays.focused.addData(target.feature);
    map.fitBounds(target.getBounds(), { padding: [40, 40] });
    map.closePopup();
    setStatus('Focused one tract with fill off. Toggle layers to read what sits under it.');
  }

  // ── Owner search ────────────────────────────────────────────────────────
  async function ownerSearch(name) {
    if (!name) return;
    openDock('Owner search', 'Searching cached counties…',
             '<div class="acq-empty">Searching…</div>');
    try {
      const r = await fetch('/api/acq/owner-dossier?name=' + encodeURIComponent(name),
                            { credentials: 'same-origin' });
      const d = await r.json();
      if (!r.ok || d.error) throw new Error(d.error || ('HTTP ' + r.status));
      const parcels = d.parcels || [];
      overlays.focused.clearLayers();
      parcels.forEach(p => {
        if (p.geometry) overlays.focused.addData({ type: 'Feature', geometry: p.geometry, properties: p });
      });
      if (parcels.length) {
        fillOn = false;
        overlays.tracts.setStyle(tractStyle);
        try { map.fitBounds(overlays.focused.getBounds(), { padding: [50, 50] }); } catch (e) {}
      }
      const acres = parcels.reduce((s, p) => s + (Number(p.acres) || 0), 0);
      $('acq-dock-sub').textContent =
        `${fmtNum(d.total_count)} tract${d.total_count === 1 ? '' : 's'} · `
        + `${fmtNum(acres, 1)} ac · ${(d.variants || []).length} name spelling`
        + ((d.variants || []).length === 1 ? '' : 's');
      $('acq-dock-body').innerHTML = parcels.length ? parcels.map(p => `
        <div class="acq-row">
          <div class="acq-row-name">${escapeHtml(p.owner_name || '')}</div>
          <div class="acq-row-meta">${fmtNum(p.acres, 1)} ac</div>
          <div class="acq-row-meta">${escapeHtml(p.county || '')}</div>
          <div class="acq-row-actions">
            <button data-acq-add="${escapeHtml(p.prop_id || '')}">Add to project</button>
          </div>
        </div>`).join('')
        : `<div class="acq-empty">No cached parcels match “${escapeHtml(name)}”.</div>`;
    } catch (e) {
      $('acq-dock-body').innerHTML =
        `<div class="acq-empty">Owner lookup failed: ${escapeHtml(e.message)}</div>`;
    }
  }

  // ── Dock ────────────────────────────────────────────────────────────────
  function openDock(title, sub, html) {
    $('acq-dock-title').textContent = title;
    $('acq-dock-sub').textContent = sub || '';
    if (html !== undefined) $('acq-dock-body').innerHTML = html;
    $('acq-dock').classList.add('show');
    setTimeout(() => { try { map.invalidateSize(); } catch (e) {} }, 80);
  }
  $('acq-dock-close').addEventListener('click', () => {
    $('acq-dock').classList.remove('show');
    setTimeout(() => { try { map.invalidateSize(); } catch (e) {} }, 80);
  });

  // ── Geocoder ────────────────────────────────────────────────────────────
  let suggestTimer = null;
  let pickedPoint = null;

  $('acq-where').addEventListener('input', (e) => {
    const q = e.target.value.trim();
    clearTimeout(suggestTimer);
    if (q.length < 3) { $('acq-suggest').classList.remove('show'); return; }
    suggestTimer = setTimeout(async () => {
      try {
        const c = map.getCenter();
        const r = await fetch(`/api/acq/geocode/suggest?q=${encodeURIComponent(q)}`
                            + `&lat=${c.lat}&lon=${c.lng}`, { credentials: 'same-origin' });
        const d = await r.json();
        const items = d.suggestions || [];
        const box = $('acq-suggest');
        if (!items.length) { box.classList.remove('show'); return; }
        box.innerHTML = items.map((s, i) =>
          `<div class="acq-suggest-item" data-i="${i}">${escapeHtml(s.label)}</div>`).join('');
        box.classList.add('show');
        box.querySelectorAll('.acq-suggest-item').forEach(el => {
          el.addEventListener('click', () => {
            const s = items[+el.dataset.i];
            $('acq-where').value = s.label;
            pickedPoint = { lat: s.lat, lon: s.lon };
            box.classList.remove('show');
            map.setView([s.lat, s.lon], 12);
          });
        });
      } catch (err) { /* suggestions are a convenience; silence is correct */ }
    }, 260);
  });

  document.addEventListener('click', (e) => {
    if (!e.target.closest('.acq-control--wide')) $('acq-suggest').classList.remove('show');
  });

  // ── Search ──────────────────────────────────────────────────────────────
  async function runSearch() {
    const btn = $('acq-search');
    const where = $('acq-where').value.trim();
    let pt = pickedPoint;

    if (!pt && where) {
      setStatus('Finding that place…');
      try {
        const r = await fetch('/api/acq/geocode?q=' + encodeURIComponent(where),
                              { credentials: 'same-origin' });
        const d = await r.json();
        if (r.ok && d.lat) pt = { lat: d.lat, lon: d.lon };
      } catch (e) { /* fall through to the map centre */ }
    }
    if (!pt) { const c = map.getCenter(); pt = { lat: c.lat, lon: c.lng }; }

    btn.disabled = true;
    setStatus('Searching parcels…');
    try {
      const r = await fetch('/api/acq/search', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        credentials: 'same-origin',
        body: JSON.stringify({
          lat: pt.lat, lon: pt.lon,
          radius_mi: parseFloat($('acq-radius').value) || 5,
          min_acres: parseFloat($('acq-min-acres').value) || 0,
          max_acres: parseFloat($('acq-max-acres').value) || 1e12,
        }),
      });
      const d = await r.json();
      if (!r.ok || d.error) throw new Error(d.error || ('HTTP ' + r.status));

      overlays.tracts.clearLayers();
      overlays.focused.clearLayers();
      overlays.radius.clearLayers();
      fillOn = true;
      if (d.tracts) overlays.tracts.addData(d.tracts);

      map.setView([pt.lat, pt.lon], 11);
      // Style goes on the circle itself. overlays.radius is a GeoJSON layer and
      // its `style` option only applies to features added via addData - an
      // L.circle dropped into the group keeps Leaflet's default blue.
      L.circle([pt.lat, pt.lon], {
        radius: (d.radius_mi || 5) * 1609.344,
        color: '#F25929', weight: 2, dashArray: '6 5', fill: false,
      }).addTo(overlays.radius);
      if (d.count) {
        try { map.fitBounds(overlays.tracts.getBounds(), { padding: [40, 40] }); } catch (e) {}
      }

      const src = d.source === 'live'
        ? ' (live StratMap — this county is not cached locally, so it was slower)'
        : '';
      setStatus(`${fmtNum(d.count)} tract${d.count === 1 ? '' : 's'} · `
              + `${fmtNum(d.total_acres, 0)} acres${src}`);
      renderResults(d);
    } catch (e) {
      setStatus('Search failed: ' + escapeHtml(e.message), true);
    } finally {
      btn.disabled = false;
    }
  }

  function renderResults(d) {
    const feats = ((d.tracts || {}).features) || [];
    const rows = feats.map(f => tractProps(f))
      .sort((a, b) => (b.acres || 0) - (a.acres || 0));
    openDock('Search results',
      `${fmtNum(d.count)} tracts · ${fmtNum(d.total_acres, 0)} acres`,
      rows.length ? rows.map(t => `
        <div class="acq-row" data-pid="${escapeHtml(t.pid)}">
          <div class="acq-row-name">${escapeHtml(t.owner)}</div>
          <div class="acq-row-meta">${fmtNum(t.acres, 1)} ac</div>
          <div class="acq-row-meta">${escapeHtml(t.county)}</div>
          <div class="acq-row-actions">
            <button data-acq-owner="${escapeHtml(t.owner)}">Owner</button>
            <button data-acq-add="${escapeHtml(t.pid)}">Add</button>
          </div>
        </div>`).join('')
      : '<div class="acq-empty">No parcels in that radius matched the acreage filter.</div>');

    $('acq-dock-body').querySelectorAll('.acq-row').forEach(row => {
      row.addEventListener('click', (ev) => {
        if (ev.target.tagName === 'BUTTON') return;
        overlays.tracts.eachLayer(l => {
          if (String(tractProps(l.feature).pid) === String(row.dataset.pid)) {
            map.fitBounds(l.getBounds(), { padding: [60, 60] });
            l.openPopup();
          }
        });
      });
    });
  }

  $('acq-search').addEventListener('click', runSearch);
  $('acq-where').addEventListener('keydown', (e) => {
    if (e.key === 'Enter') { $('acq-suggest').classList.remove('show'); runSearch(); }
  });

  // ── Projects ────────────────────────────────────────────────────────────
  const staged = new Map();   // prop_id -> tract record

  function addToProject(pid) {
    let feat = null;
    overlays.tracts.eachLayer(l => {
      if (String(tractProps(l.feature).pid) === String(pid)) feat = l.feature;
    });
    if (!feat) { setStatus('That parcel is no longer on the map.', true); return; }
    const t = tractProps(feat);
    staged.set(String(pid), {
      prop_id: String(pid), owner_name: t.owner, county: t.county,
      acres: Number(t.acres) || 0, geometry: feat.geometry,
    });
    const acres = [...staged.values()].reduce((s, x) => s + (x.acres || 0), 0);
    setStatus(`${staged.size} tract${staged.size === 1 ? '' : 's'} staged · `
            + `${fmtNum(acres, 1)} ac — click <b>Projects</b> to save them.`);
    map.closePopup();
  }

  async function showProjects() {
    openDock('Projects', 'Loading…', '<div class="acq-empty">Loading…</div>');
    let d = { projects: [], history: [] };
    try {
      const r = await fetch('/api/acq/projects', { credentials: 'same-origin' });
      d = await r.json();
      if (d.error) throw new Error(d.error);
    } catch (e) {
      $('acq-dock-body').innerHTML =
        `<div class="acq-empty">Could not load projects: ${escapeHtml(e.message)}</div>`;
      return;
    }

    const stagedAcres = [...staged.values()].reduce((s, x) => s + (x.acres || 0), 0);
    const stageBlock = staged.size ? `
      <div class="acq-row" style="background:var(--eax-accent-soft)">
        <div class="acq-row-name"><b>${staged.size} staged tract${staged.size === 1 ? '' : 's'}</b></div>
        <div class="acq-row-meta">${fmtNum(stagedAcres, 1)} ac</div>
        <div class="acq-row-meta"></div>
        <div class="acq-row-actions">
          <button id="acq-save-project">Save as project</button>
          <button id="acq-clear-staged">Clear</button>
        </div>
      </div>` : '';

    const list = (arr, empty) => arr.length ? arr.map(p => `
      <div class="acq-row">
        <div class="acq-row-name">${escapeHtml(p.name || 'Untitled')}</div>
        <div class="acq-row-meta">${fmtNum(p.total_acres, 1)} ac</div>
        <div class="acq-row-meta">${(p.tracts || []).length} tracts</div>
        <div class="acq-row-actions">
          <button onclick="location.href='/acquisitions/project/${encodeURIComponent(p.id)}'">Analyse</button>
        </div>
      </div>`).join('') : `<div class="acq-empty">${empty}</div>`;

    $('acq-dock-sub').textContent =
      `${d.projects.length} project${d.projects.length === 1 ? '' : 's'}`
      + (d.history.length ? ` · ${d.history.length} in history` : '');
    $('acq-dock-body').innerHTML = stageBlock
      + list(d.projects || [], 'No saved projects yet. Stage tracts on the map, then save them here.')
      + (d.history.length
          ? '<div class="acq-layers-head" style="border-top:1px solid var(--eax-line)">Quick analysis history</div>'
            + list(d.history, '')
          : '');

    const saveBtn = $('acq-save-project');
    if (saveBtn) saveBtn.addEventListener('click', saveStagedProject);
    const clearBtn = $('acq-clear-staged');
    if (clearBtn) clearBtn.addEventListener('click', () => { staged.clear(); showProjects(); });
  }

  async function saveStagedProject() {
    const name = prompt('Name this project:');
    if (!name || !name.trim()) return;
    try {
      const r = await fetch('/api/acq/projects', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        credentials: 'same-origin',
        body: JSON.stringify({ name: name.trim(), tracts: [...staged.values()] }),
      });
      const d = await r.json();
      if (!r.ok || d.error) throw new Error(d.error || ('HTTP ' + r.status));
      staged.clear();
      const newId = (d.project || {}).id;
      if (newId) {
        setStatus(`Saved “${escapeHtml(name.trim())}” — opening it…`);
        location.href = '/acquisitions/project/' + encodeURIComponent(newId);
        return;
      }
      setStatus(`Saved “${escapeHtml(name.trim())}”.`);
      showProjects();
    } catch (e) {
      setStatus('Could not save the project: ' + escapeHtml(e.message), true);
    }
  }

  $('acq-projects').addEventListener('click', showProjects);

  // ── Boot ────────────────────────────────────────────────────────────────
  buildLayerPanel();
  setTimeout(() => { try { map.invalidateSize(); } catch (e) {} }, 120);

  // Tell the user up front if the parcel cache is empty — an uncached area
  // returns nothing, and "no results" reads as a broken map otherwise.
  fetch('/api/acq/cache/status', { credentials: 'same-origin' })
    .then(r => r.json())
    .then(d => {
      const counties = (d && d.counties) || [];
      if (!counties.length) {
        setStatus('No counties are cached locally yet — searches will fall back '
                + 'to a slower live StratMap query.', true);
      }
    })
    .catch(() => {});
})();
