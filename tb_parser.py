"""Trial-balance parser for Sage Intacct HTML-formatted .xls exports.

Extracted from frp_builder.py (built by another team member to solve
the exact problem we hit with raw GLENTRY aggregation: Sage's TB
report applies internal filters — exclude commit-side bookkeeping,
placeholder JEs, etc. — that we can't perfectly reverse-engineer.

The path forward: skip the GLENTRY mess entirely and trust Sage's
authoritative TB output. Accountant exports two TBs from the Sage
UI (one Current-Month, one Current-YTD), uploads them to /financials,
and we render the BS/IS/SCF using the same predicates and roll-ups
that frp_builder.py uses.

Owner = Location in this Sage instance (per accountant confirmation).
"""
from __future__ import annotations
from collections import OrderedDict
import re
from typing import Optional
from bs4 import BeautifulSoup


# ─── Account-prefix groupings (BS) ────────────────────────────────
RELATED_PARTY_ACCTS = {21020, 21022, 21023, 21024, 21034, 21036, 21052}
FINANCING_LIAB_ACCTS = {21015, 21016, 21017, 21018}
CAPTURED_LIAB_ACCTS = (
    {20020, 20030, 20060, 20108, 21060}
    | RELATED_PARTY_ACCTS
    | FINANCING_LIAB_ACCTS
)


def parse_number(s: str) -> float:
    """Parse number string from TB, handling parentheses for negatives."""
    if not s or s.strip() in ("", "-"):
        return 0.0
    s = s.strip()
    neg = False
    if s.startswith("(") and s.endswith(")"):
        neg = True
        s = s[1:-1]
    s = s.replace(",", "")
    try:
        val = float(s)
    except ValueError:
        return 0.0
    return -val if neg else val


def get_prefix(acct_num: str) -> int:
    """Strip suffixes like -A, -C and return the numeric prefix."""
    base = acct_num.split("-")[0]
    try:
        return int(base)
    except ValueError:
        return 0


# ─── HTML TB parser ──────────────────────────────────────────────
def parse_tb_html(content: bytes | str) -> dict:
    """Parse a Sage Intacct HTML-formatted .xls trial balance.

    Returns: {sage_owner_code: {
        'full_name':        str,
        'sage_code':        str,
        'accounts':         [{num, name, opening, debit, credit, closing}, ...],
        'reporting_period': str,
        'as_of_date':       str (MM/DD/YYYY),
    }}

    Auto-detects single-entity vs multi-entity files. Multi-entity files
    have a header table before each entity's data table.
    """
    if isinstance(content, bytes):
        # Sage exports as Latin-1 or UTF-8; try UTF-8 first, fall back.
        try:
            content = content.decode("utf-8")
        except UnicodeDecodeError:
            content = content.decode("latin-1", errors="replace")

    soup = BeautifulSoup(content, "html.parser")
    tables = soup.find_all("table")

    entities: dict[str, dict] = {}
    pending_owner: Optional[tuple] = None
    i = 0

    while i < len(tables):
        table = tables[i]
        rows = table.find_all("tr")

        # Detect "header table" (multi-entity format puts an Owner: header
        # in its own table before the entity's data table).
        is_header = False
        if len(rows) <= 10:
            for row in rows:
                cells = row.find_all(["td", "th"])
                text = " ".join(c.get_text(strip=True) for c in cells)
                if "Owner:" in text and "--" in text:
                    match = re.search(r"Owner:\s*(.+?)--(.+)", text)
                    if match:
                        pending_owner = (
                            match.group(1).strip(),
                            match.group(2).strip(),
                        )
                        is_header = True
                        break
        if is_header:
            i += 1
            continue

        # Detect data table.
        if len(rows) >= 8:
            has_accounts = False
            for row in rows[8:]:
                cells = row.find_all(["td", "th"])
                if len(cells) >= 6:
                    first = cells[0].get_text(strip=True)
                    if first and first[0].isdigit() and ":" not in first:
                        has_accounts = True
                        break

            if has_accounts:
                owner_info = None
                reporting_period = None
                as_of_date = None

                # Header rows carry Reporting Period / As Of Date / Owner.
                for row in rows[:8]:
                    cells = row.find_all(["td", "th"])
                    texts = [c.get_text(strip=True) for c in cells]
                    if len(texts) >= 2:
                        if texts[0] == "Reporting Period:":
                            reporting_period = texts[1]
                        elif texts[0] == "As Of Date:":
                            as_of_date = texts[1]
                        elif texts[0] == "Owner:":
                            if "--" in texts[1]:
                                parts = texts[1].split("--", 1)
                                owner_info = (parts[0].strip(), parts[1].strip())

                if pending_owner:
                    sage_code, full_name = pending_owner
                    pending_owner = None
                elif owner_info:
                    sage_code, full_name = owner_info
                else:
                    i += 1
                    continue

                accounts = []
                for row in rows:
                    cells = row.find_all(["td", "th"])
                    texts = [c.get_text(strip=True) for c in cells]
                    if len(texts) < 6:
                        continue
                    acct_num = texts[0].strip()
                    if not acct_num or not acct_num[0].isdigit():
                        continue
                    if acct_num.startswith("Totals") or acct_num in ("Account", "Number"):
                        continue
                    if ":" in acct_num:
                        continue
                    accounts.append({
                        "num":     acct_num,
                        "name":    texts[1].strip(),
                        "opening": parse_number(texts[2]),
                        "debit":   parse_number(texts[3]),
                        "credit":  parse_number(texts[4]),
                        "closing": parse_number(texts[5]),
                    })

                entities[sage_code] = {
                    "full_name":        full_name,
                    "sage_code":        sage_code,
                    "accounts":         accounts,
                    "reporting_period": reporting_period or "",
                    "as_of_date":       as_of_date or "",
                }
        else:
            pending_owner = None

        i += 1

    return entities


