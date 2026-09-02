/* Ported from the standalone Acquisitions GIS map page.
 * Served as a static file, so Jinja never parses it — the `{{` hazard that
 * applies to the inline templates in app.py does not exist here. Values the
 * page needs from the server arrive on window.ACQ_* from the template.
 */
// -----------------------------------------------------------------------
// Number-input helpers — acres fields show commas (e.g. 100,000), still parsed cleanly
// -----------------------------------------------------------------------
function parseAcres(id) {
  const v = document.getElementById(id).value || '0';
  return parseFloat(String(v).replace(/,/g, '')) || 0;
}

function formatAcres(input) {
  // Allow only digits + commas; reformat on every keystroke (keeping caret roughly stable)
  const cleaned = String(input.value).replace(/[^0-9]/g, '');
  if (cleaned === '') { input.value = ''; return; }
  const n = parseInt(cleaned, 10);
  input.value = n.toLocaleString('en-US');
}

document.querySelectorAll('#min_acres, #max_acres').forEach(el => {
  el.addEventListener('input', () => formatAcres(el));
  el.addEventListener('blur',  () => formatAcres(el));
});

// -----------------------------------------------------------------------
// Group-by helpers — color tracts on the map by owner or by plat,
// so adjacent tracts sharing an owner / plat visually cluster.
// -----------------------------------------------------------------------
function getGroupBy() {
  if (document.getElementById('group-by-owner').checked) return 'owner';
  if (document.getElementById('group-by-plat').checked)  return 'plat';
  return null;
}

// Make the two checkboxes mutually exclusive (you can have neither, but not both)
document.getElementById('group-by-owner').addEventListener('change', (e) => {
  if (e.target.checked) document.getElementById('group-by-plat').checked = false;
});
document.getElementById('group-by-plat').addEventListener('change', (e) => {
  if (e.target.checked) document.getElementById('group-by-owner').checked = false;
});

function groupKeyOf(props, mode) {
  if (mode === 'owner') return (props.OWNER_NAME || '').trim().toUpperCase();
  if (mode === 'plat')  return (props.LEGAL_DESC || '').trim().toUpperCase();
  return '';
}

// Deterministic hash → palette index, so the same owner/plat always gets the same color
function _hashStr(s) {
  let h = 0;
  for (let i = 0; i < s.length; i++) h = ((h << 5) - h + s.charCodeAt(i)) | 0;
  return Math.abs(h);
}
const GROUP_PALETTE = [
  '#e63946','#f4a261','#e9c46a','#2a9d8f','#264653','#9b5de5','#f15bb5','#00bbf9',
  '#00f5d4','#fb6f92','#ff7f50','#7209b7','#3a86ff','#06d6a0','#ffd166','#ef476f',
  '#118ab2','#a8dadc','#457b9d','#bc6c25','#dda15e','#606c38','#cb997e','#a5a58d'
];
function colorForKey(key) {
  if (!key) return '#888';   // unknown owner/plat → neutral gray
  return GROUP_PALETTE[_hashStr(key) % GROUP_PALETTE.length];
}

// Populated by renderSearch when grouping is active; used by tract popups.
window._groupBy = null;
window._groupTotals = new Map();

// -----------------------------------------------------------------------
// Map setup
// -----------------------------------------------------------------------
// Map: keep zoom controls out of the top-left so they don't fight the address bar
const map = L.map('map', { zoomControl: false }).setView([30.0258, -95.8452], 11);
// The page shell's nav toggle needs to tell Leaflet the viewport changed.
// In the standalone this file was inline so `map` was already global; as a
// static module-scope const it is not.
window.map = map;
L.control.zoom({ position: 'bottomright' }).addTo(map);

// "Satellite" = imagery + city/water labels only. (Road-shield overlay was too busy on freeways.)
const satelliteImagery = L.tileLayer(
  'https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}',
  { maxZoom: 19, attribution: 'Imagery © Esri' }
);
const satelliteLabels = L.tileLayer(
  'https://server.arcgisonline.com/ArcGIS/rest/services/Reference/World_Boundaries_and_Places/MapServer/tile/{z}/{y}/{x}',
  { maxZoom: 19, attribution: 'Labels © Esri', pane: 'shadowPane' }
);

const baseLayers = {
  // Satellite is the default — Street + USGS Topo stay available via the layer panel
  "Satellite": L.layerGroup([satelliteImagery, satelliteLabels]).addTo(map),
  "Street":    L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png',
                            { maxZoom: 19, attribution: '© OpenStreetMap' }),
  // USGS Topo basemap — has contour lines baked in. Best for terrain analysis.
  "USGS Topo": L.tileLayer(
    'https://basemap.nationalmap.gov/arcgis/rest/services/USGSTopo/MapServer/tile/{z}/{y}/{x}',
    { maxZoom: 16, attribution: 'USGS National Map' }),
};

const overlays = {
  searchCenter: L.layerGroup().addTo(map),
  radius:       L.layerGroup().addTo(map),
  tracts:       L.layerGroup().addTo(map),
  focusTract:   L.layerGroup().addTo(map),
  counties:     L.layerGroup(),
  etj:          L.layerGroup(),
  schools:      L.layerGroup(),
  water_dist:   L.layerGroup(),
  muds:         L.layerGroup(),
  ccn:          L.layerGroup(),
  electric:     L.layerGroup(),
  flood:        L.layerGroup(),
  wetlands:     L.layerGroup(),
  streams:      L.layerGroup(),
  pipelines:    L.layerGroup(),
  transmission: L.layerGroup(),
  wells:        L.layerGroup(),
  txdot_projects: L.layerGroup(),
  contours:     L.layerGroup(),   // USGS topo tile layer (added below as a tile-source overlay)
  browse:       L.layerGroup(),   // network-triggered, controlled by sidebar checkbox
};

// USGS topographic contours — ArcGIS MapServer overlay via Esri Leaflet's
// dynamicMapLayer (handles the export-image API server-side; pre-tiled URLs
// don't work for this service).
let usgsContoursLayer = null;
if (window.L && L.esri && L.esri.dynamicMapLayer) {
  usgsContoursLayer = L.esri.dynamicMapLayer({
    url: 'https://carto.nationalmap.gov/arcgis/rest/services/contours/MapServer',
    opacity: 0.65,
    attribution: 'Contours: USGS National Map',
  });
  overlays.contours.addLayer(usgsContoursLayer);
} else {
  // Fallback if Esri Leaflet failed to load — log so the user knows why nothing renders
  console.warn('Esri Leaflet not available; USGS contours overlay disabled.');
}

// Unified layer control — basemaps + every overlay in one top-right panel
const overlayDefs = {
  "Search pin":              overlays.searchCenter,
  "Search radius":           overlays.radius,
  "Matching tracts":         overlays.tracts,
  "Focused tract":           overlays.focusTract,
  "Counties":                overlays.counties,
  "City ETJ":                overlays.etj,
  "School districts":        overlays.schools,
  "Water districts":         overlays.water_dist,
  "MUDs only":               overlays.muds,
  "Water CCN":               overlays.ccn,
  "Electric utility (TDU)":  overlays.electric,
  "Floodplain (100-yr)":     overlays.flood,
  "Wetlands (NWI)":          overlays.wetlands,
  "Streams":                 overlays.streams,
  "Pipelines":               overlays.pipelines,
  "Transmission lines":      overlays.transmission,
  "Oil/gas wells":           overlays.wells,
  "TxDOT planned projects":  overlays.txdot_projects,
  "USGS topo contours":      overlays.contours,
  // Removed: Conservation easements (NCED moved to paid), EPA Superfund (no
  // public spatial REST), NRCS soils (ArcGIS Online token required), TX GLO
  // land grants (service path moved). These need paid data sources (Regrid /
  // ATTOM / CoreLogic) or USDA's data download path.
};
// Collapsible layer control — auto-collapses to a small layer icon (top-right of map).
// Hover or click the icon to expand it. Click the layer icon again or click off
// to collapse. Keeps the map view clean when you're not toggling overlays.
// Top-LEFT, under the draw toolbar. It used to sit top-right, where the owner
// and elevation panels covered it - which is why those were flattened into a
// bottom dock. Moving the control instead lets them stay side panels.
const layersControl = L.control.layers(baseLayers, overlayDefs,
                                       { position: 'topleft', collapsed: true }).addTo(map);

// Pinnable layer panel. Collapsed-on-hover is fine for a quick toggle, but it
// closes the moment the pointer leaves, which makes turning several layers on
// in sequence tedious. The pin holds it open until you unpin it, and the choice
// persists across reloads.
(function () {
  const el = layersControl.getContainer();
  if (!el) return;
  const pin = L.DomUtil.create('button', 'layers-pin', el);
  pin.type = 'button';
  pin.innerHTML = '&#128204;';
  pin.style.cssText = 'position:absolute;top:4px;right:4px;z-index:2;border:0;background:none;' +
                      'cursor:pointer;font-size:13px;line-height:1;padding:2px 3px;border-radius:3px;' +
                      'opacity:.35;transition:opacity 120ms,background 120ms;';
  L.DomEvent.disableClickPropagation(pin);

  function apply(pinned) {
    if (pinned) {
      L.DomUtil.addClass(el, 'leaflet-control-layers-expanded');
      pin.style.opacity = '1';
      pin.style.background = 'rgba(242,89,41,0.14)';
      pin.title = 'Unpin layer panel';
    } else {
      pin.style.opacity = '.35';
      pin.style.background = 'none';
      pin.title = 'Pin layer panel open';
    }
    // While pinned, suppress Leaflet's own hover collapse.
    el.classList.toggle('layers-pinned', pinned);
  }

  let pinned = localStorage.getItem('layersPinned') === '1';
  apply(pinned);
  pin.addEventListener('click', (e) => {
    e.preventDefault();
    e.stopPropagation();
    pinned = !pinned;
    localStorage.setItem('layersPinned', pinned ? '1' : '0');
    apply(pinned);
  });
  // Leaflet collapses on mouseout; re-expand immediately when pinned.
  el.addEventListener('mouseleave', () => {
    if (pinned) setTimeout(() => L.DomUtil.addClass(el, 'leaflet-control-layers-expanded'), 0);
  });
})();

let _focusModeRemovedTracts = false;

function _activeBottomDockHeight() {
  const ids = ['dossier-panel', 'elev-dock', 'elev-line-panel'];
  let h = 0;
  ids.forEach(id => {
    const el = document.getElementById(id);
    if (!el) return;
    const shown = el.classList.contains('show') || el.style.display === 'flex' || el.style.display === 'block';
    if (shown) h = Math.max(h, el.getBoundingClientRect().height || 0);
  });
  return h ? h + 28 : 0;
}

function _fitBoundsKeepingDockClear(bounds, opts) {
  if (!bounds || !bounds.isValid || !bounds.isValid()) return;
  const options = Object.assign({
    maxZoom: 17,
    paddingTopLeft: [24, 80],
    paddingBottomRight: [24, _activeBottomDockHeight()],
  }, opts || {});
  try { map.fitBounds(bounds, options); } catch (e) {}
}

function _hideMatchingTractsForFocus() {
  if (map.hasLayer(overlays.tracts)) {
    map.removeLayer(overlays.tracts);
    _focusModeRemovedTracts = true;
  }
}

function _focusTractOnMap(geometry, props, mode) {
  if (!geometry || !geometry.type) return null;
  const p = props || {};
  const label = mode === 'owner' ? 'owner search source'
              : mode === 'elevation' ? 'elevation tract'
              : mode === 'line-elevation' ? 'elevation line area'
              : 'focused tract';
  try { map.closePopup(); } catch (e) {}
  _hideMatchingTractsForFocus();
  overlays.focusTract.clearLayers();
  if (!map.hasLayer(overlays.focusTract)) map.addLayer(overlays.focusTract);
  try {
    const layer = L.geoJSON(
      { type: 'FeatureCollection', features: [{ type: 'Feature', geometry, properties: p }] },
      {
        style: {
          color: '#00BCD4',
          weight: 4,
          fillColor: '#00BCD4',
          fillOpacity: _tractFillOpacity(0.16),
          dashArray: '7,4',
        },
      }
    ).addTo(overlays.focusTract);
    layer.bindPopup(
      `<b>${escapeHtml(p.OWNER_NAME || p.owner_name || label)}</b><br>` +
      `${escapeHtml(String(p.Acres || p.acres || '—'))} ac · Prop ${escapeHtml(p.Prop_ID || p.prop_id || '')}<br>` +
      `<span style="color:#6B7B8B;font-size:11px">Matching search tracts are hidden while this workflow is active.</span>`,
      { maxWidth: 320 }
    );
    try { layer.bringToFront(); } catch (e) {}
    try { _fitBoundsKeepingDockClear(layer.getBounds().pad(0.25)); } catch (e) {}
    return layer;
  } catch (e) {
    console.warn('focus tract draw failed:', e);
    return null;
  }
}

function _restoreMatchingTracts(force) {
  if ((force || _focusModeRemovedTracts) && overlays.tracts.getLayers().length && !map.hasLayer(overlays.tracts)) {
    map.addLayer(overlays.tracts);
  }
  _focusModeRemovedTracts = false;
  overlays.focusTract.clearLayers();
  if (window._renderTractStyles) window._renderTractStyles();
  setTimeout(() => { try { map.invalidateSize(); } catch (e) {} }, 60);
}
window.showMatchingTracts = () => _restoreMatchingTracts(true);

window._tractFillVisible = localStorage.getItem('tractFillVisible') !== '0';

function _tractFillOpacity(opacity) {
  return window._tractFillVisible ? opacity : 0;
}

function _withTractFill(style) {
  return Object.assign({}, style, { fillOpacity: _tractFillOpacity(style.fillOpacity || 0) });
}

// ---- Tract-size legend: collapsible ----------------------------------------
// The owner dock and the elevation panel both open across the bottom of the
// map, and the legend sat on top of them. Collapsing leaves just the title bar,
// and the choice is remembered so it does not have to be closed every session.
function _setTractLegendCollapsed(collapsed) {
  const body = document.getElementById('tract-legend-body');
  const caret = document.getElementById('tract-legend-caret');
  const box = document.getElementById('map-tract-legend');
  if (!body || !caret) return;
  body.style.display = collapsed ? 'none' : '';
  caret.style.transform = collapsed ? 'rotate(-90deg)' : '';
  if (box) box.style.padding = collapsed ? '6px 10px' : '8px 12px';
  try { localStorage.setItem('acq-legend-collapsed', collapsed ? '1' : '0'); }
  catch (e) { /* private mode - the legend still toggles, it just won't persist */ }
}

(function _initTractLegend() {
  const btn = document.getElementById('tract-legend-toggle');
  if (!btn) return;
  let collapsed = false;
  try { collapsed = localStorage.getItem('acq-legend-collapsed') === '1'; }
  catch (e) {}
  _setTractLegendCollapsed(collapsed);
  btn.addEventListener('click', () => {
    const body = document.getElementById('tract-legend-body');
    _setTractLegendCollapsed(body && body.style.display !== 'none');
  });
})();

function _updateTractFillUi() {
  const btn = document.getElementById('tract-fill-toggle');
  if (!btn) return;
  btn.textContent = window._tractFillVisible ? 'Hide fill' : 'Show fill';
  btn.title = window._tractFillVisible
    ? 'Hide tract fills so constraint layers stay visible'
    : 'Restore tract color fills';
  btn.style.background = window._tractFillVisible ? '#FFF' : '#FFF4ED';
  btn.style.borderColor = window._tractFillVisible ? '#DDE3E8' : '#F25929';
  btn.style.color = window._tractFillVisible ? '#13344E' : '#F25929';
}

window.setTractFillVisible = function (visible) {
  window._tractFillVisible = !!visible;
  localStorage.setItem('tractFillVisible', window._tractFillVisible ? '1' : '0');
  _updateTractFillUi();
  if (window._renderTractStyles) window._renderTractStyles();
  overlays.focusTract.eachLayer(layer => {
    try { layer.setStyle({ fillOpacity: _tractFillOpacity(0.16) }); } catch (e) {}
  });
};

window.focusTractForLayerReview = function (propId, geometryStr, ownerName, acres) {
  let geometry = null;
  try { geometry = JSON.parse(geometryStr); } catch (e) {}
  if (!geometry || !geometry.type) return;
  window.setTractFillVisible(false);
  _focusTractOnMap(geometry, { Prop_ID: propId, OWNER_NAME: ownerName, Acres: acres }, 'layer-review');
};

(function initTractFillControls() {
  const toggle = document.getElementById('tract-fill-toggle');
  const clear = document.getElementById('tract-focus-clear');
  [toggle, clear].forEach(el => {
    if (!el) return;
    ['click', 'mousedown', 'dblclick'].forEach(evt => {
      el.addEventListener(evt, e => e.stopPropagation());
    });
  });
  if (toggle) {
    toggle.addEventListener('click', () => {
      window.setTractFillVisible(!window._tractFillVisible);
    });
  }
  if (clear) {
    clear.addEventListener('click', () => {
      window.showMatchingTracts();
    });
  }
  _updateTractFillUi();
})();

// Map layer display-name → key. Mirror of overlayDefs keys.
const _LAYER_NAME_TO_KEY = {
  "Counties": "counties",
  "City ETJ": "etj",
  "School districts": "schools",
  "Water districts": "water_dist",
  "MUDs only": "muds",
  "Water CCN": "ccn",
  "Electric utility (TDU)": "electric",
  "Floodplain (100-yr)": "flood",
  "Wetlands (NWI)": "wetlands",
  "Streams": "streams",
  "Pipelines": "pipelines",
  "Transmission lines": "transmission",
  "Oil/gas wells": "wells",
  "TxDOT planned projects": "txdot_projects",
};

// Popup-field whitelist per layer (extracted to module scope so on-demand layer
// loads can render popups consistently with renderSearch).
const POPUP_FIELDS = {
  counties:     [["BASENAME","County"], ["NAME","Name"], ["STATE","State"]],
  etj:          [["CityName","City"], ["NAME","City"], ["ETJ_Status","ETJ Status"]],
  schools:      [["NAME","District"], ["DISTNAME","District"], ["DISTRICT_N","District"]],
  water_dist:   [["NAME","District"], ["TYPE_DESCRIPTION","Type"], ["TYPE","Type code"], ["COUNTY","County"], ["Area_Acres","Acres"]],
  muds:         [["NAME","District"], ["TYPE_DESCRIPTION","Type"], ["COUNTY","County"], ["Area_Acres","Acres"]],
  ccn:          [["UTILITY","Utility"], ["CCN_NO","CCN #"], ["CCN_TYPE","Type"], ["COUNTY","County"]],
  electric:     [["Utility_Name","Utility (TDU)"], ["Area_SqMi","Service area (sq mi)"]],
  flood:        [["FLD_ZONE","Flood zone"], ["ZONE_SUBTY","Subtype"]],
  wetlands:     [["WETLAND_TYPE","Wetland type"], ["ATTRIBUTE","Code"]],
  streams:      [["GNIS_NAME","Stream"], ["FTYPE","Type"]],
  pipelines:    [["OPERATOR","Operator"], ["COMMODITY","Commodity"], ["DIAMETER","Diameter (in)"]],
  transmission: [["OWNER","Owner"], ["VOLTAGE","Voltage (kV)"], ["STATUS","Status"]],
  txdot_projects: [["HIGHWAY_NUMBER","Highway"], ["PROJ_CLASS","Project class"], ["TYPE_OF_WORK","Work type"], ["LIMITS_FROM","From"], ["LIMITS_TO","To"], ["DIST_LET_DATE","Let date"], ["CONTROL_SECT_JOB","CSJ"]],
};
function _layerPopupHtml(key, props) {
  const fields = POPUP_FIELDS[key] || [];
  const rows = fields
    .map(([k, label]) => [label, props[k]])
    .filter(([_, v]) => v != null && v !== "" && String(v).trim() !== "");
  if (rows.length === 0) {
    return Object.entries(props).filter(([k, v]) =>
      v != null && v !== "" && !k.startsWith('_') &&
      !/^(OBJECTID|FID|SHAPE|Shape__|GLOBALID|ACCURACY|STATUS|COMMENTS|INITIALS|CREATION_D|UPDATED|BNDRY_CHAN|METHOD|SOURCE|DIGITIZED|TX_CNTY|FIPS|DISTRICT_ID)/i.test(k)
    ).slice(0, 5).map(([k, v]) => `<b>${k}:</b> ${v}`).join('<br>');
  }
  return rows.map(([label, v]) => `<b>${label}:</b> ${v}`).join('<br>');
}

// On-demand layer loading: when the user toggles a layer ON in the top-right
// panel, fetch its data for the current map bbox. No search required.
//
// State per layer:
//   _layerLoadedBbox[key] = "minx,miny,maxx,maxy" of the bbox at last load
//   _layerInflight[key]   = AbortController for the in-flight request, if any
// This lets us:
//   - skip the fetch when the user pans only slightly (same loaded bbox)
//   - cancel a stale request when the user pans again before it finishes
const _layerLoadedBbox = {};
const _layerInflight   = {};

// Returns true if the current map bbox is "close enough" to the last loaded
// bbox that we don't need to refetch (within ~25% of the smaller dimension).
function _bboxNearlyEqual(prev, cur) {
  if (!prev) return false;
  const tolerance = Math.min(cur.width, cur.height) * 0.25;
  return Math.abs(prev.w - cur.w) < tolerance
      && Math.abs(prev.s - cur.s) < tolerance
      && Math.abs(prev.e - cur.e) < tolerance
      && Math.abs(prev.n - cur.n) < tolerance;
}

// Show/hide a loading badge next to the layer name in the top-right panel.
function _layerPanelLoading(key, on) {
  const labelMap = {
    counties: "Counties", etj: "City ETJ", schools: "School districts",
    water_dist: "Water districts", muds: "MUDs only", ccn: "Water CCN",
    electric: "Electric utility (TDU)", flood: "Floodplain (100-yr)",
    wetlands: "Wetlands (NWI)", streams: "Streams", pipelines: "Pipelines",
    transmission: "Transmission lines", wells: "Oil/gas wells",
    txdot_projects: "TxDOT planned projects",
  };
  const target = labelMap[key];
  if (!target) return;
  const control = document.querySelector('.leaflet-control-layers-overlays');
  if (!control) return;
  control.querySelectorAll('label span span').forEach(s => {
    const base = s.textContent.replace(/\s*\(\d+\)\s*$/, '').replace(/\s*⏳\s*/, '').trim();
    if (base === target) {
      s.textContent = ' ' + target + (on ? ' ⏳' : '');
    }
  });
}

async function _loadLayerForCurrentView(key, opts) {
  opts = opts || {};
  const overlay = overlays[key];
  if (!overlay) return;
  const b = map.getBounds();
  const cur = {
    w: b.getWest(), s: b.getSouth(), e: b.getEast(), n: b.getNorth(),
    width: b.getEast() - b.getWest(), height: b.getNorth() - b.getSouth(),
  };
  // Skip if loaded bbox is close enough AND overlay still has data
  if (!opts.force && _bboxNearlyEqual(_layerLoadedBbox[key], cur)
      && overlay.getLayers().length > 0) return;
  if (cur.width > 5.0 || cur.height > 5.0) {
    setStatus(`Zoom in to load layers — current view too wide.`, true);
    return;
  }

  // Cancel any prior in-flight fetch for this layer (stale pan)
  if (_layerInflight[key]) {
    try { _layerInflight[key].abort(); } catch {}
  }
  const ctrl = new AbortController();
  _layerInflight[key] = ctrl;
  _layerPanelLoading(key, true);

  try {
    const url = `/api/acq/load-layer/${encodeURIComponent(key)}?minx=${cur.w}&miny=${cur.s}&maxx=${cur.e}&maxy=${cur.n}`;
    const r = await fetch(url, { signal: ctrl.signal });
    const d = await r.json();
    if (!r.ok || d.error) {
      setStatus(`Layer <b>${key}</b> failed: ${escapeHtml(d.error || ('HTTP ' + r.status))}`, true);
      return;
    }
    overlay.clearLayers();
    const fc = d.fc;
    if (fc && fc.features && fc.features.length) {
      if (key === 'wells') {
        fc.features.forEach(f => {
          if (!f.geometry || f.geometry.type !== 'Point') return;
          const [x, y] = f.geometry.coordinates;
          L.circleMarker([y, x], { radius: 3, color: '#b22222',
                                    fillColor: '#e63946', fillOpacity: 0.85,
                                    weight: 0.5 }).addTo(overlay);
        });
      } else {
        L.geoJSON(fc, {
          style: STYLES[key],
          onEachFeature: (f, layer) => {
            layer.bindPopup(_layerPopupHtml(key, f.properties || {}));
          },
        }).addTo(overlay);
      }
    }
    updateLayerCount(key, d.count || 0);
    _layerLoadedBbox[key] = cur;
  } catch (e) {
    if (e.name === 'AbortError') return;   // stale request — silently ignore
    setStatus(`Layer <b>${key}</b> error: ${escapeHtml(e.message)}`, true);
  } finally {
    if (_layerInflight[key] === ctrl) {
      delete _layerInflight[key];
      _layerPanelLoading(key, false);
    }
  }
}

// Wire the on-demand load to Leaflet's overlayadd event — fires when user
// checks a layer in the top-right panel.
map.on('overlayadd', (e) => {
  const key = _LAYER_NAME_TO_KEY[e.name];
  if (!key) return;
  // Always reload on toggle ON — even if overlay has stale data, the user
  // wants to see THIS view. Force ensures the bbox-nearly-equal skip doesn't apply.
  _loadLayerForCurrentView(key, { force: true });
});

// On pan/zoom, refresh visible layers — but only if bbox shifted meaningfully
// (handled inside _loadLayerForCurrentView via _bboxNearlyEqual). 800ms debounce
// so quick map drags don't spam requests.
let _layerPanTimer = null;
map.on('moveend', () => {
  clearTimeout(_layerPanTimer);
  _layerPanTimer = setTimeout(() => {
    for (const key of Object.values(_LAYER_NAME_TO_KEY)) {
      const overlay = overlays[key];
      if (overlay && map.hasLayer(overlay)) {
        _loadLayerForCurrentView(key);
      }
    }
  }, 800);
});

// (No standalone measure tool — Leaflet.draw's polyline/polygon tools already
//  show distance/area inline while drawing. See the "Elevation profile line"
//  feature below for a richer measurement that also samples USGS 3DEP along the line.)

const STYLES = {
  counties:     { color: "#13344E", weight: 2.5, fillOpacity: 0, dashArray: "6,4" },
  etj:          { color: "#A8330D", weight: 1.5, fillColor: "#FCD4C2", fillOpacity: 0.18 },
  schools:      { color: "#0c5460", weight: 1.2, fillColor: "#bee5eb", fillOpacity: 0.10 },
  water_dist:   { color: "#0066cc", weight: 1, fillColor: "#cce5ff", fillOpacity: 0.08 },
  muds:         { color: "#003366", weight: 1.5, fillColor: "#80b3ff", fillOpacity: 0.18 },
  ccn:          { color: "#1aaaaa", weight: 1, fillColor: "#b3e0e0", fillOpacity: 0.10 },
  electric:     { color: "#8B5A00", weight: 1.5, fillColor: "#F4D58D", fillOpacity: 0.15 },
  flood:        { color: "#0040aa", weight: 0.5, fillColor: "#3380ff", fillOpacity: 0.30 },
  wetlands:     { color: "#005c2e", weight: 0.5, fillColor: "#33a06f", fillOpacity: 0.35 },
  streams:      { color: "#1976d2", weight: 1, opacity: 0.85 },
  pipelines:    { color: "#F25929", weight: 2, opacity: 0.85 },
  transmission: { color: "#9c27b0", weight: 2, opacity: 0.85, dashArray: "4,3" },
  txdot_projects: { color: "#FF8F00", weight: 3, opacity: 0.9, dashArray: "8,4" },
};

const LAYER_LABELS = [
  ["counties",     "Counties"],
  ["etj",          "City ETJ"],
  ["schools",      "School districts"],
  ["water_dist",   "Water districts"],
  ["muds",         "MUDs only"],
  ["ccn",          "Water CCN"],
  ["electric",     "Electric utility (TDU)"],
  ["flood",        "Floodplain (100-yr)"],
  ["wetlands",     "Wetlands (NWI)"],
  ["streams",      "Streams"],
  ["pipelines",    "Pipelines"],
  ["transmission", "Transmission lines"],
  ["wells",        "Oil/gas wells"],
  ["txdot_projects", "TxDOT planned projects"],
];

// All layer toggles live in the L.control.layers panel (top-left of map).
// Counts get refreshed by renderSearch via updateLayerCount().
function updateLayerCount(key, n) {
  // Leaflet renders layer rows as <label><span>name</span></label> — patch the span text in place
  const control = document.querySelector('.leaflet-control-layers-overlays');
  if (!control) return;
  const labelMap = {
    counties: "Counties", etj: "City ETJ", schools: "School districts",
    water_dist: "Water districts", muds: "MUDs only", ccn: "Water CCN",
    electric: "Electric utility (TDU)", flood: "Floodplain (100-yr)",
    wetlands: "Wetlands (NWI)", streams: "Streams", pipelines: "Pipelines",
    transmission: "Transmission lines", wells: "Oil/gas wells",
  };
  const target = labelMap[key];
  if (!target) return;
  control.querySelectorAll('label span span').forEach(s => {
    const base = s.textContent.replace(/\s*\(\d+\)\s*$/, '').trim();
    if (base === target) s.textContent = ` ${target}` + (n != null ? ` (${n})` : '');
  });
}

// -----------------------------------------------------------------------
// Pin pick
// -----------------------------------------------------------------------
let pickMode = false;
const pickBtn = document.getElementById('btn-pick');
pickBtn.addEventListener('click', () => {
  pickMode = !pickMode;
  pickBtn.classList.toggle('active', pickMode);
  pickBtn.textContent = pickMode ? 'Click anywhere on the map...' : 'Click map to drop pin';
  map.getContainer().style.cursor = pickMode ? 'crosshair' : '';
});
map.on('click', (e) => {
  if (!pickMode) return;
  setCenter(e.latlng.lat, e.latlng.lng);
  pickMode = false;
  pickBtn.classList.remove('active');
  pickBtn.textContent = 'Click map to drop pin';
  map.getContainer().style.cursor = '';
});

// -----------------------------------------------------------------------
// Import KMZ / KML boundary — uploads file to /api/import-boundary, gets
// back GeoJSON, drops it on the map, and makes it the search polygon so
// the next Run Search hits parcels inside it.
// -----------------------------------------------------------------------
const importedBoundaryOverlay = L.layerGroup().addTo(map);
let _importedBoundary = null;   // GeoJSON Geometry (Polygon or MultiPolygon)

function setImportedBoundary(geojsonFeature, label) {
  importedBoundaryOverlay.clearLayers();
  _importedBoundary = geojsonFeature ? geojsonFeature.geometry : null;
  const info = document.getElementById('imported-boundary-info');
  if (!geojsonFeature) {
    info.style.display = 'none';
    return;
  }
  const layer = L.geoJSON(geojsonFeature, {
    style: { color: '#F25929', weight: 3, fillColor: '#F25929',
             fillOpacity: 0.18, dashArray: '6,4' },
  }).addTo(importedBoundaryOverlay);
  try { map.fitBounds(layer.getBounds().pad(0.2)); } catch {}
  const count = geojsonFeature.properties && geojsonFeature.properties.polygon_count;
  const countStr = count > 1 ? ` (${count} polygons)` : '';
  document.getElementById('imported-boundary-name').textContent =
    `Boundary: ${label || 'imported'}${countStr}`;
  info.style.display = 'block';
}

document.getElementById('btn-import-kmz').addEventListener('click', () => {
  document.getElementById('kmz-file-input').click();
});

