"""
Project Returns — PDF report generation.

Ports the math + chart helpers from the design handoff
(`design_handoff_returns_pdf/_design_reference/Returns Report.html`)
to Python, normalizes Ember's live `reports.data` shape into a
canonical structure, and exposes `build_context(data, run_date, tone)`
returning a kwargs dict ready for
`render_template("returns_report.html", **ctx)`.

Math is intentionally identical to the JS prototype so the PDF
matches the HTML reference page-for-page.
"""

from __future__ import annotations

import math
import re
from datetime import datetime
from typing import Any


# ============================================================
# NORMALIZE — adapt Ember's live `reports.data` shape to canonical
# ============================================================
#
# Live data (from report_parser.py):
#   {
#     "years": [2023, 2024, ..., 2036],
#     "projects": [
#       { "name": "...",
#         "metrics": [
#           {"label": "Preferred Return",       "total": ..., "yearly": [...]},
#           {"label": "Return of Capital",      "total": ..., "yearly": [...]},
#           {"label": "Excess Cash Flow",       "total": ..., "yearly": [...]},
#           {"label": "Total LP Distributions", "total": ..., "yearly": [...]},
#           {"label": "Total LP Contributions", "total": ..., "yearly": [...]},
#           {"label": "Total LP Profit",        "total": ..., "yearly": [...]},
#           {"label": "Promote",                "total": ..., "yearly": [...]},
#           {"label": "LP IRR",                 "total": 0.198},   # decimal!
#           {"label": "LP Equity Multiple",     "total": 1.96},
#         ]
#       }
#     ]
#   }
#
# The handoff prototype expects:
#   { "years": ["2023", ...],
#     "projects": [
#       { "name", "location", "role", "active", "vintage",
#         "irr" (percent), "em", "profit", "promote", "lp_equity",
#         "hero_image_url",
#         "rows": {
#           "preferred_return", "return_of_capital", "excess_cashflow",
#           "lp_distributions", "lp_contributions", "lp_profit",
#           "net_cashflow", "cumulative_net_cf", "promote",
#         }
#       }
#     ]
#   }


# Label aliases — handles minor spelling differences (e.g. "Excess Cash Flow"
# vs "Excess Cashflow") so live and prototype data both work.
_LABEL_ALIASES = {
    "preferred_return":   ["Preferred Return"],
    "return_of_capital":  ["Return of Capital"],
    "excess_cashflow":    ["Excess Cash Flow", "Excess Cashflow"],
    "lp_distributions":   ["Total LP Distributions", "LP Distributions"],
    "lp_contributions":   ["Total LP Contributions", "LP Contributions"],
    "lp_profit":          ["Total LP Profit", "LP Profit"],
    "promote":            ["Promote"],
    "irr":                ["LP IRR", "IRR"],
    "em":                 ["LP Equity Multiple", "Equity Multiple", "EM"],
    "net_cashflow":       ["Net Cashflow", "LP Net Cashflow"],
    "cumulative_net_cf":  ["Cumulative Net Cashflow", "Cumulative Net CF"],
}


def normalize(raw: dict[str, Any], project_meta: dict[str, dict] | None = None) -> dict[str, Any]:
    """Canonical shape (see module docstring).

    Args:
        raw: live `reports.data` JSON.
        project_meta: optional mapping of project name -> {location, role,
            hero_image_url}, sourced from a separate "project_metadata"
            report row. Layered onto each project so the PDF can show real
            photos + locations without changing the returns parser.
    """
    project_meta = project_meta or {}

    years_raw = raw.get("years") or _fallback_years()
    years = [str(y) for y in years_raw]
    n_years = len(years)
    start_year = int(years[0])

    projects: list[dict[str, Any]] = []
    for raw_p in raw.get("projects") or []:
        if raw_p.get("active") is False:
            continue

        # Three input shapes, in order of preference:
        #  1) prototype shape with `rows` already filled (offline samples)
        #  2) live shape with a `metrics` list (Ember's parser output)
        #  3) prototype shape with scalars only (sample_data.json) — derive
        if isinstance(raw_p.get("rows"), dict) and raw_p["rows"]:
            p = _normalize_prototype_project(raw_p, years)
        elif raw_p.get("metrics"):
            p = _normalize_live_project(raw_p, years, n_years, start_year)
        else:
            p = _normalize_prototype_project(raw_p, years)
            p["rows"] = _derive_rows(p, years)
        if p is None:
            continue

        # Layer in project metadata (image / location / role) if available.
        meta = project_meta.get(p["name"]) or project_meta.get(p["name"].strip()) or {}
        if meta.get("location"): p["location"] = meta["location"]
        if meta.get("role"):     p["role"] = meta["role"]
        if meta.get("hero_image_url"): p["hero_image_url"] = meta["hero_image_url"]

        projects.append(p)

    uploaded_at = raw.get("uploaded_at")
    if isinstance(uploaded_at, str):
        try:
            uploaded_at = datetime.fromisoformat(uploaded_at.replace("Z", "+00:00"))
        except ValueError:
            uploaded_at = datetime.now()
    elif uploaded_at is None:
        uploaded_at = datetime.now()

    return {"uploaded_at": uploaded_at, "years": years, "projects": projects}


