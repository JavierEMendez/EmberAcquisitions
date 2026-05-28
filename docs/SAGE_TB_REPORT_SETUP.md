# Sage Intacct Setup: API-Accessible Trial Balance Report

**Audience:** Sage Intacct administrator (Ember Group)
**Purpose:** Enable the Financials dashboard at `/financials` to pull live
Trial Balance data directly from Sage Intacct — eliminating monthly manual
TB exports while matching the FRP to the penny.
**Time required:** ~30 minutes for first-time setup, then permanent.

---

## TL;DR

The dashboard needs a **Custom Report** (built in Sage's Custom Report
Writer) named exactly **`API_TB`** that outputs a per-account Trial
Balance with `Reporting Period` and `Owner` as runtime parameters.

The previous attempt — using "Memorize" on the standard Trial Balance
report — does not work. Sage's XML API operation `<readReport>` only
accepts **Custom Reports** (built via the Report Writer), not Memorized
Reports.

---

## Why Memorize Doesn't Work

Sage Intacct has two distinct concepts that look similar in the UI:

| | Memorized Report | Custom Report |
|---|---|---|
| Created via | "Memorize" button on any standard report | Reports → Custom Reports → New |
| What it is | A saved set of parameters for an existing report | A report definition built from scratch |
| API access | **Not accessible** via `<readReport>` | **Accessible** via `<readReport>` |
| What we tested | "API_TB" memorized — returns "Report does not exist" | This setup makes it work |

We confirmed this by trying 15 name variants × 2 API operations
(`readReport` and `runReport`) against the memorized `API_TB`. Every
attempt returned `Report 'X' does not exist`. Sage's API treats
Memorized Reports as a separate (inaccessible) layer.

---

## What to Build

### Output specification

The dashboard parses the report output server-side via column-name
matching. We need a per-account row with these columns (any standard
Sage column names will be auto-detected by our parser — case-insensitive
with common synonyms):

| Required column | Description | Sage synonyms our parser recognizes |
|---|---|---|
| **Account number** | GL account number (e.g. `10110`, `16066-C`) | `Account No`, `Account Number`, `GL Account`, `ACCOUNTNO` |
| **Account name** | Account title | `Account Name`, `Account Title`, `Title`, `Description` |
| **Opening balance** | Period-start balance | `Opening Balance`, `Beginning Balance`, `OPENBAL` |
| **Debit total** | Period debits | `Debit`, `Debits`, `Total Debits` |
| **Credit total** | Period credits | `Credit`, `Credits`, `Total Credits` |
| **Closing balance** | Period-end balance | `Closing Balance`, `Ending Balance`, `Balance`, `ENDBAL` |

Filters that should be applied (matching the standard TB defaults):

- **Posted entries only** (exclude Draft / Submitted / Approved)
- **Period-end cutoff** — only entries dated on or before the period end
- Sage's standard TB-report exclusions (commit-side bookkeeping, etc.)
  should apply automatically since we're building on the same data model

### Runtime parameters

These two parameters MUST be configurable at runtime (i.e., we pass them
in via the API call, not baked in):

| Parameter name | What our code passes | Notes |
|---|---|---|
| **Reporting Period** | e.g. `Month Ended March 2026` | Must match a period exactly as it appears in Reports → Reporting Periods |
| **Owner** | e.g. `16 - GPD` | Owner here = Location in Sage. Pass the Location ID/code. |

We pass these via the `<arguments>` element of `<readReport>` using
several common Sage argument names (`REPORTINGPERIOD`, `OWNER`,
`LOCATIONID`) — whichever your report definition binds will be honored.

---

## Step-by-Step Setup in Sage Intacct

> **Note:** Exact menu paths vary slightly between Sage Intacct versions.
> If a menu item isn't where described, search Sage's help for "Custom
> Report Writer" or "Create Custom Report".

### 1. Navigate to Custom Reports

- Top nav: **Reports**
- Look for **Custom Reports** or **Customize Reports** in the sub-menu
- If you don't see Custom Reports, your Sage instance may need the
  **Customization Services** module enabled — contact Sage support

### 2. Create New Custom Report

- Click **+ New** (or **Add** / **Create**) to open the Report Writer
- Choose **Trial Balance** as the report type / template
  - If asked: this is a "General Ledger" / "GL" report
  - Choose the column layout that matches the standard TB
    (Opening / Debit / Credit / Closing per account)

### 3. Configure the report

- **Title / Name:** Enter exactly `API_TB`
  - Case-sensitive — no spaces, no special characters, just `API_TB`
- **Description:** "Trial Balance for API access — used by Financials
  dashboard. Do not delete."