document.getElementById('kmz-file-input').addEventListener('change', async (e) => {
  const files = Array.from(e.target.files || []);
  if (files.length === 0) return;

  // ---- Single-file: try the existing single-boundary path first. If the file
  // has no polygon (i.e. it's a centerline KMZ), fall through to bulk-corridor.
  if (files.length === 1) {
    const file = files[0];
    setStatus(`Parsing ${escapeHtml(file.name)}…`);
    try {
      const form = new FormData();
      form.append('file', file);
      const r = await fetch('/api/acq/import-boundary', { method: 'POST', body: form });
      const data = await r.json();
      if (r.ok && !data.error) {
        setImportedBoundary(data, file.name);
        setStatus(`Imported boundary from <b>${escapeHtml(file.name)}</b>. Click <b>Run Search</b> to search inside it.`);
        e.target.value = '';
        return;
      }
      // If it's the "no polygons" case, fall through to corridor flow with this same file.
      const msg = (data && data.error) || '';
      if (!/no polygons|no .kml|no polygon/i.test(msg)) {
        throw new Error(msg || `HTTP ${r.status}`);
      }
    } catch (err) {
      // Network/other failure — give up
      if (!/no polygons|no polygon|no \.kml/i.test(err.message || '')) {
        setStatus(`Import failed: ${escapeHtml(err.message)}`, true);
        e.target.value = '';
        return;
      }
    }
  }

  // ---- Multi-file OR centerline-only single file → bulk corridor import.
  const defaultMiles = '1';
  const ans = prompt(
    files.length > 1
      ? `Bulk import ${files.length} KMZ files.\nFor any centerlines, buffer half-width (miles) on each side:`
      : `This file is a centerline — buffer half-width (miles) on each side:`,
    defaultMiles
  );
  if (ans === null) { e.target.value = ''; return; }
  const bufMiles = parseFloat(ans);
  if (!isFinite(bufMiles) || bufMiles <= 0 || bufMiles > 50) {
    setStatus('Buffer width must be a number between 0 and 50 miles.', true);
    e.target.value = ''; return;
  }

  await _runCorridorImport(files, bufMiles, /*replace=*/ false);
  e.target.value = '';
});

async function _runCorridorImport(files, bufMiles, replace) {
  setStatus(`Importing ${files.length} file(s) — buffering corridors at ±${bufMiles} mi…`);
  try {
    const form = new FormData();
    for (const f of files) form.append('files', f);
    form.append('buffer_miles', String(bufMiles));
    if (replace) form.append('replace', '1');
    const r = await fetch('/api/acq/import-corridors', { method: 'POST', body: form });
    const data = await r.json();
    if (!r.ok || data.error) throw new Error(data.error || `HTTP ${r.status}`);

    const ok      = (data.imported || []).length;
    const skipped = (data.skipped  || []).length;
    const errs    = (data.errors   || []).length;
    const parts = data.imported.map(it => `<b>${escapeHtml(it.label)}</b>`).slice(0, 6);
    const more = ok > 6 ? ` <span style="color:#6B7B8B">+${ok - 6} more</span>` : '';

    let msg = '';
    if (ok > 0) {
      msg += `Imported <b>${ok}</b> ${ok === 1 ? 'corridor' : 'corridors'}` +
             (parts.length ? `: ${parts.join(' · ')}${more}` : '') + '. ';
    }
    if (skipped > 0) {
      // Build a "Replace?" prompt so the user can re-import with overwrite enabled.
      const skippedNames = data.skipped.map(x => x.file).join(', ');
      msg += `<span style="color:#FFA000"><b>${skipped}</b> already imported (skipped):</span> ` +
             `${escapeHtml(skippedNames)}. ` +
             `Use the <b>±Xmi</b> pill on each row to change buffer, ` +
             `or <a href="#" id="bulk-replace-link" style="color:#F25929;font-weight:600">re-import + overwrite →</a>`;
    }
    if (errs) {
      msg += ` <span style="color:#c62828">(${errs} error${errs === 1 ? '' : 's'})</span>`;
    }
    if (!msg) msg = 'Nothing imported.';
    setStatus(msg);

    // Re-attempt with replace=true when the user clicks the link
    const link = document.getElementById('bulk-replace-link');
    if (link) {
      link.addEventListener('click', (ev) => {
        ev.preventDefault();
        if (!confirm(`Overwrite ${skipped} existing corridor(s) with the new ±${bufMiles}mi buffer? This deletes the existing one(s) first.`)) return;
        _runCorridorImport(files, bufMiles, /*replace=*/ true);
      });
    }
    if (typeof loadSearches === 'function') loadSearches();
  } catch (err) {
    setStatus(`Bulk import failed: ${escapeHtml(err.message)}`, true);
  }
}

document.getElementById('btn-clear-boundary').addEventListener('click', () => {
  setImportedBoundary(null);
  setStatus('Imported boundary cleared.');
});

// Save the imported boundary as a Saved Search so it can be re-run later
document.getElementById('btn-save-boundary').addEventListener('click', async () => {
  if (!_importedBoundary) { setStatus('No imported boundary to save.', true); return; }
  const defaultLabel = (document.getElementById('imported-boundary-name').textContent || '')
    .replace(/^Boundary:\s*/, '').replace(/\s*\(\d+ polygons?\)$/, '').trim() || 'Imported boundary';
  const label = prompt('Label for this saved search:', defaultLabel);
  if (!label) return;
  const payload = {
    label, lat: 0, lon: 0, radius_mi: 0,
    min_acres: parseAcres('min_acres') || 1,
    max_acres: parseAcres('max_acres') || 100000,
    polygon: _importedBoundary,
    group_by: getGroupBy(),
  };
  try {
    const r = await fetch('/api/acq/searches', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    const d = await r.json();
    if (!r.ok || d.error) throw new Error(d.error || `HTTP ${r.status}`);
    setStatus(`Saved boundary as "<b>${escapeHtml(label)}</b>". Click ▶ next to it under <b>Saved searches</b> to re-run.`);
    loadSearches();
  } catch (e) {
    setStatus(`Save failed: ${escapeHtml(e.message)}`, true);
  }
});

// -----------------------------------------------------------------------
// Owner-highlight banner — floats above the map when a tract popup is open
// and that owner has multiple tracts in the current search.
// -----------------------------------------------------------------------
function showOwnerHighlightBanner(ownerName, count, acresStr) {
  let el = document.getElementById('owner-highlight-banner');
  if (!el) {
    el = document.createElement('div');
    el.id = 'owner-highlight-banner';
    el.style.cssText = 'position:absolute;top:14px;left:50%;transform:translateX(-50%);' +
      'z-index:1500;background:#F25929;color:#FFF;padding:8px 16px;border-radius:8px;' +
      'box-shadow:0 4px 14px rgba(0,0,0,0.25);font-size:13px;font-weight:600;' +
      'pointer-events:none;line-height:1.4;text-align:center';
    document.getElementById('map').appendChild(el);
  }
  el.innerHTML = `<b>${escapeHtml(ownerName)}</b> owns <b>${count}</b> tracts in this search · ` +
                  `<b>${acresStr}</b> ac total <span style="font-weight:400;opacity:0.85">(all outlined orange)</span>`;
  el.style.display = 'block';
}
function hideOwnerHighlightBanner() {
  const el = document.getElementById('owner-highlight-banner');
  if (el) el.style.display = 'none';
}

// Ember-orange search-center icon (distinct from the blue saved-pin markers)
const SEARCH_PIN_ICON = L.divIcon({
  className: 'search-pin-icon',
  iconSize: [22, 22],
  iconAnchor: [11, 22],
  html: '<svg viewBox="0 0 24 24" width="22" height="22" style="filter:drop-shadow(0 1px 2px rgba(0,0,0,0.4))">'
        + '<path fill="#F25929" stroke="#7b1216" stroke-width="1.2" d="M12 2 C8 2 5 5 5 9 c0 5.25 7 13 7 13 s7-7.75 7-13 c0-4-3-7-7-7 z"/>'
        + '<circle cx="12" cy="9" r="2.5" fill="#FFF"/></svg>',
});

function setCenter(lat, lon) {
  document.getElementById('lat').value = lat.toFixed(4);
  document.getElementById('lon').value = lon.toFixed(4);
  overlays.searchCenter.clearLayers();
  L.marker([lat, lon], { icon: SEARCH_PIN_ICON, title: 'Search center' }).addTo(overlays.searchCenter);
  map.setView([lat, lon], Math.max(map.getZoom(), 11));
}

// Clear all inputs button
document.getElementById('btn-clear-inputs').addEventListener('click', () => {
  ['lat','lon','radius','min_acres','max_acres','label','address'].forEach(id => {
    const el = document.getElementById(id);
    if (el) el.value = '';
  });
  document.getElementById('group-by-owner').checked = false;
  document.getElementById('group-by-plat').checked  = false;
  // Drop any imported boundary too
  if (typeof setImportedBoundary === 'function') setImportedBoundary(null);
  overlays.searchCenter.clearLayers();
  overlays.radius.clearLayers();
  overlays.tracts.clearLayers();
  highlightedPinOverlay.clearLayers();
  setStatus('Inputs cleared. Type an address or click map to start.');
  cancelEdit && cancelEdit();
});

// -----------------------------------------------------------------------
// Address autocomplete (Google Maps style)
// -----------------------------------------------------------------------
const addressInput = document.getElementById('address');
const acDropdown = document.getElementById('ac-dropdown');
let acSuggestions = [];
let acActiveIdx = -1;
let acTimer = null;
const PIN_ICON_SVG = '<svg class="icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 22s-7-7.58-7-13a7 7 0 0114 0c0 5.42-7 13-7 13z"/><circle cx="12" cy="9" r="2.5"/></svg>';

function closeAc() { acDropdown.classList.remove('open'); acDropdown.innerHTML = ''; acActiveIdx = -1; acSuggestions = []; }

function renderAc(suggestions) {
  acSuggestions = suggestions;
  if (!suggestions.length) { closeAc(); return; }
  acDropdown.innerHTML = suggestions.map((s, i) =>
    `<div class="ac-item" data-i="${i}">${PIN_ICON_SVG}<div class="label">${escapeHtml(s.label)}</div></div>`
  ).join('');
  acDropdown.classList.add('open');
  acActiveIdx = -1;
  acDropdown.querySelectorAll('.ac-item').forEach(el => {
    el.addEventListener('mousedown', (e) => {        // mousedown not click — fires before blur
      e.preventDefault();
      pickSuggestion(parseInt(el.dataset.i));
    });
  });
}

function pickSuggestion(i) {
  const s = acSuggestions[i];
  if (!s) return;
  addressInput.value = s.label;
  closeAc();
  setCenter(s.lat, s.lon);
  setStatus(`Pin set at: <b>${escapeHtml(s.label)}</b><br><span style="color:#ABB4BD">${s.lat.toFixed(4)}, ${s.lon.toFixed(4)} — ready to Run Search</span>`);
}

function escapeHtml(s) { return String(s).replace(/[&<>"']/g, c => ({ '&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;' }[c])); }

addressInput.addEventListener('input', () => {
  const q = addressInput.value.trim();
  clearTimeout(acTimer);
  if (q.length < 2) { closeAc(); return; }
  acTimer = setTimeout(async () => {
    try {
      const c = map.getCenter();
      const r = await fetch(`/api/acq/geocode/suggest?q=${encodeURIComponent(q)}&lat=${c.lat}&lon=${c.lng}`);
      const d = await r.json();
      renderAc(d.suggestions || []);
    } catch {}
  }, 220);
});

addressInput.addEventListener('keydown', (e) => {
  if (!acDropdown.classList.contains('open')) {
    if (e.key === 'Enter') geocodeBest();
    return;
  }
  if (e.key === 'ArrowDown') {
    e.preventDefault();
    acActiveIdx = Math.min(acActiveIdx + 1, acSuggestions.length - 1);
    updateActive();
  } else if (e.key === 'ArrowUp') {
    e.preventDefault();
    acActiveIdx = Math.max(acActiveIdx - 1, 0);
    updateActive();
  } else if (e.key === 'Enter') {
    e.preventDefault();
    if (acActiveIdx >= 0) pickSuggestion(acActiveIdx);
    else if (acSuggestions.length) pickSuggestion(0);
    else geocodeBest();
  } else if (e.key === 'Escape') {
    closeAc();
  }
});

function updateActive() {
  acDropdown.querySelectorAll('.ac-item').forEach((el, i) => {
    el.classList.toggle('active', i === acActiveIdx);
  });
}

addressInput.addEventListener('blur', () => setTimeout(closeAc, 150));

document.getElementById('btn-geocode').addEventListener('click', geocodeBest);

async function geocodeBest() {
  const q = addressInput.value.trim();
  if (!q) return;
  // If we have suggestions visible, use the first one
  if (acSuggestions.length) { pickSuggestion(0); return; }
  const btn = document.getElementById('btn-geocode');
  btn.disabled = true; btn.textContent = '...';
  try {
    const c = map.getCenter();
    const r = await fetch(`/api/acq/geocode?q=${encodeURIComponent(q)}&lat=${c.lat}&lon=${c.lng}`);
    const d = await r.json();
    if (d.error) {
      setStatus(`Not found: ${q}`, true);
    } else {
      setCenter(d.lat, d.lon);
      setStatus(`Pin set at: <b>${escapeHtml(d.address)}</b><br><span style="color:#ABB4BD">${d.lat.toFixed(4)}, ${d.lon.toFixed(4)} — ready to Run Search</span>`);
    }
  } catch (e) {
    setStatus('Geocoder error: ' + e.message, true);
  } finally {
    btn.disabled = false; btn.textContent = 'Find';
  }
}

// -----------------------------------------------------------------------
// Search
// -----------------------------------------------------------------------
function setStatus(html, isError = false) {
  const el = document.getElementById('status');
  el.className = 'status' + (isError ? ' error' : '');
  el.innerHTML = html;
}

// Owner / entity search — location-independent, so it does not touch the
// radius controls above it.
(function _wireEntitySearch() {
  const input = document.getElementById('entity-q');
  const btn = document.getElementById('btn-entity');
  if (!input || !btn) return;
  function go() {
    const q = (input.value || '').trim();
    if (!q) { setStatus('Type an owner or entity name first.', true); return; }
    showOwnerDossier(q, null, null, null, true);
  }
  btn.addEventListener('click', go);
  input.addEventListener('keydown', (e) => { if (e.key === 'Enter') { e.preventDefault(); go(); } });
})();

document.getElementById('btn-search').addEventListener('click', runSearch);

async function runSearch() {
  // If the user has any corridors checked in the Corridors toolbar, route the
  // main Run Search button to the corridor-union search instead of a single
  // point-radius search. This way clicking the big primary button always does
  // what the user actually wants, no scrolling to find a second button.
  const corridorSelCount = (window._corridorSelection && window._corridorSelection.size) || 0;
  if (corridorSelCount > 0) {
    return _runSelectedCorridors();
  }
  return _runRadiusSearch();
}

async function _runRadiusSearch() {
  const btn = document.getElementById('btn-search');
  const payload = {
    lat: parseFloat(document.getElementById('lat').value),
    lon: parseFloat(document.getElementById('lon').value),
    radius_mi: parseFloat(document.getElementById('radius').value),
    min_acres: parseAcres('min_acres'),
    max_acres: parseAcres('max_acres'),
    label: document.getElementById('label').value || 'search',
  };
  // If the currently-loaded search has a polygon (drawn from a previously-saved polygon search),
  // include it so the backend runs an inside-polygon search instead of a radius search.
  if (_editingSearchId) {
    const s = _searches.find(x => x.id === _editingSearchId);
    if (s && s.polygon) payload.polygon = s.polygon;
  }
  // Imported KMZ/KML boundary takes precedence — that's what the user just dropped on the map.
  if (_importedBoundary) {
    payload.polygon = _importedBoundary;
  }
  btn.disabled = true; btn.textContent = 'Running...';
  setStatus('Querying parcels and overlays...');
  const t0 = Date.now();
  try {
    const r = await fetch('/api/acq/search', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    const data = await r.json();
    if (data.error) throw new Error(data.error);
    renderSearch(data, payload);
    // Layers stay OFF until the user turns them on. Auto-enabling five overlays
    // on every search buried the tracts under floodplain and wetland polygons
    // and made the map slow to read.
    const dt = ((Date.now() - t0) / 1000).toFixed(1);
    let msg = `<span class="accent">${data.summary.tracts}</span> tracts matched (${dt}s).<br>` +
              `${data.summary.hcad_verified || 0} HCAD-live · ${data.summary.mcad_verified || 0} MCAD-live.`;
    // If grouping is active, add a one-line summary of the biggest cluster(s)
    if (window._groupBy && window._groupTotals && window._groupTotals.size) {
      const totals = Array.from(window._groupTotals.entries())
        .map(([k, v]) => ({ key: k, count: v.count, acres: v.acres, color: v.color }))
        .filter(g => g.count > 1)                     // only clusters (2+ tracts)
        .sort((a, b) => b.acres - a.acres);
      const what = window._groupBy === 'owner' ? 'owners' : 'plats';
      msg += `<br>Grouped by ${window._groupBy} — <b>${window._groupTotals.size}</b> unique ${what}, <b>${totals.length}</b> with 2+ tracts.`;
      if (totals.length) {
        const top = totals.slice(0, 3).map(g => {
          const acresStr = Math.round(g.acres).toLocaleString('en-US');
          const swatch = `<span style="display:inline-block;width:9px;height:9px;background:${g.color};border-radius:2px;vertical-align:middle;margin-right:4px"></span>`;
          const label = g.key.length > 30 ? g.key.slice(0, 30) + '…' : g.key;
          return `${swatch}<b>${escapeHtml(label)}</b> (${g.count}, ${acresStr} ac)`;
        }).join('<br>');
        msg += `<div style="margin-top:4px;font-size:11px;line-height:1.5">Biggest clusters:<br>${top}</div>`;
      }
    }
    if (data.summary.truncated) {
      const matched = ((data.summary.total_matched || data.summary.total_in_buffer || 0)).toLocaleString();
      msg += `<br><span style="color:#c62828"><b>Truncated.</b>Your filter matched <b>${matched}</b>tracts; we're showing the <b>${data.summary.tracts.toLocaleString()}</b>closest to your search center. Tighten the acreage band or shrink the radius to include tracts farther out.</span>`;
    }
    setStatus(msg);
    document.querySelectorAll('#btn-export-kmz, #btn-export-xlsx, #btn-export-pdf').forEach(b => b.disabled = false);
  } catch (e) {
    setStatus('Error: ' + e.message, true);
  } finally {
    btn.disabled = false; btn.textContent = 'Run Search';
  }
}

// -----------------------------------------------------------------------
// Tract results list — populated after each search, drives the sidebar
// list panel + bulk select for exports.
// -----------------------------------------------------------------------
window._tractList = [];          // array of features (with numeric # index)
window._tractSelected = new Set(); // Prop_IDs currently checked

function renderTractList() {
  const filterText = (document.getElementById('tract-filter').value || '').toLowerCase().trim();
  const sort = document.getElementById('tract-sort').value;

  let rows = (window._tractList || []).slice();
  if (filterText) {
    rows = rows.filter(r => {
      const p = r.properties || {};
      return (p.OWNER_NAME || '').toLowerCase().includes(filterText)
          || (p.LEGAL_DESC || '').toLowerCase().includes(filterText)
          || (p._county || '').toLowerCase().includes(filterText);
    });
  }
  const cmp = {
    'acres-desc': (a, b) => (b.properties.Acres || 0) - (a.properties.Acres || 0),
    'acres-asc':  (a, b) => (a.properties.Acres || 0) - (b.properties.Acres || 0),
    'owner':      (a, b) => (a.properties.OWNER_NAME || '').localeCompare(b.properties.OWNER_NAME || ''),
    'county':     (a, b) => (a.properties._county || '').localeCompare(b.properties._county || ''),
    'num':        (a, b) => a._num - b._num,
  }[sort] || ((a, b) => 0);
  rows.sort(cmp);

  const list = document.getElementById('tract-results-list');
  if (!rows.length) {
    list.innerHTML = `<div style="padding:12px;color:#6B7B8B;font-size:11px;text-align:center">No tracts match the filter.</div>`;
  } else {
    list.innerHTML = rows.map(f => {
      const x = f.properties || {};
      const pid = x.Prop_ID || '';
      const ownerEsc = escapeHtml((x.OWNER_NAME || '?'));
      const county   = escapeHtml((x._county || '').replace(' County', ''));
      const acres    = (x.Acres || 0);
      const verifTag = x._hcad_owner_verified ? '<span class="tr-tag">HCAD</span>'
                     : x._mcad_owner_verified ? '<span class="tr-tag" style="background:#1976d2">MCAD</span>' : '';
      const outreachStatus = (window._outreachByPid || {})[pid];
      const outreachTag = outreachStatus ? outreachPill(outreachStatus) : '';
      const checked  = window._tractSelected.has(pid) ? 'checked' : '';
      const selClass = checked ? ' selected' : '';
      return `
        <div class="tract-row${selClass}" data-pid="${escapeHtml(pid)}">
          <input type="checkbox" data-pid-check="${escapeHtml(pid)}" ${checked} onclick="event.stopPropagation()">
          <span class="tr-num">#${f._num}</span>
          <div class="tr-main">
            <div class="tr-owner">${ownerEsc}${verifTag}${outreachTag}</div>
            <div class="tr-meta">${county} · ${escapeHtml(x.LEGAL_DESC || '').slice(0, 36)}</div>
          </div>
          <span class="tr-acres">${acres}</span>
        </div>`;
    }).join('');
    // Wire row clicks (zoom + open popup)
    list.querySelectorAll('.tract-row').forEach(row => {
      row.addEventListener('click', () => {
        const pid = row.dataset.pid;
        overlays.tracts.eachLayer(l => {
          if (l.feature && l.feature.properties && l.feature.properties.Prop_ID === pid) {
            try { map.fitBounds(l.getBounds().pad(0.5)); } catch {}
            l.openPopup();
          }
        });
      });
    });
    // Wire checkboxes (bulk select) — also update the map styles so the user
    // sees the cyan highlight on selected tracts.
    list.querySelectorAll('input[data-pid-check]').forEach(cb => {
      cb.addEventListener('change', (e) => {
        const pid = e.target.dataset.pidCheck;
        if (e.target.checked) window._tractSelected.add(pid);
        else window._tractSelected.delete(pid);
        renderSelectionBar();
        e.target.closest('.tract-row').classList.toggle('selected', e.target.checked);
        if (window._renderTractStyles) window._renderTractStyles();
      });
    });
  }
}

function renderSelectionBar() {
  const bar = document.getElementById('tract-selection-bar');
  if (window._tractSelected.size === 0) {
    bar.style.display = 'none';
    return;
  }
  bar.style.display = 'flex';
  let totalAcres = 0;
  for (const f of (window._tractList || [])) {
    if (window._tractSelected.has((f.properties || {}).Prop_ID)) {
      totalAcres += (f.properties.Acres || 0);
    }
  }
  document.getElementById('tract-sel-count').textContent = window._tractSelected.size;
  document.getElementById('tract-sel-acres').textContent = Math.round(totalAcres).toLocaleString('en-US');
}

document.getElementById('tract-filter').addEventListener('input', renderTractList);
document.getElementById('tract-sort').addEventListener('change', renderTractList);
document.getElementById('btn-tract-select-all').addEventListener('click', () => {
  for (const f of (window._tractList || [])) {
    const pid = (f.properties || {}).Prop_ID;
    if (pid) window._tractSelected.add(pid);
  }
  renderTractList();
  renderSelectionBar();
  if (window._renderTractStyles) window._renderTractStyles();
});
document.getElementById('btn-tract-select-none').addEventListener('click', () => {
  window._tractSelected.clear();
  renderTractList();
  renderSelectionBar();
  if (window._renderTractStyles) window._renderTractStyles();
});

// Single-tract quick analysis — creates a one-tract project on the fly and
// opens its analysis page in a new tab. The user can rename it from the page.
window.quickAnalyzeTract = async function (propId, ownerName, acres, county, geomJsonStr) {
  let geometry = null;
  try { geometry = JSON.parse(geomJsonStr); } catch { geometry = geomJsonStr; }
  if (!geometry) { alert("Couldn't read tract geometry."); return; }
  const defaultName = (ownerName ? ownerName.slice(0, 50) : 'Prop ' + propId)
                       + ` (${Math.round(+acres || 0)} ac)`;
  setStatus(`Building project for <b>${escapeHtml(ownerName || propId)}</b>…`);
  try {
    const r = await fetch('/api/acq/projects', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        name: defaultName,
        is_user_project: false,
        project_kind: 'quick_analysis',
        tracts: [{ prop_id: String(propId), owner_name: ownerName, acres: +acres || 0,
                    county: county || '', geometry }],
      }),
    });
    const d = await r.json();
    if (!r.ok || d.error) throw new Error(d.error || `HTTP ${r.status}`);
    setStatus(`Opening analysis for <b>${escapeHtml(defaultName)}</b>…`);
    if (typeof loadProjectHistory === 'function') loadProjectHistory();
    window.open(`/acquisitions/project/${d.id}`, '_blank');
  } catch (e) {
    setStatus(`Acq Analysis failed: ${escapeHtml(e.message)}`, true);
  }
};

window.createSingleTractProject = async function (propId, ownerName, acres, county, geomJsonStr) {
  let geometry = null;
  try { geometry = JSON.parse(geomJsonStr); } catch { geometry = geomJsonStr; }
  if (!geometry) { alert("Couldn't read tract geometry."); return; }
  const defaultName = (ownerName ? ownerName.slice(0, 50) : 'Prop ' + propId)
                       + ` (${Math.round(+acres || 0)} ac)`;
  const name = prompt(`Name this project (1 tract, ${(+acres || 0).toLocaleString('en-US', { maximumFractionDigits: 0 })} ac):`, defaultName);
  if (!name) return;
  setStatus(`Creating project "${escapeHtml(name.trim())}"…`);
  try {
    const r = await fetch('/api/acq/projects', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        name: name.trim(),
        is_user_project: true,
        project_kind: 'user_project',
        tracts: [{ prop_id: String(propId), owner_name: ownerName, acres: +acres || 0,
                    county: county || '', geometry }],
      }),
    });
    const d = await r.json();
    if (!r.ok || d.error) throw new Error(d.error || `HTTP ${r.status}`);
    setStatus(`Project "<b>${escapeHtml(d.project.name)}</b>" created with 1 tract.`);
    if (typeof loadProjects === 'function') loadProjects();
    window.open(`/acquisitions/project/${d.id}`, '_blank');
  } catch (e) {
    setStatus(`Project create failed: ${escapeHtml(e.message)}`, true);
  }
};

// --- Create Project from selected tracts ---
document.getElementById('btn-create-project').addEventListener('click', async () => {
  if (window._tractSelected.size < 1) return;
  const selectedTracts = (window._tractList || [])
    .filter(f => window._tractSelected.has((f.properties || {}).Prop_ID))
    .map(f => ({
      prop_id:    String(f.properties.Prop_ID || ''),
      owner_name: f.properties.OWNER_NAME || '',
      acres:      f.properties.Acres || 0,
      county:     f.properties._county || '',
      geometry:   f.geometry,
    }))
    .filter(t => t.prop_id);
  if (!selectedTracts.length) return;
  const tractWord = selectedTracts.length === 1 ? 'tract' : 'tracts';
  const defaultName = `Project ${new Date().toLocaleDateString('en-US', { month: 'short', day: 'numeric' })} (${selectedTracts.length} ${tractWord})`;
  const name = prompt(`Name this project (${selectedTracts.length} ${tractWord}, ${selectedTracts.reduce((a,t)=>a+(+t.acres||0),0).toLocaleString('en-US',{maximumFractionDigits:0})} ac total):`, defaultName);
  if (!name) return;
  setStatus(`Creating project "${escapeHtml(name)}"…`);
  try {
    const r = await fetch('/api/acq/projects', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        name: name.trim(),
        is_user_project: true,
        project_kind: 'user_project',
        tracts: selectedTracts,
      }),
    });
    const d = await r.json();
    if (!r.ok || d.error) throw new Error(d.error || `HTTP ${r.status}`);
    setStatus(`Project "<b>${escapeHtml(d.project.name)}</b>" created with ${d.project.tracts.length} tracts. Open it from the Projects section in the sidebar.`);
    loadProjects();
    window._tractSelected.clear();
    renderTractList();
    renderSelectionBar();
  } catch (e) {
    setStatus(`Project create failed: ${escapeHtml(e.message)}`, true);
  }
});

// --- Projects sidebar list ---
async function loadProjects() {
  try {
    const r = await fetch('/api/acq/projects');
    const d = await r.json();
    const items = d.projects || [];
    const badge = document.getElementById('projects-badge');
    const list  = document.getElementById('projects-list');
    if (!items.length) {
      badge.style.display = 'none';
      list.innerHTML = '<div class="changes-empty">Select one or more tracts above, then click <b>Create Project</b>, or use <b>New project</b> in any tract popup.</div>';
      return;
    }
    badge.textContent = items.length;
    badge.style.display = 'inline-block';
    list.innerHTML = items.map(p => `
      <div class="saved-search">
        <div class="name" style="flex:1;cursor:pointer" onclick='window.open("/acquisitions/project/${p.id}", "_blank")' title="Open project">
          ${escapeHtml(p.name)}
          <div class="meta">${p.tract_count} tract${p.tract_count === 1 ? '' : 's'} · ${(p.total_acres||0).toLocaleString('en-US', { maximumFractionDigits: 0 })} ac</div>
        </div>
        <div class="actions">
          <button class="icon-btn" data-act="project-open" data-id="${p.id}" title="Open project page">▶</button>
          <button class="icon-btn" data-act="project-del" data-id="${p.id}" title="Delete project">×</button>
        </div>
      </div>
    `).join('');
    list.querySelectorAll('button[data-act]').forEach(b => b.addEventListener('click', async (e) => {
      e.stopPropagation();
      const id = b.dataset.id;
      if (b.dataset.act === 'project-open') {
        window.open(`/acquisitions/project/${id}`, '_blank');
      } else if (b.dataset.act === 'project-del') {
        if (!confirm('Delete this project? Tracts remain in your search results.')) return;
        await fetch(`/api/acq/projects/${id}`, { method: 'DELETE' });
        loadProjects();
        loadProjectHistory();
      }
    }));
  } catch (e) { console.warn('loadProjects failed', e); }
}
loadProjects();

async function loadProjectHistory() {
  try {
    const r = await fetch('/api/acq/projects?history_only=1');
    const d = await r.json();
    const items = (d.projects || []).slice(0, 12);
    const badge = document.getElementById('project-history-badge');
    const list  = document.getElementById('project-history-list');
    if (!items.length) {
      badge.style.display = 'none';
      list.innerHTML = '<div class="changes-empty">One-tract quick analyses will show here.</div>';
      return;
    }
    badge.textContent = d.projects.length;
    badge.style.display = 'inline-block';
    list.innerHTML = items.map(p => `
      <div class="saved-search">
        <div class="name" style="flex:1;cursor:pointer" onclick='window.open("/acquisitions/project/${p.id}", "_blank")' title="Open analysis">
          ${escapeHtml(p.name)}
          <div class="meta">${p.tract_count} tract${p.tract_count === 1 ? '' : 's'} · ${(p.total_acres||0).toLocaleString('en-US', { maximumFractionDigits: 0 })} ac</div>
        </div>
        <div class="actions">
          <button class="icon-btn" data-act="history-open" data-id="${p.id}" title="Open analysis">▶</button>
          <button class="icon-btn" data-act="history-save" data-id="${p.id}" title="Move to Projects">★</button>
          <button class="icon-btn" data-act="history-del" data-id="${p.id}" title="Delete history item">×</button>
        </div>
      </div>
    `).join('') + (d.projects.length > items.length
      ? `<div class="changes-empty">${d.projects.length - items.length} older quick analyses hidden.</div>`
      : '');
    list.querySelectorAll('button[data-act]').forEach(b => b.addEventListener('click', async (e) => {
      e.stopPropagation();
      const id = b.dataset.id;
      if (b.dataset.act === 'history-open') {
        window.open(`/acquisitions/project/${id}`, '_blank');
      } else if (b.dataset.act === 'history-save') {
        const hit = (d.projects || []).find(p => p.id === id);
        const currentName = hit ? hit.name : 'Project';
        const name = prompt('Name this project:', currentName);
        if (!name) return;
        const rr = await fetch(`/api/acq/projects/${id}`, {
          method: 'PATCH', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ name: name.trim(), is_user_project: true, project_kind: 'user_project' }),
        });
        const dd = await rr.json();
        if (!rr.ok || dd.error) {
          setStatus(`Couldn't move analysis to Projects: ${escapeHtml(dd.error || 'unknown error')}`, true);
          return;
        }
        setStatus(`Moved "<b>${escapeHtml(dd.project.name)}</b>" to Projects.`);
        loadProjectHistory();
        loadProjects();
      } else if (b.dataset.act === 'history-del') {
        if (!confirm('Delete this analysis history item? Tracts remain in your search results.')) return;
        await fetch(`/api/acq/projects/${id}`, { method: 'DELETE' });
        loadProjectHistory();
        loadProjects();
      }
    }));
  } catch (e) { console.warn('loadProjectHistory failed', e); }
}
loadProjectHistory();

