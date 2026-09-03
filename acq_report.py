"""Executive acquisition report — data assembly and figure rendering.

The report is HTML/CSS rendered by WeasyPrint, not hand-positioned on a PDF
canvas. That decision was already made in this codebase: WeasyPrint is in
requirements.txt and nixpacks.toml already installs Pango, Cairo and the
Liberation fonts on Railway for the Ember Capital Report, which ships the same
way. Following it means the layout lives in a template anyone can edit rather
than in a thousand lines of absolute coordinates.

Nothing here recomputes the acquisition analysis. Every number comes from the
endpoints that already feed the project page; this module calls those view
functions inside the request context and reshapes what they return. If a figure
looks wrong, it is wrong on the page too.

Figures (site map, competitor map, trend charts) are matplotlib PNGs inlined as
data URIs. They are deliberately NOT screenshots of the Leaflet map: a screenshot
carries the zoom buttons, the attribution strip and whatever the user had panned
into view, none of which belong in a document going to an investment committee.
"""
import base64
import io
import math
import re

# Palette — matches the report template and the rest of the acquisitions tab.
NAVY = "#13344E"
NAVY_DEEP = "#0B2233"
ORANGE = "#F25929"
BLUE = "#3B5BA5"
GREEN = "#2E7D4F"
GREY = "#6B7B8B"
GREY_LINE = "#DDE3E8"
CREAM = "#F7F4EF"


# ---------------------------------------------------------------------------
# Figure helpers
# ---------------------------------------------------------------------------
def _fig_to_uri(fig, dpi=170, pad=0.0, transparent=False):
    """PNG data URI with no surrounding padding.

    bbox_inches="tight" plus a white facecolor is what put a white band around
    the map in the reference deck: the axes keep an equal aspect, so any figure
    whose shape does not match the geography gets letterboxed and the padding
    is painted white. Everything here is sized to fill its frame instead.
    """
    import matplotlib.pyplot as plt
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=dpi, pad_inches=pad,
                bbox_inches=None if pad == 0 else "tight",
                facecolor="none" if transparent else fig.get_facecolor(),
                edgecolor="none", transparent=transparent)
    plt.close(fig)
    buf.seek(0)
    return "data:image/png;base64," + base64.b64encode(buf.read()).decode("ascii")


def _geo_figsize(bounds, target_w=7.4, min_h=3.0, max_h=5.2):
    """Figure size whose shape matches the ground, so nothing is letterboxed.

    A degree of longitude is shorter than a degree of latitude everywhere but
    the equator, so the true aspect uses cos(lat) at the map's centre.
    """
    minx, miny, maxx, maxy = bounds
    mid_lat = (miny + maxy) / 2.0
    w_deg = max(maxx - minx, 1e-6)
    h_deg = max(maxy - miny, 1e-6)
    ground_w = w_deg * math.cos(math.radians(mid_lat))
    ground_h = h_deg
    h = target_w * (ground_h / ground_w) if ground_w else target_w * 0.7
    return (target_w, max(min_h, min(max_h, h)))


def _padded_bounds(bounds, frac=0.12):
    minx, miny, maxx, maxy = bounds
    px = (maxx - minx) * frac or 0.004
    py = (maxy - miny) * frac or 0.004
    return minx - px, miny - py, maxx + px, maxy + py


def _fit_bounds_to_figure(ax, bounds, figsize):
    """Widen the shorter axis so the data fills the frame exactly.

    With set_aspect("equal") matplotlib will letterbox whenever the data's
    aspect and the figure's aspect disagree. Rather than drop equal aspect --
    which would stretch the parcels out of shape -- the view is grown on
    whichever axis has slack, so the map reaches every edge and the geometry
    stays true.
    """
    minx, miny, maxx, maxy = bounds
    mid_lat = (miny + maxy) / 2.0
    k = math.cos(math.radians(mid_lat)) or 1.0
    fig_ar = figsize[0] / figsize[1]                 # width / height
    data_ar = ((maxx - minx) * k) / max(maxy - miny, 1e-9)
    if data_ar < fig_ar:                             # too tall -> widen
        want = (maxy - miny) * fig_ar / k
        cx = (minx + maxx) / 2.0
        minx, maxx = cx - want / 2.0, cx + want / 2.0
    else:                                            # too wide -> heighten
        want = ((maxx - minx) * k) / fig_ar
        cy = (miny + maxy) / 2.0
        miny, maxy = cy - want / 2.0, cy + want / 2.0
    ax.set_xlim(minx, maxx)
    ax.set_ylim(miny, maxy)
    return minx, miny, maxx, maxy


def _strip_axes(ax):
    """A map is a picture, not a plot: no ticks, no frame, no labels."""
    ax.set_xticks([])
    ax.set_yticks([])
    for s in ax.spines.values():
        s.set_visible(False)
    ax.set_xlabel("")
    ax.set_ylabel("")


def _scale_bar(ax, bounds, colour="#FFFFFF"):
    """Distance scale, chosen from the map's own width at its centre latitude."""
    minx, miny, maxx, maxy = bounds
    span_ft = (maxx - minx) * 364000.0 * math.cos(math.radians((miny + maxy) / 2))
    if span_ft <= 0:
        return
    nice = min([264, 528, 1320, 2640, 5280, 10560, 26400, 52800],
               key=lambda v: abs(v - span_ft * 0.24))
    frac = nice / span_ft
    bx = minx + (maxx - minx) * 0.045
    by = miny + (maxy - miny) * 0.055
    ax.plot([bx, bx + (maxx - minx) * frac], [by, by], color="#00000055",
            lw=4.4, solid_capstyle="butt", zorder=60)
    ax.plot([bx, bx + (maxx - minx) * frac], [by, by], color=colour,
            lw=2.0, solid_capstyle="butt", zorder=61)
    ax.text(bx + (maxx - minx) * frac / 2, by + (maxy - miny) * 0.022,
            (f"{nice/5280:g} mi" if nice >= 5280 else f"{nice:,} ft"),
            ha="center", va="bottom", fontsize=7.5, color="#FFFFFF",
            fontweight="bold", zorder=62,
            bbox=dict(boxstyle="round,pad=0.22", facecolor=NAVY_DEEP,
                      edgecolor="none", alpha=0.82))


def _north_arrow(ax):
    ax.text(0.972, 0.965, "N\n▲", transform=ax.transAxes, ha="center", va="top",
            fontsize=10.5, color="#FFFFFF", fontweight="bold", zorder=62,
            bbox=dict(boxstyle="round,pad=0.28", facecolor=NAVY_DEEP,
                      edgecolor="white", linewidth=0.7))