def _normalize_prototype_project(raw_p: dict[str, Any], years: list[str]) -> dict[str, Any]:
    """Pass-through path for data already shaped per the handoff prototype."""
    n = len(years)
    rows = {k: list(v)[:n] + [0.0] * max(0, n - len(v))
            for k, v in (raw_p.get("rows") or {}).items()}
    return {
        "name":     raw_p.get("name") or "Untitled",
        "location": raw_p.get("location") or "—",
        "role":     raw_p.get("role") or "Common Equity",
        "vintage":  int(raw_p.get("vintage") or years[0]),
        "irr":      float(raw_p.get("irr") or 0),
        "em":       float(raw_p.get("equity_multiple") or raw_p.get("em") or 1.0),
        "profit":   float(raw_p.get("total_profit") or raw_p.get("profit") or 0),
        "promote":  float(raw_p.get("promote") or 0),
        "lp_equity": float(raw_p.get("lp_equity") or raw_p.get("lp_contributions_total") or 0),
        "hero_image_url": raw_p.get("hero_image_url"),
        "rows": rows,
    }


def _normalize_live_project(raw_p: dict[str, Any], years: list[str],
                            n_years: int, start_year: int) -> dict[str, Any] | None:
    """Adapt the live `metrics`-list shape (Ember's parser output)."""
    by_label = {(m.get("label") or "").strip(): m for m in (raw_p.get("metrics") or [])}

    def _lookup(canonical_key: str):
        for alias in _LABEL_ALIASES.get(canonical_key, []):
            if alias in by_label:
                return by_label[alias]
        return None

    def _total(canonical_key: str, default: float = 0.0) -> float:
        m = _lookup(canonical_key) or {}
        v = m.get("total")
        try:
            return float(v) if v is not None else float(default)
        except (TypeError, ValueError):
            return float(default)

    def _yearly(canonical_key: str) -> list[float]:
        m = _lookup(canonical_key) or {}
        y = list(m.get("yearly") or [])
        out = [0.0] * n_years
        for i, v in enumerate(y[:n_years]):
            try:
                out[i] = float(v) if v is not None else 0.0
            except (TypeError, ValueError):
                out[i] = 0.0
        return out

    contrib = _yearly("lp_contributions")
    distrib = _yearly("lp_distributions")
    pref    = _yearly("preferred_return")
    roc     = _yearly("return_of_capital")
    excess  = _yearly("excess_cashflow")
    profit  = _yearly("lp_profit")
    promote_y = _yearly("promote")

    # Net cashflow: prefer the report's row, else derive (= distributions + contributions).
    net = _yearly("net_cashflow")
    if not any(abs(v) > 1e-9 for v in net):
        net = [distrib[i] + contrib[i] for i in range(n_years)]

    # Cumulative: prefer the row, else running sum.
    cum = _yearly("cumulative_net_cf")
    if not any(abs(v) > 1e-9 for v in cum):
        running = 0.0
        cum = []
        for v in net:
            running += v
            cum.append(running)

    # Vintage = first year with non-zero LP contribution.
    vintage = None
    for i, v in enumerate(contrib):
        if abs(v) > 1e-9:
            vintage = start_year + i
            break
    if vintage is None:
        for i in range(n_years):
            if any(abs(row[i]) > 1e-9 for row in (pref, roc, excess, distrib, profit, net)):
                vintage = start_year + i
                break
    if vintage is None:
        # Project has no activity — drop it; it shouldn't appear on summary.
        return None

    irr_dec = _total("irr")        # stored as decimal in live data (0.198)
    irr_pct = irr_dec * 100 if abs(irr_dec) <= 1.5 else irr_dec   # tolerate either

    return {
        "name":     (raw_p.get("name") or "Untitled").strip(),
        "location": raw_p.get("location") or "—",
        "role":     raw_p.get("role") or "Common Equity",
        "vintage":  int(vintage),
        "irr":      float(irr_pct),
        "em":       float(_total("em", default=1.0)),
        "profit":   float(_total("lp_profit")),
        "promote":  float(_total("promote")),
        "lp_equity": abs(float(_total("lp_contributions"))),
        "hero_image_url": raw_p.get("hero_image_url"),
        "rows": {
            "preferred_return":  pref,
            "return_of_capital": roc,
            "excess_cashflow":   excess,
            "lp_distributions":  distrib,
            "lp_contributions":  contrib,
            "lp_profit":         profit,
            "net_cashflow":      net,
            "cumulative_net_cf": cum,
            "promote":           promote_y,
        },
    }