async function renderSearch(data, p) {
  Object.values(overlays).forEach(g => g.clearLayers());
  _focusModeRemovedTracts = false;
  if (!map.hasLayer(overlays.tracts)) map.addLayer(overlays.tracts);

  // Fetch which tract Prop_IDs have notes or are favorites (so popups show correct state)
  const propIds = (data.layers.tracts.features || [])
    .map(f => f.properties.Prop_ID).filter(Boolean);
  if (propIds.length) {
    try {
      const [notesRes, favRes, outreachRes] = await Promise.all([
        fetch('/api/acq/notes/by-props',     { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ prop_ids: propIds }) }).then(r => r.json()),
        fetch('/api/acq/favorites/by-props', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ prop_ids: propIds }) }).then(r => r.json()),
        fetch('/api/acq/outreach/by-props',  { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ prop_ids: propIds }) }).then(r => r.json()),
      ]);
      window._noteIndicators = new Set(notesRes.have_notes || []);
      window._favoriteIndicators = new Set(favRes.favorites || []);
      window._outreachByPid = outreachRes.status_by_pid || {};
    } catch {
      window._noteIndicators = new Set();
      window._favoriteIndicators = new Set();
      window._outreachByPid = {};
    }
  } else {
    window._noteIndicators = new Set();
    window._favoriteIndicators = new Set();
    window._outreachByPid = {};
  }

  L.marker([p.lat, p.lon], { icon: SEARCH_PIN_ICON, title: 'Search center' }).addTo(overlays.searchCenter)
    .bindPopup(`<b>Search center</b><br>${p.lat.toFixed(4)}, ${p.lon.toFixed(4)}`);
  L.circle([p.lat, p.lon], {
    radius: p.radius_mi * 1609.344, color: '#F25929', weight: 2,
    fillOpacity: 0.04, dashArray: '8,4',
  }).addTo(overlays.radius);

  // Tracts
  const tracts = data.layers.tracts;

  // Populate the sidebar tract-results list. Stamp each feature with its number
  // (matches the PDF map labels) so the list and map stay in sync.
  window._tractList = (tracts.features || []).map((f, i) => {
    f._num = i + 1;
    return f;
  });
  window._tractSelected.clear();   // selection is per-search
  document.getElementById('tract-results-section').style.display = window._tractList.length ? 'block' : 'none';
  document.getElementById('tract-results-count').textContent = window._tractList.length
    ? `(${window._tractList.length})` : '';
  renderTractList();
  renderSelectionBar();

  // If grouping is active, precompute per-group totals (count + sum acres) so we can
  // show "Owner has N tracts, X ac in your search" in each popup and a top-cluster
  // summary in the status panel.
  const groupBy = getGroupBy();
  const groupTotals = new Map();   // key -> { count, acres, color }
  if (groupBy) {
    for (const f of (tracts.features || [])) {
      const k = groupKeyOf(f.properties, groupBy);
      if (!k) continue;
      const t = groupTotals.get(k) || { count: 0, acres: 0, color: colorForKey(k) };
      t.count += 1;
      t.acres += (f.properties.Acres || 0);
      groupTotals.set(k, t);
    }
  }
  window._groupBy = groupBy;
  window._groupTotals = groupTotals;

  // Remember each tract layer's base (non-highlighted) style so we can revert after a click.
  // The "highlight all by this owner" feature mutates layer styles temporarily.
  window._tractBaseStyle = (f) => {
    if (groupBy) {
      const k = groupKeyOf(f.properties, groupBy);
      return _withTractFill({ color: '#333', weight: 1.5, fillColor: colorForKey(k), fillOpacity: 0.62 });
    }
    const a = f.properties.Acres;
    const fill = a < 500 ? '#e63946' : a < 1000 ? '#f4a261' : '#e9c46a';
    return _withTractFill({ color: '#7b1216', weight: 2, fillColor: fill, fillOpacity: 0.55 });
  };
  // Style applied on top of the base when a tract is selected (via shift-click
  // on the map OR a checkbox in the sidebar list). Cyan dashed border so it's
  // unambiguous against any background.
  window._tractSelectedStyle = function () {
    return _withTractFill({
    color: '#00BCD4', weight: 4, dashArray: '6,4',
    fillColor: '#00BCD4', fillOpacity: 0.30,
    });
  };
  // Track per-tract Leaflet layers so shift-click + checkbox can both update styles
  window._tractLayersByPid = {};
  // Recompute every tract's style based on current selection (call after any
  // selection change from either the map or the sidebar list).
  window._renderTractStyles = function () {
    for (const [pid, lyr] of Object.entries(window._tractLayersByPid)) {
      if (!lyr.feature) continue;
      const isSelected = window._tractSelected.has(pid)
                       || window._tractSelected.has(Number(pid));
      lyr.setStyle(isSelected
        ? window._tractSelectedStyle()
        : window._tractBaseStyle(lyr.feature));
      if (isSelected) { try { lyr.bringToFront(); } catch {} }
    }
  };
  L.geoJSON(tracts, {
    style: window._tractBaseStyle,
    onEachFeature: (f, layer) => {
      const x = f.properties;
      const verified = x._hcad_owner_verified
        ? '<span class="ember-verified" title="Refreshed weekly from HCAD">HCAD LIVE</span>'
        : (x._mcad_owner_verified
            ? '<span class="ember-verified" style="background:#1976d2" title="Refreshed monthly from MCAD">MCAD LIVE</span>'
            : '');
      const electric = x._electric_provider ? escapeHtml(x._electric_provider) : '— (likely rural co-op)';
      // One consolidated water/district line — MUD first, fallback to other districts, then provider.
      let waterLine;
      if (x._in_mud && x._water_dist_name) {
        waterLine = `<b>MUD:</b> ${escapeHtml(x._water_dist_name)}<br>`;
      } else if (x._water_dist_name) {
        waterLine = `<b>Water district:</b> ${escapeHtml(x._water_dist_name)} <span style="color:#666;font-size:11px">(${escapeHtml(x._water_dist_type || '')})</span><br>`;
      } else if (x._water_provider) {
        waterLine = `<b>Water provider:</b> ${escapeHtml(x._water_provider)}${x._water_ccn ? ` <span style="color:#666;font-size:11px">(CCN ${escapeHtml(x._water_ccn)})</span>` : ''}<br>`;
      } else {
        waterLine = `<b>MUD / water:</b> <span style="color:#666">none</span><br>`;
      }
      const center = centerOf(f.geometry).split(',');
      const lat = center[0], lon = center[1];
      const safeOwner = (x.OWNER_NAME || '').replace(/'/g, "&#39;");
      const isFav = window._favoriteIndicators && window._favoriteIndicators.has(x.Prop_ID);
      const favBtn = `<button class="${isFav ? 'fav-on': ''}"onclick='toggleFavorite(${JSON.stringify(x.Prop_ID)}, ${JSON.stringify(x.OWNER_NAME || "")}, ${x.Acres}, ${JSON.stringify(x._county || "")}, ${lat}, ${lon}, ${JSON.stringify(JSON.stringify(f.geometry))}, this)'>${isFav ? 'Favorited': 'Favorite'}</button>`;

      const wells = x._wells_n || 0, pipes = x._pipelines_n || 0;

      // If grouping is active and this tract's group has multiple members,
      // surface that in the popup — that's the assemblage hint.
      let clusterLine = '';
      if (window._groupBy && window._groupTotals) {
        const gk = groupKeyOf(x, window._groupBy);
        const gt = window._groupTotals.get(gk);
        if (gt && gt.count > 1) {
          const acresStr = Math.round(gt.acres).toLocaleString('en-US');
          const dot = `<span style="display:inline-block;width:10px;height:10px;background:${gt.color};border-radius:2px;vertical-align:middle;margin-right:5px"></span>`;
          const what = window._groupBy === 'owner' ? 'tracts (same owner)' : 'tracts (same plat)';
          clusterLine = `<div style="background:#FFF4ED;border-left:3px solid ${gt.color};padding:4px 8px;margin:4px 0;font-size:11px">${dot}<b>${gt.count}</b> ${what} in this search · <b>${acresStr}</b> ac total</div>`;
        }
      }

      // Freshness line — exposes the staleness window so user knows when to verify
      // against deed records. HCAD lags actual deeds by 2-8 weeks + annual roll cycle.
      let freshness = '';
      if (x._hcad_owner_verified) {
        const since = x._owner_since ? ` since <b>${escapeHtml(x._owner_since)}</b>` : '';
        const tyr   = x._hcad_tax_year ? ` · HCAD tax yr ${escapeHtml(String(x._hcad_tax_year))}` : '';
        freshness = `<div style="font-size:11px;color:#666;margin:2px 0" title="HCAD updates ownership ~2-8 weeks after a deed is recorded with the County Clerk, plus the annual roll cycle. Use 'Clerk deeds →' below to check for newer transactions HCAD hasn't picked up yet.">Owner${since}${tyr} <span style="color:#999">ⓘ</span></div>`;
      } else if (x._mcad_owner_verified) {
        freshness = `<div style="font-size:11px;color:#666;margin:2px 0" title="MCAD refreshed monthly. For real-time ownership, check Montgomery County Clerk deed records.">MCAD live (monthly) <span style="color:#999">ⓘ</span></div>`;
      } else {
        freshness = `<div style="font-size:11px;color:#9a9a9a;margin:2px 0" title="Statewide StratMap parcel data refreshes annually. The county CAD always has fresher owner records — for non-Harris/non-Montgomery counties, check the county's appraisal district website directly.">StratMap (annual statewide) <span style="color:#999">ⓘ</span></div>`;
      }

      // Pass the enriched tract props so the modal can show tax jurisdictions, etc.
      const enrichedSubset = {
        Prop_ID: x.Prop_ID,
        OWNER_NAME: x.OWNER_NAME,
        Acres: x.Acres,
        _county: x._county,
        _city_etj: x._city_etj,
        _in_city: x._in_city,
        _schools: x._schools,
        _school_dist: x._school_dist,
        _water_dist_name: x._water_dist_name,
        _water_dist_type: x._water_dist_type,
        _water_provider: x._water_provider,
        _in_mud: x._in_mud,
        _electric_provider: x._electric_provider,
        _flood_pct: x._flood_pct,
        _hcad_owner_verified: x._hcad_owner_verified,
        _mcad_owner_verified: x._mcad_owner_verified,
      };
      const detailsBtn = `<button onclick='showParcelDetail(${JSON.stringify(x.Prop_ID)}, ${JSON.stringify(enrichedSubset)})'>Full details</button>`;
      const focusLayerBtn = `<button onclick='focusTractForLayerReview(${JSON.stringify(x.Prop_ID)}, ${JSON.stringify(JSON.stringify(f.geometry))}, ${JSON.stringify(x.OWNER_NAME || "")}, ${x.Acres})' title="Hide other matching tracts and turn off tract fill so layers show through">Focus layers</button>`;

      // Register this layer so shift-click and checkbox can both restyle it
      const pidStr = String(x.Prop_ID);
      window._tractLayersByPid[pidStr] = layer;

      layer.bindPopup(
        `<b>${escapeHtml(x.OWNER_NAME || '?')}</b>${verified} — ${x.Acres} ac<br>` +
        freshness +
        clusterLine +
        `County: ${escapeHtml(x._county || '')}` +
          (x._city_etj ? ` · ETJ: ${escapeHtml(x._city_etj)}` : '') + `<br>` +
        `Mailing: ${escapeHtml(x.MAIL_ADDR || '')}<br>` +
        `Site: ${escapeHtml(x.SITUS_ADDR || '(no street address)')}<br>` +
        waterLine +
        `<b>Electric:</b> ${electric}<br>` +
        `Flood: ${x._flood_pct || 0}%` +
          (x._wetlands_pct ? ` · Wetlands: ${x._wetlands_pct}%` : '') +
          ` · Wells: ${wells} · Pipes: ${pipes}` +
          (x._transmission_n ? ` · Tx lines: ${x._transmission_n}` : '') + `<br>` +
        `Prop ID: ${escapeHtml(x.Prop_ID)}<br>` +
        `<div style="font-size:11px;color:#6B7B8B;margin:4px 0">Shift+click any tract on the map to add/remove from selection.</div>` +
        `<a target="_blank" href="https://www.google.com/maps/?q=${lat},${lon}">Google Maps</a>` +
        `<div class="popup-actions">` +
        detailsBtn +
        focusLayerBtn +
        favBtn +
        `<button onclick='quickAnalyzeTract(${JSON.stringify(x.Prop_ID)}, ${JSON.stringify(x.OWNER_NAME || "")}, ${x.Acres}, ${JSON.stringify(x._county || "")}, ${JSON.stringify(JSON.stringify(f.geometry))})'style="background:#F25929;color:#FFF" title="Open a one-off analysis without adding it to Projects">Acq Analysis</button>` +
        `<button onclick='createSingleTractProject(${JSON.stringify(x.Prop_ID)}, ${JSON.stringify(x.OWNER_NAME || "")}, ${x.Acres}, ${JSON.stringify(x._county || "")}, ${JSON.stringify(JSON.stringify(f.geometry))})' title="Save this tract as a Project">New project</button>` +
        `<button onclick='showElevationProfile(${JSON.stringify(x.Prop_ID)}, ${JSON.stringify(x.OWNER_NAME || "")}, ${x.Acres}, ${JSON.stringify(JSON.stringify(f.geometry))})'>Elevation</button>` +
        `<button onclick='showOwnerDossier(${JSON.stringify(x.OWNER_NAME || "")}, ${JSON.stringify(x.Prop_ID || "")}, ${JSON.stringify(x.Acres || 0)}, ${JSON.stringify(JSON.stringify(f.geometry))})'>Search by owner</button>` +
        `<button onclick='showOutreach(${JSON.stringify(x.Prop_ID)}, ${JSON.stringify(x.OWNER_NAME || "")}, ${x.Acres}, ${JSON.stringify(x._county || "")})'>Outreach</button>` +
        `<button onclick='window.open("/api/acq/tract-sheet/${encodeURIComponent(x.Prop_ID)}", "_blank")'>Tract sheet</button>` +
        `<button onclick='saveTract(${JSON.stringify(x.Prop_ID)}, ${JSON.stringify(x.OWNER_NAME || "")}, ${x.Acres}, ${JSON.stringify(x._county || "")}, ${lat}, ${lon}, ${JSON.stringify(JSON.stringify(f.geometry))})'>Save to folder</button>` +
        `</div>`,
        { maxWidth: 420 }
      );

      // Shift+click toggles the tract in/out of the selection set (same set the
      // sidebar checkboxes use, so "Create Project" includes both sources).
      // Regular click keeps the existing popup-open behavior.
      layer.on('click', (ev) => {
        if (ev.originalEvent && ev.originalEvent.shiftKey) {
          L.DomEvent.stopPropagation(ev);
          if (ev.originalEvent.preventDefault) ev.originalEvent.preventDefault();
          // Don't open popup
          setTimeout(() => layer.closePopup(), 0);
          // Toggle selection
          if (window._tractSelected.has(pidStr)) {
            window._tractSelected.delete(pidStr);
          } else {
            window._tractSelected.add(pidStr);
          }
          renderTractList();   // update sidebar checkbox state
          renderSelectionBar();
          window._renderTractStyles();
        }
      });

      // Highlight every same-owner tract whenever this popup opens. Click any
      // tract → all of that owner's other parcels in the search glow with a
      // bold dashed border so adjacency / assemblage is immediately visible.
      layer.on('popupopen', () => {
        const targetOwner = (x.OWNER_NAME || '').trim().toUpperCase();
        if (!targetOwner) return;
        let count = 0, totalAcres = 0;
        overlays.tracts.eachLayer(lyr => {
          const p = lyr.feature && lyr.feature.properties;
          if (!p) return;
          if ((p.OWNER_NAME || '').trim().toUpperCase() === targetOwner) {
            count += 1;
            totalAcres += (p.Acres || 0);
            // Bold orange dashed outline + brighter fill — sits on top of group color
            lyr.setStyle({
              color: '#F25929',
              weight: 4,
              dashArray: '6,4',
              fillOpacity: _tractFillOpacity(0.75),
            });
            try { lyr.bringToFront(); } catch {}
          }
        });
        // Banner on top of the map with the count + total
        if (count > 1) {
          const acresStr = Math.round(totalAcres).toLocaleString('en-US');
          showOwnerHighlightBanner(x.OWNER_NAME, count, acresStr);
        }
      });
      layer.on('popupclose', () => {
        if (window._renderTractStyles) window._renderTractStyles();
        hideOwnerHighlightBanner();
      });
    },
  }).addTo(overlays.tracts);

  // Field whitelists per overlay — keep popups readable, drop metadata noise
  const POPUP_FIELDS = {
    counties:     [["BASENAME","County"], ["NAME","Name"], ["STATE","State"]],
    etj:          [["CityName","City"], ["NAME","City"], ["ETJ_Status","ETJ Status"]],
    schools:      [["NAME","District"], ["DISTNAME","District"], ["DISTRICT_N","District"]],
    water_dist:   [["NAME","District"], ["TYPE_DESCRIPTION","Type"], ["TYPE","Type code"], ["COUNTY","County"], ["Area_Acres","Acres"]],
    muds:         [["NAME","District"], ["TYPE_DESCRIPTION","Type"], ["COUNTY","County"], ["Area_Acres","Acres"]],
    ccn:          [["UTILITY","Utility"], ["CCN_NO","CCN #"], ["CCN_TYPE","Type"], ["COUNTY","County"]],
    electric:     [["Utility_Name","Utility (TDU)"], ["Area_SqMi","Service area (sq mi)"]],
    flood:        [["FLD_ZONE","Flood zone"], ["ZONE_SUBTY","Subtype"]],
    wetlands:     [["WETLAND_TYPE","Wetland type"], ["ATTRIBUTE","Code"]],
    streams:      [["GNIS_NAME","Stream"], ["FTYPE","Type"]],
    pipelines:    [["OPERATOR","Operator"], ["COMMODITY","Commodity"], ["DIAMETER","Diameter (in)"]],
    transmission: [["OWNER","Owner"], ["VOLTAGE","Voltage (kV)"], ["STATUS","Status"]],
    txdot_projects: [["HIGHWAY_NUMBER","Highway"], ["PROJ_CLASS","Project class"], ["TYPE_OF_WORK","Work type"], ["LIMITS_FROM","From"], ["LIMITS_TO","To"], ["DIST_LET_DATE","Let date"], ["CONTROL_SECT_JOB","CSJ"]],
  };

  function popupHtml(key, props) {
    const fields = POPUP_FIELDS[key] || [];
    const rows = fields
      .map(([k, label]) => [label, props[k]])
      .filter(([_, v]) => v != null && v !== "" && String(v).trim() !== "");
    if (rows.length === 0) {
      // Fallback: show whatever the layer has (skip metadata-y fields)
      return Object.entries(props).filter(([k,v]) =>
        v != null && v !== "" &&
        !k.startsWith('_') &&
        !/^(OBJECTID|FID|SHAPE|Shape__|GLOBALID|ACCURACY|STATUS|COMMENTS|INITIALS|CREATION_D|UPDATED|BNDRY_CHAN|METHOD|SOURCE|DIGITIZED|TX_CNTY|FIPS|DISTRICT_ID)/i.test(k)
      ).slice(0, 5).map(([k,v]) => `<b>${k}:</b> ${v}`).join('<br>');
    }
    return rows.map(([label, v]) => `<b>${label}:</b> ${v}`).join('<br>');
  }

  // Other layers
  for (const key of ["counties","etj","schools","water_dist","muds","ccn","electric","flood","wetlands","streams","pipelines","transmission","txdot_projects"]) {
    const fc = data.layers[key];
    updateLayerCount(key, fc && fc.features ? fc.features.length : 0);
    if (!fc || !fc.features || fc.features.length === 0) continue;
    L.geoJSON(fc, {
      style: STYLES[key],
      onEachFeature: (f, layer) => {
        layer.bindPopup(popupHtml(key, f.properties || {}));
      },
    }).addTo(overlays[key]);
  }

  // Wells
  const wells = data.layers.wells;
  updateLayerCount('wells', wells && wells.features ? wells.features.length : 0);
  if (wells && wells.features) {
    wells.features.forEach(f => {
      if (!f.geometry || f.geometry.type !== 'Point') return;
      const [x, y] = f.geometry.coordinates;
      L.circleMarker([y, x], { radius: 3, color: '#b22222', fillColor: '#e63946', fillOpacity: 0.85, weight: 0.5 })
        .addTo(overlays.wells);
    });
  }

  if (tracts.features.length) {
    const layer = L.geoJSON(tracts);
    map.fitBounds(layer.getBounds().pad(0.1));
  }
}

function centerOf(g) {
  if (g.type === 'Point') return `${g.coordinates[1]},${g.coordinates[0]}`;
  let xs = 0, ys = 0, n = 0;
  const ring = g.type === 'Polygon' ? g.coordinates[0] : g.coordinates[0][0];
  ring.forEach(([x,y]) => { xs += x; ys += y; n++; });
  return `${ys/n},${xs/n}`;
}

// -----------------------------------------------------------------------
// Exports
// -----------------------------------------------------------------------
['kmz', 'xlsx', 'pdf'].forEach(fmt => {
  document.getElementById('btn-export-' + fmt).addEventListener('click', () => {
    let url = '/api/acq/export/' + fmt;
    // If user has tracts selected in the list, include only those in the export
    if (window._tractSelected && window._tractSelected.size > 0) {
      const ids = Array.from(window._tractSelected).join(',');
      url += '?prop_ids=' + encodeURIComponent(ids);
    }
    window.open(url, '_blank');
  });
});

// -----------------------------------------------------------------------
// Saved searches + change detection (server-backed)
// -----------------------------------------------------------------------
let _searches = [];

async function loadSearches() {
  try {
    const r = await fetch('/api/acq/searches');
    const d = await r.json();
    _searches = d.searches || [];
    renderSaved();
  } catch (e) {
    document.getElementById('saved-list').innerHTML = `<div class="changes-empty">Error loading searches: ${e.message}</div>`;
  }
}

// Per-corridor checkbox state. Defaults to EMPTY so the big primary
// "Run Search" button does a normal radius search until the user explicitly
// opts into a corridor search by checking one or more corridor boxes.
window._corridorSelection = window._corridorSelection || null;   // Set<id> or null = "uninitialized"

function renderSaved() {
  const list = document.getElementById('saved-list');
  if (!_searches.length) {
    list.innerHTML = '<div style="font-size:11px;color:#6B7B8B">No saved searches yet. Click + Save current.</div>';
    return;
  }

  // Partition into corridors vs other saved searches so the corridor toolbar
  // (checkboxes + "Run N selected") can sit above its own group.
  const corridors = [], other = [];
  for (const s of _searches) {
    const isC = ((s.polygon && s.polygon.properties) || {}).source === 'kmz_corridor';
    (isC ? corridors : other).push(s);
  }

  // Initialize selection set the first time we see corridors (NONE selected by
  // default — the user has to opt in to a corridor search by checking boxes).
  if (corridors.length && window._corridorSelection === null) {
    window._corridorSelection = new Set();
  }
  // Drop any selection IDs that no longer exist
  if (window._corridorSelection) {
    for (const id of Array.from(window._corridorSelection)) {
      if (!corridors.find(c => c.id === id)) window._corridorSelection.delete(id);
    }
  }
  const sel = window._corridorSelection || new Set();

  function renderRow(s, withCheckbox) {
    const readOnly = s.read_only;
    const editBtn = readOnly ? '': `<button class="icon-btn"data-act="edit"data-id="${s.id}"title="Edit"></button>`;
    const delBtn  = readOnly ? '' : `<button class="icon-btn" data-act="del"  data-id="${s.id}" title="Delete">×</button>`;
    const sharedMark = readOnly ? ' <span style="color:#FFB997;font-size:10px">(shared)</span>' : '';
    const props = (s.polygon && s.polygon.properties) || {};
    const isCorridor = props.source === 'kmz_corridor';
    const canRebuf   = isCorridor && Array.isArray(props.centerline_coords) && props.centerline_coords.length >= 2;
    const bufMi      = props.buffer_miles;
    const corridorPill = isCorridor
      ? (canRebuf
          ? `<button class="icon-btn corridor-pill"data-act="rebuf"data-id="${s.id}"title="Change corridor buffer width — currently ±${bufMi} mi each side">±${bufMi}mi</button>`
          : `<span class="corridor-pill disabled"title="This corridor was imported before centerlines were stored. Delete it and re-import to enable buffer editing.">±${bufMi}mi · re-import to edit</span>`)
      : '';
    const metaLine = isCorridor
      ? `<div class="meta">${s.min_acres}-${s.max_acres}ac</div>`
      : `<div class="meta">${s.radius_mi}mi · ${s.min_acres}-${s.max_acres}ac</div>`;
    const cb = withCheckbox
      ? `<input type="checkbox" class="corridor-cb" data-id="${s.id}" ${sel.has(s.id) ? 'checked' : ''} style="margin-right:4px;accent-color:#F25929;cursor:pointer">`
      : '';
    // For corridor rows, hide the redundant "  ±Xmi" suffix from the display
    // (the orange pill on the right already shows the buffer width). Strip
    // it so the narrow sidebar doesn't truncate clean corridor names.
    const displayLabel = isCorridor
      ? (s.label || '').replace(/\s*±[\d.]+mi\s*$/, '').trim() || s.label
      : s.label;
    return `
      <div class="saved-search" data-id="${s.id}">
        ${cb}
        <div class="name" title="${escapeHtml(s.label)} — ${s.lat}, ${s.lon} · ${s.radius_mi}mi · ${s.min_acres}-${s.max_acres}ac">${escapeHtml(displayLabel)}${sharedMark}${metaLine}</div>
        <div class="actions">
          ${corridorPill}
          <button class="icon-btn" data-act="load" data-id="${s.id}" title="Load into form">▶</button>
          ${editBtn}${delBtn}
        </div>
      </div>`;
  }

  // Header bar for the corridor group — selection counter + "Run N" button +
  // select-all / clear shortcuts. The radius input above doubles as the
  // corridor buffer half-width at search time.
  let html = '';
  if (corridors.length) {
    const curRadius = parseFloat(document.getElementById('radius')?.value || '1') || 1;
    html += `
      <div id="corridor-bar" style="background:rgba(242,89,41,0.08);border:1px solid rgba(242,89,41,0.30);border-radius:6px;padding:8px 10px;margin-bottom:8px">
        <div style="display:flex;align-items:center;justify-content:space-between;font-size:11px;font-weight:600;color:#FFB997;text-transform:uppercase;letter-spacing:0.06em;margin-bottom:6px">
          <span>Corridors</span>
          <span style="font-weight:400">
            <a href="#" id="corr-all"  style="color:#FFB997;text-decoration:none">all</a> ·
            <a href="#" id="corr-none" style="color:#FFB997;text-decoration:none">none</a>
          </span>
        </div>
        ${corridors.map(c => renderRow(c, /*withCheckbox=*/ true)).join('')}
        <div style="font-size:10px;color:#FFB997;margin-top:8px;line-height:1.45">
          Buffer width is driven by the <b>Radius</b> field above. Change it to widen / narrow every selected corridor at search time.
        </div>
        <button class="btn btn-primary" id="run-corridors-btn" type="button" style="width:100%;margin-top:6px;font-size:12px;padding:8px">
          ▶ Run search across <span id="run-corridors-count">${sel.size}</span> ${sel.size === 1 ? 'corridor' : 'corridors'} @ ±<span id="run-corridors-buf">${curRadius}</span> mi
        </button>
      </div>`;
  }

  // Regular saved searches (everything that's not a corridor)
  html += other.map(s => renderRow(s, /*withCheckbox=*/ false)).join('');

  list.innerHTML = html;

  // Wire row-action buttons
  list.querySelectorAll('button[data-act]').forEach(b => b.addEventListener('click', async (e) => {
    e.stopPropagation();
    const id = b.dataset.id;
    const s = _searches.find(x => x.id === id);
    if (!s) return;
    if (b.dataset.act === 'load') {
      loadSearchIntoForm(s, false);
      setStatus(`Loaded "${escapeHtml(s.label)}". Click Run Search to query the data.`);
    } else if (b.dataset.act === 'edit') {
      loadSearchIntoForm(s, true);
      setStatus(`Editing <b>${escapeHtml(s.label)}</b>. Change values + Save changes, or <a href="#" id="cancel-edit">cancel</a>.`);
      const ce = document.getElementById('cancel-edit');
      if (ce) ce.addEventListener('click', (ev) => { ev.preventDefault(); cancelEdit(); });
    } else if (b.dataset.act === 'del') {
      if (!confirm(`Delete "${s.label}"?`)) return;
      await fetch(`/api/acq/searches/${id}`, { method: 'DELETE' });
      loadSearches();
    } else if (b.dataset.act === 'rebuf') {
      _rebufferCorridor(s);
    }
  }));

  // Wire corridor-bar widgets
  const runBtn = document.getElementById('run-corridors-btn');
  const countSpan = document.getElementById('run-corridors-count');
  const refreshCount = () => {
    if (countSpan) countSpan.textContent = String(window._corridorSelection.size);
    if (runBtn) {
      const n = window._corridorSelection.size;
      runBtn.disabled = (n === 0);
      runBtn.style.opacity = (n === 0) ? '0.5' : '1';
      runBtn.style.cursor = (n === 0) ? 'not-allowed' : 'pointer';
      runBtn.innerHTML = `▶ Run search across <span id="run-corridors-count">${n}</span> ${n === 1 ? 'corridor' : 'corridors'}`;
    }
  };
  list.querySelectorAll('.corridor-cb').forEach(cb => {
    cb.addEventListener('change', (e) => {
      const id = cb.dataset.id;
      if (cb.checked) window._corridorSelection.add(id);
      else            window._corridorSelection.delete(id);
      refreshCount();
      _syncMainRunButton();
    });
  });
  const allLink  = document.getElementById('corr-all');
  const noneLink = document.getElementById('corr-none');
  if (allLink) allLink.addEventListener('click', (e) => {
    e.preventDefault();
    window._corridorSelection = new Set(corridors.map(c => c.id));
    renderSaved();
  });
  if (noneLink) noneLink.addEventListener('click', (e) => {
    e.preventDefault();
    window._corridorSelection = new Set();
    renderSaved();
  });
  if (runBtn) runBtn.addEventListener('click', _runSelectedCorridors);
  refreshCount();
  _syncMainRunButton();

  // Live-update the buffer indicator in the "Run N corridors @ ±X mi" button
  // (and the main Run Search button label) when the user types in the Radius
  // field. Mounted once per render.
  const radInput = document.getElementById('radius');
  if (radInput && !radInput._corridorWired) {
    radInput._corridorWired = true;
    radInput.addEventListener('input', () => {
      const bufSpan = document.getElementById('run-corridors-buf');
      if (bufSpan) bufSpan.textContent = (parseFloat(radInput.value) || 0).toString();
      _syncMainRunButton();
    });
  }
}

// Keep the big primary "Run Search" button's label in sync with what it'll
// actually do when clicked: radius search vs. corridor-union search.
function _syncMainRunButton() {
  const btn = document.getElementById('btn-search');
  if (!btn) return;
  const n = (window._corridorSelection && window._corridorSelection.size) || 0;
  const radius = parseFloat(document.getElementById('radius')?.value || '0') || 0;
  if (n > 0) {
    btn.textContent = `▶ Run search across ${n} corridor${n === 1 ? '' : 's'} @ ±${radius}mi`;
  } else {
    btn.textContent = 'Run Search';
  }
}

