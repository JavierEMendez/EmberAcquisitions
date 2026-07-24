/* ============================================================
   render.js — render report pages from data
   ============================================================ */

(function (global) {
  // ---------- helpers ----------
  const fmtUsd0 = n => '$' + Math.round(Number(n) || 0).toLocaleString();
  const fmtUsdM = n => '$' + (Number(n) / 1_000_000).toFixed(1) + 'M';
  const fmtUsdK = n => '$' + (Number(n) / 1_000).toFixed(0) + 'K';
  const fmtPct  = n => (Number(n) || 0).toFixed(0) + '%';
  const fmtNum  = n => Math.round(Number(n) || 0).toLocaleString();
  // Format a calendar date in UTC so a DATE column ('YYYY-MM-DD' stored as
  // UTC midnight by pg) doesn't shift to the previous day in the viewer's
  // local timezone.
  const fmtDate = iso => {
    if (!iso) return '—';
    const m = String(iso).match(/^(\d{4})-(\d{2})-(\d{2})/);
    if (m) {
      const d = new Date(Date.UTC(+m[1], +m[2] - 1, +m[3]));
      return d.toLocaleDateString('en-US', { month: 'short', day: 'numeric', year: 'numeric', timeZone: 'UTC' });
    }
    const d = new Date(iso);
    if (isNaN(d)) return iso;
    return d.toLocaleDateString('en-US', { month: 'short', day: 'numeric', year: 'numeric', timeZone: 'UTC' });
  };
  const todayStr = () => new Date().toLocaleDateString('en-US', { month: 'long', day: 'numeric', year: 'numeric' });
  const escapeHtml = s => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));

  // ---------- shared chrome ----------
  function pageHeader(project) {
    return `
      <div class="page-header">
        <div class="brand">
          <svg class="mark" viewBox="0 0 24 24" aria-hidden="true">
            <rect x="1"  y="9"  width="3" height="6"  rx="1.5" fill="#F25929"/>
            <rect x="6"  y="5"  width="3" height="14" rx="1.5" fill="#F25929"/>
            <rect x="11" y="2"  width="3" height="20" rx="1.5" fill="#F25929"/>
            <rect x="16" y="6"  width="3" height="12" rx="1.5" fill="#F25929"/>
            <rect x="21" y="10" width="3" height="4"  rx="1.5" fill="#F25929"/>
          </svg>
          <span class="wordmark">EMBER</span>
        </div>
        <div class="meta">${escapeHtml(project)} · Executive Report</div>
      </div>`;
  }
  function pageFooter(project, pageNum, totalPages, asOf) {
    return `
      <div class="page-footer">
        <span class="footer-project">${escapeHtml(project)}</span>
        <span>As of ${escapeHtml(asOf)}</span>
        <span>Page ${pageNum} of ${totalPages}</span>
      </div>`;
  }

  // ---------- charts (inline SVG) ----------
  function lineChart({ labels, series, height = 170, width = 720, yLabel = '' }) {
    // series: [{ name, color, data: [n|null,...] }]
    const padL = 36, padR = 14, padT = 14, padB = 26;
    const w = width, h = height;
    const innerW = w - padL - padR;
    const innerH = h - padT - padB;
    const all = series.flatMap(s => s.data.filter(v => v != null));
    const max = Math.max(...all, 1);
    const min = Math.min(0, Math.min(...all));
    const range = max - min || 1;
    const x = i => padL + (innerW * i / Math.max(labels.length - 1, 1));
    const y = v => padT + innerH - ((v - min) / range) * innerH;

    const gridLines = 4;
    const gridSvg = Array.from({ length: gridLines + 1 }, (_, i) => {
      const yy = padT + (innerH * i / gridLines);
      const val = max - (range * i / gridLines);
      return `<line x1="${padL}" x2="${w - padR}" y1="${yy}" y2="${yy}" stroke="#E5E6E7" stroke-width="1"/>
              <text x="${padL - 6}" y="${yy + 3}" text-anchor="end" font-size="9" fill="#939598" font-family="Helvetica Neue, sans-serif">${Math.round(val)}</text>`;
    }).join('');

    const labelsSvg = labels.map((lab, i) =>
      `<text x="${x(i)}" y="${h - 8}" text-anchor="middle" font-size="9" fill="#939598" font-family="Helvetica Neue, sans-serif">${escapeHtml(lab)}</text>`
    ).join('');

    const seriesSvg = series.map(s => {
      const pts = s.data.map((v, i) => v == null ? null : [x(i), y(v)]).filter(Boolean);
      if (!pts.length) return '';
      const d = pts.map((p, i) => (i === 0 ? 'M' : 'L') + p[0].toFixed(1) + ',' + p[1].toFixed(1)).join(' ');
      const dots = pts.map(p => `<circle cx="${p[0].toFixed(1)}" cy="${p[1].toFixed(1)}" r="3" fill="${s.color}"/>`).join('');
      return `<path d="${d}" fill="none" stroke="${s.color}" stroke-width="2.2" stroke-linejoin="round" stroke-linecap="round"/>${dots}`;
    }).join('');

    return `<svg viewBox="0 0 ${w} ${h}" width="100%" preserveAspectRatio="xMidYMid meet">${gridSvg}${seriesSvg}${labelsSvg}</svg>`;
  }

  function barChart({ labels, series, height = 170, width = 720 }) {
    // series: [{ name, color, data:[n,...] }]
    const padL = 36, padR = 14, padT = 14, padB = 26;
    const w = width, h = height;
    const innerW = w - padL - padR;
    const innerH = h - padT - padB;
    const groups = labels.length;
    const groupW = innerW / groups;
    const barW = (groupW - 8) / series.length;
    const max = Math.max(...series.flatMap(s => s.data), 1);
    const min = 0;
    const y = v => padT + innerH - ((v - min) / (max - min || 1)) * innerH;

    const gridLines = 4;
    const gridSvg = Array.from({ length: gridLines + 1 }, (_, i) => {
      const yy = padT + (innerH * i / gridLines);
      const val = max - (max * i / gridLines);
      return `<line x1="${padL}" x2="${w - padR}" y1="${yy}" y2="${yy}" stroke="#E5E6E7" stroke-width="1"/>
              <text x="${padL - 6}" y="${yy + 3}" text-anchor="end" font-size="9" fill="#939598" font-family="Helvetica Neue, sans-serif">${Math.round(val)}</text>`;
    }).join('');

    const bars = labels.map((lab, gi) => {
      const gx = padL + groupW * gi + 4;
      return series.map((s, si) => {
        const v = s.data[gi] || 0;
        const bx = gx + si * barW;
        const by = y(v);
        const bh = padT + innerH - by;
        return `<rect x="${bx.toFixed(1)}" y="${by.toFixed(1)}" width="${(barW - 2).toFixed(1)}" height="${bh.toFixed(1)}" fill="${s.color}" rx="2"/>`;
      }).join('');
    }).join('');

    const labelsSvg = labels.map((lab, i) =>
      `<text x="${(padL + groupW * i + groupW / 2).toFixed(1)}" y="${h - 8}" text-anchor="middle" font-size="9" fill="#939598" font-family="Helvetica Neue, sans-serif">${escapeHtml(lab)}</text>`
    ).join('');

    return `<svg viewBox="0 0 ${w} ${h}" width="100%" preserveAspectRatio="xMidYMid meet">${gridSvg}${bars}${labelsSvg}</svg>`;
  }

  function donut({ slices, size = 130 }) {
    // slices: [{ label, value, color }]
    const total = slices.reduce((a, s) => a + s.value, 0) || 1;
    const cx = size / 2, cy = size / 2, r = size * 0.42, ir = size * 0.27;
    let acc = -Math.PI / 2;
    const arcs = slices.map(s => {
      const ang = (s.value / total) * 2 * Math.PI;
      const a0 = acc, a1 = acc + ang; acc = a1;
      const x0 = cx + r * Math.cos(a0), y0 = cy + r * Math.sin(a0);
      const x1 = cx + r * Math.cos(a1), y1 = cy + r * Math.sin(a1);
      const x2 = cx + ir * Math.cos(a1), y2 = cy + ir * Math.sin(a1);
      const x3 = cx + ir * Math.cos(a0), y3 = cy + ir * Math.sin(a0);
      const large = ang > Math.PI ? 1 : 0;
      const d = `M${x0},${y0} A${r},${r} 0 ${large} 1 ${x1},${y1} L${x2},${y2} A${ir},${ir} 0 ${large} 0 ${x3},${y3} Z`;
      return `<path d="${d}" fill="${s.color}"/>`;
    }).join('');
    return `<svg viewBox="0 0 ${size} ${size}" width="${size}" height="${size}">${arcs}</svg>`;
  }

  // ---------- COVER ----------
  function coverPage({ title, subtitle, project, asOfStr, periodStr, heroImage, projectLogo }) {
    const heroStyle = heroImage
      ? `background-image: linear-gradient(180deg, rgba(8,35,59,0.15) 0%, rgba(8,35,59,0.55) 60%, rgba(8,35,59,0.85) 100%), url('${heroImage}'); background-size: cover; background-position: center;`
      : '';
    const coverClass = heroImage ? 'cover hero' : 'cover';
    const projectLogoHtml = projectLogo
      ? `<img class="cover-project-logo" src="${projectLogo}" alt="" />`
      : '';
    return `
      <section class="page ${coverClass}" style="${heroStyle}">
        ${heroImage ? '' : '<div class="cover-bars"><span></span><span></span><span></span><span></span><span></span></div>'}
        <div class="cover-inner">
          <div class="top-row">
            <svg width="34" height="34" viewBox="0 0 24 24" aria-hidden="true">
              <rect x="1"  y="9"  width="3" height="6"  rx="1.5" fill="#F25929"/>
              <rect x="6"  y="5"  width="3" height="14" rx="1.5" fill="#F25929"/>
              <rect x="11" y="2"  width="3" height="20" rx="1.5" fill="#F25929"/>
              <rect x="16" y="6"  width="3" height="12" rx="1.5" fill="#F25929"/>
              <rect x="21" y="10" width="3" height="4"  rx="1.5" fill="#F25929"/>
            </svg>
            <span class="wm">EMBER</span>
          </div>
          <div>
            ${projectLogoHtml}
            <div class="cover-eyebrow">Executive Report · ${escapeHtml(periodStr)}</div>
            <h1 class="cover-title">${escapeHtml(title)}</h1>
            <p class="cover-sub">${escapeHtml(subtitle)}</p>
          </div>
          <div class="cover-meta">
            <div class="item"><div class="label">Project</div><div class="value">${escapeHtml(project)}</div></div>
            <div class="item"><div class="label">Data as of</div><div class="value">${escapeHtml(asOfStr)}</div></div>
            <div class="item"><div class="label">Prepared</div><div class="value">${escapeHtml(todayStr())}</div></div>
            <div class="item"><div class="label">Confidential</div><div class="value">Internal Use Only</div></div>
          </div>
        </div>
      </section>`;
  }

  // ---------- DIVIDER ----------
  function dividerPage({ eyebrow, title, sub, heroImage, projectLogo }) {
    const heroStyle = heroImage
      ? `background-image: linear-gradient(180deg, rgba(8,35,59,0.10) 0%, rgba(8,35,59,0.50) 60%, rgba(8,35,59,0.85) 100%), url('${heroImage}'); background-size: cover; background-position: center;`
      : '';
    const cls = heroImage ? 'divider-page hero' : 'divider-page';
    const projectLogoHtml = projectLogo
      ? `<img class="div-project-logo" src="${projectLogo}" alt="" />`
      : '';
    return `
      <section class="page ${cls}" style="${heroStyle}">
        <div class="div-inner">
          ${projectLogoHtml}
          <div class="div-eyebrow">${escapeHtml(eyebrow)}</div>
          <h2>${escapeHtml(title)}</h2>
          <div class="div-sub">${escapeHtml(sub)}</div>
        </div>
        <div class="pill-bars"><span></span><span></span><span></span><span></span><span></span></div>
      </section>`;
  }

  // ---------- GALLERY (4-up image strip + caption) ----------
  function galleryPage({ project, eyebrow, title, lede, images, asOf, ctx }) {
    const tiles = images.map(img => `
      <div class="gal-tile">
        <img src="${img.src}" alt="${escapeHtml(img.alt || '')}" />
        ${img.caption ? `<div class="gal-cap">${escapeHtml(img.caption)}</div>` : ''}
      </div>`).join('');
    const page = `
      <section class="page">
        ${pageHeader(project)}
        <div class="page-body">
          <div class="eyebrow">${escapeHtml(eyebrow)}</div>
          <h2 class="section-title">${escapeHtml(title)}</h2>
          <div class="divider-bar"></div>
          ${lede ? `<p class="gal-lede">${escapeHtml(lede)}</p>` : ''}
          <div class="gallery-grid">${tiles}</div>
        </div>
        ${pageFooter(project, ctx.pageNum++, ctx.totalPages, asOf)}
      </section>`;
    return page;
  }

  // ---------- COLLAGE COVER ----------
  // Replaces the title-only hero cover with a magazine-style mosaic of every
  // unique project photo. Used as page 1 of every project section so each
  // section opens visually instead of with a single image.
  function collagePage({ project, eyebrow, title, sub, images, asOf, periodStr, ctx }) {
    // Dedupe by src, keep the first occurrence (preserves caption order).
    const seen = new Set();
    const uniq = [];
    for (const img of (images || [])) {
      if (!img || !img.src || seen.has(img.src)) continue;
      seen.add(img.src);
      uniq.push(img);
    }
    // Need at least 1 image to render a collage. Pad with the first image
    // duplicated only if the collage grid would otherwise have empty cells —
    // but we still display the deduped set; layout adapts to count.
    const n = uniq.length;
    const tiles = uniq.map((img, i) => `
      <figure class="collage-tile collage-tile-${i + 1}" style="background-image:url('${img.src}');" aria-label="${escapeHtml(img.caption || '')}"></figure>
    `).join('');
    return `
      <section class="page collage-cover collage-${Math.min(n, 8)}up">
        ${pageHeader(project)}
        <div class="page-body collage-body">
          <header class="collage-head">
            <div class="eyebrow">${escapeHtml(eyebrow || ('Executive Report · ' + (periodStr || '')))}</div>
            <h1 class="collage-title">${escapeHtml(title || project)}</h1>
            ${sub ? `<p class="collage-sub">${escapeHtml(sub)}</p>` : ''}
          </header>
          <div class="collage-grid">${tiles}</div>
        </div>
        ${pageFooter(project, ctx.pageNum++, ctx.totalPages, asOf)}
      </section>`;
  }

  // ---------- DEDUPED IMAGE SET PER PROJECT ----------
  // Combine hero + divider + gallery into a single uniq list (no repeats).
  // Used by collagePage().
  function projectPhotos(brand) {
    const pool = [];
    if (brand.hero)    pool.push({ src: brand.hero,    caption: 'Project hero' });
    if (brand.divider) pool.push({ src: brand.divider, caption: '' });
    for (const img of (brand.gallery || [])) pool.push(img);
    const seen = new Set();
    const out = [];
    for (const img of pool) {
      if (!img || !img.src || seen.has(img.src)) continue;
      seen.add(img.src);
      out.push(img);
    }
    return out;
  }

  // ---------- BRAND ASSETS PER PROJECT ----------
  const BRAND = {
    lighthaven: {
      logo: 'assets/lighthaven-logo.png',
      hero: 'assets/lh-pool-clubhouse.jpg',
      divider: 'assets/lh-entry.jpg',
      gallery: [
        { src: 'assets/lh-aerial-2.jpg',     caption: 'Aerial — 248 units across three phases' },
        { src: 'assets/lh-clubhouse.jpg',    caption: 'Clubhouse and leasing office' },
        { src: 'assets/lh-pool-aerial.jpg',  caption: 'Resort pool & cabana' },
        { src: 'assets/lh-pool-2.jpg',       caption: 'Pool deck and gathering' },
        { src: 'assets/lh-amenity-aerial.jpg', caption: 'Amenity center, aerial' },
        { src: 'assets/lh-playground-1.jpg', caption: 'Outdoor play & shade structure' },
      ],
    },
    hawthorne: {
      logo: 'assets/hw-logo.webp',
      hero: 'assets/hw-pool.jpg',
      divider: 'assets/hw-concierge.jpg',
      gallery: [
        { src: 'assets/hw-pool.jpg',             caption: 'Rooftop pool deck overlooking Uptown' },
        { src: 'assets/hw-concierge.jpg',        caption: 'Lobby and concierge — limestone and Verde marble' },
        { src: 'assets/hw-lounge-1.jpg',         caption: 'Residents’ lounge — dining and gathering' },
        { src: 'assets/hw-lounge-2.jpg',         caption: 'Residents’ lounge — terrace and bar' },
        { src: 'assets/hw-claymore-living.jpg',  caption: 'The Claymore — open-plan living, kitchen, and dining' },
        { src: 'assets/hw-claymore-kitchen.jpg', caption: 'The Claymore — chef’s kitchen with calacatta surrounds' },
        { src: 'assets/hw-greenbay-mainliving.jpg', caption: 'The Greenbay — main living with skyline views' },
        { src: 'assets/hw-claymore-bedroom-1.jpg', caption: 'The Claymore — primary suite' },
      ],
    },
    tgp: {
      hero: 'assets/tgp-aerial.jpg',
      divider: 'assets/tgp-aerial.jpg',
    },
  };

  // ---------- LIGHTHAVEN ----------
  function lighthavenPages(d, ctx) {
    const pages = [];
    const asOf = fmtDate(d.lastImport.lastImportAt);
    const proj = 'LightHaven District West';

    // Page: Photo collage cover — every unique LH photo, no repeats.
    pages.push(collagePage({
      project: proj,
      eyebrow: 'Built to Rent · Lease-Up',
      title: 'LightHaven District West',
      sub: '248 build-to-rent homes across three phases in District West, Houston — a gated, amenity-rich community with resort pool, clubhouse, and shaded play areas.',
      images: projectPhotos(BRAND.lighthaven),
      asOf, ctx,
    }));

    // Page: Snapshot
    const occ = d.occupancy;
    const occupiedCount = (occ.counts['Tenant Occupied'] || 0) + (occ.counts['Renewal'] || 0);
    const leasedCount   = (occ.counts['Leased'] || 0);
    const availableCount= (occ.counts['Turned'] || 0) + (occ.counts['AVAILABLE'] || 0);

    pages.push(`
      <section class="page">
        ${pageHeader(proj)}
        <div class="page-body">
          <div class="eyebrow">Occupancy &amp; Leasing</div>
          <h2 class="section-title">Portfolio at a glance</h2>
          <div class="divider-bar"></div>

          <div class="kpi-grid" style="margin-bottom:14px;">
            <div class="kpi dark">
              <div class="kpi-label">Total Units</div>
              <div class="kpi-value">${fmtNum(occ.total)}</div>
              <div class="kpi-foot">Across 3 phases</div>
            </div>
            <div class="kpi accent">
              <div class="kpi-label">Leased</div>
              <div class="kpi-value">${fmtPct(occ.leasedPct)}</div>
              <div class="kpi-foot">${fmtNum(occupiedCount + leasedCount)} units</div>
            </div>
            <div class="kpi">
              <div class="kpi-label">Occupied</div>
              <div class="kpi-value">${fmtPct(occ.occupiedPct)}</div>
              <div class="kpi-foot">${fmtNum(occupiedCount)} tenants in place</div>
            </div>
            <div class="kpi">
              <div class="kpi-label">Available</div>
              <div class="kpi-value">${fmtNum(availableCount)}</div>
              <div class="kpi-foot">${fmtPct(occ.availablePct)} of inventory</div>
            </div>
          </div>

          <div class="row cols-2-1" style="margin-bottom:14px;">
            <div class="chart-card">
              <div class="chart-head">
                <h4>Leasing pace — cumulative units leased</h4>
                <div class="legend"><span class="l-budget">Budget</span><span class="l-actual">Actual</span></div>
              </div>
              ${lineChart({
                labels: d.leasingPace.months,
                series: [
                  { name: 'Budget', color: '#13344E', data: d.leasingPace.budget },
                  { name: 'Actual', color: '#F25929', data: d.leasingPace.actual },
                ],
                height: 200, width: 480,
              })}
            </div>
            <div class="notes-block">
              <h5>Executive summary</h5>
              <p>${escapeHtml((d.comments.executive_summary && d.comments.executive_summary.body) || '')}</p>
            </div>
          </div>

          <div class="chart-card">
            <div class="chart-head">
              <h4>NOI — Budget vs. Forecast (${d.noi.year || ''})</h4>
              <div class="legend"><span class="l-budget">Budget</span><span class="l-forecast">Forecast</span></div>
            </div>
            ${barChart({
              labels: d.noi.months,
              series: [
                { name: 'Budget',   color: '#13344E', data: d.noi.budget },
                { name: 'Forecast', color: '#F25929', data: d.noi.forecast.map(v => v ?? 0) },
              ],
              height: 170, width: 720,
            })}
          </div>
        </div>
        ${pageFooter(proj, ctx.pageNum++, ctx.totalPages, asOf)}
      </section>
    `);

    // Page: Floorplan rollup + rents
    const rents = Object.entries(d.rents.byFloorplan);
    pages.push(`
      <section class="page">
        ${pageHeader(proj)}
        <div class="page-body">
          <div class="eyebrow">Plans &amp; Rents</div>
          <h2 class="section-title">Floorplan absorption &amp; rent positioning</h2>
          <div class="divider-bar"></div>

          <div class="row cols-2" style="margin-bottom:14px;">
            <div class="chart-card">
              <div class="chart-head"><h4>Leased vs. total by floorplan</h4></div>
              ${d.phaseRollup.map(r => `
                <div class="bar-row">
                  <div class="label">${escapeHtml(r.fp)}</div>
                  <div class="track"><div class="fill" style="width:${(100 * r.leased / r.total).toFixed(0)}%;"></div></div>
                  <div class="num">${r.leased}/${r.total}</div>
                </div>`).join('')}
            </div>
            <div class="chart-card">
              <div class="chart-head"><h4>Average rent by floorplan</h4></div>
              <table class="report">
                <thead><tr><th>Plan</th><th class="num">Avg</th><th class="num">Min</th><th class="num">Max</th><th class="num">Units</th></tr></thead>
                <tbody>
                  ${rents.map(([fp, r]) => `
                    <tr><td><strong>${escapeHtml(fp)}</strong></td>
                      <td class="num">${fmtUsd0(r.avg)}</td>
                      <td class="num">${fmtUsd0(r.min)}</td>
                      <td class="num">${fmtUsd0(r.max)}</td>
                      <td class="num">${r.units}</td>
                    </tr>`).join('')}
                </tbody>
              </table>
            </div>
          </div>

          <div class="row cols-2-1" style="margin-bottom:0;">
            <div class="chart-card">
              <div class="chart-head"><h4>Weekly traffic &amp; leads</h4>
                <div class="legend"><span class="l-budget">Visitors</span><span class="l-actual">Leads</span></div>
              </div>
              ${barChart({
                labels: d.traffic.weeks,
                series: [
                  { name: 'Visitors', color: '#13344E', data: d.traffic.visitors },
                  { name: 'Leads',    color: '#F25929', data: d.traffic.leads },
                ],
                height: 160, width: 480,
              })}
            </div>
            <div class="chart-card">
              <div class="chart-head"><h4>Lead sources</h4></div>
              <div style="display:flex;align-items:center;gap:14px;">
                ${donut({
                  slices: Object.entries(d.traffic.sources).map(([k, v], i) => ({
                    label: k, value: v,
                    color: ['#F25929','#13344E','#58595B','#D1D3D4','#F8906A'][i % 5],
                  })),
                  size: 110,
                })}
                <div style="flex:1;font-size:10px;line-height:1.7;">
                  ${Object.entries(d.traffic.sources).map(([k, v], i) => `
                    <div style="display:flex;align-items:center;gap:6px;">
                      <span style="width:8px;height:8px;border-radius:2px;background:${['#F25929','#13344E','#58595B','#D1D3D4','#F8906A'][i % 5]};display:inline-block;"></span>
                      <span style="flex:1;color:#3F4041;">${escapeHtml(k)}</span>
                      <strong style="color:#13344E;">${v}</strong>
                    </div>`).join('')}
                </div>
              </div>
            </div>
          </div>

          <div class="notes-block" style="margin-top:14px;">
            <h5>Leasing &amp; ops commentary</h5>
            <p><strong>Strategy:</strong> ${escapeHtml((d.comments.leasing_strategy && d.comments.leasing_strategy.body) || '')}</p>
            <p><strong>Operations:</strong> ${escapeHtml((d.comments.ops_notes && d.comments.ops_notes.body) || '')}</p>
          </div>
        </div>
        ${pageFooter(proj, ctx.pageNum++, ctx.totalPages, asOf)}
      </section>
    `);

    return pages;
  }

  // ---------- HAWTHORNE ----------
  function hawthornePages(d, ctx) {
    const pages = [];
    const asOf = fmtDate(d.lastImport.lastImportAt);
    const proj = 'The Hawthorne';
    const s = d.sales;

    // Page: Photo collage cover — every unique Hawthorne photo, no repeats.
    pages.push(collagePage({
      project: proj,
      eyebrow: 'For-Sale Condominium · Sales',
      title: 'The Hawthorne',
      sub: 'A 67-residence boutique condominium in Uptown Houston — limestone, Verde marble, and floor-to-ceiling glass framing skyline and treetop views, with concierge service, rooftop pool, and a residents-only lounge.',
      images: projectPhotos(BRAND.hawthorne),
      asOf, ctx,
    }));

    pages.push(`
      <section class="page">
        ${pageHeader(proj)}
        <div class="page-body">
          <div class="eyebrow">Sales &amp; Revenue</div>
          <h2 class="section-title">Sales pace &amp; closings</h2>
          <div class="divider-bar"></div>

          <div class="kpi-grid" style="margin-bottom:14px;">
            <div class="kpi dark">
              <div class="kpi-label">Total Units</div>
              <div class="kpi-value">${s.total.actual}<span style="font-size:14px;color:rgba(255,255,255,0.6);font-weight:500;">/${s.total.budget}</span></div>
              <div class="kpi-foot">${fmtPct(100 * s.total.actual / s.total.budget)} of plan</div>
            </div>
            <div class="kpi accent">
              <div class="kpi-label">Closed</div>
              <div class="kpi-value">${s.closed.actual}</div>
              <div class="kpi-foot">Budget ${s.closed.budget} · ${fmtPct(100 * s.closed.actual / s.closed.budget)}</div>
            </div>
            <div class="kpi">
              <div class="kpi-label">Sold, Not Closed</div>
              <div class="kpi-value">${s.soldNotClosed.actual}</div>
              <div class="kpi-foot">Budget ${s.soldNotClosed.budget}</div>
            </div>
            <div class="kpi">
              <div class="kpi-label">Available</div>
              <div class="kpi-value">${s.available.actual}</div>
              <div class="kpi-foot">Budget ${s.available.budget}</div>
            </div>
          </div>

          <div class="row cols-2" style="margin-bottom:14px;">
            <div class="chart-card">
              <div class="chart-head">
                <h4>Sales by month</h4>
                <div class="legend"><span class="l-budget">Closed</span><span class="l-actual">SNC</span></div>
              </div>
              ${barChart({
                labels: d.salesByMonth.months,
                series: [
                  { name: 'Closed', color: '#13344E', data: d.salesByMonth.closed },
                  { name: 'SNC',    color: '#F25929', data: d.salesByMonth.snc },
                ],
                height: 170, width: 480,
              })}
            </div>
            <div class="chart-card">
              <div class="chart-head"><h4>Stacking plan</h4>
                <div class="legend">
                  <span style="font-size:9px;"><span style="display:inline-block;width:10px;height:10px;background:#13344E;border-radius:2px;margin-right:4px;vertical-align:middle;"></span>Closed</span>
                  <span style="font-size:9px;"><span style="display:inline-block;width:10px;height:10px;background:#F25929;border-radius:2px;margin-right:4px;vertical-align:middle;"></span>SNC</span>
                  <span style="font-size:9px;"><span style="display:inline-block;width:10px;height:10px;background:#D1D3D4;border-radius:2px;margin-right:4px;vertical-align:middle;"></span>Available</span>
                </div>
              </div>
              <div class="stacking" style="height:170px;">
                ${d.stacking.map((floor, fi) => {
                  // Snapshot now sends {level, cells}; tolerate the legacy
                  // array shape (and derive a descending floor number) so a
                  // stale cached payload still renders.
                  const cells = Array.isArray(floor) ? floor : (floor.cells || []);
                  const level = Array.isArray(floor) ? (d.stacking.length - fi) : floor.level;
                  return `
                  <div class="floor" style="grid-template-columns:repeat(${cells.length}, 1fr);">
                    ${cells.map((u, ui) => `<div class="unit s-${u}" title="Floor ${level}">${level}-${(ui + 1).toString().padStart(2,'0')}</div>`).join('')}
                  </div>`;
                }).join('')}
              </div>
            </div>
          </div>

          <div class="notes-block">
            <h5>Executive summary</h5>
            <p>${escapeHtml((d.comments.executive_summary && d.comments.executive_summary.body) || '')}</p>
          </div>
        </div>
        ${pageFooter(proj, ctx.pageNum++, ctx.totalPages, asOf)}
      </section>
    `);

    pages.push(`
      <section class="page">
        ${pageHeader(proj)}
        <div class="page-body">
          <div class="eyebrow">Floorplans &amp; Proforma</div>
          <h2 class="section-title">Plan-level absorption &amp; financial summary</h2>
          <div class="divider-bar"></div>

          <div class="chart-card" style="margin-bottom:14px;">
            <div class="chart-head"><h4>Floorplan rollup</h4></div>
            <table class="report">
              <thead>
                <tr>
                  <th>Floorplan</th>
                  <th class="num">Total</th>
                  <th class="num">Closed</th>
                  <th class="num">SNC</th>
                  <th class="num">Available</th>
                  <th class="num">Avg Price</th>
                  <th class="num">Sold %</th>
                </tr>
              </thead>
              <tbody>
                ${d.floorplans.map(fp => {
                  const sold = fp.closed + fp.snc;
                  const pct = 100 * sold / fp.total;
                  return `<tr>
                    <td><strong>${escapeHtml(fp.fp)}</strong></td>
                    <td class="num">${fp.total}</td>
                    <td class="num">${fp.closed}</td>
                    <td class="num">${fp.snc}</td>
                    <td class="num">${fp.available}</td>
                    <td class="num">${fmtUsd0(fp.avgPrice)}</td>
                    <td class="num"><span class="pill ${pct >= 80 ? 'green' : pct >= 60 ? 'amber' : 'red'}">${fmtPct(pct)}</span></td>
                  </tr>`;
                }).join('')}
                <tr class="total">
                  <td>Total</td>
                  <td class="num">${d.floorplans.reduce((a,f)=>a+f.total,0)}</td>
                  <td class="num">${d.floorplans.reduce((a,f)=>a+f.closed,0)}</td>
                  <td class="num">${d.floorplans.reduce((a,f)=>a+f.snc,0)}</td>
                  <td class="num">${d.floorplans.reduce((a,f)=>a+f.available,0)}</td>
                  <td class="num">—</td>
                  <td class="num">—</td>
                </tr>
              </tbody>
            </table>
          </div>

          <div class="chart-card" style="margin-bottom:14px;">
            <div class="chart-head"><h4>Proforma · Budget vs. Actual vs. Forecast ($M)</h4></div>
            <table class="report">
              <thead>
                <tr>
                  <th>Line item</th>
                  <th class="num">Budget</th>
                  <th class="num">Actual</th>
                  <th class="num">Forecast</th>
                  <th class="num">Variance</th>
                </tr>
              </thead>
              <tbody>
                ${d.proforma.map(p => {
                  const variance = p.forecast - p.budget;
                  const isFav = (p.line.toLowerCase().includes('revenue') || p.line.toLowerCase().includes('proceeds')) ? variance >= 0 : variance <= 0;
                  // Rows carry a unit: '$M' (millions) or '$' (raw dollars, e.g.
                  // revenue per SF). Legacy/mock rows omit it → default '$M'.
                  const unit = p.unit || '$M';
                  const fmtV   = (x) => unit === '$M' ? ('$' + x.toFixed(2) + 'M') : fmtUsd0(x);
                  const fmtVar = (x) => (x >= 0 ? '+' : '−') + (unit === '$M' ? ('$' + Math.abs(x).toFixed(2) + 'M') : fmtUsd0(Math.abs(x)));
                  return `<tr>
                    <td>${escapeHtml(p.line)}</td>
                    <td class="num">${fmtV(p.budget)}</td>
                    <td class="num">${fmtV(p.actual)}</td>
                    <td class="num">${fmtV(p.forecast)}</td>
                    <td class="num"><span style="color:${isFav ? '#1F7A4D' : '#C0311A'};font-weight:700;">${fmtVar(variance)}</span></td>
                  </tr>`;
                }).join('')}
              </tbody>
            </table>
          </div>

          <div class="notes-block">
            <h5>Sales commentary</h5>
            <p>${escapeHtml((d.comments.sales_notes && d.comments.sales_notes.body) || '')}</p>
          </div>
        </div>
        ${pageFooter(proj, ctx.pageNum++, ctx.totalPages, asOf)}
      </section>
    `);

    return pages;
  }

  // ---------- TGP ----------
  function tgpPages(d, ctx) {
    const pages = [];
    const asOf = fmtDate(d.lastImport.lastImportAt);
    const proj = 'The Shops at Grand Prairie';
    const p = d.psr;
    const ov = p.overview;

    // Page: Photo collage cover — every unique TGP photo (currently aerials).
    pages.push(collagePage({
      project: proj,
      eyebrow: 'Retail Development · HPI / Ember JV',
      title: 'The Shops at Grand Prairie',
      sub: 'A retail center + pad sites development in Grand Prairie, TX — tracked end-to-end via budget, schedule, GMP, risk, and pre-leasing.',
      images: projectPhotos(BRAND.tgp),
      asOf, ctx,
    }));

    pages.push(`
      <section class="page">
        ${pageHeader(proj)}
        <div class="page-body">
          <div class="eyebrow">Project Status Report</div>
          <h2 class="section-title">Development snapshot</h2>
          <div class="divider-bar"></div>

          <div class="kpi-grid cols-3" style="margin-bottom:14px;">
            <div class="kpi dark">
              <div class="kpi-label">Total Budget</div>
              <div class="kpi-value">${fmtUsdM(ov.totalBudget)}</div>
              <div class="kpi-foot">GMP ${fmtUsdM(p.gmp.gmpAmount)}</div>
            </div>
            <div class="kpi accent">
              <div class="kpi-label">Overall Status</div>
              <div class="kpi-value" style="font-size:18px;line-height:1.2;">${escapeHtml(ov.status)}</div>
              <div class="kpi-foot">14-day weather impact, recovery plan in motion</div>
            </div>
            <div class="kpi">
              <div class="kpi-label">Pre-Leased</div>
              <div class="kpi-value">${fmtPct(p.leaseUp.preLeasedPct)}</div>
              <div class="kpi-foot">${fmtNum(p.leaseUp.preLeasedSf)} of ${fmtNum(p.leaseUp.nrsf)} NRSF</div>
            </div>
          </div>

          <div class="kpi-grid cols-3" style="margin-bottom:14px;">
            <div class="kpi"><div class="kpi-label">NRSF</div><div class="kpi-value">${fmtNum(ov.nrsf)}</div><div class="kpi-foot">Net rentable square feet</div></div>
            <div class="kpi"><div class="kpi-label">Pad Acres</div><div class="kpi-value">${ov.padAcres.toFixed(1)}</div><div class="kpi-foot">3 pad sites</div></div>
            <div class="kpi"><div class="kpi-label">Cost / NRSF</div><div class="kpi-value">$${fmtNum(ov.costPerNrsf)}</div><div class="kpi-foot">Cost / acre ${fmtUsdM(ov.costPerAcre)}</div></div>
          </div>

          <div class="row cols-2-1">
            <div class="chart-card">
              <div class="chart-head"><h4>Project information</h4></div>
              <table class="report">
                <tbody>
                  ${p.info.map(r => `<tr><td style="color:#939598;width:38%;">${escapeHtml(r.k)}</td><td><strong>${escapeHtml(r.v)}</strong></td></tr>`).join('')}
                </tbody>
              </table>
            </div>
            <div class="chart-card">
              <div class="chart-head"><h4>Key team</h4></div>
              <table class="report">
                <tbody>
                  ${p.team.map(t => `<tr><td style="color:#939598;width:55%;">${escapeHtml(t.role)}</td><td><strong>${escapeHtml(t.name)}</strong></td></tr>`).join('')}
                </tbody>
              </table>
            </div>
          </div>

          <div class="notes-block" style="margin-top:14px;">
            <h5>Executive summary</h5>
            <p>${escapeHtml(p.executiveSummary)}</p>
          </div>
        </div>
        ${pageFooter(proj, ctx.pageNum++, ctx.totalPages, asOf)}
      </section>
    `);

    // Budget page
    const totals = p.budget.reduce((a, r) => {
      a.retail += r.retail; a.pads += r.pads; a.realloc += r.realloc;
      a.spent += r.spent; a.balance += r.balance;
      return a;
    }, { retail:0, pads:0, realloc:0, spent:0, balance:0 });
    const totBudget = totals.retail + totals.pads;
    const totAdj = totBudget + totals.realloc;
    pages.push(`
      <section class="page">
        ${pageHeader(proj)}
        <div class="page-body">
          <div class="eyebrow">Budget</div>
          <h2 class="section-title">Cost &amp; commitments</h2>
          <div class="divider-bar"></div>

          <div class="chart-card">
            <table class="report">
              <thead>
                <tr>
                  <th>Line item</th>
                  <th class="num">Retail</th>
                  <th class="num">Pads</th>
                  <th class="num">Total</th>
                  <th class="num">Realloc.</th>
                  <th class="num">Adjusted</th>
                  <th class="num">Spent</th>
                  <th class="num">Balance</th>
                  <th class="num">% Cmpl</th>
                </tr>
              </thead>
              <tbody>
                ${p.budget.map(r => {
                  const tot = r.retail + r.pads;
                  const adj = tot + r.realloc;
                  return `<tr>
                    <td><strong>${escapeHtml(r.line)}</strong></td>
                    <td class="num">${fmtUsdK(r.retail)}</td>
                    <td class="num">${fmtUsdK(r.pads)}</td>
                    <td class="num">${fmtUsdK(tot)}</td>
                    <td class="num" style="color:${r.realloc < 0 ? '#C0311A' : r.realloc > 0 ? '#1F7A4D' : '#939598'};">${r.realloc === 0 ? '—' : (r.realloc > 0 ? '+' : '') + fmtUsdK(r.realloc)}</td>
                    <td class="num">${fmtUsdK(adj)}</td>
                    <td class="num">${fmtUsdK(r.spent)}</td>
                    <td class="num">${fmtUsdK(r.balance)}</td>
                    <td class="num"><span class="pill ${r.pct >= 70 ? 'green' : r.pct >= 30 ? 'amber' : 'gray'}">${fmtPct(r.pct)}</span></td>
                  </tr>`;
                }).join('')}
                <tr class="total">
                  <td>Total</td>
                  <td class="num">${fmtUsdK(totals.retail)}</td>
                  <td class="num">${fmtUsdK(totals.pads)}</td>
                  <td class="num">${fmtUsdK(totBudget)}</td>
                  <td class="num">${totals.realloc === 0 ? '—' : (totals.realloc > 0 ? '+' : '') + fmtUsdK(totals.realloc)}</td>
                  <td class="num">${fmtUsdK(totAdj)}</td>
                  <td class="num">${fmtUsdK(totals.spent)}</td>
                  <td class="num">${fmtUsdK(totals.balance)}</td>
                  <td class="num">—</td>
                </tr>
              </tbody>
            </table>
          </div>

          <div class="kpi-grid cols-3" style="margin-top:14px;">
            <div class="kpi">
              <div class="kpi-label">GMP Buyout</div>
              <div class="kpi-value">${fmtPct(p.gmp.buyoutPct)}</div>
              <div class="kpi-foot">${p.gmp.buyoutVariance < 0 ? 'Favorable ' : 'Over '} ${fmtUsdK(Math.abs(p.gmp.buyoutVariance))}</div>
            </div>
            <div class="kpi">
              <div class="kpi-label">Contingency Used</div>
              <div class="kpi-value">${fmtUsdK(p.gmp.contingencyUsed)}</div>
              <div class="kpi-foot">Opening ${fmtUsdK(p.gmp.contingencyOpening)}</div>
            </div>
            <div class="kpi accent">
              <div class="kpi-label">Projected Contingency Balance</div>
              <div class="kpi-value">${fmtUsdK(p.gmp.contingencyProjected)}</div>
              <div class="kpi-foot">After known + pending COs</div>
            </div>
          </div>
        </div>
        ${pageFooter(proj, ctx.pageNum++, ctx.totalPages, asOf)}
      </section>
    `);

    // Schedule + GMP page
    pages.push(`
      <section class="page">
        ${pageHeader(proj)}
        <div class="page-body">
          <div class="eyebrow">Schedule &amp; Risk</div>
          <h2 class="section-title">Milestones, change orders &amp; contingency</h2>
          <div class="divider-bar"></div>

          <div class="chart-card" style="margin-bottom:14px;">
            <div class="chart-head"><h4>Project milestones</h4></div>
            <table class="report">
              <thead><tr><th>Milestone</th><th>Target</th><th>Actual</th><th class="num">Var. (days)</th><th>Status</th></tr></thead>
              <tbody>
                ${p.schedule.map(m => `
                  <tr>
                    <td><strong>${escapeHtml(m.milestone)}</strong></td>
                    <td>${escapeHtml(fmtDate(m.target))}</td>
                    <td>${m.actual ? escapeHtml(fmtDate(m.actual)) : '—'}</td>
                    <td class="num" style="color:${m.variance > 0 ? '#C0311A' : m.variance < 0 ? '#1F7A4D' : '#939598'};">${m.variance > 0 ? '+' : ''}${m.variance || '—'}</td>
                    <td><span class="pill ${m.status}">${escapeHtml(m.note)}</span></td>
                  </tr>`).join('')}
              </tbody>
            </table>
          </div>

          <div class="row cols-2">
            <div class="chart-card">
              <div class="chart-head"><h4>Leasing &amp; marketing milestones</h4></div>
              <table class="report">
                <thead><tr><th>Milestone</th><th>Target</th><th>Actual</th><th>Status</th></tr></thead>
                <tbody>
                  ${p.leasingMilestones.map(m => `
                    <tr>
                      <td><strong>${escapeHtml(m.milestone)}</strong></td>
                      <td>${escapeHtml(fmtDate(m.target))}</td>
                      <td>${m.actual ? escapeHtml(fmtDate(m.actual)) : '—'}</td>
                      <td><span class="pill ${m.status}">${m.variance < 0 ? `${Math.abs(m.variance)}d ahead` : m.variance > 0 ? `${m.variance}d late` : 'On target'}</span></td>
                    </tr>`).join('')}
                </tbody>
              </table>
            </div>
            <div class="chart-card">
              <div class="chart-head"><h4>Change order log</h4></div>
              <table class="report">
                <thead><tr><th>#</th><th>Description</th><th class="num">Amount</th><th>Status</th></tr></thead>
                <tbody>
                  ${p.gmp.changeOrders.map(co => `
                    <tr>
                      <td><strong>${escapeHtml(co.num)}</strong></td>
                      <td>${escapeHtml(co.desc)}</td>
                      <td class="num">${fmtUsdK(co.amount)}</td>
                      <td><span class="pill ${co.status === 'Approved' ? 'green' : 'amber'}">${escapeHtml(co.status)}</span></td>
                    </tr>`).join('')}
                  <tr class="total">
                    <td colspan="2">Total log</td>
                    <td class="num">${fmtUsdK(p.gmp.changeOrders.reduce((a,c)=>a+c.amount,0))}</td>
                    <td>—</td>
                  </tr>
                </tbody>
              </table>
            </div>
          </div>
        </div>
        ${pageFooter(proj, ctx.pageNum++, ctx.totalPages, asOf)}
      </section>
    `);

    // Lease-up + actions
    pages.push(`
      <section class="page">
        ${pageHeader(proj)}
        <div class="page-body">
          <div class="eyebrow">Lease-Up &amp; Actions</div>
          <h2 class="section-title">Tenants, action items &amp; issues</h2>
          <div class="divider-bar"></div>

          <div class="chart-card" style="margin-bottom:14px;">
            <div class="chart-head"><h4>Tenant &amp; lease-up tracker</h4>
              <div class="legend"><span style="font-size:10px;color:#13344E;font-weight:700;">${fmtPct(p.leaseUp.preLeasedPct)} pre-leased of ${fmtNum(p.leaseUp.nrsf)} NRSF</span></div>
            </div>
            <table class="report">
              <thead><tr><th>Tenant</th><th>Type</th><th class="num">SF</th><th>Status</th></tr></thead>
              <tbody>
                ${p.leaseUp.tenants.map(t => `
                  <tr>
                    <td><strong>${escapeHtml(t.tenant)}</strong></td>
                    <td>${escapeHtml(t.type)}</td>
                    <td class="num">${fmtNum(t.sf)}</td>
                    <td><span class="pill ${t.pct}">${escapeHtml(t.status)}</span></td>
                  </tr>`).join('')}
              </tbody>
            </table>
          </div>

          <div class="row cols-2">
            <div class="chart-card">
              <div class="chart-head"><h4>Upcoming action items</h4></div>
              <table class="report">
                <thead><tr><th>Item</th><th>Owner</th><th>Due</th><th>Pri</th></tr></thead>
                <tbody>
                  ${p.actionItems.map(a => `
                    <tr>
                      <td>${escapeHtml(a.item)}</td>
                      <td style="color:#58595B;">${escapeHtml(a.owner)}</td>
                      <td style="color:#58595B;">${escapeHtml(a.due)}</td>
                      <td><span class="pill ${a.priority === 'High' ? 'red' : 'amber'}">${escapeHtml(a.priority)}</span></td>
                    </tr>`).join('')}
                </tbody>
              </table>
            </div>
            <div class="chart-card">
              <div class="chart-head"><h4>Issues &amp; concerns</h4></div>
              <table class="report">
                <thead><tr><th>Issue</th><th>Impact</th><th>Sev.</th></tr></thead>
                <tbody>
                  ${p.issues.map(i => `
                    <tr>
                      <td>${escapeHtml(i.issue)}</td>
                      <td style="color:#58595B;">${escapeHtml(i.impact)}</td>
                      <td><span class="pill ${i.severity}">${i.severity === 'red' ? 'High' : i.severity === 'amber' ? 'Med' : 'Low'}</span></td>
                    </tr>`).join('')}
                </tbody>
              </table>
            </div>
          </div>

          <div class="notes-block" style="margin-top:14px;">
            <h5>Owner commentary</h5>
            <p>${escapeHtml((d.comments.executive_summary && d.comments.executive_summary.body) || '')}</p>
          </div>
        </div>
        ${pageFooter(proj, ctx.pageNum++, ctx.totalPages, asOf)}
      </section>
    `);

    return pages;
  }

  // ---------- PORTFOLIO COVER + TOC + SUMMARY ----------
  function portfolioOverview({ all, ctx }) {
    const lh = all.lighthaven, hw = all.hawthorne, tgp = all.tgp;
    const asOf = fmtDate([lh.lastImport.lastImportAt, hw.lastImport.lastImportAt, tgp.lastImport.lastImportAt].sort().slice(-1)[0]);

    return `
      <section class="page">
        ${pageHeader('Portfolio')}
        <div class="page-body">
          <div class="eyebrow">Portfolio</div>
          <h2 class="section-title">Three projects, one view</h2>
          <div class="divider-bar"></div>

          <div class="row cols-3" style="margin-bottom:14px;">
            <div class="chart-card">
              <div class="chart-head"><h4>LightHaven District West</h4></div>
              <div class="kpi" style="border:none;padding:6px 0;">
                <div class="kpi-label">Leased</div>
                <div class="kpi-value">${fmtPct(lh.occupancy.leasedPct)}</div>
                <div class="kpi-foot">${fmtNum(lh.occupancy.total)} units · ${fmtPct(lh.occupancy.occupiedPct)} occupied</div>
              </div>
              <p style="font-size:10px;color:#58595B;line-height:1.5;margin-top:8px;">Built-to-rent lease-up tracking ahead of pace; concession pressure easing.</p>
            </div>
            <div class="chart-card">
              <div class="chart-head"><h4>The Hawthorne</h4></div>
              <div class="kpi" style="border:none;padding:6px 0;">
                <div class="kpi-label">Closed</div>
                <div class="kpi-value">${hw.sales.closed.actual}<span style="font-size:14px;color:#939598;font-weight:500;">/${hw.sales.total.budget}</span></div>
                <div class="kpi-foot">${fmtPct(100 * hw.sales.closed.actual / hw.sales.total.budget)} of plan · pricing +2.3%</div>
              </div>
              <p style="font-size:10px;color:#58595B;line-height:1.5;margin-top:8px;">Condo sales pacing 8 units behind plan, pricing offsetting.</p>
            </div>
            <div class="chart-card">
              <div class="chart-head"><h4>Shops at Grand Prairie</h4></div>
              <div class="kpi" style="border:none;padding:6px 0;">
                <div class="kpi-label">Pre-Leased</div>
                <div class="kpi-value">${fmtPct(tgp.psr.leaseUp.preLeasedPct)}</div>
                <div class="kpi-foot">${fmtUsdM(tgp.psr.overview.totalBudget)} budget · ${tgp.psr.overview.status}</div>
              </div>
              <p style="font-size:10px;color:#58595B;line-height:1.5;margin-top:8px;">Retail dev tracking 14 days behind from Q1 weather; recovery plan.</p>
            </div>
          </div>

          <div class="chart-card" style="margin-bottom:14px;">
            <div class="chart-head"><h4>Portfolio status</h4></div>
            <table class="report">
              <thead><tr><th>Project</th><th>Phase</th><th class="num">Scale</th><th class="num">Performance</th><th>Status</th></tr></thead>
              <tbody>
                <tr><td><strong>LightHaven District West</strong></td><td>Lease-up</td><td class="num">${fmtNum(lh.occupancy.total)} units</td><td class="num">${fmtPct(lh.occupancy.leasedPct)} leased</td><td><span class="pill green">Ahead of Plan</span></td></tr>
                <tr><td><strong>The Hawthorne</strong></td><td>Sales</td><td class="num">${hw.sales.total.budget} units</td><td class="num">${hw.sales.closed.actual} closed</td><td><span class="pill amber">Behind Plan</span></td></tr>
                <tr><td><strong>Shops at Grand Prairie</strong></td><td>Construction</td><td class="num">${fmtNum(tgp.psr.overview.nrsf)} NRSF</td><td class="num">${fmtPct(tgp.psr.leaseUp.preLeasedPct)} pre-leased</td><td><span class="pill amber">Schedule At Risk</span></td></tr>
              </tbody>
            </table>
          </div>

          <div class="notes-block">
            <h5>Portfolio commentary</h5>
            <ul>
              <li><strong>LightHaven</strong> — ${escapeHtml((lh.comments.executive_summary && lh.comments.executive_summary.body) || '')}</li>
              <li><strong>Hawthorne</strong> — ${escapeHtml((hw.comments.executive_summary && hw.comments.executive_summary.body) || '')}</li>
              <li><strong>Grand Prairie</strong> — ${escapeHtml((tgp.comments.executive_summary && tgp.comments.executive_summary.body) || '')}</li>
            </ul>
          </div>
        </div>
        ${pageFooter('Portfolio', ctx.pageNum++, ctx.totalPages, asOf)}
      </section>
    `;
  }

  // ---------- ASSEMBLY ----------
  async function renderReport(target, view = 'portfolio') {
    target.innerHTML = '';

    let html = '';
    const periodStr = `Q2 ${new Date().getFullYear()}`;

    if (view === 'portfolio') {
      const all = await EmberData.loadAll();
      const ctx = { pageNum: 2, totalPages: 0 };
      const lhPages = lighthavenPages(all.lighthaven, ctx);
      const hwPages = hawthornePages(all.hawthorne, ctx);
      const tgpPgs = tgpPages(all.tgp, ctx);
      // Pages: cover + overview + 3 dividers + (lh) + (hw) + (tgp) — recompute
      const totalPages = 1 /*cover*/ + 1 /*overview*/ + 3 /*dividers*/ + lhPages.length + hwPages.length + tgpPgs.length;
      ctx.pageNum = 2; ctx.totalPages = totalPages;

      const asOf = fmtDate([all.lighthaven.lastImport.lastImportAt, all.hawthorne.lastImport.lastImportAt, all.tgp.lastImport.lastImportAt].sort().slice(-1)[0]);

      html += coverPage({
        title: 'Ember Vertical Projects Portfolio',
        subtitle: 'A consolidated executive view of LightHaven District West, The Hawthorne, and The Shops at Grand Prairie — generated live from operational and development data.',
        project: 'Three Projects',
        asOfStr: asOf,
        periodStr,
      });
      html += portfolioOverview({ all, ctx });

      html += dividerPage({ eyebrow: 'Project I · Built to Rent', title: 'LightHaven District West', sub: 'Lease-up and operational performance — the residential anchor of the portfolio.', heroImage: BRAND.lighthaven.divider, projectLogo: BRAND.lighthaven.logo });
      html += lhPages.join('');

      html += dividerPage({ eyebrow: 'Project II · For-Sale', title: 'The Hawthorne', sub: 'Condominium sales velocity, pricing performance, and proforma against budget.', heroImage: BRAND.hawthorne.divider });
      html += hwPages.join('');

      html += dividerPage({ eyebrow: 'Project III · Development', title: 'The Shops at Grand Prairie', sub: 'HPI / Ember JV — development PSR covering budget, schedule, GMP, risk, and lease-up.', heroImage: BRAND.tgp.divider });
      html += tgpPgs.join('');

    } else if (view === 'lighthaven') {
      const d = await EmberData.loadLightHaven();
      const ctx = { pageNum: 1, totalPages: 0 };
      const pages = lighthavenPages(d, ctx);
      ctx.totalPages = pages.length; ctx.pageNum = 1;
      const pages2 = lighthavenPages(d, ctx);
      // No separate coverPage — collagePage at index 0 is the cover.
      html += pages2.join('');

    } else if (view === 'hawthorne') {
      const d = await EmberData.loadHawthorne();
      const ctx = { pageNum: 1, totalPages: 0 };
      const pages = hawthornePages(d, ctx);
      ctx.totalPages = pages.length; ctx.pageNum = 1;
      const pages2 = hawthornePages(d, ctx);
      html += pages2.join('');

    } else if (view === 'tgp') {
      const d = await EmberData.loadTgp();
      const ctx = { pageNum: 1, totalPages: 0 };
      const pages = tgpPages(d, ctx);
      ctx.totalPages = pages.length; ctx.pageNum = 1;
      const pages2 = tgpPages(d, ctx);
      html += pages2.join('');
    }

    target.innerHTML = html;
  }

  global.EmberReport = { renderReport };
})(window);