# ─── BS account predicates (from frp_builder.py) ─────────────────
def is_cash(p):              return (10000 <= p <= 10899) or p == 10911
def is_restricted_bonds(p):  return p in (10912, 10913, 10914)
def is_receivables(p):       return 11000 <= p <= 11999
def is_prepaids(p):          return 12000 <= p <= 12999
def is_promissory(p):        return p == 13015
def is_ppe(p):               return 14000 <= p <= 14999
def is_land(p):              return p in (16000, 18020, 19000)

def is_development(p):
    if 16001 <= p <= 16799: return True
    if 17000 <= p <= 17999: return True
    if 18000 <= p <= 18999 and p != 18020: return True
    if 19001 <= p <= 19999: return True
    return False

def is_contra_sales(p): return p in (16800, 16850)
def is_contra_mud(p):   return p in (16900, 16901, 16902)
def is_dev_loan(p):     return 22000 <= p <= 22999
def is_bond_lt(p):      return p in (21016, 21017, 21018)

def is_members_eq(p):
    return 30000 <= p <= 30999 and p != 30500 and p != 30550

def is_income_stmt(p):  return 40000 <= p <= 99999

def is_other_current_liab(p):
    return (20000 <= p <= 21999) and p not in CAPTURED_LIAB_ACCTS


def is_other_op_liab(p):
    # Same predicate as is_other_current_liab; named separately to
    # match frp_builder.py's distinction (used in SCF working-capital
    # changes section).
    return (20000 <= p <= 21999) and p not in CAPTURED_LIAB_ACCTS


def is_re_invest(p):
    """RE Development Expenditures for SCF investing. Excludes contras
    (those are non-cash bookkeeping moves)."""
    if 15000 <= p <= 15999: return True
    if 16001 <= p <= 16799: return True
    if 16851 <= p <= 16899: return True
    if 16903 <= p <= 16999: return True
    if 17000 <= p <= 17999: return True
    if 18000 <= p <= 18999 and p != 18020: return True
    if 19001 <= p <= 19999: return True
    return False


def _normalize_account(a: dict) -> dict:
    """Coerce different account-dict shapes to the canonical TB shape
    {num, name, opening, debit, credit, closing}.

    The repo has two account-dict formats in circulation:
      - frp_builder.py / tb_parser native:  num / opening / closing
      - sage_intacct.get_trial_balance:     no  / open    / close

    Either can wind up in the cache (TB upload vs Refresh-from-Sage),
    so every entry point through tb_parser normalizes defensively.
    """
    return {
        "num":     a.get("num")     or a.get("no")    or "",
        "name":    a.get("name", ""),
        "opening": a.get("opening") if "opening" in a else a.get("open",  0.0) or 0.0,
        "debit":   a.get("debit",  0.0) or 0.0,
        "credit":  a.get("credit", 0.0) or 0.0,
        "closing": a.get("closing") if "closing" in a else a.get("close", 0.0) or 0.0,
    }