async function _runSelectedCorridors() {
  const ids = Array.from(window._corridorSelection || []);
  if (ids.length === 0) {
    setStatus('Pick at least one corridor (checkboxes above).', true);
    return;
  }

  // The Radius field doubles as the runtime corridor buffer. Validate it.
  const radius = parseFloat(document.getElementById('radius').value);
  if (!isFinite(radius) || radius <= 0 || radius > 50) {
    setStatus('Set a corridor buffer in the <b>Radius</b> field (0–50 miles) before running.', true);
    return;
  }

  // ---- Draw a *preview* polygon for each selected corridor at the requested
  // buffer width. The server re-buffers from the stored centerline; we draw a
  // simple ratio-scaled outline here so the user sees the right area BEFORE
  // the search completes. For a perfect match the polygons re-draw after
  // renderSearch using the server's union geometry below.
  const picked = ids.map(id => _searches.find(x => x.id === id)).filter(Boolean);
  highlightedPinOverlay.clearLayers();
  let unionBounds = null;
  picked.forEach(s => {
    if (!s.polygon) return;
    const lyr = L.geoJSON(s.polygon, {
      style: { color: '#F25929', weight: 2.5, fillColor: '#F25929', fillOpacity: 0.10, dashArray: '4,3' },
    }).addTo(highlightedPinOverlay);
    try {
      const b = lyr.getBounds();
      if (b.isValid()) {
        unionBounds = unionBounds ? unionBounds.extend(b) : L.latLngBounds(b);
      }
    } catch {}
  });
  if (unionBounds && unionBounds.isValid()) {
    try { map.fitBounds(unionBounds.pad(0.05)); } catch {}
  }

  const minAc   = parseAcres('min_acres') || 1;
  const maxAc   = parseAcres('max_acres') || 100000;
  const groupBy = getGroupBy();
  const label   = `${ids.length} corridor${ids.length === 1 ? '' : 's'} @ ±${radius}mi`;
  setStatus(`Running search across <b>${ids.length}</b> corridor${ids.length === 1 ? '' : 's'} @ ±${radius} mi · ${minAc}–${maxAc} ac …`);

  const runBtn = document.getElementById('run-corridors-btn');
  if (runBtn) { runBtn.disabled = true; runBtn.style.opacity = '0.6'; }

  // Compute a synthetic "search center" for renderSearch (it expects lat/lon/
  // radius_mi). Use the union-bounds center; radius_mi=0 so no radius circle.
  const ctr = (unionBounds && unionBounds.isValid())
    ? unionBounds.getCenter()
    : { lat: 0, lng: 0 };
  const synth_p = { lat: ctr.lat, lon: ctr.lng, radius_mi: 0, label };

  const t0 = Date.now();
  try {
    const r = await fetch('/api/acq/search-corridors', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        search_ids:    ids,
        min_acres:     minAc,
        max_acres:     maxAc,
        group_by:      groupBy,
        buffer_miles:  radius,    // <-- drives runtime corridor width
        label,
      }),
    });
    const data = await r.json();
    if (!r.ok || data.error) throw new Error(data.error || `HTTP ${r.status}`);

    await renderSearch(data, synth_p);

    // renderSearch clears overlays (including the search-center pin overlay).
    // For corridor searches a single pin in the middle of a multi-corridor
    // union isn't useful, so we leave the pin off.
    overlays.searchCenter.clearLayers();

    // NOTE: corridor union search skips ancillary layers server-side for speed
    // (huge polygons hit Esri rate limits). Layers load per-tract when the user
    // clicks an individual tract — see _loadLayersAroundFocusTract.

    const dt   = ((Date.now() - t0) / 1000).toFixed(1);
    const meta = data.corridor_meta || {};
    const n    = data.summary?.tracts ?? 0;
    const bufStr = meta.buffer_miles ? ` @ ±${meta.buffer_miles}mi` : '';
    let msg = `<span class="accent">${n}</span> tract${n === 1 ? '' : 's'} matched across ` +
              `<b>${meta.count || ids.length}</b> corridor${(meta.count || ids.length) === 1 ? '' : 's'}` +
              `${bufStr} (${dt}s).`;
    if (data.summary?.truncated) {
      const matched = ((data.summary.total_matched || data.summary.total_in_buffer || 0)).toLocaleString();
      msg += `<br><span style="color:#c62828"><b>Truncated.</b>Your filter matched <b>${matched}</b>tracts; we're showing the closest <b>${n.toLocaleString()}</b>. Tighten the acreage band or run fewer corridors at once.</span>`;
    }
    setStatus(msg);
    document.querySelectorAll('#btn-export-kmz, #btn-export-xlsx, #btn-export-pdf').forEach(b => b.disabled = false);
  } catch (e) {
    setStatus(`Corridor search failed: ${escapeHtml(e.message)}`, true);
  } finally {
    if (runBtn) { runBtn.disabled = false; runBtn.style.opacity = '1'; }
  }
}

// Prompt for a new buffer width, then re-buffer the corridor server-side.
async function _rebufferCorridor(s) {
  const cur = (s.polygon && s.polygon.properties && s.polygon.properties.buffer_miles) || 1;
  const ans = prompt(
    `Corridor buffer half-width for "${s.label}" (current ±${cur} mi each side):`,
    String(cur)
  );
  if (ans === null) return;
  const miles = parseFloat(ans);
  if (!isFinite(miles) || miles <= 0 || miles > 50) {
    setStatus('Buffer must be a number between 0 and 50 miles.', true);
    return;
  }
  setStatus(`Re-buffering "${escapeHtml(s.label)}" at ±${miles} mi…`);
  try {
    const r = await fetch(`/api/acq/searches/${encodeURIComponent(s.id)}/rebuffer`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ buffer_miles: miles }),
    });
    const d = await r.json();
    if (!r.ok || d.error) throw new Error(d.error || `HTTP ${r.status}`);
    setStatus(`Updated to <b>${escapeHtml(d.label)}</b>.`);
    await loadSearches();
    // If the rebuffered corridor is currently visualized on the map, refresh it.
    if (window.highlightedPinOverlay && _searches.find(x => x.id === s.id)) {
      const updated = _searches.find(x => x.id === s.id);
      if (updated && updated.polygon) {
        highlightedPinOverlay.clearLayers();
        const layer = L.geoJSON(updated.polygon, {
          style: { color: '#1976d2', weight: 3, fillColor: '#90caf9', fillOpacity: 0.20, dashArray: '6,4' },
        }).addTo(highlightedPinOverlay);
        try { map.fitBounds(layer.getBounds().pad(0.2)); } catch {}
      }
    }
  } catch (e) {
    setStatus(`Rebuffer failed: ${escapeHtml(e.message)}`, true);
  }
}

let _editingSearchId = null;

function loadSearchIntoForm(s, editMode) {
  document.getElementById('lat').value = s.lat || '';
  document.getElementById('lon').value = s.lon || '';
  document.getElementById('radius').value = s.radius_mi || '';
  document.getElementById('min_acres').value = Number(s.min_acres || 0).toLocaleString('en-US');
  document.getElementById('max_acres').value = Number(s.max_acres || 0).toLocaleString('en-US');
  document.getElementById('group-by-owner').checked = (s.group_by === 'owner');
  document.getElementById('group-by-plat').checked  = (s.group_by === 'plat');
  document.getElementById('label').value = s.label;
  _editingSearchId = editMode ? s.id : null;
  document.getElementById('btn-save').textContent = editMode ? `Save changes` : `+ Save current`;
  // If this search has a polygon, draw it on the map so the user sees the search area
  highlightedPinOverlay.clearLayers();   // re-use as a temp visualization layer
  if (s.polygon) {
    const layer = L.geoJSON(s.polygon, {
      style: { color: '#1976d2', weight: 3, fillColor: '#90caf9', fillOpacity: 0.20, dashArray: '6,4' },
    }).addTo(highlightedPinOverlay);
    try { map.fitBounds(layer.getBounds().pad(0.2)); } catch {}
  }
}

function cancelEdit() {
  _editingSearchId = null;
  document.getElementById('btn-save').textContent = '+ Save current';
  setStatus('Edit cancelled.');
}

document.getElementById('btn-save').addEventListener('click', async () => {
  const payload = {
    id: _editingSearchId || undefined,
    label: document.getElementById('label').value || 'search',
    lat: parseFloat(document.getElementById('lat').value),
    lon: parseFloat(document.getElementById('lon').value),
    radius_mi: parseFloat(document.getElementById('radius').value),
    min_acres: parseAcres('min_acres'),
    max_acres: parseAcres('max_acres'),
    group_by: getGroupBy(),
  };
  await fetch('/api/acq/searches', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  });
  const verb = _editingSearchId ? 'Updated' : 'Saved';
  _editingSearchId = null;
  document.getElementById('btn-save').textContent = '+ Save current';
  setStatus(`${verb} "${escapeHtml(payload.label)}".`);
  loadSearches();
});

// -----------------------------------------------------------------------
// Folders
// -----------------------------------------------------------------------
let _folders = [];

async function loadFolders() {
  try {
    const r = await fetch('/api/acq/folders');
    const d = await r.json();
    _folders = d.folders || [];
    renderFolders();
    refreshFolderSelect();
  } catch (e) {
    document.getElementById('folders-list').innerHTML = `<div style="font-size:11px;color:#c62828">Error: ${e.message}</div>`;
  }
}

function renderFolders() {
  const list = document.getElementById('folders-list');
  if (!_folders.length) {
    list.innerHTML = '<div style="font-size:11px;color:#6B7B8B">No folders yet. Click + New folder.</div>';
    return;
  }
  list.innerHTML = _folders.map(f => {
    const sharedNote = f.is_owner
      ? (f.shared_with_names && f.shared_with_names.length
          ? `<span title="Shared with ${escapeHtml(f.shared_with_names.join(', '))}"></span>`
          : '')
      : `<span title="Shared by ${escapeHtml(f.owner_name)}"style="color:#FFB997">from ${escapeHtml(f.owner_name)}</span>`;
    const ownerActions = f.is_owner ? `
        <button class="icon-btn"data-act="share"title="Share with teammates"></button>
        <button class="icon-btn"data-act="rename"title="Rename"></button>
        <button class="icon-btn" data-act="delete" title="Delete">×</button>` : '';
    return `
    <div class="folder-item" data-id="${f.id}">
      <div class="head">
        <span class="swatch-dot" style="background:${f.color}"></span>
        <span class="name">${escapeHtml(f.name)} ${sharedNote}</span>
        <span class="count">${f.search_count || 0} · ${f.pin_count || 0} · ${f.polygon_count || 0}</span>
        <span class="actions">${ownerActions}</span>
      </div>
      <div class="contents" id="folder-contents-${f.id}"></div>
    </div>`;
  }).join('');
  list.querySelectorAll('.folder-item').forEach(item => {
    const fid = item.dataset.id;
    item.querySelector('.head').addEventListener('click', (e) => {
      if (e.target.closest('button')) return;
      const open = item.classList.toggle('open');
      if (open) loadFolderContents(fid);
    });
    const renameBtn = item.querySelector('[data-act="rename"]');
    if (renameBtn) renameBtn.addEventListener('click', async (e) => {
      e.stopPropagation();
      const folder = _folders.find(f => f.id === fid);
      const newName = prompt('Rename folder:', folder.name);
      if (newName && newName.trim()) {
        await fetch(`/api/acq/folders/${fid}`, {
          method: 'PATCH', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ name: newName.trim() })
        });
        loadFolders();
      }
    });
    const delBtn = item.querySelector('[data-act="delete"]');
    if (delBtn) delBtn.addEventListener('click', async (e) => {
      e.stopPropagation();
      const folder = _folders.find(f => f.id === fid);
      if (!confirm(`Delete folder "${folder.name}"? (Contents stay, just unassigned)`)) return;
      await fetch(`/api/acq/folders/${fid}`, { method: 'DELETE' });
      loadFolders(); loadSearches();
    });
    const shareBtn = item.querySelector('[data-act="share"]');
    if (shareBtn) shareBtn.addEventListener('click', (e) => {
      e.stopPropagation();
      openShareModal(fid);
    });
  });
}

async function openShareModal(fid) {
  const folder = _folders.find(f => f.id === fid);
  if (!folder) return;
  document.getElementById('share-sub').textContent = `"${folder.name}" — only the listed teammates will see its contents.`;
  // Fetch all users
  const r = await fetch('/api/acq/users');
  const d = await r.json();
  const meId = (await fetch('/api/acq/me').then(r => r.json())).id;
  const currentlyShared = new Set(folder.shared_with || []);
  const usersDiv = document.getElementById('share-users');
  const others = (d.users || []).filter(u => u.id !== meId);
  if (!others.length) {
    usersDiv.innerHTML = '<div style="font-size:12px;color:#666;padding:8px">No teammates yet. Add some on the Admin page first.</div>';
  } else {
    usersDiv.innerHTML = others.map(u => `
      <label style="display:flex;align-items:center;padding:4px 0;font-weight:400;text-transform:none;margin:0">
        <input type="checkbox" value="${u.id}" ${currentlyShared.has(u.id) ? 'checked' : ''} style="margin-right:8px">
        <span>${escapeHtml(u.name)} <span style="color:#666;font-size:11px">${escapeHtml(u.email)}</span></span>
      </label>
    `).join('');
  }
  const cancel = document.getElementById('share-cancel');
  const save = document.getElementById('share-save');
  const newCancel = cancel.cloneNode(true); cancel.parentNode.replaceChild(newCancel, cancel);
  const newSave = save.cloneNode(true); save.parentNode.replaceChild(newSave, save);
  newCancel.addEventListener('click', () => document.getElementById('share-modal').classList.remove('show'));
  newSave.addEventListener('click', async () => {
    const checked = Array.from(usersDiv.querySelectorAll('input[type=checkbox]:checked')).map(c => c.value);
    await fetch(`/api/acq/folders/${fid}/share`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ shared_with: checked })
    });
    document.getElementById('share-modal').classList.remove('show');
    loadFolders();
  });
  document.getElementById('share-modal').classList.add('show');
}

async function loadFolderContents(fid) {
  const container = document.getElementById(`folder-contents-${fid}`);
  container.innerHTML = '<div style="font-size:11px;color:#6B7B8B;padding:4px">Loading…</div>';
  try {
    // Searches in this folder
    const sRes = await fetch('/api/acq/searches').then(r => r.json());
    const searches = (sRes.searches || []).filter(s => s.folder_id === fid);
    const pinsRes = await fetch(`/api/acq/tract-pins?folder_id=${fid}`).then(r => r.json());
    const pins = pinsRes.pins || [];
    let html = '';
    if (searches.length === 0 && pins.length === 0) {
      html = '<div style="font-size:11px;color:#6B7B8B;padding:4px">Empty folder. Save a search or pin a tract into it.</div>';
    } else {
      searches.forEach(s => {
        html += `<div class="folder-contents-row"><span class="icon"></span><span class="label"data-act="load-search"data-id="${s.id}"title="${escapeHtml(s.label)} — ${s.radius_mi}mi, ${s.min_acres}-${s.max_acres}ac">${escapeHtml(s.label)}</span><span class="row-actions"><button class="icon-btn"data-act="unassign-search"data-id="${s.id}"title="Remove from folder">×</button></span></div>`;
      });
      pins.forEach(p => {
        // Stash the full pin object on the row so the click handler can show its outline + popup.
        _pinCache[p.id] = p;
        const sub = `${p.owner_name || ''} · ${p.acres || '?'} ac`;
        html += `<div class="folder-contents-row"title="${escapeHtml(sub)}"><span class="icon"></span><span class="label"data-act="goto-pin"data-id="${p.id}">${escapeHtml(p.label)}</span><span class="row-actions"><button class="icon-btn"data-act="delete-pin"data-id="${p.id}"title="Delete pin">×</button></span></div>`;
      });
      // Outreach campaign PDF button — shows only if folder has any pinned tracts
      if (pins.length > 0) {
        html += `<div style="padding:6px 4px 2px;text-align:right">
                   <button class="btn-ghost" data-act="outreach-campaign" style="font-size:10px;color:#F25929" title="Multi-page PDF — one detailed dossier per tract (map + ownership + outreach + notes)">
                     Campaign dossier PDF (${pins.length} tracts) →
                   </button>
                 </div>`;
      }
    }
    container.innerHTML = html;
    container.querySelectorAll('[data-act]').forEach(el => el.addEventListener('click', async (e) => {
      e.stopPropagation();
      const act = el.dataset.act;
      if (act === 'load-search') {
        const sid = el.dataset.id;
        const search = (await fetch('/api/acq/searches').then(r => r.json())).searches.find(x => x.id === sid);
        if (search) {
          document.getElementById('lat').value = search.lat;
          document.getElementById('lon').value = search.lon;
          document.getElementById('radius').value = search.radius_mi;
          document.getElementById('min_acres').value = search.min_acres;
          document.getElementById('max_acres').value = search.max_acres;
          document.getElementById('group-by-owner').checked = (search.group_by === 'owner');
          document.getElementById('group-by-plat').checked  = (search.group_by === 'plat');
          document.getElementById('label').value = search.label;
          setStatus(`Loaded "${escapeHtml(search.label)}". Click Run Search.`);
        }
      } else if (act === 'goto-pin') {
        showSavedPin(el.dataset.id);
      } else if (act === 'outreach-campaign') {
        window.open(`/api/acq/outreach-campaign/${encodeURIComponent(fid)}`, '_blank');
      } else if (act === 'unassign-search') {
        await fetch(`/api/acq/searches/${el.dataset.id}/folder`, {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ folder_id: null })
        });
        loadFolderContents(fid); loadFolders(); loadSearches();
      } else if (act === 'delete-pin') {
        if (!confirm('Delete this tract pin?')) return;
        await fetch(`/api/acq/tract-pins/${el.dataset.id}`, { method: 'DELETE' });
        loadFolderContents(fid); loadFolders();
      }
    }));
  } catch (e) {
    container.innerHTML = `<div style="font-size:11px;color:#c62828">${e.message}</div>`;
  }
}

function refreshFolderSelect() {
  // Populate every folder <select> on the page (pin modal + shape modal).
  // Adds a "+ New folder…" option so the user can create one inline.
  const opts = '<option value="">— No folder —</option>' +
    _folders.filter(f => f.is_owner).map(f => `<option value="${f.id}">${escapeHtml(f.name)}</option>`).join('') +
    '<option value="__new__">+ New folder…</option>';
  document.querySelectorAll('select[id$="-folder"]').forEach(sel => {
    const prev = sel.value;
    sel.innerHTML = opts;
    if (prev && prev !== '__new__') sel.value = prev;
  });
}

// Wire any folder <select> to create-folder-inline when user picks "+ New folder…"
document.addEventListener('change', async (e) => {
  const sel = e.target;
  if (!sel.matches || !sel.matches('select[id$="-folder"]')) return;
  if (sel.value !== '__new__') return;
  const name = prompt('New folder name:');
  if (!name || !name.trim()) {
    sel.value = '';
    return;
  }
  const r = await fetch('/api/acq/folders', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ name: name.trim() }),
  });
  const d = await r.json();
  await loadFolders();        // refreshes _folders and re-populates all dropdowns
  if (d.id) sel.value = d.id;
});

// New folder modal
document.getElementById('btn-new-folder').addEventListener('click', () => {
  document.getElementById('folder-name').value = '';
  document.getElementById('folder-modal').classList.add('show');
  setTimeout(() => document.getElementById('folder-name').focus(), 50);
});
document.getElementById('folder-cancel').addEventListener('click', () => document.getElementById('folder-modal').classList.remove('show'));
document.getElementById('folder-create').addEventListener('click', async () => {
  const name = document.getElementById('folder-name').value.trim();
  if (!name) return;
  await fetch('/api/acq/folders', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ name })
  });
  document.getElementById('folder-modal').classList.remove('show');
  loadFolders();
});

// -----------------------------------------------------------------------
// Tract pin save (called from tract popup buttons)
// -----------------------------------------------------------------------
let _pinPending = null;
window.saveTract = function (propId, ownerName, acres, county, lat, lon, geometryStr) {
  let geometry = null;
  if (geometryStr) {
    try { geometry = JSON.parse(geometryStr); } catch {}
  }
  _pinPending = { prop_id: propId, owner_name: ownerName, acres, county, lat, lon, geometry };
  document.getElementById('save-pin-sub').textContent = `${ownerName || ''} — ${acres || '?'} ac (${county || ''})`;
  document.getElementById('pin-label').value = ownerName ? `${ownerName} (${acres} ac)` : propId;
  refreshFolderSelect();
  document.getElementById('save-pin-modal').classList.add('show');
};
document.getElementById('pin-cancel').addEventListener('click', () => document.getElementById('save-pin-modal').classList.remove('show'));
document.getElementById('pin-save').addEventListener('click', async () => {
  if (!_pinPending) return;
  const payload = Object.assign({}, _pinPending, {
    label: document.getElementById('pin-label').value.trim(),
    folder_id: document.getElementById('pin-folder').value || null,
  });
  await fetch('/api/acq/tract-pins', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload)
  });
  document.getElementById('save-pin-modal').classList.remove('show');
  setStatus(`Pinned "${escapeHtml(payload.label)}".`);
  loadFolders();
  loadSavedPinOverlays();
});

// -----------------------------------------------------------------------
// Outreach tracking — per-tract status (Lead → Contacted → Negotiating → …)
// plus an append-only contact log. Team-shared.
// -----------------------------------------------------------------------
window._outreachByPid = {};   // pid -> status, populated after each search

window.showOutreach = async function (propId, ownerName, acres, county) {
  const modal = document.getElementById('outreach-modal');
  const sub   = document.getElementById('outreach-sub');
  const body  = document.getElementById('outreach-body');
  const title = document.getElementById('outreach-title');
  title.textContent = ownerName || 'Outreach';
  sub.textContent = `${county || ''} · ${acres || '?'} ac · Prop ID ${propId}`;
  body.innerHTML = '<div style="text-align:center;padding:30px;color:#6B7B8B">Loading…</div>';
  modal.classList.add('show');
  try {
    const r = await fetch(`/api/acq/outreach/${encodeURIComponent(propId)}`);
    const d = await r.json();
    body.innerHTML = _renderOutreach(d, propId, ownerName, acres, county);
    _wireOutreachActions(propId, ownerName, acres, county, d);
  } catch (e) {
    body.innerHTML = `<div style="padding:20px;color:#c62828">Couldn't load: ${escapeHtml(e.message)}</div>`;
  }
};
document.getElementById('outreach-close').addEventListener('click', () => {
  document.getElementById('outreach-modal').classList.remove('show');
});
document.getElementById('outreach-modal').addEventListener('click', (e) => {
  if (e.target.id === 'outreach-modal') document.getElementById('outreach-modal').classList.remove('show');
});

function _renderOutreach(d, propId, ownerName, acres, county) {
  const rec = d.record || {};
  const currentStatus = rec.status || 'lead';
  const statusOpts = (d.statuses || []).map(s =>
    `<option value="${s.value}" ${s.value === currentStatus ? 'selected' : ''}>${s.label}</option>`
  ).join('');
  const methodOpts = (d.methods || []).map(m =>
    `<option value="${m}">${m.charAt(0).toUpperCase() + m.slice(1)}</option>`
  ).join('');

  const log = (rec.log || []).slice().reverse();   // newest first
  const logHtml = log.length === 0
    ? '<div style="padding:14px;text-align:center;color:#9a9a9a;font-size:12px">No contact log yet. Add the first entry below.</div>'
    : log.map(e => {
        const when = e.at ? new Date(e.at).toLocaleString('en-US', {dateStyle:'medium', timeStyle:'short'}) : '';
        const by   = e.by_name || 'Someone';
        const methodEmoji = { call:'', email:'', letter:'', text:'', meeting:'',
                              'site visit':'', status:'', other:''}[e.method] || '';
        const editedTag = e.edited_at ? ' <span style="color:#9a9a9a;font-size:10px">(edited)</span>' : '';
        const actions = (e.can_edit || e.can_delete) ? `
          <div style="display:flex;gap:8px">
            ${e.can_edit ? `<button class="btn-ghost" style="font-size:10px;color:#1976d2;background:none;border:0;cursor:pointer;padding:0" onclick='editOutreachLog(${JSON.stringify(propId)}, ${JSON.stringify(e.id)}, ${JSON.stringify(e.method || "other")}, ${JSON.stringify(e.notes || "")})'>Edit</button>` : ''}
            ${e.can_delete ? `<button class="btn-ghost" style="font-size:10px;color:#c62828;background:none;border:0;cursor:pointer;padding:0" onclick='deleteOutreachLog(${JSON.stringify(propId)}, ${JSON.stringify(e.id)})'>Delete</button>` : ''}
          </div>` : '';
        // Log-line style: notes text first, then method · author · timestamp underneath
        return `
          <div id="log-entry-${escapeHtml(e.id)}" style="border-bottom:1px solid #E8EBED;padding:8px 0">
            <div style="font-size:13px;color:#13344E;white-space:pre-wrap;line-height:1.4">${escapeHtml(e.notes || '')}</div>
            <div style="display:flex;align-items:baseline;justify-content:space-between;gap:8px;margin-top:4px">
              <div style="font-size:11px;color:#9a9a9a">${methodEmoji} <b style="color:#58595B">${escapeHtml(e.method || 'other')}</b> · ${escapeHtml(by)} · ${escapeHtml(when)}${editedTag}</div>
              ${actions}
            </div>
          </div>`;
      }).join('');

  return `
    <div style="display:grid;grid-template-columns:1fr 1fr;gap:14px;margin-bottom:14px">
      <div>
        <label style="font-size:10px;color:#6B7B8B;text-transform:uppercase;letter-spacing:0.06em;font-weight:700">Status</label>
        <select id="outreach-status" style="width:100%;padding:8px;border:1px solid #D1D3D4;border-radius:6px;font:inherit;margin-top:4px">${statusOpts}</select>
      </div>
      <div>
        <label style="font-size:10px;color:#6B7B8B;text-transform:uppercase;letter-spacing:0.06em;font-weight:700">Broker</label>
        <input id="outreach-broker" type="text" value="${escapeHtml(rec.broker_name || '')}" style="width:100%;padding:8px;border:1px solid #D1D3D4;border-radius:6px;font:inherit;margin-top:4px">
      </div>
      <div>
        <label style="font-size:10px;color:#6B7B8B;text-transform:uppercase;letter-spacing:0.06em;font-weight:700">Next action</label>
        <input id="outreach-next" type="text" value="${escapeHtml(rec.next_action || '')}" placeholder="e.g. Send LOI" style="width:100%;padding:8px;border:1px solid #D1D3D4;border-radius:6px;font:inherit;margin-top:4px">
      </div>
      <div>
        <label style="font-size:10px;color:#6B7B8B;text-transform:uppercase;letter-spacing:0.06em;font-weight:700">By when</label>
        <input id="outreach-next-date" type="date" value="${escapeHtml(rec.next_action_date || '')}" style="width:100%;padding:8px;border:1px solid #D1D3D4;border-radius:6px;font:inherit;margin-top:4px">
      </div>
    </div>
    <button id="outreach-save-fields" class="btn-save" style="background:var(--ember-orange);color:#FFF;padding:8px 14px;border:0;border-radius:6px;font-weight:600;font-size:12px;cursor:pointer">Save status &amp; fields</button>

    <h4 style="margin:20px 0 6px;color:#F25929;text-transform:uppercase;font-size:11px;letter-spacing:0.06em">Contact log</h4>
    <div style="background:#FAFAFA;border-radius:6px;padding:4px 12px;max-height:240px;overflow-y:auto">
      ${logHtml}
    </div>

    <h4 style="margin:20px 0 6px;color:#F25929;text-transform:uppercase;font-size:11px;letter-spacing:0.06em">+ Add entry</h4>
    <div style="display:grid;grid-template-columns:140px 1fr auto;gap:8px;align-items:start">
      <select id="outreach-log-method" style="padding:8px;border:1px solid #D1D3D4;border-radius:6px;font:inherit">${methodOpts}</select>
      <textarea id="outreach-log-notes" placeholder="What happened? (e.g. 'Called owner — Bill says interested but wants $20k/ac, said he'd call back')" style="min-height:60px;padding:8px;border:1px solid #D1D3D4;border-radius:6px;font:inherit;resize:vertical"></textarea>
      <button id="outreach-log-save" class="btn-save" style="background:var(--ember-blue);color:#FFF;padding:8px 14px;border:0;border-radius:6px;font-weight:600;font-size:12px;cursor:pointer;align-self:start;height:60px">Log</button>
    </div>
    ${rec.created_at ? `<div style="margin-top:12px;font-size:10px;color:#9a9a9a">Created ${new Date(rec.created_at).toLocaleString('en-US',{dateStyle:'medium',timeStyle:'short'})} · last updated ${new Date(rec.updated_at).toLocaleString('en-US',{dateStyle:'medium',timeStyle:'short'})}</div>` : ''}
  `;
}

function _wireOutreachActions(propId, ownerName, acres, county, initialData) {
  document.getElementById('outreach-save-fields').addEventListener('click', async () => {
    const payload = {
      status:           document.getElementById('outreach-status').value,
      broker_name:      document.getElementById('outreach-broker').value.trim(),
      next_action:      document.getElementById('outreach-next').value.trim(),
      next_action_date: document.getElementById('outreach-next-date').value,
      owner_name:       ownerName,
      county:           county,
      acres:            acres,
    };
    await fetch(`/api/acq/outreach/${encodeURIComponent(propId)}`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    window._outreachByPid[propId] = payload.status;
    // Re-open to refresh log (in case status changed → auto-logged)
    showOutreach(propId, ownerName, acres, county);
    // Refresh tract list + pipeline view so badges + counts update
    if (typeof renderTractList === 'function') renderTractList();
    if (typeof loadPipeline === 'function') loadPipeline();
  });
  document.getElementById('outreach-log-save').addEventListener('click', async () => {
    const method = document.getElementById('outreach-log-method').value;
    const notes  = document.getElementById('outreach-log-notes').value.trim();
    if (!notes) { document.getElementById('outreach-log-notes').focus(); return; }
    await fetch(`/api/acq/outreach/${encodeURIComponent(propId)}/log`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ method, notes }),
    });
    showOutreach(propId, ownerName, acres, county);
    if (typeof renderTractList === 'function') renderTractList();
    if (typeof loadPipeline === 'function') loadPipeline();
  });
}

window.deleteOutreachLog = async function (propId, entryId) {
  if (!confirm('Delete this log entry?')) return;
  try {
    const r = await fetch(`/api/acq/outreach/${encodeURIComponent(propId)}/log/${encodeURIComponent(entryId)}`,
      { method: 'DELETE' });
    if (!r.ok) throw new Error('Delete failed');
    // Re-render the modal to drop the entry
    const ownerName = document.getElementById('outreach-title').textContent;
    const subParts  = (document.getElementById('outreach-sub').textContent || '').split(' · ');
    const county = subParts[0] || '';
    const acres  = (subParts[1] || '').replace(' ac', '');
    showOutreach(propId, ownerName, acres, county);
    if (typeof renderTractList === 'function') renderTractList();
  } catch (e) {
    alert(e.message);
  }
};

window.editOutreachLog = function (propId, entryId, currentMethod, currentNotes) {
  // Replace the entry's display with an inline edit form
  const el = document.getElementById('log-entry-' + entryId);
  if (!el) return;
  const methods = ['call','email','letter','text','meeting','site visit','other'];
  const opts = methods.map(m =>
    `<option value="${m}" ${m === currentMethod ? 'selected' : ''}>${m.charAt(0).toUpperCase() + m.slice(1)}</option>`
  ).join('');
  // Escape for use inside an HTML textarea
  const escapedNotes = (currentNotes || '').replace(/</g, '&lt;').replace(/>/g, '&gt;');
  el.innerHTML = `
    <div style="background:#FFF4ED;border-left:3px solid #F25929;padding:8px;border-radius:4px">
      <textarea id="edit-log-notes-${entryId}" style="width:100%;min-height:60px;padding:6px;border:1px solid #D1D3D4;border-radius:4px;font:inherit;resize:vertical">${escapedNotes}</textarea>
      <div style="display:flex;gap:8px;align-items:center;margin-top:6px">
        <select id="edit-log-method-${entryId}" style="padding:6px;border:1px solid #D1D3D4;border-radius:4px;font:inherit;font-size:12px">${opts}</select>
        <button onclick='_saveOutreachLogEdit(${JSON.stringify(propId)}, ${JSON.stringify(entryId)})' style="background:var(--ember-orange);color:#FFF;border:0;padding:6px 12px;border-radius:4px;font-weight:600;font-size:12px;cursor:pointer">Save</button>
        <button onclick='showOutreach(${JSON.stringify(propId)}, document.getElementById("outreach-title").textContent, "", "")' style="background:#E8EBED;color:#13344E;border:0;padding:6px 12px;border-radius:4px;font-size:12px;cursor:pointer">Cancel</button>
      </div>
    </div>`;
};

window._saveOutreachLogEdit = async function (propId, entryId) {
  const notes  = document.getElementById('edit-log-notes-' + entryId).value;
  const method = document.getElementById('edit-log-method-' + entryId).value;
  try {
    const r = await fetch(`/api/acq/outreach/${encodeURIComponent(propId)}/log/${encodeURIComponent(entryId)}`, {
      method: 'PATCH', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ notes, method }),
    });
    if (!r.ok) throw new Error('Save failed');
    const ownerName = document.getElementById('outreach-title').textContent;
    const subParts  = (document.getElementById('outreach-sub').textContent || '').split(' · ');
    const county = subParts[0] || '';
    const acres  = (subParts[1] || '').replace(' ac', '');
    showOutreach(propId, ownerName, acres, county);
  } catch (e) {
    alert(e.message);
  }
};

