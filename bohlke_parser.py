"""
bohlke_parser.py — Parses the Bohlke competitive-set Excel file
(e.g. "Ember - The Grand Prairie Competitive Set thru Q4-2025.xlsx")
and returns structured JSON for the Bohlke Report dashboard tab.

The file contains three sheets we care about:

  • YEARLY TOTALS — annual Waller ISD permit totals, avg size/price/PPSF,
    30yr rate and notable events.
  • COMPS         — community-level permit counts and 2025 size/price/PPSF.
                    Communities are grouped under ISD headers (e.g.
                    "WALLER ISD", "KATY ISD"). We only keep rows under
                    the "WALLER ISD" header.
  • MPCS          — builder-level permit counts grouped under MPC headers
                    (e.g. "GRAND PRAIRIE", "BRIDGELAND"). We only keep
                    builder rows under the "GRAND PRAIRIE" header.

Ported from the coworker's bulkImportExcel() JS at
_sales_dashboard_ref/index.html lines 2825-2913.
"""

from __future__ import annotations

import io
from typing import Any

import openpyxl


# ── Helpers ─────────────────────────────────────────────────────────────────

def _s(val: Any) -> str:
    return "" if val is None else str(val).strip()


def _num(val: Any) -> float | None:
    """Coerce to float; return None for blanks / non-numeric."""
    if val is None or val == "":
        return None
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


def _int_or_none(val: Any) -> int | None:
    n = _num(val)
    return None if n is None else int(round(n))


def _round2(val: Any) -> float | None:
    n = _num(val)
    return None if n is None else round(n, 2)


# ── Sheet parsers ───────────────────────────────────────────────────────────

def _parse_yearly(ws) -> list[dict]:
    """
    YEARLY TOTALS — row 3+ contains per-year data.
      col A (0): year
      col B (1): total permits
      col D (3): avg home size
      col F (5): avg base price
      col H (7): avg PPSF
      col J (9): 30yr rate (stored as fraction, e.g. 0.065)
      col K (10): events
    """
    rows = list(ws.iter_rows(values_only=True))
    out: list[dict] = []
    # Match JS: "for (let i = 2; i < rows.length; i++)" — skip first two rows
    for i in range(2, len(rows)):
        r = rows[i]
        if not r or not r[0]:
            continue
        year = _int_or_none(r[0])
        if year is None:
            continue
        rate = _num(r[9]) if len(r) > 9 else None
        out.append({
            "year":     year,
            "total":    _int_or_none(r[1])               if len(r) > 1  else None,
            "avgSize":  _int_or_none(r[3])               if len(r) > 3  else None,
            "avgPrice": _int_or_none(r[5])               if len(r) > 5  else None,
            "avgPPSF":  _round2(r[7])                    if len(r) > 7  else None,
            "rate30yr": round(rate * 100, 2) if rate else None,
            "events":   _s(r[10]) or None                if len(r) > 10 else None,
        })
    return out


def _parse_comps(ws) -> list[dict]:
    """
    COMPS — community rows grouped by ISD headers.
      col A (0): community name OR section marker like "WALLER ISD"
      col B (1): MPC type (defaults to "NON")
      col C (2): permits 2023
      col D (3): permits 2024
      col E (4): permits 2025
      col H (7): avg size 2025
      col K (10): avg base price 2025
      col N (13): avg PPSF 2025
    """
    rows = list(ws.iter_rows(values_only=True))
    out: list[dict] = []
    in_waller = False
    for i in range(2, len(rows)):
        r = rows[i]
        if not r:
            continue
        name = _s(r[0]) if len(r) > 0 else ""
        # Section transitions
        if name == "WALLER ISD":
            in_waller = True
            continue
        if " ISD" in name and name != "WALLER ISD":
            in_waller = False
            continue
        if not in_waller or not name:
            continue
        # Skip rows with no permit data across 2023-2025
        p23 = _num(r[2]) if len(r) > 2 else None
        p24 = _num(r[3]) if len(r) > 3 else None
        p25 = _num(r[4]) if len(r) > 4 else None
        if not p23 and not p24 and not p25:
            continue
        out.append({
            "community": name,
            "mpc":       (_s(r[1]) if len(r) > 1 and _s(r[1]) else "NON"),
            "p23":       int(p23) if p23 else 0,
            "p24":       int(p24) if p24 else 0,
            "p25":       int(p25) if p25 else 0,
            "size25":    _int_or_none(r[7])  if len(r) > 7  else None,
            "price25":   _int_or_none(r[10]) if len(r) > 10 else None,
            "ppsf25":    _round2(r[13])      if len(r) > 13 else None,
        })
    return out


def _parse_tgp_builders(ws) -> list[dict]:
    """
    MPCS — builder rows grouped by MPC name in col A.
      When col A contains "GRAND PRAIRIE" we enter the TGP section.
      Inside the section:
        col D (3): builder name
        col G (6): 2025 permits
        col P (15): PPSF
      Section ends when we hit a new non-empty col A with no col D value.
    Permits are summed per builder and PPSF is permit-weighted.
    """
    rows = list(ws.iter_rows(values_only=True))
    in_tgp = False
    bt: dict[str, dict] = {}
    for i in range(2, len(rows)):
        r = rows[i]
        if not r:
            continue
        name = _s(r[0]).upper() if len(r) > 0 else ""
        builder = _s(r[3]) if len(r) > 3 else ""
        # Enter TGP section
        if "GRAND PRAIRIE" in name:
            in_tgp = True
            continue
        # Exit when a new section header appears (col A set, col D blank)
        if in_tgp and name and not builder:
            in_tgp = False
            continue
        if not in_tgp or not builder or "Total" in builder:
            continue
        p25 = _num(r[6]) if len(r) > 6 else None
        ppsf = _num(r[15]) if len(r) > 15 else None
        if not p25 or p25 <= 0:
            continue
        if builder not in bt:
            bt[builder] = {"builder": builder, "permits": 0, "_ps": 0.0, "_pc": 0}
        bt[builder]["permits"] += int(p25)
        if ppsf:
            bt[builder]["_ps"] += ppsf * p25
            bt[builder]["_pc"] += p25

    result = []
    for b in bt.values():
        avg = round(b["_ps"] / b["_pc"], 2) if b["_pc"] else None
        result.append({"builder": b["builder"], "permits": b["permits"], "avgPPSF": avg})
    result.sort(key=lambda x: x["permits"], reverse=True)
    return result


# ── Entry point ─────────────────────────────────────────────────────────────

def parse_bohlke(file_bytes: bytes) -> dict:
    """
    Parse a Bohlke competitive-set xlsx and return:
      {
        "yearly":       [ {year, total, avgSize, avgPrice, avgPPSF, rate30yr, events}, ... ],
        "comps":        [ {community, mpc, p23, p24, p25, size25, price25, ppsf25}, ... ],
        "tgp_builders": [ {builder, permits, avgPPSF}, ... ],
      }
    """
    wb = openpyxl.load_workbook(io.BytesIO(file_bytes), data_only=True, read_only=True)

    data: dict[str, Any] = {"yearly": [], "comps": [], "tgp_builders": []}

    if "YEARLY TOTALS" in wb.sheetnames:
        data["yearly"] = _parse_yearly(wb["YEARLY TOTALS"])
    if "COMPS" in wb.sheetnames:
        data["comps"] = _parse_comps(wb["COMPS"])
    if "MPCS" in wb.sheetnames:
        data["tgp_builders"] = _parse_tgp_builders(wb["MPCS"])

    wb.close()
    return data
