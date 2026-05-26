"""
Financial Reporting Package mapping rules.

Translates Sage Intacct trial-balance rows into the line-item buckets
used by the entity FRP workbooks (Balance Sheet · Income Statement ·
Statement of Cash Flows). Rules were reverse-engineered by comparing
the GPD (Grand Prairie Development) March-2026 trial balance to the
hand-curated FRP and verifying every line item reconciles to the
penny.

Phase 1 scope:
  * Balance Sheet only.
  * Retained Earnings = the Sage "Retained earnings" account balance
    as-is (does NOT yet roll current-year YTD net income into RE).
    Phase 2 will add IS rendering + YTD-NI rollup so the BS foots.

The rules are entity-agnostic — they match on account-number prefix
plus title patterns from the standard chart of accounts (1,351 acts).
A different entity simply has a different subset of accounts active;
the line items with zero value are hidden by the renderer.

If you add new GL account types, extend RULES_BS below — the first
matching rule wins. Order matters only for ambiguous titles
(e.g., "Earnest money" precedes the generic 20xxx Other Current
catch-all so 21060 doesn't fall through to that bucket).
"""
from __future__ import annotations

from collections import OrderedDict
from typing import Iterable, Optional


# ─── BS section / line-item ordering ─────────────────────────────────────────
# The renderer walks this in order; line items with zero rolled-up
# value are skipped so each entity's BS shows only its populated lines.
# `flip_sign`: TB stores credit-normal accounts as negative; for the
# Liabilities & Equity sections we flip so values display positive.
# `is_contra`: shown as negative within an otherwise-positive section
# (e.g., Contra Real Estate lines reducing the Development buildup).

BS_LAYOUT = [
    # section,                    subsection,              line_item,                              flip_sign, is_contra
    ("Assets",                    "Current Assets",        "Cash and Cash Equivalents",            False, False),
    ("Assets",                    "Current Assets",        "Restricted Funds - Bonds",             False, False),
    ("Assets",                    "Current Assets",        "Receivables, Net",                     False, False),
    ("Assets",                    "Current Assets",        "Prepaids and Other Current Assets",    False, False),
    ("Assets",                    "Non-Current Assets",    "Land",                                 False, False),
    ("Assets",                    "Non-Current Assets",    "Development",                          False, False),
    ("Assets",                    "Non-Current Assets",    "Contra Real Estate - Sales",           False, True),
    ("Assets",                    "Non-Current Assets",    "Contra Real Estate - MUD Receivable",  False, True),
    ("Assets",                    "Non-Current Assets",    "Promissory Note - MUD Board fee",      False, False),
    ("Assets",                    "Non-Current Assets",    "Property, Plant and Equipment, Net",   False, False),
    ("Liabilities and Equity",    "Current Liabilities",   "Trade Payables",                       True,  False),
    ("Liabilities and Equity",    "Current Liabilities",   "Retention",                            True,  False),
    ("Liabilities and Equity",    "Current Liabilities",   "Related Party Payables",               True,  False),
    ("Liabilities and Equity",    "Current Liabilities",   "Taxes Payable",                        True,  False),
    ("Liabilities and Equity",    "Current Liabilities",   "Other Current Liabilities",            True,  False),
    ("Liabilities and Equity",    "Current Liabilities",   "Builder Earnest Money",                True,  False),
    ("Liabilities and Equity",    "Current Liabilities",   "Bond Payable - Short Term",            True,  False),
    ("Liabilities and Equity",    "Non-Current Liabilities","Development Loan Payable",            True,  False),
    ("Liabilities and Equity",    "Non-Current Liabilities","Bond Payable - Long Term",            True,  False),
    ("Liabilities and Equity",    "Non-Current Liabilities","Deferred Income",                     True,  False),
    ("Liabilities and Equity",    "Equity",                "Members' Equity",                      True,  False),
    ("Liabilities and Equity",    "Equity",                "Retained Earnings",                    True,  False),
]


# ─── Classification rules ────────────────────────────────────────────────────
# Each rule is (line_item, predicate). First match wins. Predicates take
# (account_no_str, account_title_str) — both already trimmed. Account
# numbers from Sage are always strings, never numeric.

def _prefix(no: str, *prefixes: str) -> bool:
    return any(no.startswith(p) for p in prefixes)

def _title_contains(title: str, *needles_lower: str) -> bool:
    t = title.lower()
    return any(n in t for n in needles_lower)

def _title_contains_case(title: str, *needles: str) -> bool:
    """Case-SENSITIVE — used for "MUD Receivable" (capital R, contra to
    asset) vs "MUD receivable" (lowercase r, contra to sales) which are
    DIFFERENT FRP line items in the GPD COA."""
    return any(n in title for n in needles)