// -----------------------------------------------------------------------
// Pipeline summary — just the lightweight pill in the sidebar that links
// to the dedicated /pipeline page. Full kanban view lives on /pipeline.
// -----------------------------------------------------------------------
window._showArchived = false;
async function loadPipeline() {
  // Sidebar pill mode: fetch outreach data, surface count + overdue badge.
  try {
    const r = await fetch('/api/acq/outreach/pipeline');
    const d = await r.json();
    // Stash the per-pid status for tract-list badges
    window._outreachByPid = {};
    let total = 0, overdue = 0, dueSoon = 0;
    for (const [status, records] of Object.entries(d.by_status || {})) {
      for (const rec of records) {
        if (rec.prop_id) window._outreachByPid[rec.prop_id] = status;
        total++;
        const u = _dueClass(rec.next_action_date);
        if (u && u.isOverdue) overdue++;
        else if (u && u.isDueSoon) dueSoon++;
      }
    }
    const countEl = document.getElementById('pipeline-count-pill');
    if (countEl) countEl.textContent = total ? `(${total})` : '';
    const dueEl = document.getElementById('pipeline-due-pill');
    if (dueEl) {
      if (overdue + dueSoon > 0) {
        dueEl.textContent = ` ${overdue + dueSoon}`;
        dueEl.title = `${overdue} overdue, ${dueSoon} due ≤ 7 days`;
        dueEl.style.display = 'inline-block';
      } else {
        dueEl.style.display = 'none';
      }
    }
  } catch (e) {
    // Stay quiet — the pill is non-critical.
    console.warn('Pipeline summary failed:', e.message);
  }
}

function _dueClass(dateStr) {
  // Returns {label, color, isOverdue, isDueSoon} for a YYYY-MM-DD next_action_date.
  if (!dateStr) return null;
  const due = new Date(dateStr + 'T23:59:59');
  if (isNaN(due)) return null;
  const today = new Date(); today.setHours(0, 0, 0, 0);
  const days = Math.round((due - today) / 86400000);
  if (days < 0)   return { label: `${-days}d overdue`,    color: '#C62828', isOverdue: true,  isDueSoon: false };
  if (days === 0) return { label: 'due today',            color: '#F25929', isOverdue: false, isDueSoon: true  };
  if (days <= 7)  return { label: `in ${days}d`,          color: '#F25929', isOverdue: false, isDueSoon: true  };
  if (days <= 30) return { label: `in ${days}d`,          color: '#FFA000', isOverdue: false, isDueSoon: false };
  return { label: `in ${days}d`, color: '#6B7B8B', isOverdue: false, isDueSoon: false };
}

function renderPipeline(d) {
  // Legacy renderer — kept for back-compat, but the sidebar no longer hosts
  // a kanban list (that's on /pipeline now). Bail if the target isn't here.
  if (!document.getElementById('pipeline-list')) return;
  const byStatus = d.by_status || {};
  const total = d.total || 0;
  document.getElementById('pipeline-count').textContent = total ? `(${total})` : '';

  // Aggregate due-action urgency across all records (regardless of status)
  let overdue = 0, dueSoon = 0;
  for (const records of Object.values(byStatus)) {
    for (const r of records) {
      const u = _dueClass(r.next_action_date);
      if (u && u.isOverdue) overdue++;
      else if (u && u.isDueSoon) dueSoon++;
    }
  }
  const dueEl = document.getElementById('pipeline-due-summary');
  if (overdue + dueSoon > 0) {
    const parts = [];
    if (overdue) parts.push(`<b>${overdue}</b> overdue`);
    if (dueSoon) parts.push(`<b>${dueSoon}</b> due ≤ 7 days`);
    dueEl.innerHTML = ''+ parts.join('· ');
    dueEl.style.display = 'block';
  } else {
    dueEl.style.display = 'none';
  }

  const list = document.getElementById('pipeline-list');
  if (total === 0) {
    list.innerHTML = '<div class="changes-empty">No outreach activity yet. Click Outreach on any tract popup to start tracking.</div>';
    return;
  }

  // Group order: lead → contacted → negotiating → under_contract → closed → lost → passed
  const statusOrder = (d.statuses || []).map(s => s.value);
  const statusMeta = Object.fromEntries((d.statuses || []).map(s => [s.value, s]));

  let html = '';
  for (const status of statusOrder) {
    const records = (byStatus[status] || []).slice();
    if (records.length === 0) continue;
    const meta = statusMeta[status] || { label: status, color: '#6B7B8B' };
    // Sort records within status: overdue first, then due-soon, then by next_date asc
    records.sort((a, b) => {
      const ua = _dueClass(a.next_action_date);
      const ub = _dueClass(b.next_action_date);
      const aScore = ua ? (ua.isOverdue ? 0 : ua.isDueSoon ? 1 : 2) : 3;
      const bScore = ub ? (ub.isOverdue ? 0 : ub.isDueSoon ? 1 : 2) : 3;
      if (aScore !== bScore) return aScore - bScore;
      return (a.next_action_date || '~') < (b.next_action_date || '~') ? -1 : 1;
    });
    html += `<div style="margin-bottom:6px">
      <div style="display:flex;align-items:center;padding:4px 6px;background:${meta.color};color:#FFF;border-radius:3px;cursor:pointer;font-weight:700;font-size:11px;text-transform:uppercase;letter-spacing:0.04em" onclick="this.nextElementSibling.style.display = (this.nextElementSibling.style.display === 'none' ? 'block' : 'none')">
        <span style="flex:1">${escapeHtml(meta.label)}</span>
        <span>${records.length}</span>
      </div>
      <div style="display:${status === 'lead' || status === 'contacted' || status === 'negotiating' || status === 'under_contract' ? 'block' : 'none'};padding:4px 0">
        ${records.map(r => {
          const u = _dueClass(r.next_action_date);
          const ownerStr = escapeHtml((r.owner_name || '?').slice(0, 36));
          const acresStr = r.acres ? Math.round(r.acres) + ' ac' : '';
          const nextStr  = r.next_action ? escapeHtml(r.next_action.slice(0, 50)) : '';
          const dueBadge = u ? `<span style="background:${u.color};color:#FFF;padding:0 4px;border-radius:2px;font-size:9px;font-weight:700;text-transform:uppercase;letter-spacing:0.04em">${u.label}</span>` : '';
          // Action buttons — archive (or restore if already archived) + delete
          const isArchived = !!r.archived;
          const archiveBtn = isArchived
            ? `<button title="Restore — unarchive and show in main pipeline" onclick='event.stopPropagation(); _restoreOutreach(${JSON.stringify(r.prop_id || "")})' style="background:rgba(46,125,50,0.40);color:#FFF;border:0;padding:0 4px;border-radius:2px;font-size:10px;cursor:pointer">↩</button>`
            : `<button title="Archive — hides from main pipeline but keeps history"onclick='event.stopPropagation(); _archiveOutreach(${JSON.stringify(r.prop_id || "")})'style="background:rgba(255,255,255,0.10);color:#FFF;border:0;padding:0 4px;border-radius:2px;font-size:10px;cursor:pointer"></button>`;
          const actions = `<div style="display:flex;gap:4px;align-items:center">
              ${dueBadge}
              ${archiveBtn}
              <button title="Permanently delete this outreach record" onclick='event.stopPropagation(); _deleteOutreach(${JSON.stringify(r.prop_id || "")}, ${JSON.stringify(r.owner_name || "")})' style="background:rgba(198,40,40,0.30);color:#FFF;border:0;padding:0 4px;border-radius:2px;font-size:10px;cursor:pointer">×</button>
            </div>`;
          return `<div style="padding:5px 8px;border-bottom:1px solid rgba(255,255,255,0.05);cursor:pointer" onclick='_jumpToPipelineTract(${JSON.stringify(r.prop_id || "")}, ${JSON.stringify(r.owner_name || "")}, ${r.acres || 0}, ${JSON.stringify(r.county || "")})'>
            <div style="color:#FFF;font-weight:600">${ownerStr}</div>
            <div style="color:#ABB4BD;font-size:10px;display:flex;justify-content:space-between;gap:6px;align-items:center">
              <span>${acresStr} · ${escapeHtml(r.county || '')}</span>
              ${actions}
            </div>
            ${nextStr ? `<div style="color:#9a9a9a;font-size:10px;margin-top:2px">Next: ${nextStr}${r.next_action_date ? ' <span style="color:#6B7B8B">· ' + escapeHtml(r.next_action_date) + '</span>' : ''}</div>` : ''}
          </div>`;
        }).join('')}
      </div>
    </div>`;
  }
  list.innerHTML = html;
}

window._archiveOutreach = async function (propId) {
  if (!confirm('Archive this outreach record? (Hidden from main pipeline; can be restored later.)')) return;
  await fetch(`/api/acq/outreach/${encodeURIComponent(propId)}/archive`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ archived: true }),
  });
  loadPipeline();
  if (typeof renderTractList === 'function') renderTractList();
};

window._restoreOutreach = async function (propId) {
  await fetch(`/api/acq/outreach/${encodeURIComponent(propId)}/archive`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ archived: false }),
  });
  loadPipeline();
  if (typeof renderTractList === 'function') renderTractList();
};

window._deleteOutreach = async function (propId, ownerName) {
  if (!confirm(`Permanently delete the outreach record for ${ownerName || propId}? This removes the status + every log entry. (Cannot be undone.)`)) return;
  await fetch(`/api/acq/outreach/${encodeURIComponent(propId)}`, { method: 'DELETE' });
  loadPipeline();
  if (typeof renderTractList === 'function') renderTractList();
};

window._jumpToPipelineTract = function (propId, ownerName, acres, county) {
  // If this tract is in the current search, focus it + open popup. Otherwise
  // just open the outreach modal so the user can keep working on it.
  let found = false;
  if (window.overlays && overlays.tracts) {
    overlays.tracts.eachLayer(l => {
      if (l.feature && l.feature.properties && l.feature.properties.Prop_ID === propId) {
        try { map.fitBounds(l.getBounds().pad(0.5)); l.openPopup(); } catch {}
        found = true;
      }
    });
  }
  if (!found) {
    showOutreach(propId, ownerName, acres, county);
  }
};

// Pipeline refresh / toggle buttons used to live in the sidebar — those
// controls moved to /pipeline (where the full kanban view is). Guard so we
// don't crash if the elements aren't present.
const _btnRefreshPipeline = document.getElementById('btn-refresh-pipeline');
if (_btnRefreshPipeline) _btnRefreshPipeline.addEventListener('click', loadPipeline);
const _btnToggleArchived = document.getElementById('btn-toggle-archived');
if (_btnToggleArchived) _btnToggleArchived.addEventListener('click', () => {
  window._showArchived = !window._showArchived;
  loadPipeline();
});

// Status pill color lookup (matches the Python OUTREACH_STATUSES table)
const OUTREACH_COLORS = {
  lead:           '#6B7B8B',  contacted:      '#1976D2',
  negotiating:    '#F25929',  under_contract: '#FFA000',
  closed:         '#2E7D32',  lost:           '#9E9E9E',
  passed:         '#5D4037',
};
function outreachPill(status) {
  if (!status) return '';
  const label = { lead:'Lead', contacted:'Contacted', negotiating:'Negotiating',
                  under_contract:'Under contract', closed:'Closed', lost:'Lost', passed:'Passed' }[status] || status;
  const color = OUTREACH_COLORS[status] || '#6B7B8B';
  return `<span style="background:${color};color:#FFF;padding:1px 6px;border-radius:3px;font-size:9px;font-weight:700;margin-left:4px;text-transform:uppercase;letter-spacing:0.04em">${label}</span>`;
}

// -----------------------------------------------------------------------
// Owner research dossier — "show me everything this owner has in our cache"
// -----------------------------------------------------------------------
// Every tract the owner search matched, drawn on the map so the result is
// spatial rather than just a list. Cleared when the panel closes.
const ownerMatchOverlay = L.layerGroup().addTo(map);
window._ownerMatches = [];

function _clearOwnerMatches() {
  try { ownerMatchOverlay.clearLayers(); } catch (e) {}
  window._ownerMatches = [];
}

function _drawOwnerMatches(parcels, opts) {
  _clearOwnerMatches();
  const options = opts || {};
  const bounds = L.latLngBounds([]);
  (parcels || []).forEach((p, i) => {
    if (p.lat == null || p.lon == null) {
      window._ownerMatches.push({ idx: i, marker: null, parcel: p });
      return;
    }
    const m = L.circleMarker([p.lat, p.lon], {
      radius: 7, color: '#FFF', weight: 2, fillColor: '#13344E', fillOpacity: 0.95,
    }).addTo(ownerMatchOverlay);
    m.bindPopup(
      `<b>${escapeHtml(p.owner_name || '')}</b><br>` +
      `${Number(p.acres || 0).toLocaleString('en-US')} ac · ${escapeHtml(p.county || '')}<br>` +
      `Prop ID: ${escapeHtml(p.prop_id || '')}` +
      (p.match_pass && p.match_pass !== 'exact'
        ? `<br><span style="color:#856404;font-size:11px">matched: ${escapeHtml(p.match_pass)}` +
          (p.match_score != null ? ` (${Math.round(p.match_score * 100)}%)` : '') + `</span>`
        : '') +
      _ownerMatchActionsHtml(i));
    // Draw the parcel outline too when we know its extent.
    if (p.bounds && p.bounds.length === 4) {
      const b = L.latLngBounds([[p.bounds[1], p.bounds[0]], [p.bounds[3], p.bounds[2]]]);
      L.rectangle(b, { color: '#13344E', weight: 2, fillOpacity: 0.08, dashArray: '4,3' })
        .addTo(ownerMatchOverlay);
      bounds.extend(b);
    } else {
      bounds.extend([p.lat, p.lon]);
    }
    window._ownerMatches.push({ idx: i, marker: m, parcel: p });
  });
  if (bounds.isValid() && !options.skipFit) {
    _fitBoundsKeepingDockClear(bounds.pad(0.15));
  }
  return bounds;
}

window.zoomOwnerMatch = function (i) {
  const hit = (window._ownerMatches || []).find(x => x.idx === i);
  if (!hit) return;
  const p = hit.parcel;
  if (p.bounds && p.bounds.length === 4) {
    try {
      _fitBoundsKeepingDockClear(
        L.latLngBounds([[p.bounds[1], p.bounds[0]], [p.bounds[3], p.bounds[2]]]).pad(0.6),
        { maxZoom: 17 }
      );
    } catch (e) {}
  } else if (p.lat != null) {
    map.setView([p.lat, p.lon], 16);
  }
  try { if (hit.marker) hit.marker.openPopup(); } catch (e) {}
  document.querySelectorAll('#dossier-body .owner-row').forEach(el => el.classList.remove('active'));
  const row = document.querySelector(`#dossier-body .owner-row[data-i="${i}"]`);
  if (row) row.classList.add('active');
};

function _ownerMatchAt(i) {
  const hit = (window._ownerMatches || []).find(x => x.idx === i);
  return hit ? hit.parcel : null;
}

function _ownerParcelCountySubset(p) {
  return p ? { _county: p.county || '', _hcad_owner_verified: false, _mcad_owner_verified: false } : {};
}

window.showOwnerMatchDetail = function (i) {
  const p = _ownerMatchAt(i);
  if (!p || !p.prop_id) return;
  showParcelDetail(p.prop_id, _ownerParcelCountySubset(p));
};

window.openOwnerMatchTractPage = function (i) {
  const p = _ownerMatchAt(i);
  if (!p || !p.prop_id) return;
  const q = p.county ? `?county=${encodeURIComponent(p.county)}` : '';
  window.open(`/acquisitions/tract/${encodeURIComponent(p.prop_id)}${q}`, '_blank');
};

window.openOwnerMatchTractSheet = function (i) {
  const p = _ownerMatchAt(i);
  if (!p || !p.prop_id) return;
  window.open(`/api/acq/tract-sheet/${encodeURIComponent(p.prop_id)}`, '_blank');
};

window.showOwnerMatchOutreach = function (i) {
  const p = _ownerMatchAt(i);
  if (!p || !p.prop_id) return;
  showOutreach(p.prop_id, p.owner_name || '', p.acres || 0, p.county || '');
};

window.saveOwnerMatchTract = function (i) {
  const p = _ownerMatchAt(i);
  if (!p || !p.prop_id) return;
  saveTract(p.prop_id, p.owner_name || '', p.acres || 0, p.county || '', p.lat, p.lon, '');
};

// One tract from the owner panel, straight into a quick analysis.
window.analyzeOwnerMatchTract = function (i) {
  const p = _ownerMatchAt(i);
  if (!p || !p.prop_id) return;
  if (!p.geometry) { setStatus('That tract has no boundary stored, so it cannot be analysed.', true); return; }
  quickAnalyzeTract(p.prop_id, p.owner_name || '', p.acres || 0,
                    p.county || '', JSON.stringify(p.geometry));
};

// Every tract the owner holds, as a single project. This is the reason the
// owner panel is useful: 969 acres across four parcels is one deal, and
// analysing them one at a time answers the wrong question.
window.analyzeOwnerHolding = async function () {
  const d = window._ownerDossier;
  const parcels = (d && d.parcels || []).filter(p => p.geometry && p.prop_id);
  if (!parcels.length) { setStatus('No tracts with boundaries to analyse.', true); return; }
  const acres = parcels.reduce((a, p) => a + (+p.acres || 0), 0);
  const name = `${(d.query || 'Owner').slice(0, 40)} — ${parcels.length} tract${parcels.length === 1 ? '' : 's'} (${Math.round(acres)} ac)`;
  setStatus(`Building a project from <b>${parcels.length}</b> tracts · ${Math.round(acres)} ac…`);
  try {
    const r = await fetch('/api/acq/projects', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        name,
        is_user_project: true,
        tracts: parcels.map(p => ({
          prop_id: String(p.prop_id), owner_name: p.owner_name || '',
          acres: +p.acres || 0, county: p.county || '', geometry: p.geometry,
        })),
      }),
    });
    const j = await r.json();
    if (!r.ok || j.error) throw new Error(j.error || `HTTP ${r.status}`);
    const pid = (j.project || j).id;
    if (!pid) throw new Error('no project id returned');
    window.open(`/acquisitions/project/${encodeURIComponent(pid)}`, '_blank');
    setStatus(`Project created — ${parcels.length} tracts, ${Math.round(acres)} ac.`);
  } catch (e) {
    setStatus(`Could not build the project: ${escapeHtml(e.message)}`, true);
  }
};

function _ownerMatchActionsHtml(i) {
  return `
    <div class="owner-row-actions">
      <button type="button" onclick="event.stopPropagation();showOwnerMatchDetail(${i})">Full details</button>
      <button type="button" onclick="event.stopPropagation();openOwnerMatchTractPage(${i})">Tract page</button>
      <button type="button" onclick="event.stopPropagation();openOwnerMatchTractSheet(${i})">Tract sheet</button>
      <button type="button" onclick="event.stopPropagation();showOwnerMatchOutreach(${i})">Outreach</button>
      <button type="button" onclick="event.stopPropagation();analyzeOwnerMatchTract(${i})"
              style="background:var(--ember-orange)">Acq Analysis</button>
      <button type="button" onclick="event.stopPropagation();saveOwnerMatchTract(${i})">Save</button>
    </div>`;
}

// `typedSearch` marks a name a person typed into the entity box rather than
// one carried from a parcel popup. A typed single token should widen -
// see the `typed` flag on /api/owner-dossier.
window.showOwnerDossier = async function (ownerName, focusPropId, focusAcres, focusGeomJsonStr, typedSearch) {
  const panel = document.getElementById('dossier-panel');
  const sub   = document.getElementById('dossier-sub');
  const body  = document.getElementById('dossier-body');
  const title = document.getElementById('dossier-title');
  if (!ownerName || !ownerName.trim()) {
    setStatus('No owner name to look up.', true);
    return;
  }
  try { map.closePopup(); } catch (e) {}
  title.textContent = ownerName;
  sub.textContent = 'Searching all cached counties…';
  body.innerHTML = '<div style="text-align:center;padding:30px;color:#6B7B8B">Searching…<br>' +
                   '<span style="font-size:11px">Close name matches take a few seconds.</span></div>';
  panel.classList.add('show');
  let focusGeom = null;
  if (focusGeomJsonStr) {
    try {
      const parsed = typeof focusGeomJsonStr === 'string' ? JSON.parse(focusGeomJsonStr) : focusGeomJsonStr;
      if (parsed && parsed.type) focusGeom = parsed;
    } catch (e) {}
  }
  if (focusGeom) window.setTractFillVisible(false);
  const focusLayer = focusGeom
    ? _focusTractOnMap(focusGeom, { Prop_ID: focusPropId, OWNER_NAME: ownerName, Acres: focusAcres }, 'owner')
    : null;
  if (!focusLayer) _hideMatchingTractsForFocus();
  setTimeout(() => { try { map.invalidateSize(); } catch (e) {} }, 60);
  try {
    // geometry=1: the Acq Analysis actions build a project from these
    // parcels, and a project without boundaries cannot be analysed.
    const r = await fetch(`/api/acq/owner-dossier?name=${encodeURIComponent(ownerName)}&geometry=1`
                          + (typedSearch ? '&typed=1' : ''));
    const d = await r.json();
    if (!r.ok || d.error) throw new Error(d.error || `HTTP ${r.status}`);
    window._ownerDossier = d;
    body.innerHTML = _renderDossier(d);
    _drawOwnerMatches(d.parcels || [], { skipFit: !!focusLayer });
    if (focusLayer) {
      try { focusLayer.bringToFront(); } catch (e) {}
      try { _fitBoundsKeepingDockClear(focusLayer.getBounds().pad(0.25)); } catch (e) {}
    }
    const nVar = (d.variants || []).length;
    sub.textContent = `${d.total_count} tract${d.total_count === 1 ? '' : 's'} · `
      + `${Number(d.total_acres || 0).toLocaleString('en-US')} ac · `
      + `${nVar} name spelling${nVar === 1 ? '' : 's'}`
      + (d.truncated ? ' · capped at 500' : '');
  } catch (e) {
    body.innerHTML = `<div style="padding:20px;color:#c62828"><b>Lookup failed:</b> ${escapeHtml(e.message)}</div>`;
  }
};

document.getElementById('dossier-close').addEventListener('click', () => {
  document.getElementById('dossier-panel').classList.remove('show');
  _clearOwnerMatches();
  _restoreMatchingTracts(false);
  setTimeout(() => { try { map.invalidateSize(); } catch (e) {} }, 60);
});
document.getElementById('dossier-analyze').addEventListener('click', analyzeOwnerHolding);
document.getElementById('dossier-zoom').addEventListener('click', () => {
  const d = window._ownerDossier;
  if (d && d.parcels) _drawOwnerMatches(d.parcels);
});
document.getElementById('dossier-show-results').addEventListener('click', () => _restoreMatchingTracts(true));

function _renderDossier(d) {
  if (!d || d.total_count === 0) {
    return `<div style="padding:26px 10px;text-align:center;color:#6B7B8B">
      No parcels found for this owner in the cached counties.
      <br><span style="font-size:11px">Harris, Fort Bend, Montgomery, Brazoria, Galveston, Liberty,
      Waller, Chambers, Austin, San Jacinto, Grimes, Walker, Madison, Washington.</span>
    </div>`;
  }

  const summary = `
    <div style="display:flex;gap:8px;margin-bottom:12px">
      <div style="flex:1;background:var(--gray-50);border-radius:6px;padding:9px">
        <div style="font-size:9px;color:#6B7B8B;text-transform:uppercase;letter-spacing:0.06em;font-weight:700">Tracts</div>
        <div style="font-size:19px;color:#13344E;font-weight:700">${d.total_count}</div>
      </div>
      <div style="flex:1;background:var(--gray-50);border-radius:6px;padding:9px">
        <div style="font-size:9px;color:#6B7B8B;text-transform:uppercase;letter-spacing:0.06em;font-weight:700">Acres</div>
        <div style="font-size:19px;color:#13344E;font-weight:700">${Number(d.total_acres).toLocaleString('en-US')}</div>
      </div>
      <div style="flex:1;background:#FFF4ED;border-left:3px solid #F25929;border-radius:6px;padding:9px">
        <div style="font-size:9px;color:#6B7B8B;text-transform:uppercase;letter-spacing:0.06em;font-weight:700">Counties</div>
        <div style="font-size:19px;color:#F25929;font-weight:700">${(d.by_county || []).length}</div>
      </div>
    </div>`;

  // Name spellings that matched. This is the whole point of the fuzzy pass —
  // showing which variants were pulled in, rather than blending them silently.
  const vars = d.variants || [];
  const variantBlock = vars.length > 1 ? `
    <h4 style="margin:4px 0 6px;color:#F25929;text-transform:uppercase;font-size:10px;letter-spacing:0.06em">
      Name spellings matched (${vars.length})
    </h4>
    <div style="border:1px solid var(--gray-200);border-radius:6px;margin-bottom:12px;max-height:130px;overflow-y:auto">
      ${vars.map(v => `
        <div style="display:flex;justify-content:space-between;gap:8px;padding:5px 8px;border-bottom:1px solid var(--gray-100);font-size:12px">
          <span style="overflow-wrap:anywhere">${escapeHtml(v.owner_name || '')}</span>
          <span style="color:#6B7B8B;white-space:nowrap">${v.count} · ${Number(v.acres).toLocaleString('en-US')} ac</span>
        </div>`).join('')}
    </div>` : '';

  const byCounty = (d.by_county || []).map(c => `
    <tr style="border-top:1px solid var(--gray-100)">
      <td style="padding:4px 10px 4px 0">${escapeHtml(c.county)}</td>
      <td style="padding:4px 10px 4px 0;text-align:right">${c.count}</td>
      <td style="padding:4px 0;text-align:right">${Number(c.acres).toLocaleString('en-US')}</td>
    </tr>`).join('');

  const rows = (d.parcels || []).map((p, i) => {
    const badge = (p.match_pass && p.match_pass !== 'exact')
      ? `<span title="matched by ${escapeHtml(p.match_pass)}" style="color:#856404;font-size:9px;font-weight:700;margin-left:4px">
           ${escapeHtml(String(p.match_pass).toUpperCase())}</span>` : '';
    return `
      <div class="owner-row" data-i="${i}" onclick="zoomOwnerMatch(${i})">
        <div style="display:flex;justify-content:space-between;gap:8px">
          <span style="font-weight:600;font-size:12px;overflow-wrap:anywhere">${escapeHtml(p.owner_name || '')}${badge}</span>
          <span style="white-space:nowrap;font-size:12px;font-weight:700;color:#13344E">${Number(p.acres || 0).toLocaleString('en-US')} ac</span>
        </div>
        <div style="font-size:10px;color:#6B7B8B;margin-top:1px">
          ${escapeHtml(p.county || '')} · Prop ${escapeHtml(p.prop_id || '')}
          ${p.situs_addr ? ' · ' + escapeHtml(p.situs_addr) : ''}
        </div>
        ${_ownerMatchActionsHtml(i)}
      </div>`;
  }).join('');

  return `
    ${summary}
    ${variantBlock}
    <h4 style="margin:4px 0 6px;color:#F25929;text-transform:uppercase;font-size:10px;letter-spacing:0.06em">By county</h4>
    <table style="width:100%;border-collapse:collapse;font-size:12px;margin-bottom:12px">
      <thead><tr style="color:#6B7B8B;font-size:9px;text-transform:uppercase;letter-spacing:0.06em">
        <th style="text-align:left;padding:0 10px 3px 0">County</th>
        <th style="text-align:right;padding:0 10px 3px 0">Tracts</th>
        <th style="text-align:right;padding:0 0 3px 0">Acres</th>
      </tr></thead>
      ${byCounty}
    </table>
    <h4 style="margin:4px 0 6px;color:#F25929;text-transform:uppercase;font-size:10px;letter-spacing:0.06em">
      Tracts <span style="color:#9a9a9a;font-weight:400;text-transform:none;letter-spacing:0">— click row to zoom, or use tract actions</span>
    </h4>
    <div style="border:1px solid var(--gray-200);border-radius:6px;overflow:hidden">${rows}</div>
    ${d.truncated ? '<div style="font-size:11px;color:#856404;margin-top:8px;font-style:italic">Capped at 500 tracts. Narrow the name to see fewer.</div>' : ''}`;
}


// -----------------------------------------------------------------------
// Elevation profile LINE — user draws a polyline on the map, server samples
// USGS 3DEP along it, modal shows an SVG profile chart.
// -----------------------------------------------------------------------

// Map control button (top-right) to start a profile-line draw
const ElevLineControl = L.Control.extend({
  // topleft, directly under Leaflet.draw's shape and measure tools - drawing a
  // profile line is the same kind of action, so it belongs in the same cluster
  // rather than off on the opposite side of the map.
  options: { position: 'topleft' },
  onAdd: function() {
    const div = L.DomUtil.create('div', 'leaflet-bar leaflet-control');
    const btn = L.DomUtil.create('a', '', div);
    btn.href = '#';
    btn.title = 'Draw a line to see the elevation profile along it (water-flow analysis)';
    btn.innerHTML = '&#9585;';   // was empty, so the control rendered as a blank box
    btn.style.cssText = 'background:#FFF;width:30px;height:30px;line-height:30px;text-align:center;font-size:18px;text-decoration:none;color:#13344E';
    L.DomEvent.on(btn, 'click', L.DomEvent.preventDefault).on(btn, 'click', startElevLineDraw);
    return div;
  },
});
map.addControl(new ElevLineControl());

let _elevLineLayer = null;   // the drawn line overlay (so we can replace on next draw)

// In-app corridor drawing — Leaflet.draw polyline → buffer to corridor polygon
// → save as a "kmz_corridor" search (same shape as KMZ-imported corridors so the
// rest of the app — corridors page, all-corridors search, rebuffer — just works).
async function startCorridorDraw() {
  if (!L.Draw || !L.Draw.Polyline) {
    setStatus('Leaflet.draw not loaded — refresh the page.', true);
    return;
  }
  setStatus('Click along the corridor centerline · double-click to finish. We\'ll prompt for name + buffer when you\'re done.');
  const drawer = new L.Draw.Polyline(map, {
    shapeOptions: { color: '#13344E', weight: 4, opacity: 0.95, dashArray: '4,4' },
    showLength: true,
    metric: false,
    feet: false,   // miles for corridors
  });
  drawer.enable();

  const onCreated = async (e) => {
    if (e.layerType !== 'polyline') return;
    map.off(L.Draw.Event.CREATED, onCreated);
    const latlngs = e.layer.getLatLngs();
    if (!latlngs || latlngs.length < 2) {
      setStatus('Need at least 2 points for a corridor.', true);
      return;
    }
    // Show the line temporarily while we prompt
    e.layer.addTo(map);

    const name = prompt('Name for this corridor (e.g. "FM 1488 west"):', '');
    if (!name || !name.trim()) {
      map.removeLayer(e.layer);
      setStatus('Corridor draw cancelled.');
      return;
    }
    const bufStr = prompt('Buffer half-width in miles (default 2):', '2');
    if (bufStr === null) {
      map.removeLayer(e.layer);
      setStatus('Corridor draw cancelled.');
      return;
    }
    const buffer_miles = parseFloat(bufStr);
    if (!isFinite(buffer_miles) || buffer_miles <= 0 || buffer_miles > 50) {
      map.removeLayer(e.layer);
      setStatus('Buffer must be a number between 0 and 50.', true);
      return;
    }

    const centerline = latlngs.map(p => [p.lng, p.lat]);
    setStatus(`Saving corridor "${escapeHtml(name)}" at ±${buffer_miles}mi…`);
    try {
      const r = await fetch('/api/acq/draw-corridor', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ label: name.trim(), buffer_miles, centerline }),
      });
      const d = await r.json();
      if (!r.ok || d.error) throw new Error(d.error || `HTTP ${r.status}`);
      map.removeLayer(e.layer);
      // Reload the corridors list (sidebar saved searches + corridor tabs)
      if (typeof loadSavedSearches === 'function') loadSavedSearches();
      setStatus(`Corridor "<b>${escapeHtml(d.label)}</b>" saved. Go to <a href="/acquisitions/corridors">Corridors</a> to use it, or check the saved searches list.`);
    } catch (err) {
      map.removeLayer(e.layer);
      setStatus(`Save failed: ${escapeHtml(err.message)}`, true);
    }
  };
  map.on(L.Draw.Event.CREATED, onCreated);
}

document.getElementById('btn-draw-corridor').addEventListener('click', startCorridorDraw);

function startElevLineDraw() {
  if (!L.Draw || !L.Draw.Polyline) {
    setStatus('Leaflet.draw not loaded — refresh the page.', true);
    return;
  }
  setStatus('Click points along your line · double-click to finish. Drawing in orange.');
  const drawer = new L.Draw.Polyline(map, {
    shapeOptions: { color: '#F25929', weight: 4, opacity: 0.95, dashArray: '8,4' },
    showLength: true,
    metric: false,
    feet: true,
  });
  drawer.enable();
  // One-time listener so we don't intercept other draws (shape-save etc.)
  const onCreated = (e) => {
    if (e.layerType !== 'polyline') return;
    map.off(L.Draw.Event.CREATED, onCreated);
    // Replace any prior elev-line on the map
    if (_elevLineLayer) { try { map.removeLayer(_elevLineLayer); } catch {} }
    _elevLineLayer = e.layer;
    e.layer.addTo(map);
    const latlngs = e.layer.getLatLngs();
    showLineElevationProfile(latlngs);
  };
  map.on(L.Draw.Event.CREATED, onCreated);
}

