# EmberApps — Dashboard Contributor Handoff

**Audience:** A new engineer (and their AI coding agent) joining the
EmberApps codebase to build new dashboards.

**Goal:** Get up to speed in 30 minutes. Ship a new dashboard that
looks, feels, authenticates, deploys, and behaves like everything
else in the app.

---

## 1 · What EmberApps is

EmberApps (a.k.a. "EmberAcquisitions") is the internal web app for
**Ember Group**, a private real-estate operator running residential
land developments and adjacent verticals.

It started as a server-side replacement for a 10-sheet Excel
underwriting model and has grown into a multi-dashboard portal:

| Dashboard | URL | Purpose |
|---|---|---|
| MPC Underwriting | `/` | Deal underwriting (full pro forma, scenarios, sensitivity) |
| Ember Capital | `/ember-capital` | LP + Promote recycling portfolio model |
| Project Returns | `/returns` | Active-deal returns roll-up |
| Loans & Debt | `/loans` | Loan capacities, debt schedules |
| Operating Revenues | `/operations` | Fee revenue across active projects |
| Community Sales | `/sales` | Permits, starts, absorption (tabbed) |
| Macro | `/macro` | FRED macro series + housing/rates |
| Verticals | `/verticals` | LightHaven / TGP / NOI / leasing pace |
| Invoice Dashboard | `/invoice-dashboard` | Bi-weekly Stampli AP snapshots |
| Financial Statements | `/financials` | Live Sage Intacct BS / IS / SCF |
| Acquisitions GIS | `/acquisitions` | Land screening — parcel search, constraints, project assembly |
| Home | `/home` | Landing page (nav cards, admin) |
| Login | `/login` | Username + password |
| Account Settings | (modal) | Self-serve, on every page |

A new dashboard means: a new entry in this table.

---

## 2 · Stack & deployment

| Layer | Stack |
|---|---|
| Backend | Python 3.11+, Flask |
| Database | PostgreSQL (psycopg2 / RealDictCursor) |
| Frontend | Server-rendered Jinja2 templates + vanilla JS (no React/Vue) |
| Charts | Chart.js (when needed) |
| Auth | Session cookies + bcrypt (werkzeug) |
| PDFs | fpdf2 (server-side, branded) |
| Excel I/O | openpyxl |
| Scheduler | APScheduler (monthly report job) |
| Hosting | Railway (auto-deploys on push to `main`) |
| Web server | gunicorn (`gunicorn -c gunicorn.conf.py app:app`) |

**Deploy flow:** push to `main` → Railway autodeploys. There is no
staging environment. Local dev: `python app.py` runs at
`http://localhost:5001`. See `DEPLOY.md` for env vars
(`DATABASE_URL`, `SECRET_KEY`, plus optional SMTP / FRED / Intacct
creds).

---

## 3 · Repo layout