def _first_word_lower(title: str) -> str:
    """Lowercased first word of the title. Used by the Land rule to
    distinguish "Land - GPD" (real Land bucket) from "WIP - Landscape
    Architecture" or "Closing costs - land" (Development buckets)."""
    parts = title.strip().split()
    return parts[0].lower() if parts else ""


RULES_BS = [
    # ── Assets · Current ─────────────────────────────────────────────────
    ("Cash and Cash Equivalents",
        lambda no, t: _prefix(no, "10") and (
            _title_contains(t, "cash") or _title_contains(t, "money market")
        ) and not _title_contains(t, "bond")
    ),
    ("Restricted Funds - Bonds",
        lambda no, t: _prefix(no, "10") and _title_contains(t, "special revenue bond")
    ),
    # Catch any remaining 10xxx (clearing etc.) as cash, since FRPs lump
    # them with Cash and Cash Equivalents in every entity I've seen.
    ("Cash and Cash Equivalents",
        lambda no, t: _prefix(no, "10")
    ),
    ("Receivables, Net",
        lambda no, t: _prefix(no, "11")
    ),
    ("Prepaids and Other Current Assets",
        lambda no, t: _prefix(no, "12")
    ),

    # ── Assets · Non-Current ─────────────────────────────────────────────
    # 13xxx houses Promissory Notes, LT Due From, MUD/SID receivables (LT).
    # FRP's "Promissory Note - MUD Board fee" specifically isolates the
    # MUD-board promissory note. Anything else in 13xxx rolls into the
    # generic "Promissory Note - MUD Board fee" bucket too (rare —
    # GPD's 13xxx is a single account that matches the FRP line exactly).
    ("Promissory Note - MUD Board fee",
        lambda no, t: _prefix(no, "13")
    ),
    # 14xxx (FFE) + 15xxx (intangibles, software) → PPE net.
    ("Property, Plant and Equipment, Net",
        lambda no, t: _prefix(no, "14", "15")
    ),

    # ── Real Estate buckets (16xxx-19xxx, the big 725-account family) ────
    # Order matters here. Contra accounts get matched first so the
    # Development catch-all doesn't swallow them.
    ("Contra Real Estate - MUD Receivable",
        lambda no, t: _prefix(no, "16", "17", "18", "19")
                       and _title_contains_case(t, "MUD Receivable")
    ),
    ("Contra Real Estate - Sales",
        lambda no, t: _prefix(no, "16", "17", "18", "19")
                       and (_title_contains(t, "wip release")
                            or _title_contains_case(t, "MUD receivable"))
    ),
    ("Land",
        # First-word match so "WIP - Landscape Architecture" and
        # "Closing costs - land" stay in Development (they incidentally
        # contain "land" but aren't land-cost accounts).
        lambda no, t: _prefix(no, "16", "17", "18", "19")
                       and _first_word_lower(t) == "land"
    ),
    # Everything else in 16-19xxx is Development (WIP, brokerage,
    # title policy, closing costs, interim carrying, etc.).
    ("Development",
        lambda no, t: _prefix(no, "16", "17", "18", "19")
    ),

    # ── Liabilities · Current ────────────────────────────────────────────
    # Specific titles win before the generic 20xxx/21xxx catch-alls.
    # "Earnest money" appears at 21060 in GPD (not 20xxx) so the rule
    # has to match on title irrespective of prefix.
    ("Builder Earnest Money",
        lambda no, t: _title_contains(t, "earnest money")
    ),
    ("Deferred Income",
        lambda no, t: _title_contains(t, "deferred")
    ),
    ("Bond Payable - Short Term",
        lambda no, t: t.strip().lower() == "bond payable - short term"
    ),
    # Bond Payable - LT + contra-liability discounts that reduce it
    ("Bond Payable - Long Term",
        lambda no, t: t.strip().lower() == "bond payable - long term"
                       or _title_contains(t, "discount") and _title_contains(t, "contra")
                       or (_prefix(no, "21") and _title_contains(t, "discount"))
    ),
    ("Retention",
        lambda no, t: _prefix(no, "20")
                       and (_title_contains(t, "retainage") or _title_contains(t, "retention"))
    ),
    ("Taxes Payable",
        lambda no, t: _prefix(no, "20") and _title_contains(t, "tax")
    ),
    ("Trade Payables",
        lambda no, t: _prefix(no, "20") and _title_contains(t, "accounts payable")
    ),
    ("Related Party Payables",
        lambda no, t: _prefix(no, "21") and t.lower().startswith("due to")
    ),
    # 20xxx catch-all goes last so the specific buckets above win.
    ("Other Current Liabilities",
        lambda no, t: _prefix(no, "20")
    ),

    # ── Liabilities · Non-Current ────────────────────────────────────────
    ("Development Loan Payable",
        lambda no, t: _prefix(no, "22")
    ),

    # ── Equity ───────────────────────────────────────────────────────────
    ("Retained Earnings",
        lambda no, t: _prefix(no, "30") and _title_contains(t, "retained earnings")
    ),
    # All other 30xxx (Contributions, Distributions, Opening equity) →
    # Members' Equity per the FRP convention.
    ("Members' Equity",
        lambda no, t: _prefix(no, "30")
    ),
]