// Map marker that tracks where you're hovering on the chart
let _elevHoverMarker = null;

async function showLineElevationProfile(latlngs) {
  const panel = document.getElementById('elev-line-panel');
  const body  = document.getElementById('elev-line-body');
  const sub   = document.getElementById('elev-line-sub');
  sub.textContent = `${latlngs.length} vertices · sampling 60 points…`;
  body.innerHTML = '<div style="text-align:center;padding:30px;color:#6B7B8B">Sampling USGS 3DEP elevation along the line…<br><span style="font-size:11px;color:#9a9a9a">(~5–10 seconds)</span></div>';
  panel.style.display = 'block';
  window.setTractFillVisible(false);
  _hideMatchingTractsForFocus();
  overlays.focusTract.clearLayers();
  if (_elevLineLayer) {
    setTimeout(() => {
      try { _fitBoundsKeepingDockClear(_elevLineLayer.getBounds().pad(0.2)); } catch (e) {}
    }, 80);
  }
  try {
    const line = latlngs.map(p => [p.lat, p.lng]);
    const r = await fetch('/api/acq/elevation-line', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ line, samples: 60 }),
    });
    const d = await r.json();
    if (!r.ok || d.error) throw new Error(d.error || `HTTP ${r.status}`);
    sub.textContent = `${(d.total_length_mi).toFixed(2)} mi · ${Math.round(d.total_length_ft).toLocaleString('en-US')} ft · range ${d.range_ft} ft · ${d.drop_ft > 0 ? '↓' : '↑'} ${Math.abs(d.drop_ft)} ft end-to-end`;
    body.innerHTML = _renderLineProfile(d);
    _wireChartHover(d);
  } catch (e) {
    body.innerHTML = `<div style="padding:20px;color:#c62828"><b>Couldn't load:</b> ${escapeHtml(e.message)}</div>`;
  }
}

// Wire up: hover the chart → move marker on map line + show tooltip at cursor.
function _wireChartHover(d) {
  const svg = document.getElementById('elev-line-svg');
  if (!svg) return;
  const samples = (d.samples || []).filter(s => s.elev_ft != null);
  if (samples.length < 2) return;
  const guide = document.getElementById('elev-line-guide');
  const dot   = document.getElementById('elev-line-dot');
  const tip   = document.getElementById('elev-line-tip');
  if (!guide || !tip) return;

  function findSample(svgX) {
    // svgX is in SVG userspace coords. Find sample whose plot-x is closest.
    const PAD_L = 50, PAD_R = 14;
    const W = 680;
    const plotW = W - PAD_L - PAD_R;
    const maxD = samples[samples.length - 1].distance_ft;
    const dist = Math.max(0, Math.min(maxD, ((svgX - PAD_L) / plotW) * maxD));
    // Find nearest sample
    let best = samples[0], bestDelta = Infinity;
    for (const s of samples) {
      const delta = Math.abs(s.distance_ft - dist);
      if (delta < bestDelta) { bestDelta = delta; best = s; }
    }
    return best;
  }

  function setHover(svgX, evt) {
    const s = findSample(svgX);
    const PAD_L = 50, PAD_R = 14, PAD_T = 10, PAD_B = 30;
    const W = 680, H = 240;
    const plotW = W - PAD_L - PAD_R;
    const plotH = H - PAD_T - PAD_B;
    const maxD = samples[samples.length - 1].distance_ft;
    const xPx = PAD_L + (s.distance_ft / maxD) * plotW;
    const padE = (d.max_ft - d.min_ft) * 0.05;
    const yMin = d.min_ft - padE, yMax = d.max_ft + padE;
    const yPx = PAD_T + (1 - (s.elev_ft - yMin) / (yMax - yMin)) * plotH;
    // Move vertical guide + dot
    guide.setAttribute('x1', xPx); guide.setAttribute('x2', xPx);
    guide.setAttribute('y1', PAD_T); guide.setAttribute('y2', PAD_T + plotH);
    guide.style.display = '';
    dot.setAttribute('cx', xPx); dot.setAttribute('cy', yPx);
    dot.style.display = '';
    // Tooltip
    tip.innerHTML = `<b>${s.elev_ft.toFixed(1)} ft</b> · ${Math.round(s.distance_ft).toLocaleString('en-US')} ft from start`;
    tip.style.display = 'block';
    // Place tooltip near cursor inside the panel
    if (evt && evt.clientX) {
      const panelRect = document.getElementById('elev-line-panel').getBoundingClientRect();
      tip.style.left = (evt.clientX - panelRect.left + 12) + 'px';
      tip.style.top  = (evt.clientY - panelRect.top - 28) + 'px';
    }
    // Move marker on the map at the sample's lat/lon
    if (!_elevHoverMarker) {
      _elevHoverMarker = L.circleMarker([s.lat, s.lon], {
        radius: 9, color: '#FFF', weight: 2, fillColor: '#F25929', fillOpacity: 1,
      }).addTo(map);
    } else {
      _elevHoverMarker.setLatLng([s.lat, s.lon]);
    }
    _elevHoverMarker.bindTooltip(`${s.elev_ft.toFixed(1)} ft`, { permanent: false, direction: 'top' });
  }

  svg.addEventListener('mousemove', (e) => {
    const rect = svg.getBoundingClientRect();
    const W = 680;   // SVG viewBox width
    const svgX = ((e.clientX - rect.left) / rect.width) * W;
    setHover(svgX, e);
  });
  svg.addEventListener('mouseleave', () => {
    guide.style.display = 'none';
    dot.style.display = 'none';
    tip.style.display = 'none';
    if (_elevHoverMarker) { try { map.removeLayer(_elevHoverMarker); } catch {} _elevHoverMarker = null; }
  });
}

function _renderLineProfile(d) {
  const samples = (d.samples || []).filter(s => s.elev_ft != null);
  if (samples.length < 2) {
    return '<div style="padding:20px;color:#c62828">Not enough valid samples returned.</div>';
  }
  // SVG line chart — x = distance, y = elevation
  const W = 680, H = 240;
  const PAD_L = 50, PAD_R = 14, PAD_T = 10, PAD_B = 30;
  const plotW = W - PAD_L - PAD_R;
  const plotH = H - PAD_T - PAD_B;
  const maxD = samples[samples.length - 1].distance_ft;
  const minE = d.min_ft;
  const maxE = d.max_ft;
  const rangeE = Math.max(1, maxE - minE);
  // pad range +/- 5% for headroom
  const padE = rangeE * 0.05;
  const yMin = minE - padE;
  const yMax = maxE + padE;
  function xFor(distFt) { return PAD_L + (distFt / maxD) * plotW; }
  function yFor(elevFt) { return PAD_T + (1 - (elevFt - yMin) / (yMax - yMin)) * plotH; }

  // Build polyline + filled area below it (terrain look)
  const points = samples.map(s => `${xFor(s.distance_ft).toFixed(1)},${yFor(s.elev_ft).toFixed(1)}`).join(' ');
  const areaPath = `M ${PAD_L},${PAD_T + plotH} L ` + points + ` L ${PAD_L + plotW},${PAD_T + plotH} Z`;
  const linePath = 'M ' + samples.map(s => `${xFor(s.distance_ft).toFixed(1)},${yFor(s.elev_ft).toFixed(1)}`).join(' L ');

  // Y-axis labels (5 ticks)
  let yTicks = '';
  for (let i = 0; i <= 4; i++) {
    const e = yMin + (yMax - yMin) * (i / 4);
    const y = yFor(e);
    yTicks += `<line x1="${PAD_L}" y1="${y}" x2="${PAD_L + plotW}" y2="${y}" stroke="#E8EBED" stroke-width="0.5"/>`;
    yTicks += `<text x="${PAD_L - 4}" y="${y + 3}" text-anchor="end" font-size="10" fill="#6B7B8B" font-family="Helvetica">${e.toFixed(0)} ft</text>`;
  }
  // X-axis labels (5 ticks, in feet — or miles if line > 1 mi)
  let xTicks = '';
  const useMiles = maxD > 5280;
  for (let i = 0; i <= 4; i++) {
    const d_ft = maxD * (i / 4);
    const x = xFor(d_ft);
    const lbl = useMiles ? (d_ft / 5280).toFixed(2) + ' mi' : Math.round(d_ft).toLocaleString('en-US') + ' ft';
    xTicks += `<text x="${x}" y="${PAD_T + plotH + 14}" text-anchor="middle" font-size="10" fill="#6B7B8B" font-family="Helvetica">${lbl}</text>`;
  }

  const dropDir = d.drop_ft > 0 ? 'drops downhill' : 'rises uphill';
  const dropFt = Math.abs(d.drop_ft);
  return `
    <div style="position:relative">
      <svg id="elev-line-svg" viewBox="0 0 ${W} ${H}" style="width:100%;height:auto;max-height:200px;background:#FAFAFA;border-radius:4px;cursor:crosshair">
        ${yTicks}
        <path d="${areaPath}" fill="#F25929" fill-opacity="0.15" stroke="none"/>
        <path d="${linePath}" stroke="#F25929" stroke-width="2.5" fill="none" stroke-linecap="round" stroke-linejoin="round"/>
        ${xTicks}
        <line id="elev-line-guide" stroke="#13344E" stroke-width="1" stroke-dasharray="3,2" style="display:none" pointer-events="none"/>
        <circle id="elev-line-dot" r="5" fill="#13344E" stroke="#FFF" stroke-width="2" style="display:none" pointer-events="none"/>
      </svg>
      <div id="elev-line-tip" style="display:none;position:absolute;background:#13344E;color:#FFF;padding:4px 8px;border-radius:4px;font-size:11px;pointer-events:none;box-shadow:0 2px 6px rgba(0,0,0,0.30);white-space:nowrap;z-index:10"></div>
    </div>
    <div style="display:flex;gap:14px;align-items:flex-start;margin-top:10px;flex-wrap:wrap;font-size:12px">
      <div style="background:#F1F2F3;border-radius:6px;padding:8px 12px;flex:1;min-width:140px">
        <div style="font-size:10px;color:#6B7B8B;text-transform:uppercase;letter-spacing:0.06em;font-weight:700">Range</div>
        <div style="font-weight:700;color:#13344E">${d.min_ft} – ${d.max_ft} ft <span style="color:#6B7B8B;font-weight:400">(${d.range_ft} ft)</span></div>
      </div>
      <div style="background:#FFF4ED;border-left:3px solid #F25929;border-radius:6px;padding:8px 12px;flex:1;min-width:160px">
        <div style="font-size:10px;color:#6B7B8B;text-transform:uppercase;letter-spacing:0.06em;font-weight:700">End-to-end</div>
        <div style="font-weight:700;color:#F25929">${dropFt} ft ${dropDir}</div>
        <div style="color:#13344E">${(d.total_length_mi).toFixed(2)} mi · grade ~${((dropFt / d.total_length_ft) * 100).toFixed(2)}%</div>
      </div>
      <div style="color:#6B7B8B;font-size:11px;align-self:center">Hover the chart — orange dot tracks the line on the map.</div>
    </div>`;
}

document.getElementById('elev-line-close').addEventListener('click', () => {
  document.getElementById('elev-line-panel').style.display = 'none';
  if (_elevHoverMarker) { try { map.removeLayer(_elevHoverMarker); } catch {} _elevHoverMarker = null; }
  // Also clear the drawn line so the map is clean again
  if (_elevLineLayer) { try { map.removeLayer(_elevLineLayer); } catch {} _elevLineLayer = null; }
  _restoreMatchingTracts(false);
});
document.getElementById('elev-line-show-results').addEventListener('click', () => _restoreMatchingTracts(true));


// -----------------------------------------------------------------------
// Elevation profile — samples USGS 3DEP elevation across the tract and
// renders a 9x9 heatmap + drainage-direction arrow + min/max/range stats.
// "Drainage direction" = compass bearing water flows (downhill from gradient).
// -----------------------------------------------------------------------
// Layer group for the on-map elevation overlay (colored sample-point dots
// dropped at each grid cell's lat/lon, sitting on top of the tract polygon).
const elevationOverlayGroup = L.layerGroup().addTo(map);

// -----------------------------------------------------------------------
// Sidebar collapse toggle — lets the user reclaim screen space when they
// want a wider map. Click the chevron at the sidebar's right edge to hide;
// click again to bring it back. State persists in localStorage so the
// preference sticks across reloads.
// -----------------------------------------------------------------------
(function () {
  const sidebar = document.getElementById('acq-sidebar');
  const toggle  = document.getElementById('sidebar-toggle');
  function applyState(collapsed) {
    // Clear focus-mode classes defensively: if anything ever leaves them set,
    // the chevron is the user's only way out and it must always work.
    sidebar.classList.remove('expanded-hidden');
    document.body.classList.remove('expanded-mode');
    if (collapsed) {
      sidebar.classList.add('collapsed');
      document.body.classList.add('sidebar-collapsed');
      toggle.textContent = '▶';
      toggle.title = 'Show sidebar';
    } else {
      sidebar.classList.remove('collapsed');
      document.body.classList.remove('sidebar-collapsed');
      toggle.textContent = '◀';
      toggle.title = 'Hide sidebar';
    }
    // Give Leaflet a tick to re-measure the map size
    setTimeout(() => { try { map.invalidateSize(); } catch {} }, 220);
  }
  // Initial state from localStorage
  applyState(localStorage.getItem('sidebarCollapsed') === '1');
  toggle.addEventListener('click', () => {
    const next = !sidebar.classList.contains('collapsed');
    localStorage.setItem('sidebarCollapsed', next ? '1' : '0');
    applyState(next);
  });
})();

// Expand-tract focus mode was removed: it hid the sidebar and every overlay
// to show one boundary, which duplicated what zooming to the tract already
// does, and its exit path could leave the sidebar permanently hidden.



window.showElevationProfile = async function (propId, ownerName, acres, geomJsonStr) {
  const dock = document.getElementById('elev-dock');
  const ownerEl = document.getElementById('elev-owner');
  const sub  = document.getElementById('elev-sub');
  const body = document.getElementById('elev-body');
  ownerEl.textContent = ownerName || 'Tract';
  sub.textContent = `${acres || '?'} ac · Prop ID ${propId}`;
  body.innerHTML = '<div style="text-align:center;padding:30px 0;color:#7A828D">Sampling USGS 3DEP elevation…<br><span style="font-size:11px;color:#A5ADB7">~10-20 seconds (dense grid + contour extraction)</span></div>';
  dock.style.display = 'flex';
  elevationOverlayGroup.clearLayers();

  let geom = null;
  try { geom = JSON.parse(geomJsonStr); } catch { geom = geomJsonStr; }
  window.setTractFillVisible(false);
  const focusLayer = (geom && geom.type)
    ? _focusTractOnMap(geom, { Prop_ID: propId, OWNER_NAME: ownerName, Acres: acres }, 'elevation')
    : null;
  if (!focusLayer) _hideMatchingTractsForFocus();
  try {
    const r = await fetch('/api/acq/elevation-profile', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ geometry: geom }),
    });
    const d = await r.json();
    if (!r.ok || d.error) throw new Error(d.error || `HTTP ${r.status}`);
    body.innerHTML = _renderElevationDock(d);
    _drawElevationOnMap(d);
    if (focusLayer) {
      try { _fitBoundsKeepingDockClear(focusLayer.getBounds().pad(0.25)); } catch (e) {}
    }
    // Wire the toggle (rendered inside the dock body)
    const tog = document.getElementById('elev-show-contours');
    if (tog) tog.addEventListener('change', () => _redrawElevationContours(d, tog.checked));
  } catch (e) {
    body.innerHTML = `<div style="padding:20px;color:#c62828"><b>Couldn't load elevation:</b> ${escapeHtml(e.message)}</div>`;
  }
};

// Color ramp for elevation — 5-stop low→high (cool blue → green → warm brown)
function _elevColor(elev, minE, rangeE) {
  if (elev == null || rangeE === 0) return '#9a9a9a';
  const t = Math.max(0, Math.min(1, (elev - minE) / Math.max(1, rangeE)));
  const stops = [
    [22, 81, 122],     // very low — deep blue
    [97, 158, 188],    // low — pale blue
    [142, 193, 130],   // mid — green
    [216, 187, 121],   // mid-high — tan
    [142, 81, 38],     // high — brown
  ];
  const idx = Math.min(stops.length - 2, Math.floor(t * (stops.length - 1)));
  const local = (t * (stops.length - 1)) - idx;
  const a = stops[idx], b = stops[idx + 1];
  const r = Math.round(a[0] + (b[0] - a[0]) * local);
  const g = Math.round(a[1] + (b[1] - a[1]) * local);
  const bl = Math.round(a[2] + (b[2] - a[2]) * local);
  return `rgb(${r},${g},${bl})`;
}

// Drainage arrow (downstream pointer)
function _drainageArrowSvg(dir) {
  const dirs = { N:0, NNE:22.5, NE:45, ENE:67.5, E:90, ESE:112.5, SE:135, SSE:157.5,
                 S:180, SSW:202.5, SW:225, WSW:247.5, W:270, WNW:292.5, NW:315, NNW:337.5 };
  if (!(dir in dirs)) {
    return '<svg width="36" height="36" viewBox="0 0 36 36"><circle cx="18" cy="18" r="5" fill="#A5ADB7"/></svg>';
  }
  const deg = dirs[dir];
  return `<svg width="36" height="36" viewBox="0 0 36 36" style="transform:rotate(${deg}deg)">
    <line x1="18" y1="5" x2="18" y2="30" stroke="#F25929" stroke-width="3" stroke-linecap="round"/>
    <polygon points="18,33 12,23 24,23" fill="#F25929"/>
  </svg>`;
}

// Draw contour lines + hi/lo markers on the actual tract on the map.
window._elevMarkers = null;
function _drawElevationOnMap(d) {
  elevationOverlayGroup.clearLayers();
  _redrawElevationContours(d, true);

  // High and low point markers — placed precisely on the tract
  if (d.highest_latlon) {
    L.marker(d.highest_latlon, {
      icon: L.divIcon({
        className: '', iconAnchor: [11, 11], iconSize: [22, 22],
        html: `<div style="background:#8C5126;color:#FFF;width:22px;height:22px;border-radius:50%;display:flex;align-items:center;justify-content:center;font-size:11px;font-weight:700;border:2px solid #FFF;box-shadow:0 1px 4px rgba(0,0,0,0.30)">▲</div>`,
      })
    }).bindTooltip(`Highest: ${d.highest_ft} ft (${d.highest_loc})`, { permanent: false }).addTo(elevationOverlayGroup);
  }
  if (d.lowest_latlon) {
    L.marker(d.lowest_latlon, {
      icon: L.divIcon({
        className: '', iconAnchor: [11, 11], iconSize: [22, 22],
        html: `<div style="background:#16517A;color:#FFF;width:22px;height:22px;border-radius:50%;display:flex;align-items:center;justify-content:center;font-size:11px;font-weight:700;border:2px solid #FFF;box-shadow:0 1px 4px rgba(0,0,0,0.30)">▽</div>`,
      })
    }).bindTooltip(`Lowest: ${d.lowest_ft} ft (${d.lowest_loc})`, { permanent: false }).addTo(elevationOverlayGroup);
  }
}

function _redrawElevationContours(d, show) {
  // Clear only the polyline layers, leave the marker layers alone
  elevationOverlayGroup.eachLayer(l => {
    if (l instanceof L.Polyline && !(l instanceof L.Marker)) elevationOverlayGroup.removeLayer(l);
  });
  if (!show) return;
  const contours = d.contours || [];
  for (const c of contours) {
    // GeoJSON coords come as [lon, lat]; Leaflet wants [lat, lon]
    const latlngs = c.coords.map(([x, y]) => [y, x]);
    const col = _elevColor(c.elev_ft, d.min_ft, d.range_ft);
    const isLabeled = (Math.round(c.elev_ft) % (d.contour_interval_ft * 4) === 0);
    L.polyline(latlngs, {
      color: col, weight: isLabeled ? 2.2 : 1.2, opacity: 0.95,
    }).bindTooltip(`${c.elev_ft} ft`, { sticky: true, direction: 'top' }).addTo(elevationOverlayGroup);
  }
}

document.getElementById('elev-close').addEventListener('click', () => {
  document.getElementById('elev-dock').style.display = 'none';
  elevationOverlayGroup.clearLayers();
  _restoreMatchingTracts(false);
});
document.getElementById('elev-show-results').addEventListener('click', () => _restoreMatchingTracts(true));
document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape') {
    const dock = document.getElementById('elev-dock');
    if (dock && dock.style.display !== 'none') {
      dock.style.display = 'none';
      elevationOverlayGroup.clearLayers();
      _restoreMatchingTracts(false);
    }
  }
});

// =============================================================================
// Side-dock body: summary card + cross-sections + hypsometric histogram.
// Contours go on the actual map, not in here.
// =============================================================================
function _renderElevationDock(d) {
  return `
    ${_renderElevSummary(d)}
    ${_renderElevCrossSections(d)}
    ${_renderElevHistogram(d)}
    ${_renderElevContoursControl(d)}
    <div style="font-size:10px;color:#A5ADB7;margin-top:14px;line-height:1.5">
      ${d.sample_count} samples on a ${d.grid_size}×${d.grid_size} grid from USGS 3DEP
      (1-meter LiDAR where available).
    </div>
  `;
}

function _renderElevSummary(d) {
  // Plain-English read of the land
  const reliefLabel = d.range_ft < 3 ? 'essentially flat' :
                      d.range_ft < 10 ? 'gentle rolling' :
                      d.range_ft < 30 ? 'noticeable relief' :
                      d.range_ft < 80 ? 'rolling / hilly' : 'significant relief';
  const slopeLabel = d.slope_mean_pct < 0.5 ? 'flat (slab-friendly)' :
                     d.slope_mean_pct < 2.0 ? 'gentle (standard grading)' :
                     d.slope_mean_pct < 5.0 ? 'moderate (engineered grading)' :
                                              'steep (significant earthwork)';
  const dropInfo = d.range_ft >= 1
    ? `<b>${d.range_ft} ft</b> drop from ${d.highest_loc} to ${d.lowest_loc}`
    : 'no meaningful drop across the tract';

  // Drainage box — only show arrow when there's a real flow direction
  const compass = ["N","NNE","NE","ENE","E","ESE","SE","SSE","S","SSW","SW","WSW","W","WNW","NW","NNW"];
  const hasFlow = compass.includes(d.drainage_dir);
  const drainageContext = !hasFlow
    ? 'Tract is essentially flat — engineered detention will be required for any significant impervious cover.'
    : (d.slope_mean_pct < 0.5
        ? 'Very gentle gradient. Standard residential grading should drain, but commercial pads will need engineered routing.'
        : 'Natural drainage gradient. Watch the low side for floodplain overlap.');

  return `
    <div style="background:#F1F3F6;border-radius:8px;padding:14px 16px;margin-bottom:14px">
      <div style="font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:0.08em;color:#7A828D">Range</div>
      <div style="font-size:22px;font-weight:700;letter-spacing:-0.02em;color:#1A2330;line-height:1.1;margin:4px 0">
        ${d.min_ft.toFixed(0)} – ${d.max_ft.toFixed(0)}<span style="font-size:13px;color:#7A828D;font-weight:400"> ft</span>
      </div>
      <div style="font-size:12px;color:#1A2330;line-height:1.5">
        <b>${reliefLabel}</b> · ${d.range_ft.toFixed(1)} ft total ·
        <span style="color:#7A828D">median ${d.median_ft.toFixed(0)} ft</span>
      </div>
      <div style="font-size:12px;color:#1A2330;line-height:1.5;margin-top:4px">
        Mean slope <b>${d.slope_mean_pct.toFixed(2)}%</b> — ${slopeLabel}
      </div>
      <div style="font-size:12px;color:#1A2330;line-height:1.5;margin-top:4px">
        ${dropInfo}
      </div>
    </div>

    <div style="background:#FFF4ED;border-left:3px solid #F25929;border-radius:0 8px 8px 0;padding:12px 14px;margin-bottom:14px;display:flex;align-items:center;gap:12px">
      ${_drainageArrowSvg(d.drainage_dir)}
      <div style="flex:1">
        <div style="font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:0.08em;color:#F25929">Water drains</div>
        <div style="font-size:16px;font-weight:600;color:#1A2330;margin:2px 0">${d.drainage_dir}</div>
        <div style="font-size:11px;color:#7A828D;line-height:1.45">${drainageContext}</div>
      </div>
    </div>

    <div style="display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-bottom:14px">
      <div style="background:#FFF;border:1px solid #E5E8EC;border-left:3px solid #8C5126;border-radius:6px;padding:10px 12px">
        <div style="font-size:9px;font-weight:700;text-transform:uppercase;letter-spacing:0.08em;color:#7A828D">▲ Highest</div>
        <div style="font-size:18px;font-weight:700;color:#1A2330;margin-top:2px">${d.highest_ft.toFixed(0)} ft</div>
        <div style="font-size:11px;color:#7A828D">${d.highest_loc}</div>
      </div>
      <div style="background:#FFF;border:1px solid #E5E8EC;border-left:3px solid #16517A;border-radius:6px;padding:10px 12px">
        <div style="font-size:9px;font-weight:700;text-transform:uppercase;letter-spacing:0.08em;color:#7A828D">▽ Lowest</div>
        <div style="font-size:18px;font-weight:700;color:#1A2330;margin-top:2px">${d.lowest_ft.toFixed(0)} ft</div>
        <div style="font-size:11px;color:#7A828D">${d.lowest_loc}</div>
      </div>
    </div>
  `;
}

function _renderElevCrossSections(d) {
  const ns = d.ns_profile || [];
  const ew = d.ew_profile || [];
  if (ns.length < 2 && ew.length < 2) return '';

  const W = 380, H = 90, PAD_L = 28, PAD_R = 6, PAD_T = 8, PAD_B = 18;

  function buildSvg(profile, label, axisLabel) {
    if (profile.length < 2) return '';
    const xs = profile.map(p => p.frac);
    const ys = profile.map(p => p.elev_ft);
    const ymin = Math.min(...ys), ymax = Math.max(...ys);
    const yrange = Math.max(1, ymax - ymin);

    const xToPx = f => PAD_L + f * (W - PAD_L - PAD_R);
    const yToPx = e => (H - PAD_B) - ((e - ymin) / yrange) * (H - PAD_T - PAD_B);

    // Path string — separate sub-paths for inside vs outside polygon
    let insidePath = '', outsidePath = '';
    let lastInside = null;
    profile.forEach((p, i) => {
      const x = xToPx(p.frac), y = yToPx(p.elev_ft);
      const cmd = (i === 0 || lastInside !== p.inside) ? 'M' : 'L';
      if (p.inside) insidePath += `${cmd}${x.toFixed(1)},${y.toFixed(1)} `;
      else          outsidePath += `${cmd}${x.toFixed(1)},${y.toFixed(1)} `;
      lastInside = p.inside;
    });

    // Y-axis ticks (min, median, max)
    const ymid = (ymin + ymax) / 2;
    const ticks = [ymin, ymid, ymax];
    let ticksHtml = '';
    ticks.forEach(t => {
      const y = yToPx(t);
      ticksHtml += `<line x1="${PAD_L}" y1="${y}" x2="${W - PAD_R}" y2="${y}" stroke="#E5E8EC" stroke-width="0.5" stroke-dasharray="2,3"/>`;
      ticksHtml += `<text x="${PAD_L - 4}" y="${y + 3}" text-anchor="end" font-size="9" fill="#7A828D">${t.toFixed(0)}</text>`;
    });

    // X-axis end labels
    const xLabels = `
      <text x="${PAD_L}" y="${H - 4}" font-size="9" fill="#7A828D">${axisLabel[0]}</text>
      <text x="${W - PAD_R}" y="${H - 4}" text-anchor="end" font-size="9" fill="#7A828D">${axisLabel[1]}</text>`;

    return `
      <div style="background:#FFF;border:1px solid #E5E8EC;border-radius:8px;padding:10px 12px;margin-bottom:8px">
        <div style="font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:0.08em;color:#7A828D;margin-bottom:4px">${label}</div>
        <svg viewBox="0 0 ${W} ${H}" style="width:100%;height:auto">
          ${ticksHtml}
          <path d="${outsidePath}" fill="none" stroke="#CFD4DB" stroke-width="1.5" stroke-dasharray="2,3"/>
          <path d="${insidePath}" fill="none" stroke="#F25929" stroke-width="2"/>
          ${xLabels}
        </svg>
      </div>`;
  }

  return `
    <div style="font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:0.08em;color:#7A828D;margin:14px 0 6px">Cross-sections through center (ft)</div>
    ${buildSvg(ns, 'North → South', ['N', 'S'])}
    ${buildSvg(ew, 'West → East', ['W', 'E'])}
  `;
}

function _renderElevHistogram(d) {
  const h = d.histogram || [];
  if (h.length === 0) return '';
  const maxAcres = Math.max(...h.map(b => b.acres));
  const bars = h.slice().reverse().map(b => {
    const pct = b.acres / maxAcres * 100;
    const color = _elevColor((b.low + b.high) / 2, d.min_ft, d.range_ft);
    return `
      <div style="display:grid;grid-template-columns:60px 1fr 84px;align-items:center;gap:8px;font-size:11px;padding:2px 0">
        <div style="color:#1A2330;font-weight:500;text-align:right">${b.low}–${b.high} ft</div>
        <div style="background:#F1F3F6;border-radius:3px;height:14px;overflow:hidden">
          <div style="width:${pct.toFixed(1)}%;height:100%;background:${color}"></div>
        </div>
        <div style="color:#7A828D"><b style="color:#1A2330">${b.acres.toFixed(1)}</b> ac · ${b.pct}%</div>
      </div>`;
  }).join('');
  return `
    <div style="font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:0.08em;color:#7A828D;margin:14px 0 6px">Acreage by elevation band</div>
    <div style="background:#FFF;border:1px solid #E5E8EC;border-radius:8px;padding:10px 12px">
      ${bars}
      <div style="font-size:10px;color:#A5ADB7;margin-top:6px;border-top:1px solid #F1F3F6;padding-top:6px">
        Total ${d.polygon_acres} ac · band size ${d.contour_interval_ft} ft
      </div>
    </div>`;
}

function _renderElevContoursControl(d) {
  return `
    <div style="margin-top:14px;display:flex;align-items:center;gap:8px;padding:10px 12px;background:#F8F9FB;border:1px solid #E5E8EC;border-radius:8px">
      <label style="display:flex;align-items:center;gap:8px;font-size:12px;color:#1A2330;cursor:pointer;flex:1">
        <input type="checkbox" id="elev-show-contours" checked style="cursor:pointer;accent-color:#F25929">
        <span>Show contour lines on tract (every <b>${d.contour_interval_ft} ft</b>)</span>
      </label>
      <span style="font-size:10px;color:#7A828D">${(d.contours || []).length} lines</span>
    </div>
  `;
}

// -----------------------------------------------------------------------
// Notes (called from tract popup)
// -----------------------------------------------------------------------
// The Notes editor was removed from the map page. Storage, the API and the
// pipeline 'has notes' indicators are untouched.

// -----------------------------------------------------------------------
// Parcel detail modal — pulls EVERYTHING HCAD/MCAD/StratMap have on a single
// account, formatted as an in-app detail page (replaces the dead external links).
// -----------------------------------------------------------------------
function _fmtMoney(v) {
  const n = parseFloat(v);
  if (!isFinite(n) || n === 0) return '—';
  return '$' + n.toLocaleString('en-US', { maximumFractionDigits: 0 });
}
function _fmtAcres(v) {
  const s = String(v || '').replace(' AC', '').replace(',', '').trim();
  const n = parseFloat(s);
  return isFinite(n) ? n.toLocaleString('en-US', { maximumFractionDigits: 3 }) + ' ac' : '—';
}
function _fmtMsDate(ms) {
  const n = parseInt(ms, 10);
  if (!isFinite(n) || n <= 0) return '—';
  try { return new Date(n).toISOString().slice(0, 10); } catch { return '—'; }
}
function _row(label, value) {
  if (value == null || value === '' || value === '—') return '';
  return `<tr><td style="padding:3px 12px 3px 0;color:#6B7B8B;font-weight:600;white-space:nowrap;vertical-align:top">${label}</td><td style="padding:3px 0;color:#13344E">${value}</td></tr>`;
}
function _section(title, html) {
  if (!html) return '';
  return `<div style="margin:14px 0 4px;font-size:11px;font-weight:700;color:#F25929;text-transform:uppercase;letter-spacing:0.06em;border-bottom:1px solid #F4D58D;padding-bottom:3px">${title}</div><table style="width:100%;border-collapse:collapse">${html}</table>`;
}

