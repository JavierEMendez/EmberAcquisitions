"""
Community Sales Tracker — data pipeline.

Pulls live sales/cancellation data from the Pipsy GraphQL API for three
Ember communities (The Grand Prairie, Highlands, Windrose Green), computes
all analytics (monthly net sales, builder/lot breakdowns, weekly detail,
pacing vs. target, all-time totals), merges in manually-maintained starts
inventory rows, and returns a single JSON-serializable dict.

Ported from the reference repo (csaldierna1-bot/ember-sales-dashboard1,
update_dashboard.py). HTML generation and Excel parsing are intentionally
excluded — the frontend (templates/sales.html) renders everything from
the JSON this module returns.

Set PIPSY_API_TOKEN in environment (Railway Variables) — the module
refuses to run without it.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
import urllib.request
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Optional

# ─── Pipsy GraphQL API ────────────────────────────────────────────────────────

PIPSY_API_URL = "https://ember-server.pipsy.io/secure"
SALES_FIELDS = (
    "id sale_date builder_name lot_type price address section "
    "cancel_date cancel_reason"
)

# Property IDs on the Pipsy side
PROP_GPD = 1  # The Grand Prairie
PROP_HLD = 3  # The Grand Prairie - Highlands
PROP_WRG = 2  # Windrose Green


def _token() -> str:
    t = os.environ.get("PIPSY_API_TOKEN")
    if not t:
        raise RuntimeError(
            "PIPSY_API_TOKEN environment variable is not set. "
            "Add it to Railway Variables before the sales dashboard can load."
        )
    return t


def graphql_query(query: str) -> dict:
    """Send a GraphQL query to Pipsy and return the parsed data payload."""
    body = json.dumps({"query": query}).encode("utf-8")
    req = urllib.request.Request(
        PIPSY_API_URL,
        data=body,
        headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer " + _token(),
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        raw = resp.read().decode("utf-8", errors="replace")
    result = json.loads(raw)
    if "errors" in result:
        raise RuntimeError("Pipsy GraphQL error: " + json.dumps(result["errors"]))
    return result.get("data", {})


def fetch_sales_records(property_id: int, cancels: bool = False) -> list:
    """Fetch all sales (or cancellations) for a property, across all history."""
    date_from = int(datetime(2020, 1, 1).timestamp())
    date_to = int(datetime.now().timestamp()) + 86400
    cancel_flag = "true" if cancels else "false"
    q = "{ sales(property: [%d], dateFrom: %d, dateTo: %d, cancels: %s) { %s } }" % (
        property_id,
        date_from,
        date_to,
        cancel_flag,
        SALES_FIELDS,
    )
    data = graphql_query(q)
    return data.get("sales", [])


def fetch_lot_records(property_id: int) -> list:
    """Fetch all lots for a property via Pipsy's `lots()` query. Used by
    the Underwriting Actuals overlay — each lot's takedown_date + section
    drives the "lot takedowns by section/month" actuals series."""
    q = "{ lots(property: [%d]) { id section takedown_date } }" % property_id
    data = graphql_query(q)
    return data.get("lots", [])


def _ts_to_ct_ym(ts) -> Optional[str]:
    """Convert a Pipsy timestamp (epoch seconds, possibly stringified) to
    a YYYY-MM key in America/Chicago. Returns None on bad input. Pinning
    to CT (not UTC) so late-evening CT events don't spill into next month."""
    if ts is None:
        return None
    try:
        ts_num = int(float(ts))
    except (TypeError, ValueError):
        return None
    # Try zoneinfo (stdlib 3.9+); fall back to pytz; final fallback is UTC.
    try:
        from zoneinfo import ZoneInfo
        dt = datetime.fromtimestamp(ts_num, tz=ZoneInfo("America/Chicago"))
    except Exception:
        try:
            import pytz
            dt = datetime.fromtimestamp(ts_num, tz=pytz.timezone("America/Chicago"))
        except Exception:
            dt = datetime.utcfromtimestamp(ts_num)
    return dt.strftime("%Y-%m")