```
EmberAcquisitions/
├── app.py                       # 10K+ line Flask app. All routes,
│                                # auth, DB schema, scheduler, PDFs.
│                                # Add new routes here.
├── acq_gis.py                   # Acquisitions GIS engine — live ArcGIS/REST
│                                # layer queries, geometry, HCAD/MCAD owner
│                                # overlays, spatial enrichment
├── acq_parcels.py               # Statewide parcel cache (SQLite + R-Tree).
│                                # Lives on a volume, not in Postgres — see
│                                # ACQ_DATA_DIR
├── acq_store.py                 # Acquisitions persistence (Postgres JSONB
│                                # document store, acq_objects)
├── calc.py                      # Underwriting calculation engine
├── frp_excel.py                 # Financial Reporting Package .xlsx builder
├── frp_mapping.py               # COA → FRP line-item rules
├── tb_parser.py                 # Sage Intacct TB HTML parser + BS/IS/SCF rollups
├── sage_intacct.py              # Sage Intacct XML API client
├── *_parser.py                  # Various per-source Excel/CSV parsers
│                                # (returns, loans, ops, sales, bohlke,
│                                #  waller permits, houston permits,
│                                #  macro, GPD UW performance)
├── excel_export.py / excel_import.py  # MPC project I/O
├── report.py                    # Branded PDF generation
├── data_puller.py               # FRED API fetcher (for /macro)
│
├── templates/
│   ├── login.html               # Login page
│   ├── home.html                # Landing page
│   ├── app.html                 # MPC Underwriting (~4K lines vanilla JS)
│   ├── portfolio.html           # Ember Capital
│   ├── returns.html
│   ├── loans.html
│   ├── operations.html
│   ├── sales.html
│   ├── macro.html
│   ├── verticals.html
│   ├── invoice_dashboard.html
│   ├── financials.html          # Newest dashboard — a good template to copy from
│   ├── admin_*.html             # Admin views
│   └── _partials/
│       ├── _sidebar_foot.html   # Account + Home buttons (drop into every sidebar)
│       ├── _account_modal.html  # Account settings modal (drop near </body>)
│       ├── _data_dates_footer.html
│       └── …
│
├── static/
│   ├── theme.css                # ★ Canonical design tokens (the EAX system).
│   │                              Source of truth for colors, fonts, loaders,
│   │                              account modal, sidebar foot, data dates footer.
│   ├── css/
│   │   ├── financials.css       # Per-dashboard CSS — pattern to follow
│   │   ├── operations.css
│   │   ├── loans.css
│   │   ├── invoice_dashboard.css
│   │   └── capital.css
│   ├── js/
│   │   ├── operations.js
│   │   ├── loans.js
│   │   ├── invoice_dashboard.js
│   │   └── capital.js
│   ├── img/                     # Logo lockups, favicon
│   ├── ember_logo.png           # Dark logo (use on light bg)
│   ├── ember_logo_white.png     # White logo (use on blue bg)
│   ├── ember_mark.png           # Square brand mark
│   └── verticals/               # Verticals-specific bundle (sub-app)
│
├── docs/
│   ├── SAGE_TB_REPORT_SETUP.md  # Sage admin setup for live TB pull
│   └── DASHBOARD_CONTRIBUTOR_GUIDE.md  # ← this file
│
├── requirements.txt
├── Procfile / Dockerfile / gunicorn.conf.py / railway.toml / nixpacks.toml
├── README.md                    # High-level overview (read this first)
└── DEPLOY.md                    # Railway deployment + env vars
```

---

## 4 · The design system (the part to get right)

Visual consistency is non-negotiable. Every dashboard pulls
`/static/theme.css` and respects a small set of conventions.

### 4.1 — Color & type tokens

Defined in `static/theme.css`. Reference via CSS variables, never
hardcode colors. The same tokens flip between **light (Warm Paper)**
and **dark (Deep Navy)** themes:

```css
/* Canonical EAX tokens — use these */
--eax-bg            /* page background (paper / navy) */
--eax-bg-2          /* sidebar / hover background */
--eax-surface       /* card background */
--eax-ink           /* primary text */
--eax-ink-2         /* slightly stronger text (headings) */
--eax-muted         /* secondary text */
--eax-subtle        /* labels, eyebrows, dimmer text */
--eax-line          /* hairline borders (10% ink) */
--eax-line-2        /* stronger hairline (18% ink) */
--eax-accent        /* Ember orange #F25929 (light) / #F57346 (dark) */
--eax-accent-soft   /* 10% accent — hover bg, callout bands */
--eax-accent-fg     /* readable text ON accent fill (always #FFFFFF) */
--eax-good          /* success green */
--eax-bad           /* error red */
--eax-shadow-card   /* canonical card shadow */
```

**Legacy aliases** (`--bg`, `--surface`, `--text`, `--accent`, etc.)
also work — they re-resolve against the active theme. Either is
fine; new code should prefer the `--eax-*` names.

**Fonts** (loaded via Google Fonts in every page `<head>`):
- `--font-display` — `'Plus Jakarta Sans'` — page titles, section heads
- `--font-sans` — `'DM Sans', 'Plus Jakarta Sans'` — body
- `--font-mono` — `'JetBrains Mono'` — numbers, codes
- `--font-eyebrow` — `'Plus Jakarta Sans'` — small uppercase labels

### 4.2 — Theme switcher

Every page must include this snippet in `<head>` BEFORE any
stylesheet, so the saved theme applies before the first paint and
you don't get a flash of the wrong palette:

```html
<script>(function(){
  var t=localStorage.getItem('ember-theme');
  document.documentElement.dataset.theme=(t==='dark')?'dark':'light';
})();</script>
```

Then load CSS:

```html
<link rel="stylesheet" href="/static/theme.css?v=3"/>
<link rel="stylesheet" href="/static/css/your_dashboard.css?v=1"/>
```

The theme toggle lives inside the Account modal — you don't have to
build it; just include `_account_modal.html`.

### 4.3 — Page shell (sidebar + content)

Every dashboard uses the same 240px-wide sidebar shell. The cleanest
starting point is `templates/financials.html` lines 1–120 — copy that
shell and replace the `.fin-page` content.

Required elements:

1. **Sidebar with logo** — same nav list every page. Each `<a>`
   wrapped in `{% if page_access.get('your_key', true) %}` so per-user
   permissions hide entries.
2. **`active` class** on the current page's sidebar item.
3. **`<div style="flex:1"></div>`** spacer to push foot buttons to bottom.
4. **`{% include "_partials/_sidebar_foot.html" %}`** — adds the
   Account + Home buttons at the bottom.
5. **`{% include "_partials/_account_modal.html" %}`** — drop near
   `</body>`. Adds the modal AND wires up `openAcct()` /
   `closeAcct()` / theme toggle globally.

The body uses `overflow: hidden` and the page content scrolls inside
its own container. Any data-dates footer or fixed bar pins to the
bottom over that.

### 4.4 — Card / table / control conventions

Each dashboard's CSS file is named for the section
(`financials.css`, `loans.css`, etc.) and uses a 2–4 letter prefix on
class names so they don't collide:

- `fin-*` for Financials
- `op-*` for Operations
- `inv-*` for Invoice Dashboard
- `loan-*` for Loans
- `cap-*` for Capital

The recurring building blocks:

**Eyebrow + title** (top of every page):
```html
<div class="fin-eyebrow">FINANCIAL STATEMENTS</div>
<h1 class="fin-title">Balance Sheet, Income Statement &amp; Cash Flows</h1>
<p class="fin-sub">Pulls per-entity statements from Sage Intacct…</p>
```

Patterns: eyebrow is `11px / weight 700 / 0.22em letter-spacing /
uppercase / accent-colored`. Title is Plus Jakarta Sans 32px. Sub is
13px muted.

**Pill nav / tab control** — used for switching between sub-views
(BS / IS / SCF, or report tabs). See `.fin-tabnav` / `.fin-tab` in
`financials.css` for canonical styling: rounded pills, accent-filled
active state with subtle orange shadow.

**Statement / data card** — white surface, 1px ink-10 border, 12px
radius, 28–32px padding. Class `.fin-statement` is a reference.

**Buttons** — three variants:
- `.fin-btn` — outline, muted color, accent hover
- `.fin-btn.primary` — accent fill, white text, 4px orange shadow
- `.sb-foot-btn` — outline + muted, used at sidebar foot

**Tables for financials** — see `.fin-stmt-table` in `financials.css`:
hairline header, `tabular-nums` for numeric columns, `JetBrains Mono`
for the value cells, alternating section/lineitem/subtotal row
classes. Use this pattern wherever you need a financial table.

**Data Dates Footer** — every dashboard pins a small footer with
"data from / uploaded" timestamps. Use `_partials/_data_dates_footer.html`
and the `.data-dates-footer` styles in `theme.css`. They handle the
`left: 240px` (240px sidebar) offset automatically; if your page has
an additional rail (e.g. invoice dashboard archive), override:

```css
body.page-yourdash .data-dates-footer { left: 488px; }
```

**Loaders / spinners** — use the canonical Ember "Wave" loader from
`theme.css`:
```html
<span class="ember-loader ember-loader--md">
  <svg class="loader-wave" viewBox="0 0 56 28"><!-- 9 <rect>s --></svg>
  <span class="ember-loader__label">LOADING</span>
</span>
```
Sizes: `--sm` (button-fit), `--md` (inline), `--lg` (page splash).
Legacy `.loading` / `.spinner` classes also work — they're aliased to
a single-bar pulse of the same brand motion.

---

## 5 · Adding a new dashboard — checklist

A working dashboard end-to-end takes about ~150 LOC plus its own CSS.