// Texas property-tax state class codes — what HCAD's `state_class` field means.
// Single-letter major categories used by every TX appraisal district.
const STATE_CLASS_MAP = {
  'A':  'Single-family residential',
  'B':  'Multi-family residential',
  'C1': 'Vacant lot — residential',
  'C2': 'Vacant lot — commercial',
  'C3': 'Vacant land — rural',
  'D1': 'Qualified open-space ag (productivity-valued)',
  'D2': 'Qualified open-space ag (productivity-valued)',
  'D3': 'Farm / ranch improvement',
  'D4': 'Undeveloped acreage (NOT productivity-valued)',
  'E':  'Rural land — non-ag, with improvement',
  'F1': 'Commercial real',
  'F2': 'Industrial real',
  'G1': 'Oil / gas / mineral reserve',
  'J':  'Utility property',
  'L1': 'Commercial personal property',
  'L2': 'Industrial personal property',
  'M':  'Mobile home',
  'O':  'Residential inventory (builder lots)',
  'S':  'Special inventory (dealer)',
  'X':  'Totally exempt (church, gov, school, etc.)',
};
function stateClassLabel(code) {
  if (!code) return '—';
  const c = String(code).trim().toUpperCase();
  return (STATE_CLASS_MAP[c] || STATE_CLASS_MAP[c[0]] || 'Unknown class') + ` <span style="color:#999;font-weight:400">(${escapeHtml(c)})</span>`;
}

// Published per-$100 tax rates, injected from the Python TAX_RATES dict so we keep
// one source of truth. Keys are entity names; values are [rate_per_$100, note].
const TAX_RATES = window.ACQ_TAX_RATES || {};
window._currentUserName = window.ACQ_USER_NAME || '';
const TAX_YEAR  = window.ACQ_TAX_YEAR || null;

// Normalize a district name so HCAD's 'HARRIS COUNTY MUD 319' matches the Comptroller's
// 'Harris County MUD #319' / 'Harris County Municipal Utility District 319' / etc.
function _normalizeName(s) {
  if (!s) return '';
  return String(s).toUpperCase()
    .replace(/MUNICIPAL UTILITY DISTRICT/g, 'MUD')
    .replace(/EMERGENCY SERVICES DISTRICT/g, 'ESD')
    .replace(/WATER CONTROL (AND|&) IMPROVEMENT DISTRICT/g, 'WCID')
    .replace(/LEVEE IMPROVEMENT DISTRICT/g, 'LID')
    .replace(/FRESH WATER SUPPLY DISTRICT/g, 'FWSD')
    .replace(/ CO\.? /g, ' COUNTY ')
    .replace(/#/g, '')
    .replace(/\bNO\.?\b/g, '')
    .replace(/\bNUMBER\b/g, '')
    .replace(/\s+/g, ' ')
    .trim();
}

// Look up a rate. Tries exact, then a few common variants, then normalized form
// against every key (case-insensitive). Returns the [rate, note] tuple or null.
let _normalizedTaxRatesIndex = null;
function _lookupRate(name) {
  if (!name) return null;
  if (TAX_RATES[name]) return TAX_RATES[name];
  const norm = String(name).trim();
  const variants = [
    norm,
    norm.replace(/ Independent School District$/i, ' ISD'),
    norm.replace(/ ISD$/i, ' Independent School District'),
    norm.replace(/^City of /i, ''),
  ];
  for (const v of variants) if (TAX_RATES[v]) return TAX_RATES[v];
  // Build a normalized index once (lazy) for MUD/ESD fuzzy matching
  if (_normalizedTaxRatesIndex === null) {
    _normalizedTaxRatesIndex = {};
    for (const k of Object.keys(TAX_RATES)) {
      _normalizedTaxRatesIndex[_normalizeName(k)] = TAX_RATES[k];
    }
  }
  const nrm = _normalizeName(name);
  if (_normalizedTaxRatesIndex[nrm]) return _normalizedTaxRatesIndex[nrm];
  return null;
}

// Tax-jurisdiction renderer — combines spatial-enrichment data + published rate table.
function _renderTaxJurisdictions(county, x, taxableValue) {
  if (!x) return '';
  const isHarris = (county || '').toUpperCase().includes('HARRIS');
  const isMontgomery = (county || '').toUpperCase().includes('MONTGOMERY');
  const entries = [];   // each: { name, rate (string or null), note }

  function add(name, fallbackNote) {
    const rate = _lookupRate(name);
    entries.push({
      name,
      rate: rate ? rate[0] : null,
      note: rate ? rate[1] : (fallbackNote || ''),
    });
  }

  if (isHarris) {
    add('Harris County (general)');
    add('Harris County Flood Control District');
    add('Harris County Hospital District');
    add('Port of Houston Authority');
  } else if (isMontgomery) {
    entries.push({ name: 'Montgomery County (general)', rate: null, note: 'County operations (rate not in table)' });
    entries.push({ name: 'Montgomery County Hospital District', rate: null, note: 'Public health (rate not in table)' });
  } else if (county) {
    entries.push({ name: county + ' County', rate: null, note: 'County operations (rate not in table)' });
  }

  // School district
  const isd = x._school_dist || x._schools || '';
  if (isd) add(isd, 'School district');

  // Community college — derived by ISD/area (Harris geography)
  if (isHarris) {
    const isdLower = (isd || '').toLowerCase();
    let ccd = null;
    if (/(houston|alief)/i.test(isdLower))                                  ccd = 'Houston Community College System';
    else if (/(cypress|spring|klein|tomball|aldine|magnolia|waller)/i.test(isdLower)) ccd = 'Lone Star College System';
    else if (/(pasadena|la porte|deer park|channelview|goose creek|galena|sheldon)/i.test(isdLower)) ccd = 'San Jacinto Community College';
    if (ccd) add(ccd, 'Community college');
  }

  // City — apply tax only if the tract is INSIDE city limits, NOT just in the ETJ.
  // ETJ tracts get city services but pay no city ad-valorem tax.
  if (x._city_etj) {
    const cityName = String(x._city_etj).replace(/^City of /i, '');
    const rate = _lookupRate(cityName);
    if (x._in_city) {
      entries.push({
        name: 'City of ' + cityName,
        rate: rate ? rate[0] : null,
        note: rate ? rate[1] : 'City rate not in table',
      });
    } else {
      // In ETJ but not city limits — explicitly null rate so it doesn't add to combined.
      entries.push({
        name: 'City of ' + cityName + ' (ETJ only)',
        rate: null,
        note: 'ETJ-only — no city property tax applies',
      });
    }
  }

  // MUD / special district — uses the TX Comptroller's annual rate file (loaded server-side).
  if (x._in_mud && x._water_dist_name) {
    add(x._water_dist_name, 'Municipal Utility District (rate not in Comptroller file)');
  } else if (x._water_dist_name && /(MUD|ESD|WCID|LID|FWSD)/i.test(x._water_dist_name)) {
    // Not flagged as "in MUD" but the water-district name looks like a special district — try anyway
    add(x._water_dist_name, 'Special district');
  }

  if (entries.length === 0) {
    return _section('Tax jurisdictions', _row('—', '<span style="color:#999">No jurisdiction data for this tract</span>'));
  }

  const taxable = parseFloat(taxableValue) || 0;
  let combinedRate = 0;
  let combinedDollars = 0;

  const rows = entries.map(e => {
    let rateCell, amountCell;
    if (e.rate !== null) {
      const r = parseFloat(e.rate);
      combinedRate += r;
      const dollars = (taxable * r) / 100;
      combinedDollars += dollars;
      rateCell = `<b>$${r.toFixed(4)}</b><span style="color:#999"> /$100</span>`;
      amountCell = taxable ? `<b style="color:#13344E">${_fmtMoney(dollars)}</b>` : '<span style="color:#999">—</span>';
    } else {
      rateCell = '<span style="color:#999">—</span>';
      amountCell = '<span style="color:#999">—</span>';
    }
    return `<tr>
              <td style="padding:4px 12px 4px 0;color:#13344E;vertical-align:top">• ${escapeHtml(e.name)}<br><span style="color:#6B7B8B;font-size:10px">${escapeHtml(e.note)}</span></td>
              <td style="padding:4px 8px 4px 0;text-align:right;white-space:nowrap;vertical-align:top">${rateCell}</td>
              <td style="padding:4px 0;text-align:right;white-space:nowrap;vertical-align:top">${amountCell}</td>
            </tr>`;
  }).join('');

  // Totals row
  const totalPct = (combinedRate).toFixed(4);
  const effectivePct = (combinedRate).toFixed(2);
  const totalRow = `<tr style="border-top:2px solid #F4D58D">
                      <td style="padding:8px 12px 4px 0;color:#13344E"><b>Combined (known rates only)</b><br><span style="color:#6B7B8B;font-size:10px">Effective ~${effectivePct}% of taxable value · MUD/ESD adds more</span></td>
                      <td style="padding:8px 8px 4px 0;text-align:right;white-space:nowrap"><b>$${totalPct}</b><span style="color:#999"> /$100</span></td>
                      <td style="padding:8px 0;text-align:right;white-space:nowrap"><b style="color:#F25929">${taxable ? _fmtMoney(combinedDollars) : '—'}</b></td>
                    </tr>`;

  const footer = `<tr><td colspan="3" style="padding-top:10px;color:#6B7B8B;font-size:11px;font-style:italic">TY${TAX_YEAR} published rates from county Truth-in-Taxation filings + TX Comptroller's special-district report. Applied to <b>${_fmtMoney(taxable)}</b> taxable.</td></tr>`;

  return `<div style="margin:14px 0 4px;font-size:11px;font-weight:700;color:#F25929;text-transform:uppercase;letter-spacing:0.06em;border-bottom:1px solid #F4D58D;padding-bottom:3px">Tax jurisdictions &amp; rates (TY${TAX_YEAR})</div>
          <table style="width:100%;border-collapse:collapse;font-size:12px">
            <thead><tr style="color:#6B7B8B;font-size:10px;text-transform:uppercase;letter-spacing:0.06em">
              <th style="text-align:left;padding:0 12px 4px 0">Jurisdiction</th>
              <th style="text-align:right;padding:0 8px 4px 0">Rate / $100</th>
              <th style="text-align:right;padding:0 0 4px 0">Est. annual</th>
            </tr></thead>
            ${rows}${totalRow}${footer}
          </table>`;
}

function _renderHcadDetail(h, x) {
  // Owners — HCAD allows up to 3 with percent ownership
  let ownersHtml = '';
  for (let i = 1; i <= 3; i++) {
    const n = h[`owner_name_${i}`];
    if (!n) continue;
    const pct = h[`owner_pct_${i}`];
    const pctStr = pct ? ` <span style="color:#6B7B8B">(${(parseFloat(pct) * 100).toFixed(0)}%)</span>` : '';
    ownersHtml += `<div><b>${escapeHtml(String(n))}</b>${pctStr}</div>`;
  }

  // Site address — rebuild from parts
  const sitePieces = [
    h.site_str_num && h.site_str_num !== '0' ? h.site_str_num : '',
    h.site_str_pfx, h.site_str_name, h.site_str_sfx, h.site_str_sfx_dir
  ].map(s => (s || '').toString().trim()).filter(Boolean).join(' ');
  const siteFull = [sitePieces, (h.site_city || '').trim(), 'TX', (h.site_zip || '').trim()].filter(Boolean).join(', ');

  // Mail address
  const mailFull = [h.mail_addr_1, h.mail_addr_2, h.mail_city, h.mail_state, h.mail_zip]
    .map(s => (s || '').toString().trim()).filter(Boolean).join(', ');

  // Legal description
  const legal = [h.legal_dscr_1, h.legal_dscr_2, h.legal_dscr_3, h.legal_dscr_4]
    .map(s => (s || '').toString().trim()).filter(Boolean).join(' ');

  const mkt = parseFloat(h.total_market_val) || 0;
  const appr = parseFloat(h.total_appraised_val) || 0;
  const taxable = parseFloat(h.tax_value) || appr;
  const exemptionNote = (function () {
    const sc = (h.state_class || '').toString().trim().toUpperCase();
    if (sc.startsWith('D1') || sc.startsWith('D2')) return '<span style="color:#2E8B57">Productivity-valued — Texas ag exemption</span>';
    if (sc.startsWith('D4')) return '<span style="color:#C2410C">Undeveloped, no ag valuation — taxed at full market</span>';
    if (sc === 'X')          return '<span style="color:#2E8B57">Totally exempt (church / gov / school)</span>';
    return '';
  })();

  let html = '';
  html += _section('Owner', _row('Name(s)', ownersHtml) + _row('Mailing', escapeHtml(mailFull)) + _row('Owner since', escapeHtml(_fmtMsDate(h.new_owner_date))));
  html += _section('Property', _row('Site address', escapeHtml(siteFull) || '<span style="color:#999">(no street address on file)</span>') +
                                 _row('Acreage', escapeHtml(_fmtAcres(h.Acreage || h.acreage_1))) +
                                 _row('Land sq ft', h.land_sqft ? parseFloat(h.land_sqft).toLocaleString('en-US') : '—') +
                                 _row('Lot / Block', [h.LOT_NUM, h.BLK_NUM].filter(Boolean).join(' / ') || '—') +
                                 _row('Legal desc', escapeHtml(legal)) +
                                 _row('State class', stateClassLabel(h.state_class) + (exemptionNote ? `<br><span style="font-size:11px">${exemptionNote}</span>` : '')) +
                                 _row('Land use code', escapeHtml(String(h.land_use || '—'))));
  html += _section('Appraisal — tax year ' + (h.tax_year || ''),
                    _row('Land value', _fmtMoney(h.land_value)) +
                    _row('Improvements', _fmtMoney(h.impr_value)) +
                    _row('Building value', _fmtMoney(h.bld_value)) +
                    _row('Productivity value', _fmtMoney(h.productivity_value)) +
                    `<tr><td colspan="2" style="border-top:1px solid #E8EBED;padding-top:6px"></td></tr>` +
                    _row('Total market value', `<b>${_fmtMoney(h.total_market_val)}</b>`) +
                    _row('Total appraised',    `<b>${_fmtMoney(h.total_appraised_val)}</b>`) +
                    _row('Taxable value',      `<b>${_fmtMoney(taxable)}</b>`));
  html += _renderTaxJurisdictions(x && x._county, x, taxable);
  html += _section('Account', _row('HCAD #', escapeHtml(h.HCAD_NUM || h.acct_num || '—')) +
                                _row('Active?', h.activeAccount_flag === 'Y' ? 'Yes' : 'No') +
                                _row('CAMA?', h.isInCama === 'Y' ? 'Yes' : 'No'));
  return html;
}

function _renderGenericDetail(props, title) {
  // For MCAD / StratMap: show every non-empty field as a row
  let rows = '';
  for (const [k, v] of Object.entries(props)) {
    if (v == null || v === '' || k.startsWith('_') || k === 'Shape__Area' || k === 'Shape__Length' || k === 'OBJECTID' || k === 'GlobalID') continue;
    rows += _row(escapeHtml(k), escapeHtml(String(v)));
  }
  return _section(title, rows);
}

async function showParcelDetail(propId, enriched) {
  const m = document.getElementById('detail-modal');
  const body = document.getElementById('detail-body');
  const sub = document.getElementById('detail-sub');
  const title = document.getElementById('detail-title');
  title.textContent = 'Tract details';
  sub.textContent = `Prop ID ${propId} — loading live from HCAD…`;
  body.innerHTML = '<div style="text-align:center;padding:30px;color:#6B7B8B">Fetching live data…</div>';
  m.classList.add('show');
  try {
    const r = await fetch(`/api/acq/parcel-detail/${encodeURIComponent(propId)}`);
    const d = await r.json();
    if (d.error) throw new Error(d.error);
    let html = '';
    if (d.hcad) {
      title.textContent = (d.hcad.owner_name_1 || '?') + ' — Tract detail';
      sub.innerHTML = `Prop ID <b>${escapeHtml(propId)}</b> &middot; <span style="color:#F25929;font-weight:700">HCAD LIVE</span> &middot; ${escapeHtml(d.sources.join(', '))}`;
      html = _renderHcadDetail(d.hcad, enriched);
    } else if (d.mcad) {
      title.textContent = (d.mcad.ownerName || '?') + ' — Tract detail';
      sub.innerHTML = `Prop ID <b>${escapeHtml(propId)}</b> &middot; <span style="color:#1976d2;font-weight:700">MCAD LIVE</span>`;
      const taxableM = parseFloat(d.mcad.totalValue || d.mcad.appraisedValue || 0);
      html = _renderGenericDetail(d.mcad, 'All fields (MCAD)') + _renderTaxJurisdictions(enriched && enriched._county, enriched, taxableM);
    } else if (d.stratmap) {
      title.textContent = (d.stratmap.OWNER_NAME || '?') + ' — Tract detail';
      sub.innerHTML = `Prop ID <b>${escapeHtml(propId)}</b> &middot; StratMap (county CAD has fresher data)`;
      html = _renderGenericDetail(d.stratmap, 'All fields (StratMap)') + _renderTaxJurisdictions(enriched && enriched._county, enriched, 0);
    }
    body.innerHTML = html || '<div style="padding:20px;color:#6B7B8B">No fields returned.</div>';
  } catch (e) {
    sub.textContent = '';
    body.innerHTML = `<div style="padding:20px;color:#c62828"><b>Couldn't load detail:</b> ${escapeHtml(e.message)}</div>`;
  }
}
document.getElementById('detail-close').addEventListener('click', () => document.getElementById('detail-modal').classList.remove('show'));
// Click outside the inner modal also closes
document.getElementById('detail-modal').addEventListener('click', (e) => {
  if (e.target.id === 'detail-modal') document.getElementById('detail-modal').classList.remove('show');
});

// -----------------------------------------------------------------------
// Drawing tools (Leaflet.draw) + measurement
// -----------------------------------------------------------------------
const drawnItems = new L.FeatureGroup().addTo(map);
const SHAPE_COLORS = ["#F25929", "#1976d2", "#2e7d32", "#7b1fa2", "#c62828", "#00838f", "#ef6c00", "#5d4037"];

const drawControl = new L.Control.Draw({
  position: 'topleft',
  edit: { featureGroup: drawnItems, edit: false, remove: true },
  draw: {
    polygon:    { allowIntersection: false, showArea: true, metric: false, shapeOptions: { color: "#F25929", weight: 2 } },
    rectangle:  { showArea: true, metric: false, shapeOptions: { color: "#F25929", weight: 2 } },
    polyline:   { showLength: true, metric: false, shapeOptions: { color: "#F25929", weight: 3 } },
    circle:     { showRadius: true, metric: false, shapeOptions: { color: "#F25929", weight: 2 } },
    marker:     false,
    circlemarker: false,
  }
});
map.addControl(drawControl);

// Geometric helpers — units in popups
function polygonAreaM2(latlngs) {
  if (!latlngs || latlngs.length < 3) return 0;
  const R = 6378137;
  let a = 0;
  for (let i = 0; i < latlngs.length; i++) {
    const p1 = latlngs[i], p2 = latlngs[(i+1) % latlngs.length];
    a += (p2.lng - p1.lng) * Math.PI/180 *
         (2 + Math.sin(p1.lat * Math.PI/180) + Math.sin(p2.lat * Math.PI/180));
  }
  return Math.abs(a * R * R / 2);
}
function polylineLengthM(latlngs) {
  if (!latlngs || latlngs.length < 2) return 0;
  let t = 0;
  for (let i = 1; i < latlngs.length; i++) t += latlngs[i-1].distanceTo(latlngs[i]);
  return t;
}
function fmtArea(m2) {
  const acres = m2 / 4046.86;
  const sqft = m2 * 10.7639;
  const ha = m2 / 10000;
  const sqmi = m2 / 2589988;
  return `<b>${acres.toFixed(2)} acres</b> &nbsp;·&nbsp; ${Math.round(sqft).toLocaleString()} sqft &nbsp;·&nbsp; ${ha.toFixed(2)} ha &nbsp;·&nbsp; ${sqmi.toFixed(3)} sq mi`;
}
function fmtLength(m) {
  const ft = m * 3.28084;
  const mi = m / 1609.344;
  const km = m / 1000;
  return `<b>${Math.round(ft).toLocaleString()} ft</b> &nbsp;·&nbsp; ${mi.toFixed(3)} mi &nbsp;·&nbsp; ${Math.round(m).toLocaleString()} m &nbsp;·&nbsp; ${km.toFixed(3)} km`;
}

function computeMetrics(layer, shapeType) {
  if (shapeType === 'circle') {
    const r = layer.getRadius();      // meters
    const a = Math.PI * r * r;
    return {
      type: 'circle', radius_m: r, area_m2: a,
      summary: `<u>Circle</u><br>Radius: ${fmtLength(r)}<br>Area: ${fmtArea(a)}`
    };
  }
  if (shapeType === 'polyline') {
    const ll = layer.getLatLngs();
    const m = polylineLengthM(ll);
    return {
      type: 'polyline', length_m: m,
      summary: `<u>Line</u><br>Length: ${fmtLength(m)}`
    };
  }
  // polygon / rectangle
  const ll = shapeType === 'rectangle'
    ? (() => { const b = layer.getBounds(); return [b.getSouthWest(), b.getNorthWest(), b.getNorthEast(), b.getSouthEast()]; })()
    : layer.getLatLngs()[0];
  const a = polygonAreaM2(ll);
  const perim = polylineLengthM([...ll, ll[0]]);
  return {
    type: shapeType, area_m2: a, perimeter_m: perim,
    summary: `<u>${shapeType === 'rectangle' ? 'Rectangle' : 'Polygon'}</u><br>Area: ${fmtArea(a)}<br>Perimeter: ${fmtLength(perim)}`
  };
}

function bindShapePopup(layer, label, metrics, savedId) {
  // Stash the layer + metrics so the popup buttons can find them.
  // "Search inside this shape" button is added when the shape can be used as a search buffer
  // (polygon / rectangle — not point or polyline).
  const canSearchInside = metrics.type === 'polygon' || metrics.type === 'rectangle' || metrics.type === 'circle';
  let actionsHtml;
  if (savedId) {
    actionsHtml = `<button onclick="deleteShape('${savedId}', this)" style="background:#c62828;color:#FFF;border:0;padding:4px 10px;border-radius:4px;font-size:12px;cursor:pointer;font-weight:600">Delete</button>`;
  } else {
    actionsHtml =
      `<button onclick="openSaveForCurrentShape()" style="background:#F25929;color:#FFF;border:0;padding:4px 10px;border-radius:4px;font-size:12px;cursor:pointer;font-weight:600">Save shape</button>` +
      `<button onclick="discardCurrentShape()" style="background:#999;color:#FFF;border:0;padding:4px 10px;border-radius:4px;font-size:12px;cursor:pointer">Remove</button>`;
  }
  if (canSearchInside) {
    actionsHtml += `<button onclick="searchInsideShape('${savedId || ''}')"style="background:#1976d2;color:#FFF;border:0;padding:4px 10px;border-radius:4px;font-size:12px;cursor:pointer;font-weight:600;margin-left:6px">Search inside</button>`;
  }
  layer.bindPopup(
    `<b>${escapeHtml(label || 'Measurement')}</b><br>${metrics.summary}` +
    `<div style="margin-top:8px;display:flex;gap:6px;flex-wrap:wrap">${actionsHtml}</div>`,
    { maxWidth: 380 }
  );
}

window.searchInsideShape = function (savedId) {
  // Find the layer the user clicked on — either from saved store or pending
  let layer = null, shapeType = null;
  if (savedId && _savedShapeLayers[savedId]) {
    layer = _savedShapeLayers[savedId];
    // Re-infer shape type from the layer
    if (layer instanceof L.Circle) shapeType = 'circle';
    else if (layer instanceof L.Rectangle) shapeType = 'rectangle';
    else shapeType = 'polygon';
  } else if (_pendingShape) {
    layer = _pendingShape.layer;
    shapeType = _pendingShape.shapeType;
  }
  if (!layer) return;
  const geometry = shapeToGeoJson(layer, shapeType);
  // For circle, convert to a polygon approximation since the backend expects polygon-ish
  let polyGeom = geometry;
  if (shapeType === 'circle') {
    const c = layer.getLatLng();
    const r = layer.getRadius();
    const pts = [];
    for (let i = 0; i <= 64; i++) {
      const ang = (i / 64) * 2 * Math.PI;
      const lat = c.lat + (r / 111320) * Math.sin(ang);
      const lon = c.lng + (r / (111320 * Math.cos(c.lat * Math.PI / 180))) * Math.cos(ang);
      pts.push([lon, lat]);
    }
    polyGeom = { type: 'Polygon', coordinates: [pts] };
  }
  const minA = parseAcres('min_acres') || 300;
  const maxA = parseAcres('max_acres') || 100000;
  const labelInput = document.getElementById('label').value || 'shape-search';
  map.closePopup();
  setStatus(`Running search inside the drawn shape (acres ${minA}–${maxA})…`);
  fetch('/api/acq/search', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      polygon: polyGeom,
      min_acres: minA, max_acres: maxA, label: labelInput,
    }),
  }).then(r => r.json()).then(data => {
    if (data.error) { setStatus('Error: ' + data.error, true); return; }
    // Use the centroid of the polygon as the visual "center"
    const c = polyGeom.coordinates[0].reduce((acc, [x, y]) => [acc[0] + x, acc[1] + y], [0, 0]);
    const lat = c[1] / polyGeom.coordinates[0].length;
    const lon = c[0] / polyGeom.coordinates[0].length;
    renderSearch(data, { lat, lon, radius_mi: 0, min_acres: minA, max_acres: maxA });
    setStatus(`<span class="accent">${data.summary.tracts}</span> tracts inside the shape.`);
    document.querySelectorAll('#btn-export-kmz, #btn-export-xlsx, #btn-export-pdf').forEach(b => b.disabled = false);
  }).catch(e => setStatus('Search failed: ' + e.message, true));
};

// --- Render saved polygons on page load ---
let _savedShapeLayers = {};  // id -> Leaflet layer

async function loadPolygons() {
  try {
    const r = await fetch('/api/acq/polygons');
    const d = await r.json();
    // Clear any previously-loaded saved shapes
    Object.values(_savedShapeLayers).forEach(l => drawnItems.removeLayer(l));
    _savedShapeLayers = {};
    for (const p of (d.polygons || [])) {
      let layer = null;
      if (p.shape_type === 'circle') {
        const c = p.geometry.coordinates;   // [lon, lat]
        layer = L.circle([c[1], c[0]], { radius: p.radius || 0, color: p.color, weight: 2 });
      } else if (p.shape_type === 'polyline') {
        const latlngs = p.geometry.coordinates.map(([lon, lat]) => [lat, lon]);
        layer = L.polyline(latlngs, { color: p.color, weight: 3 });
      } else {
        // polygon / rectangle
        const ring = (p.geometry.coordinates && p.geometry.coordinates[0]) || [];
        const latlngs = ring.map(([lon, lat]) => [lat, lon]);
        layer = L.polygon(latlngs, { color: p.color, weight: 2 });
      }
      if (!layer) continue;
      layer.feature = { properties: { _savedShapeId: p.id, label: p.label } };
      const m = computeMetrics(layer, p.shape_type);
      bindShapePopup(layer, p.label, m, p.id);
      drawnItems.addLayer(layer);
      _savedShapeLayers[p.id] = layer;
    }
  } catch (e) {
    console.warn('loadPolygons failed', e);
  }
}

window.deleteShape = async function (id, btn) {
  if (!confirm('Delete this saved shape?')) return;
  await fetch(`/api/acq/polygons/${id}`, { method: 'DELETE' });
  const layer = _savedShapeLayers[id];
  if (layer) { drawnItems.removeLayer(layer); delete _savedShapeLayers[id]; }
  if (btn) btn.closest('.leaflet-popup-pane') && map.closePopup();
};

window.discardUnsavedShape = function (btn) {
  // Find the popup's source layer and remove it
  map.closePopup();
};

// --- On draw:created — show metrics in popup. User decides if they want to save. ---
let _pendingShape = null;       // { layer, shapeType, metrics }

map.on(L.Draw.Event.CREATED, (e) => {
  const layer = e.layer;
  const shapeType = e.layerType;
  const metrics = computeMetrics(layer, shapeType);
  drawnItems.addLayer(layer);
  _pendingShape = { layer, shapeType, metrics };
  // Popup shows the measurements + Save/Remove buttons. No modal opens.
  bindShapePopup(layer, 'Measurement', metrics, null);
  layer.openPopup();
});

// Called by the Save button inside the popup
window.openSaveForCurrentShape = function () {
  if (!_pendingShape) return;
  openShapeModal(_pendingShape.shapeType, _pendingShape.metrics);
};

window.discardCurrentShape = function () {
  if (_pendingShape) {
    drawnItems.removeLayer(_pendingShape.layer);
    _pendingShape = null;
  }
  map.closePopup();
};

function openShapeModal(shapeType, metrics) {
  document.getElementById('shape-sub').innerHTML = metrics.summary;
  document.getElementById('shape-label').value = '';
  document.getElementById('shape-notes').value = '';
  // Folder dropdown
  const folderSel = document.getElementById('shape-folder');
  folderSel.innerHTML = '<option value="">— No folder —</option>' +
    _folders.map(f => `<option value="${f.id}">${escapeHtml(f.name)}</option>`).join('');
  // Color swatches
  const cw = document.getElementById('shape-color-row');
  cw.innerHTML = SHAPE_COLORS.map((c, i) =>
    `<button type="button" data-color="${c}" style="width:24px;height:24px;border-radius:50%;border:${i===0?'2px solid #13344E':'1px solid #ccc'};background:${c};cursor:pointer"></button>`
  ).join('');
  let chosen = SHAPE_COLORS[0];
  cw.querySelectorAll('button').forEach(btn => btn.addEventListener('click', () => {
    chosen = btn.dataset.color;
    cw.querySelectorAll('button').forEach(b => b.style.border = '1px solid #ccc');
    btn.style.border = '2px solid #13344E';
  }));
  cw.dataset.selectedColor = chosen;
  // Wire save/cancel (re-bind each open to avoid stacked listeners)
  const cancel = document.getElementById('shape-cancel');
  const save = document.getElementById('shape-save');
  const newCancel = cancel.cloneNode(true); cancel.parentNode.replaceChild(newCancel, cancel);
  const newSave = save.cloneNode(true); save.parentNode.replaceChild(newSave, save);
  newCancel.addEventListener('click', () => {
    // Discard the unsaved shape
    if (_pendingShape) drawnItems.removeLayer(_pendingShape.layer);
    _pendingShape = null;
    document.getElementById('shape-modal').classList.remove('show');
  });
  newSave.addEventListener('click', async () => {
    if (!_pendingShape) return;
    const color = cw.querySelector('button[style*="2px solid"]').dataset.color || SHAPE_COLORS[0];
    const label = document.getElementById('shape-label').value.trim() || 'Untitled shape';
    const folder_id = document.getElementById('shape-folder').value || null;
    const notes = document.getElementById('shape-notes').value.trim();
    const alsoSearch = document.getElementById('shape-also-search').checked;
    const geometry = shapeToGeoJson(_pendingShape.layer, _pendingShape.shapeType);
    const payload = {
      label, folder_id, notes, color,
      shape_type: _pendingShape.shapeType,
      geometry,
      radius: _pendingShape.shapeType === 'circle' ? _pendingShape.layer.getRadius() : null,
      metrics: _pendingShape.metrics,
    };
    const r = await fetch('/api/acq/polygons', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    const d = await r.json();
    if (d.id) {
      _pendingShape.layer.setStyle && _pendingShape.layer.setStyle({ color });
      bindShapePopup(_pendingShape.layer, label, _pendingShape.metrics, d.id);
      _savedShapeLayers[d.id] = _pendingShape.layer;
      let statusMsg = `Saved shape "${escapeHtml(label)}".`;
      // If the user also wants this as a saved search, create one with the polygon as the buffer
      if (alsoSearch && (_pendingShape.shapeType === 'polygon' || _pendingShape.shapeType === 'rectangle' || _pendingShape.shapeType === 'circle')) {
        let polyGeom = geometry;
        if (_pendingShape.shapeType === 'circle') {
          const c = _pendingShape.layer.getLatLng();
          const rr = _pendingShape.layer.getRadius();
          const pts = [];
          for (let i = 0; i <= 64; i++) {
            const ang = (i / 64) * 2 * Math.PI;
            const la = c.lat + (rr / 111320) * Math.sin(ang);
            const lo = c.lng + (rr / (111320 * Math.cos(c.lat * Math.PI / 180))) * Math.cos(ang);
            pts.push([lo, la]);
          }
          polyGeom = { type: 'Polygon', coordinates: [pts] };
        }
        const minA = parseAcres('min_acres') || 300;
        const maxA = parseAcres('max_acres') || 100000;
        await fetch('/api/acq/searches', {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            label, folder_id, lat: 0, lon: 0, radius_mi: 0,
            min_acres: minA, max_acres: maxA,
            polygon: polyGeom,
          }),
        });
        statusMsg += ` Saved as a search too — click the ▶ on it under Saved searches to re-run.`;
        loadSearches();
      }
      setStatus(statusMsg + ` ${_pendingShape.metrics.summary.split('<br>').slice(1).join(' · ').replace(/<\/?[^>]+>/g,'')}`);
      loadFolders();
    }
    _pendingShape = null;
    document.getElementById('shape-modal').classList.remove('show');
  });
  document.getElementById('shape-modal').classList.add('show');
  setTimeout(() => document.getElementById('shape-label').focus(), 50);
}