def _esri_basemap(ax, bounds, kind="imagery"):
    """Composite Esri XYZ tiles behind the map. Silent no-op if unreachable."""
    import numpy as np
    try:
        import requests
        from PIL import Image
    except Exception:
        return False
    minx, miny, maxx, maxy = bounds

    def to_tile(lon, lat, z):
        n = 2 ** z
        lat_r = math.radians(lat)
        xt = (lon + 180.0) / 360.0 * n
        yt = (1 - math.log(math.tan(lat_r) + 1 / math.cos(lat_r)) / math.pi) / 2 * n
        return xt, yt

    # Imagery alone has no place names, so a reader cannot tell where the site
    # is. Esri publishes the labels and roads as a separate transparent
    # reference layer; drawing it over the imagery gives the hybrid view.
    services = (["World_Imagery", "Reference/World_Boundaries_and_Places"]
                if kind == "imagery" else ["World_Topo_Map"])
    service = services[0]
    for z in range(16, 9, -1):
        x0, y1 = to_tile(minx, miny, z)
        x1, y0 = to_tile(maxx, maxy, z)
        tx0, tx1 = int(math.floor(x0)), int(math.floor(x1))
        ty0, ty1 = int(math.floor(y0)), int(math.floor(y1))
        if (tx1 - tx0 + 1) * (ty1 - ty0 + 1) <= 36:
            break
    else:
        return False
    try:
        cols, rows = tx1 - tx0 + 1, ty1 - ty0 + 1
        canvas = Image.new("RGB", (cols * 256, rows * 256), "#DfE6EC")

        # Fetched in parallel. Serially this was 36 round trips at roughly
        # 0.7s each -- 25 seconds for one map, and the report draws two.
        from concurrent.futures import ThreadPoolExecutor

        def grab(ij):
            i, j, tx, ty = ij
            url = (f"https://server.arcgisonline.com/ArcGIS/rest/services/"
                   f"{service}/MapServer/tile/{z}/{ty}/{tx}")
            try:
                r = requests.get(url, timeout=10)
                if r.status_code != 200:
                    return None
                return i, j, Image.open(io.BytesIO(r.content)).convert("RGB")
            except Exception:
                return None

        jobs = [(i, j, tx, ty)
                for i, tx in enumerate(range(tx0, tx1 + 1))
                for j, ty in enumerate(range(ty0, ty1 + 1))]
        got = 0
        with ThreadPoolExecutor(max_workers=12) as pool:
            for res in pool.map(grab, jobs):
                if res is None:
                    continue
                i, j, im = res
                canvas.paste(im, (i * 256, j * 256))
                got += 1
        if not got:
            return False

        def tile_to_lonlat(xt, yt, z):
            n = 2 ** z
            lon = xt / n * 360.0 - 180.0
            lat = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * yt / n))))
            return lon, lat

        w_lon, n_lat = tile_to_lonlat(tx0, ty0, z)
        e_lon, s_lat = tile_to_lonlat(tx1 + 1, ty1 + 1, z)
        ax.imshow(np.asarray(canvas), extent=[w_lon, e_lon, s_lat, n_lat],
                  origin="upper", zorder=0, interpolation="bilinear")
        return True
    except Exception:
        return False


def _draw_geom(ax, geom, **kw):
    """Draw a shapely geometry of any type without caring which type it is."""
    from shapely.geometry import (Polygon, MultiPolygon, LineString,
                                  MultiLineString, GeometryCollection)
    if geom is None or geom.is_empty:
        return
    if isinstance(geom, (MultiPolygon, MultiLineString, GeometryCollection)):
        for g in geom.geoms:
            _draw_geom(ax, g, **kw)
        return
    if isinstance(geom, Polygon):
        xs, ys = geom.exterior.xy
        ax.fill(xs, ys, facecolor=kw.get("face", "none"),
                edgecolor=kw.get("edge", "none"),
                linewidth=kw.get("lw", 0.8), alpha=kw.get("alpha", 1.0),
                zorder=kw.get("z", 5))
        for ring in geom.interiors:
            xs, ys = ring.xy
            ax.fill(xs, ys, facecolor=CREAM, edgecolor="none",
                    alpha=kw.get("alpha", 1.0), zorder=kw.get("z", 5) + 0.1)
    elif isinstance(geom, LineString):
        xs, ys = geom.xy
        ax.plot(xs, ys, color=kw.get("edge", BLUE), linewidth=kw.get("lw", 1.0),
                alpha=kw.get("alpha", 1.0), zorder=kw.get("z", 5))


def render_site_map(union_geom, constraint_geoms=None, tracts=None,
                    width_in=7.4):
    """The subject site with its physical constraints, edge to edge.

    No zoom controls, no attribution strip, no letterboxing -- the three things
    that gave away the reference deck's map as a screenshot of the web app.
    """
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from shapely.geometry import shape as shp_shape

    if union_geom is None:
        return None
    bounds = _padded_bounds(union_geom.bounds, 0.16)
    figsize = _geo_figsize(bounds, target_w=width_in, min_h=3.2, max_h=4.9)
    fig, ax = plt.subplots(figsize=figsize, dpi=170)
    fig.subplots_adjust(left=0, right=1, top=1, bottom=0)
    ax.set_position([0, 0, 1, 1])
    ax.set_facecolor("#E7ECF0")
    ax.set_aspect("equal")
    bounds = _fit_bounds_to_figure(ax, bounds, figsize)
    _esri_basemap(ax, bounds, "imagery")

    cg = constraint_geoms or {}
    layers = [
        ("floodplain", "#5B6FD6", 0.42, "Floodplain (100-yr)"),
        ("wetlands", "#2E9E6B", 0.48, "Wetlands (NWI)"),
        ("stream_buffers", "#2F7FD6", 0.55, "Streams"),
        ("pipeline_easements", "#C99A2E", 0.55, "Pipeline easement"),
        ("transmission_row", "#B0552E", 0.55, "Transmission ROW"),
    ]
    legend = []
    for key, colour, alpha, label in layers:
        gj = cg.get(key)
        if not gj:
            continue
        try:
            g = shp_shape(gj) if isinstance(gj, dict) else gj
        except Exception:
            continue
        if g is None or g.is_empty:
            continue
        _draw_geom(ax, g, face=colour, edge=colour, alpha=alpha, lw=0.6, z=3)
        legend.append((label, colour))

    # Individual tracts hairlined inside the assembly outline, so a multi-tract
    # deal reads as an assembly rather than one blob.
    for t in (tracts or []):
        try:
            g = shp_shape(t.get("geometry")) if t.get("geometry") else None
        except Exception:
            g = None
        if g is not None and not g.is_empty:
            _draw_geom(ax, g, face="none", edge="#FFD9A0", lw=0.7, alpha=0.85, z=6)

    _draw_geom(ax, union_geom, face="none", edge=ORANGE, lw=2.1, z=8)
    _strip_axes(ax)
    _north_arrow(ax)
    _scale_bar(ax, bounds)

    if legend:
        from matplotlib.patches import Patch
        ax.legend(handles=[Patch(facecolor=c, edgecolor="none", alpha=0.75,
                                 label=l) for l, c in legend],
                  loc="lower right", fontsize=6.8, frameon=True,
                  facecolor="#FFFFFFEE", edgecolor=GREY_LINE,
                  borderpad=0.5, handlelength=1.1).set_zorder(63)
    return _fig_to_uri(fig, dpi=170, pad=0.0)