def _normalize_accounts(accounts: list[dict]) -> list[dict]:
    return [_normalize_account(a) for a in (accounts or [])]


def sum_accounts(accounts: list[dict], predicate, field: str = "closing") -> float:
    total = 0.0
    for a in accounts:
        if predicate(get_prefix(a.get("num") or a.get("no") or "")):
            # Fallback for old-format keys.
            v = a.get(field)
            if v is None and field == "closing":
                v = a.get("close", 0.0)
            elif v is None and field == "opening":
                v = a.get("open", 0.0)
            total += (v or 0.0)
    return total


def change(accounts: list[dict], predicate, flip: bool = False) -> float:
    """Sum (closing - opening) over matching accounts. `flip=True`
    negates the result (used for revenue accounts which have CR
    balances — flip to positive for display)."""
    total = 0.0
    for a in accounts:
        if predicate(get_prefix(a["num"])):
            chg = a["closing"] - a["opening"]
            total += -chg if flip else chg
    return total


def cash_change(accounts: list[dict], predicate) -> float:
    """Sum (opening - closing) over matching accounts — the SCF
    convention used by frp_builder.py. For assets: positive when
    asset DECREASED (cash inflow). For liabilities: positive when
    liability INCREASED (cash inflow). Result rolls up into the
    correct cash-flow direction."""
    total = 0.0
    for a in accounts:
        if predicate(get_prefix(a["num"])):
            total += a["opening"] - a["closing"]
    return total


# ─── BS value computation (FRP-shaped) ────────────────────────────
def compute_bs(accounts: list[dict]) -> "OrderedDict[str, float]":
    """Roll up TB accounts into the canonical FRP balance-sheet shape.

    Sign convention: assets positive, liabilities/equity flipped to
    positive for display. Retained Earnings includes the YTD income-
    statement roll-up plus account 30500 + 30550 — this is what
    closes the footing the GLENTRY approach left $7.5M open.

    Accounts can arrive in either TB-format (num/closing) or
    GLENTRY-format (no/close); we normalize defensively up front so
    either source caches transparently.
    """
    accounts = _normalize_accounts(accounts)
    def gs(predicate): return sum_accounts(accounts, predicate, "closing")

    other_cl = sum(
        -a["closing"] for a in accounts
        if is_other_current_liab(get_prefix(a["num"]))
    )
    meq = -sum(
        a["closing"] for a in accounts
        if is_members_eq(get_prefix(a["num"]))
    )
    re_30500 = gs(lambda p: p == 30500)
    re_30550 = gs(lambda p: p == 30550)
    retained = -(re_30500 + re_30550 + gs(is_income_stmt))

    v: "OrderedDict[str, float]" = OrderedDict()
    # Assets
    v["cash"]         = gs(is_cash)
    v["restricted"]   = gs(is_restricted_bonds)
    v["receivables"]  = gs(is_receivables)
    v["prepaids"]     = gs(is_prepaids)
    v["land"]         = gs(is_land)
    v["development"]  = gs(is_development)
    v["contra_sales"] = gs(is_contra_sales)
    v["contra_mud"]   = gs(is_contra_mud)
    v["promissory"]   = gs(is_promissory)
    v["ppe"]          = gs(is_ppe)
    # Liabilities (flipped to positive for display)
    v["trade_pay"]    = -gs(lambda p: p == 20030)
    v["retention"]    = -gs(lambda p: p == 20060)
    v["rp_pay"]       = -gs(lambda p: p in RELATED_PARTY_ACCTS)
    v["tax_pay"]      = -gs(lambda p: p == 20108)
    v["other_cl"]     = other_cl
    v["earnest"]      = -gs(lambda p: p == 21060)
    v["bond_st"]      = -gs(lambda p: p == 21015)
    v["dev_loan"]     = -gs(is_dev_loan)
    v["bond_lt"]      = -gs(is_bond_lt)
    v["deferred"]     = -gs(lambda p: p == 20020)
    # Equity
    v["members_eq"]   = meq
    v["retained"]     = retained
    # Computed subtotals
    v["total_ca"]     = v["cash"] + v["restricted"] + v["receivables"] + v["prepaids"]
    v["re_net"]       = v["land"] + v["development"] + v["contra_sales"] + v["contra_mud"]
    v["total_nca"]    = v["re_net"] + v["promissory"] + v["ppe"]
    v["total_assets"] = v["total_ca"] + v["total_nca"]
    v["ap_net"]       = v["trade_pay"] + v["retention"] + v["rp_pay"] + v["tax_pay"] + v["other_cl"]
    v["total_cl"]     = v["ap_net"] + v["earnest"] + v["bond_st"]
    v["total_ncl"]    = v["dev_loan"] + v["bond_lt"] + v["deferred"]
    v["total_liab"]   = v["total_cl"] + v["total_ncl"]
    v["total_eq"]     = v["members_eq"] + v["retained"]
    v["total_liab_eq"] = v["total_liab"] + v["total_eq"]
    return v