def _fallback_years() -> list[str]:
    return [str(y) for y in range(2023, 2037)]


def _derive_rows(p: dict[str, Any], years: list[str]) -> dict[str, list[float]]:
    """Mirror of `rowsFor(p)` in the handoff JS prototype. Builds a
    plausible per-period waterfall from project-level scalars (vintage,
    IRR, profit, equity, promote) when the live data doesn't provide
    real cashflow rows. Used for offline samples + back-compat.
    """
    n = len(years)
    start_idx = max(0, p["vintage"] - int(years[0]))
    dist_years = [n - 3, n - 2, n - 1]

    contrib = [0.0] * n
    contrib[start_idx] = -p["lp_equity"]

    dist = [0.0] * n
    total = p["profit"] + p["lp_equity"]
    dist[dist_years[0]] = round(total * 0.25)
    dist[dist_years[1]] = round(total * 0.30)
    dist[dist_years[2]] = total - dist[dist_years[0]] - dist[dist_years[1]]
    if p["irr"] >= 15 and start_idx + 4 < n:
        early = round(p["profit"] * 0.10)
        dist[start_idx + 4] = early
        dist[dist_years[2]] -= early

    pref = [0.0] * n
    pref[dist_years[2]] = round(p["lp_equity"] * 0.08 * (n - start_idx) * 0.4)

    roc = [0.0] * n
    roc[dist_years[2]] = p["lp_equity"]

    excess = [dist[i] - pref[i] - roc[i] for i in range(n)]
    lp_profit = [dist[i] + contrib[i] for i in range(n)]
    net_cf = list(lp_profit)

    cum, running = [], 0.0
    for v in net_cf:
        running += v
        cum.append(running)

    promote_flow = [0.0] * n
    if p["promote"]:
        total_excess = sum(max(0, v) for v in excess) or 1
        promote_flow = [round(p["promote"] * max(0, v) / total_excess) for v in excess]

    return {
        "preferred_return":  pref,
        "return_of_capital": roc,
        "excess_cashflow":   excess,
        "lp_distributions":  dist,
        "lp_contributions":  contrib,
        "lp_profit":         lp_profit,
        "net_cashflow":      net_cf,
        "cumulative_net_cf": cum,
        "promote":           promote_flow,
    }


# ============================================================
# AGGREGATES
# ============================================================

def _portfolio_cum(projects, n_years):
    totals = [0.0] * n_years
    for p in projects:
        for i, v in enumerate(p["rows"]["net_cashflow"]):
            totals[i] += v
    out, running = [], 0.0
    for v in totals:
        running += v
        out.append(running)
    return out