### 5.1 — Backend (`app.py`)

1. **Add a route** for the page render and as many `/api/...` JSON
   routes as the dashboard needs:
   ```python
   @app.route("/yourdash")
   @login_required
   def yourdash_page():
       if not _can_view_yourdash():
           return redirect(url_for("home"))
       return render_template(
           "yourdash.html",
           username=session.get("username"),
           is_admin=session.get("is_admin"),
           page_access=session.get("page_access", {}),
       )

   @app.route("/api/yourdash/data")
   @login_required
   def api_yourdash_data():
       if not _can_view_yourdash():
           return jsonify({"error": "forbidden"}), 403
       # … pull data, compute, return JSON
       return jsonify({...})
   ```

2. **Permission helper** (always re-reads from DB so admin grants
   apply without re-login):
   ```python
   def _can_view_yourdash() -> bool:
       """Per-user gate. Admin overrides; otherwise page_access.yourdash."""
       if session.get("is_admin"):
           return True
       pa = _refresh_page_access_from_db()
       return bool(pa.get("yourdash", False))  # default False = sensitive
   ```
   For non-sensitive dashboards default to `True`. For anything
   investor-facing or money-related, default to `False`.

3. **Add the permission key to admin UI** —
   `templates/admin_users.html` has a `PAGE_LABELS` map / checkbox
   list; add a `yourdash` entry there so admins can grant access.

4. **DB schema (if needed)** — append `CREATE TABLE IF NOT EXISTS …`
   inside the `_init_db()` block (around line 109 of `app.py`). Use
   `ALTER TABLE … ADD COLUMN IF NOT EXISTS …` for additions to
   existing tables, never destructive migrations. Always include a
   primary key and the standard `created_at TIMESTAMP DEFAULT NOW()`.

### 5.2 — Frontend (`templates/yourdash.html`)

Copy `templates/financials.html` as the starting point — it's the
newest dashboard, uses every convention cleanly, and has the
sidebar shell already wired with permission gating per item.

Required edits:
1. `<title>` and the sidebar's `.logo-sub` label
2. Mark the right sidebar item `active`
3. Replace `.fin-page` content with your dashboard
4. Switch the per-page CSS link to `/static/css/yourdash.css`
5. If you need a JS bundle, add it as `/static/js/yourdash.js` and
   include via `<script defer>` at the bottom (most dashboards keep
   their JS inline in the template — fine for <500 LOC of JS;
   factor out if it grows)

### 5.3 — CSS (`static/css/yourdash.css`)

Follow the prefix convention (`.yd-*` or similar). Use only
`var(--eax-*)` tokens for colors. Don't import other dashboards'
CSS — copy specific patterns you need.

### 5.4 — Add to navigation

The sidebar nav list is duplicated in every dashboard template (each
page renders its own sidebar with the current page's item marked
`active`). To make a new dashboard appear in every page's sidebar:

1. Add an `<a>` entry to **every existing template's sidebar block**
   (most templates have an identical block — `financials.html` lines
   54–114 is the current canonical list).
2. Add the corresponding card to `templates/home.html`.

This is a known papercut (DRY violation). Be careful to update every
template — otherwise the new dashboard will only appear in some
pages' sidebars. Until we refactor the sidebar into a partial, this
is the manual cost of adding a dashboard.

---

## 6 · Auth & permissions

- **Session-based.** `session["user_id"]`, `session["username"]`,
  `session["is_admin"]`, `session["page_access"]` populated at login.
- **`@login_required`** wraps every protected route. Defined around
  `app.py:382`. Redirects to `/login?next=…` on unauth.
- **`page_access`** is a JSONB column on `users`. Default new
  permissions to `False` and add them to the schema with an `ALTER
  TABLE`-style backfill. Sensitive dashboards (financials, capital)
  default to False; benign ones (macro, sales) default to True.
- **`_refresh_page_access_from_db()`** (line 1235) re-reads from DB.
  Use this in your permission helpers so that when an admin grants a
  permission to an already-logged-in user, the grant applies on the
  next request without forcing re-login.
- **Admin override.** Anyone with `is_admin = TRUE` bypasses page
  access checks.

---

## 7 · Database conventions