# ─── Income Statement (Monthly + YTD) ─────────────────────────────
#
# IS uses change-in-balance (closing - opening) rather than absolute
# closing balance. Each line is computed twice: once from the
# Current-Month TB (gives monthly activity) and once from the YTD TB
# (gives year-to-date activity). Revenue accounts have CR balances so
# flip=True for display positive.
def compute_is(monthly_accounts: list[dict],
               ytd_accounts: list[dict]) -> "OrderedDict[str, dict]":
    """Roll up TB account changes into the canonical FRP income-
    statement shape. Returns each line keyed by short name, value =
    {'monthly': float, 'ytd': float}."""
    monthly_accounts = _normalize_accounts(monthly_accounts)
    ytd_accounts     = _normalize_accounts(ytd_accounts)
    def both(predicate, flip=False):
        return {
            "monthly": change(monthly_accounts, predicate, flip),
            "ytd":     change(ytd_accounts,     predicate, flip),
        }
    v: "OrderedDict[str, dict]" = OrderedDict()
    # Revenue — flipped (CR balance → positive display)
    v["lot_sales"]     = both(lambda p: 40000 <= p <= 40999,          flip=True)
    v["mkt_fee"]       = both(lambda p: p == 41020,                   flip=True)
    v["fence"]         = both(lambda p: p == 41001,                   flip=True)
    v["dev_income"]    = both(lambda p: 42000 <= p <= 46999,          flip=True)
    # Expenses — NOT flipped (DR balance, positive value = real expense)
    v["cos"]           = both(lambda p: 50000 <= p <= 59999)
    v["mkt_exp"]       = both(lambda p: 70001 <= p <= 70039)
    v["ga"]            = both(lambda p: (60000 <= p <= 69999) or (70040 <= p <= 79999))
    # Other Income (Expense)
    v["int_income"]    = both(lambda p: p == 90031,                   flip=True)
    franchise_tax      = both(lambda p: p == 80015)
    # Franchise tax is an expense — display negative under Other Income
    v["franchise_tax"] = {"monthly": -franchise_tax["monthly"],
                          "ytd":     -franchise_tax["ytd"]}
    # Subtotals
    def add(*keys):
        return {
            "monthly": sum(v[k]["monthly"] for k in keys if k in v),
            "ytd":     sum(v[k]["ytd"]     for k in keys if k in v),
        }
    v["total_re_rev"]  = add("lot_sales", "mkt_fee", "fence")
    v["total_op_rev"]  = add("total_re_rev", "dev_income")
    v["total_rev"]     = add("total_op_rev")
    v["total_op_exp"]  = add("cos", "mkt_exp", "ga")
    v["total_exp"]     = add("total_op_exp")
    v["total_other"]   = add("int_income", "franchise_tax")
    v["net_income"]    = {
        "monthly": v["total_rev"]["monthly"] - v["total_exp"]["monthly"] + v["total_other"]["monthly"],
        "ytd":     v["total_rev"]["ytd"]     - v["total_exp"]["ytd"]     + v["total_other"]["ytd"],
    }
    return v