- **Columns:** Make sure the report outputs the 6 columns listed in the
  "Output specification" section above
- **Filters:**
  - Default the **Reporting Period** filter to a sane value (any period
    — the API will override at runtime)
  - Default the **Owner / Location** filter the same way (or set it to
    "All Entities" — the API will narrow it per request)
  - **Crucially: configure both as "Ask at runtime" or "Prompt"** so the
    API can pass them in. The exact UI label varies — look for:
    - "Use as parameter"
    - "Prompt user"
    - "Runtime selection"
    - A checkbox next to the filter
- **Sharing:** Mark the report **Public** so all users (including the
  API service account, `Ember_API`) can access it

### 4. Save the report

- Click **Save**
- Verify the report appears in your Custom Reports list with the name
  `API_TB`

### 5. Verify the API user has access

The dashboard authenticates as the Sage user **`Ember_API`**. That user
needs **Read** permission on Custom Reports.

- Go to **Company → Roles** (or **Users → Roles**)
- Find the role assigned to `Ember_API`
- Under **Permissions → Reports → Custom Reports** (or similar), confirm
  the role has at least **List** and **Run** permissions
- Save

### 6. Test from Sage UI

- As `Ember_API` (or temporarily as yourself), open the Custom Reports
  list and click `API_TB`
- It should prompt for the runtime parameters (Reporting Period, Owner)
  — confirm the prompts appear (this confirms the parameters are
  configured as runtime, not hardcoded)
- Run with: Reporting Period = `Month Ended March 2026`, Owner = `16 - GPD`
- Verify the output matches the FRP for GPD March 2026

### 7. Notify the dev team

Once the report runs cleanly from the UI, message us with:
- Confirmation the report is named exactly `API_TB`
- The exact column names Sage shows (so we can verify the parser will
  auto-recognize them — if Sage uses non-standard names like
  "Beg Bal" we'll add them to the synonym list)
- Anything unusual about the parameter binding

We'll then click **Pull TB from Sage** on the dashboard. If the report
is accessible, the Balance Sheet renders from real Sage data — to the
penny — on every refresh going forward.

---

## What Happens On Our End When This Works

Once `API_TB` is accessible, our existing code (already deployed)
will:

1. Hit `<readReport>` with `report=API_TB`, `arguments` containing
   the entity & period
2. Parse the returned rows via column-name matching
3. Roll up into the FRP-shaped Balance Sheet using the same predicates
   as your accountant's `frp_builder.py` script
4. Cache the result in `intacct_tb_cache`
5. Render at `/financials`

No code change required after the report exists — the candidate-name
list already includes `API_TB` as the first entry.

---

## If Custom Report Writer Isn't Available

Some Sage Intacct subscriptions don't include the Custom Report Writer
module. If when you navigate to **Reports** you don't see a way to
build new custom reports:

1. Contact Sage Intacct support and ask whether **Customization
   Services** or **Platform Services** is included in your subscription
2. If not, ask about the cost to add it (typically a low-cost add-on)
3. If purchasing isn't viable, fall back to the **TB Upload** workflow
   already wired up in the dashboard — accountant exports the TB
   monthly and drags it into the upload panel (60 seconds per month)

---

## Troubleshooting

### "Report 'API_TB' does not exist" after creation

- Confirm the report is in **Custom Reports**, not just memorized
- Confirm the report is marked **Public** (Sharing → All Users)
- Verify the `Ember_API` user's role has Custom Reports access
- Try refreshing the Sage session — log in/out as `Ember_API` to clear
  any cached permissions

### Report runs but returns 0 rows

- The runtime parameters aren't binding. Check the report's filter
  configuration — each filter that should be configurable at runtime
  needs the "Prompt" / "Ask at runtime" flag set
- Test by running from Sage UI with the same parameters we pass via
  API: Reporting Period = `Month Ended March 2026`, Owner = `16 - GPD`

### Report returns rows but parser fails

- Sage returned data but with column names our parser doesn't
  recognize. Send us the exact column names from a sample Excel export
  of the report and we'll add synonyms in 5 minutes

---

## Contact

Dev team contact for follow-up questions on this setup:

- **GitHub:** the PR that landed this dashboard is in the EmberApps repo
  under the `claude/financials-*` branch series
- **Module reference:** `sage_intacct.py:pull_tb_via_report`
  and `tb_parser.py:compute_bs`
- **Endpoint:** `POST /api/financials/pull-tb-report` on the deployed
  app

Once `API_TB` exists in Custom Reports and is accessible to the
`Ember_API` user, this dashboard runs forever on automation. No more
monthly TB exports needed.