def pipsy_home_sales_by_section(property_id: int) -> dict:
    """Aggregate net home sales (gross − cancel) by section + YYYY-MM.

    Returns {"Section N": {"YYYY-MM": net_count, ...}, ...}.
    Zero-net cells are stripped to match the legacy update_dashboard.py
    output shape. Used by the Underwriting Actuals overlay (#8) to plot
    actuals against UW Modeled net sales.
    """
    gross = fetch_sales_records(property_id, cancels=False)
    cancel = fetch_sales_records(property_id, cancels=True)
    result: dict = {}
    def _bump(sec, ym, delta):
        if not sec or not ym:
            return
        key = "Section " + str(sec).strip()
        result.setdefault(key, {})
        result[key][ym] = result[key].get(ym, 0) + delta
    for s in gross:
        ym = _ts_to_ct_ym(s.get("sale_date"))
        _bump(s.get("section"), ym, 1)
    for c in cancel:
        ym = _ts_to_ct_ym(c.get("cancel_date"))
        _bump(c.get("section"), ym, -1)
    # Strip zero cells + empty sections (matches coworker output).
    for sec in list(result.keys()):
        for ym in list(result[sec].keys()):
            if result[sec][ym] == 0:
                del result[sec][ym]
        if not result[sec]:
            del result[sec]
    return result


def pipsy_lot_takedowns_by_section(property_id: int) -> dict:
    """Aggregate lot takedowns by section + YYYY-MM of takedown_date.

    Returns {"Section N": {"YYYY-MM": count, ...}, ...}. Same shape as
    pipsy_home_sales_by_section but built from the `lots()` query
    instead of `sales()`. Drives the Lot Takedowns metric in the UW
    actuals overlay."""
    lots = fetch_lot_records(property_id)
    result: dict = {}
    for lot in lots:
        ym = _ts_to_ct_ym(lot.get("takedown_date"))
        sec = lot.get("section")
        if not sec or not ym:
            continue
        key = "Section " + str(sec).strip()
        result.setdefault(key, {})
        result[key][ym] = result[key].get(ym, 0) + 1
    return result


# ─── Configuration (targets + static builder data) ────────────────────────────

TARGETS = {
    "gpd_annual": 228, "gpd_monthly": 19,
    "hld_annual": 372, "hld_monthly": 31,
    "wrg_annual": 216, "wrg_monthly": 18,
    "combined_annual": 600, "combined_monthly": 50,
}

# Static avg prices / $/SF per builder (Pipsy doesn't always surface sqft)
GPD_BUILDER_STATIC = {
    ("40", "David Weekley Homes"): {"psf": "$159", "price": "$327,327"},
    ("40", "Perry Homes"):         {"psf": "$183", "price": "$332,124"},
    ("45", "Village Builders"):    {"psf": "$166", "price": "$311,392"},
    ("45", "Westin Homes"):        {"psf": "$159", "price": "$434,378"},
    ("50", "Perry Homes"):         {"psf": "$176", "price": "$450,438"},
    ("50", "David Weekley Homes"): {"psf": "$158", "price": "$395,108"},
    ("60", "Westin Homes"):        {"psf": "$154", "price": "$571,279"},
    ("60", "Sitterle Homes"):      {"psf": "$197", "price": "$594,129"},
    ("60", "David Weekley Homes"): {"psf": "$181", "price": "$512,440"},
    ("60", "Shea Homes"):          {"psf": "$179", "price": "$660,040"},
}
GPD_LOT_TOTALS_STATIC = {
    "40": {"psf": "$171", "price": "$329,689"},
    "45": {"psf": "$163", "price": "$362,777"},
    "50": {"psf": "$167", "price": "$421,376"},
    "60": {"psf": "$164", "price": "$571K"},
}
GPD_AVG_PRICE = "~$393K"

HLD_BUILDER_STATIC = {
    ("40", "Lennar Homes"): {"price": "$250,088"},
    ("45", "Lennar Homes"): {"price": "$258,490"},
    ("50", "Lennar Homes"): {"price": "$302,331"},
}
HLD_AVG_PRICE = "$260K"

WRG_BUILDER_STATIC = {
    ("40", "Lennar Homes"):           {"price": "$229,637"},
    ("40", "CastleRock Communities"): {"price": "$278,942"},
    ("45", "CastleRock Communities"): {"price": "$324,146"},
    ("45", "K. Hovnanian"):           {"price": "$320,086"},
    ("45", "Lennar Homes"):           {"price": "$224,540"},
    ("45", "Coventry Homes"):         {"price": "$279,993"},
    ("50", "K. Hovnanian"):           {"price": "$356,611"},
    ("50", "CastleRock Communities"): {"price": "$338,906"},
    ("50", "Lennar Homes"):           {"price": "$289,310"},
    ("50", "Coventry Homes"):         {"price": "$310,368"},
}
WRG_AVG_PRICE = "~$305K"