def render_is(v: dict, monthly_label: str = "Current Month",
              ytd_label: str = "Year To Date") -> dict:
    """Emit IS in the BS-template's structure shape so the frontend
    renders both with the same JS path. Each line gets BOTH a
    monthly and YTD value."""
    def L(name, key, indent=0):
        return {
            "line_item": name,
            "value":     v[key]["monthly"],
            "value_ytd": v[key]["ytd"],
            "indent":    indent,
        }

    structure = [
        {
            "section": "Revenue",
            "total":   v["total_rev"]["monthly"],
            "total_ytd": v["total_rev"]["ytd"],
            "subsections": [
                {
                    "subsection": "Real Estate Revenue",
                    "total":      v["total_re_rev"]["monthly"],
                    "total_ytd":  v["total_re_rev"]["ytd"],
                    "line_items": [
                        L("Lot Sales Revenue",     "lot_sales"),
                        L("Marketing Fee Income",  "mkt_fee"),
                        L("Fence Credits",         "fence"),
                    ],
                },
                {
                    "subsection": "Development & Management Income",
                    "total":      v["dev_income"]["monthly"],
                    "total_ytd":  v["dev_income"]["ytd"],
                    "line_items": [
                        L("Development/Management Income", "dev_income"),
                    ],
                },
            ],
        },
        {
            "section": "Expenses",
            "total":   v["total_exp"]["monthly"],
            "total_ytd": v["total_exp"]["ytd"],
            "subsections": [
                {
                    "subsection": "Operating Expenses",
                    "total":      v["total_op_exp"]["monthly"],
                    "total_ytd":  v["total_op_exp"]["ytd"],
                    "line_items": [
                        L("Cost of Sales - Real Estate", "cos"),
                        L("Marketing and Advertising",   "mkt_exp"),
                        L("General & Administrative",    "ga"),
                    ],
                },
            ],
        },
        {
            "section": "Other Income (Expense), Net",
            "total":   v["total_other"]["monthly"],
            "total_ytd": v["total_other"]["ytd"],
            "subsections": [
                {
                    "subsection": "Other",
                    "total":      v["total_other"]["monthly"],
                    "total_ytd":  v["total_other"]["ytd"],
                    "line_items": [
                        L("Interest Income", "int_income"),
                        L("Franchise Tax",   "franchise_tax"),
                    ],
                },
            ],
        },
    ]
    return {
        "statement":     "Income Statement",
        "monthly_label": monthly_label,
        "ytd_label":     ytd_label,
        "structure":     structure,
        "net_income": {
            "monthly": v["net_income"]["monthly"],
            "ytd":     v["net_income"]["ytd"],
        },
    }