def render_competitor_map(center, communities, radius_mi=None, width_in=7.4):
    """Subject site with the competing communities around it."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    pts = [(c.get("lon"), c.get("lat"), c) for c in (communities or [])
           if c.get("lat") and c.get("lon")]
    if not center or not center.get("lat"):
        return None
    clon, clat = float(center["lon"]), float(center["lat"])
    xs = [p[0] for p in pts] + [clon]
    ys = [p[1] for p in pts] + [clat]
    if len(xs) < 2:
        span = (radius_mi or 5) / 55.0
        bounds = (clon - span, clat - span, clon + span, clat + span)
    else:
        bounds = _padded_bounds((min(xs), min(ys), max(xs), max(ys)), 0.14)

    figsize = _geo_figsize(bounds, target_w=width_in, min_h=3.2, max_h=4.6)
    fig, ax = plt.subplots(figsize=figsize, dpi=170)
    fig.subplots_adjust(left=0, right=1, top=1, bottom=0)
    ax.set_position([0, 0, 1, 1])
    ax.set_facecolor("#EEF1F4")
    ax.set_aspect("equal")
    bounds = _fit_bounds_to_figure(ax, bounds, figsize)
    _esri_basemap(ax, bounds, "topo")

    if radius_mi:
        from matplotlib.patches import Circle
        r_deg = float(radius_mi) / 69.0
        ax.add_patch(Circle((clon, clat), r_deg, fill=False, linestyle=(0, (4, 3)),
                            edgecolor=ORANGE, linewidth=1.1, alpha=0.75, zorder=4))

    status_style = {
        "active": (GREEN, "o", "Active"),
        "future": ("#C99A2E", "^", "Future"),
        "closed": (GREY, "s", "Built out"),
    }
    seen = {}
    for lon, lat, c in pts:
        st = str(c.get("status") or "active").lower()
        key = ("future" if "future" in st else
               "closed" if ("close" in st or "built" in st or "sold" in st) else "active")
        colour, marker, label = status_style[key]
        lots = float(c.get("total_lots") or c.get("lots") or 0)
        size = 18 + min(math.sqrt(max(lots, 0)) * 3.2, 90)
        ax.scatter([lon], [lat], s=size, c=colour, marker=marker,
                   edgecolors="#FFFFFF", linewidths=0.6, alpha=0.9, zorder=6)
        seen[label] = (colour, marker)

    ax.scatter([clon], [clat], s=230, marker="*", c=ORANGE,
               edgecolors="#FFFFFF", linewidths=1.1, zorder=9)
    ax.annotate("SUBJECT", (clon, clat), textcoords="offset points",
                xytext=(0, -15), ha="center", fontsize=7.4, fontweight="bold",
                color="#FFFFFF", zorder=10,
                bbox=dict(boxstyle="round,pad=0.24", facecolor=ORANGE,
                          edgecolor="none"))

    _strip_axes(ax)
    _north_arrow(ax)
    _scale_bar(ax, bounds, colour="#FFFFFF")
    if seen:
        from matplotlib.lines import Line2D
        handles = [Line2D([], [], marker=m, color="none", markerfacecolor=c,
                          markeredgecolor="#FFFFFF", markersize=7, label=l)
                   for l, (c, m) in seen.items()]
        ax.legend(handles=handles, loc="lower right", fontsize=6.8, frameon=True,
                  facecolor="#FFFFFFEE", edgecolor=GREY_LINE,
                  borderpad=0.5).set_zorder(63)
    return _fig_to_uri(fig, dpi=170, pad=0.0)


# ---------------------------------------------------------------------------
# Formatting — one place, so "1,064.7 ac" and "$326,881" look the same
# everywhere and a missing value never prints as None, NaN or undefined.
# ---------------------------------------------------------------------------
def _n(v):
    try:
        f = float(v)
        return None if f != f else f          # NaN check
    except (TypeError, ValueError):
        return None


def ac(v, dash="—"):
    f = _n(v)
    return dash if f is None else f"{f:,.1f} ac"


def num(v, dash="—"):
    f = _n(v)
    return dash if f is None else f"{f:,.0f}"


def pct(v, dash="—", dp=1):
    f = _n(v)
    return dash if f is None else f"{f:,.{dp}f}%"


def money(v, dash="—"):
    f = _n(v)
    if f is None:
        return dash
    if abs(f) >= 1_000_000_000:
        return f"${f/1e9:,.2f}B"
    if abs(f) >= 1_000_000:
        return f"${f/1e6:,.0f}M"
    if abs(f) >= 10_000:
        return f"${f/1000:,.0f}k"
    return f"${f:,.0f}"


def miles(v, direction=None, dash="—"):
    f = _n(v)
    if f is None:
        return dash
    return f"{f:,.2f} mi" + (f" {direction}" if direction else "")


def _first(d, *keys, default=None):
    """First key that carries a usable value. The upstream payloads spell the
    same idea several ways depending on which service answered."""
    for k in keys:
        if isinstance(d, dict) and d.get(k) not in (None, "", [], {}):
            return d[k]
    return default


# ---------------------------------------------------------------------------
# Charts
# ---------------------------------------------------------------------------
def render_quarter_chart(quarters, width_in=6.9):
    """Starts vs closings by quarter — grouped bars, no chart junk."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import numpy as np
    if not quarters:
        return None
    labels = [q.get("label") or q.get("quarter") or "" for q in quarters]
    starts = [_n(q.get("starts")) or 0 for q in quarters]
    clos = [_n(q.get("closings")) or 0 for q in quarters]
    fig, ax = plt.subplots(figsize=(width_in, 1.45), dpi=170)
    fig.patch.set_facecolor("white")
    x = np.arange(len(labels))
    ax.bar(x - 0.19, starts, 0.36, color=NAVY, label="Starts")
    ax.bar(x + 0.19, clos, 0.36, color=ORANGE, label="Closings")
    ax.set_xticks(x)
    ax.set_xticklabels(labels, fontsize=7, color=GREY)
    ax.tick_params(axis="y", labelsize=7, colors=GREY)
    ax.grid(axis="y", linestyle=":", color=GREY_LINE, linewidth=0.6)
    ax.set_axisbelow(True)
    for s in ("top", "right", "left"):
        ax.spines[s].set_visible(False)
    ax.spines["bottom"].set_color(GREY_LINE)
    ax.legend(fontsize=7, frameon=False, ncol=2, loc="upper left")
    fig.tight_layout(pad=0.4)
    return _fig_to_uri(fig, dpi=170, pad=0.02)