# Target pace (homes/month) per (lot, builder) — used for GPD builder scorecard
GPD_TARGET_PACE = {
    ("40", "David Weekley Homes"): 2.0,
    ("40", "Perry Homes"):         2.0,
    ("45", "Village Builders"):    4.0,
    ("45", "Westin Homes"):        2.0,
    ("50", "Perry Homes"):         3.0,
    ("50", "David Weekley Homes"): 3.0,
    ("60", "Westin Homes"):        0.75,
    ("60", "Sitterle Homes"):      0.75,
    ("60", "David Weekley Homes"): 0.75,
    ("60", "Shea Homes"):          0.75,
}

# Display order for the GPD builder summary table (lot, short_name, full_name)
GPD_BM_ORDER = [
    ("40", "DWH",      "David Weekley Homes"),
    ("40", "Perry",    "Perry Homes"),
    ("45", "Village",  "Village Builders"),
    ("45", "Westin",   "Westin Homes"),
    ("50", "Perry",    "Perry Homes"),
    ("50", "DWH",      "David Weekley Homes"),
    ("60", "Westin",   "Westin Homes"),
    ("60", "Sitterle", "Sitterle Homes"),
    ("60", "DWH",      "David Weekley Homes"),
    ("60", "Shea",     "Shea Homes"),
]

HLD_BM_ORDER = [
    ("40", "Lennar", "Lennar Homes"),
    ("45", "Lennar", "Lennar Homes"),
    ("50", "Lennar", "Lennar Homes"),
]

WRG_BM_ORDER = [
    ("40", "Lennar",     "Lennar Homes"),
    ("40", "CastleRock", "CastleRock Communities"),
    ("45", "CastleRock", "CastleRock Communities"),
    ("45", "K.Hov",      "K. Hovnanian"),
    ("45", "Lennar",     "Lennar Homes"),
    ("45", "Coventry",   "Coventry Homes"),
    ("50", "K.Hov",      "K. Hovnanian"),
    ("50", "CastleRock", "CastleRock Communities"),
    ("50", "Lennar",     "Lennar Homes"),
    ("50", "Coventry",   "Coventry Homes"),
]

# First-sale months (used to bound the x-axis on monthly charts)
TGP_START = (2023, 10)
HLD_START = (2024, 6)
WRG_START = (2024, 1)


# ─── Data extraction ──────────────────────────────────────────────────────────

def _ts_to_dt(ts):
    if ts is None:
        return None
    try:
        return datetime.fromtimestamp(int(ts))
    except Exception:
        return None


def _normalize_lot_type(val):
    """'40ft' -> '40', '45\\'' -> '45', 40 -> '40'."""
    s = str(val).strip()
    digits = re.sub(r"[^0-9]", "", s)
    return digits if digits else s


def get_month_key(dt):
    return (dt.year, dt.month)


def month_label(ym):
    names = ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"]
    return f"{names[ym[1] - 1]} {str(ym[0])[-2:]}"


def get_week_start(dt):
    """Monday of the week containing dt, at midnight."""
    monday = dt - timedelta(days=dt.weekday())
    return monday.replace(hour=0, minute=0, second=0, microsecond=0)


def week_label(monday_dt):
    names = ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"]
    return f"{names[monday_dt.month - 1]} {monday_dt.day}"


def extract_gross_sales(records):
    """Transform Pipsy records into the internal sales dict shape."""
    out = []
    for r in records:
        sale_date = _ts_to_dt(r.get("sale_date"))
        if sale_date is None:
            continue
        lot_type = r.get("lot_type")
        if lot_type is None:
            continue
        out.append({
            "date": sale_date,
            "lot_type": _normalize_lot_type(lot_type),
            "builder": (r.get("builder_name") or "Unknown").strip() or "Unknown",
            "price": r.get("price"),
            "address": str(r.get("address") or ""),
            "section": str(r.get("section") or ""),
        })
    return out


def extract_cancellations(records):
    """Transform Pipsy cancel records into the internal cancel dict shape."""
    out = []
    for r in records:
        cancel_date = _ts_to_dt(r.get("cancel_date"))
        if cancel_date is None:
            continue
        lot_type = r.get("lot_type")
        if lot_type is None:
            continue
        sale_dt = _ts_to_dt(r.get("sale_date"))
        out.append({
            "date": cancel_date,
            "lot_type": _normalize_lot_type(lot_type),
            "builder": (r.get("builder_name") or "Unknown").strip() or "Unknown",
            "sale_date_str": sale_dt.strftime("%m/%d/%Y") if sale_dt else "",
            "reason": str(r.get("cancel_reason") or ""),
            "address": str(r.get("address") or ""),
            "section": str(r.get("section") or ""),
        })
    return out


# ─── Analytics ────────────────────────────────────────────────────────────────