# ─── Statement of Cash Flows (Monthly + YTD, indirect method) ─────
def compute_scf(monthly_accounts: list[dict],
                ytd_accounts: list[dict]) -> "OrderedDict[str, dict]":
    """Indirect-method SCF, per frp_builder.py's roadmap methodology.
    Each line: change in balance from period open to close, in cash-
    flow direction (assets decrease = cash in, liabilities increase
    = cash in)."""
    monthly_accounts = _normalize_accounts(monthly_accounts)
    ytd_accounts     = _normalize_accounts(ytd_accounts)
    def both(predicate):
        return {
            "monthly": cash_change(monthly_accounts, predicate),
            "ytd":     cash_change(ytd_accounts,     predicate),
        }
    v: "OrderedDict[str, dict]" = OrderedDict()
    # Operating Activities
    v["ni"]            = both(is_income_stmt)
    v["wip_reclass"]   = both(is_contra_sales)
    v["mud_adj"]       = both(lambda p: p == 16902)
    v["recv_chg"]      = both(is_receivables)
    v["prep_chg"]      = both(is_prepaids)
    v["ap_chg"]        = both(lambda p: p == 20030)
    v["ret_chg"]       = both(lambda p: p == 20060)
    v["tax_chg"]       = both(lambda p: p == 20108)
    v["rp_chg"]        = both(lambda p: p in RELATED_PARTY_ACCTS)
    v["earn_chg"]      = both(lambda p: p == 21060)
    v["def_chg"]       = both(lambda p: p == 20020)
    v["other_op_chg"]  = both(is_other_op_liab)
    # Investing Activities
    v["re_dev"]        = both(is_re_invest)
    v["land_purch"]    = both(is_land)
    v["bond_cash"]     = both(is_restricted_bonds)
    v["ppe_chg"]       = both(is_ppe)
    v["prom_chg"]      = both(is_promissory)
    # Financing Activities
    v["loan_chg"]      = both(is_dev_loan)
    v["bond_st_chg"]   = both(lambda p: p == 21015)
    v["bond_lt_chg"]   = both(is_bond_lt)
    v["mem_chg"]       = both(is_members_eq)

    def add(*keys):
        return {
            "monthly": sum(v[k]["monthly"] for k in keys if k in v),
            "ytd":     sum(v[k]["ytd"]     for k in keys if k in v),
        }
    v["net_op"]  = add("ni", "wip_reclass", "mud_adj", "recv_chg", "prep_chg",
                       "ap_chg", "ret_chg", "tax_chg", "rp_chg", "earn_chg",
                       "def_chg", "other_op_chg")
    v["net_inv"] = add("re_dev", "land_purch", "bond_cash", "ppe_chg", "prom_chg")
    v["net_fin"] = add("loan_chg", "bond_st_chg", "bond_lt_chg", "mem_chg")
    v["net_change"] = add("net_op", "net_inv", "net_fin")

    # Cash beginning / ending — pull straight from the monthly TB
    # for monthly view; YTD uses YTD TB so opening = Jan 1.
    v["cash_beg"]  = {
        "monthly": sum_accounts(monthly_accounts, is_cash, "opening"),
        "ytd":     sum_accounts(ytd_accounts,     is_cash, "opening"),
    }
    cash_end_val = sum_accounts(monthly_accounts, is_cash, "closing")
    v["cash_end"]  = {"monthly": cash_end_val, "ytd": cash_end_val}
    # Footing: beg + net_change == end
    v["footing"]   = {
        "monthly": v["cash_beg"]["monthly"] + v["net_change"]["monthly"] - v["cash_end"]["monthly"],
        "ytd":     v["cash_beg"]["ytd"]     + v["net_change"]["ytd"]     - v["cash_end"]["ytd"],
    }
    return v


def render_scf(v: dict, monthly_label: str = "Current Month",
               ytd_label: str = "Year To Date") -> dict:
    """Emit SCF in the same structure shape as BS/IS."""
    def L(name, key):
        return {
            "line_item": name,
            "value":     v[key]["monthly"],
            "value_ytd": v[key]["ytd"],
        }
    structure = [
        {
            "section": "Cash Flows from Operating Activities",
            "total":   v["net_op"]["monthly"],
            "total_ytd": v["net_op"]["ytd"],
            "subsections": [{
                "subsection": "Operating",
                "total":      v["net_op"]["monthly"],
                "total_ytd":  v["net_op"]["ytd"],
                "line_items": [
                    L("Net Income (Loss)",                              "ni"),
                    L("Cost of Sales - WIP Reclassification (non-cash)", "wip_reclass"),
                    L("MUD Receivable - Bond Proceeds Adj (non-cash)",   "mud_adj"),
                    L("Changes in Receivables",                          "recv_chg"),
                    L("Changes in Prepaid Expenses",                     "prep_chg"),
                    L("Changes in Accounts Payable",                     "ap_chg"),
                    L("Changes in Retainage Payable",                    "ret_chg"),
                    L("Changes in Taxes Payable",                        "tax_chg"),
                    L("Changes in Related Party Payables",               "rp_chg"),
                    L("Changes in Earnest Money Deposits",               "earn_chg"),
                    L("Changes in Deferred Revenue",                     "def_chg"),
                    L("Changes in Other Operating Liabilities",          "other_op_chg"),
                ],
            }],
        },
        {
            "section": "Cash Flows from Investing Activities",
            "total":   v["net_inv"]["monthly"],
            "total_ytd": v["net_inv"]["ytd"],
            "subsections": [{
                "subsection": "Investing",
                "total":      v["net_inv"]["monthly"],
                "total_ytd":  v["net_inv"]["ytd"],
                "line_items": [
                    L("Real Estate Development Expenditures",  "re_dev"),
                    L("Purchases of Land",                     "land_purch"),
                    L("Changes in Restricted Cash (Bond Funds)", "bond_cash"),
                    L("Purchases of PP&E",                     "ppe_chg"),
                    L("Changes in Promissory Note",            "prom_chg"),
                ],
            }],
        },
        {
            "section": "Cash Flows from Financing Activities",
            "total":   v["net_fin"]["monthly"],
            "total_ytd": v["net_fin"]["ytd"],
            "subsections": [{
                "subsection": "Financing",
                "total":      v["net_fin"]["monthly"],
                "total_ytd":  v["net_fin"]["ytd"],
                "line_items": [
                    L("Net Borrowings/(Repayments) - Development Loan", "loan_chg"),
                    L("Changes in Bond Payable - Short Term",           "bond_st_chg"),
                    L("Changes in Bond Payable - Long Term, Net",       "bond_lt_chg"),
                    L("Member Contributions/Distributions",             "mem_chg"),
                ],
            }],
        },
    ]
    return {
        "statement":     "Statement of Cash Flows",
        "monthly_label": monthly_label,
        "ytd_label":     ytd_label,
        "structure":     structure,
        "net_change":    v["net_change"],
        "cash_beg":      v["cash_beg"],
        "cash_end":      v["cash_end"],
        "footing":       v["footing"],
    }