def _portfolio_promote_cum(projects, n_years):
    totals = [0.0] * n_years
    for p in projects:
        for i, v in enumerate(p["rows"]["promote"]):
            totals[i] += v
    out, running = [], 0.0
    for v in totals:
        running += v
        out.append(running)
    return out


def _kpis(projects):
    total_profit  = sum(p["profit"]    for p in projects)
    total_equity  = sum(p["lp_equity"] for p in projects) or 1
    weighted_irr  = sum(p["irr"] * p["lp_equity"] for p in projects) / total_equity
    weighted_em   = sum(p["em"]  * p["lp_equity"] for p in projects) / total_equity
    total_dist    = sum(sum(p["rows"]["lp_distributions"]) for p in projects)
    total_promote = sum(p["promote"]   for p in projects)
    return {
        "total_profit": total_profit,
        "total_equity": total_equity,
        "weighted_irr": weighted_irr,
        "weighted_em":  weighted_em,
        "total_distributions": total_dist,
        "total_promote": total_promote,
    }


# ============================================================
# CHART HELPERS — return SVG strings (port of prototype JS)
# ============================================================

def nice_ticks(rmin: float, rmax: float, count: int = 5) -> list[float]:
    rng = rmax - rmin
    if rng == 0:
        return [rmin]
    rough = rng / (count - 1)
    mag = 10 ** math.floor(math.log10(rough))
    norm = rough / mag
    if   norm < 1.5: step = 1 * mag
    elif norm < 3:   step = 2 * mag
    elif norm < 7:   step = 5 * mag
    else:            step = 10 * mag
    start = math.floor(rmin / step) * step
    end   = math.ceil(rmax / step) * step
    ticks, v = [], start
    while v <= end + 1e-9:
        ticks.append(round(v))
        v += step
    return ticks


def fmt_tick(v: float) -> str:
    if v == 0:
        return "0"
    abs_v = abs(v)
    if abs_v >= 1000:
        return f"{(v / 1000):.{0 if abs_v >= 10000 else 1}f}M"
    return f"{int(v)}k"


def cumulative_chart(values, w, h, color, promote_values=None, *,
                     pad_left=36, pad_right=4, pad_top=6, pad_bottom=4,
                     label_size=8.5, label_gap=4, tick_count=5,
                     uid: str = "g") -> str:
    """Port of `cumulativeChart()` from the JS prototype."""
    inner_w = w - pad_left - pad_right
    inner_h = h - pad_top - pad_bottom

    all_vals = list(values) + list(promote_values or []) + [0]
    ticks = nice_ticks(min(all_vals), max(all_vals), tick_count)
    tmin, tmax = ticks[0], ticks[-1]
    rng = (tmax - tmin) or 1

    x_step = inner_w / (len(values) - 1) if len(values) > 1 else 0
    def x_for(i): return pad_left + i * x_step
    def y_for(v): return pad_top + (1 - (v - tmin) / rng) * inner_h
    zero_y = y_for(0)

    path = " ".join(
        f"{'M' if i == 0 else 'L'} {x_for(i):.1f} {y_for(v):.1f}"
        for i, v in enumerate(values)
    )
    area = (
        f"{path} "
        f"L {x_for(len(values) - 1):.1f} {zero_y:.1f} "
        f"L {pad_left} {zero_y:.1f} Z"
    )

    grid_lines = []
    for t in ticks:
        y = f"{y_for(t):.1f}"
        is_zero = t == 0
        grid_lines.append(
            f'<line x1="{pad_left}" x2="{w - pad_right}" y1="{y}" y2="{y}" '
            f'stroke="#08233B" stroke-width="{0.9 if is_zero else 0.5}" '
            f'stroke-dasharray="{"3 3" if is_zero else "2 3"}" '
            f'opacity="{0.35 if is_zero else 0.12}"/>'
        )

    y_labels = []
    for t in ticks:
        y = f"{y_for(t):.1f}"
        y_labels.append(
            f'<text x="{pad_left - label_gap}" y="{y}" text-anchor="end" '
            f'dominant-baseline="middle" font-family="JetBrains Mono, ui-monospace, monospace" '
            f'font-size="{label_size}" fill="#5A6E80" letter-spacing="0.04em">{fmt_tick(t)}</text>'
        )

    promote_line = ""
    if promote_values and any(v != 0 for v in promote_values):
        ppath = " ".join(
            f"{'M' if i == 0 else 'L'} {x_for(i):.1f} {y_for(v):.1f}"
            for i, v in enumerate(promote_values)
        )
        promote_line = (
            f'<path d="{ppath}" fill="none" stroke="#08233B" '
            f'stroke-width="1.2" stroke-dasharray="4 3" opacity="0.55"/>'
        )

    return (
        f'<svg viewBox="0 0 {w} {h}" preserveAspectRatio="xMidYMid meet" '
        f'style="overflow:visible;width:100%;height:100%;">'
          f'<defs>'
            f'<linearGradient id="{uid}" x1="0" y1="0" x2="0" y2="1">'
              f'<stop offset="0%" stop-color="{color}" stop-opacity="0.35"/>'
              f'<stop offset="100%" stop-color="{color}" stop-opacity="0"/>'
            f'</linearGradient>'
          f'</defs>'
          f'{"".join(grid_lines)}'
          f'<path d="{area}" fill="url(#{uid})"/>'
          f'<path d="{path}" fill="none" stroke="{color}" stroke-width="1.6"/>'
          f'{promote_line}'
          f'{"".join(y_labels)}'
        f'</svg>'
    )