def render_enrollment_chart(trend, width_in=6.9):
    """District enrollment history — the migration indicator."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    pts = [(p.get("year"), _n(p.get("enrollment"))) for p in (trend or [])]
    pts = [(y, e) for y, e in pts if y and e]
    if len(pts) < 3:
        return None
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    fig, ax = plt.subplots(figsize=(width_in, 1.5), dpi=170)
    fig.patch.set_facecolor("white")
    ax.fill_between(xs, ys, color=ORANGE, alpha=0.13)
    ax.plot(xs, ys, color=ORANGE, linewidth=1.7)
    ax.scatter([xs[-1]], [ys[-1]], s=22, color=ORANGE, zorder=5)
    ax.annotate(f"{ys[-1]:,.0f}", (xs[-1], ys[-1]), textcoords="offset points",
                xytext=(-4, 7), ha="right", fontsize=7.5, fontweight="bold",
                color=ORANGE)
    ax.tick_params(labelsize=7, colors=GREY)
    ax.grid(axis="y", linestyle=":", color=GREY_LINE, linewidth=0.6)
    ax.set_axisbelow(True)
    for s in ("top", "right", "left"):
        ax.spines[s].set_visible(False)
    ax.spines["bottom"].set_color(GREY_LINE)
    fig.tight_layout(pad=0.4)
    return _fig_to_uri(fig, dpi=170, pad=0.02)


# ---------------------------------------------------------------------------
# Narrative — deterministic rules, not free-form prose. Every sentence is
# anchored to a number that appears elsewhere in the report, so nothing here
# can assert something the rest of the document does not show.
# ---------------------------------------------------------------------------
def _thesis(ctx, a, cbas, schools, county):
    bits = []
    gross = _n(a.get("gross_acres")) or 0
    conv = _n(a.get("net_saleable_pct"))
    dist = (schools or {}).get("district_name") or ""
    scale = ("Large-scale" if gross >= 500 else
             "Mid-scale" if gross >= 150 else "Infill-scale")
    where = dist or (county or "the submarket")
    bits.append(f"{scale} residential opportunity in the {where} corridor")
    starts = _n((cbas or {}).get("ring_annual_starts"))
    if starts:
        bits[-1] += f" with {starts:,.0f} annual starts in the competitive ring"
    bits[-1] += "."
    if conv is not None:
        tone = ("Development efficiency is the central site issue"
                if conv < 55 else "Development efficiency is favourable")
        bits.append(f"{tone}: gross-to-saleable conversion is {conv:,.1f}%.")
    mos = _n((cbas or {}).get("months_lot_supply"))
    fut = _n(((cbas or {}).get("aggregate") or {}).get("futures"))
    if mos or fut:
        s = "Supply warrants caution"
        if mos:
            s += f": {mos:,.1f} months of lot supply"
        if fut:
            s += f"{' and' if mos else ':'} {fut:,.0f} future lots in the ring"
        bits.append(s + ".")
    return " ".join(bits)


def _factors(a, cbas):
    out = []
    conv = _n(a.get("net_saleable_pct"))
    if conv is not None:
        v, t = (("STRONG", "v-strong") if conv >= 60 else
                ("MODERATE", "v-mod") if conv >= 40 else ("CONSTRAINED", "v-risk"))
        flood = next((d for d in (a.get("netout_detail") or [])
                      if d.get("key") == "flood"), {})
        note = (f"floodplain drives efficiency" if (_n(flood.get("acres")) or 0) > 0
                else f"{conv:,.1f}% conversion")
        out.append({"label": "Development potential", "verdict": v, "tone": t, "note": note})
    starts = _n((cbas or {}).get("ring_annual_starts"))
    if starts is not None:
        v, t = (("STRONG", "v-strong") if starts >= 800 else
                ("MODERATE", "v-mod") if starts >= 250 else ("THIN", "v-risk"))
        out.append({"label": "Market demand", "verdict": v, "tone": t,
                    "note": f"{starts:,.0f} annual starts"})
    mos = _n((cbas or {}).get("months_lot_supply"))
    if mos is not None:
        v, t = (("LOW", "v-strong") if mos < 12 else
                ("MODERATE", "v-mod") if mos < 20 else ("MOD-HIGH", "v-risk"))
        out.append({"label": "Competitive risk", "verdict": v, "tone": t,
                    "note": f"{mos:,.1f} mos. lot supply"})
    return out


def _positives(a, cbas, schools, county_growth):
    out = []
    g5 = (schools or {}).get("growth", {}).get("5yr", {}) if schools else {}
    if _n(g5.get("pct")) and _n(g5.get("pct")) > 8:
        out.append(f"District enrollment is up {_n(g5.get('pct')):,.1f}% over five years, "
                   "a credible household-growth signal.")
    starts = _n((cbas or {}).get("ring_annual_starts"))
    if starts and starts >= 400:
        out.append(f"{starts:,.0f} annual starts in the ring: builders are already "
                   "proving demand here, which lowers market-proof risk.")
    slope = _n((a.get("_topo") or {}).get("mean_slope_pct"))
    if slope is not None and slope < 2:
        out.append(f"Gentle topography ({slope:,.2f}% mean slope) implies standard "
                   "grading rather than heavy earthwork.")
    conv = _n(a.get("net_saleable_pct"))
    if conv is not None and conv >= 55:
        out.append(f"{conv:,.1f}% of gross converts to saleable land, an efficient site.")
    return out[:5]


def _risks(a, cbas):
    out = []
    for d in (a.get("netout_detail") or []):
        acres = _n(d.get("acres")) or 0
        gross = _n(a.get("gross_acres")) or 0
        if d.get("key") == "flood" and gross and acres / gross > 0.15:
            out.append(f"{acres:,.1f} acres of floodplain ({acres/gross*100:,.1f}% of gross) "
                       "materially reduces land efficiency and implies mitigation "
                       "and detention cost.")
    mos = _n((cbas or {}).get("months_lot_supply"))
    fut = _n(((cbas or {}).get("aggregate") or {}).get("futures"))
    if mos and mos >= 18:
        s = f"{mos:,.1f} months of lot supply"
        if fut:
            s += f" plus {fut:,.0f} future lots"
        out.append(s + " creates absorption and pricing pressure.")
    slope = _n((a.get("_topo") or {}).get("max_slope_pct"))
    if slope is not None and slope > 8:
        out.append(f"Maximum slope of {slope:,.1f}% indicates areas needing grading design.")
    return out[:5]


NEXT_STEPS = [
    "Civil: confirm floodplain reclamation assumptions, detention need, drainage "
    "outfalls and off-site utility availability.",
    "Market: builder calls and LOIs by lot width, phase and price; reconcile "
    "capture against current community absorption.",
    "Phasing: model a conservative lot takedown against prevailing months of "
    "supply and the future pipeline.",
    "Land basis: derive maximum land value from net saleable acreage and "
    "finished-lot economics, not gross acreage.",
    "Entitlements: validate jurisdiction, MUD / PID / utility-district path, "
    "school boundaries and roadway obligations.",
    "IC output: return with low / base / high yield, absorption and cost cases "
    "plus a clear walk-away land basis.",
]



# ---------------------------------------------------------------------------
# Height budget
#
# A section either fits its sheet or it does not. If a table spills three rows
# onto a fresh page, the page it came from ends short AND the spill page sits
# two-thirds empty, because the next section always starts fresh -- so the only
# real fix is to not spill. These are the template's own measurements in
# points; keep them in step if the CSS padding changes.
#
# Letter is 792pt tall; the @page margins take 0.52in and 0.62in, leaving this.
# ---------------------------------------------------------------------------
PAGE_PT = 709.9
SECTION_CHROME_PT = 74.0        # running header + section title + subtitle
KPI_ROW_PT = 56.0               # a row of metric cards, incl. its margin
TABLE_CHROME_PT = 42.0          # card title + table header + card margin
TABLE_ROW_PT = 18.0
CARD_CHROME_PT = 45.0           # padding + title + margin on a plain card
READ_LINE_PT = 16.0
IMAGE_W_PT = 532.8              # content width: 8.5in less 0.55in each side


def _image_pt(aspect_w, aspect_h, card=True):
    """Height of a full-width figure, plus its card if it sits in one."""
    return IMAGE_W_PT * (aspect_h / aspect_w) + (CARD_CHROME_PT if card else 0)


def _rows_that_fit(used_pt, n_wanted, min_rows=3):
    """How many table rows are left after everything else on the sheet."""
    spare = PAGE_PT - used_pt - TABLE_CHROME_PT
    return max(min_rows, min(n_wanted, int(spare // TABLE_ROW_PT)))

# ---------------------------------------------------------------------------
# Context assembly
# ---------------------------------------------------------------------------
BRIDGE_COLOURS = {
    "gross": NAVY, "flood": "#5B6FD6", "wetlands": "#2E9E6B",
    "streams": "#2F7FD6", "pipelines": "#C99A2E", "transmission": "#B0552E",
    "netdev": NAVY, "infra": ORANGE, "saleable": ORANGE,
}
CONSTRAINT_COLOURS = {
    "flood": "#5B6FD6", "wetlands": "#2E9E6B", "streams": "#2F7FD6",
    "pipelines": "#C99A2E", "transmission": "#B0552E",
}


def build_context(proj, analysis, data=None, elevation=None):
    """Everything the template needs, from data the app already produced.

    `data` carries the payloads the route pulled from the existing endpoints.
    Any of them may be missing or carry an "error" -- in that case the section
    is left out of the context entirely and the template drops it, rather than
    printing an empty card or the word None.
    """
    data = data or {}
    a = dict(analysis or {})
    if elevation:
        a["_topo"] = elevation
    tracts = proj.get("tracts") or []
    gross = _n(a.get("gross_acres")) or 0.0

    counties = sorted({(t.get("county") or "").strip() for t in tracts if t.get("county")})
    location = ", ".join(counties) + (" County, Texas" if len(counties) == 1
                                      else " Counties, Texas") if counties else "Texas"
    import datetime as _dt
    today = _dt.date.today()

    r = {
        "project": {
            "name": proj.get("name") or "Untitled project",
            "name_upper": (proj.get("name") or "Untitled project").upper(),
            "location": location,
            "tract_line": f"{len(tracts)} tract" + ("" if len(tracts) == 1 else "s"),
            "date_long": today.strftime("%B %Y"),
        },
        "kpi": {
            "gross": ac(a.get("gross_acres")),
            "net_dev": ac(a.get("net_developable_acres")),
            "net_saleable": ac(a.get("net_saleable_acres")),
            "lots": num((a.get("yield_estimates") or {}).get("total_lots")) + " lots",
        },
        "missing": [],
    }

    # ---- tract composition -------------------------------------------------
    if tracts:
        r["tracts"] = [{
            "owner": t.get("owner_name") or "—",
            "prop_id": t.get("prop_id") or "—",
            "county": t.get("county") or "—",
            "acres": ac(t.get("acres")),
            "pct": pct((_n(t.get("acres")) or 0) / gross * 100 if gross else None),
        } for t in tracts]

    # ---- gross-to-saleable bridge -----------------------------------------
    detail = a.get("netout_detail") or []
    if gross:
        rows = [{"label": "Gross", "value": ac(gross), "pct": 100,
                 "colour": BRIDGE_COLOURS["gross"], "total": True}]
        for d in detail:
            if not d.get("applied"):
                continue
            marg = _n(d.get("acres_marginal"))
            if marg is None:
                marg = _n(d.get("acres")) or 0
            if marg <= 0:
                continue
            rows.append({
                "label": d.get("label", "").replace(" (100-yr)", "").replace(" (NWI)", ""),
                "value": "-" + ac(marg),
                "pct": max(1.0, marg / gross * 100),
                "colour": BRIDGE_COLOURS.get(d.get("key"), BLUE), "total": False})
        nd = _n(a.get("net_developable_acres"))
        rows.append({"label": "Net developable", "value": ac(nd),
                     "pct": (nd / gross * 100) if nd else 0,
                     "colour": BRIDGE_COLOURS["netdev"], "total": True})
        infra = _n(a.get("infrastructure_acres"))
        if infra:
            rows.append({"label": "Infra / landscape", "value": "-" + ac(infra),
                         "pct": max(1.0, infra / gross * 100),
                         "colour": BRIDGE_COLOURS["infra"], "total": False})
        ns = _n(a.get("net_saleable_acres"))
        rows.append({"label": "Net saleable", "value": ac(ns),
                     "pct": (ns / gross * 100) if ns else 0,
                     "colour": BRIDGE_COLOURS["saleable"], "total": True})
        r["bridge"] = rows

        cons = []
        for d in detail:
            acres = _n(d.get("acres"))
            if acres is None:
                continue
            cons.append({
                "label": d.get("label") or d.get("key"),
                "acres": ac(acres),
                "pct": pct(acres / gross * 100 if gross else None),
                "bar": min(100.0, (acres / gross * 100) if gross else 0),
                "colour": CONSTRAINT_COLOURS.get(d.get("key"), BLUE),
            })
        r["constraints"] = cons

    # ---- topography --------------------------------------------------------
    e = elevation or {}
    if _n(e.get("min_ft")) is not None:
        rng = (_n(e.get("max_ft")) or 0) - (_n(e.get("min_ft")) or 0)
        char = e.get("site_character") or e.get("character")
        r["topo"] = {
            "range": f"{rng:,.1f} ft",
            "min_max": f"{_n(e.get('min_ft')):,.1f} - {_n(e.get('max_ft')):,.1f} ft",
            "mean_slope": pct(e.get("mean_slope_pct"), dp=2),
            "max_slope": pct(e.get("max_slope_pct"), dp=2),
            "drainage": e.get("drainage") or e.get("drainage_dir"),
            "character": (str(char).upper() if char else None),
        }

    # ---- yield -------------------------------------------------------------
    y = a.get("yield_estimates") or {}
    if y.get("total_lots") is not None:
        r["yield"] = {
            "total_lots": num(y.get("total_lots")),
            "density": (f"{_n(y.get('weighted_density')):,.2f} u/ac"
                        if _n(y.get("weighted_density")) else "—"),
            "conversion": pct(a.get("net_saleable_pct")),
            "products": [{
                "product": p.get("label") or p.get("product") or "—",
                "density": (f"{_n(p.get('density')):,.1f} u/ac"
                            if _n(p.get("density")) else "—"),
                "allocation": pct(p.get("allocation_pct"), dp=0),
                "acres": ac(p.get("acres")),
                "lots": num(p.get("lots")),
            } for p in (y.get("breakdown") or [])],
        }

    r["thesis"] = _thesis(r, a, data.get("cbas"), data.get("schools"),
                          counties[0] if counties else None)
    r["factors"] = _factors(a, data.get("cbas"))
    r["dev_read"] = _positives(a, data.get("cbas"), data.get("schools"), None)[:2] or None
    r["why_works"] = _positives(a, data.get("cbas"), data.get("schools"), None)
    r["what_breaks"] = _risks(a, data.get("cbas"))
    r["next_steps"] = NEXT_STEPS
    r["diligence"] = NEXT_STEPS[:4]

    # Each section is mapped in isolation. One upstream payload shaped
    # differently than expected should cost its own page, not the whole
    # document -- an executive report that fails entirely because the macro
    # feed renamed a field is worse than one that comes back a page short and
    # says so.
    import traceback
    sections = (
        ("market", _map_market, (data.get("cbas"),)),
        ("competition", _map_comps, (data.get("cbas"), data.get("comp_map"))),
        ("schools", _map_schools, (data.get("schools"),)),
        ("demographics", _map_demographics, (data.get("market"),)),
        ("access", _map_access, (data.get("roads"), data.get("amenities"))),
        ("amenities", _map_amenities, (data.get("amenities"),)),
        ("news", _map_news, (data.get("news"),)),
        ("macro", _map_macro, (data.get("fred"),)),
    )
    for name, fn, args in sections:
        try:
            fn(r, *args)
        except Exception as e:
            r["missing"].append(f"{name}: {type(e).__name__}: {e}")
            print(f"[report] section {name!r} failed: {e}{chr(10)}"
                  f"{traceback.format_exc()}", flush=True)
    return r


# ---------------------------------------------------------------------------
# Section mappers. Each is a no-op when its payload is absent, so the template
# omits that page rather than printing an empty shell.
# ---------------------------------------------------------------------------
def _map_market(r, cb):
    if not cb:
        return
    agg = cb.get("aggregate") or {}
    starts = _first(agg, "annual_starts") or cb.get("ring_annual_starts")
    closings = _first(agg, "annual_closings")
    vdls = _first(agg, "vdls", "finished_lots")
    radius = _n(cb.get("radius_mi"))

    # Months of lot supply is derived when the field is absent rather than
    # printed as a dash. It is a definition, not a lookup: finished lots over
    # monthly closings. The first run showed "-" here beside 3,349 finished
    # lots and 1,857 annual closings, which is 21.6 months sitting in plain
    # sight on the same row.
    mos = _n(cb.get("months_lot_supply"))
    if mos is None and _n(vdls) and _n(closings):
        mos = _n(vdls) / (_n(closings) / 12.0)

    # price_band is {"min": ..., "max": ...}, not a string.
    pb = cb.get("price_band")
    if isinstance(pb, dict):
        # Exact figures here, not the abbreviated form used elsewhere: a price
        # band is a range someone will quote, and "$185k - $1M" loses the ends.
        lo, hi = _n(pb.get("min")), _n(pb.get("max"))
        price_range = (f"${lo:,.0f} - ${hi:,.0f}" if lo and hi
                       else (f"${(lo or hi):,.0f}" if (lo or hi) else "-"))
    else:
        price_range = str(pb) if pb else "-"

    ctx = {
        "subtitle": ("New-home velocity, lot supply, future pipeline and product depth"
                     + (f" within the {radius:g}-mile competitive ring" if radius else "")),
        "starts": num(starts),
        "closings": num(closings),
        "vdl": num(vdls),
        "mos": (f"{mos:,.1f}" if mos is not None else "-"),
        "mos_hot": bool(mos is not None and mos >= 18),
        "uc": num(_first(agg, "under_construction")),
        # The payload calls it complete_vacant; there is no finished_vacant.
        "fv": num(_first(agg, "complete_vacant")),
        "future": num(_first(agg, "futures")),
        "price_range": price_range,
    }
    bits = []
    if cb.get("quarter_label"):
        bits.append(str(cb["quarter_label"]))
    for key, word in (("community_count", "communities"),
                      ("active_count", "actively closing"),
                      ("builder_count", "builders")):
        if _n(cb.get(key)) is not None:
            bits.append(f"{_n(cb[key]):,.0f} {word}")
    ctx["context"] = "  |  ".join(bits)

    qs = cb.get("quarter_series") or []
    if qs:
        ctx["quarterly_chart"] = render_quarter_chart([
            {"label": q.get("label") or q.get("quarter"),
             "starts": _first(q, "starts", "start"),
             "closings": _first(q, "closings", "closing")} for q in qs])

    # lot_bands ship pre-aggregated: label ("40-50 FF"), avg_price, avg_ppsf.
    # Reading prices/ppsfs/lot_width_ff -- the internal accumulator's names --
    # put a dash in Width, Avg price and $/SF on every row.
    bands = []
    for b in (cb.get("lot_bands") or []):
        ppsf = _n(b.get("avg_ppsf"))
        bands.append({
            "width": str(b.get("label") or "-")[:12],
            "lots": num(b.get("lots")),
            "avg_price": money(b.get("avg_price")),
            "psf": (f"${ppsf:,.0f}" if ppsf is not None else "-"),
            "_sort": _n(b.get("lots")) or 0,
        })
    # Budget the sheet before choosing row counts. Two KPI rows, the quarterly
    # chart and the section chrome already spend 382 of 710 points; two full
    # tables plus a read block came to 793 and spilled.
    used = SECTION_CHROME_PT + 2 * KPI_ROW_PT
    if ctx.get("quarterly_chart"):
        used += _image_pt(6.9, 1.45)   # a shorter chart buys three table rows
    used += CARD_CHROME_PT + 3 * READ_LINE_PT          # the market read
    per_table = max(0.0, (PAGE_PT - used) / 2.0)
    n_rows = max(3, int((per_table - TABLE_CHROME_PT) // TABLE_ROW_PT))
    ctx["lot_bands"] = sorted(bands, key=lambda x: -x["_sort"])[:n_rows]
    ctx["_row_budget"] = n_rows

    blds = []
    for b in (cb.get("builders") or []):
        prices = [p for p in (b.get("prices") or []) if _n(p)]
        avg = _n(b.get("avg_price"))
        if avg is None and prices:
            avg = sum(prices) / len(prices)
        blds.append({
            "name": b.get("name") or "-",
            "starts": num(b.get("est_annual_starts")),
            "lots": num(b.get("lots")),
            "avg_price": money(avg),
            "_sort": _n(b.get("est_annual_starts")) or 0,
        })
    blds.sort(key=lambda x: -x["_sort"])
    nb = ctx.get("_row_budget") or 6
    ctx["builders"] = blds[:nb]
    if len(blds) > nb:
        n = _n(cb.get("builder_count")) or len(blds)
        ctx["builder_note"] = f"{n:,.0f} builders active within the competitive study area."

    read = []
    st, cl = _n(starts), _n(_first(agg, "annual_closings"))
    if st and cl:
        read.append(
            f"Starts ({st:,.0f}) and closings ({cl:,.0f}) are close, so absorption is "
            "keeping pace with delivery."
            if cl >= st * 0.95 else
            f"Starts ({st:,.0f}) run ahead of closings ({cl:,.0f}), a signal of "
            "inventory build in the ring.")
    if mos is not None:
        read.append(f"{mos:,.1f} months of lot supply is elevated; phase conservatively."
                    if mos >= 18 else
                    f"{mos:,.1f} months of lot supply indicates a constrained lot market.")
    # Only claim a deepest product when it is actually named. The first run
    # printed "The deepest product is -, which is where the preliminary mix
    # should concentrate", which is worse than saying nothing.
    top_band = next((b for b in ctx["lot_bands"] if b["width"] not in ("-", "")), None)
    if top_band:
        read.append(f"The deepest product is {top_band['width']} at "
                    f"{top_band['lots']} lots, which is where the preliminary mix "
                    "should concentrate.")
    ctx["read"] = read or None
    r["market"] = ctx


def _map_comps(r, cb, comp_map=None):
    """The competitive set, with the dead entries left out.

    The first run listed the fourteen nearest communities whatever their state,
    so half the table was rows of zeroes -- subdivisions with no starts, no
    closings and no pipeline tell a reader nothing about the competitive
    environment and push the ones that matter off the page. A community earns
    a row only if it is actually doing something.

    Field names are the endpoint's: lot_type_range, months_lot_supply and
    pct_built_out. The first attempt guessed lot_widths / months_supply /
    pct_built and printed a dash in all three columns for every row.
    """
    comms = (cb or {}).get("communities") or []
    if not comms:
        return

    def dist(c):
        return _n(c.get("distance_mi")) or 999.0

    def activity(c):
        return ((_n(c.get("annual_closings")) or 0) + (_n(c.get("annual_starts")) or 0)
                + (_n(c.get("vdls")) or 0) + (_n(c.get("futures")) or 0))

    live = [c for c in comms if activity(c) > 0]
    dropped = len(comms) - len(live)
    # Nearest first, but a community with real velocity outranks a closer one
    # that is only sitting on future lots.
    live.sort(key=lambda c: (0 if (_n(c.get("annual_closings")) or 0) > 0 else 1, dist(c)))

    # Same budget: the competitor map and a KPI row come first, the table gets
    # what is left rather than a fixed count that spilled onto a near-empty
    # page.
    used = SECTION_CHROME_PT + KPI_ROW_PT + CARD_CHROME_PT + READ_LINE_PT * 2
    if comp_map:
        used += _image_pt(7.4, 3.6)
    n_rows = _rows_that_fit(used, 12, min_rows=6)

    rows = []
    for c in live[:n_rows]:
        bl = c.get("builders")
        if isinstance(bl, (list, set, tuple)):
            bl = ", ".join(sorted(str(x) for x in bl)[:2])
        mos = _n(c.get("months_lot_supply"))
        rows.append({
            "name": str(c.get("name") or "-")[:26],
            "distance": miles(c.get("distance_mi"), c.get("direction")),
            "builders": (str(bl)[:24] if bl else "-"),
            "widths": str(c.get("lot_type_range") or c.get("lot_types_ff") or "-")[:12],
            "closings": num(c.get("annual_closings")),
            "starts": num(c.get("annual_starts")),
            "mos": (f"{mos:,.1f}" if mos is not None else "-"),
            "pipeline": num(c.get("futures")),
            "built": pct(c.get("pct_built_out"), dp=0),
        })
    if not rows:
        return

    kpis = []
    nearest = min((dist(c) for c in live), default=None)
    if nearest and nearest < 900:
        kpis.append({"label": "Nearest active comp", "value": miles(nearest)})
    for label, key in (("Top closings", "annual_closings"),
                       ("Largest pipeline", "futures")):
        v = max((_n(c.get(key)) or 0 for c in live), default=0)
        if v:
            kpis.append({"label": label, "value": num(v)})
    mx = [m for m in (_n(c.get("months_lot_supply")) for c in live) if m is not None]
    if mx:
        kpis.append({"label": "Highest MOS", "value": f"{max(mx):,.1f}"})

    note = []
    if len(live) > len(rows):
        note.append(f"{len(live):,} active communities in the study area; the "
                    f"{len(rows)} most relevant are listed.")
    if dropped:
        note.append(f"{dropped:,} with no starts, closings, lots or pipeline omitted.")
    r["comps"] = {
        "map": comp_map, "rows": rows, "kpis": kpis[:4],
        "note": " ".join(note) or None,
        "read": ("nearest communities are already proving demand, but several early-stage "
                 "projects carry large pipelines and long remaining buildout."),
    }


def _map_schools(r, sd):
    """Field names here are the district endpoint's, not invented ones.

    The first attempt guessed district_name / school_count / schools_nearby and
    got an empty section: the payload calls them name, schools_count and
    schools, and buries the growth windows under growth["windows"] keyed
    5_year rather than 5yr.
    """
    if not sd or sd.get("error"):
        return
    win = (sd.get("growth") or {}).get("windows") or {}
    growth = []
    for key, label in (("5_year", "Enrollment / 5 yr"), ("10_year", "10-year"),
                       ("20_year", "20-year"), ("all_time", "All-time")):
        blk = win.get(key) or {}
        p = _n(blk.get("total_pct"))
        if p is not None:
            cagr = _n(blk.get("cagr_pct"))
            growth.append({"label": label, "value": f"{p:+,.1f}%",
                           "note": (f"{cagr:+,.2f}% CAGR" if cagr is not None else "")})
    tea = sd.get("tea") or {}
    rating = tea.get("overall_rating")
    score = _n(tea.get("overall_score"))
    campuses = []
    for c in sorted((sd.get("schools") or []),
                    key=lambda x: _n(x.get("distance_mi")) or 999.0)[:8]:
        campuses.append({
            "name": str(c.get("name") or "-")[:30],
            "tea": str(c.get("tea_rating") or "Not rated")[:12],
            "level": str(c.get("level") or "-")[:20],
            "enrollment": num(c.get("enrollment")),
            "distance": miles(c.get("distance_mi"), c.get("direction")),
        })
    r["schools"] = {
        "district": str(sd.get("name") or "School district").upper(),
        "rating": (f"{rating} / {score:,.0f}" if rating and score is not None
                   else (str(rating) if rating else "-")),
        "rating_note": ("TEA " + str(sd.get("tea_year") or "")).strip(),
        "growth": growth[:3],
        "enrollment": num(sd.get("enrollment")),
        "school_count": num(sd.get("schools_count")),
        "teachers": num(sd.get("teachers_fte")),
        "ratio": (f"{_n(sd.get('student_teacher_ratio')):,.1f} : 1"
                  if _n(sd.get("student_teacher_ratio")) is not None else "-"),
        "trend_chart": render_enrollment_chart(sd.get("enrollment_trend")),
        "campuses": campuses,
    }


ACS = [
    ("B01003_001E", "Population", num), ("B11001_001E", "Households", num),
    ("B19013_001E", "Median HH income", money), ("B19301_001E", "Per-capita income", money),
    ("B25077_001E", "Median home value", money), ("B25064_001E", "Median rent", money),
    ("B25001_001E", "Housing units", num), ("B23025_004E", "Employed", num),
]


def _map_demographics(r, mk):
    if not mk or mk.get("error"):
        return
    src = mk.get("current") if isinstance(mk.get("current"), dict) else mk
    rows = []
    for code, label, fmt in ACS:
        v = src.get(code, mk.get(code))
        if _n(v) is None:
            continue
        rows.append({"label": label, "value": fmt(v), "note": ""})
    occ, vac = _n(src.get("B25002_002E")), _n(src.get("B25002_003E"))
    if occ and vac is not None and (occ + vac):
        rows.append({"label": "Vacancy rate", "value": pct(vac / (occ + vac) * 100), "note": ""})
    own, rent = _n(src.get("B25003_002E")), _n(src.get("B25003_003E"))
    if own and rent is not None and (own + rent):
        rows.append({"label": "Owner occupancy", "value": pct(own / (own + rent) * 100, dp=0),
                     "note": ""})
    tot = _n(src.get("B15003_001E"))
    ba = sum(_n(src.get(k)) or 0 for k in
             ("B15003_022E", "B15003_023E", "B15003_024E", "B15003_025E"))
    if tot and ba:
        rows.append({"label": "Bachelor's degree +", "value": pct(ba / tot * 100), "note": ""})
    if rows:
        r["demographics"] = {
            "title": str(mk.get("county_name") or "County") + " - demand context",
            "rows": rows[:12],
        }


def _map_access(r, roads, am):
    am = am or {}
    cards = []
    hw = [h for h in (am.get("highways") or []) if _n(h.get("distance_mi")) is not None]
    if hw:
        h = min(hw, key=lambda x: _n(x.get("distance_mi")))
        # OSM packs concurrent routes into one ref separated by semicolons
        # ("US 290;TX 6"), which reads as a typo on a printed page.
        ref = str(h.get("ref") or h.get("name") or "-").replace(";", " / ")
        cards.append({"label": "Nearest freeway", "value": ref[:20],
                      "note": miles(h.get("distance_mi"), h.get("direction"))})
    if _n(am.get("nearest_ramp_mi")) is not None:
        cards.append({"label": "Nearest on-ramp", "value": miles(am["nearest_ramp_mi"]),
                      "note": "closest interchange"})
    ap = [a for a in (am.get("airports") or []) if _n(a.get("distance_mi")) is not None]
    if ap:
        comm = [a for a in ap if a.get("commercial")] or ap
        a0 = min(comm, key=lambda x: _n(x.get("distance_mi")))
        cards.append({"label": "Airport", "value": str(a0.get("iata") or a0.get("name") or "-")[:16],
                      "note": miles(a0.get("distance_mi"), a0.get("direction"))})
    if not cards and not roads:
        return

    projects, note, funded = [], None, None
    if roads:
        planned = (roads.get("planned") or []) + (roads.get("programmed") or [])

        def yr(p):
            return _n(_first(p, "let_year", "start_year")) or 9999

        rows = sorted(planned, key=yr)[:10]
        for p in rows:
            frm = _first(p, "from", default="") or ""
            to = _first(p, "to", default="") or ""
            projects.append({
                "road": str(_first(p, "roadway", "highway", default="-"))[:16],
                "work": str(_first(p, "work", "description", default="-"))[:38],
                "limits": (f"{frm} - {to}".strip(" -") or "-")[:38],
                "start": num(_first(p, "let_year", "start_year")),
            })
        near = _n((roads.get("counts") or {}).get("near_term"))
        if near:
            funded = f"{near:,.0f} funded / dated in the next four years"
        if len(planned) > len(rows):
            note = (f"{len(planned):,} programmed projects within "
                    f"{roads.get('radius_mi', '')} mi; nearest-term shown.")

    read = []
    if cards and cards[0]["label"] == "Nearest freeway":
        read.append(f"{cards[0]['value']} at {cards[0]['note']} gives a credible regional "
                    "access story.")
    if projects:
        read.append("Programmed capacity work on the surrounding network reinforces the "
                    "longer-term growth corridor.")
        read.append("Off-site road obligations and timing should be tied directly into "
                    "phase-level development underwriting.")
    r["access"] = {"cards": cards[:4], "projects": projects, "funded_note": funded,
                   "note": note, "read": read or None, "map": None}


AMENITY_GROUPS = [
    ("Grocery & retail", ("grocery_stores",)),
    ("Healthcare", ("hospitals",)),
    ("Pharmacies & parks", ("pharmacies", "parks")),
    ("Fuel & convenience", ("fuel",)),
]


def _map_amenities(r, am):
    if not am:
        return
    groups = []
    for title, keys in AMENITY_GROUPS:
        items = []
        for k in keys:
            for x in (am.get(k) or []):
                if _n(x.get("distance_mi")) is None:
                    continue
                items.append({"name": str(x.get("name") or x.get("brand") or "-")[:32],
                              "distance": miles(x.get("distance_mi"), x.get("direction")),
                              "_d": _n(x.get("distance_mi"))})
        items.sort(key=lambda i: i["_d"])
        if items:
            # NOT "items": in Jinja, `group.items` resolves to dict.items --
            # the bound method -- before it falls back to the key, and the
            # template then tries to iterate a builtin_function_or_method.
            groups.append({"title": title, "places": items[:5]})
    nearest = []
    for label, key in (("Nearest fuel", "fuel"), ("Nearest pharmacy", "pharmacies"),
                       ("Nearest grocery", "grocery_stores"),
                       ("Nearest hospital", "hospitals")):
        pool = [x for x in (am.get(key) or []) if _n(x.get("distance_mi")) is not None]
        if pool:
            x = min(pool, key=lambda i: _n(i["distance_mi"]))
            nearest.append({"label": label, "value": miles(x["distance_mi"]),
                            "note": str(x.get("name") or x.get("brand") or "")[:20]})
    if groups or nearest:
        r["amenities"] = {"groups": groups, "nearest": nearest[:4]}


NEWS_MAX_AGE_DAYS = 550          # roughly eighteen months

# Why a story matters to an acquisition, keyed off what it is about. A headline
# on its own makes a reader do the work; the point of this section is to say
# what the signal is.
NEWS_SIGNALS = [
    (("jobs", "hiring", "employment", "workforce", "manufactur", "plant"),
     "Employment signal supporting regional household growth."),
    (("distribution", "industrial", "warehouse", "logistics", "data center"),
     "Industrial absorption -- a demand driver for nearby rooftops."),
    (("hospital", "medical", "health", "clinic"),
     "Healthcare investment adds an institutional growth signal."),
    (("highway", "road", "interchange", "corridor", "expansion", "infrastructure",
      "utility", "water"),
     "Infrastructure investment affecting access and development timing."),
    (("home", "housing", "subdivision", "master-planned", "master planned",
      "lots", "builder", "residential", "development"),
     "Reinforces the residential growth thesis and the future-supply picture."),
    (("school", "isd", "enrollment", "campus"),
     "District growth pressure, a proxy for household formation."),
    (("acres", "land", "ranch", "acquisition", "sold", "purchase"),
     "Land transaction comparable to the subject."),
]


def _news_relevance(title):
    """Match on whole words, not substrings.

    A plain `"water" in title` tagged "New Homes Now Selling in Attwater" as
    infrastructure, because the place name contains the keyword. Place names
    swallow short keywords constantly, so every term is anchored.
    """
    t = " " + re.sub(r"[^a-z0-9 ]+", " ", (title or "").lower()) + " "
    for words, why in NEWS_SIGNALS:
        for w in words:
            # prefix match so "manufactur" still catches manufacturing
            if re.search(r"\b" + re.escape(w), t):
                return why
    return None


def _map_news(r, nw):
    """Recent, relevant stories only.

    The feed returns whatever the query matched, oldest included, in query
    order. A report dated this month carrying a two-year-old headline reads as
    stale research, so anything past NEWS_MAX_AGE_DAYS is dropped and the rest
    are newest first.
    """
    import datetime as _dt
    from email.utils import parsedate_to_datetime

    stories = (nw or {}).get("stories") or []
    if not stories:
        return
    now = _dt.datetime.now(_dt.timezone.utc)
    dated = []
    for st in stories:
        raw = st.get("published")
        when = None
        if raw:
            try:                                  # RSS: RFC-2822
                when = parsedate_to_datetime(str(raw))
            except Exception:
                try:                              # or ISO
                    when = _dt.datetime.fromisoformat(str(raw)[:19])
                except Exception:
                    when = None
        if when is not None and when.tzinfo is None:
            when = when.replace(tzinfo=_dt.timezone.utc)
        # An undated story is kept but sorts last -- dropping it would hide a
        # relevant item purely because the feed omitted a timestamp.
        if when is not None and (now - when).days > NEWS_MAX_AGE_DAYS:
            continue
        dated.append((when, st))
    dated.sort(key=lambda p: (p[0] is not None, p[0]), reverse=True)

    # The aggregator runs several overlapping queries, so one event arrives as
    # three near-identical headlines from three outlets -- "Waller ISD faces
    # registration backlog", "Waller ISD experiences application surge", "'All
    # hands on deck': Waller ISD experiences application..." all in one list.
    # Keeping the first of each cluster is what a person would do.
    STOP = {"the", "a", "an", "of", "in", "at", "to", "for", "and", "on", "as",
            "with", "after", "new", "its", "is", "are", "from", "by"}

    def sig(title):
        return {w for w in re.findall(r"[a-z0-9]+", (title or "").lower())
                if len(w) > 2 and w not in STOP}

    # Token overlap alone does not cluster these: "Waller ISD faces
    # registration backlog", "Waller ISD experiences application backlog" and
    # "'All hands on deck': Waller ISD experiences application backlog" share
    # only two or three words once stop words are gone. Capping each signal
    # category at two is the rule that actually produces a varied page.
    kept, per_topic = [], {}
    for when, st in dated:
        s = sig(st.get("title"))
        if not s:
            continue
        if any(len(s & k) / max(len(s | k), 1) >= 0.34 for k in kept):
            continue                       # same story, different outlet
        why = _news_relevance(st.get("title"))
        topic = why or "other"
        if per_topic.get(topic, 0) >= 2:
            continue                       # already covered this signal
        per_topic[topic] = per_topic.get(topic, 0) + 1
        kept.append(s)
        r.setdefault("news", []).append({
            "headline": str(st.get("title") or "")[:150],
            "source": str(st.get("source") or "")[:34],
            "date": (when.strftime("%b %Y") if when else ""),
            "why": why,
        })
        if len(r["news"]) >= 5:
            break


def _map_macro(r, fr):
    out = []
    for i in ((fr or {}).get("indicators") or []):
        d = i.get("data") or {}
        cur = _n(d.get("current"))
        if d.get("error") or cur is None:
            continue
        fmt = str(i.get("format") or "").lower()
        if "dollar" in fmt or "usd" in fmt or "$" in fmt:
            val = money(cur)
        elif "percent" in fmt or "pct" in fmt or "rate" in fmt:
            val = pct(cur, dp=2)
        elif abs(cur) >= 1_000_000:
            val = f"{cur / 1e6:,.1f}M"
        else:
            val = f"{cur:,.1f}" if abs(cur) < 1000 else num(cur)
        note = []
        if _n(d.get("yoy_pct")) is not None:
            note.append(f"YoY {_n(d['yoy_pct']):+,.1f}%")
        if _n(d.get("five_year_total_pct")) is not None:
            note.append(f"5Y {_n(d['five_year_total_pct']):+,.1f}%")
        out.append({"label": str(i.get("label") or i.get("series_id") or "").upper()[:26],
                    "value": val, "note": "   ".join(note)})
    if out:
        r["macro"] = out[:12]