# ─── Render BS in the shape the /financials template expects ──────
#
# Matches the output of _fin_render_bs(accounts) in app.py:
#   {
#     "structure": [
#       {"section": "...", "total": <num>, "subsections": [
#         {"subsection": "...", "total": <num>, "line_items": [
#           {"line_item": "...", "value": <num>, "is_contra": <bool>}, ...
#         ]}, ...
#       ]}, ...
#     ],
#     "total_assets":      <num>,
#     "total_liab_and_eq": <num>,
#     "footing_check":     <num>,
#   }
def render_bs(v: dict) -> dict:
    def L(name, val, contra=False):
        return {"line_item": name, "value": val, "is_contra": contra}

    structure = [
        {
            "section": "Assets",
            "total":   v["total_assets"],
            "subsections": [
                {
                    "subsection": "Current Assets",
                    "total":      v["total_ca"],
                    "line_items": [
                        L("Cash and Cash Equivalents",         v["cash"]),
                        L("Restricted Funds - Bonds",          v["restricted"]),
                        L("Receivables, Net",                  v["receivables"]),
                        L("Prepaids and Other Current Assets", v["prepaids"]),
                    ],
                },
                {
                    "subsection": "Non-Current Assets",
                    "total":      v["total_nca"],
                    "line_items": [
                        L("Land",                                v["land"]),
                        L("Development",                         v["development"]),
                        L("Contra Real Estate - Sales",          v["contra_sales"], contra=True),
                        L("Contra Real Estate - MUD Receivable", v["contra_mud"],   contra=True),
                        L("Promissory Note - MUD Board fee",     v["promissory"]),
                        L("Property, Plant and Equipment, Net",  v["ppe"]),
                    ],
                },
            ],
        },
        {
            "section": "Liabilities and Equity",
            "total":   v["total_liab_eq"],
            "subsections": [
                {
                    "subsection": "Current Liabilities",
                    "total":      v["total_cl"],
                    "line_items": [
                        L("Trade Payables",            v["trade_pay"]),
                        L("Retention",                 v["retention"]),
                        L("Related Party Payables",    v["rp_pay"]),
                        L("Taxes Payable",             v["tax_pay"]),
                        L("Other Current Liabilities", v["other_cl"]),
                        L("Builder Earnest Money",     v["earnest"]),
                        L("Bond Payable - Short Term", v["bond_st"]),
                    ],
                },
                {
                    "subsection": "Non-Current Liabilities",
                    "total":      v["total_ncl"],
                    "line_items": [
                        L("Development Loan Payable", v["dev_loan"]),
                        L("Bond Payable - Long Term", v["bond_lt"]),
                        L("Deferred Income",          v["deferred"]),
                    ],
                },
                {
                    "subsection": "Equity",
                    "total":      v["total_eq"],
                    "line_items": [
                        L("Members' Equity",   v["members_eq"]),
                        L("Retained Earnings", v["retained"]),
                    ],
                },
            ],
        },
    ]
    return {
        "structure":         structure,
        "total_assets":      v["total_assets"],
        "total_liab_and_eq": v["total_liab_eq"],
        "footing_check":     v["total_assets"] - v["total_liab_eq"],
    }
