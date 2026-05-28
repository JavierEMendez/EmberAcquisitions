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


def sum_accounts(accounts: list[dict], predicate, field: str = "closing") -> float:
    total = 0.0
    for a in accounts:
        if predicate(get_prefix(a["num"])):
            total += a[field]
    return total


# ─── BS value computation (FRP-shaped) ────────────────────────────
def compute_bs(accounts: list[dict]) -> "OrderedDict[str, float]":
    """Roll up TB accounts into the canonical FRP balance-sheet shape.

    Sign convention: assets positive, liabilities/equity flipped to
    positive for display. Retained Earnings includes the YTD income-
    statement roll-up plus account 30500 + 30550 — this is what
    closes the footing the GLENTRY approach left $7.5M open.
    """
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