def generate_month_range(start_ym, end_ym):
    months = []
    y, m = start_ym
    while (y, m) <= end_ym:
        months.append((y, m))
        m += 1
        if m > 12:
            m = 1
            y += 1
    return months


def compute_monthly_net(gross, cancels, month_range):
    """Net sales per month, {(y,m): int}."""
    month_set = set(month_range)
    g = defaultdict(int)
    c = defaultdict(int)
    for s in gross:
        ym = get_month_key(s["date"])
        if ym in month_set:
            g[ym] += 1
    for x in cancels:
        ym = get_month_key(x["date"])
        if ym in month_set:
            c[ym] += 1
    return {ym: g[ym] - c[ym] for ym in month_range}


def compute_builder_month_net(gross, cancels, month_range):
    g = defaultdict(lambda: defaultdict(int))
    c = defaultdict(lambda: defaultdict(int))
    for s in gross:
        g[(s["lot_type"], s["builder"])][get_month_key(s["date"])] += 1
    for x in cancels:
        c[(x["lot_type"], x["builder"])][get_month_key(x["date"])] += 1
    keys = set(g.keys()) | set(c.keys())
    return {
        k: {ym: g[k].get(ym, 0) - c[k].get(ym, 0) for ym in month_range}
        for k in keys
    }


def compute_weekly_data(gross, cancels, year):
    """Weekly gross/cancel/net for a year (Monday-start weeks)."""
    year_g = [s for s in gross if s["date"].year == year]
    year_c = [x for x in cancels if x["date"].year == year]

    g_by_w = defaultdict(int)
    c_by_w = defaultdict(int)
    g_detail = defaultdict(lambda: defaultdict(int))
    c_detail = defaultdict(lambda: defaultdict(int))

    for s in year_g:
        ws = get_week_start(s["date"])
        if ws.year < year:
            continue
        g_by_w[ws] += 1
        g_detail[(s["lot_type"], s["builder"])][ws] += 1
    for x in year_c:
        ws = get_week_start(x["date"])
        if ws.year < year:
            continue
        c_by_w[ws] += 1
        c_detail[(x["lot_type"], x["builder"])][ws] += 1

    first_monday = datetime(year, 1, 1)
    while first_monday.weekday() != 0:
        first_monday += timedelta(days=1)

    active = set(g_by_w.keys()) | set(c_by_w.keys())
    today = datetime.now()
    today_week = today - timedelta(days=today.weekday())
    today_week = datetime(today_week.year, today_week.month, today_week.day)
    if today_week.year == year:
        active.add(today_week)

    if not active:
        return {"labels": [], "gross": [], "cancel": [], "builder_weekly": {}}

    last = max(active)
    weeks = []
    cur = first_monday
    while cur <= last:
        weeks.append(cur)
        cur += timedelta(days=7)

    labels = [week_label(w) for w in weeks]
    gross_series = [g_by_w[w] for w in weeks]
    cancel_series = [-c_by_w[w] for w in weeks]

    builder_weekly = {}
    for k in set(g_detail.keys()) | set(c_detail.keys()):
        gg = [g_detail[k].get(w, 0) for w in weeks]
        cc = [-c_detail[k].get(w, 0) for w in weeks]
        nn = [gg[i] + cc[i] for i in range(len(weeks))]
        # Serialize key as "lot|builder" so it survives JSON
        builder_weekly[f"{k[0]}|{k[1]}"] = {"g": gg, "c": cc, "n": nn}

    return {
        "labels": labels,
        "gross": gross_series,
        "cancel": cancel_series,
        "builder_weekly": builder_weekly,
    }


def compute_all_time_totals(gross, cancels):
    """All-time gross/cancel/net per (lot, builder)."""
    g = defaultdict(int)
    c = defaultdict(int)
    for s in gross:
        g[(s["lot_type"], s["builder"])] += 1
    for x in cancels:
        c[(x["lot_type"], x["builder"])] += 1
    keys = set(g.keys()) | set(c.keys())
    return {f"{k[0]}|{k[1]}": {"tg": g[k], "tc": -c[k], "tn": g[k] - c[k]} for k in keys}


def compute_avg_pace(net_by_month, months_list):
    if not months_list:
        return 0.0
    total = sum(net_by_month.get(m, 0) for m in months_list)
    return round(total / len(months_list), 2)


# ─── Starts tracking ──────────────────────────────────────────────────────────