- **psycopg2 + RealDictCursor** — every row is a dict, address by
  column name: `row["column_name"]`.
- **Always close cursor + connection**, even on early returns:
  ```python
  conn = get_db(); cur = conn.cursor()
  try:
      cur.execute(...)
      row = cur.fetchone()
  finally:
      cur.close(); conn.close()
  ```
  (Older code uses the inline `cur.close(); conn.close()` pattern;
  matching it is fine.)
- **JSON columns** — use `json.dumps(...)` on the way in,
  RealDictCursor auto-decodes on the way out.
- **UPSERT pattern** — preferred for cache-style tables:
  ```python
  cur.execute("""
      INSERT INTO mytable (key, value, updated_at)
      VALUES (%s, %s, NOW())
      ON CONFLICT (key) DO UPDATE
        SET value = EXCLUDED.value, updated_at = NOW()
  """, (k, json.dumps(v)))
  ```
- **No mutating tests against the live DB.** This is a hard rule —
  the production DB has investor data. Local dev should use a
  separate Postgres (set `DATABASE_URL` to point at a local
  instance).

---

## 8 · Frontend conventions

- **Vanilla JS only.** No build step, no frameworks, no JSX, no
  TypeScript. The team values being able to open the file and read
  it. Modern ES2020+ syntax is fine (the app targets recent Chrome).
- **`fetch()`** for API calls; always include `credentials: 'same-origin'`
  if you ever fetch cross-handler.
- **DOM manipulation** by `document.getElementById` /
  `querySelector`. There is a small `$(id)` shorthand in most dashboards.