function shapeToGeoJson(layer, shapeType) {
  if (shapeType === 'circle') {
    const c = layer.getLatLng();
    return { type: 'Point', coordinates: [c.lng, c.lat] };
  }
  if (shapeType === 'polyline') {
    return { type: 'LineString',
             coordinates: layer.getLatLngs().map(p => [p.lng, p.lat]) };
  }
  // polygon / rectangle
  let ring;
  if (shapeType === 'rectangle') {
    const b = layer.getBounds();
    ring = [b.getSouthWest(), b.getNorthWest(), b.getNorthEast(), b.getSouthEast(), b.getSouthWest()];
  } else {
    ring = layer.getLatLngs()[0].slice();
    ring.push(ring[0]);  // close ring
  }
  return { type: 'Polygon', coordinates: [ring.map(p => [p.lng, p.lat])] };
}

// Handle Leaflet.draw deletes (the trash-icon edit mode)
map.on(L.Draw.Event.DELETED, async (e) => {
  e.layers.eachLayer(async (layer) => {
    const id = layer.feature && layer.feature.properties && layer.feature.properties._savedShapeId;
    if (id) {
      await fetch(`/api/acq/polygons/${id}`, { method: 'DELETE' });
      delete _savedShapeLayers[id];
      loadFolders();
    }
  });
});

// Mobile sidebar toggle
const mobileBtn = document.getElementById('mobile-menu-btn');
if (mobileBtn) {
  mobileBtn.addEventListener('click', () => {
    document.getElementById('acq-sidebar').classList.toggle('open');
  });
  // Close sidebar when user taps the map on mobile
  map.on('click', () => document.getElementById('acq-sidebar').classList.remove('open'));
}

// -----------------------------------------------------------------------
// Render saved tract pins as outlines on the map + show full info on click
// -----------------------------------------------------------------------
const savedPinsOverlay = L.layerGroup().addTo(map);
const highlightedPinOverlay = L.layerGroup().addTo(map);
const _pinCache = {};   // id -> full pin object (filled by folder rendering + loadSavedPinOverlays)
const _pinLayerById = {};   // id -> Leaflet layer (so we can fly + open popup)

function savedPinPopupHtml(p) {
  const lat = p.lat || 0, lon = p.lon || 0;
  const center = (lat && lon) ? `${lat},${lon}` : '';
  const safeOwner = (p.owner_name || '').replace(/'/g, "&#39;");
  return (
    `<div style="min-width:220px"><b>${escapeHtml(p.label || '')}</b><br>` +
    `<span style="color:#666;font-size:11px">Saved ${p.saved_at ? new Date(p.saved_at).toLocaleDateString() : ''}</span><br>` +
    `<hr style="margin:6px 0;border:0;border-top:1px solid #eee">` +
    `<b>Owner:</b> ${escapeHtml(p.owner_name || '—')}<br>` +
    `<b>Acres:</b> ${p.acres != null ? p.acres : '—'}<br>` +
    `<b>County:</b> ${escapeHtml(p.county || '—')}<br>` +
    (p.site_address ? `<b>Site:</b> ${escapeHtml(p.site_address)}<br>` : '') +
    (p.mailing ? `<b>Mailing:</b> ${escapeHtml(p.mailing)}<br>` : '') +
    `<b>Prop ID:</b> ${escapeHtml(p.prop_id || '')}<br>` +
    (center ? `<a target="_blank" href="https://www.google.com/maps/?q=${center}">Google Maps</a><br>` : '') +
    `<div style="margin-top:8px;display:flex;gap:6px;flex-wrap:wrap">` +
    `<button onclick='unhighlightPin()' style="background:#999;color:#FFF;border:0;padding:4px 10px;border-radius:4px;font-size:11px;cursor:pointer">Hide outline</button>` +
    `</div></div>`
  );
}

window.unhighlightPin = function () {
  highlightedPinOverlay.clearLayers();
  map.closePopup();
};

// Small saved-pin dot — replaces Leaflet's huge default 25x41 px teardrop
const SAVED_PIN_DOT = L.divIcon({
  className: 'saved-pin-dot',
  iconSize: [14, 14], iconAnchor: [7, 7],
  html: '<div style="width:12px;height:12px;border-radius:50%;background:#1976d2;border:2px solid #FFF;box-shadow:0 1px 3px rgba(0,0,0,0.35)"></div>',
});

async function loadSavedPinOverlays() {
  try {
    const r = await fetch('/api/acq/tract-pins');
    const d = await r.json();
    savedPinsOverlay.clearLayers();
    for (const p of (d.pins || [])) {
      _pinCache[p.id] = p;
      if (p.geometry) {
        const layer = L.geoJSON(p.geometry, {
          style: { color: '#1976d2', weight: 2, fillColor: '#90caf9', fillOpacity: 0.18, dashArray: '4,3' },
        });
        layer.bindPopup(savedPinPopupHtml(p), { maxWidth: 360 });
        layer.addTo(savedPinsOverlay);
        _pinLayerById[p.id] = layer;
      } else if (p.lat && p.lon) {
        const m = L.marker([p.lat, p.lon], { icon: SAVED_PIN_DOT, title: p.label });
        m.bindPopup(savedPinPopupHtml(p), { maxWidth: 360 });
        m.addTo(savedPinsOverlay);
        _pinLayerById[p.id] = m;
      }
    }
  } catch (e) { console.warn('loadSavedPinOverlays failed', e); }
}

// Called when user clicks a tract pin row in a folder.
// - Looks up the pin
// - Draws its outline prominently in Ember orange
// - Flies the map to its bounds
// - Opens a popup with full info
window.showSavedPin = function (pinId) {
  const p = _pinCache[pinId];
  if (!p) {
    // Refetch and try again
    fetch('/api/acq/tract-pins').then(r => r.json()).then(d => {
      (d.pins || []).forEach(x => _pinCache[x.id] = x);
      if (_pinCache[pinId]) window.showSavedPin(pinId);
      else alert('Pin not found.');
    });
    return;
  }
  highlightedPinOverlay.clearLayers();
  let bounds = null;
  if (p.geometry) {
    const hl = L.geoJSON(p.geometry, {
      style: { color: '#F25929', weight: 4, fillColor: '#F25929', fillOpacity: 0.20 }
    });
    hl.bindPopup(savedPinPopupHtml(p), { maxWidth: 360 });
    hl.addTo(highlightedPinOverlay);
    bounds = hl.getBounds();
    if (bounds && bounds.isValid()) {
      map.fitBounds(bounds.pad(0.4));
      hl.openPopup();
      return;
    }
  }
  // Fallback: no geometry — just marker + popup at lat/lon
  if (p.lat && p.lon) {
    const m = L.marker([p.lat, p.lon]).bindPopup(savedPinPopupHtml(p), { maxWidth: 360 });
    m.addTo(highlightedPinOverlay);
    map.setView([p.lat, p.lon], 16);
    m.openPopup();
  }
};

// -----------------------------------------------------------------------
// Favorites — one-click star, sidebar list, orange pins on the map
// -----------------------------------------------------------------------
window._favoriteIndicators = new Set();   // prop_ids that are favorited (set on load)
const favoritesOverlay = L.layerGroup().addTo(map);     // all favorite pins, always visible
const _favLayerById = {};                                // id -> Leaflet marker, for click-to-open

// Bright orange teardrop pin — visually distinct from the Ember-red search center pin
const FAVORITE_PIN_ICON = L.divIcon({
  className: 'favorite-pin-icon',
  iconSize: [22, 22],
  iconAnchor: [11, 22],
  html: '<svg viewBox="0 0 24 24" width="22" height="22" style="filter:drop-shadow(0 1px 2px rgba(0,0,0,0.4))">'
        + '<path fill="#FF8800" stroke="#8B4500" stroke-width="1.2" d="M12 2 C8 2 5 5 5 9 c0 5.25 7 13 7 13 s7-7.75 7-13 c0-4-3-7-7-7 z"/>'
        + '<circle cx="12" cy="9" r="2.5" fill="#FFF"/></svg>',
});

function favoritePopupHtml(fav) {
  return (
    `<b>${escapeHtml(fav.label)}</b><br>` +
    `Owner: ${escapeHtml(fav.owner_name || '—')}<br>` +
    `${fav.acres != null ? fav.acres + ' ac' : ''} · ${escapeHtml(fav.county || '')}<br>` +
    `Prop ID: ${escapeHtml(fav.prop_id || '')}<br>` +
    (fav.lat && fav.lon ? `<a target="_blank" href="https://www.google.com/maps/?q=${fav.lat},${fav.lon}">Google Maps</a><br>` : '') +
    `<div style="margin-top:8px;display:flex;gap:6px">` +
      `<button onclick='unfavoriteFromMap(${JSON.stringify(fav.prop_id)})' style="background:#999;color:#FFF;border:0;padding:4px 10px;border-radius:4px;font-size:11px;cursor:pointer">Unfavorite</button>` +
    `</div>`
  );
}

window.unfavoriteFromMap = async function (propId) {
  await fetch('/api/acq/favorites', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ prop_id: propId }),
  });
  map.closePopup();
  loadFavorites();
};

async function loadFavorites() {
  try {
    const r = await fetch('/api/acq/favorites');
    const d = await r.json();
    const items = d.favorites || [];
    window._favoriteIndicators = new Set(items.map(f => f.prop_id));
    renderFavorites(items);
    renderFavoritesOnMap(items);
  } catch (e) { console.warn('loadFavorites failed', e); }
}

function renderFavorites(items) {
  const badge = document.getElementById('favorites-badge');
  const list = document.getElementById('favorites-list');
  if (!items.length) {
    badge.style.display = 'none';
    list.innerHTML = '<div class="changes-empty">Click "Favorite"on any tract — it\'ll drop an orange pin here and on the map.</div>';
    return;
  }
  badge.textContent = items.length;
  badge.style.display = 'inline-block';
  list.innerHTML = items.map(f => `
    <div class="saved-search" data-id="${f.id}">
      <span style="display:inline-block;width:10px;height:10px;border-radius:50%;background:#FF8800;margin-right:8px;flex-shrink:0;border:1px solid #8B4500"></span>
      <div class="name" title="Favorited ${f.favorited_at || ''}">${escapeHtml(f.label)}<div class="meta">${f.acres != null ? f.acres + ' ac' : ''} · ${escapeHtml(f.county || '')}</div></div>
      <div class="actions">
        <button class="icon-btn" data-act="fav-view" data-id="${f.id}" title="Fly to pin">▶</button>
        <button class="icon-btn" data-act="fav-del" data-id="${f.id}" title="Remove">×</button>
      </div>
    </div>
  `).join('');
  list.querySelectorAll('button[data-act]').forEach(btn => btn.addEventListener('click', async (e) => {
    e.stopPropagation();
    const id = btn.dataset.id;
    const fav = items.find(x => x.id === id);
    if (!fav) return;
    if (btn.dataset.act === 'fav-view') {
      flyToFavorite(fav);
    } else if (btn.dataset.act === 'fav-del') {
      await fetch(`/api/acq/favorites/${id}`, { method: 'DELETE' });
      loadFavorites();
    }
  }));
}

// Drop orange pins on the map for every favorite (always visible, like saved-pin outlines).
function renderFavoritesOnMap(items) {
  favoritesOverlay.clearLayers();
  for (const k of Object.keys(_favLayerById)) delete _favLayerById[k];
  for (const f of items) {
    if (!f.lat || !f.lon) continue;
    const m = L.marker([f.lat, f.lon], { icon: FAVORITE_PIN_ICON, title: f.label });
    m.bindPopup(favoritePopupHtml(f), { maxWidth: 320 });
    m.addTo(favoritesOverlay);
    _favLayerById[f.id] = m;
  }
}

function flyToFavorite(fav) {
  const marker = _favLayerById[fav.id];
  if (marker && fav.lat && fav.lon) {
    map.setView([fav.lat, fav.lon], 16);
    marker.openPopup();
  }
}

window.toggleFavorite = async function (propId, ownerName, acres, county, lat, lon, geometryStr, btn) {
  let geometry = null;
  try { geometry = JSON.parse(geometryStr); } catch {}
  const r = await fetch('/api/acq/favorites', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      prop_id: propId,
      label: ownerName ? `${ownerName} (${acres} ac)` : propId,
      owner_name: ownerName, acres: parseFloat(acres) || null,
      county, lat, lon, geometry,
    }),
  });
  const d = await r.json();
  if (d.ok) {
    if (d.favorited) {
      window._favoriteIndicators.add(propId);
      if (btn) { btn.classList.add('fav-on'); btn.textContent = 'Favorited'; }
    } else {
      window._favoriteIndicators.delete(propId);
      if (btn) { btn.classList.remove('fav-on'); btn.textContent = 'Favorite'; }
    }
    loadFavorites();   // refreshes sidebar list AND orange pins on the map
  }
};

// Initial load
loadFolders();
loadSearches();
loadPolygons();
loadSavedPinOverlays();
loadFavorites();
loadPipeline();

// -----------------------------------------------------------------------
// Restore last search — if the user ran a search, navigated away (to a
// tract page / pipeline / corridors), then came back here, we re-render
// the previous search results from the server-side cache so they don't
// have to re-search. Only triggers when no focus_tract / focus_outreach
// param is in the URL (those have their own restore logic).
// -----------------------------------------------------------------------
(async function _maybeRestoreLastSearch() {
  const params = new URLSearchParams(window.location.search);
  if (params.get('focus_tract') || params.get('focus_tract_local') || params.get('focus_outreach')) {
    return;   // a focus_* deep-link is handling page state
  }
  try {
    const r = await fetch('/api/acq/last-search');
    const d = await r.json();
    if (!d.ok || !d.layers || !d.meta) return;
    const ageMin = d.saved_at ? Math.round((Date.now() / 1000 - d.saved_at) / 60) : null;
    // Only auto-prompt for recent searches (≤30 min). Beyond that, ignore.
    if (ageMin != null && ageMin > 30) return;
    const summary = (d.layers.tracts && d.layers.tracts.features) ? d.layers.tracts.features.length : 0;
    const label = (d.meta.label || 'last search').replace(/</g, '');
    // Banner with one-click restore — non-intrusive, dismissable
    const banner = document.createElement('div');
    banner.id = 'restore-search-banner';
    banner.style.cssText = 'position:fixed;top:64px;left:50%;transform:translateX(-50%);' +
      'background:#FFF;border:1px solid #E5E8EC;border-left:4px solid #F25929;border-radius:8px;' +
      'box-shadow:0 8px 24px rgba(15,23,42,0.16);padding:10px 14px;z-index:1400;' +
      'display:flex;align-items:center;gap:12px;font-size:13px;color:#1A2330;max-width:90vw';
    banner.innerHTML = `
      <span>↻ Restore last search: <b>${escapeHtml(label)}</b> · ${summary} tracts${ageMin != null ? ` · ${ageMin} min ago` : ''}</span>
      <button id="restore-yes" style="background:#F25929;color:#FFF;border:0;padding:5px 12px;border-radius:4px;font:inherit;font-size:12px;font-weight:600;cursor:pointer">Restore</button>
      <button id="restore-no" style="background:transparent;border:1px solid #CFD4DB;color:#7A828D;padding:5px 10px;border-radius:4px;font:inherit;font-size:12px;cursor:pointer">Dismiss</button>
    `;
    document.body.appendChild(banner);
    document.getElementById('restore-yes').addEventListener('click', () => {
      banner.remove();
      const synthPayload = {
        lat: (d.meta.center || [0,0])[0] || 0,
        lon: (d.meta.center || [0,0])[1] || 0,
        radius_mi: d.meta.radius_mi || 0,
      };
      renderSearch({ layers: d.layers, summary: { tracts: summary } }, synthPayload);
      setStatus(`Restored: <b>${escapeHtml(label)}</b> · ${summary} tracts (cached).`);
    });
    document.getElementById('restore-no').addEventListener('click', () => banner.remove());
  } catch (e) {
    console.warn('restore-last-search failed:', e);
  }
})();

// -----------------------------------------------------------------------
// Deep link from /pipeline (or anywhere):
//   /?focus_outreach=<prop_id> → opens the outreach modal
//   /?focus_tract=<prop_id>    → draws the parcel + zooms + opens a popup
// -----------------------------------------------------------------------
(async function _checkFocusOutreach() {
  const params = new URLSearchParams(window.location.search);
  const pid = params.get('focus_outreach');
  if (!pid) return;
  try {
    const r = await fetch(`/api/acq/outreach/${encodeURIComponent(pid)}`);
    const d = await r.json();
    const rec = d.record || {};
    showOutreach(pid, rec.owner_name || '', rec.acres || '', rec.county || '');
  } catch (e) {
    console.error('Failed to focus outreach for', pid, e);
  } finally {
    const url = new URL(window.location.href);
    url.searchParams.delete('focus_outreach');
    window.history.replaceState({}, '', url.pathname + (url.searchParams.toString() ? '?' + url.searchParams.toString() : ''));
  }
})();

// Load ancillary layers (floodplain, wetlands, MUDs, streams, pipelines, etc.)
// around a focused tract. Called after the orange focus polygon is drawn so the
// layer-control panel toggles show real data instead of being empty.
//
// Strategy: send the tract's centroid + a ~1-mile radius to /api/search. We
// don't pass `polygon` because that would clip layers to the tract itself —
// users want to see floodplain in the surrounding area, not just on the tract.
// `min_acres=0, max_acres=0` keeps the matching-tracts query empty so we don't
// crowd the focus polygon with neighbor tracts in the sidebar list.
async function _loadLayersAroundFocusTract(geometry, ownerLabel) {
  if (!geometry || !window.renderSearch) return;
  try {
    const tmp = L.geoJSON(geometry);
    const c = tmp.getBounds().getCenter();
    setStatus(`Loading layers around <span class="accent">${escapeHtml(ownerLabel || 'tract')}</span>…`);
    const body = {
      lat: c.lat, lon: c.lng,
      radius_mi: 1.0,
      min_acres: 0,
      max_acres: 0,    // 0 max => skip matching tracts, just get layers
      label: 'tract context',
    };
    const r = await fetch('/api/acq/search', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    const data = await r.json();
    if (!r.ok || data.error) { console.warn('layer fetch failed:', data.error); return; }
    // Reuse the full renderSearch — it populates all overlay groups, popups,
    // styles, and counts in the layer-control panel. After it runs we:
    //   1) clear search-center marker + radius circle (user didn't ask for them)
    //   2) re-zoom to the focus tract (renderSearch fit-bounds to tracts list)
    //   3) bring the orange focus polygon back to the front
    await renderSearch(data, { lat: c.lat, lon: c.lng, radius_mi: 1.0 });
    try { overlays.searchCenter.clearLayers(); } catch {}
    try { overlays.radius.clearLayers(); } catch {}
    if (window._focusTractLayer) {
      try { window._focusTractLayer.bringToFront(); } catch {}
      try { map.fitBounds(window._focusTractLayer.getBounds().pad(0.40), { maxZoom: 17 }); } catch {}
    }
    // Layers stay off on load - the user turns on what they want from the
    // layer panel.
    setStatus(`Tract loaded. Turn on floodplain / wetlands / MUDs from the layer panel (top-right) as needed.`);
  } catch (e) {
    console.warn('_loadLayersAroundFocusTract failed:', e);
  }
}

(async function _checkFocusTract() {
  const params = new URLSearchParams(window.location.search);
  // Two modes:
  //   ?focus_tract=<pid>        → server-side lookup (legacy, can mis-resolve
  //                               when the same prop_id exists in multiple
  //                               data sources)
  //   ?focus_tract_local=<pid>  → uses geometry stashed in sessionStorage by
  //                               whichever page sent the user here. Bypasses
  //                               server-side lookup entirely so you always
  //                               land on the EXACT tract that was clicked.
  const pidLocal = params.get('focus_tract_local');
  const pid      = pidLocal || params.get('focus_tract');
  if (!pid) return;
  if (window._focusTractLayer) {
    try { map.removeLayer(window._focusTractLayer); } catch {}
    window._focusTractLayer = null;
  }

  // ---- Local-mode: read the tract geometry directly from sessionStorage.
  // We NEVER fall through to a server lookup for focus_tract_local — the
  // server's by-prop_id lookup can resolve to the wrong parcel when the
  // same numeric ID exists in different data sources (HCAD account vs.
  // Galveston StratMap prop_id, etc.). Better to show a clear "couldn't
  // load — go back" error than a random other tract.
  if (pidLocal) {
    const cleanUrl = () => {
      const url = new URL(window.location.href);
      url.searchParams.delete('focus_tract_local');
      window.history.replaceState({}, '', url.pathname + (url.searchParams.toString() ? '?' + url.searchParams.toString() : ''));
    };
    let raw = null, d2 = null, err = null;
    try {
      // New: per-pid key in localStorage (shared across tabs so a popup
      // window can read it). Legacy: sessionStorage 'focus_tract_data'.
      raw = localStorage.getItem('focus_tract_data:' + pidLocal)
         || sessionStorage.getItem('focus_tract_data');
      if (raw) d2 = JSON.parse(raw);
    } catch (e) { err = e; }
    console.log('[focus_tract_local] pid=', pidLocal, 'storedPid=', d2?.prop_id, 'hasGeom=', !!d2?.geometry);

    if (!d2 || String(d2.prop_id) !== String(pidLocal)) {
      setStatus(
        `Couldn't show the tract on the map — its data was lost in transit. ` +
        `Click <a href="/acquisitions/corridors"style="color:#F25929">back to Corridors</a>and try the button again.`,
        true);
      cleanUrl();
      return;
    }
    if (!d2.geometry) {
      setStatus(
        `Tract <b>${escapeHtml(d2.owner_name || pidLocal)}</b> has no map geometry on file — ` +
        `<a href="/acquisitions/tract/${encodeURIComponent(pidLocal)}" style="color:#F25929">open its full page</a> instead.`,
        true);
      cleanUrl();
      return;
    }
    try {
      localStorage.removeItem('focus_tract_data:' + pidLocal);
      sessionStorage.removeItem('focus_tract_data');
    } catch {}
    const layer = L.geoJSON(d2.geometry, {
      style: { color: '#F25929', weight: 4, opacity: 1, fillColor: '#F25929', fillOpacity: 0.15 }
    }).addTo(map);
    window._focusTractLayer = layer;
    const ownerLine = `<b style="font-size:13px">${escapeHtml(d2.owner_name || '(no owner)')}</b>`
      + (d2.hcad_live ? ` <span style="background:rgba(46,125,50,0.12);color:#2E7D32;font-size:9px;padding:1px 5px;border-radius:3px;font-weight:600">HCAD</span>` :
         d2.mcad_live ? ` <span style="background:rgba(25,118,210,0.12);color:#1976D2;font-size:9px;padding:1px 5px;border-radius:3px;font-weight:600">MCAD</span>` : '');
    const acres = +(d2.acres || 0);
    // Stash THIS tract for the /tract page so its "Full tract page" button
    // opens the right parcel — never the server's wrong-county fallback. We
    // synthesize the same shape /api/tract-page-data returns: a Feature with
    // geometry + properties drawn from the corridor data we already have.
    try {
      const synthTract = {
        type: 'Feature',
        geometry: d2.geometry,
        properties: {
          Prop_ID:    pidLocal,
          OWNER_NAME: d2.owner_name || '',
          Acres:      d2.acres || 0,
          _county:    d2.county || '',
          MAIL_ADDR:  d2.mail_addr || '',
          SITUS_ADDR: d2.situs_addr || '',
          LEGAL_DESC: d2.legal || '',
        },
      };
      localStorage.setItem('tract_page_local:' + pidLocal, JSON.stringify({
        prop_id: pidLocal, tract: synthTract, stored_at: Date.now(),
      }));
    } catch (e) { console.warn('tract_page_local stash failed:', e); }
    const tractUrl = `/acquisitions/tract/${encodeURIComponent(pidLocal)}` +
                     (d2.county ? `?county=${encodeURIComponent(d2.county)}` : '?') +
                     `${d2.county ? '&' : ''}from_corridors=1`;
    const popupHtml =
      `${ownerLine}<br><b>${acres.toLocaleString('en-US', { maximumFractionDigits: 1 })}</b> ac · ${escapeHtml(d2.county || '')}` +
      `<br>Mailing: ${escapeHtml(d2.mail_addr) || '<i>—</i>'}` +
      `<br>Site: ${escapeHtml(d2.situs_addr) || '<i>(no street address)</i>'}` +
      `<br>Prop ID: ${escapeHtml(pidLocal)}` +
      `<div class="popup-actions" style="margin-top:8px">` +
        `<button onclick='window.open(${JSON.stringify(tractUrl)}, "_blank")'>Full tract page</button>` +
        `<button onclick='showOutreach(${JSON.stringify(pidLocal)}, ${JSON.stringify(d2.owner_name || "")}, ${acres || 0}, ${JSON.stringify(d2.county || "")})'>Outreach</button>` +
      `</div>`;
    layer.bindPopup(popupHtml, { maxWidth: 380 });
    try {
      map.fitBounds(layer.getBounds().pad(0.40), { maxZoom: 17 });
      layer.openPopup();
    } catch {}
    setStatus(`Focused on <span class="accent">${escapeHtml(d2.owner_name || '?')}</span> · ${Math.round(acres)} ac (from corridors)`);
    cleanUrl();
    // Kick off ancillary-layer load in the background (floodplain, wetlands, MUDs,
    // streams, pipelines, etc.) so the layer-control toggles actually do something.
    _loadLayersAroundFocusTract(d2.geometry, d2.owner_name);
    return;
  }

  try {
    const r = await fetch(`/api/acq/tract-page-data/${encodeURIComponent(pid)}`);
    const d = await r.json();
    if (!r.ok || d.error) throw new Error(d.error || `HTTP ${r.status}`);

    const h = d.hcad || {};
    const m = d.mcad || {};
    const p = d.parcel ? d.parcel.properties : {};
    const owner = h.owner_name_1 || m.ownerName || p.OWNER_NAME || '(no owner on file)';
    const acres = h.Acreage ? parseFloat(String(h.Acreage).replace(' AC', '')) : (p.Acres || 0);
    const county = p._county || h.site_county || '';

    if (!d.parcel || !d.parcel.geometry) {
      setStatus(`Found ${owner} but no geometry on file — opening tract page instead.`);
      window.location.href = `/acquisitions/tract/${encodeURIComponent(pid)}`;
      return;
    }

    // Bright orange boundary so it pops against any basemap
    const layer = L.geoJSON(d.parcel.geometry, {
      style: { color: '#F25929', weight: 4, opacity: 1, fillColor: '#F25929', fillOpacity: 0.15 }
    }).addTo(map);
    window._focusTractLayer = layer;

    // Popup: owner / acres / address + quick actions
    let mailing = h.mail_addr_1
      ? [h.mail_addr_1, h.mail_city, h.mail_state, h.mail_zip].filter(Boolean).join(', ')
      : (p.MAIL_ADDR || '');
    let site = p.SITUS_ADDR || '';
    if (h.site_str_name) {
      const num = (h.site_str_num && h.site_str_num !== '0') ? h.site_str_num : '';
      site = [num, h.site_str_pfx, h.site_str_name, h.site_str_sfx].filter(s => s && String(s).trim()).join(' ').trim();
      const tail = [h.site_city, 'TX', h.site_zip].filter(Boolean).join(', ');
      site = [site, tail].filter(Boolean).join(', ');
    }
    const sourceBadge = (d.sources || [])[0] || '';

    const popupHtml =
      `<b style="font-size:13px">${escapeHtml(owner)}</b>` +
      (sourceBadge ? ` <span style="background:rgba(46,125,50,0.12);color:#2E7D32;font-size:9px;padding:1px 6px;border-radius:3px;font-weight:600;text-transform:uppercase;letter-spacing:0.04em">${escapeHtml(sourceBadge.includes('HCAD') ? 'HCAD' : sourceBadge.includes('MCAD') ? 'MCAD' : sourceBadge)}</span>` : '') +
      `<br><b>${(+acres).toLocaleString('en-US', { maximumFractionDigits: 1 })}</b> ac · ${escapeHtml(county || '')}` +
      `<br>Mailing: ${escapeHtml(mailing) || '<i>—</i>'}` +
      `<br>Site: ${escapeHtml(site) || '<i>(no street address)</i>'}` +
      `<br>Prop ID: ${escapeHtml(pid)}` +
      `<div class="popup-actions" style="margin-top:8px">` +
        `<button onclick='window.open("/acquisitions/tract/${encodeURIComponent(pid)}", "_blank")'>Full tract page</button>` +
        `<button onclick='showOutreach(${JSON.stringify(pid)}, ${JSON.stringify(owner)}, ${acres || 0}, ${JSON.stringify(county || "")})'>Outreach</button>` +
        `<button onclick='window.open("/api/acq/tract-sheet/${encodeURIComponent(pid)}", "_blank")'>Tract sheet</button>` +
      `</div>`;
    layer.bindPopup(popupHtml, { maxWidth: 380 });

    // Zoom + auto-open
    try {
      map.fitBounds(layer.getBounds().pad(0.40), { maxZoom: 17 });
      layer.openPopup();
    } catch (e) { console.warn('fitBounds failed', e); }

    setStatus(`Focused on <span class="accent">${escapeHtml(owner)}</span> · ${Math.round(acres)} ac`);
    // Kick off ancillary-layer load in the background (floodplain, wetlands, MUDs,
    // streams, pipelines, etc.) so the layer-control toggles actually do something.
    _loadLayersAroundFocusTract(d.parcel.geometry, owner);
  } catch (e) {
    console.error('Failed to focus tract', pid, e);
    setStatus(`Couldn't load tract ${pid}: ${e.message}`, true);
  } finally {
    const url = new URL(window.location.href);
    url.searchParams.delete('focus_tract');
    window.history.replaceState({}, '', url.pathname + (url.searchParams.toString() ? '?' + url.searchParams.toString() : ''));
  }
})();

// -----------------------------------------------------------------------
// Browse parcels (live)
// -----------------------------------------------------------------------
let browseTimer = null;
document.getElementById('browse-on').addEventListener('change', (e) => {
  if (e.target.checked) { overlays.browse.addTo(map); loadBrowseParcels(); }
  else map.removeLayer(overlays.browse);
});
map.on('moveend', () => {
  if (document.getElementById('browse-on').checked) {
    clearTimeout(browseTimer);
    browseTimer = setTimeout(loadBrowseParcels, 300);
  }
});
async function loadBrowseParcels() {
  if (map.getZoom() < 13) { overlays.browse.clearLayers(); return; }
  const b = map.getBounds();
  const url = `/api/acq/parcels?minx=${b.getWest()}&miny=${b.getSouth()}&maxx=${b.getEast()}&maxy=${b.getNorth()}`;
  try {
    const r = await fetch(url);
    const data = await r.json();
    overlays.browse.clearLayers();
    if (data.error) return;
    L.geoJSON(data, {
      style: { color: '#444', weight: 0.7, fillColor: '#999', fillOpacity: 0.08 },
      onEachFeature: (f, layer) => {
        const x = f.properties || {};
        const acres = parseFloat(x.GIS_AREA) || parseFloat(x.LEGAL_AREA) || 0;
        layer.bindPopup(
          `<b>${x.OWNER_NAME || '(no owner)'}</b><br>` +
          `Acres: ${acres.toFixed(1)}<br>` +
          `Site: ${x.SITUS_ADDR || ''}<br>` +
          `Mailing: ${x.MAIL_ADDR || ''}<br>` +
          `Prop ID: ${x.Prop_ID}`
        );
      },
    }).addTo(overlays.browse);
  } catch (e) { console.error('browse fetch failed', e); }
}