def classify_bs(account_no: str, account_title: str) -> Optional[str]:
    """Return the FRP BS line item for an account, or None if the
    account isn't a balance-sheet account (e.g. 4xxxx revenue,
    5xxxx COGS, etc.)."""
    no = (account_no or "").strip()
    title = (account_title or "").strip()
    if not no:
        return None
    for line_item, predicate in RULES_BS:
        try:
            if predicate(no, title):
                return line_item
        except Exception:  # never let a buggy predicate break the whole roll-up
            continue
    return None


# ─── Roll-up ─────────────────────────────────────────────────────────────────
def roll_up_balance_sheet(tb_rows: Iterable[dict]) -> list[dict]:
    """Aggregate trial-balance rows into the FRP BS layout.

    Input: iterable of TB rows with at minimum {'no', 'name', 'close'}.
    The `close` values are signed Sage style — credit-normal accounts
    are negative.

    Output: ordered list of section/line-item rows:
        [{'section': 'Assets',
          'subsection': 'Current Assets',
          'line_item': 'Cash and Cash Equivalents',
          'value': 6_238_973.00,
          'is_contra': False,
          'accounts': [{'no': ..., 'name': ..., 'close': ...}, ...]}, ...]

    Sections that have a nonzero subsection total are kept; line items
    with exactly zero (no activity, no balance) are dropped so the
    rendered BS only shows what the entity actually has.
    """
    # Bucket TB rows by line item
    buckets: OrderedDict[str, list[dict]] = OrderedDict()
    unmatched: list[dict] = []
    for row in tb_rows:
        line = classify_bs(row.get("no", ""), row.get("name", ""))
        if line is None:
            unmatched.append(row)
            continue
        buckets.setdefault(line, []).append(row)

    # Walk the canonical layout in order and emit nonzero rows
    out: list[dict] = []
    for section, subsection, line_item, flip_sign, is_contra in BS_LAYOUT:
        members = buckets.get(line_item, [])
        if not members:
            continue
        # Sage TB convention: credit-normal accounts come back negative.
        # Liability/Equity sections flip sign for display.
        signed_sum = sum(float(r.get("close", 0) or 0) for r in members)
        value = -signed_sum if flip_sign else signed_sum
        # Snap sub-cent floats to zero so we don't show "$0" rows that
        # accumulated from rounding noise.
        if abs(value) < 0.5:
            continue
        out.append({
            "section":    section,
            "subsection": subsection,
            "line_item":  line_item,
            "value":      value,
            "is_contra":  is_contra,
            "accounts":   [
                {"no": r.get("no",""), "name": r.get("name",""), "close": float(r.get("close",0) or 0)}
                for r in members
            ],
        })

    return out


def summarize_bs(rolled: list[dict]) -> dict:
    """Compute section / subsection subtotals + footing check.
    Output matches the structure the template will render."""
    structure: OrderedDict = OrderedDict()
    for row in rolled:
        sec = row["section"]
        sub = row["subsection"]
        structure.setdefault(sec, OrderedDict())
        structure[sec].setdefault(sub, []).append(row)

    # Section/subsection totals
    section_totals: dict[str, float] = {}
    subsection_totals: dict[tuple[str, str], float] = {}
    for sec, subs in structure.items():
        s_total = 0.0
        for sub, rows in subs.items():
            sub_total = sum(r["value"] for r in rows)
            subsection_totals[(sec, sub)] = sub_total
            s_total += sub_total
        section_totals[sec] = s_total

    total_assets = section_totals.get("Assets", 0.0)
    total_liab_eq = section_totals.get("Liabilities and Equity", 0.0)
    footing = total_assets - total_liab_eq

    return {
        "structure":          structure,
        "section_totals":     section_totals,
        "subsection_totals":  subsection_totals,
        "total_assets":       total_assets,
        "total_liab_and_eq":  total_liab_eq,
        "footing_check":      footing,
    }
