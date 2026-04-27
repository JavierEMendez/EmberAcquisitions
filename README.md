# EmberApps

A web-based underwriting and reporting platform for residential land development. Built as a full replacement for the Excel pro forma model — all calculations run server-side in Python, and the results are accessible to the whole team through a shared URL with no Excel required.

Deployed on [Railway](https://railway.app) with a PostgreSQL database.

---

## Purpose

Ember's land acquisition team underwrites residential tract deals using a detailed 10-sheet Excel model. This application replicates that model exactly in Python, layers an executive-level portfolio dashboard on top of it, and pulls in live reporting (project returns, loan capacities, operating revenues, community sales, macro data) from separately maintained Excel/CSV feeds. The goal is a single source of truth for all active and prospective deals, accessible from any browser.

---

## Application Pages

### MPC Underwriting (`/`)
The core underwriting tool. Users enter deal inputs (land cost, lot mix, front footage, section pacing, infrastructure costs, debt terms, MUD/WCID structure, etc.) and the calculation engine produces a full monthly pro forma — revenues, costs, cashflows, IRR, and equity multiple. Projects are saved per-user to the database, support multiple named scenarios with promote/compare workflows, sensitivity analysis, full change history, and Excel import/export.

### Ember Capital (`/ember-capital`)
The executive portfolio dashboard. Aggregates every active project into a single LP + Promote recycling view: equity outstanding, equity recycled, total commitments, projected promote, and IRR/equity-multiple at the portfolio level. Includes editable LP commitments, an admin settings panel, monthly automated email reports, and a branded 2-page executive PDF export.

### Active Project Returns (`/returns`)
Consolidated LP-level return metrics for all active projects. Populated by uploading the Ember Dashboard Excel file. Shows per-project tables (LP distributions, contributions, profit, IRR, equity multiple, promote) broken out by year — each project's columns automatically start at its own first contribution year so leading zeros never appear. Exportable as a branded executive PDF or Excel workbook.

### Loan Capacities & Debt Schedules (`/loans`)
The current loan book — terms, drawn amounts, interest reserves, utilization, and capacity health for each active loan facility, plus per-project debt schedules. Populated from the same uploaded dashboard file. Exportable as a branded executive PDF.

### Ember Operating Revenues (`/operations`)
Tracks fee revenue across all active projects — development fees, project personnel, bookkeeping, receivables & bond fees, and brokerage. Shows KPI cards, an annual forecast, monthly history (with date filter), next 12 months, and next 12 quarters. Populated from the uploaded dashboard file. Exportable as a branded executive PDF or Excel workbook.

### Community Sales (`/sales`)
Tabbed sales-and-permit intelligence dashboard. Each tab is fed by its own uploaded file:
- **Bohlke Report** — GPD (gross pace per day) by community, monthly
- **Sales by Week** — weekly absorption snapshot
- **Starts & Inventory** — builder starts and standing inventory
- **Waller Permits** — Waller County permit volume
- **Houston Permits** — Houston-MSA permit data
- **GPD UW Performance** — actual vs. underwritten pace by deal

### Macro (`/macro`)
Macroeconomic context page. Pulls FRED series (rates, housing starts, mortgage indicators) on a refreshable schedule and overlays them on the active deal pipeline.

### Home (`/home`)
Landing page with navigation cards to every page above, plus admin controls for user management (create/delete, assign per-page access, reset passwords, set email).

### Account Settings (modal, every page)
Self-serve account panel: change name, email, password, and opt in/out of monthly report emails. Identical UI on every page.

---

## Repository Structure

```
EmberAcquisitions/
│
├── app.py                  # Flask application — routes, auth, DB access, PDFs, scheduler
├── calc.py                 # Pure-Python calculation engine (ports the 10-sheet Excel model)
├── report_parser.py        # Parses the Ember Dashboard Excel into JSON (returns/loans/ops)
├── sales_parser.py         # Community Sales dashboard parser (Sales by Week, Starts & Inv.)
├── bohlke_parser.py        # Bohlke GPD report parser
├── waller_parser.py        # Waller County permits parser
├── hpermits_parser.py      # Houston permits parser
├── uw_parser.py            # GPD UW Performance parser
├── macro_parser.py         # FRED-series macro data parser
├── data_puller.py          # FRED data fetcher
├── excel_export.py         # Excel export helper for MPC underwriting output
├── excel_import.py         # Excel import helper for MPC projects
├── requirements.txt        # Python dependencies
├── Procfile                # Process entry point for Railway
├── railway.toml            # Railway deployment configuration
│
├── templates/
│   ├── login.html          # Login page
│   ├── home.html           # Home / navigation page
│   ├── app.html            # MPC Underwriting (inputs + pro forma output)
│   ├── portfolio.html      # Ember Capital executive dashboard
│   ├── returns.html        # Active Project Returns
│   ├── loans.html          # Loan Capacities & Debt Schedules
│   ├── operations.html     # Ember Operating Revenues
│   ├── sales.html          # Community Sales (tabbed)
│   └── macro.html          # Macro / FRED data
│
└── static/
    ├── ember_logo.png      # Color logo (light backgrounds)
    ├── ember_logo_white.png # White logo lockup (for blue header bars)
    ├── ember_mark.png      # Square brand mark / favicon
    └── img/
```

---

## Key Files

### `calc.py`
The calculation engine. A faithful Python port of the Excel underwriting model — every revenue line, cost line, and cashflow formula is implemented here with explicit comments referencing the original Excel sheet and row. Takes a flat dict of user inputs and returns a dict of monthly arrays and summary outputs. No Excel dependency at runtime.

### `report_parser.py`
Reads the Ember Dashboard `.xlsx` file (uploaded by an admin) using openpyxl and extracts three datasets — project returns, loan schedules, and operating revenues — into structured JSON, which is stored in the database and served to the reporting pages.

### `app.py`
Flask backend. Handles:
- Session-based authentication (username/password, bcrypt hashing)
- Per-user page access controls (JSONB column on the `users` table)
- CRUD API for underwriting projects (with scenarios, sensitivity, change history)
- Dashboard / sales / macro file uploads and report storage
- Excel exports (Returns, Operations, MPC project)
- Branded server-side **executive PDFs** for Ember Capital, Returns, Loans, Operations
- APScheduler job that emails monthly report PDFs on the 1st of every month
- Admin endpoints (user management, "send reports now")
- Account-settings endpoints (name / email / password / report-email opt-in)

### `templates/app.html`
The MPC underwriting frontend. A single-page vanilla JS application (~4000 lines) — input handling, chart rendering (Chart.js), scenario manager, and Excel import/export live here. Communicates with the backend via `fetch` JSON calls.

### `templates/portfolio.html`
The Ember Capital executive dashboard. Renders the LP + Promote recycling model (equity outstanding, recycled, commitments, projected promote, portfolio IRR/EM), with editable LP commitments and a "Print" button that opens the branded server-rendered PDF in a new tab.

---

## Branded Executive PDFs

All four reporting pages (`/ember-capital`, `/returns`, `/loans`, `/operations`) expose two buttons:

- **Executive Report** — opens the server-rendered branded PDF in a new tab
- **Download PDF** — downloads the same PDF as an attachment

Routes: `/api/ember-capital/pdf`, `/api/returns/pdf`, `/api/loans/pdf`, `/api/operations/pdf` (the latter three accept `?download=1`).

The PDFs use a shared Ember brand system: blue (#13344E) header bars with the official white-logo lockup, an orange (#F25929) accent stripe, eyebrow + title section blocks, alternating warm-paper row tints (#FAF7F2 / #F3EEE5), and a confidential footer bar with `PAGE N OF M`. Generated with **fpdf2** (Helvetica + Latin-1 sanitization).

The Returns PDF in particular keeps each project's title glued to its table (no orphaned headings across pages) and trims year columns per-project to start at the project's first capital contribution.

---

## Monthly Report Emails

`APScheduler` is wired into the Flask process. On the 1st of every month at 08:00 UTC it generates the four executive PDFs (Ember Capital, Returns, Loans, Operations) and emails them to every user who has opted in via Account Settings. Admins can also trigger a one-off send via `POST /api/admin/send-reports-now`.

SMTP credentials are read from environment variables (`SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASS`, `SMTP_FROM`).

---

## Stack

| Layer | Technology |
|---|---|
| Backend | Python 3, Flask |
| Database | PostgreSQL (psycopg2) |
| Frontend | Vanilla JS, Chart.js, jsPDF (legacy), html2canvas (legacy) |
| Auth | Session-based, werkzeug password hashing |
| PDFs | fpdf2 (server-side branded executive reports) |
| Scheduler | APScheduler (monthly email job) |
| Email | smtplib (SMTP via env-configured relay) |
| Hosting | Railway |
| Excel I/O | openpyxl |
| Macro data | FRED API |

---

## Local Development

```bash
# Install dependencies
pip install -r requirements.txt

# Set environment variables
export DATABASE_URL="postgresql://user:password@localhost/ember"
export SECRET_KEY="dev-secret-key"

# Optional — for monthly email reports
export SMTP_HOST="smtp.example.com"
export SMTP_PORT="587"
export SMTP_USER="reports@yourdomain.com"
export SMTP_PASS="..."
export SMTP_FROM="Ember Reports <reports@yourdomain.com>"

# Optional — for /macro
export FRED_API_KEY="..."

# Run
python app.py
# Open http://localhost:5001
```

---

## Deployment

See [`DEPLOY.md`](DEPLOY.md) for full Railway deployment instructions, environment variable setup, and first-login credentials.

Pushes to `main` auto-deploy on Railway.

---

## Data Flow

```
User inputs (browser)
        │
        ▼
    app.py API
        │
        ▼
    calc.py  ──────────────────► Pro forma outputs (JSON)
                                          │
                                          ▼
                                  PostgreSQL (projects table)


Admin uploads Dashboard .xlsx
        │
        ▼
  report_parser.py
        │
        ▼
  PostgreSQL (reports table)
        │
        ├──► /returns        (project returns data)         ──► branded PDF / Excel
        ├──► /loans          (loan schedule data)           ──► branded PDF
        ├──► /operations     (fee revenue data)             ──► branded PDF / Excel
        └──► /ember-capital  (aggregated LP/Promote model)  ──► branded PDF


Sales / permits / macro uploads
        │
        ├──► sales_parser.py     ──► /sales (Sales by Week, Starts & Inv.)
        ├──► bohlke_parser.py    ──► /sales (Bohlke Report)
        ├──► waller_parser.py    ──► /sales (Waller Permits)
        ├──► hpermits_parser.py  ──► /sales (Houston Permits)
        ├──► uw_parser.py        ──► /sales (GPD UW Performance)
        └──► macro_parser.py     ──► /macro
                  ▲
                  │
            data_puller.py  (FRED API)


APScheduler  (1st of month, 08:00 UTC)
        │
        ▼
  Generate 4 branded PDFs ──► SMTP ──► users opted in via Account Settings
```

---

## Recent Highlights

- **Returns PDF pagination** — sub-section titles never separate from their tables; each project's year columns start at its own first contribution year; ~2 projects per landscape page.
- **Branded executive PDFs** — Returns, Loans, and Operations now share the Ember Capital PDF styling (blue/orange header, KPI cards, branded tables, page numbering) instead of the old client-side html2canvas screenshots.
- **Account Settings consistency** — identical Monthly Report Emails section on every page.
- **Ember Capital dashboard** — repurposed portfolio page into an LP + Promote recycling model with Equity column, editable Commitments, Print/PDF export, and monthly email.
- **Community Sales tabs** — added Sales by Week, Starts & Inventory, Bohlke, Waller Permits, Houston Permits, and GPD UW Performance.
- **Theme** — light/dark toggle with darkened light-mode greys for readability.
