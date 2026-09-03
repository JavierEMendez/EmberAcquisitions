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

    service = ("World_Imagery" if kind == "imagery" else "World_Topo_Map")
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
