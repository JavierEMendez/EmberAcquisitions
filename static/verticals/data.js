/* ============================================================
   data.js — API client + mock data fallback
   ============================================================
   When served alongside the dashboard (relative paths), live
   endpoints are auto-detected. Set window.EMBER_API_BASE to a
   full origin only if hosting the report on a different domain.
   Any fetch that 404s falls through to the demo dataset so the
   report always renders end-to-end.
   ============================================================ */

(function (global) {
  const API_BASE = (global.EMBER_API_BASE || '').replace(/\/$/, '');

  async function tryFetch(path) {
    try {
      const r = await fetch(API_BASE + path, { headers: { 'Accept': 'application/json' } });
      if (!r.ok) return null;
      return await r.json();
    } catch (e) {
      return null;
    }
  }

  // ---------- MOCK DATA ----------
  const MOCK = {
    lighthaven: {
      lastImport: { lastImportAt: '2026-04-22T16:30:00Z', sourceFile: 'Master Leasing & Ops Tracker & Report.xlsx' },
      occupancy: { total: 248, counts: { 'Tenant Occupied': 168, 'Leased': 22, 'Renewal': 6, 'Model': 2, 'Turned': 38, 'AVAILABLE': 12 }, occupiedPct: 67.7, leasedPct: 79.8, availablePct: 15.3 },
      units: [], // not needed at this level — phase rollup carries the summary
      leasingPace: {
        months: ['Nov','Dec','Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct'],
        budget: [12, 28, 46, 68, 92, 118, 142, 162, 178, 192, 204, 212],
        actual: [14, 31, 49, 74, 98, 124, 152, 174, 196, null, null, null],
      },
      noi: {
        year: 2026,
        months: ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'],
        budget:   [182, 188, 194, 201, 208, 214, 220, 224, 228, 232, 236, 240],
        forecast: [171, 184, 192, 198, 209, 217, 224, null, null, null, null, null],
      },
      rents: {
        byFloorplan: {
          'Canary':   { avg: 1845, min: 1795, max: 1925, units: 48 },
          'Sparrow':  { avg: 2120, min: 2050, max: 2225, units: 56 },
          'Wren':     { avg: 2385, min: 2295, max: 2495, units: 62 },
          'Finch':    { avg: 2680, min: 2580, max: 2825, units: 44 },
          'Heron':    { avg: 3140, min: 3025, max: 3290, units: 38 },
        },
      },
      phaseRollup: [
        { fp: 'Canary',  p1: 18, p2: 16, p3: 14, total: 48, leased: 38, pct: 79 },
        { fp: 'Sparrow', p1: 22, p2: 20, p3: 14, total: 56, leased: 47, pct: 84 },
        { fp: 'Wren',    p1: 24, p2: 22, p3: 16, total: 62, leased: 51, pct: 82 },
        { fp: 'Finch',   p1: 16, p2: 14, p3: 14, total: 44, leased: 32, pct: 73 },
        { fp: 'Heron',   p1: 14, p2: 14, p3: 10, total: 38, leased: 30, pct: 79 },
      ],
      traffic: {
        weeks: ['W14','W15','W16','W17','W18','W19','W20','W21'],
        visitors: [38, 42, 51, 47, 58, 62, 55, 64],
        leads:    [11, 14, 18, 16, 21, 23, 19, 24],
        sources: { 'Drive-by': 28, 'Apartments.com': 22, 'Referral': 18, 'Zillow': 16, 'Other': 10 },
      },
      comments: {
        executive_summary: { body: 'LightHaven crossed 80% leased this period, four weeks ahead of budget. Phase 3 absorption remains strongest among Wren and Sparrow plans; concession pressure has eased to 2 weeks free across all plans. Renewal offers tracking 92% retention.' },
        leasing_strategy: { body: 'Hold rents on Wren and Heron; soften concessions on Canary in May to clear the 12 remaining unleased units. Move-in quality scores at 4.7/5.' },
        ops_notes: { body: 'NOI forecast tracking 1.6% below budget YTD, driven by R&M overruns in Phase 1 (HVAC retrofit). Forecast recovery in Q3 as turnover slows.' },
      },
    },

    hawthorne: {
      lastImport: { lastImportAt: '2026-04-24T14:10:00Z', sourceFile: 'Hawthorne Master Template.xlsx' },
      sales: {
        total:       { actual: 84, budget: 96 },
        closed:      { actual: 52, budget: 58 },
        available:   { actual: 32, budget: 38 },
        soldNotClosed:{ actual: 14, budget: 16 },
        avgPrice:    { actual: 742000, budget: 725000 },
        gci:         { actual: 1.85, budget: 2.10 }, // $M
      },
      stacking: [
        // 4 floors × 12 units. Status codes: closed/snc/available
        ['closed','closed','closed','snc','closed','closed','available','closed','snc','closed','closed','available'],
        ['closed','snc','closed','closed','available','closed','closed','snc','closed','available','closed','closed'],
        ['closed','closed','available','closed','snc','closed','available','closed','closed','snc','available','closed'],
        ['snc','available','closed','available','closed','available','snc','available','closed','available','available','snc'],
      ],
      salesByMonth: {
        months: ['Oct','Nov','Dec','Jan','Feb','Mar','Apr'],
        closed:[ 4, 6, 8, 9, 10, 8, 7 ],
        snc:   [ 3, 4, 5, 5, 4, 4, 3 ],
      },
      floorplans: [
        { fp: 'A1 — 1BR Loft',     total: 16, closed: 11, snc: 3, available: 2,  avgPrice: 612000 },
        { fp: 'B1 — 2BR Standard', total: 28, closed: 18, snc: 4, available: 6,  avgPrice: 728000 },
        { fp: 'B2 — 2BR + Den',    total: 22, closed: 14, snc: 4, available: 4,  avgPrice: 805000 },
        { fp: 'C1 — 3BR Corner',   total: 12, closed: 6,  snc: 2, available: 4,  avgPrice: 925000 },
        { fp: 'PH — Penthouse',    total: 6,  closed: 3,  snc: 1, available: 2,  avgPrice: 1480000 },
      ],
      proforma: [
        { line: 'Gross Sales Revenue',       budget: 62.4, actual: 38.6, forecast: 60.8 },
        { line: 'Sales Commissions',         budget: 2.18, actual: 1.35, forecast: 2.13 },
        { line: 'Marketing & Advertising',   budget: 0.82, actual: 0.61, forecast: 0.79 },
        { line: 'HOA / Operating Reserves',  budget: 0.35, actual: 0.18, forecast: 0.34 },
        { line: 'Net Sales Proceeds',        budget: 59.1, actual: 36.5, forecast: 57.5 },
      ],
      comments: {
        executive_summary: { body: 'Hawthorne is 62% closed on units and pacing 8 units behind plan, offset by stronger-than-budget pricing (+2.3% blended). PH and C1 demand remains soft; A1 sold out is in sight.' },
        sales_notes: { body: 'Reduce PH list pricing 3% in May; redeploy marketing to brokered C1 buyers in San Diego North County. SNC backlog converts in 60–90 days based on current escrow timelines.' },
      },
    },

    tgp: {
      lastImport: { lastImportAt: '2026-04-26T09:45:00Z', sourceFile: 'Shops at TGP_Development PSR.xlsx' },
      psr: {
        overview: {
          totalBudget: 47800000,
          nrsf: 92500,
          padAcres: 4.8,
          costPerNrsf: 516.76,
          costPerAcre: 9958333,
          status: 'On Budget · Schedule At Risk',
          statusColor: 'amber',
        },
        info: [
          { k: 'Project Name',    v: 'The Shops at Grand Prairie' },
          { k: 'Location',        v: 'Grand Prairie, TX' },
          { k: 'Owner',           v: 'HPI / Ember JV' },
          { k: 'GC',              v: 'Cadence Construction' },
          { k: 'Architect',       v: 'GFF Inc.' },
          { k: 'Civil',           v: 'Pacheco Koch' },
          { k: 'Project Type',    v: 'Retail Center + Pad Sites' },
          { k: 'GMP Date',        v: 'Feb 14, 2026' },
        ],
        team: [
          { role: 'Project Executive',  name: 'C. Saldierna' },
          { role: 'Senior PM',          name: 'M. Reyes' },
          { role: 'Owner Rep',          name: 'D. Whitlock' },
          { role: 'Construction Mgr',   name: 'J. Park' },
          { role: 'Leasing Lead',       name: 'A. Ngo' },
          { role: 'JV Liaison (HPI)',   name: 'R. Patel' },
        ],
        executiveSummary: 'Site work is 78% complete and tracking 14 days behind the baseline schedule due to two weeks of weather impact in March. GMP came in 2.1% favorable to budget; reallocations are funding upgraded storefront systems on the Retail Center. Pre-leasing reached 41% of NRSF this period — two LOIs out on Pad B and Pad C.',
        budget: [
          { line: 'Site Work & Earthwork',  retail: 4_200_000, pads: 1_800_000, realloc:   180_000, spent: 4_650_000, balance: 1_530_000, pct: 75 },
          { line: 'Foundations & Concrete', retail: 5_800_000, pads: 2_100_000, realloc:  -120_000, spent: 5_120_000, balance: 2_660_000, pct: 65 },
          { line: 'Structural Steel',       retail: 6_400_000, pads: 1_900_000, realloc:        0, spent: 4_980_000, balance: 3_320_000, pct: 60 },
          { line: 'Building Envelope',      retail: 4_900_000, pads: 1_400_000, realloc:   240_000, spent: 2_180_000, balance: 4_360_000, pct: 33 },
          { line: 'MEP Systems',            retail: 5_600_000, pads: 1_700_000, realloc:        0, spent: 1_950_000, balance: 5_350_000, pct: 27 },
          { line: 'Storefronts & Glazing',  retail: 2_100_000, pads:   480_000, realloc:   180_000, spent:   420_000, balance: 2_340_000, pct: 16 },
          { line: 'FF&E and Tenant Allow.', retail: 1_800_000, pads:   720_000, realloc:        0, spent:   180_000, balance: 2_340_000, pct:  7 },
          { line: 'Soft Costs',             retail: 2_900_000, pads:   900_000, realloc:        0, spent: 2_140_000, balance: 1_660_000, pct: 56 },
          { line: 'Contingency',            retail: 1_400_000, pads:   500_000, realloc:  -480_000, spent:         0, balance: 1_420_000, pct:  0 },
        ],
        schedule: [
          { milestone: 'Site Demolition',          target: '2025-09-15', actual: '2025-09-12', variance: -3, status: 'green',  note: 'Complete' },
          { milestone: 'Mass Grading',             target: '2025-11-30', actual: '2025-12-04', variance:  4, status: 'amber',  note: 'Weather' },
          { milestone: 'Underground Utilities',    target: '2026-01-31', actual: '2026-02-12', variance: 12, status: 'amber',  note: 'Coord delay' },
          { milestone: 'Foundations Complete',     target: '2026-03-31', actual: '2026-04-14', variance: 14, status: 'amber',  note: 'Weather' },
          { milestone: 'Structural Steel Topout',  target: '2026-06-30', actual: '',           variance:  0, status: 'blue',   note: 'In progress' },
          { milestone: 'Dry-In',                   target: '2026-09-30', actual: '',           variance:  0, status: 'gray',   note: 'Pending' },
          { milestone: 'Substantial Completion',   target: '2027-02-28', actual: '',           variance:  0, status: 'gray',   note: 'Pending' },
        ],
        leasingMilestones: [
          { milestone: 'Marketing Launch',          target: '2025-06-30', actual: '2025-06-25', variance: -5, status: 'green' },
          { milestone: 'First LOI',                 target: '2025-12-31', actual: '2025-11-18', variance: -43,status: 'green' },
          { milestone: '50% Pre-Leased',            target: '2026-09-30', actual: '',           variance:  0, status: 'amber' },
          { milestone: 'Anchor Lease Executed',     target: '2026-04-30', actual: '2026-04-21', variance: -9, status: 'green' },
        ],
        gmp: {
          gmpDate: 'Feb 14, 2026',
          gmpAmount: 39_400_000,
          buyoutVariance: -820_000,
          buyoutPct: 91,
          contingencyOpening: 1_900_000,
          contingencyUsed: 480_000,
          contingencyProjected: 1_180_000,
          changeOrders: [
            { num: 'CO-001', desc: 'Storefront upgrade — premium aluminum',     amount:  240_000, status: 'Approved' },
            { num: 'CO-002', desc: 'Owner-directed paving in delivery court',   amount:   85_000, status: 'Approved' },
            { num: 'CO-003', desc: 'Unforeseen rock removal — Pad C',           amount:  150_000, status: 'Approved' },
            { num: 'CO-004', desc: 'Trash enclosure relocation (city req.)',    amount:   42_000, status: 'In Review' },
            { num: 'CO-005', desc: 'TI allowance increase — Anchor tenant',     amount:  180_000, status: 'In Review' },
          ],
        },
        leaseUp: {
          nrsf: 92500,
          preLeasedSf: 37800,
          preLeasedPct: 41,
          tenants: [
            { tenant: 'Anchor — Health & Wellness', sf: 14500, type: 'Anchor',      status: 'Lease Executed', pct: 'green' },
            { tenant: 'Quick-Serve Restaurant A',   sf: 3200,  type: 'Pad B',       status: 'LOI Out',         pct: 'amber' },
            { tenant: 'Coffee Concept',             sf: 2400,  type: 'Pad C',       status: 'LOI Out',         pct: 'amber' },
            { tenant: 'Boutique Fitness',           sf: 4600,  type: 'Inline',      status: 'In Negotiation',  pct: 'amber' },
            { tenant: 'Specialty Grocery',          sf: 8900,  type: 'Junior Anchor',status: 'Lease Executed', pct: 'green' },
            { tenant: 'Apparel — Active',           sf: 4200,  type: 'Inline',      status: 'Prospect',        pct: 'gray'  },
          ],
        },
        actionItems: [
          { item: 'Finalize CO-005 with Anchor leasing team',   owner: 'A. Ngo / J. Park',   due: 'May 9',  priority: 'High' },
          { item: 'Recover 8 days on steel topout via 6-day weeks (May–Jun)', owner: 'M. Reyes', due: 'May 15', priority: 'High' },
          { item: 'Resolve city storm-drain comments — Pad B',  owner: 'Pacheco Koch',       due: 'May 12', priority: 'Med'  },
          { item: 'Lock storefront finish samples for owner approval', owner: 'GFF', due: 'May 20', priority: 'Med'  },
        ],
        issues: [
          { issue: 'Weather contingency depleted in Q1 — 14 days behind on foundations',  impact: 'Schedule', severity: 'amber' },
          { issue: 'MEP submittals lagging — 6 of 14 outstanding',                        impact: 'Schedule', severity: 'amber' },
          { issue: 'Two pad-site LOIs unsigned past target',                              impact: 'Lease-up', severity: 'amber' },
          { issue: 'CO log trending toward 4.5% of GMP (target ≤ 3%)',                    impact: 'Budget',   severity: 'red'   },
        ],
      },
      comments: {
        executive_summary: { body: 'Schedule pressure from Q1 weather has been partially mitigated; recovery plan adds 6-day weeks through June. Budget remains favorable to GMP. Pre-leasing momentum is positive but needs two more LOIs to convert by end of Q3 to stay on plan.' },
      },
    },
  };

  // ---------- LIVE FETCH WITH FALLBACK ----------
  // Single endpoint per vertical (see SNAPSHOT_API_SPEC.md). The server
  // can return data in a slightly different shape than render.js
  // expects (e.g. hawthorne live → kpis/byFloorplan; render needs
  // sales/floorplans). We validate, then fill any missing field from
  // MOCK so the report always has a complete object — no more
  // "Failed to load" from a half-formed live payload.
  async function loadSnapshot(vertical) {
    // Honor the renderer's force-mock fallback so the second-attempt render
    // doesn't re-hit the same broken live response.
    if (typeof window !== 'undefined' && window.__EMBER_FORCE_MOCK__) return null;
    const wrap = await tryFetch(`/api/verticals/snapshot?vertical=${vertical}`);
    if (wrap && wrap.hasData && wrap.data) {
      return {
        live: true,
        lastImport: { lastImportAt: wrap.generatedAt, sourceFile: wrap.sourceFile },
        ...wrap.data,
      };
    }
    return null;
  }
  // Empty/neutral fallback structures for when live data is present but a
  // sub-field is missing — these keep render.js from throwing while making
  // sure NO demo MOCK numbers leak into a real report. The user will see
  // an empty chart instead of fake "$1845/mo Canary" data.
  const EMPTY = {
    lighthaven: {
      occupancy:   { total: 0, counts: {}, occupiedPct: 0, leasedPct: 0, availablePct: 0 },
      leasingPace: { months: [], budget: [], actual: [] },
      noi:         { year: new Date().getFullYear(), months: [], budget: [], forecast: [] },
      rents:       { byFloorplan: {} },
      phaseRollup: [],
      traffic:     { weeks: [], visitors: [], leads: [], sources: {} },
      comments:    {},
    },
    hawthorne: {
      sales:        { total: { actual: 0, budget: 0 }, closed: { actual: 0, budget: 0 }, available: { actual: 0, budget: 0 }, soldNotClosed: { actual: 0, budget: 0 }, avgPrice: { actual: 0, budget: 0 }, gci: { actual: 0, budget: 0 } },
      stacking:     [],
      salesByMonth: { months: [], closed: [], snc: [] },
      floorplans:   [],
      proforma:     [],
      comments:     {},
    },
    tgp: {
      psr: {
        overview: { totalBudget: 0, nrsf: 0, padAcres: 0, costPerNrsf: 0, costPerAcre: 0, status: '—', statusColor: 'gray' },
        info: [], team: [], executiveSummary: '',
        budget: [], schedule: [], leasingMilestones: [],
        gmp: { gmpDate: null, gmpAmount: 0, buyoutVariance: 0, buyoutPct: 0, contingencyOpening: 0, contingencyUsed: 0, contingencyProjected: 0, changeOrders: [] },
        leaseUp: { nrsf: 0, preLeasedSf: 0, preLeasedPct: 0, tenants: [] },
        actionItems: [], issues: [],
      },
      comments: {},
    },
  };

  // Pull a live key whole if present and non-null; otherwise EMPTY (not MOCK)
  // so a real upload never gets contaminated with demo numbers.
  function liveOr(live, key, empty) {
    return (live && live[key] != null) ? live[key] : empty;
  }

  async function loadLightHaven() {
    const live = await loadSnapshot('lighthaven');
    if (live) {
      const e = EMPTY.lighthaven;
      return {
        live: true,
        lastImport:  liveOr(live, 'lastImport',  { lastImportAt: null, sourceFile: null }),
        occupancy:   liveOr(live, 'occupancy',   e.occupancy),
        leasingPace: liveOr(live, 'leasingPace', e.leasingPace),
        noi:         liveOr(live, 'noi',         e.noi),
        rents:       liveOr(live, 'rents',       e.rents),
        phaseRollup: liveOr(live, 'phaseRollup', e.phaseRollup),
        traffic:     liveOr(live, 'traffic',     e.traffic),
        comments:    liveOr(live, 'comments',    e.comments),
      };
    }
    // No live upload yet — use demo MOCK so the report still renders.
    const m = MOCK.lighthaven;
    return { live: false, lastImport: m.lastImport, occupancy: m.occupancy, leasingPace: m.leasingPace, noi: m.noi, rents: m.rents, phaseRollup: m.phaseRollup, traffic: m.traffic, comments: m.comments };
  }

  async function loadHawthorne() {
    const live = await loadSnapshot('hawthorne');
    if (live) {
      const e = EMPTY.hawthorne;
      return {
        live: true,
        lastImport:   liveOr(live, 'lastImport',   { lastImportAt: null, sourceFile: null }),
        sales:        liveOr(live, 'sales',        e.sales),
        stacking:     liveOr(live, 'stacking',     e.stacking),
        salesByMonth: liveOr(live, 'salesByMonth', e.salesByMonth),
        floorplans:   liveOr(live, 'floorplans',   e.floorplans),
        proforma:     liveOr(live, 'proforma',     e.proforma),
        comments:     liveOr(live, 'comments',     e.comments),
      };
    }
    const m = MOCK.hawthorne;
    return { live: false, lastImport: m.lastImport, sales: m.sales, stacking: m.stacking, salesByMonth: m.salesByMonth, floorplans: m.floorplans, proforma: m.proforma, comments: m.comments };
  }

  async function loadTgp() {
    const live = await loadSnapshot('tgp');
    if (live) {
      const e = EMPTY.tgp;
      return {
        live: true,
        lastImport: liveOr(live, 'lastImport', { lastImportAt: null, sourceFile: null }),
        psr:        liveOr(live, 'psr',        e.psr),
        comments:   liveOr(live, 'comments',   e.comments),
      };
    }
    const m = MOCK.tgp;
    return { live: false, lastImport: m.lastImport, psr: m.psr, comments: m.comments };
  }

  global.EmberData = {
    loadLightHaven,
    loadHawthorne,
    loadTgp,
    async loadAll() {
      const [lh, hw, tgp] = await Promise.all([loadLightHaven(), loadHawthorne(), loadTgp()]);
      return { lighthaven: lh, hawthorne: hw, tgp };
    },
  };
})(window);