- **Chart.js** is loaded only on pages that need it (it's heavy).
  Import via CDN with a `defer` script tag.
- **`escapeHtml(s)`** helper is in most templates — use it before
  injecting user data into innerHTML to avoid XSS. The financials
  template has a canonical implementation:
  ```js
  function escapeHtml(s) {
    return String(s ?? '').replace(/[&<>"']/g,
      c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  }
  ```
- **`fmtMoney(n)`** is the standard money formatter. Most dashboards
  copy this verbatim:
  ```js
  function fmtMoney(n) {
    if (n === null || n === undefined || isNaN(n)) return '—';
    const r = Math.round(n);
    const neg = r < 0;
    const s = Math.abs(r).toLocaleString('en-US');
    return neg ? '(' + s + ')' : s;
  }
  ```
  Negatives use parentheses — standard accounting convention.

---

## 9 · PDF exports (when you need them)

If your dashboard needs a branded executive PDF:

1. Look at the **Ember Capital / Returns / Loans / Operations**
   pattern — they share a layout in `report.py` (header band, KPI
   strip, alternating row tints, confidential footer with `PAGE N
   OF M`).
2. Use **fpdf2** (NOT WeasyPrint or jsPDF — those are legacy /
   non-canonical and produce off-brand PDFs).
3. Brand colors are hardcoded as RGB tuples in `report.py`:
   - Blue header band: `#13344E` → `(19, 52, 78)`
   - Accent stripe: `#F25929` → `(242, 89, 41)`
   - Warm paper row tints: `#FAF7F2` / `#F3EEE5`
4. Always include Helvetica + Latin-1 sanitization for arbitrary text
   (special characters break fpdf2).
5. Route convention: `/api/yourdash/pdf` with `?download=1` for
   attachment-mode (otherwise inline display).

---

## 10 · Excel exports (when you need them)

- Use **openpyxl** (already in requirements).
- Follow `frp_excel.py` as the reference — it pre-computes totals
  as VALUES (not formulas) so the workbook displays correctly in
  any viewer (Excel, Numbers, LibreOffice, Drive preview).
- Brand fonts/colors live in constants at the top of the module.
- Stream as a download via Flask's `send_file(BytesIO, ...)`.

---

## 11 · Git / deployment workflow

- **`main` is production.** Every push triggers a Railway deploy
  (~2–3 minutes). No staging.
- **Branch naming:** `claude/<short-slug>` for AI-assisted work,
  `<initials>/<short-slug>` for human commits. Anything works as
  long as it's not `main`.
- **PRs:** open against `main`. Squash-merge is the norm. Multi-
  commit branches are fine if each commit is meaningful.
- **Commit messages:** short imperative title, then a paragraph or
  two explaining the *why*. The repo's existing commit history is
  a good style reference.
- **Pre-commit hooks:** none configured. Don't add `--no-verify` to
  bypass hooks if any get added — fix the actual issue.
- **Pushes that would force-push to `main`:** forbidden. Don't.

---

## 12 · Common gotchas

1. **Don't edit `frp_mapping.py` predicates without re-validating
   against the FRP files.** Account classifications were tuned to
   match the accountant's existing FRP workbook to the penny.
   `tb_parser.py` predicates are the canonical source of truth (and
   `frp_mapping.py` predates them and is largely superseded by
   `tb_parser.py` for the financials dashboard).

2. **The cache key for `intacct_tb_cache` is `(entity_id, period)`** —
   note that Sage uses different identifiers in different places
   (`entity_id` like "E_Grand Prairie Development LLC" vs sage_code
   like "DEV_GPD LLC"). The financials upload handles this by caching
   under both keys; do the same for any new Sage-fed dashboard.

3. **APScheduler runs in the same process.** Monthly report job
   fires on the 1st at 08:00 UTC. If your dashboard adds a scheduled
   task, register it through APScheduler in `app.py` (search for
   `apscheduler` to find the canonical pattern). Be careful with job
   IDs to avoid duplicates after redeploys.

4. **Railway free tier resource limits.** Long-running synchronous
   work (>30s) is risky — gunicorn's worker may be killed. For slow
   operations (Sage pulls, large parsers): cache aggressively, add
   spinners, and consider breaking work into smaller endpoints.

5. **No mutating tests against the live DB.** Repeated for emphasis.
   The production DB is the same DB the team uses daily.

6. **Sidebar nav is duplicated across templates.** When you add or
   rename a dashboard, you have to update every template's sidebar.
   This is a known papercut; until someone refactors the sidebar
   into a Jinja partial, just grep for the existing item names and
   update them everywhere.

7. **The Account modal partial includes its own JS.** Don't
   redefine `openAcct()`, `closeAcct()`, or `applyTheme()` in your
   page — just include the partial and they're wired up.

8. **CSS cache-busting** uses a `?v=N` query param on stylesheet
   links. Bump it when you change CSS to force a fresh download for
   users who have stale cached copies.

9. **`overflow: hidden` on body** is intentional — the sidebar +
   data-dates footer rely on it. Don't remove it. Page content
   should scroll inside its own container.

10. **Don't add documentation files (`.md`) for completed work**
    unless explicitly requested. Comments in code + commit
    messages are the canonical documentation.

---

## 13 · For your AI agent — quick reference

If you're letting an AI coding agent drive, give it this guide and
the following pinned context:

- **README.md** — high-level project overview
- **`docs/DASHBOARD_CONTRIBUTOR_GUIDE.md`** — this file
- **`templates/financials.html`** — newest dashboard, copy as template
- **`static/theme.css`** — design tokens (read this before touching CSS)
- **`static/css/financials.css`** — per-dashboard CSS pattern
- **`app.py` lines 109–305** — DB schema (so it knows what tables exist)
- **`app.py` lines 382–420** — `@login_required` definition
- **`app.py` lines 1235–1260** — `_refresh_page_access_from_db`
- **`app.py` lines 1947–1955** — example `_can_view_xxx()` helper

A new dashboard prompt template that has worked well:

> Add a new `/<slug>` dashboard for `<one-line description>`. Match
> the visual language of `/financials` (sidebar shell, eyebrow +
> title, pill nav tabs, statement-card layout). Per-user permission
> key: `<key>` (default `False`). DB needs: `<tables/columns or
> "no new tables">`. Backend route: `/<slug>` returns the template;
> `/api/<slug>/...` returns JSON. Add a card on `/home`, an entry
> in every sidebar, and an admin checkbox. Use the EAX design
> tokens — no hardcoded colors.

---

## 14 · Where to ask

- **Javier** (javier@maquinaholdings.com) — project owner; he's
  using Claude Code on Windows PowerShell and manages the Railway
  deployment.
- The codebase's commit history is the next-best reference. Most
  features have detailed commit messages explaining the *why*.

Welcome aboard.