def _parse_drive_date(s):
    if not s:
        return None
    s = s.strip()
    for fmt in ("%m/%d/%y", "%m/%d/%Y"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


def _norm_section(sec):
    if sec is None:
        return ""
    s = str(sec).strip().lower()
    for pref in ("section ", "sec. ", "sec ", "sect. ", "sect "):
        if s.startswith(pref):
            s = s[len(pref):].strip()
            break
    s = s.rstrip(".").strip()
    if s.isdigit():
        s = s.lstrip("0") or "0"
    return s


def compute_starts_rows(rows, drive_dates, gross, cancels):
    """Auto-populate Homes Sold / Cancellations / Net Sales in a starts-row list.

    Each "row" is a dict: {lot, bld, sec, del, metric, vals}.
    Completed Homes rows carry the section — we carry it down to the sold/cancel/net rows.
    vals[0] is the pre-first-drive bucket and always stays "".
    vals[i] for i>=1 counts records where drive_dates[i-1] < date <= drive_dates[i].
    """
    drive_dts = [_parse_drive_date(d) for d in drive_dates or []]
    n_cols = len(drive_dts)
    if n_cols == 0:
        return rows

    # Build (lot, bld) → section from Completed Homes rows
    sec_map = {}
    for r in rows:
        if r.get("metric", "").strip().lower() == "completed homes" and r.get("sec"):
            sec_map[(str(r["lot"]), r["bld"])] = r["sec"]

    def count_in_window(records, lot, bld, section, start_dt, end_dt):
        lot_key = str(lot)
        sec_norm = _norm_section(section)
        c = 0
        for r in records:
            if str(r.get("lot_type")) != lot_key:
                continue
            if (r.get("builder") or "").strip() != (bld or "").strip():
                continue
            if sec_norm and _norm_section(r.get("section")) != sec_norm:
                continue
            d = r.get("date")
            if d is None:
                continue
            if start_dt is not None and d <= start_dt:
                continue
            if end_dt is not None and d > end_dt:
                continue
            c += 1
        return c

    out = []
    for r in rows:
        metric_key = r.get("metric", "").strip().lower()
        if metric_key not in ("homes sold", "cancellations", "net sales"):
            # Make sure vals has n_cols entries (pad with 0, truncate if over)
            vals = list(r.get("vals", []))
            while len(vals) < n_cols:
                vals.append(0)
            vals = vals[:n_cols]
            out.append({**r, "vals": vals})
            continue

        section = r.get("sec") or sec_map.get((str(r["lot"]), r["bld"]), "")

        sold = [""]  # col 0 always blank
        canc = [""]
        for i in range(1, n_cols):
            start_dt = drive_dts[i - 1]
            end_dt = drive_dts[i]
            if start_dt is None or end_dt is None:
                sold.append(0)
                canc.append(0)
                continue
            sold.append(count_in_window(gross, r["lot"], r["bld"], section, start_dt, end_dt))
            canc.append(count_in_window(cancels, r["lot"], r["bld"], section, start_dt, end_dt))

        if metric_key == "homes sold":
            new_vals = sold
        elif metric_key == "cancellations":
            new_vals = canc
        else:  # net sales
            new_vals = []
            for s, c in zip(sold, canc):
                if s == "" and c == "":
                    new_vals.append("")
                else:
                    new_vals.append((s or 0) - (c or 0))

        out.append({**r, "vals": new_vals})

    return out


# ─── Top-level data builder (cached) ──────────────────────────────────────────

_REPO_ROOT = os.path.dirname(os.path.abspath(__file__))
_STARTS_CONFIG_PATH = os.path.join(_REPO_ROOT, "starts_config.json")
_STARTS_GPD_PATH    = os.path.join(_REPO_ROOT, "starts_gpd.json")
_STARTS_WRG_PATH    = os.path.join(_REPO_ROOT, "starts_wrg.json")

_DEFAULT_STARTS_CONFIG = {
    "gpd": {
        "months": ["Jan 26", "Feb 26 (1)", "Feb 26 (2)", "Mar 26"],
        "dates":  ["01/21/26", "02/02/26", "02/19/26", "03/06/26"],
    },
    "wrg": {
        "months": ["Jan 26", "Feb 26", "Mar 26"],
        "dates":  ["01/20/26", "02/13/26", "03/03/26"],
    },
}


def _load_json_file(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def _serialize_ym_dict(d):
    """Convert {(y,m): v} → {"y-m": v} for JSON."""
    return {f"{y}-{m:02d}": v for (y, m), v in d.items()}


def _serialize_bm_net(bm):
    """Convert {(lot, builder): {(y,m): v}} → {"lot|builder": {"y-m": v}}."""
    return {f"{k[0]}|{k[1]}": _serialize_ym_dict(v) for k, v in bm.items()}


def _serialize_months(months):
    return [f"{y}-{m:02d}" for (y, m) in months]


def _build_dashboard_data():
    """Fetch everything from Pipsy + starts files and assemble the payload."""
    now = datetime.now()
    current_year = now.year
    current_month = now.month

    # Pull all six datasets from Pipsy
    gpd_gross = extract_gross_sales(fetch_sales_records(PROP_GPD, cancels=False))
    gpd_cancel = extract_cancellations(fetch_sales_records(PROP_GPD, cancels=True))
    hld_gross = extract_gross_sales(fetch_sales_records(PROP_HLD, cancels=False))
    hld_cancel = extract_cancellations(fetch_sales_records(PROP_HLD, cancels=True))
    wrg_gross = extract_gross_sales(fetch_sales_records(PROP_WRG, cancels=False))
    wrg_cancel = extract_cancellations(fetch_sales_records(PROP_WRG, cancels=True))

    # Month ranges for each community
    end_ym = (current_year, current_month)
    gpd_months = generate_month_range(TGP_START, end_ym)
    hld_months = generate_month_range(HLD_START, end_ym)
    wrg_months = generate_month_range(WRG_START, end_ym)

    # Monthly net sales
    gpd_net_monthly = compute_monthly_net(gpd_gross, gpd_cancel, gpd_months)
    hld_net_monthly = compute_monthly_net(hld_gross, hld_cancel, hld_months)
    wrg_net_monthly = compute_monthly_net(wrg_gross, wrg_cancel, wrg_months)

    # Builder × month net
    gpd_bm_net = compute_builder_month_net(gpd_gross, gpd_cancel, gpd_months)
    hld_bm_net = compute_builder_month_net(hld_gross, hld_cancel, hld_months)
    wrg_bm_net = compute_builder_month_net(wrg_gross, wrg_cancel, wrg_months)

    # All-time totals
    gpd_at = compute_all_time_totals(gpd_gross, gpd_cancel)
    hld_at = compute_all_time_totals(hld_gross, hld_cancel)
    wrg_at = compute_all_time_totals(wrg_gross, wrg_cancel)

    gpd_total_net = sum(v["tn"] for v in gpd_at.values())
    hld_total_net = sum(v["tn"] for v in hld_at.values())
    wrg_total_net = sum(v["tn"] for v in wrg_at.values())

    # YTD net
    ytd_months = generate_month_range((current_year, 1), end_ym)
    gpd_ytd_net = sum(gpd_net_monthly.get(m, 0) for m in ytd_months)
    hld_ytd_net = sum(hld_net_monthly.get(m, 0) for m in ytd_months)
    wrg_ytd_net = sum(wrg_net_monthly.get(m, 0) for m in ytd_months)

    # YTD pace
    months_elapsed = len(ytd_months)
    gpd_ytd_pace = round(gpd_ytd_net / months_elapsed, 2) if months_elapsed else 0.0
    hld_ytd_pace = round(hld_ytd_net / months_elapsed, 2) if months_elapsed else 0.0
    wrg_ytd_pace = round(wrg_ytd_net / months_elapsed, 2) if months_elapsed else 0.0

    # Prior-year average paces (useful for comparison)
    prev_year = current_year - 1
    prev_months = [(prev_year, m) for m in range(1, 13)]
    prev2_months = [(prev_year - 1, m) for m in range(1, 13)]
    gpd_pace_prev  = compute_avg_pace(gpd_net_monthly, prev_months)
    hld_pace_prev  = compute_avg_pace(hld_net_monthly, prev_months)
    wrg_pace_prev  = compute_avg_pace(wrg_net_monthly, prev_months)
    gpd_pace_prev2 = compute_avg_pace(gpd_net_monthly, prev2_months)
    hld_pace_prev2 = compute_avg_pace(hld_net_monthly, prev2_months)
    wrg_pace_prev2 = compute_avg_pace(wrg_net_monthly, prev2_months)

    # Current-year cancel counts
    gpd_canc_ytd = sum(1 for c in gpd_cancel if c["date"].year == current_year)
    hld_canc_ytd = sum(1 for c in hld_cancel if c["date"].year == current_year)
    wrg_canc_ytd = sum(1 for c in wrg_cancel if c["date"].year == current_year)

    # Weekly data (current year)
    gpd_weekly = compute_weekly_data(gpd_gross, gpd_cancel, current_year)
    hld_weekly = compute_weekly_data(hld_gross, hld_cancel, current_year)
    wrg_weekly = compute_weekly_data(wrg_gross, wrg_cancel, current_year)

    # Recent cancellations (GPD shows 5)
    gpd_recent = sorted(gpd_cancel, key=lambda x: x["date"], reverse=True)[:5]

    # Starts tracking — read row data + drive-date config from JSON
    cfg = _load_json_file(_STARTS_CONFIG_PATH, _DEFAULT_STARTS_CONFIG)
    gpd_starts_rows_raw = _load_json_file(_STARTS_GPD_PATH, [])
    wrg_starts_rows_raw = _load_json_file(_STARTS_WRG_PATH, [])

    gpd_cfg = cfg.get("gpd", {}) if isinstance(cfg, dict) else {}
    wrg_cfg = cfg.get("wrg", {}) if isinstance(cfg, dict) else {}
    gpd_starts_months = gpd_cfg.get("months") or _DEFAULT_STARTS_CONFIG["gpd"]["months"]
    gpd_starts_dates  = gpd_cfg.get("dates")  or _DEFAULT_STARTS_CONFIG["gpd"]["dates"]
    wrg_starts_months = wrg_cfg.get("months") or _DEFAULT_STARTS_CONFIG["wrg"]["months"]
    wrg_starts_dates  = wrg_cfg.get("dates")  or _DEFAULT_STARTS_CONFIG["wrg"]["dates"]

    gpd_starts = compute_starts_rows(
        gpd_starts_rows_raw, gpd_starts_dates, gpd_gross, gpd_cancel,
    )
    wrg_starts = compute_starts_rows(
        wrg_starts_rows_raw, wrg_starts_dates, wrg_gross, wrg_cancel,
    )

    # Assemble static config for the frontend
    def _static_list(static_dict, mapping_order):
        """Return a list of {lot, builder_short, builder_full, static_fields...}."""
        out = []
        for row in mapping_order:
            lot, short, full = row
            st = static_dict.get((lot, full), {})
            out.append({
                "lot": lot, "short": short, "full": full,
                "psf": st.get("psf", ""),
                "price": st.get("price", ""),
                "target_pace": GPD_TARGET_PACE.get((lot, full)) if static_dict is GPD_BUILDER_STATIC else None,
            })
        return out

    # Earliest sale date for each community (for the "community age" UI bit)
    def _earliest(records):
        if not records:
            return None
        d = min(r["date"] for r in records)
        return d.strftime("%b %Y")

    # Cancellation breakdowns for the Cancellations tab. Three rollups:
    #   - top_builder_product: rows = builder×lot, cols = per-community
    #     counts + total. Sorted total desc, top 10.
    #   - top_builders:        rows = builder,    cols = per-community counts + total.
    #   - top_lot_types:       rows = lot,        cols = per-community counts + total.
    # Coworker had these as hardcoded tables (their dashboard regenerates
    # the HTML nightly); we compute live from cancellation records.
    def _build_cancel_breakdowns(by_community: dict) -> dict:
        bp: dict = {}   # (builder, lot) -> {community: count}
        bb: dict = {}   # builder        -> {community: count}
        bl: dict = {}   # lot            -> {community: count}
        for comm, cancels in by_community.items():
            for c in cancels:
                b   = c.get("builder")  or "Unknown"
                lot = str(c.get("lot_type") or "—")
                bp.setdefault((b, lot), {}).setdefault(comm, 0)
                bp[(b, lot)][comm] += 1
                bb.setdefault(b, {}).setdefault(comm, 0)
                bb[b][comm] += 1
                bl.setdefault(lot, {}).setdefault(comm, 0)
                bl[lot][comm] += 1
        def _row_with_total(key_pairs, key_label_fn):
            out = []
            for k, by_c in key_pairs:
                gpd_n = by_c.get("gpd", 0)
                hld_n = by_c.get("hld", 0)
                wrg_n = by_c.get("wrg", 0)
                tot   = gpd_n + hld_n + wrg_n
                row = key_label_fn(k)
                row.update({"gpd": gpd_n, "hld": hld_n, "wrg": wrg_n, "total": tot})
                out.append(row)
            return sorted(out, key=lambda r: -r["total"])
        return {
            "top_builder_product": _row_with_total(
                bp.items(),
                lambda k: {"builder": k[0], "lot": k[1]},
            )[:10],
            "top_builders": _row_with_total(
                bb.items(),
                lambda k: {"builder": k},
            )[:15],
            "top_lot_types": _row_with_total(
                bl.items(),
                lambda k: {"lot": k},
            ),
        }
    cancel_breakdowns = _build_cancel_breakdowns({
        "gpd": gpd_cancel, "hld": hld_cancel, "wrg": wrg_cancel,
    })

    # Emit the timestamp as an ISO 8601 UTC string. The browser formats it in
    # the user's local timezone — Railway runs in UTC, so a naive strftime here
    # bakes in the wrong wall-clock for anyone outside UTC (e.g. Houston is
    # UTC-5/6). Front-end uses fmtTimestamp() to localize on render.
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "current_year": current_year,
        "current_month": current_month,
        "targets": TARGETS,
        "cancel_breakdowns": cancel_breakdowns,
        "communities": {
            "gpd": {
                "name": "The Grand Prairie",
                "months": _serialize_months(gpd_months),
                "net_monthly": _serialize_ym_dict(gpd_net_monthly),
                "bm_net": _serialize_bm_net(gpd_bm_net),
                "at": gpd_at,
                "total_net": gpd_total_net,
                "ytd_net": gpd_ytd_net,
                "ytd_pace": gpd_ytd_pace,
                "pace_prev": gpd_pace_prev,
                "pace_prev2": gpd_pace_prev2,
                "canc_ytd": gpd_canc_ytd,
                "canc_total": len(gpd_cancel),
                "gross_total": len(gpd_gross),
                "weekly": gpd_weekly,
                "avg_price": GPD_AVG_PRICE,
                "earliest": _earliest(gpd_gross),
                "bm_order": _static_list(GPD_BUILDER_STATIC, GPD_BM_ORDER),
                "recent_cancels": [
                    {
                        "date": c["date"].strftime("%m/%d/%Y"),
                        "sale_date": c["sale_date_str"],
                        "address": c["address"],
                        "builder": c["builder"],
                        "lot_type": c["lot_type"],
                        "reason": c["reason"],
                    }
                    for c in gpd_recent
                ],
                "starts": {
                    "months": gpd_starts_months,
                    "dates":  gpd_starts_dates,
                    "rows":   gpd_starts,
                },
            },
            "hld": {
                "name": "The Grand Prairie — Highlands",
                "months": _serialize_months(hld_months),
                "net_monthly": _serialize_ym_dict(hld_net_monthly),
                "bm_net": _serialize_bm_net(hld_bm_net),
                "at": hld_at,
                "total_net": hld_total_net,
                "ytd_net": hld_ytd_net,
                "ytd_pace": hld_ytd_pace,
                "pace_prev": hld_pace_prev,
                "pace_prev2": hld_pace_prev2,
                "canc_ytd": hld_canc_ytd,
                "canc_total": len(hld_cancel),
                "gross_total": len(hld_gross),
                "weekly": hld_weekly,
                "avg_price": HLD_AVG_PRICE,
                "earliest": _earliest(hld_gross),
                "bm_order": _static_list(HLD_BUILDER_STATIC, HLD_BM_ORDER),
            },
            "wrg": {
                "name": "Windrose Green",
                "months": _serialize_months(wrg_months),
                "net_monthly": _serialize_ym_dict(wrg_net_monthly),
                "bm_net": _serialize_bm_net(wrg_bm_net),
                "at": wrg_at,
                "total_net": wrg_total_net,
                "ytd_net": wrg_ytd_net,
                "ytd_pace": wrg_ytd_pace,
                "pace_prev": wrg_pace_prev,
                "pace_prev2": wrg_pace_prev2,
                "canc_ytd": wrg_canc_ytd,
                "canc_total": len(wrg_cancel),
                "gross_total": len(wrg_gross),
                "weekly": wrg_weekly,
                "avg_price": WRG_AVG_PRICE,
                "earliest": _earliest(wrg_gross),
                "bm_order": _static_list(WRG_BUILDER_STATIC, WRG_BM_ORDER),
                "starts": {
                    "months": wrg_starts_months,
                    "dates":  wrg_starts_dates,
                    "rows":   wrg_starts,
                },
            },
        },
    }

    return payload


# ─── In-memory cache (1-hour TTL) ─────────────────────────────────────────────

_CACHE_LOCK = threading.Lock()
_CACHE = {"data": None, "ts": 0.0, "ttl": 3600}


def get_sales_dashboard_data(force_refresh: bool = False) -> dict:
    """Return the dashboard payload. Cached in memory for 1 hour.

    The upstream Pipsy cron refreshes once a day, so a 1-hour TTL means
    users see near-fresh data without hammering the API.

    Call with force_refresh=True to bypass the cache (e.g. admin button).
    """
    with _CACHE_LOCK:
        now = time.time()
        if (not force_refresh) and _CACHE["data"] and (now - _CACHE["ts"] < _CACHE["ttl"]):
            return _CACHE["data"]
        data = _build_dashboard_data()
        _CACHE["data"] = data
        _CACHE["ts"] = now
        return data