# ============================================================
# FORMATTERS — used by the Jinja template
# ============================================================

def fmt_int(v) -> str:
    """1234 -> '1,234'   -1234 -> '(1,234)'   0 -> '—'"""
    if v == 0 or v is None:
        return "—"
    n = int(round(v))
    if n == 0:
        return "—"
    if n < 0:
        return f"({abs(n):,})"
    return f"{n:,}"


def fmt_money_M(v) -> str:
    """Returns '$1.5M' or '($1.5M)' from a $K input."""
    if v is None:
        return "—"
    f = float(v)
    if abs(f) < 0.5:
        return "—"
    if f < 0:
        return f"(${abs(f) / 1000:.1f}M)"
    return f"${f / 1000:.1f}M"


def fmt_pct(v) -> str:
    return f"{v:.1f}%"


def fmt_em(v) -> str:
    return f"{v:.2f}x"


# ============================================================
# CONTEXT BUILDER
# ============================================================

def build_context(data: dict[str, Any], run_date: datetime,
                  tone: str = "institutional") -> dict[str, Any]:
    projects = data["projects"]
    years = data["years"]
    n_years = len(years)

    # Sort by total profit, descending — used on summary's project list
    projects_ranked = sorted(projects, key=lambda p: p["profit"], reverse=True)

    # Group projects two-per-page for the project pages
    project_pages: list[list[dict[str, Any]]] = []
    for i in range(0, len(projects_ranked), 2):
        project_pages.append(projects_ranked[i:i + 2])

    kpis = _kpis(projects) if projects else {
        "total_profit": 0, "total_equity": 0, "weighted_irr": 0,
        "weighted_em": 0, "total_distributions": 0, "total_promote": 0,
    }
    portfolio_cum_vals = _portfolio_cum(projects, n_years)
    promote_cum_vals   = _portfolio_promote_cum(projects, n_years)

    accent_color  = "#F57346" if tone == "editorial" else "#F25929"
    promote_color = "#08233B"

    portfolio_net_chart = cumulative_chart(
        portfolio_cum_vals, 900, 230, accent_color, None,
        pad_left=64, label_size=12, tick_count=4, label_gap=8, uid="pgrad-net",
    ) if portfolio_cum_vals else ""
    portfolio_promote_chart = cumulative_chart(
        promote_cum_vals, 900, 230, promote_color, None,
        pad_left=64, label_size=12, tick_count=4, label_gap=8, uid="pgrad-prom",
    ) if promote_cum_vals else ""

    # Per-project: clip rows to the project's actual active range (first
    # contribution → last distribution). The portfolio runs 2018-2039 but
    # a 2025-vintage that wraps in 2032 has no business showing 2018-2024
    # or 2033-2039 columns of zeros — they crowd the table and force the
    # year font down to unreadable sizes. We compute the active span from
    # the row data, slice every per-year array to it, and re-derive the
    # cumulative net so the chart starts at zero on the project's vintage.
    _active_keys = ("preferred_return", "return_of_capital", "excess_cashflow",
                    "lp_distributions", "lp_contributions",
                    "net_cashflow", "promote")
    portfolio_start_year = int(years[0])

    for idx, p in enumerate(projects_ranked):
        rows = p["rows"]
        first_active = n_years
        last_active  = -1
        for i in range(n_years):
            if any(i < len(rows.get(k, [])) and abs(rows[k][i]) > 1e-9
                   for k in _active_keys):
                first_active = min(first_active, i)
                last_active  = max(last_active,  i)

        if last_active < 0:
            # No activity at all — fall back to full range.
            start_idx, end_idx = 0, n_years
        else:
            # Floor at vintage so we don't drop the contribution year if
            # it happens to be all-zeros for a particular row key set.
            v_idx = max(0, p["vintage"] - portfolio_start_year)
            start_idx = max(0, min(first_active, v_idx))
            end_idx   = min(last_active + 1, n_years)

        sliced = {k: list(v[start_idx:end_idx]) for k, v in rows.items()}
        # Re-derive cumulative_net_cf from the sliced net so the chart
        # starts at the project's first active year, not from the
        # portfolio's 2018 baseline.
        cum, running = [], 0.0
        for v in sliced.get("net_cashflow", []):
            running += v
            cum.append(running)
        sliced["cumulative_net_cf"] = cum

        p["rows"]              = sliced
        p["years"]              = years[start_idx:end_idx]
        p["year_labels_short"]  = [f"'{y[-2:]}" for y in p["years"]]
        p["pcf_start_year"]     = int(years[start_idx])
        p["pcf_end_year"]       = int(years[end_idx - 1])

        prom_cum, running = [], 0.0
        for v in sliced.get("promote", []):
            running += v
            prom_cum.append(running)
        p["cum_chart_svg"] = cumulative_chart(
            sliced["cumulative_net_cf"], 400, 130, accent_color, prom_cum,
            pad_left=50, label_size=11, tick_count=4, label_gap=6,
            uid=f"pcgrad-{idx}",
        )

    period_label = run_date.strftime("%B %Y")
    period_short = run_date.strftime("%b %Y").upper()

    vintages = [int(p["vintage"]) for p in projects] or [int(years[0])]
    horizon_year = int(years[-1])
    start_year   = int(years[0])

    # Page count: cover + summary + N project pages.
    # (The previous appendix / definitions page was dropped per partner
    # feedback; total_pages no longer includes it.)
    total_pages = 2 + max(1, len(project_pages)) if projects else 2

    return {
        # Header / chrome
        "tone": tone,
        "tone_class": "tone-editorial" if tone == "editorial" else "tone-institutional",
        "period_label": period_label,
        "period_short": period_short,
        "horizon_year": horizon_year,
        "start_year":   start_year,
        "min_vintage":  min(vintages),
        "max_vintage":  max(vintages),
        "n_projects":   len(projects),

        # Cover
        "cover_lede": (
            f"A monthly read of every active Ember project — modeled cashflows, "
            f"distributions, and partner returns through {horizon_year}. "
            f"{len(projects)} projects, ${kpis['total_equity'] / 1000:.1f}M of LP equity at work, "
            f"{min(vintages)}–{max(vintages)} vintages."
        ),

        # KPIs + charts
        "kpis": kpis,
        "portfolio_net_chart_svg":     portfolio_net_chart,
        "portfolio_promote_chart_svg": portfolio_promote_chart,

        # Project data
        "years": years,
        "year_labels_short":  [f"'{y[-2:]}" for y in years],
        "year_labels_xaxis":  [years[i] for i in range(0, len(years), 2)],
        "projects_ranked":    projects_ranked,
        "project_pages":      project_pages,
        "total_pages":        total_pages,

        # Formatter functions (referenced by Jinja template)
        "fmt_int":      fmt_int,
        "fmt_money_M":  fmt_money_M,
        "fmt_pct":      fmt_pct,
        "fmt_em":       fmt_em,

        "accent_color":  accent_color,
        "promote_color": promote_color,
    }
