"""Acquisitions GIS routes.

A blueprint rather than handlers inline in app.py. The subsystem is ~3,400
lines of engine (acq_gis) plus these handlers, most of them ported from the
standalone app, and they call engine helpers by bare name - `cached_layer_query`,
`_cbas_get_data`, `_overpass_query`. The star import below is what lets those
bodies port across unchanged rather than being rewritten with a prefix on every
call, which is where a port this size would otherwise spend its bugs.

Auth, permissions and persistence are EmberApps'. `init_app` is handed the
callables it needs so this module never imports app.py, which would be circular.
"""

from flask import (Blueprint, request, jsonify, session, redirect,
                   render_template, url_for, send_file)
import datetime
import io as _io

from acq_gis import *            # noqa: F401,F403 - see the docstring
import acq_gis

# `import *` skips names beginning with an underscore, and most of the engine's
# helpers are private by convention - _overpass_query, _acs_year, _cbas_get_data
# and so on. The ported handlers call them by bare name, so pull them in too.
# Dunders stay out; they would clobber this module's own __name__ and __doc__.
globals().update({k: v for k, v in vars(acq_gis).items()
                  if k.startswith("_") and not k.startswith("__")})
import acq_parcels as parcel_cache
import acq_store

acq_bp = Blueprint("acq", __name__)

# Filled in by init_app so this module never imports app.py.
get_db = None
login_required = None
_refresh_page_access_from_db = None
_log_activity = None


def init_app(app):
    """Wire the blueprint to the host app's auth, DB and logging."""
    global get_db, login_required, _refresh_page_access_from_db, _log_activity
    get_db = app.config["ACQ_GET_DB"]
    login_required = app.config["ACQ_LOGIN_REQUIRED"]
    _refresh_page_access_from_db = app.config["ACQ_REFRESH_PAGE_ACCESS"]
    _log_activity = app.config["ACQ_LOG_ACTIVITY"]
    app.register_blueprint(acq_bp)



def _login_required(f):
    """Defer to the host app's login_required.

    The real decorator is not available at import time - init_app has not run
    yet - so this wrapper resolves it per request instead of at decoration.
    """
    from functools import wraps

    @wraps(f)
    def wrapper(*a, **kw):
        return login_required(f)(*a, **kw)
    return wrapper

# ══════════════════════════════════════════════════════════════════════════
# ACQUISITIONS GIS
#
# Land screening: search parcels, assemble them into a project, run the
# constraint/yield analysis, pull market and competition context.
#
# The engine lives in acq_gis (live GIS layer queries, geometry, spatial
# enrichment), the statewide parcel cache in acq_parcels, and persistence in
# acq_store. These routes are the thin layer between them and the browser.
#
# Everything namespaces under /api/acq/ deliberately. The standalone app used
# bare /api/projects, which is already MPC Underwriting's in this app - four
# other routes collided the same way (/, /login, /logout, /api/projects/<id>).
# ══════════════════════════════════════════════════════════════════════════

ACQ_PAGE_KEY = "acquisitions"


def _can_view_acquisitions() -> bool:
    """Per-user gate for /acquisitions. Admin overrides; otherwise the user
    must hold page_access.acquisitions.

    Defaults to False. Land screening exposes owner names, mailing addresses
    and appraisal values for parcels the company may be about to approach, so
    it is closer to the financials tier than to macro. Re-reads from the DB so
    an admin grant applies without forcing a re-login."""
    if session.get("is_admin"):
        return True
    pa = _refresh_page_access_from_db()
    return bool(pa.get(ACQ_PAGE_KEY, False))


def _acq_guard():
    """403 JSON for API routes. Returns None when the user may proceed."""
    if not _can_view_acquisitions():
        return jsonify({"error": "forbidden"}), 403
    return None


def _acq_owner():
    return session.get("user_id")


def _acq_is_admin():
    return bool(session.get("is_admin"))


@acq_bp.route("/acquisitions")
@_login_required
def acquisitions_page():
    if not _can_view_acquisitions():
        return redirect(url_for("home"))
    return render_template(
        "acquisitions.html",
        username=session.get("username"),
        display_name=session.get("display_name", session.get("username")),
        is_admin=session.get("is_admin", False),
        page_access=session.get("page_access") or {},
    )


# ── Geocoding ─────────────────────────────────────────────────────────────

@acq_bp.route("/api/acq/geocode/suggest")
@_login_required
def api_acq_geocode_suggest():
    guard = _acq_guard()
    if guard:
        return guard
    q = (request.args.get("q") or "").strip()
    if len(q) < 3:
        return jsonify({"suggestions": []})
    try:
        return jsonify({"suggestions": geocode_suggest(q)})
    except Exception as e:
        print(f"[acq] geocode suggest failed: {e}", flush=True)
        return jsonify({"suggestions": []})


@acq_bp.route("/api/acq/geocode")
@_login_required
def api_acq_geocode():
    guard = _acq_guard()
    if guard:
        return guard
    q = (request.args.get("q") or "").strip()
    if not q:
        return jsonify({"error": "q required"}), 400
    try:
        hit = geocode(q)
        if not hit:
            return jsonify({"error": "no match"}), 404
        return jsonify(hit)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ── Map layers ────────────────────────────────────────────────────────────

@acq_bp.route("/api/acq/load-layer/<key>")
@_login_required
def api_acq_load_layer(key):
    """One ancillary layer for the visible bbox.

    Bbox is required and capped: a whole-state query against FEMA or NWI would
    run for minutes and tie up a worker.
    """
    guard = _acq_guard()
    if guard:
        return guard
    from shapely.geometry import box as shp_box
    try:
        minx = float(request.args["minx"]); miny = float(request.args["miny"])
        maxx = float(request.args["maxx"]); maxy = float(request.args["maxy"])
    except (KeyError, TypeError, ValueError) as e:
        return jsonify({"error": f"bad bbox: {e}"}), 400

    width, height = maxx - minx, maxy - miny
    if width <= 0 or height <= 0:
        return jsonify({"error": "bbox is empty"}), 400
    if width > 5.0 or height > 5.0:
        return jsonify({"error": "bbox too large; zoom in further"}), 400

    bbox_poly = shp_box(minx, miny, maxx, maxy)
    G, E = acq_gis, ENDPOINTS
    fetchers = {
        "counties":     lambda: G.cached_layer_query("counties", bbox_poly),
        "etj":          lambda: G.arcgis_query(E["etj"], bbox=bbox_poly.bounds),
        "schools":      lambda: G.cached_layer_query("tea_schools", bbox_poly),
        "water_dist":   lambda: G.cached_layer_query(
                            "tceq_water", bbox_poly,
                            where="ACCURACY='P' AND STATUS='A'",
                            out_fields="NAME,TYPE,TYPE_DESCRIPTION,COUNTY,DISTRICT_ID,Area_Acres"),
        "muds":         lambda: G.cached_layer_query(
                            "tceq_water", bbox_poly,
                            where="ACCURACY='P' AND STATUS='A' AND TYPE='MUD'",
                            out_fields="NAME,TYPE,TYPE_DESCRIPTION,COUNTY,Area_Acres"),
        "ccn":          lambda: G.cached_layer_query("puc_ccn", bbox_poly,
                            out_fields="UTILITY,CCN_NO,COUNTY,CCN_TYPE"),
        "electric":     lambda: G.cached_layer_query("electric", bbox_poly,
                            out_fields="Utility_Name"),
        "flood":        lambda: G.cached_layer_query(
                            "fema_flood", bbox_poly,
                            where="FLD_ZONE IN ('A','AE','AH','AO','AR','A99','V','VE')",
                            out_fields="FLD_ZONE,ZONE_SUBTY"),
        "wetlands":     lambda: G.cached_wetlands(bbox_poly, (miny + maxy) / 2,
                            (minx + maxx) / 2, max(width, height) * 69),
        "streams":      lambda: G.cached_layer_query("streams", bbox_poly),
        "pipelines":    lambda: G.arcgis_query(E["rrc_pipelines"], geometry_polygon=bbox_poly),
        "transmission": lambda: G.cached_layer_query("transmission", bbox_poly),
        "wells":        lambda: G.arcgis_query(E["rrc_wells"], geometry_polygon=bbox_poly),
        "txdot_projects": lambda: G.cached_layer_query("txdot_projects", bbox_poly,
                            page_size=500, max_pages=4),
    }
    if key not in fetchers:
        return jsonify({"error": f"unknown layer key: {key!r}",
                        "known": sorted(fetchers)}), 400
    try:
        fc = fetchers[key]()
        return jsonify({"key": key, "fc": fc,
                        "count": len(fc.get("features") or [])})
    except Exception as e:
        return jsonify({"error": f"layer fetch failed: {type(e).__name__}: {e}"}), 500


@acq_bp.route("/api/acq/parcels")
@_login_required
def api_acq_parcels():
    """Parcels in the visible map area, for browse mode."""
    guard = _acq_guard()
    if guard:
        return guard
    try:
        minx = float(request.args["minx"]); miny = float(request.args["miny"])
        maxx = float(request.args["maxx"]); maxy = float(request.args["maxy"])
    except (KeyError, TypeError, ValueError) as e:
        return jsonify({"error": f"bad bbox: {e}"}), 400
    if (maxx - minx) > 0.5 or (maxy - miny) > 0.5:
        return jsonify({"type": "FeatureCollection", "features": [],
                        "note": "Zoom in further to load parcels."})
    try:
        return jsonify(arcgis_query(
            ENDPOINTS["parcels"], bbox=(minx, miny, maxx, maxy),
            out_fields="Prop_ID,OWNER_NAME,SITUS_ADDR,MAIL_ADDR,LEGAL_AREA,GIS_AREA,LEGAL_DESC",
            page_size=2000, max_pages=5))
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ── Owner search ──────────────────────────────────────────────────────────

@acq_bp.route("/api/acq/owner-dossier")
@_login_required
def api_acq_owner_dossier():
    """Every cached parcel belonging to one owner.

    Fuzzy matching only for names carrying two or more distinctive tokens.
    The caller always passes a full OWNER_NAME off a parcel popup, never a
    typed fragment, so a bare one-token query staying exact-only costs nothing
    and keeps "EMBER" from dragging in every EMBERG in the state.
    """
    guard = _acq_guard()
    if guard:
        return guard
    name = (request.args.get("name") or "").strip()
    if not name:
        return jsonify({"error": "name required"}), 400
    exact_only = request.args.get("exact", "0") == "1"
    tokens = parcel_cache._owner_tokens(name)
    used_fuzzy = False
    if exact_only or len(tokens) < 2:
        result = parcel_cache.find_parcels_by_owner(name, exact=True, limit=500)
    else:
        result = parcel_cache.find_parcels_by_owner(name, exact=False, limit=500)
        used_fuzzy = True
    passes = {p.get("match_pass") for p in result.get("parcels") or []}
    result["match_mode"] = ("fuzzy" if used_fuzzy and not (passes and passes <= {"exact"})
                            else "exact")
    result["query"] = name
    return jsonify(result)


@acq_bp.route("/api/acq/parcel-detail/<prop_id>")
@_login_required
def api_acq_parcel_detail(prop_id):
    guard = _acq_guard()
    if guard:
        return guard
    try:
        return jsonify(parcel_detail(prop_id))
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ── Saved objects (projects, pins, folders, notes, favorites) ─────────────

def _acq_list(kind):
    conn = get_db()
    try:
        return acq_store.list_objects(conn, kind, _acq_owner(), _acq_is_admin())
    finally:
        conn.close()


def _acq_save(kind, obj):
    conn = get_db()
    try:
        return acq_store.put_object(conn, kind, obj, _acq_owner())
    finally:
        conn.close()


@acq_bp.route("/api/acq/projects", methods=["GET"])
@_login_required
def api_acq_projects_list():
    guard = _acq_guard()
    if guard:
        return guard
    projects = _acq_list("project")
    # A one-off "quick analysis" is not a project you meant to keep. Both come
    # back, split, so the page can list intentional work first and park the
    # rest in history.
    return jsonify({
        "projects": [p for p in projects if not p.get("is_quick_analysis")],
        "history":  [p for p in projects if p.get("is_quick_analysis")],
    })


@acq_bp.route("/api/acq/projects", methods=["POST"])
@_login_required
def api_acq_projects_create():
    guard = _acq_guard()
    if guard:
        return guard
    body = request.get_json(silent=True) or {}
    name = (body.get("name") or "").strip()
    if not name:
        return jsonify({"error": "name required"}), 400
    proj = {
        "name": name,
        "tracts": body.get("tracts") or [],
        "is_quick_analysis": bool(body.get("is_quick_analysis")),
        "created_at": datetime.datetime.utcnow().isoformat(),
    }
    proj["total_acres"] = round(
        sum(float(t.get("acres") or 0) for t in proj["tracts"]), 2)
    return jsonify({"project": _acq_save("project", proj)}), 201


@acq_bp.route("/api/acq/projects/<pid>", methods=["GET"])
@_login_required
def api_acq_project_get(pid):
    guard = _acq_guard()
    if guard:
        return guard
    conn = get_db()
    try:
        proj = acq_store.get_object(conn, "project", pid, _acq_owner(), _acq_is_admin())
    finally:
        conn.close()
    if not proj:
        return jsonify({"error": "project not found"}), 404
    return jsonify({"project": proj})


@acq_bp.route("/api/acq/projects/<pid>", methods=["PATCH"])
@_login_required
def api_acq_project_patch(pid):
    guard = _acq_guard()
    if guard:
        return guard
    body = request.get_json(silent=True) or {}
    conn = get_db()
    try:
        proj = acq_store.get_object(conn, "project", pid, _acq_owner(), _acq_is_admin())
        if not proj:
            return jsonify({"error": "project not found"}), 404
        for k in ("name", "tracts", "yield_assumptions", "netout_assumptions",
                  "notes", "is_quick_analysis"):
            if k in body:
                proj[k] = body[k]
        if "tracts" in body:
            proj["total_acres"] = round(
                sum(float(t.get("acres") or 0) for t in proj.get("tracts") or []), 2)
        saved = acq_store.put_object(conn, "project", proj, proj.get("_owner_id") or _acq_owner())
    finally:
        conn.close()
    return jsonify({"project": saved})


@acq_bp.route("/api/acq/projects/<pid>", methods=["DELETE"])
@_login_required
def api_acq_project_delete(pid):
    guard = _acq_guard()
    if guard:
        return guard
    conn = get_db()
    try:
        ok = acq_store.delete_object(conn, "project", pid, _acq_owner(), _acq_is_admin())
    finally:
        conn.close()
    return jsonify({"deleted": ok}), (200 if ok else 404)


@acq_bp.route("/api/acq/tract-pins", methods=["GET", "POST"])
@_login_required
def api_acq_tract_pins():
    guard = _acq_guard()
    if guard:
        return guard
    if request.method == "GET":
        return jsonify({"pins": _acq_list("tract_pin")})
    body = request.get_json(silent=True) or {}
    if not (body.get("prop_id") or "").strip():
        return jsonify({"error": "prop_id required"}), 400
    return jsonify({"pin": _acq_save("tract_pin", body)}), 201


@acq_bp.route("/api/acq/tract-pins/<pin_id>", methods=["DELETE"])
@_login_required
def api_acq_tract_pin_delete(pin_id):
    guard = _acq_guard()
    if guard:
        return guard
    conn = get_db()
    try:
        ok = acq_store.delete_object(conn, "tract_pin", pin_id, _acq_owner(), _acq_is_admin())
    finally:
        conn.close()
    return jsonify({"deleted": ok}), (200 if ok else 404)


@acq_bp.route("/api/acq/folders", methods=["GET", "POST"])
@_login_required
def api_acq_folders():
    guard = _acq_guard()
    if guard:
        return guard
    if request.method == "GET":
        return jsonify({"folders": _acq_list("folder")})
    body = request.get_json(silent=True) or {}
    name = (body.get("name") or "").strip()
    if not name:
        return jsonify({"error": "name required"}), 400
    return jsonify({"folder": _acq_save("folder", {"name": name,
                                                    "color": body.get("color")})}), 201


@acq_bp.route("/api/acq/notes/by-props", methods=["POST"])
@_login_required
def api_acq_notes_by_props():
    """Note counts for a screenful of parcels, so the map can flag which ones
    already carry a note without one request per parcel."""
    guard = _acq_guard()
    if guard:
        return guard
    prop_ids = (request.get_json(silent=True) or {}).get("prop_ids") or []
    conn = get_db()
    try:
        found = acq_store.find_by_prop(conn, "note", prop_ids,
                                        _acq_owner(), _acq_is_admin())
    finally:
        conn.close()
    return jsonify({"counts": {k: len(v) for k, v in found.items()}})


@acq_bp.route("/api/acq/notes/<prop_id>", methods=["GET", "POST"])
@_login_required
def api_acq_notes(prop_id):
    guard = _acq_guard()
    if guard:
        return guard
    conn = get_db()
    try:
        if request.method == "GET":
            found = acq_store.find_by_prop(conn, "note", [prop_id],
                                            _acq_owner(), _acq_is_admin())
            return jsonify({"notes": found.get(str(prop_id), [])})
        body = request.get_json(silent=True) or {}
        text = (body.get("text") or "").strip()
        if not text:
            return jsonify({"error": "text required"}), 400
        note = acq_store.put_object(conn, "note", {
            "prop_id": str(prop_id),
            "text": text,
            "author": session.get("username"),
            "created_at": datetime.datetime.utcnow().isoformat(),
        }, _acq_owner())
    finally:
        conn.close()
    return jsonify({"note": note}), 201


# Distinct path rather than /api/acq/notes/<note_id>: that collides with the
# GET/POST rule above, which takes a prop_id. Werkzeug does disambiguate them
# by method, but two rules sharing a path with different meanings for the same
# segment is a trap for whoever adds a method next.
@acq_bp.route("/api/acq/notes/id/<note_id>", methods=["DELETE"])
@_login_required
def api_acq_note_delete(note_id):
    guard = _acq_guard()
    if guard:
        return guard
    conn = get_db()
    try:
        ok = acq_store.delete_object(conn, "note", note_id, _acq_owner(), _acq_is_admin())
    finally:
        conn.close()
    return jsonify({"deleted": ok}), (200 if ok else 404)


@acq_bp.route("/api/acq/search", methods=["POST"])
@_login_required
def api_acq_search():
    """Parcels within a radius of a point, filtered by acreage.

    Serves from the local parcel cache when it covers the area - an R-Tree
    bbox pre-filter plus an exact shapely intersect answers in well under a
    second. Falls back to a live StratMap query for counties that have not
    been bootstrapped, which is slower but means an uncached county returns
    results rather than an empty map.
    """
    guard = _acq_guard()
    if guard:
        return guard
    body = request.get_json(silent=True) or {}
    try:
        lat = float(body["lat"]); lon = float(body["lon"])
    except (KeyError, TypeError, ValueError):
        return jsonify({"error": "lat and lon required"}), 400
    radius_mi = max(0.1, min(float(body.get("radius_mi") or 5), 50))
    min_acres = max(0.0, float(body.get("min_acres") or 0))
    max_acres = float(body.get("max_acres") or 1e12)
    if max_acres <= min_acres:
        return jsonify({"error": "max_acres must exceed min_acres"}), 400

    from shapely.geometry import Point as _Pt
    # Degrees of latitude are ~69 miles everywhere; longitude shrinks with
    # latitude. Buffering in degrees would give an ellipse that is too wide in
    # east-west, so buffer in metres and project back.
    import pyproj as _pyproj
    from shapely.ops import transform as _shp_transform
    utm_epsg = 32614 if lon < -96 else 32615
    to_utm = _pyproj.Transformer.from_crs("EPSG:4326", f"EPSG:{utm_epsg}", always_xy=True).transform
    to_wgs = _pyproj.Transformer.from_crs(f"EPSG:{utm_epsg}", "EPSG:4326", always_xy=True).transform
    buf = _shp_transform(to_wgs, _shp_transform(to_utm, _Pt(lon, lat)).buffer(radius_mi * 1609.344))

    source = "cache"
    try:
        coverage = parcel_cache.coverage_for_polygon(buf)
        cached = parcel_cache.is_fully_cached(coverage)
    except Exception as e:
        print(f"[acq-search] coverage check failed: {e}", flush=True)
        coverage, cached = [], False

    try:
        if cached:
            features = parcel_cache.query_parcels_in_polygon(buf, min_acres, max_acres)
        else:
            source = "live"
            fc = arcgis_query(
                ENDPOINTS["parcels"], geometry_polygon=buf,
                out_fields="Prop_ID,OWNER_NAME,SITUS_ADDR,MAIL_ADDR,LEGAL_AREA,GIS_AREA,LEGAL_DESC",
                page_size=2000, max_pages=6)
            features = []
            for f in fc.get("features") or []:
                p = f.get("properties") or {}
                ac = p.get("GIS_AREA") or p.get("LEGAL_AREA") or 0
                try:
                    ac = float(ac)
                except (TypeError, ValueError):
                    ac = 0.0
                if min_acres <= ac <= max_acres:
                    p["Acres"] = round(ac, 2)
                    features.append(f)
    except Exception as e:
        return jsonify({"error": f"parcel search failed: {type(e).__name__}: {e}"}), 500

    # Live owner refresh for the two counties whose appraisal districts publish
    # one - StratMap is a yearly snapshot and ownership is the field most
    # likely to have moved since.
    fc = {"type": "FeatureCollection", "features": features}
    for fn, label in ((hcad_live_overlay, "hcad"),
                      (mcad_live_overlay, "mcad")):
        try:
            fn(fc)
        except Exception as e:
            print(f"[acq-search] {label} overlay failed: {e}", flush=True)

    total_acres = 0.0
    for f in fc["features"]:
        p = f.get("properties") or {}
        try:
            total_acres += float(p.get("Acres") or p.get("GIS_AREA") or 0)
        except (TypeError, ValueError):
            pass

    return jsonify({
        "tracts": fc,
        "count": len(fc["features"]),
        "total_acres": round(total_acres, 1),
        "source": source,
        "coverage": coverage,
        "center": {"lat": lat, "lon": lon},
        "radius_mi": radius_mi,
    })


@acq_bp.route("/api/acq/projects/<pid>/analyze", methods=["POST"])
@_login_required
def api_acq_project_analyze(pid):
    """Run the constraint and yield analysis over the project's tracts.

    Slow by nature - it queries FEMA, USFWS, USGS and the RRC live, and a large
    assemblage against a busy wetlands service can take the better part of a
    minute. gunicorn runs with threads for this reason; see gunicorn.conf.py.
    """
    guard = _acq_guard()
    if guard:
        return guard
    conn = get_db()
    try:
        proj = acq_store.get_object(conn, "project", pid, _acq_owner(), _acq_is_admin())
        if not proj:
            return jsonify({"error": "project not found"}), 404
        try:
            analysis = run_analysis(proj)
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
        except Exception as e:
            print(f"[acq-analyze] {pid} failed: {type(e).__name__}: {e}", flush=True)
            return jsonify({"error": f"analysis failed: {type(e).__name__}: {e}"}), 500

        # Cache on the project so reopening the page is instant.
        proj["analysis_cache"] = analysis
        proj["updated_at"] = analysis.get("computed_at")
        acq_store.put_object(conn, "project", proj,
                             proj.get("_owner_id") or _acq_owner())
    finally:
        conn.close()

    # activity_log's second column is `path`, so pass one - the acreage detail
    # rides along after it rather than pretending to be a path of its own.
    _log_activity("acq_project_analyze",
                  f"/acquisitions/project/{pid} "
                  f"({analysis.get('gross_acres')} gross ac -> "
                  f"{analysis.get('net_saleable_acres')} saleable ac)")
    return jsonify({"ok": True, "analysis": analysis})


@acq_bp.route("/acquisitions/project/<pid>")
@_login_required
def acquisitions_project_page(pid):
    if not _can_view_acquisitions():
        return redirect(url_for("home"))
    return render_template(
        "acquisitions_project.html",
        project_id=pid,
        username=session.get("username"),
        display_name=session.get("display_name", session.get("username")),
        is_admin=session.get("is_admin", False),
        page_access=session.get("page_access") or {},
    )


@acq_bp.route("/api/acq/cache/status")
@_login_required
def api_acq_cache_status():
    """Parcel-cache coverage. Surfaces on the page so an empty or stale cache
    reads as a known state rather than as the map being broken."""
    guard = _acq_guard()
    if guard:
        return guard
    try:
        return jsonify(parcel_cache.cache_status())
    except Exception as e:
        return jsonify({"error": str(e), "counties": []}), 200


def _acq_log(event, detail=None):
    """The standalone app's _log_action, mapped onto EmberApps' activity_log."""
    try:
        _log_activity(f"acq_{event}", str(detail)[:400] if detail else None)
    except Exception as e:
        print(f"[acq] activity log failed ({event}): {e}", flush=True)



# ══════════════════════════════════════════════════════════════════════════
# Market, competition and reports
#
# Ported from the standalone app. The bodies are unchanged; only the preamble
# differs - the project comes out of Postgres via acq_store rather than out of
# one JSON file under a file lock.
#
# These are the slow endpoints. CBAS, Overpass, the Census and FRED are all
# remote, and several fan out across mirrors. gunicorn runs with threads for
# this reason; see gunicorn.conf.py.
# ══════════════════════════════════════════════════════════════════════════


@acq_bp.route("/api/acq/tract-sheet/<prop_id>")
@_login_required
def acq_api_tract_sheet(prop_id):
    """Build a one-page PDF for one tract. Tries the current search cache first
    (has all the spatial enrichment); falls back to a live StratMap + HCAD/MCAD
    pull so the report works even after a server restart or for a tract the user
    hasn't run a search for yet."""
    pid = (prop_id or "").strip()
    if not pid:
        return jsonify({"error": "prop_id required"}), 400

    tract = None
    # 1. Try the last-search cache — already has flood %, wells, MUD, etc.
    cache = _last_search_cache.get("data") or {}
    features = (cache.get("tracts") or {}).get("features") or []
    tract = next((f for f in features
                  if str((f.get("properties") or {}).get("Prop_ID") or "").strip() == pid), None)

    # 2. Fallback — look up in the local parcel cache (instant by Prop_ID).
    if not tract:
        try:
            import acq_parcels as parcel_cache
            tract = parcel_cache.find_parcel_by_pid(pid)
        except Exception as e:
            print(f"[tract-sheet] cache lookup failed: {e}", flush=True)

    if not tract:
        return jsonify({"error": f"No parcel found for Prop_ID {pid} in StratMap or local cache."}), 404

    # Refresh CAD values for the sheet even when the tract came from the
    # last-search cache. That cache may have been built before appraised/taxable
    # values were part of the HCAD overlay.
    tmp_fc = {"type": "FeatureCollection", "features": [tract]}
    try: hcad_live_overlay(tmp_fc)
    except Exception as e: print(f"[tract-sheet] hcad overlay failed: {e}", flush=True)
    try: mcad_live_overlay(tmp_fc)
    except Exception as e: print(f"[tract-sheet] mcad overlay failed: {e}", flush=True)
    _enrich_tract_sheet_feature(tract)

    meta = _last_search_cache.get("meta") or {"label": f"tract_{pid}"}
    buf = build_tract_sheet_pdf(tract, meta)
    safe_pid = "".join(c if c.isalnum() else "_" for c in pid)
    owner_slug = (tract.get("properties", {}).get("OWNER_NAME") or "")
    owner_slug = "".join(c if c.isalnum() else "_" for c in owner_slug)[:30].strip("_")
    fname = f"ember_tract_{safe_pid}{('_' + owner_slug) if owner_slug else ''}_{datetime.now().strftime('%Y%m%d_%H%M')}.pdf"
    _acq_log("export_tract_sheet", {"prop_id": pid})
    return send_file(buf, mimetype="application/pdf",
                      as_attachment=True, download_name=fname)


@acq_bp.route("/api/acq/projects/<pid>/cbas", methods=["GET"])
@_login_required
def acq_api_projects_cbas(pid):
    """CBAS competitor survey for everything within `radius_mi` of the project.

    Communities are filtered by true great-circle distance from the project
    centroid using CBAS's own coordinates, so the ring is centred on the deal
    rather than approximated by ZIP boundaries, and builders come back by name.

    Returns, for the ring:
      communities   — each with distance/direction, builder names, lot widths
                      (FF), price range, absorption, VDL, futures, % built out
      builders      — lots under control, communities, lot widths, avg price,
                      avg sqft, $/sf, estimated annual starts
      lot_bands     — supply and pricing by lot width (the product read)
      quarter_series— starts/closings by quarter, summed across the ring
    """
    import math as _m
    from shapely.geometry import shape as shp_shape
    from shapely.ops import unary_union

    token = os.environ.get("CBAS_TOKEN", "").strip()
    if not token:
        return jsonify({"error": "CBAS_TOKEN not configured on the server. "
                                 "Add it in Railway Variables to enable competitor data."}), 503

    guard = _acq_guard()
    if guard:
        return guard
    uid = _acq_owner()
    conn = get_db()
    try:
        proj = acq_store.get_object(conn, "project", pid, uid, _acq_is_admin())
    finally:
        conn.close()
    if not proj:
        return jsonify({"error": "project not found"}), 404

    geoms = []
    for t in proj.get("tracts") or []:
        g = t.get("geometry")
        if g:
            try:
                geoms.append(shp_shape(g))
            except Exception:
                pass
    if not geoms:
        return jsonify({"error": "no tract geometries"}), 400
    proj_union = unary_union(geoms)
    centroid = proj_union.centroid
    c_lat, c_lon = centroid.y, centroid.x

    try:
        radius_mi = float(request.args.get("radius_mi", "8"))
    except (TypeError, ValueError):
        radius_mi = 8.0
    radius_mi = max(1.0, min(radius_mi, 25.0))

    entries, latest, err = _cbas_get_data(token)
    if err:
        return jsonify({"error": err}), 502
    bmap = _cbas_builder_map(entries)

    _COMPASS = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
                "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"]

    def _dist_mi(la, lo):
        R = 3958.8
        p1, p2 = _m.radians(c_lat), _m.radians(la)
        dp, dl = _m.radians(la - c_lat), _m.radians(lo - c_lon)
        x = _m.sin(dp / 2) ** 2 + _m.cos(p1) * _m.cos(p2) * _m.sin(dl / 2) ** 2
        return 2 * R * _m.asin(_m.sqrt(x))

    def _dir(la, lo):
        dy = la - c_lat
        dx = (lo - c_lon) * _m.cos(_m.radians(c_lat))
        ang = (_m.degrees(_m.atan2(dx, dy)) + 360) % 360
        return _COMPASS[int((ang + 11.25) // 22.5) % 16]

    # --- 1) The ring -------------------------------------------------------
    near = []
    for e in entries:
        la, lo = e.get("lat"), e.get("lng")
        if not la or not lo:
            continue
        try:
            d_mi = _dist_mi(float(la), float(lo))
        except Exception:
            continue
        if d_mi <= radius_mi:
            near.append((d_mi, e))
    near.sort(key=lambda t: t[0])

    # Every school district the project POLYGON touches, so in-district
    # competition sorts first. A point-in-polygon test on the centroid was wrong
    # for any tract straddling a boundary — it picked one district and flagged
    # the other half of the project's own market as out-of-district. Query the
    # bbox across all three district layers (Unified / Secondary / Elementary)
    # and keep every one that actually intersects the tract.
    dnames = []
    try:
        import requests as _rq
        b = proj_union.bounds
        for layer_id in (0, 1, 2):
            try:
                sd = _rq.get("https://tigerweb.geo.census.gov/arcgis/rest/services/"
                             f"TIGERweb/School/MapServer/{layer_id}/query",
                             params={"geometry": f"{b[0]},{b[1]},{b[2]},{b[3]}",
                                     "geometryType": "esriGeometryEnvelope", "inSR": "4326",
                                     "spatialRel": "esriSpatialRelIntersects",
                                     "outFields": "NAME", "returnGeometry": "true",
                                     "outSR": "4326", "f": "geojson"},
                             timeout=20)
                if sd.status_code != 200:
                    continue
                for feat in (sd.json().get("features") or []):
                    g = feat.get("geometry")
                    nm = (feat.get("properties") or {}).get("NAME")
                    if not g or not nm or nm in dnames:
                        continue
                    try:
                        if shp_shape(g).intersects(proj_union):
                            dnames.append(nm)
                    except Exception:
                        continue
            except Exception:
                continue
    except Exception:
        pass
    dname = " / ".join(dnames) if dnames else None

    def _norm_d(x):
        return "".join(ch for ch in (x or "").upper() if ch.isalnum()) \
            .replace("INDEPENDENTSCHOOLDISTRICT", "ISD").replace("SCHOOLDISTRICT", "ISD")

    proj_ds = {_norm_d(n) for n in dnames if n}

    def _cbas_num(v):
        try:
            if v in (None, ""):
                return None
            return float(v)
        except (TypeError, ValueError):
            return None

    def _price_stats(prices, sqfts, ppsfs=None):
        pr = [p for p in (_cbas_num(x) for x in prices) if p and p > 0]
        sf = [s for s in (_cbas_num(x) for x in sqfts) if s and s > 0]
        ppsf = [p for p in (_cbas_num(x) for x in (ppsfs or [])) if p and p > 0]
        if not ppsf:
            ppsf = [p / s for p, s in zip(pr, sf) if s]
        return {
            "avg_price": round(sum(pr) / len(pr)) if pr else None,
            "min_price": min(pr) if pr else None,
            "max_price": max(pr) if pr else None,
            "avg_sqft": round(sum(sf) / len(sf)) if sf else None,
            "avg_ppsf": round(sum(ppsf) / len(ppsf), 2) if ppsf else None,
            "plans": len(pr),
        }

    def _community_product_detail(e, ann_starts, ann_closings, vdls, futures):
        """Per-community lot-width and builder detail for the UI expand row.

        CBAS gives lot counts by builder/width, and plan pricing by builder/width.
        VDL/futures are community-level, so those are apportioned by lot share.
        """
        lot_rows = {}
        builder_rows = {}
        builder_lot_rows = {}
        section_lots = 0

        def _builder_name(bid, fallback=None):
            nm = str(fallback or bmap.get(bid) or f"Builder #{bid}").strip()
            return None if nm == "Builder TBD" else nm

        def _lot(ff):
            if ff not in lot_rows:
                lot_rows[ff] = {"lot_width_ff": ff, "lots": 0, "builders": {},
                                "prices": [], "sqfts": [], "ppsfs": []}
            return lot_rows[ff]

        def _builder(bid, name):
            key = bid if bid is not None else name
            if key not in builder_rows:
                builder_rows[key] = {"builder_id": bid, "name": name, "lots": 0,
                                     "lot_types": set(), "prices": [], "sqfts": [],
                                     "ppsfs": []}
            return builder_rows[key]

        def _builder_lot(bid, name, ff):
            key = (bid if bid is not None else name, ff)
            if key not in builder_lot_rows:
                builder_lot_rows[key] = {"builder_id": bid, "name": name,
                                         "lot_width_ff": ff, "lots": 0,
                                         "prices": [], "sqfts": [], "ppsfs": []}
            return builder_lot_rows[key]

        for sec in (e.get("sections") or []):
            for lb in (sec.get("lot_types_builders") or []):
                bid = lb.get("builder")
                if bid is None or bid in _CBAS_PLACEHOLDER_BUILDERS:
                    continue
                ff = _cbas_ff(lb.get("lot_type"))
                if not ff:
                    continue
                lots = _cbas_num(lb.get("num_lots")) or 0
                if lots <= 0:
                    continue
                name = _builder_name(bid)
                if not name:
                    continue
                section_lots += lots
                lrow = _lot(ff)
                lrow["lots"] += lots
                lrow["builders"][name] = lrow["builders"].get(name, 0) + lots
                brow = _builder(bid, name)
                brow["lots"] += lots
                brow["lot_types"].add(ff)
                blrow = _builder_lot(bid, name, ff)
                blrow["lots"] += lots

        for fp in ((e.get("latestFloorplanPricing") or {}).get("entries") or []):
            ff = _cbas_ff(fp.get("lot_type"))
            if not ff:
                continue
            bid = fp.get("builderID")
            if bid in _CBAS_PLACEHOLDER_BUILDERS:
                continue
            name = _builder_name(bid, fp.get("builderName"))
            if not name:
                continue
            price = _cbas_num(fp.get("price"))
            sqft = _cbas_num(fp.get("sqft"))
            if price:
                _lot(ff)["prices"].append(price)
                _builder(bid, name)["prices"].append(price)
                _builder_lot(bid, name, ff)["prices"].append(price)
            if sqft:
                _lot(ff)["sqfts"].append(sqft)
                _builder(bid, name)["sqfts"].append(sqft)
                _builder_lot(bid, name, ff)["sqfts"].append(sqft)
            if price and sqft:
                ppsf = price / sqft
                _lot(ff)["ppsfs"].append(ppsf)
                _builder(bid, name)["ppsfs"].append(ppsf)
                _builder_lot(bid, name, ff)["ppsfs"].append(ppsf)
            _builder(bid, name)["lot_types"].add(ff)

        def _finish(row, share_basis):
            share = (row.get("lots") or 0) / share_basis if share_basis and row.get("lots") else None
            row.update(_price_stats(row.pop("prices", []), row.pop("sqfts", []),
                                    row.pop("ppsfs", [])))
            row["share_pct"] = round(share * 100, 1) if share is not None else None
            row["est_vdls"] = round((vdls or 0) * share) if share is not None else None
            row["est_futures"] = round((futures or 0) * share) if share is not None else None
            row["est_annual_starts"] = round((ann_starts or 0) * share, 1) if share is not None else None
            row["est_annual_closings"] = round((ann_closings or 0) * share, 1) if share is not None else None
            return row

        lots_out = []
        for row in lot_rows.values():
            builders = [{"name": nm, "lots": round(lots)}
                        for nm, lots in sorted(row.pop("builders", {}).items(),
                                               key=lambda kv: -kv[1])]
            row = _finish(row, section_lots)
            row["builders"] = builders[:8]
            row["builder_count"] = len(builders)
            lots_out.append(row)
        lots_out.sort(key=lambda r: r["lot_width_ff"])

        builder_out = []
        for row in builder_rows.values():
            row["lot_types_ff"] = sorted(row.pop("lot_types", set()))
            builder_out.append(_finish(row, section_lots))
        builder_out.sort(key=lambda r: (-(r.get("lots") or 0), r["name"]))

        builder_lot_out = []
        for row in builder_lot_rows.values():
            builder_lot_out.append(_finish(row, section_lots))
        builder_lot_out.sort(key=lambda r: (r["name"], r["lot_width_ff"]))

        return {
            "section_lots": round(section_lots) if section_lots else None,
            "lot_widths": lots_out,
            "builders": builder_out,
            "builder_lot_widths": builder_lot_out,
        }

    comms = []
    for d_mi, e in near:
        total = e.get("total") or 0
        occ = e.get("occupied") or 0
        vdls = e.get("vdls") or 0
        futures = e.get("futures") or 0
        ann_cl = e.get("annual_closings") or 0
        ann_st = e.get("annual_starts") or 0
        lot_types = sorted({f for f in (_cbas_ff(x) for x in (e.get("lot_types") or []))
                            if f is not None})
        prices = e.get("prices") or {}
        cdist = e.get("districtsNames")
        detail = _community_product_detail(e, ann_st, ann_cl, vdls, futures)
        comms.append({
            "id": e.get("id"),
            "name": e.get("public_name") or e.get("name"),
            "community": e.get("communityName"),
            "developer": e.get("developerName"),
            "city": e.get("city"),
            "zip": e.get("zip"),
            "county": e.get("countyName"),
            "submarket": e.get("submarketName"),
            "status": e.get("status"),
            "lat": e.get("lat"), "lon": e.get("lng"),
            "distance_mi": round(d_mi, 2),
            "direction": _dir(float(e["lat"]), float(e["lng"])),
            "builders": [b for b in (e.get("builders_names") or [])
                         if b and b not in ("Builder TBD",)],
            "builder_count": len([b for b in (e.get("builders_names") or [])
                                  if b and b != "Builder TBD"]),
            "lot_types_ff": lot_types,
            "lot_type_range": e.get("lot_type_range"),
            "price_min": prices.get("min"), "price_max": prices.get("max"),
            "starts_qtr": e.get("starts"), "closings_qtr": e.get("closings"),
            "annual_starts": ann_st, "annual_closings": ann_cl,
            "occupied": occ, "models": e.get("models"),
            "complete_vacant": e.get("complete_vacant"),
            "under_construction": e.get("under_construction"),
            "vdls": vdls, "futures": futures, "total_lots": total,
            "pct_built_out": e.get("buildoutPercent"),
            "months_lot_supply": round(vdls / (ann_cl / 12.0), 1) if ann_cl > 0 else None,
            "years_to_sellout": round((vdls + futures) / ann_cl, 1) if ann_cl > 0 else None,
            "school_district": cdist,
            "schools": e.get("schoolNames") or {},
            "in_district": bool(proj_ds and _norm_d(cdist) in proj_ds),
            "link": e.get("link"),
            "detail": detail,
        })
    comms.sort(key=lambda c: (0 if c["in_district"] else 1, -(c["annual_closings"] or 0)))

    # --- 2) Builders in the ring, by name ----------------------------------
    # `sections[].lot_types_builders` is lots controlled per builder per lot
    # width — the truest read of who is actually buying here. Floorplan pricing
    # supplies price/sqft. Annual starts are attributed to a builder by its lot
    # share of the community (flagged as an estimate in the payload).
    bagg = {}

    def _b(bid):
        if bid not in bagg:
            bagg[bid] = {"builder_id": bid, "name": bmap.get(bid) or f"Builder #{bid}",
                         "named": bid in bmap, "lots": 0, "communities": set(),
                         "lot_types": set(), "prices": [], "sqfts": [],
                         "est_annual_starts": 0.0, "est_annual_closings": 0.0}
        return bagg[bid]

    for d_mi, e in near:
        cname = e.get("public_name") or e.get("name")
        per_builder = {}
        for sec in (e.get("sections") or []):
            for lb in (sec.get("lot_types_builders") or []):
                bid, n = lb.get("builder"), lb.get("num_lots") or 0
                if bid is None or bid in _CBAS_PLACEHOLDER_BUILDERS:
                    continue
                per_builder[bid] = per_builder.get(bid, 0) + n
                b = _b(bid)
                b["lots"] += n
                b["communities"].add(cname)
                ff = _cbas_ff(lb.get("lot_type"))
                if ff:
                    b["lot_types"].add(ff)
        tot = sum(per_builder.values()) or 0
        if tot:
            for bid, n in per_builder.items():
                share = n / tot
                _b(bid)["est_annual_starts"] += (e.get("annual_starts") or 0) * share
                _b(bid)["est_annual_closings"] += (e.get("annual_closings") or 0) * share
        # Builders present with no section detail still count as active here
        for bid in (e.get("builders") or []):
            if (bid is not None and bid not in _CBAS_PLACEHOLDER_BUILDERS
                    and bmap.get(bid) != "Builder TBD"):
                _b(bid)["communities"].add(cname)
        for fp in ((e.get("latestFloorplanPricing") or {}).get("entries") or []):
            bid = fp.get("builderID")
            if bid is None or bid in _CBAS_PLACEHOLDER_BUILDERS:
                continue
            b = _b(bid)
            if fp.get("builderName"):
                b["name"] = fp["builderName"]
                b["named"] = True
            if fp.get("price"):
                b["prices"].append(fp["price"])
            if fp.get("sqft"):
                b["sqfts"].append(fp["sqft"])
            ff = _cbas_ff(fp.get("lot_type"))
            if ff:
                b["lot_types"].add(ff)

    builders = []
    for b in bagg.values():
        if b["name"] == "Builder TBD":
            continue
        pr, sf = b["prices"], b["sqfts"]
        builders.append({
            "builder_id": b["builder_id"], "name": b["name"], "named": b["named"],
            "lots": b["lots"], "communities": len(b["communities"]),
            "in": sorted(b["communities"])[:6],
            "lot_types_ff": sorted(b["lot_types"]),
            "avg_price": round(sum(pr) / len(pr)) if pr else None,
            "min_price": min(pr) if pr else None, "max_price": max(pr) if pr else None,
            "avg_sqft": round(sum(sf) / len(sf)) if sf else None,
            "avg_ppsf": round((sum(pr) / len(pr)) / (sum(sf) / len(sf)), 2)
                        if pr and sf and sum(sf) else None,
            "plans": len(pr),
            "est_annual_starts": round(b["est_annual_starts"]),
            "est_annual_closings": round(b["est_annual_closings"]),
        })
    builders.sort(key=lambda b: (-(b["est_annual_starts"] or 0), -(b["lots"] or 0)))

    # --- 3) Product mix by lot width ---------------------------------------
    bands = {label: {"label": label, "min_ff": lo, "max_ff": hi, "lots": 0,
                     "communities": set(), "builders": set(), "prices": [], "sqfts": []}
             for lo, hi, label in _CBAS_LOT_BANDS}
    for d_mi, e in near:
        cname = e.get("public_name") or e.get("name")
        for sec in (e.get("sections") or []):
            for lb in (sec.get("lot_types_builders") or []):
                lbl = _cbas_band(lb.get("lot_type"))
                if not lbl:
                    continue
                bands[lbl]["lots"] += lb.get("num_lots") or 0
                bands[lbl]["communities"].add(cname)
                lbid = lb.get("builder")
                if (lbid is not None and lbid not in _CBAS_PLACEHOLDER_BUILDERS
                        and bmap.get(lbid) and bmap[lbid] != "Builder TBD"):
                    bands[lbl]["builders"].add(bmap[lbid])
        for fp in ((e.get("latestFloorplanPricing") or {}).get("entries") or []):
            lbl = _cbas_band(fp.get("lot_type"))
            if not lbl:
                continue
            if fp.get("price"):
                bands[lbl]["prices"].append(fp["price"])
            if fp.get("sqft"):
                bands[lbl]["sqfts"].append(fp["sqft"])
    lot_bands = []
    for lo, hi, label in _CBAS_LOT_BANDS:
        b = bands[label]
        pr, sf = b["prices"], b["sqfts"]
        lot_bands.append({
            "label": label, "min_ff": lo, "max_ff": hi, "lots": b["lots"],
            "communities": len(b["communities"]), "builders": len(b["builders"]),
            "avg_price": round(sum(pr) / len(pr)) if pr else None,
            "min_price": min(pr) if pr else None, "max_price": max(pr) if pr else None,
            "avg_sqft": round(sum(sf) / len(sf)) if sf else None,
            "avg_ppsf": round((sum(pr) / len(pr)) / (sum(sf) / len(sf)), 2)
                        if pr and sf and sum(sf) else None,
            "plans": len(pr),
        })

    # --- 4) Absorption by quarter across the ring --------------------------
    qs, qc = {}, {}
    for d_mi, e in near:
        for k, v in (e.get("startsByQuarter") or {}).items():
            qs[k] = qs.get(k, 0) + (v or 0)
        for k, v in (e.get("closingsByQuarter") or {}).items():
            qc[k] = qc.get(k, 0) + (v or 0)

    def _qkey(lbl):
        try:
            y, q = lbl.split(" Q")
            return (int(y), int(q))
        except Exception:
            return (0, 0)
    quarter_series = [{"label": k, "starts": qs.get(k, 0), "closings": qc.get(k, 0)}
                      for k in sorted(set(qs) | set(qc), key=_qkey)]

    # --- 4b) Sales volume the ring currently supports -----------------------
    # Dollar absorption, not just unit absorption: annual closings priced at the
    # community's own midpoint. Priced off each community rather than one blended
    # average so a high-volume entry-level community doesn't get valued at a
    # move-up community's price. Communities with no CBAS pricing fall back to
    # the ring median so they aren't silently counted as $0.
    _mids = [(c["price_min"] + c["price_max"]) / 2.0
             for c in comms if c.get("price_min") and c.get("price_max")]
    _mids.sort()
    ring_median_price = _mids[len(_mids) // 2] if _mids else None
    volume_total = 0.0
    volume_priced = 0
    for c in comms:
        mid = ((c["price_min"] + c["price_max"]) / 2.0
               if c.get("price_min") and c.get("price_max") else ring_median_price)
        c["avg_price"] = round(mid) if mid else None
        c["annual_volume"] = round((c["annual_closings"] or 0) * mid) if mid else None
        if c["annual_volume"]:
            volume_total += c["annual_volume"]
            if c.get("price_min"):
                volume_priced += 1
    for b in builders:
        if b.get("avg_price") and b.get("est_annual_closings"):
            b["est_annual_volume"] = round(b["avg_price"] * b["est_annual_closings"])
        else:
            b["est_annual_volume"] = None

    # --- 5) Ring rollup ----------------------------------------------------
    active = [c for c in comms if (c["annual_closings"] or 0) > 0]
    tot_cl = sum(c["annual_closings"] or 0 for c in comms)
    tot_st = sum(c["annual_starts"] or 0 for c in comms)
    tot_vdl = sum(c["vdls"] or 0 for c in comms)
    tot_fut = sum(c["futures"] or 0 for c in comms)
    all_prices = [c["price_min"] for c in comms if c.get("price_min")] + \
                 [c["price_max"] for c in comms if c.get("price_max")]

    # --- 6) Market-entry read ----------------------------------------------
    # The numbers an underwriter would otherwise derive by hand: what pace one
    # community can expect, what product the market is actually absorbing, and
    # how contested the lot supply is. Every figure traces to the ring above —
    # this summarises, it does not forecast.
    dominant = max(lot_bands, key=lambda b: b["lots"] or 0) if lot_bands else None
    selling = [b for b in lot_bands if (b["plans"] or 0) >= 5]
    best_ppsf = max(selling, key=lambda b: b["avg_ppsf"] or 0) if selling else None
    mos = (round(tot_vdl / (tot_cl / 12.0), 1) if tot_cl else None)
    pace = round(tot_cl / len(active), 1) if active else None

    # Lot absorption is a STARTS question, not a closings question. A start is a
    # builder pulling a lot and breaking ground — that is when the lot sells. A
    # closing is the homebuyer taking delivery months later, which is the
    # builder's metric, not the land developer's.
    #
    # Rather than assume a capture rate, measure what communities in this ring
    # actually take: each active community's share of ring-wide annual starts.
    # That gives a median (a typical community), a p75 (a strong one) and a max
    # (what the best performer here proves is achievable).
    starting = [c for c in comms if (c["annual_starts"] or 0) > 0]
    shares = sorted(((c["annual_starts"] or 0) / tot_st * 100) for c in starting) if tot_st else []

    def _pct(lst, q):
        if not lst:
            return None
        i = min(len(lst) - 1, max(0, int(round(q * (len(lst) - 1)))))
        return round(lst[i], 2)

    top_starter = max(starting, key=lambda c: c["annual_starts"] or 0) if starting else None

    # You don't compete for every start — only for starts in the lot widths you
    # intend to build. Apportion ring starts by the share of ring lots sitting in
    # the project's own target bands to get an addressable figure. Capture of
    # that is the number worth arguing about; capture of all starts understates
    # it whenever the project targets part of the product range.
    # Same default mix /analyze applies when a project hasn't set one, so the
    # addressable figure works before anyone touches the yield inputs.
    _lot_types = ((proj.get("yield_assumptions") or {}).get("lot_types")
                  or [{"label": "40 FF"}, {"label": "50 FF"},
                      {"label": "60 FF"}, {"label": "70 FF"}])
    target_ff = []
    for lt in _lot_types:
        m = _sub_parse_re.search(r"\d+", str(lt.get("label") or ""))
        if m:
            ff = _cbas_ff(m.group())
            if ff:
                target_ff.append(ff)
    target_bands = {_cbas_band(f) for f in target_ff}
    target_bands.discard(None)
    band_lots_total = sum(b["lots"] or 0 for b in lot_bands) or 0
    band_lots_target = sum(b["lots"] or 0 for b in lot_bands if b["label"] in target_bands)
    seg_share = (band_lots_target / band_lots_total) if band_lots_total else None
    addressable = round(tot_st * seg_share) if (tot_st and seg_share) else None

    capture = {
        "ring_annual_starts": tot_st,
        "active_communities": len(starting),
        "share_median_pct": _pct(shares, 0.50),
        "share_p75_pct": _pct(shares, 0.75),
        "share_max_pct": _pct(shares, 1.0),
        "top_community": (top_starter or {}).get("name"),
        "top_community_starts": (top_starter or {}).get("annual_starts"),
        "target_bands": sorted(target_bands),
        "target_ff": sorted(set(target_ff)),
        "segment_share_pct": round(seg_share * 100, 1) if seg_share else None,
        "addressable_starts": addressable,
    }
    # Anchor the ladder to observed percentiles rather than round numbers, then
    # append 20% and 25% because those get asked for — so the answer sits next to
    # the ceiling this ring has actually demonstrated instead of floating free.
    _pts = []
    for pct, lbl in ((capture["share_median_pct"], "median community here"),
                     (capture["share_p75_pct"], "strong community here"),
                     (capture["share_max_pct"], f"best here — {capture['top_community'] or 'top performer'}"),
                     (20.0, "commonly assumed"),
                     (25.0, "commonly assumed")):
        if pct and not any(abs(pct - p["capture_pct"]) < 0.25 for p in _pts):
            _pts.append({"capture_pct": round(pct, 1), "label": lbl})
    _pts.sort(key=lambda p: p["capture_pct"])
    ceiling = capture["share_max_pct"]
    for p in _pts:
        p["lots_per_year"] = round(tot_st * p["capture_pct"] / 100.0) if tot_st else None
        p["addressable_lots_per_year"] = (round(addressable * p["capture_pct"] / 100.0)
                                          if addressable else None)
        p["above_ceiling"] = bool(ceiling and p["capture_pct"] > ceiling)
    capture["scenarios"] = _pts
    capture["base_capture_pct"] = capture["share_p75_pct"]
    top3 = builders[:3]
    top3_share = (round(sum(b["est_annual_starts"] or 0 for b in top3) / tot_st * 100, 1)
                  if tot_st else None)
    market_entry = {
        "capture": capture,
        "expected_pace_per_community": pace,
        "expected_pace_note": (
            f"{pace} closings/yr is the average across {len(active)} actively "
            f"closing communities in the submarket" if pace else None),
        "dominant_product": dominant["label"] if dominant else None,
        "dominant_product_lots": dominant["lots"] if dominant else None,
        "dominant_product_price": dominant["avg_price"] if dominant else None,
        "highest_ppsf_band": best_ppsf["label"] if best_ppsf else None,
        "highest_ppsf": best_ppsf["avg_ppsf"] if best_ppsf else None,
        "months_lot_supply": mos,
        "lot_market": ("tight" if mos is not None and mos < 6
                       else "balanced" if mos is not None and mos < 18
                       else "long" if mos is not None else None),
        "years_of_pipeline": round((tot_vdl + tot_fut) / tot_cl, 1) if tot_cl else None,
        "annual_sales_volume": round(volume_total) if volume_total else None,
        "top3_builders": [b["name"] for b in top3],
        "top3_start_share_pct": top3_share,
        "builder_concentration": ("concentrated" if top3_share and top3_share >= 50
                                  else "fragmented" if top3_share else None),
        "price_band": {"min": min(all_prices) if all_prices else None,
                       "max": max(all_prices) if all_prices else None,
                       "median": round(ring_median_price) if ring_median_price else None},
        "priced_communities": volume_priced,
        "unpriced_communities": len([c for c in comms if not c.get("price_min")]),
    }

    return jsonify({
        "market_entry":      market_entry,
        "annual_sales_volume": round(volume_total) if volume_total else None,
        "ring_median_price": round(ring_median_price) if ring_median_price else None,
        "quarter": latest,
        "quarter_label": quarter_series[-1]["label"] if quarter_series else None,
        "radius_mi": radius_mi,
        "center": {"lat": c_lat, "lon": c_lon},
        "district_name": dname,
        "district_names": dnames,
        "district_community_count": sum(1 for c in comms if c["in_district"]),
        "community_count": len(comms),
        "active_count": len(active),
        "builder_count": len(builders),
        "tracked_universe": len(entries),
        "aggregate": {
            "annual_starts": tot_st, "annual_closings": tot_cl,
            "vdls": tot_vdl, "futures": tot_fut,
            "total_lots": sum(c["total_lots"] or 0 for c in comms),
            "under_construction": sum(c["under_construction"] or 0 for c in comms),
            "occupied": sum(c["occupied"] or 0 for c in comms),
            "complete_vacant": sum(c["complete_vacant"] or 0 for c in comms),
            "models": sum(c["models"] or 0 for c in comms),
            "months_lot_supply": round(tot_vdl / (tot_cl / 12.0), 1) if tot_cl else None,
            "years_of_pipeline": round((tot_vdl + tot_fut) / tot_cl, 1) if tot_cl else None,
            "price_min": min(all_prices) if all_prices else None,
            "price_max": max(all_prices) if all_prices else None,
        },
        "submarket_annual_closings": tot_cl,
        "submarket_annual_starts": tot_st,
        "avg_annual_closings_per_community": round(tot_cl / len(active), 1) if active else None,
        "quarter_series": quarter_series,
        "lot_bands": lot_bands,
        "builders": builders[:30],
        "communities": comms[:80],
        "source": "CBAS new-home survey (cv-server.cbashome.com)",
    })


@acq_bp.route("/api/acq/projects/<pid>/cbas/search", methods=["GET"])
@_login_required
def acq_api_projects_cbas_search(pid):
    """Look up any CBAS community or builder, anywhere — not just inside the ring.

    The competition card only shows what falls within radius_mi, but the cached
    /getData payload holds every community CBAS tracks. This searches that whole
    universe by community name, builder, developer, city or county, and still
    reports each hit's distance from the project so an out-of-ring comparable
    stays in context.

    ?q=      search text (min 2 chars)
    ?kind=   community | builder | all   (default all)
    """
    import math as _m
    from shapely.geometry import shape as shp_shape
    from shapely.ops import unary_union

    token = os.environ.get("CBAS_TOKEN", "").strip()
    if not token:
        return jsonify({"error": "CBAS_TOKEN not configured on the server."}), 503

    q = (request.args.get("q") or "").strip()
    if len(q) < 2:
        return jsonify({"error": "Enter at least 2 characters to search."}), 400
    kind = (request.args.get("kind") or "all").lower()
    ql = q.lower()

    guard = _acq_guard()
    if guard:
        return guard
    uid = _acq_owner()
    conn = get_db()
    try:
        proj = acq_store.get_object(conn, "project", pid, uid, _acq_is_admin())
    finally:
        conn.close()
    if not proj:
        return jsonify({"error": "project not found"}), 404

    geoms = []
    for t in proj.get("tracts") or []:
        g = t.get("geometry")
        if g:
            try:
                geoms.append(shp_shape(g))
            except Exception:
                pass
    c_lat = c_lon = None
    if geoms:
        cen = unary_union(geoms).centroid
        c_lat, c_lon = cen.y, cen.x

    entries, latest, err = _cbas_get_data(token)
    if err:
        return jsonify({"error": err}), 502
    bmap = _cbas_builder_map(entries)

    def _dist(la, lo):
        if c_lat is None or la is None or lo is None:
            return None
        R = 3958.8
        p1, p2 = _m.radians(c_lat), _m.radians(la)
        dp, dl = _m.radians(la - c_lat), _m.radians(lo - c_lon)
        x = _m.sin(dp / 2) ** 2 + _m.cos(p1) * _m.cos(p2) * _m.sin(dl / 2) ** 2
        return round(2 * R * _m.asin(_m.sqrt(x)), 2)

    communities = []
    if kind in ("all", "community"):
        for e in entries:
            hay = " ".join(str(x or "") for x in (
                e.get("name"), e.get("public_name"), e.get("communityName"),
                e.get("developerName"), e.get("city"), e.get("countyName"),
                e.get("submarketName"), e.get("zip"))).lower()
            builders_blob = " ".join(str(b or "") for b in (e.get("builders_names") or [])).lower()
            if ql not in hay and ql not in builders_blob:
                continue
            la, lo = e.get("lat"), e.get("lng")
            prices = e.get("prices") or {}
            communities.append({
                "id": e.get("id"),
                "name": e.get("public_name") or e.get("name"),
                "developer": e.get("developerName"), "city": e.get("city"),
                "county": e.get("countyName"), "submarket": e.get("submarketName"),
                "status": e.get("status"), "lat": la, "lon": lo,
                "distance_mi": _dist(la, lo),
                "builders": [b for b in (e.get("builders_names") or []) if b and b != "Builder TBD"],
                "lot_types_ff": sorted({f for f in (_cbas_ff(x) for x in (e.get("lot_types") or []))
                                        if f is not None}),
                "price_min": prices.get("min"), "price_max": prices.get("max"),
                "annual_starts": e.get("annual_starts"), "annual_closings": e.get("annual_closings"),
                "starts_qtr": e.get("starts"), "closings_qtr": e.get("closings"),
                "vdls": e.get("vdls"), "futures": e.get("futures"),
                "total_lots": e.get("total"), "pct_built_out": e.get("buildoutPercent"),
                "school_district": e.get("districtsNames"),
                "link": e.get("link"),
            })
        communities.sort(key=lambda c: (c["distance_mi"] is None, c["distance_mi"] or 0))

    builders = []
    if kind in ("all", "builder"):
        matched_ids = {bid for bid, nm in bmap.items() if ql in (nm or "").lower()}
        agg = {}
        for e in entries:
            for bid in (e.get("builders") or []):
                if bid not in matched_ids:
                    continue
                a = agg.setdefault(bid, {"builder_id": bid, "name": bmap.get(bid),
                                         "communities": [], "lots": 0, "prices": [],
                                         "sqfts": [], "annual_starts": 0.0})
                d_mi = _dist(e.get("lat"), e.get("lng"))
                a["communities"].append({
                    "name": e.get("public_name") or e.get("name"),
                    "city": e.get("city"), "distance_mi": d_mi,
                    "status": e.get("status"),
                    "annual_closings": e.get("annual_closings"),
                    "lat": e.get("lat"), "lon": e.get("lng"),
                })
            for sec in (e.get("sections") or []):
                for lb in (sec.get("lot_types_builders") or []):
                    if lb.get("builder") in matched_ids:
                        agg.setdefault(lb["builder"], {"builder_id": lb["builder"],
                                                       "name": bmap.get(lb["builder"]),
                                                       "communities": [], "lots": 0,
                                                       "prices": [], "sqfts": [],
                                                       "annual_starts": 0.0})["lots"] += lb.get("num_lots") or 0
            for fp in ((e.get("latestFloorplanPricing") or {}).get("entries") or []):
                if fp.get("builderID") in matched_ids:
                    a = agg.setdefault(fp["builderID"], {"builder_id": fp["builderID"],
                                                         "name": fp.get("builderName"),
                                                         "communities": [], "lots": 0,
                                                         "prices": [], "sqfts": [],
                                                         "annual_starts": 0.0})
                    if fp.get("price"):
                        a["prices"].append(fp["price"])
                    if fp.get("sqft"):
                        a["sqfts"].append(fp["sqft"])
        for a in agg.values():
            pr, sf = a["prices"], a["sqfts"]
            a["communities"].sort(key=lambda c: (c["distance_mi"] is None, c["distance_mi"] or 0))
            builders.append({
                "builder_id": a["builder_id"], "name": a["name"],
                "community_count": len(a["communities"]), "lots": a["lots"],
                "avg_price": round(sum(pr) / len(pr)) if pr else None,
                "avg_sqft": round(sum(sf) / len(sf)) if sf else None,
                "avg_ppsf": round((sum(pr) / len(pr)) / (sum(sf) / len(sf)), 2)
                            if pr and sf and sum(sf) else None,
                "nearest_mi": a["communities"][0]["distance_mi"] if a["communities"] else None,
                "communities": a["communities"][:25],
            })
        builders.sort(key=lambda b: (b["nearest_mi"] is None, b["nearest_mi"] or 0))

    return jsonify({
        "query": q, "kind": kind,
        "center": {"lat": c_lat, "lon": c_lon} if c_lat is not None else None,
        "universe": len(entries),
        "community_count": len(communities),
        "builder_count": len(builders),
        "communities": communities[:60],
        "builders": builders[:25],
        "source": "CBAS new-home survey (cv-server.cbashome.com)",
    })


@acq_bp.route("/api/acq/projects/<pid>/roads", methods=["GET"])
@_login_required
def acq_api_projects_roads(pid):
    """Planned and programmed road projects near the project.

    Highway capacity is the thing that turns an outer-ring tract into a viable
    community, so knowing what TxDOT intends to build — and when — belongs in
    the acquisition package. Three TxDOT ArcGIS layers, verified live:

      Planned_Programmed_Projects_1/20  the planning layer. Carries PT_PHASE
          ("Construction underway or begins soon" / "begins in 5 to 10 years" /
          "Corridor Studies, construction in 10+ years") and LET_YEAR, which is
          exactly the horizon question.
      TxDOT_DCIS_All_Projects/0         the programmed project list, with
          EST_CONST_COST and let dates.
      TxDOT_Future_Texas_Toll_Roads/0   future toll corridors with YearToOpen.

    Query is by bounding box around the project (?radius_mi=, default 15) —
    these are line features, so an envelope is the right primitive.
    """
    import requests as _rq
    from shapely.geometry import shape as shp_shape
    from shapely.ops import unary_union
    from concurrent.futures import ThreadPoolExecutor as _RTP

    guard = _acq_guard()
    if guard:
        return guard
    uid = _acq_owner()
    conn = get_db()
    try:
        proj = acq_store.get_object(conn, "project", pid, uid, _acq_is_admin())
    finally:
        conn.close()
    if not proj:
        return jsonify({"error": "project not found"}), 404

    geoms = []
    for t in proj.get("tracts") or []:
        g = t.get("geometry")
        if g:
            try:
                geoms.append(shp_shape(g))
            except Exception:
                pass
    if not geoms:
        return jsonify({"error": "no tract geometries"}), 400
    centroid = unary_union(geoms).centroid
    c_lat, c_lon = centroid.y, centroid.x

    try:
        radius_mi = float(request.args.get("radius_mi", "15"))
    except (TypeError, ValueError):
        radius_mi = 15.0
    radius_mi = max(2.0, min(radius_mi, 40.0))
    deg = radius_mi / 69.0
    bbox = f"{c_lon-deg},{c_lat-deg},{c_lon+deg},{c_lat+deg}"

    ROOT = "https://services.arcgis.com/KTcxiTD9dsQw4r7Z/arcgis/rest/services"

    # 1500, not 300: the DCIS layer returns 433 features in a 15-mi box around
    # the test tract, so the old cap silently dropped a third of them — and the
    # dropped set is not random, it's whatever the service happened to order
    # last. A truncated list reads as "this is everything" when it isn't.
    def _q(svc, layer, fields, want_geom=True):
        try:
            r = _rq.get(f"{ROOT}/{svc}/FeatureServer/{layer}/query",
                        params={"geometry": bbox, "geometryType": "esriGeometryEnvelope",
                                "inSR": "4326", "spatialRel": "esriSpatialRelIntersects",
                                "outFields": fields, "returnGeometry": str(want_geom).lower(),
                                "outSR": "4326", "resultRecordCount": 1500, "f": "geojson"},
                        timeout=60,
                        headers={"User-Agent": "EmberAcquisitionsGIS/1.0"})
            d = r.json()
            return d.get("features") or []
        except Exception:
            return []

    with _RTP(max_workers=3) as pool:
        f_plan = pool.submit(_q, "Planned_Programmed_Projects_1", 20,
                             "ROADWAY,TYPE_OF_WORK,LIMITS_FROM,LIMITS_TO,DESCRIPTION,"
                             "PROJ_LENGTH,PT_PHASE,LET_YEAR,CSJ,Source")
        f_dcis = pool.submit(_q, "TxDOT_DCIS_All_Projects", 0,
                             "HIGHWAY_NUMBER,PROJ_CLASS,TYPE_OF_WORK,LIMITS_FROM,LIMITS_TO,"
                             "PROJ_LENGTH,EST_CONST_COST,DIST_LET_DATE,ACTUAL_LET_DATE,"
                             "COUNTY_NAME,CONTROL_SECT_JOB")
        f_toll = pool.submit(_q, "TxDOT_Future_Texas_Toll_Roads", 0,
                             "TOLL_NM,OPERATOR,TOLL_STAT,YearToOpen,YearOpen,CMNT,URL")
        plan_f, dcis_f, toll_f = f_plan.result(), f_dcis.result(), f_toll.result()

    # Rank by LET_YEAR first, PT_PHASE only as a fallback. PT_PHASE is stale in
    # this layer — projects with a 2018 let date still read "Construction
    # underway or begins soon", which would put finished work at the top of a
    # list whose whole purpose is what is still coming.
    import datetime as _dt2
    _now_yr = _dt2.date.today().year

    def _horizon(phase, let_year):
        if let_year:
            yrs = let_year - _now_yr
            if yrs < -1:
                return (5, f"Let {let_year} — likely complete")
            if yrs <= 1:
                return (0, "Underway / imminent")
            if yrs <= 4:
                return (1, "1–4 years")
            if yrs <= 10:
                return (2, "5–10 years")
            return (3, "10+ years")
        p = (phase or "").lower()
        if "underway" in p or "begins soon" in p:
            return (0, "Underway / imminent")
        if "5 to 10" in p:
            return (2, "5–10 years")
        if "10+" in p or "corridor stud" in p:
            return (3, "10+ years / study")
        if "1 to 4" in p or "1 to 5" in p or "4 years" in p:
            return (1, "1–4 years")
        return (4, "Unscheduled")

    # Capacity work moves land value; overlays, seal coats, striping, signal
    # upgrades and safety lighting do not. Of the 48 scheduled projects around
    # the test tract only a handful add lanes or build an interchange, so this
    # flag is what lets the card lead with the ones that matter instead of
    # burying them in resurfacing.
    CAPACITY = ("widen", "interchange", "new location", "constr mainlanes",
                "overpass", "grade separation", "expand", "add lanes",
                "added capacity", "freeway", "high occupancy", "hov",
                "managed lane", "toll lane")

    planned = []
    for ft in plan_f:
        a = ft.get("properties") or {}
        rank, label = _horizon(a.get("PT_PHASE"), a.get("LET_YEAR"))
        _blob = f"{a.get('TYPE_OF_WORK','')} {a.get('DESCRIPTION','')}".lower()
        planned.append({
            "capacity": any(k in _blob for k in CAPACITY),
            "roadway": a.get("ROADWAY"), "work": a.get("TYPE_OF_WORK"),
            "description": a.get("DESCRIPTION"),
            "from": a.get("LIMITS_FROM"), "to": a.get("LIMITS_TO"),
            "length_mi": a.get("PROJ_LENGTH"), "phase": a.get("PT_PHASE"),
            "let_year": a.get("LET_YEAR"), "csj": a.get("CSJ"),
            "horizon": label, "horizon_rank": rank,
            "geometry": ft.get("geometry"),
        })
    planned.sort(key=lambda p: (p["horizon_rank"], p["let_year"] or 9999))

    def _ms_to_year(v):
        if not v:
            return None
        try:
            import datetime as _d
            return _d.datetime.utcfromtimestamp(v / 1000.0).year
        except Exception:
            return None

    programmed = []
    for ft in dcis_f:
        a = ft.get("properties") or {}
        cost = a.get("EST_CONST_COST")
        programmed.append({
            "highway": a.get("HIGHWAY_NUMBER"), "klass": a.get("PROJ_CLASS"),
            "work": a.get("TYPE_OF_WORK"), "from": a.get("LIMITS_FROM"),
            "to": a.get("LIMITS_TO"), "length_mi": a.get("PROJ_LENGTH"),
            "cost": cost, "county": a.get("COUNTY_NAME"),
            "let_year": _ms_to_year(a.get("DIST_LET_DATE")) or _ms_to_year(a.get("ACTUAL_LET_DATE")),
            "csj": a.get("CONTROL_SECT_JOB"),
            "geometry": ft.get("geometry"),
        })
    for p in programmed:
        blob = f"{p['klass']} {p['work']}".lower()
        p["capacity"] = any(k in blob for k in CAPACITY)
    programmed.sort(key=lambda p: (not p["capacity"], p["let_year"] or 9999))

    tolls = []
    for ft in toll_f:
        a = ft.get("properties") or {}
        tolls.append({
            "name": a.get("TOLL_NM"), "operator": a.get("OPERATOR"),
            "status": a.get("TOLL_STAT"),
            "year_open": a.get("YearOpen") or a.get("YearToOpen"),
            "note": a.get("CMNT"), "url": a.get("URL"),
            "geometry": ft.get("geometry"),
        })
    tolls = [t for t in tolls if (t["status"] or "").lower() != "existing"]

    near_term = [p for p in planned if p["horizon_rank"] <= 1]
    upcoming = [p for p in planned if p["horizon_rank"] <= 3]
    cap = [p for p in programmed if p["capacity"]]

    # The two layers are complementary rather than redundant, and it's worth
    # measuring by how much. Matching on CSJ (TxDOT's unique project key) over a
    # 15-mi box at the test tract: 35 of 39 scheduled projects also appear in
    # DCIS, but DCIS holds 395 the scheduled layer never lists — including 110
    # capacity projects, among them the US 290 mainlane widening and the
    # Becker / Bauer / Mueschke interchanges. Conversely DCIS publishes no let
    # date or cost at all (0 of 433 populated), so it can say what is programmed
    # but never when. Timing comes from one, coverage from the other.
    def _csj(v):
        return "".join(ch for ch in str(v or "") if ch.isdigit())
    sched_csj = {_csj(p.get("csj")) for p in planned if _csj(p.get("csj"))}
    cap_scheduled = sum(1 for p in cap if _csj(p.get("csj")) in sched_csj)

    # Roll the programmed-capacity list up by roadway server-side. The response
    # truncates `programmed`, so counting it client-side reported 60 against a
    # headline of 121 — the summary has to be built over the full set.
    _by_road = {}
    for p in cap:
        _by_road[p["highway"] or "—"] = _by_road.get(p["highway"] or "—", 0) + 1

    return jsonify({
        "capacity_by_roadway": sorted(_by_road.items(), key=lambda kv: -kv[1]),
        "upcoming_count": len(upcoming),
        "center": {"lat": c_lat, "lon": c_lon},
        "radius_mi": radius_mi,
        "planned": planned[:60],
        "programmed": programmed[:60],
        "future_tolls": tolls[:20],
        "counts": {"planned": len(planned), "programmed": len(programmed),
                   "future_tolls": len(tolls), "near_term": len(near_term),
                   "capacity": len(cap), "capacity_scheduled": cap_scheduled,
                   "capacity_unscheduled": len(cap) - cap_scheduled},
        "capacity_spend": sum(p["cost"] or 0 for p in cap) or None,
        "source": "TxDOT (Planned & Programmed Projects, DCIS, Future Toll Roads)",
    })


@acq_bp.route("/api/acq/school-district/<geoid>", methods=["GET"])
@_login_required
def acq_api_school_district_profile(geoid):
    """District profile: enrollment, schools roster (with distance from the
    project), staffing, growth trend, and rating links per school.

    Source: Urban Institute Education Data API (NCES Common Core of Data) —
    free, no key, verified live. LEAID == the Census GEOID for the district.

    Optional ?lat=&lon= — when supplied, each school gets distance_mi +
    compass direction from that point, and the roster sorts by distance.
    """
    import requests as _rq
    import math as _math
    from concurrent.futures import ThreadPoolExecutor as _TPE

    leaid = (geoid or "").strip()
    if not leaid.isdigit():
        return jsonify({"error": "bad district id"}), 400
    district_name_hint = (request.args.get("name") or "").strip()

    # Optional reference point (the project centroid) for distances
    try:
        ref_lat = float(request.args.get("lat")) if request.args.get("lat") else None
        ref_lon = float(request.args.get("lon")) if request.args.get("lon") else None
    except (TypeError, ValueError):
        ref_lat = ref_lon = None

    B = "https://educationdata.urban.org/api/v1"

    def _get(url, params=None):
        try:
            r = _rq.get(url, params=params or {}, timeout=20)
            if r.status_code != 200: return None
            return r.json()
        except Exception:
            return None

    LATEST = 2024                  # NCES CCD directory has data through 2024
    SCHOOL_ROSTER_YEAR = 2024      # 2024 roster: verified 12 campuses, all with lat/lon
    HISTORY_START = 1990           # verified: CCD directory goes back to 1990

    def _directory(year):
        d = _get(f"{B}/school-districts/ccd/directory/{year}/", {"leaid": leaid})
        res = (d or {}).get("results") or []
        return res[0] if res else None

    def _schools(year):
        d = _get(f"{B}/schools/ccd/directory/{year}/", {"leaid": leaid})
        return (d or {}).get("results") or []

    # Fetch the two REQUIRED calls first, on their own. Bundling them into a
    # 36-request burst got them throttled by the Urban Institute API, which is
    # why the campus roster came back empty. History is best-effort after.
    directory = _directory(LATEST)
    schools   = _schools(SCHOOL_ROSTER_YEAR)
    if not schools:                       # roster fallback if that year is thin
        for yr in (SCHOOL_ROSTER_YEAR - 1, SCHOOL_ROSTER_YEAR - 2):
            schools = _schools(yr)
            if schools: break

    # Fall back to an earlier year if the latest directory row is missing.
    # If NCES does not know the LEAID at all, do not fan out into 34 history
    # calls; return a partial TEA-only profile quickly.
    trend_raw = {}
    if not directory:
        for fallback_yr in (LATEST - 1, LATEST - 2):
            rec = _directory(fallback_yr)
            trend_raw[fallback_yr] = rec
            if rec:
                directory = rec
                break

    # Now the long enrollment history — modest concurrency so we don't trip
    # rate limits. 34 small requests at 6 workers ≈ 5s.
    if directory:
        with _TPE(max_workers=6) as pool:
            f_trend = {yr: pool.submit(_directory, yr)
                        for yr in range(HISTORY_START, LATEST)}
            trend_raw.update({yr: fut.result() for yr, fut in f_trend.items()})

    # ---- Official TEA A–F accountability ratings -------------------------
    # data.texas.gov Socrata dataset nui6-x374 = "School Year 2024-2025
    # Statewide Accountability Ratings" — 10,292 rows covering every Texas
    # district AND campus. Free, no key. Verified live.
    # Join key: TEA district name (NCES lea_name matches closely).
    TEA_URL = "https://data.texas.gov/resource/nui6-x374.json"
    tea_district = None
    tea_campuses = []
    try:
        nces_name = ((directory or {}).get("lea_name") or district_name_hint).strip().upper()
        # Try exact name match first, then a LIKE on the leading word(s).
        rows = _get(TEA_URL, {"district": nces_name, "$limit": 60}) or []
        if not rows and nces_name:
            stem = nces_name.replace(" ISD", "").strip()
            if stem:
                rows = _get(TEA_URL, {
                    "$where": f"upper(district) like '{stem[:24]}%'",
                    "$limit": 60,
                }) or []
        for row in rows:
            if row.get("school_type") == "District":
                tea_district = row
            else:
                tea_campuses.append(row)
    except Exception:
        pass

    def _rating_block(row):
        if not row: return None
        def _num(v):
            try: return float(v)
            except (TypeError, ValueError): return None
        def _pct(v):
            n = _num(v)
            return round(n * 100, 1) if n is not None else None
        return {
            "overall_rating":  row.get("overall_rating"),
            "overall_score":   _num(row.get("overall_score")),
            "student_achievement": {"rating": row.get("student_achievement_rating"),
                                     "score": _num(row.get("student_achievement_score"))},
            "school_progress":     {"rating": row.get("school_progress_rating"),
                                     "score": _num(row.get("school_progress_score"))},
            "closing_the_gaps":    {"rating": row.get("closing_the_gaps_rating"),
                                     "score": _num(row.get("closing_the_gaps_score"))},
            "students":            _num(row.get("number_of_students")),
            "econ_disadvantaged_pct": _pct(row.get("economically_disadvantaged")),
            "el_students_pct":     _pct(row.get("eb_el_students")),
            "region":              row.get("county"),
        }

    # Index TEA campus ratings by a normalized name for joining to NCES schools
    def _norm(n):
        return "".join(ch for ch in (n or "").upper() if ch.isalnum())
    tea_by_name = {}
    for row in tea_campuses:
        key = _norm(row.get("campus"))
        if key:
            tea_by_name[key] = row

    # ---- Current enrolment from TEA, which runs ~1 year ahead of NCES --------
    # NCES CCD stops at 2024 (the Urban Institute API returns count=0 for 2025),
    # so the national feed can only ever be that current. TEA's AskTED directory
    # is refreshed continuously — May 2026 at time of writing — and carries both
    # district and campus enrolment plus an nces_district_id to join on. For
    # Waller ISD that is 10,869 against NCES's 9,905, a year of growth the
    # national dataset has not published yet.
    #
    # NCES still supplies the 1990-2024 history; AskTED cannot, being a snapshot.
    # So: long trend from NCES, current figure from TEA.
    askted = []
    tea_current = None
    tea_as_of = None
    try:
        _at = _get("https://data.texas.gov/resource/hzek-udky.json",
                   {"nces_district_id": f"'{leaid}'", "$limit": 200})
        if not _at:      # the id column is stored with a leading apostrophe
            _at = _get("https://data.texas.gov/resource/hzek-udky.json",
                       {"$where": f"nces_district_id like '%{leaid}'", "$limit": 200})
        askted = _at or []
        if askted:
            try:
                tea_current = int(float(askted[0].get("district_enrollment_as_of")))
            except (TypeError, ValueError):
                tea_current = None
            tea_as_of = (askted[0].get("update_date") or "")[:10]
    except Exception:
        askted = []

    # Campuses TEA lists as not yet open — forward capacity the district is
    # building, which is a demand signal NCES has no field for.
    pipeline_campuses = [
        {"name": (c.get("school_name") or "").title(),
         "status": c.get("school_status"),
         "grades": (c.get("grade_range") or "").lstrip("'")}
        for c in askted
        if (c.get("school_status") or "").upper() not in ("ACTIVE", "")
    ]

    # Full enrollment history (1990 → latest) + growth stats over multiple windows
    trend = []
    for yr in sorted(trend_raw):
        rec = trend_raw[yr]
        if rec and rec.get("enrollment") not in (None, -1, -2):
            trend.append({"year": yr, "enrollment": rec["enrollment"]})
    cur_enroll = directory.get("enrollment") if directory else None
    if cur_enroll not in (None, -1, -2):
        trend.append({"year": LATEST, "enrollment": cur_enroll})
    # Extend the series with TEA's current count so the chart ends on the newest
    # real number rather than a two-year-old one. Flagged as a different source.
    if tea_current and tea_as_of:
        _tea_yr = int(tea_as_of[:4])
        if not trend or _tea_yr > trend[-1]["year"]:
            trend.append({"year": _tea_yr, "enrollment": tea_current, "source": "TEA"})
        cur_enroll = tea_current

    def _window(years_back):
        """Growth over the last N years of the series."""
        if len(trend) < 2: return None
        last = trend[-1]
        target_year = last["year"] - years_back
        # nearest available year at/after the target
        start = next((t for t in trend if t["year"] >= target_year), trend[0])
        span = last["year"] - start["year"]
        if span <= 0 or not start["enrollment"]: return None
        ratio = last["enrollment"] / start["enrollment"]
        return {
            "from_year":      start["year"],
            "to_year":        last["year"],
            "span_years":     span,
            "from_enrollment": start["enrollment"],
            "to_enrollment":   last["enrollment"],
            "total_pct":      round((ratio - 1) * 100, 1),
            "cagr_pct":       round((ratio ** (1/span) - 1) * 100, 2),
            "added_students": last["enrollment"] - start["enrollment"],
        }

    growth = {}
    full = _window(999)          # entire available history
    if full:
        growth.update(full)      # keep flat keys for back-compat with the UI
        growth["windows"] = {
            "all_time": full,
            "20_year":  _window(20),
            "10_year":  _window(10),
            "5_year":   _window(5),
        }

    # School roster — level, enrollment, distance from the project
    def _level(s):
        hi = s.get("highest_grade_offered")
        try: hi_i = int(hi)
        except (TypeError, ValueError): hi_i = None
        if hi_i is None: return "Other"
        if hi_i <= 5:  return "Elementary"
        if hi_i <= 8:  return "Middle / Jr High"
        return "High School"

    def _grade_label(g):
        """NCES grade codes: -1 = PK, 0 = K, 1..12 = grade number."""
        try: gi = int(g)
        except (TypeError, ValueError): return "?"
        if gi == -1: return "PK"
        if gi == 0:  return "K"
        return str(gi)

    def _dist_dir(lat, lon):
        if ref_lat is None or ref_lon is None or lat is None or lon is None:
            return None, None
        R = 3958.7613
        p1, p2 = _math.radians(ref_lat), _math.radians(lat)
        dl = _math.radians(lon - ref_lon)
        a = (_math.sin((p2-p1)/2)**2
             + _math.cos(p1)*_math.cos(p2)*_math.sin(dl/2)**2)
        dist = round(2 * R * _math.asin(_math.sqrt(a)), 2)
        deg = (_math.degrees(_math.atan2(lon - ref_lon, lat - ref_lat)) + 360) % 360
        dirs = ["N","NE","E","SE","S","SW","W","NW"]
        return dist, dirs[int((deg + 22.5) / 45) % 8]

    roster = []
    for s in schools:
        enr = s.get("enrollment")
        lat = s.get("latitude"); lon = s.get("longitude")
        # NCES uses -1/-2 as missing-value sentinels
        if lat in (-1, -2): lat = None
        if lon in (-1, -2): lon = None
        dist, direction = _dist_dir(lat, lon)
        nces_id = s.get("ncessch") or ""
        nm = (s.get("school_name") or "").title()
        # Join to the official TEA A–F rating by normalized campus name
        tea_row = tea_by_name.get(_norm(s.get("school_name")))
        tea = _rating_block(tea_row)
        roster.append({
            "name":        nm,
            "level":       _level(s),
            "enrollment":  enr if enr not in (None, -1, -2) else None,
            "grades":      f"{_grade_label(s.get('lowest_grade_offered'))}–{_grade_label(s.get('highest_grade_offered'))}",
            "charter":     bool(s.get("charter")),
            "lat": lat, "lon": lon,
            "distance_mi": dist,
            "direction":   direction,
            # Official TEA A–F rating (inline, not a link)
            "tea_rating":  (tea or {}).get("overall_rating"),
            "tea_score":   (tea or {}).get("overall_score"),
            "tea_detail":  tea,
            # No external rating link: txschools.gov 404s every
            # /schools/<id>/overview form — NCES id and TEA campus_number alike.
            # The A–F rating below is the real payload anyway.
        })
    # Distance-first when we have a reference point, else biggest schools first
    if ref_lat is not None:
        roster.sort(key=lambda x: (x["distance_mi"] if x["distance_mi"] is not None else 9999))
    else:
        roster.sort(key=lambda x: -(x["enrollment"] or 0))

    teachers = (directory or {}).get("teachers_total_fte")
    ratio = None
    if teachers and cur_enroll and teachers > 0:
        ratio = round(cur_enroll / teachers, 1)
    nces_missing = not bool(directory)
    warnings = []
    if nces_missing:
        warnings.append(f"No NCES CCD directory record was found for district {leaid}; showing TEA data where available.")

    return jsonify({
        "leaid":            leaid,
        "name":             (((directory or {}).get("lea_name") or district_name_hint or f"District {leaid}")).title(),
        "year":             LATEST,
        "nces_available":   not nces_missing,
        "partial":          nces_missing,
        "warnings":         warnings,
        "enrollment":       cur_enroll if cur_enroll not in (-1, -2) else None,
        "enrollment_source": "TEA AskTED" if tea_current else (f"NCES CCD {LATEST}" if directory else "Unavailable"),
        "enrollment_as_of": tea_as_of,
        "nces_enrollment":  (directory or {}).get("enrollment"),
        "nces_year":        LATEST if directory else None,
        "pipeline_campuses": pipeline_campuses,
        "schools_count":    (directory or {}).get("number_of_schools") if directory else (len(schools) or None),
        "teachers_fte":     teachers if teachers not in (-1, -2) else None,
        "student_teacher_ratio": ratio,
        "county":           (directory or {}).get("county_name"),
        "city":             ((directory or {}).get("city_mailing") or "").title(),
        "phone":            (directory or {}).get("phone"),
        "enrollment_trend": trend,
        "growth":           growth,
        "schools":          roster,
        # Official TEA A–F district rating (inline)
        "tea":              _rating_block(tea_district),
        "tea_year":         "2024-2025",
        "source":           ("NCES Common Core of Data (Urban Institute API) for the 1990-"
                             f"{LATEST} history + TEA AskTED for current enrolment + "
                             "TEA 2024-25 Accountability Ratings (data.texas.gov)"),
    })


@acq_bp.route("/api/acq/projects/<pid>/builders", methods=["GET"])
@_login_required
def acq_api_projects_builders(pid):
    """Detect active homebuilders in the submarket by scanning cached parcel
    OWNER_NAME fields for known builder company patterns. When a builder owns
    N+ parcels in an area, that's a strong signal they're actively developing.

    Also groups by community/subdivision so you can see who's building where.
    Returns top builders by parcel count + a per-subdivision cross-reference.
    """
    import re as _re
    from shapely.geometry import shape as shp_shape
    from shapely.ops import unary_union, transform

    guard = _acq_guard()
    if guard:
        return guard
    uid = _acq_owner()
    conn = get_db()
    try:
        proj = acq_store.get_object(conn, "project", pid, uid, _acq_is_admin())
    finally:
        conn.close()
    if not proj:
        return jsonify({"error": "project not found"}), 404

    geoms = []
    for t in proj.get("tracts") or []:
        g = t.get("geometry")
        if g:
            try: geoms.append(shp_shape(g))
            except Exception as e: print(f"[{request.endpoint}] tract geometry skipped: {e}", flush=True)
    if not geoms:
        return jsonify({"error": "no tract geometries"}), 400
    proj_union = unary_union(geoms)
    centroid = proj_union.centroid

    try:
        radius_mi = float(request.args.get("radius_mi", "5"))
    except (TypeError, ValueError):
        radius_mi = 5.0
    radius_mi = max(0.5, min(radius_mi, 15.0))
    centroid_utm = transform(to_utm, centroid)
    circle_utm = centroid_utm.buffer(radius_mi * 1609.34)
    circle_wgs = transform(to_wgs, circle_utm)

    import acq_parcels as parcel_cache
    try:
        fc = parcel_cache.query_parcels_in_polygon(circle_wgs, min_acres=0.05, max_acres=50)
        feats = fc.get("features") or []
    except Exception as e:
        return jsonify({"error": f"parcel cache query failed: {e}"}), 500

    builders_all = {}    # builder_name -> {parcels, acres, subdivisions:set, sample_owners:set}
    builders_by_sub = {} # subdivision -> {builder_name: parcel_count}

    for f in feats:
        p = f.get("properties") or {}
        owner = (p.get("OWNER_NAME") or "").strip()
        builder = _classify_builder(owner)   # module-level shared classifier
        if not builder: continue

        # Shared subdivision parser — rejects rural abstracts, strips CAD codes
        sub = _parse_subdivision_name(p.get("LEGAL_DESC") or "")

        # Distance from project centroid
        try:
            pcent = shp_shape(f["geometry"]).centroid
            dist_mi = transform(to_utm, pcent).distance(centroid_utm) / 1609.34
        except Exception:
            dist_mi = None
        acres = float(p.get("Acres") or 0)

        entry = builders_all.setdefault(builder, {
            "name":          builder,
            "parcels":       0,
            "acres":         0.0,
            "subdivisions":  set(),
            "sample_owners": set(),
            "closest_mi":    None,
        })
        entry["parcels"] += 1
        entry["acres"] += acres
        if sub: entry["subdivisions"].add(sub)
        if len(entry["sample_owners"]) < 5:
            entry["sample_owners"].add(owner)
        if dist_mi is not None and (entry["closest_mi"] is None or dist_mi < entry["closest_mi"]):
            entry["closest_mi"] = dist_mi

        if sub:
            builders_by_sub.setdefault(sub, {})
            builders_by_sub[sub][builder] = builders_by_sub[sub].get(builder, 0) + 1

    builders_out = []
    for v in builders_all.values():
        builders_out.append({
            "name":          v["name"],
            "parcels":       v["parcels"],
            "acres":         round(v["acres"], 1),
            "subdivisions":  sorted(v["subdivisions"])[:15],
            "sub_count":     len(v["subdivisions"]),
            "sample_owners": sorted(v["sample_owners"]),
            "closest_mi":    round(v["closest_mi"], 2) if v["closest_mi"] is not None else None,
        })
    builders_out.sort(key=lambda x: x["parcels"], reverse=True)

    # Also produce a per-subdivision → dominant builder table
    sub_dominant = []
    for sub, blist in builders_by_sub.items():
        if not blist: continue
        top_builder = max(blist.items(), key=lambda kv: kv[1])
        sub_dominant.append({
            "subdivision":    sub,
            "top_builder":    top_builder[0],
            "top_count":      top_builder[1],
            "other_builders": [b for b, _ in sorted(blist.items(), key=lambda kv: kv[1], reverse=True)[1:5]],
        })
    sub_dominant.sort(key=lambda x: x["top_count"], reverse=True)

    return jsonify({
        "builders":         builders_out[:40],
        "by_subdivision":   sub_dominant[:40],
        "radius_mi":        radius_mi,
        "parcels_scanned":  len(feats),
        "notes":            [
            "Builders identified by matching OWNER_NAME against known Texas/Houston homebuilder patterns.",
            "A builder holding N+ parcels in a subdivision usually means they're actively developing / selling.",
            "Some parcels are held by the buyer, not the builder — so this UNDERSTATES builder activity in mature communities.",
            "For live builder pricing / absorption by community, need Zonda subscription.",
        ],
    })


@acq_bp.route("/api/acq/projects/<pid>/news", methods=["GET"])
@_login_required
def acq_api_projects_news(pid):
    """Pull recent news stories relevant to the project's submarket — economic
    development, major employer moves, infrastructure, jobs announcements.

    Uses Google News RSS (free, no key). Builds queries from the project's
    city / school district / county / MSA.
    """
    import requests as _rq
    import re as _re
    import xml.etree.ElementTree as _ET
    from shapely.geometry import shape as shp_shape
    from shapely.ops import unary_union

    guard = _acq_guard()
    if guard:
        return guard
    uid = _acq_owner()
    conn = get_db()
    try:
        proj = acq_store.get_object(conn, "project", pid, uid, _acq_is_admin())
    finally:
        conn.close()
    if not proj:
        return jsonify({"error": "project not found"}), 404

    # Build SUBMARKET-SPECIFIC search terms. Key insight from testing: the City
    # of Houston's boundary snakes up utility corridors 30+ miles from downtown,
    # so a rural Hockley project "contains" Houston — which floods the news with
    # metro-wide stories. So: EXCLUDE Houston, and instead pull the actual small
    # towns near the project from TIGERweb Places within a ~10-mi bbox.
    # TWO search terms only, per acquisitions feedback: the CITY the project is
    # in and its SCHOOL DISTRICT. Pulling every nearby town (Pine Island, Prairie
    # View, ...) produced irrelevant noise. Houston is excluded — its boundary
    # snakes up utility corridors so rural sites falsely "contain" it.
    _TERM_BLOCKLIST = {"houston", "houston city", "houston texas"}
    terms = []            # ordered: city first, then district

    geoms = []
    for t in proj.get("tracts") or []:
        g = t.get("geometry")
        if g:
            try: geoms.append(shp_shape(g))
            except Exception as e: print(f"[{request.endpoint}] tract geometry skipped: {e}", flush=True)
    if geoms:
        proj_union = unary_union(geoms)
        centroid = proj_union.centroid
        c_lon, c_lat = centroid.x, centroid.y

        # TIGERweb point-in-polygon. The Census *geocoder* silently omits school
        # districts and places for many rural coordinates (verified), so query
        # the TIGERweb layers directly — those return correctly.
        TIGER = "https://tigerweb.geo.census.gov/arcgis/rest/services/TIGERweb"
        def _tiger(service, layer, geometry, geom_type):
            try:
                r = _rq.get(f"{TIGER}/{service}/MapServer/{layer}/query",
                            params={"geometry": geometry,
                                    "geometryType": geom_type,
                                    "inSR": "4326",
                                    "spatialRel": "esriSpatialRelIntersects",
                                    "outFields": "NAME,GEOID",
                                    "returnGeometry": "false",
                                    "f": "json"},
                            timeout=12)
                d = r.json()
                if d.get("error"): return []
                return [f.get("attributes") or {} for f in (d.get("features") or [])]
            except Exception:
                return []

        pt = f"{c_lon},{c_lat}"
        PLACES = "Places_CouSub_ConCity_SubMCD"

        # 1) City — point-in-polygon first (incorporated, then CDP). Rural sites
        #    are usually unincorporated, so fall back to the NEAREST town within
        #    ~8 mi, skipping Houston (its limits snake far up the corridors).
        city = None
        for layer in (4, 5):        # 4 = Incorporated Places, 5 = CDPs
            for a in _tiger(PLACES, layer, pt, "esriGeometryPoint"):
                nm = _re.sub(r"\s+(city|town|village|CDP)$", "",
                             (a.get("NAME") or "").strip(), flags=_re.I).strip()
                if nm and nm.lower() not in _TERM_BLOCKLIST:
                    city = nm
                    break
            if city: break
        if not city:
            deg = 8 / 69.0
            bbox = f"{c_lon-deg},{c_lat-deg},{c_lon+deg},{c_lat+deg}"
            candidates = []
            for layer in (4, 5):
                for a in _tiger(PLACES, layer, bbox, "esriGeometryEnvelope"):
                    nm = _re.sub(r"\s+(city|town|village|CDP)$", "",
                                 (a.get("NAME") or "").strip(), flags=_re.I).strip()
                    if nm and nm.lower() not in _TERM_BLOCKLIST:
                        candidates.append(nm)
            # Prefer the shortest name — proxies for the small local town over
            # sprawling multi-word entities, and Houston is already excluded.
            if candidates:
                city = sorted(set(candidates), key=len)[0]
        if city:
            terms.append(city + " Texas")

        # 2) School districts — ALL of them, not just the centroid's. A tract
        #    straddling a boundary sits in two markets and both matter; query the
        #    project bbox across Unified / Secondary / Elementary layers.
        _dg = 0.02
        _dbox = f"{c_lon-_dg},{c_lat-_dg},{c_lon+_dg},{c_lat+_dg}"
        try:
            _b = proj_union.bounds
            _dbox = f"{_b[0]},{_b[1]},{_b[2]},{_b[3]}"
        except Exception:
            pass
        _seen_d = set()
        for _layer in (0, 1, 2):
            for a in _tiger("School", _layer, _dbox, "esriGeometryEnvelope"):
                nm = (a.get("NAME") or "").strip()
                if nm and nm not in _seen_d:
                    _seen_d.add(nm)
                    terms.append(nm.replace(" Independent School District", " ISD"))

    # Fallback — county, only if the geocoder gave us nothing usable
    if not terms:
        for t in proj.get("tracts") or []:
            if t.get("county"):
                terms.append(f"{t['county']} County Texas")
                break

    # Economic-development keywords — universal across any Texas submarket.
    # Grouped with OR so each query casts a wide net without going off-topic.
    QUERY_TEMPLATES = [
        '"{term}" (economic development OR new jobs OR expansion)',
        '"{term}" (manufacturing OR distribution center OR data center OR industrial park)',
        '"{term}" (new development OR master planned community OR homebuilder OR subdivision)',
        '"{term}" (infrastructure OR highway OR road expansion OR utility OR water district)',
        '"{term}" (population growth OR fastest growing OR relocation)',
    ]
    # A free-text ?q= overrides the derived terms entirely — it's used by the
    # search box on the card so you can chase a specific story (a named employer,
    # a corridor, a competitor) without being limited to city + district.
    custom_q = (request.args.get("q") or "").strip()
    if custom_q:
        terms = [custom_q]
        queries = [custom_q]
    else:
        queries = []
        # City plus every school district the tract touches. Capped so a tract
        # spanning three districts doesn't fan out into 20 RSS fetches.
        for term in terms[:4]:
            for tpl in QUERY_TEMPLATES:
                queries.append(tpl.format(term=term))

    def fetch_google_news(q):
        try:
            url = "https://news.google.com/rss/search"
            r = _rq.get(url, params={"q": q, "hl": "en-US", "gl": "US", "ceid": "US:en"},
                        timeout=10, headers={"User-Agent": "Mozilla/5.0"})
            if r.status_code != 200: return []
            root = _ET.fromstring(r.text)
            items = []
            for item in root.iter("item"):
                title = item.findtext("title") or ""
                link = item.findtext("link") or ""
                pub = item.findtext("pubDate") or ""
                # source is inside a <source> element in Google News
                src_el = item.find("source")
                src = src_el.text if src_el is not None else ""
                items.append({"title": title, "link": link, "published": pub, "source": src,
                              "query": q})
            return items[:6]   # cap per query
        except Exception:
            return []

    from concurrent.futures import ThreadPoolExecutor as _TPE
    all_items = []
    seen_links = set()
    with _TPE(max_workers=8) as pool:
        results = list(pool.map(fetch_google_news, queries[:16]))
    # Google News matches loosely: a search for "Waller Texas" returned an INNIO
    # story about a Waukesha expansion in Wisconsin, which mentions neither the
    # town nor anything in the county. Require the place itself to appear in the
    # headline, and reject headlines naming a different state - the term alone
    # being in the query proves nothing about the article.
    place_words = set()
    for t in terms:
        for w in _re.split(r"[^A-Za-z0-9]+", str(t)):
            w = w.strip()
            if len(w) > 2 and w.upper() not in ("TEXAS", "ISD", "THE", "AND", "COUNTY"):
                place_words.add(w.lower())

    OTHER_STATES = (
        "alabama", "alaska", "arizona", "arkansas", "california", "colorado",
        "connecticut", "delaware", "florida", "georgia", "hawaii", "idaho",
        "illinois", "indiana", "iowa", "kansas", "kentucky", "louisiana", "maine",
        "maryland", "massachusetts", "michigan", "minnesota", "mississippi",
        "missouri", "montana", "nebraska", "nevada", "hampshire", "jersey",
        "mexico", "york", "carolina", "dakota", "ohio", "oklahoma", "oregon",
        "pennsylvania", "rhode island", "tennessee", "utah", "vermont",
        "virginia", "washington", "wisconsin", "wyoming",
    )

    filtered_out = 0
    for items in results:
        for it in items:
            if it["link"] in seen_links: continue
            hay = (it.get("title") or "").lower()
            src = (it.get("source") or "").lower()
            if place_words and not any(w in hay for w in place_words):
                filtered_out += 1
                continue
            # A headline or outlet naming another state is not local news, even
            # if the place word happens to appear.
            if any(st in hay or st in src for st in OTHER_STATES):
                filtered_out += 1
                continue
            seen_links.add(it["link"])
            all_items.append(it)

    # Very rough recency sort — parse pubDate (RFC 822 format)
    from email.utils import parsedate_to_datetime
    def _pub_to_dt(s):
        try: return parsedate_to_datetime(s)
        except Exception: return None
    all_items.sort(key=lambda x: _pub_to_dt(x["published"]) or 0, reverse=True)

    return jsonify({
        "stories":   all_items[:40],
        "filtered_out": filtered_out,
        "queries":   queries[:20],
        "terms":     sorted(terms),
        "custom_query": custom_q or None,
        "source":    "Google News RSS (aggregated across queries)",
    })


@acq_bp.route("/api/acq/projects/<pid>/amenities", methods=["GET"])
@_login_required
def acq_api_projects_amenities(pid):
    """Nearby grocery stores, schools, and other draws for residential demand.

    Uses OpenStreetMap Overpass API (free, no key). Returns for each POI:
      • name / brand
      • kind (supermarket, school, hospital, etc.)
      • lat/lon
      • distance from project centroid (miles)
      • driving direction (basic bearing)

    Sorted by distance ascending; major-brand grocery highlighted separately.
    """
    import requests as _rq
    import math as _math
    from shapely.geometry import shape as shp_shape
    from shapely.ops import unary_union, transform

    guard = _acq_guard()
    if guard:
        return guard
    uid = _acq_owner()
    conn = get_db()
    try:
        proj = acq_store.get_object(conn, "project", pid, uid, _acq_is_admin())
    finally:
        conn.close()
    if not proj:
        return jsonify({"error": "project not found"}), 404

    geoms = []
    for t in proj.get("tracts") or []:
        g = t.get("geometry")
        if g:
            try: geoms.append(shp_shape(g))
            except Exception as e: print(f"[{request.endpoint}] tract geometry skipped: {e}", flush=True)
    if not geoms:
        return jsonify({"error": "no tract geometries"}), 400
    proj_union = unary_union(geoms)
    centroid = proj_union.centroid
    c_lat, c_lon = centroid.y, centroid.x

    try:
        radius_mi = float(request.args.get("radius_mi", "5"))
    except (TypeError, ValueError):
        radius_mi = 5.0
    radius_mi = max(0.5, min(radius_mi, 15.0))
    radius_m = int(radius_mi * 1609.34)

    # Overpass — CORE query (grocery / hospital / school) kept lean so it returns
    # in seconds; parks fetched separately and degrade silently if slow. `nwr`
    # matches nodes+ways+relations in one clause; `out center N;` caps results
    # and returns way/relation centroids. UA header required (406 without it).
    # _overpass_query races every planet mirror — see its notes for why.
    # Schools live in the School District Profile card now, so they're out of
    # this query — fewer clauses means a faster, less throttle-prone request.
    core_q = f"""
[out:json][timeout:25];
(
  nwr["shop"="supermarket"](around:{radius_m},{c_lat},{c_lon});
  nwr["shop"="department_store"](around:{radius_m},{c_lat},{c_lon});
  nwr["shop"="wholesale"](around:{radius_m},{c_lat},{c_lon});
  nwr["amenity"="hospital"](around:{radius_m},{c_lat},{c_lon});
);
out center 200;
"""
    parks_q = f"""
[out:json][timeout:20];
nwr["leisure"="park"](around:{radius_m},{c_lat},{c_lon});
out center 100;
"""
    # Commute & access. For outer-ring Houston tracts this is the single biggest
    # value driver — "how far to 290?" is the first question asked about a deal.
    #
    # Query the motorway/trunk WAYS, not the junction nodes: motorway_junction
    # nodes here carry neither `ref` nor `name` (95 of them around the test
    # tract, all anonymous), so they can say "a ramp is 1.9 mi away" but never
    # which highway it belongs to. The ways carry ref + name — "US 290;TX 6,
    # Northwest Freeway" — which is the answer people actually want. Junction
    # nodes are still worth one clause as a bare nearest-ramp distance, since
    # being near a freeway you can't get onto is a real and different problem.
    #
    # These radii are deliberately generous and fixed, not tied to radius_mi:
    # the point is always to surface the CLOSEST one, then let the payload flag
    # whether it fell inside the user's ring.
    ACCESS_M = int(15 * 1609.34)
    PR_M = int(25 * 1609.34)
    AIR_M = int(50 * 1609.34)
    access_q = f"""
[out:json][timeout:60];
(
  way["highway"~"^(motorway|trunk)$"](around:{ACCESS_M},{c_lat},{c_lon});
  node["highway"="motorway_junction"](around:{ACCESS_M},{c_lat},{c_lon});
  nwr["park_ride"]["park_ride"!="no"](around:{PR_M},{c_lat},{c_lon});
  nwr["aeroway"="aerodrome"]["iata"](around:{AIR_M},{c_lat},{c_lon});
);
out center 400;
"""
    # Retail anchors — a rooftop-density and retail-maturity signal, not just
    # convenience. department_store/wholesale overlap the core grocery query on
    # purpose; _ingest routes by brand so Walmart lands in both reads correctly.
    # department_store / wholesale are repeated here at the wider retail radius
    # on purpose: the core query only reaches radius_mi, so on a rural tract the
    # Target and Costco that define the retail picture sit well outside it and
    # would never be seen. Duplicates are collapsed after ingest.
    RETAIL_M = int(15 * 1609.34)
    FUEL_M = int(8 * 1609.34)
    retail_q = f"""
[out:json][timeout:60];
(
  nwr["shop"="supermarket"](around:{RETAIL_M},{c_lat},{c_lon});
  nwr["shop"="department_store"](around:{RETAIL_M},{c_lat},{c_lon});
  nwr["shop"="wholesale"](around:{RETAIL_M},{c_lat},{c_lon});
  nwr["shop"="doityourself"](around:{RETAIL_M},{c_lat},{c_lon});
  nwr["amenity"="pharmacy"](around:{int(10 * 1609.34)},{c_lat},{c_lon});
  nwr["amenity"="fuel"](around:{FUEL_M},{c_lat},{c_lon});
);
out center 300;
"""
    _overpass = _overpass_query   # shared module-level helper

    # Run the categories concurrently, but only two in flight: each one already
    # races both planet mirrors, and firing all four at once means eight
    # simultaneous requests to two hosts that throttle aggressively.
    from concurrent.futures import ThreadPoolExecutor as _ATP
    with _ATP(max_workers=2) as _apool:
        _f_core = _apool.submit(_overpass, core_q, 45)
        _f_parks = _apool.submit(_overpass, parks_q, 30)
        _f_access = _apool.submit(_overpass, access_q, 60)
        _f_retail = _apool.submit(_overpass, retail_q, 60)
        data, core_err = _f_core.result()
        parks_data, _parks_err = _f_parks.result()
        access_data, _access_err = _f_access.result()
        retail_data, _retail_err = _f_retail.result()

    if data is None:
        return jsonify({"error": f"Overpass query failed on all mirrors: {core_err}",
                        "grocery_stores": [], "schools": [], "hospitals": [], "parks": [],
                        "highways": [], "retail_anchors": []}), 502
    # Everything past the core query is best-effort — a slow parks or access
    # lookup must never take grocery and hospitals down with it.
    for _extra in (parks_data, retail_data):
        if _extra and _extra.get("elements"):
            data.setdefault("elements", []).extend(_extra["elements"])

    def _distance_mi(lat, lon):
        # Haversine — good enough at these scales
        R = 3958.7613   # earth radius in miles
        lat1 = _math.radians(c_lat); lon1 = _math.radians(c_lon)
        lat2 = _math.radians(lat);   lon2 = _math.radians(lon)
        a = _math.sin((lat2-lat1)/2)**2 + _math.cos(lat1)*_math.cos(lat2)*_math.sin((lon2-lon1)/2)**2
        return round(2 * R * _math.asin(_math.sqrt(a)), 2)
    def _bearing(lat, lon):
        # Simple 8-direction compass bearing from centroid
        dy = lat - c_lat
        dx = lon - c_lon
        if abs(dy) < 1e-5 and abs(dx) < 1e-5: return ""
        deg = (_math.degrees(_math.atan2(dx, dy)) + 360) % 360
        dirs = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"]
        return dirs[int((deg + 22.5) / 45) % 8]

    grocery = []; schools = []; hospitals = []; parks = []
    retail = []; pharmacies = []; fuel = []
    MAJOR_GROCERY_BRANDS = {"heb", "h-e-b", "kroger", "walmart", "target", "whole foods",
                              "sprouts", "aldi", "trader joe", "randalls", "sam's club",
                              "costco", "food town", "fiesta", "central market"}
    # National anchors worth calling out by name — their presence says more about
    # a submarket's retail maturity than a count of unnamed storefronts does.
    ANCHOR_BRANDS = {"walmart", "target", "costco", "sam's club", "home depot",
                     "lowe's", "kohl's", "jcpenney", "best buy", "academy",
                     "tractor supply", "marshalls", "tj maxx", "ross", "burlington"}

    # The same OSM feature arrives more than once: `nwr` matches a POI tagged on
    # both a node and its building way, and the retail sweep repeats the
    # supermarket / department_store / wholesale clauses at a wider radius than
    # the core query. Skip anything already ingested by OSM identity.
    _seen_osm = set()

    def _clean_poi_name(tags):
        for key in ("name", "brand", "operator"):
            val = (tags.get(key) or "").strip()
            if val and val.lower() not in ("(unnamed)", "unnamed", "unknown"):
                return val
        return None

    def _ingest(elements):
        for el in elements or []:
            oid = (el.get("type"), el.get("id"))
            if oid[1] is not None:
                if oid in _seen_osm:
                    continue
                _seen_osm.add(oid)
            tags = el.get("tags") or {}
            name = _clean_poi_name(tags)
            if not name:
                continue
            brand = (tags.get("brand") or tags.get("operator") or "").strip()
            # Location — for ways use center, for nodes use lat/lon
            lat = el.get("lat") or (el.get("center") or {}).get("lat")
            lon = el.get("lon") or (el.get("center") or {}).get("lon")
            if lat is None or lon is None: continue
            d = _distance_mi(lat, lon)
            b = _bearing(lat, lon)
            entry = {
                "name":         name,
                "brand":        brand,
                "lat":          lat, "lon": lon,
                "distance_mi":  d,
                "direction":    b,
                "tags":         {k: v for k, v in tags.items()
                                  if k in ("name","brand","operator","addr:city","addr:street","addr:full")},
            }
            shop = tags.get("shop") or ""
            amenity = tags.get("amenity") or ""
            leisure = tags.get("leisure") or ""
            blob = (name + " " + brand).lower()
            if shop in ("supermarket", "department_store", "wholesale"):
                is_grocer = any(m in blob for m in MAJOR_GROCERY_BRANDS)
                # Only a store you can actually buy groceries at counts as
                # grocery. shop=department_store covers Walmart and Target but
                # equally Marshalls, Ross and JCPenney — before this check the
                # wider retail sweep pushed Marshalls to the top of "closest
                # grocery store", which is the one thing that list must get right.
                if shop == "supermarket" or is_grocer:
                    grocery.append(dict(entry, is_major=is_grocer))
                # A Walmart or Costco is both a grocery run and a retail anchor;
                # it belongs in both reads rather than being forced into one.
                if shop in ("department_store", "wholesale"):
                    retail.append(dict(entry, kind=shop,
                                       is_anchor=any(m in blob for m in ANCHOR_BRANDS)))
            elif shop in ("doityourself", "hardware"):
                retail.append(dict(entry, kind="home_improvement",
                                   is_anchor=any(m in blob for m in ANCHOR_BRANDS)))
            elif amenity == "pharmacy" or tags.get("healthcare") == "pharmacy":
                pharmacies.append(entry)
            elif amenity == "fuel":
                fuel.append(entry)
            elif amenity == "school":
                schools.append(entry)
            elif amenity == "hospital":
                hospitals.append(entry)
            elif leisure == "park":
                parks.append(entry)

    _ingest(data.get("elements"))

    # --- Commute & access -------------------------------------------------
    # Highways are deduped by `ref` and reduced to the nearest point on each,
    # so "US 290" appears once at its closest approach rather than as 40 way
    # segments. Junction nodes are anonymous, so they only yield a single
    # nearest-ramp distance.
    highways = {}
    ramp_mi = None
    park_ride = []
    airports = []
    for el in (access_data or {}).get("elements") or []:
        tags = el.get("tags") or {}
        lat = el.get("lat") or (el.get("center") or {}).get("lat")
        lon = el.get("lon") or (el.get("center") or {}).get("lon")
        if lat is None or lon is None:
            continue
        d = _distance_mi(lat, lon)
        if tags.get("highway") == "motorway_junction":
            if ramp_mi is None or d < ramp_mi:
                ramp_mi = d
            continue
        if tags.get("highway") in ("motorway", "trunk"):
            ref = (tags.get("ref") or tags.get("name") or "").strip()
            if not ref:
                continue
            cur = highways.get(ref)
            if not cur or d < cur["distance_mi"]:
                highways[ref] = {"ref": ref, "name": tags.get("name") or "",
                                 "kind": tags.get("highway"),
                                 "toll": tags.get("toll") == "yes",
                                 "lat": lat, "lon": lon,
                                 "distance_mi": d, "direction": _bearing(lat, lon)}
            continue
        if tags.get("park_ride"):
            park_ride.append({"name": tags.get("name") or "Park & Ride",
                              "lat": lat, "lon": lon, "distance_mi": d,
                              "direction": _bearing(lat, lon)})
            continue
        if tags.get("aeroway") == "aerodrome":
            iata = (tags.get("iata") or "").strip()
            name = _clean_poi_name(tags) or iata
            if not name:
                continue
            airports.append({"name": name,
                             "iata": iata,
                             "type": tags.get("aerodrome:type") or "",
                             "lat": lat, "lon": lon, "distance_mi": d,
                             "direction": _bearing(lat, lon)})
    highways = sorted(highways.values(), key=lambda h: h["distance_mi"])[:6]
    park_ride.sort(key=lambda p: p["distance_mi"])
    # A 16-mi general-aviation strip is not what "nearest airport" means to a
    # homebuyer, but OSM tags cannot tell the two apart here: of the Houston
    # aerodromes only IAH carries aerodrome=international, while Hobby — a major
    # commercial airport — has nothing beyond iata/icao, exactly like the private
    # fields. So match against the FAA commercial-service list instead. Anything
    # unmatched is still returned, flagged, so a genuinely remote site isn't left
    # with a blank row.
    airports.sort(key=lambda a: a["distance_mi"])
    for a in airports:
        a["commercial"] = a["iata"].upper() in _TX_COMMERCIAL_AIRPORTS
    _comm = [a for a in airports if a["commercial"]]
    airports = (_comm or airports)[:3]

    # "Closest grocery store" must mean CLOSEST, not "within the base radius" —
    # rural sites routinely have no supermarket within 5 mi. Groceries no longer
    # need a widening pass: the retail sweep already queries supermarkets out to
    # 15 mi, so the nearest one is in hand on the first round.
    #
    # That sweep is also why the widening below tests `hospitals` alone. It used
    # to bail as soon as `grocery` was non-empty, and once the 15-mi retail
    # results began landing in grocery, a Costco 8 mi out satisfied that test and
    # suppressed the pass that had been surfacing the H-E-B at 6.3 mi — the
    # nearest real grocery store silently disappeared from the card.
    grocery_radius_used = 15
    hospital_radius_used = radius_mi
    for wider_mi in (10, 20, 30):
        if hospitals:
            break
        wider_m = int(wider_mi * 1609.34)
        widen_q = ("[out:json][timeout:25];\n"
                   f'nwr["amenity"="hospital"](around:{wider_m},{c_lat},{c_lon});\n'
                   "out center 100;\n")
        widen_data, _werr = _overpass(widen_q, 40)
        if widen_data:
            _ingest(widen_data.get("elements"))
            if hospitals:
                hospital_radius_used = wider_mi

    # Sort by distance
    for lst in (grocery, schools, hospitals, parks, retail, pharmacies, fuel):
        lst.sort(key=lambda x: x["distance_mi"])

    def _dedupe(items, tol_mi=0.2):
        """Collapse the same real-world place appearing more than once.

        OSM identity alone isn't enough: a store mapped as both a node and a
        building outline has two different ids, and the node sits a few metres
        off the way's centroid, so a rounded-coordinate key misses it too. What
        the eye catches is the giveaway — same name, effectively same distance —
        so match on that: identical name within `tol_mi`. Unnamed features fall
        back to position, since every unnamed park would otherwise collapse into
        one entry.
        """
        out = []
        for x in items:
            nm = (x.get("name") or "").strip().lower()
            dup = False
            if nm and nm != "(unnamed)":
                for y in out:
                    if ((y.get("name") or "").strip().lower() == nm
                            and abs(y["distance_mi"] - x["distance_mi"]) <= tol_mi):
                        dup = True
                        break
            else:
                for y in out:
                    if (abs(y["lat"] - x["lat"]) < 1e-4
                            and abs(y["lon"] - x["lon"]) < 1e-4):
                        dup = True
                        break
            if not dup:
                out.append(x)
        return out

    grocery = _dedupe(grocery)
    schools = _dedupe(schools)
    hospitals = _dedupe(hospitals)
    parks = _dedupe(parks)
    pharmacies = _dedupe(pharmacies)
    fuel = _dedupe(fuel)
    _dedup = _dedupe(retail)
    # shop=doityourself is applied to national big-box stores and to one-room
    # tool shops alike, so an unfiltered 15-mi sweep buries Target and Costco
    # under "Katy Tools" and "1 Top Tools". Independents still count when they're
    # genuinely close (they serve the rooftops), but out at the edge of the ring
    # only the national anchors say anything about the submarket.
    retail = [r for r in _dedup if r.get("is_anchor") or r["distance_mi"] <= radius_mi]
    retail.sort(key=lambda r: (not r.get("is_anchor"), r["distance_mi"]))

    # Every list is searched on a generous fixed radius so the CLOSEST one always
    # surfaces; flag which entries actually fell inside the user's ring so the UI
    # can say "nearest is 9.3 mi — outside the 5 mi ring" instead of showing
    # nothing. Anything already widened above keeps its own radius note.
    for lst in (grocery, hospitals, parks, retail, pharmacies, fuel):
        for x in lst:
            x["within_radius"] = x["distance_mi"] <= radius_mi

    # Cap
    grocery = grocery[:30]
    schools = schools[:30]
    hospitals = hospitals[:20]
    parks = parks[:20]
    retail = retail[:30]
    pharmacies = pharmacies[:12]
    fuel = fuel[:12]

    # ------------------------------------------------------------------
    # Full-buildout demand + commercial gap
    # ------------------------------------------------------------------
    # What the trade area looks like when every platted lot is built, and which
    # commercial uses that population would support that aren't here yet.
    #
    # Population base: ACS for what exists today (it counts all housing, not just
    # the new-home communities CBAS tracks), plus the remaining CBAS lot pipeline
    # times household size for the growth. When Census is unavailable, fall back
    # to CBAS occupied homes, which undercounts existing rural housing — the
    # payload says which basis was used so the number is never read as more
    # precise than it is.
    TX_AVG_HH = 2.85          # Texas average household size, ACS
    demo = None
    try:
        demo = _acs_radius_demographics(c_lat, c_lon, radius_mi)
    except Exception:
        demo = None
    hh_size = (demo or {}).get("avg_household_size") or TX_AVG_HH

    occupied = remaining = total_lots = 0
    annual_closings = 0
    cbas_ok = False
    try:
        _tok = os.environ.get("CBAS_TOKEN", "").strip()
        if _tok:
            _ents, _latest, _err = _cbas_get_data(_tok)
            if _ents:
                for e in _ents:
                    la, lo = e.get("lat"), e.get("lng")
                    if not la or not lo:
                        continue
                    if _distance_mi(float(la), float(lo)) > radius_mi:
                        continue
                    cbas_ok = True
                    occupied += e.get("occupied") or 0
                    total_lots += e.get("total") or 0
                    remaining += ((e.get("vdls") or 0) + (e.get("futures") or 0)
                                  + (e.get("under_construction") or 0))
                    annual_closings += e.get("annual_closings") or 0
    except Exception:
        cbas_ok = False

    # Rooftops today come from OSM building footprints, NOT CBAS occupied homes.
    # CBAS only tracks actively-selling new-home communities, so it misses every
    # subdivision that finished selling — in mature Cinco Ranch that is the
    # difference between 405 and 44,444. Using it here would have understated
    # existing rooftops on any site with established housing nearby, and it is
    # the same denominator the commercial benchmarks are calibrated against, so
    # the two now agree by construction.
    roofs_now = None
    try:
        bq = (f'[out:json][timeout:120];\n(\n'
              f'  way["building"~"^(house|detached|residential|semidetached_house|terrace)$"]'
              f'(around:{radius_m},{c_lat},{c_lon});\n'
              f'  way["building"="yes"](around:{radius_m},{c_lat},{c_lon});\n'
              f');\nout count;\n')
        bd, _berr = _overpass(bq, 90)
        if bd:
            _els = bd.get("elements") or []
            if _els:
                roofs_now = int((_els[0].get("tags") or {}).get("total") or 0) or None
    except Exception:
        roofs_now = None
    if roofs_now is None:
        roofs_now = occupied or None       # last resort; known to undercount

    pop_now = ((demo or {}).get("population")
               or (round(roofs_now * hh_size) if roofs_now else 0))
    pop_basis = "Census ACS" if (demo or {}).get("population") else (
        "OSM building count x household size" if roofs_now else None)
    pop_buildout = pop_now + round(remaining * hh_size) if pop_now else None

    buildout = {
        "population_now": pop_now or None,
        "population_at_buildout": pop_buildout,
        "population_added": round(remaining * hh_size) if remaining else None,
        "growth_pct": (round((pop_buildout - pop_now) / pop_now * 100, 1)
                       if pop_now and pop_buildout else None),
        "rooftops_now": roofs_now,
        "rooftops_source": ("OSM building footprints" if roofs_now and roofs_now != occupied
                            else "CBAS occupied homes"),
        "cbas_occupied": occupied or None,
        "rooftops_at_buildout": ((roofs_now or 0) + remaining) or None,
        "lots_remaining": remaining or None,
        "total_lots_tracked": total_lots or None,
        "avg_household_size": hh_size,
        "household_size_source": ("Census ACS" if (demo or {}).get("avg_household_size")
                                  else "Texas average"),
        "population_basis": pop_basis,
        "years_to_buildout": (round(remaining / annual_closings, 1)
                              if remaining and annual_closings else None),
        "annual_closings": annual_closings or None,
        "demographics": demo,
        "cbas_available": cbas_ok,
    }

    # Benchmarks MEASURED from mature Houston new-home suburbs, not assumed.
    #
    # Method: six built-out reference areas (Cinco Ranch, Bridgeland, Riverstone,
    # Spring/Klein, Shadow Creek, Aliana). For each, count the same OSM
    # categories within 5 mi and divide by the OSM building footprint count in
    # the same ring. Median across the six is the benchmark; the observed range
    # is carried through so the UI can show how consistent each one actually is.
    #
    # The denominator is OSM buildings, not CBAS occupied homes. Calibrating on
    # CBAS was tried first and failed badly: it tracks only actively-selling
    # new-home communities, so Cinco Ranch came back as 405 rooftops and implied
    # one grocery store per 16 homes, with a 13x spread across references. OSM
    # carries the Microsoft US building import, which returns 44,444 structures
    # in the same ring — a real count, and it needs no Census key.
    #
    # `spread` is max/min across the six references. Low spread means the ratio
    # is stable and the gap is worth acting on; hospital at 15x is essentially
    # noise and is reported without an opportunity flag.
    BENCHMARKS = [
        ("Grocery / supermarket", "grocery", 1900, 3.1,
         "anchors a neighbourhood centre; first retail a new community needs"),
        ("Pharmacy", "pharmacy", 1800, 3.7, "usually follows the grocery anchor"),
        ("Fuel / convenience", "fuel", 550, 2.8, "earliest commercial to arrive"),
        ("Parks / open space", "park", 300, 1.9, "public or HOA; the most consistent ratio measured"),
        ("General merchandise", "general_merch", 3700, 5.7, "Target / Walmart scale"),
        ("Urgent care / clinic", "clinic", 2800, 7.1, "OSM coverage patchy; verify locally"),
        ("Home improvement", "home_improvement", 8000, 5.3, "big-box, serves several communities"),
        ("Hospital", "hospital", 7400, 15.4, "regional draw; ratio too variable to act on"),
    ]
    have = {
        "grocery": len([x for x in grocery if x.get("within_radius")]),
        "pharmacy": len([x for x in pharmacies if x.get("within_radius")]),
        "fuel": len([x for x in fuel if x.get("within_radius")]),
        "clinic": 0,          # not queried; reported as unknown rather than zero
        "hospital": len([x for x in hospitals if x.get("within_radius")]),
        "home_improvement": len([x for x in retail
                                 if x.get("kind") == "home_improvement" and x.get("within_radius")]),
        "general_merch": len([x for x in retail
                              if x.get("kind") in ("department_store", "wholesale")
                              and x.get("within_radius")]),
        "park": len([x for x in parks if x.get("within_radius")]),
    }
    opportunities = []
    roofs_bo = buildout["rooftops_at_buildout"]
    if roofs_now and roofs_bo:
        for label, key, per, spread, note in BENCHMARKS:
            sup_now = roofs_now / per
            sup_bo = roofs_bo / per
            cur = have.get(key, 0)
            gap_bo = sup_bo - cur
            # Only flag a gap worth acting on where the benchmark is stable.
            # A 15x spread across reference areas means the ratio isn't
            # describing a real requirement, so it gets reported without a flag.
            confidence = ("high" if spread <= 3.5 else
                          "medium" if spread <= 6.0 else "low")
            opportunities.append({
                "use": label, "key": key,
                "rooftops_per_facility": per,
                "spread": spread, "confidence": confidence,
                "existing": cur,
                "supported_now": round(sup_now, 1),
                "supported_at_buildout": round(sup_bo, 1),
                "gap_now": round(sup_now - cur, 1),
                "gap_at_buildout": round(gap_bo, 1),
                "opportunity": gap_bo >= 1 and confidence in ("high", "medium"),
                "unknown": key == "clinic",
                "note": note,
            })
        # Biggest shortfall first, but keep a stable order for equal gaps.
        opportunities.sort(key=lambda o: (-o["gap_at_buildout"], o["use"]))

    return jsonify({
        "centroid":            {"lat": c_lat, "lon": c_lon},
        "radius_mi":           radius_mi,
        "buildout":            buildout,
        "opportunities":       opportunities,
        "grocery_radius_mi":   grocery_radius_used,
        "hospital_radius_mi":  hospital_radius_used,
        "grocery_stores":      grocery,
        "schools":             schools,
        "hospitals":           hospitals,
        "parks":               parks,
        # Commute & access
        "highways":            highways,
        "nearest_ramp_mi":     ramp_mi,
        "park_ride":           park_ride[:4],
        "airports":            airports,
        # Retail anchors
        "retail_anchors":      retail,
        "pharmacies":          pharmacies,
        "fuel":                fuel,
        "source":              "OpenStreetMap Overpass API",
    })


@acq_bp.route("/api/acq/projects/<pid>/communities", methods=["GET"])
@_login_required
def acq_api_projects_communities(pid):
    """List residential communities / subdivisions in the project's submarket.

    Approach: pull parcels from the local StratMap cache (Houston-metro counties
    pre-cached) within the same school district as the project. Group by the
    subdivision name parsed from LEGAL_DESC. Returns the top N by parcel count.

    Useful for acquisitions: "what existing communities define this submarket,
    and which one is closest in product to what we'd build?"
    """
    import re as _re
    from shapely.geometry import shape as shp_shape, Point as _Pt
    from shapely.ops import unary_union, transform

    guard = _acq_guard()
    if guard:
        return guard
    uid = _acq_owner()
    conn = get_db()
    try:
        proj = acq_store.get_object(conn, "project", pid, uid, _acq_is_admin())
    finally:
        conn.close()
    if not proj:
        return jsonify({"error": "project not found"}), 404

    geoms = []
    for t in proj.get("tracts") or []:
        g = t.get("geometry")
        if g:
            try: geoms.append(shp_shape(g))
            except Exception as e: print(f"[{request.endpoint}] tract geometry skipped: {e}", flush=True)
    if not geoms:
        return jsonify({"error": "no tract geometries"}), 400
    project_union = unary_union(geoms)
    centroid = project_union.centroid
    c_lat, c_lon = centroid.y, centroid.x

    # Circle radius centered on the project centroid (default 5 mi).
    try:
        radius_mi = float(request.args.get("radius_mi", "5"))
    except (TypeError, ValueError):
        radius_mi = 5.0
    radius_mi = max(0.5, min(radius_mi, 15.0))

    # Buffer the CENTROID by the radius — proper circular submarket, project
    # included (centroid is inside the project). Not a polygon-outward buffer.
    centroid_utm = transform(to_utm, centroid)
    circle_utm = centroid_utm.buffer(radius_mi * 1609.34)
    circle_wgs = transform(to_wgs, circle_utm)

    import acq_parcels as parcel_cache
    try:
        fc = parcel_cache.query_parcels_in_polygon(circle_wgs, min_acres=0.05, max_acres=50)
        feats = fc.get("features") or []
    except Exception as e:
        return jsonify({"error": f"parcel cache query failed: {e}", "communities": []}), 500

    # Parse subdivision name from LEGAL_DESC using the shared parser (rejects
    # rural ABS abstracts, strips CAD plat codes like Waller's "S588000").
    # Compute distance from project centroid to each parcel for proximity sort.
    by_sub = {}
    for f in feats:
        p = f.get("properties") or {}
        sub = _parse_subdivision_name(p.get("LEGAL_DESC") or "")
        if not sub: continue
        acres = float(p.get("Acres") or 0)
        # Compute distance from project centroid to this parcel (miles)
        try:
            parcel_geom = shp_shape(f["geometry"])
            parcel_pt = parcel_geom.centroid
            # UTM distance for accuracy
            dist_m = transform(to_utm, parcel_pt).distance(centroid_utm)
            dist_mi = dist_m / 1609.34
        except Exception:
            dist_mi = None
        entry = by_sub.setdefault(sub, {
            "name": sub, "parcels": 0, "acres": 0.0,
            "owners": set(), "counties": set(),
            "min_dist_mi": None,
            "sum_dist_mi": 0.0,
            "builder_held": 0,        # parcels still owned by a known builder
            "builder_counts": {},     # builder name -> parcel count (for dominant)
        })
        entry["parcels"] += 1
        entry["acres"] += acres
        owner = (p.get("OWNER_NAME") or "").strip()
        if owner: entry["owners"].add(owner)
        # Absorption proxy: is this lot still held by a homebuilder?
        b = _classify_builder(owner)
        if b:
            entry["builder_held"] += 1
            entry["builder_counts"][b] = entry["builder_counts"].get(b, 0) + 1
        county = (p.get("_county") or "").strip()
        if county: entry["counties"].add(county)
        if dist_mi is not None:
            entry["sum_dist_mi"] += dist_mi
            if entry["min_dist_mi"] is None or dist_mi < entry["min_dist_mi"]:
                entry["min_dist_mi"] = dist_mi

    # Acquisitions care about REAL communities, not 6-lot splits: require 50+
    # platted lots. Tier badge: MPC (1000+), Large (300+), Community (50+).
    MIN_PARCELS = 50
    def _size_tier(n):
        if n >= 1000: return "MPC"
        if n >= 300:  return "Large"
        return "Community"
    communities = []
    for v in by_sub.values():
        if v["parcels"] < MIN_PARCELS: continue
        avg_dist = (v["sum_dist_mi"] / v["parcels"]) if v["parcels"] > 0 else None
        builder_pct = round(v["builder_held"] / v["parcels"] * 100, 1) if v["parcels"] else 0
        # Dominant builder (most parcels held) if any
        dom = max(v["builder_counts"].items(), key=lambda kv: kv[1])[0] if v["builder_counts"] else None
        # Lifecycle heuristic from builder-held %: active-selling communities
        # still have builder-owned inventory; sold-out ones are ~all individuals.
        if builder_pct >= 30:   status = "Actively selling"
        elif builder_pct >= 5:  status = "Late phase"
        else:                   status = "Sold out / resale"
        avg_lot_ac = (v["acres"] / v["parcels"]) if v["parcels"] else 0
        avg_lot_sqft = avg_lot_ac * 43560
        # Homebuilder convention: express lot size as FRONTAGE FEET assuming a
        # standard 120-ft depth (FF = sqft / 120). 6,000 sf -> 50 FF, 7,200 -> 60 FF.
        est_ff = avg_lot_sqft / 120.0 if avg_lot_sqft > 0 else 0
        communities.append({
            "name":              v["name"],
            "tier":              _size_tier(v["parcels"]),
            "parcels":           v["parcels"],
            "acres":             round(v["acres"], 1),
            "avg_lot_acres":     round(avg_lot_ac, 3),
            "avg_lot_sqft":      int(round(avg_lot_sqft)),
            "est_ff":            int(round(est_ff)),
            "counties":          sorted(v["counties"]),
            "owner_sample":      sorted(v["owners"])[:5],
            "owner_diversity":   len(v["owners"]),
            "builder_held":      v["builder_held"],
            "builder_held_pct":  builder_pct,
            "dominant_builder":  dom,
            "status":            status,
            "closest_mi":        round(v["min_dist_mi"], 2) if v["min_dist_mi"] is not None else None,
            "avg_dist_mi":       round(avg_dist, 2) if avg_dist is not None else None,
        })
    # ---- OSM named communities ------------------------------------------
    # CRITICAL for Harris County: StratMap leaves LEGAL_DESC EMPTY on Harris
    # home lots (verified against the local cache), so parcel-based detection
    # finds nothing there. OSM has named place nodes + named residential
    # landuse polygons for real communities (Bridgeland, Towne Lake, etc.) —
    # verified live: 300 named elements within 5 mi of Cypress.
    import math as _math
    radius_m = int(radius_mi * 1609.34)
    osm_q = f"""
[out:json][timeout:25];
(
  node["place"~"^(neighbourhood|suburb|quarter|village|hamlet)$"](around:{radius_m},{c_lat},{c_lon});
  way["landuse"="residential"]["name"](around:{radius_m},{c_lat},{c_lon});
  relation["landuse"="residential"]["name"](around:{radius_m},{c_lat},{c_lon});
);
out center 400;
"""
    osm_by_name = {}
    osm_data, _osm_err = _overpass_query(osm_q, 40)
    if osm_data:
        for el in osm_data.get("elements", []):
            tags = el.get("tags") or {}
            nm = (tags.get("name") or "").strip()
            if not nm or len(nm) < 3: continue
            lat = el.get("lat") or (el.get("center") or {}).get("lat")
            lon = el.get("lon") or (el.get("center") or {}).get("lon")
            if lat is None or lon is None: continue
            d_mi = _math.hypot((lon - c_lon) * 69.0 * _math.cos(_math.radians(c_lat)),
                                (lat - c_lat) * 69.0)
            key = nm.upper()
            e = osm_by_name.setdefault(key, {"name": nm, "closest_mi": d_mi, "sections": 0})
            e["sections"] += 1
            if d_mi < e["closest_mi"]:
                e["closest_mi"] = d_mi

    # ---- Merge: OSM names (authoritative for existence/distance) enriched
    # with parcel metrics (lots, FF, builder-held) where the names match.
    parcel_by_name = {c["name"].upper(): c for c in communities}
    merged = []
    for key, osm in osm_by_name.items():
        pc = parcel_by_name.pop(key, None)
        row = {
            "name":              osm["name"],
            "closest_mi":        round(osm["closest_mi"], 2),
            "source":            "OSM+parcels" if pc else "OSM",
            "tier":              (pc or {}).get("tier"),
            "parcels":           (pc or {}).get("parcels"),
            "est_ff":            (pc or {}).get("est_ff"),
            "avg_lot_sqft":      (pc or {}).get("avg_lot_sqft"),
            "builder_held_pct":  (pc or {}).get("builder_held_pct"),
            "dominant_builder":  (pc or {}).get("dominant_builder"),
            "status":            (pc or {}).get("status"),
            "counties":          (pc or {}).get("counties") or [],
        }
        merged.append(row)
    # Parcel-only communities (counties where LEGAL_DESC works but OSM lacks the name)
    for pc in parcel_by_name.values():
        merged.append({**pc, "source": "parcels"})
    merged.sort(key=lambda x: (x["closest_mi"] if x["closest_mi"] is not None else 9999))
    top = merged[:40]

    return jsonify({
        "communities":     top,
        "total_distinct":  len(merged),
        "radius_mi":       radius_mi,
        "parcels_scanned": len(feats),
        "min_parcels":     MIN_PARCELS,
        "centroid":        {"lat": c_lat, "lon": c_lon},
        "osm_found":       len(osm_by_name),
        "note":            (f"Community names from OpenStreetMap (place nodes + named residential areas) "
                             f"merged with parcel-cache metrics where legal descriptions carry subdivision names. "
                             f"Harris County home lots have blank legal descriptions in StratMap, so Harris rows "
                             f"show name + distance only. Lot metrics require {MIN_PARCELS}+ platted parcels."),
    })


@acq_bp.route("/api/acq/projects/<pid>/submarket", methods=["GET"])
@_login_required
def acq_api_projects_submarket(pid):
    """Submarket-level data for the project — built for acquisitions thinking
    at the school-district / ZIP / Census tract level (county is too broad —
    Harris County alone is 1,700 sq mi covering radically different markets).

    Returns:
      • Containing-tract list (the Census tracts the project sits in)
      • Neighboring tracts within a configurable radius (?radius_mi=3 default)
      • Population-weighted aggregates across containing tracts + same-school-district neighbors
      • School districts (Unified/Secondary/Elementary) from Census Geocoder
      • ZIP codes the project overlaps
      • Census Designated Place (CDP) / incorporated city the project is in
      • Tract polygons so the client can render them on the map

    Query string:
      ?radius_mi=3   — neighborhood radius (default 3, max 15)
    """
    import requests as _rq
    from shapely.geometry import shape as shp_shape, Point as _Pt
    from shapely.ops import unary_union, transform

    guard = _acq_guard()
    if guard:
        return guard
    uid = _acq_owner()
    conn = get_db()
    try:
        proj = acq_store.get_object(conn, "project", pid, uid, _acq_is_admin())
    finally:
        conn.close()
    if not proj:
        return jsonify({"error": "project not found"}), 404

    try:
        radius_mi = float(request.args.get("radius_mi", "3"))
    except (TypeError, ValueError):
        radius_mi = 3.0
    radius_mi = max(0.5, min(radius_mi, 15.0))

    geoms = []
    for t in proj.get("tracts") or []:
        g = t.get("geometry")
        if g:
            try: geoms.append(shp_shape(g))
            except Exception as e: print(f"[{request.endpoint}] tract geometry skipped: {e}", flush=True)
    if not geoms:
        return jsonify({"error": "no tract geometries"}), 400
    proj_union = unary_union(geoms)
    centroid = proj_union.centroid
    c_lon, c_lat = centroid.x, centroid.y

    # Build a 3-mi (or user-specified) buffer around the project for "neighboring" tracts.
    # Buffer in UTM so radius is in true meters.
    buffer_utm = transform(to_utm, proj_union).buffer(radius_mi * 1609.34)
    buffer_wgs = transform(to_wgs, buffer_utm)

    out = {
        "centroid":         {"lat": c_lat, "lon": c_lon},
        "radius_mi":        radius_mi,
        "tracts":           [],        # containing tracts
        "neighbor_tracts":  [],        # tracts within radius_mi (excluding containing)
        "tract_summary":    {},        # weighted aggregates over containing tracts
        "wider_summary":    {},        # weighted aggregates over containing + neighbor
        "school_districts": [],
        "zip_codes":        [],
        "place":            None,      # incorporated city or CDP
        "sources":          [],
        "caveats":          [],
    }

    # --- 1) Identify Census TRACT via the Census Geocoder (free, no key) ---
    census_key = os.environ.get("CENSUS_API_KEY", "").strip()
    tract_fips_list = []   # list of (state, county, tract) tuples
    try:
        geocoder_url = "https://geocoding.geo.census.gov/geocoder/geographies/coordinates"
        gr = _rq.get(geocoder_url, params={
            "x": c_lon, "y": c_lat,
            "benchmark": "Public_AR_Current",
            "vintage":   "Current_Current",
            "format":    "json",
        }, timeout=12)
        gr.raise_for_status()
        geo = gr.json().get("result", {}).get("geographies", {})
        # Census Tracts
        for t in (geo.get("Census Tracts") or []):
            tract_fips_list.append((str(t.get("STATE")).zfill(2),
                                     str(t.get("COUNTY")).zfill(3),
                                     str(t.get("TRACT")).zfill(6)))
        # Unified School Districts
        for sd in (geo.get("Unified School Districts") or []):
            out["school_districts"].append({
                "name":  sd.get("NAME"),
                "geoid": sd.get("GEOID"),
                "type":  "Unified",
            })
        # Secondary (high school) districts — some Texas areas have separate ones
        for sd in (geo.get("Secondary School Districts") or []):
            out["school_districts"].append({
                "name":  sd.get("NAME"),
                "geoid": sd.get("GEOID"),
                "type":  "Secondary",
            })
        # Elementary
        for sd in (geo.get("Elementary School Districts") or []):
            out["school_districts"].append({
                "name":  sd.get("NAME"),
                "geoid": sd.get("GEOID"),
                "type":  "Elementary",
            })
        # ZIP code(s) — Census ZCTAs (Zip Code Tabulation Areas)
        for zc in (geo.get("Zip Code Tabulation Areas") or []):
            out["zip_codes"].append({
                "zip":   zc.get("BASENAME") or zc.get("ZCTA5") or zc.get("NAME"),
                "geoid": zc.get("GEOID"),
            })
        # Census Place (incorporated city OR CDP)
        for pl in (geo.get("Incorporated Places") or []):
            out["place"] = {"name": pl.get("NAME"), "type": "Incorporated City",
                             "geoid": pl.get("GEOID")}
            break
        if not out["place"]:
            for pl in (geo.get("Census Designated Places") or []):
                out["place"] = {"name": pl.get("NAME"), "type": "CDP (unincorporated)",
                                 "geoid": pl.get("GEOID")}
                break
        out["sources"].append("Census Bureau Geocoder (tract/school-district/ZIP/place lookup)")
    except Exception as e:
        out["caveats"].append(f"Census geocoder unavailable: {e}")

    if not tract_fips_list:
        out["caveats"].append("No census tracts found for project centroid.")

    # --- 2) Pull tract-level ACS data for each containing tract ---
    YEAR = _acs_year()
    TRACT_VARS = {
        "B01003_001E": "population",
        "B01002_001E": "median_age",
        "B11001_001E": "households",
        "B25010_001E": "avg_household_size",
        "B19013_001E": "median_household_income",
        "B19301_001E": "per_capita_income",
        "B25001_001E": "housing_units",
        "B25002_002E": "occupied_units",
        "B25002_003E": "vacant_units",
        "B25003_002E": "owner_occupied",
        "B25003_003E": "renter_occupied",
        "B25077_001E": "median_home_value",
        "B25064_001E": "median_rent",
        "B23025_004E": "employed",
        "B15003_022E": "bachelors_only",
        "B15003_023E": "masters",
        "B15003_024E": "professional",
        "B15003_025E": "doctorate",
        "B15003_001E": "education_universe",
        "B25034_001E": "year_built_universe",
        "B25035_001E": "median_year_built",
    }

    def _int_or_none(v):
        try:
            if v is None or v == "" or str(v).startswith("-666666"): return None
            return int(v)
        except (ValueError, TypeError): return None

    for state_fips, county_fips, tract_fips in tract_fips_list:
        try:
            var_list = ",".join(["NAME"] + list(TRACT_VARS.keys()))
            params = {
                "get": var_list,
                "for": f"tract:{tract_fips}",
                "in":  f"state:{state_fips}+county:{county_fips}",
            }
            if census_key:
                params["key"] = census_key
            r = _rq.get(f"https://api.census.gov/data/{YEAR}/acs/acs5", params=params, timeout=15)
            if r.status_code != 200:
                out["caveats"].append(f"Tract {tract_fips}: Census HTTP {r.status_code}")
                continue
            rows = r.json()
            if not isinstance(rows, list) or len(rows) < 2:
                continue
            headers, data = rows[0], rows[1]
            rec = dict(zip(headers, data))
            tract_data = {label: _int_or_none(rec.get(var)) for var, label in TRACT_VARS.items()}
            # Derived ratios per tract
            edu_tot = sum(filter(None, [tract_data.get(k) for k in
                                          ("bachelors_only", "masters", "professional", "doctorate")]))
            edu_uni = tract_data.get("education_universe") or 0
            tract_data["pct_bachelors_or_higher"] = round(edu_tot / edu_uni * 100, 1) if edu_uni else None
            oo, occ = tract_data.get("owner_occupied") or 0, tract_data.get("occupied_units") or 0
            tract_data["owner_occupancy_pct"] = round(oo / occ * 100, 1) if occ else None
            vu, hu = tract_data.get("vacant_units") or 0, tract_data.get("housing_units") or 0
            tract_data["vacancy_pct"] = round(vu / hu * 100, 1) if hu else None
            out["tracts"].append({
                "fips": f"{state_fips}{county_fips}{tract_fips}",
                "name": rec.get("NAME"),
                "data": tract_data,
            })
        except Exception as e:
            out["caveats"].append(f"Tract {tract_fips} fetch failed: {e}")

    if out["tracts"]:
        out["sources"].append(f"Census ACS 5-year tract-level ({YEAR})")

    # --- 2.5) Find ALL school districts the project POLYGON intersects, not just
    # the centroid's district. Critical fix — a project that straddles a district
    # boundary needs both districts counted, otherwise the submarket excludes the
    # other half of the project's own market.
    sd_polygons = []
    try:
        b = proj_union.bounds
        # Query school districts spatially over the project bbox.
        # NOTE: the TIGERweb service is named "School" (verified live) — layers
        # 0/1/2 = current-vintage Unified / Secondary / Elementary districts.
        for layer_id in [0, 1, 2]:   # Unified / Secondary / Elementary
            try:
                tu = f"https://tigerweb.geo.census.gov/arcgis/rest/services/TIGERweb/School/MapServer/{layer_id}/query"
                tr = _rq.get(tu, params={
                    "geometry":          f"{b[0]},{b[1]},{b[2]},{b[3]}",
                    "geometryType":      "esriGeometryEnvelope",
                    "inSR":              "4326",
                    "spatialRel":        "esriSpatialRelIntersects",
                    "outFields":         "GEOID,NAME",
                    "returnGeometry":    "true",
                    "outSR":             "4326",
                    "f":                 "geojson",
                }, timeout=15)
                if tr.status_code != 200: continue
                fc = tr.json()
                for feat in (fc.get("features") or []):
                    geom = feat.get("geometry")
                    if not geom: continue
                    try:
                        shp = shp_shape(geom)
                        if not shp.intersects(proj_union): continue
                        gid = (feat.get("properties") or {}).get("GEOID")
                        name = (feat.get("properties") or {}).get("NAME")
                        if any(p["geoid"] == gid for p in sd_polygons): continue
                        sd_polygons.append({
                            "name":     name,
                            "geoid":    gid,
                            "geometry": geom,
                            "shape":    shp,
                        })
                    except Exception: continue
            except Exception: continue
    except Exception as e:
        out["caveats"].append(f"School district spatial lookup failed: {e}")

    # Replace the centroid-based school_districts list with the spatial-intersect
    # result so the UI shows ALL districts the project actually overlaps.
    if sd_polygons:
        out["school_districts"] = [
            {"name": p["name"], "geoid": p["geoid"], "type": "Unified"}
            for p in sd_polygons
        ]

    # Return the school district polygons so client can render them on the map
    out["school_district_polygons"] = [
        {"name": p["name"], "geoid": p["geoid"], "geometry": p["geometry"]}
        for p in sd_polygons
    ]

    # --- 2.6) Discover NEIGHBORING census tracts within `radius_mi` of the project ---
    # Use Census TIGER REST API to find all tract polygons intersecting the buffer.
    # Then filter to ONLY those whose centroid falls inside the project's school
    # district(s) — that's the actual residential submarket.
    containing_keys = {(t["fips"][:2], t["fips"][2:5], t["fips"][5:])
                        for t in out["tracts"]}
    try:
        tigerweb = ("https://tigerweb.geo.census.gov/arcgis/rest/services/"
                     "TIGERweb/Tracts_Blocks/MapServer/0/query")
        b = buffer_wgs.bounds
        tr = _rq.get(tigerweb, params={
            "where":             "1=1",
            "geometry":          f"{b[0]},{b[1]},{b[2]},{b[3]}",
            "geometryType":      "esriGeometryEnvelope",
            "inSR":              "4326",
            "spatialRel":        "esriSpatialRelIntersects",
            "outFields":         "STATE,COUNTY,TRACT,GEOID,BASENAME",
            "returnGeometry":    "true",
            "outSR":             "4326",
            "f":                 "geojson",
        }, timeout=15)
        tr.raise_for_status()
        nearby_fc = tr.json()
        neighbor_features = []
        for feat in nearby_fc.get("features", []):
            props = feat.get("properties") or {}
            st_f  = str(props.get("STATE") or "").zfill(2)
            co_f  = str(props.get("COUNTY") or "").zfill(3)
            tr_f  = str(props.get("TRACT") or "").zfill(6)
            key   = (st_f, co_f, tr_f)
            # Verify the tract polygon ACTUALLY intersects our buffer (Esri's bbox
            # filter returns anything inside the bbox even if not the polygon).
            try:
                tract_geom = shp_shape(feat["geometry"])
                if not tract_geom.intersects(buffer_wgs): continue
            except Exception:
                continue
            if key in containing_keys:
                fips = f"{st_f}{co_f}{tr_f}"
                for containing in out["tracts"]:
                    if containing.get("fips") == fips and not containing.get("geometry"):
                        containing["geometry"] = feat.get("geometry")
                        break
                continue   # already in containing set
            # School-district filter: only count if the tract's centroid falls
            # inside any of the project's school district polygons. If we have
            # NO school district polygons (geocoder failed), keep all neighbors.
            in_district = True
            if sd_polygons:
                tract_centroid = tract_geom.centroid
                in_district = any(p["shape"].contains(tract_centroid) for p in sd_polygons)
            neighbor_features.append((key, props, feat.get("geometry"), tract_geom, in_district))
        # Pull ACS for each neighbor (cap at 50 to avoid blowing up the Census API)
        for (st_f, co_f, tr_f), props, geom, _shp_geom, in_district in neighbor_features[:50]:
            try:
                var_list = ",".join(["NAME"] + list(TRACT_VARS.keys()))
                params = {
                    "get": var_list,
                    "for": f"tract:{tr_f}",
                    "in":  f"state:{st_f}+county:{co_f}",
                }
                if census_key:
                    params["key"] = census_key
                r = _rq.get(f"https://api.census.gov/data/{YEAR}/acs/acs5", params=params, timeout=15)
                if r.status_code != 200: continue
                rows = r.json()
                if not isinstance(rows, list) or len(rows) < 2: continue
                headers, data = rows[0], rows[1]
                rec = dict(zip(headers, data))
                td = {label: _int_or_none(rec.get(var)) for var, label in TRACT_VARS.items()}
                edu_tot = sum(filter(None, [td.get(k) for k in
                                              ("bachelors_only", "masters", "professional", "doctorate")]))
                edu_uni = td.get("education_universe") or 0
                td["pct_bachelors_or_higher"] = round(edu_tot / edu_uni * 100, 1) if edu_uni else None
                oo, occ = td.get("owner_occupied") or 0, td.get("occupied_units") or 0
                td["owner_occupancy_pct"] = round(oo / occ * 100, 1) if occ else None
                vu, hu = td.get("vacant_units") or 0, td.get("housing_units") or 0
                td["vacancy_pct"] = round(vu / hu * 100, 1) if hu else None
                out["neighbor_tracts"].append({
                    "fips":             f"{st_f}{co_f}{tr_f}",
                    "name":             rec.get("NAME"),
                    "data":             td,
                    "geometry":         geom,
                    "in_school_district": in_district,
                })
            except Exception: continue
        if out["neighbor_tracts"]:
            out["sources"].append(f"TIGERweb tract polygons + ACS for {len(out['neighbor_tracts'])} neighbor tracts")
    except Exception as e:
        out["caveats"].append(f"Neighbor tract search failed: {e}")

    # --- 3) Aggregated tract summaries (population-weighted means) ---
    def _summarize(tract_list):
        if not tract_list: return {}
        sums = {}
        pop_weighted = {}
        total_pop = 0
        for t in tract_list:
            d = t["data"]
            pop = d.get("population") or 0
            total_pop += pop
            for k, v in d.items():
                if v is None: continue
                if k in ("population", "households", "housing_units", "occupied_units",
                         "vacant_units", "owner_occupied", "renter_occupied", "employed"):
                    sums[k] = sums.get(k, 0) + v
                elif pop > 0:
                    pop_weighted.setdefault(k, []).append((pop, v))
        summary = dict(sums)
        for k, pairs in pop_weighted.items():
            total_w = sum(p for p, v in pairs)
            if total_w:
                summary[k] = round(sum(p * v for p, v in pairs) / total_w, 2)
        summary["tract_count"] = len(tract_list)
        summary["total_population"] = total_pop
        return summary

    out["tract_summary"] = _summarize(out["tracts"])
    # Wider summary now respects school-district filter: containing tracts
    # (always counted) + neighbors INSIDE the same district. If we have no
    # district polygons, fall back to all neighbors so we still show something.
    in_district_neighbors = [t for t in out["neighbor_tracts"]
                              if t.get("in_school_district", True)]
    out["wider_summary"] = _summarize(out["tracts"] + in_district_neighbors)
    out["filter_applied"] = ("same school district" if sd_polygons else
                              f"within {radius_mi} mi (no school district detected)")
    out["wider_tract_count_in_district"] = len(in_district_neighbors) + len(out["tracts"])
    out["wider_tract_count_total"] = len(out["neighbor_tracts"]) + len(out["tracts"])

    # --- 4) Caveats for what we DON'T have ---
    out["caveats"].extend([
        "Submarket new-home metrics (absorption, avg pricing/SF by community) require a Zonda or similar subscription.",
        "Census ACS tract data refreshes annually (5-year estimates); HAR MLS data would be more current at submarket level.",
    ])
    return jsonify(out)


@acq_bp.route("/api/acq/projects/<pid>/market", methods=["GET"])
@_login_required
def acq_api_projects_market(pid):
    """Pull free public market data around the project: population, employment,
    median income, age distribution from the Census Bureau ACS 5-year API.

    No API key required for Census. Identifies the primary county the project
    sits in (via the cached counties layer) and fetches county-level stats.
    """
    import requests as _rq
    from shapely.geometry import shape as shp_shape
    from shapely.ops import unary_union

    guard = _acq_guard()
    if guard:
        return guard
    uid = _acq_owner()
    conn = get_db()
    try:
        proj = acq_store.get_object(conn, "project", pid, uid, _acq_is_admin())
    finally:
        conn.close()
    if not proj:
        return jsonify({"error": "project not found"}), 404

    # Use project union centroid to identify the county
    geoms = []
    for t in proj.get("tracts") or []:
        g = t.get("geometry")
        if g:
            try: geoms.append(shp_shape(g))
            except Exception as e: print(f"[{request.endpoint}] tract geometry skipped: {e}", flush=True)
    if not geoms:
        return jsonify({"error": "no tract geometries"}), 400
    proj_union = unary_union(geoms)
    centroid = proj_union.centroid

    # Find county FIPS by spatial lookup against the counties cached layer
    try:
        counties_fc = cached_layer_query("counties", proj_union)
        county_name = None
        county_fips = None
        for f in counties_fc.get("features", []):
            try:
                if shp_shape(f["geometry"]).contains(centroid):
                    props = f.get("properties") or {}
                    county_name = props.get("BASENAME") or props.get("NAME")
                    county_fips = props.get("GEOID") or props.get("COUNTYFP")
                    break
            except Exception: continue
    except Exception as e:
        return jsonify({"error": f"county lookup failed: {e}"}), 500

    if not county_fips:
        return jsonify({"error": "Could not identify county for this project"}), 404

    # Normalize FIPS — Census ACS needs state + county codes separately
    # county_fips might be 5-digit (state+county) or 3-digit (county only)
    fips_str = str(county_fips).zfill(5) if len(str(county_fips)) <= 5 else str(county_fips)
    if len(fips_str) == 5:
        state_fips = fips_str[:2]
        county_fips_only = fips_str[2:]
    else:
        # Default to Texas (48) if we only got county
        state_fips = "48"
        county_fips_only = fips_str.zfill(3)

    # Census ACS 5-year — most recent available. Expanded to 16 variables
    # covering population/income/housing/education/age structure.
    YEAR = _acs_year()
    VARS = {
        # --- Population / households ---
        "B01003_001E": "population",
        "B01002_001E": "median_age",
        "B11001_001E": "households",
        "B11016_001E": "family_households",
        "B25010_001E": "avg_household_size",
        # --- Income ---
        "B19013_001E": "median_household_income",
        "B19301_001E": "per_capita_income",
        # --- Housing stock & tenure ---
        "B25001_001E": "housing_units",
        "B25002_002E": "occupied_units",
        "B25002_003E": "vacant_units",
        "B25003_002E": "owner_occupied",
        "B25003_003E": "renter_occupied",
        "B25077_001E": "median_home_value",
        "B25064_001E": "median_rent",
        # --- Labor ---
        "B23025_004E": "employed",
        # --- Education (% bachelor's+ derived from age 25+ population) ---
        "B15003_022E": "bachelors_only",
        "B15003_023E": "masters",
        "B15003_024E": "professional",
        "B15003_025E": "doctorate",
        "B15003_001E": "education_universe",
    }
    url = f"https://api.census.gov/data/{YEAR}/acs/acs5"
    var_list = ",".join(["NAME"] + list(VARS.keys()))
    try:
        census_params = {
            "get": var_list,
            "for": f"county:{county_fips_only}",
            "in":  f"state:{state_fips}",
        }
        # Census now enforces keys above ~500 requests/day per IP.
        # CENSUS_API_KEY is free at https://api.census.gov/data/key_signup.html
        census_key = os.environ.get("CENSUS_API_KEY", "").strip()
        if census_key:
            census_params["key"] = census_key
        r = _rq.get(url, params=census_params, timeout=15)
        if r.status_code != 200:
            # Census returns 204 No Content for missing-data lookups, or HTML
            # error pages for malformed queries. Surface a useful message
            # instead of letting r.json() blow up with "Expecting value".
            return jsonify({"error": f"Census ACS HTTP {r.status_code} for state={state_fips} county={county_fips_only}. "
                                       f"Body: {r.text[:200]}"}), 502
        try:
            rows = r.json()
        except ValueError:
            return jsonify({"error": f"Census ACS returned non-JSON for state={state_fips} county={county_fips_only}. "
                                       f"Body: {r.text[:200]}"}), 502
        if not isinstance(rows, list) or len(rows) < 2:
            return jsonify({"error": f"Census ACS returned no data for state={state_fips} county={county_fips_only}. "
                                       f"This is normal for some smaller counties.",
                            "year": YEAR}), 502
        headers, data = rows[0], rows[1]
        record = dict(zip(headers, data))
        def _to_int(v):
            try:
                # Handle negative numbers (e.g. "-666666666" Census null sentinels)
                if v is None or v == "" or str(v).startswith("-666666"): return None
                return int(v)
            except (ValueError, TypeError): return None
        current = {label: _to_int(record.get(var)) for var, label in VARS.items()}
        # Derived: bachelor's degree or higher (%) — sum of bachelor's/master's/
        # professional/doctorate, divided by the universe (age 25+ population).
        edu_total = sum(filter(None, [current.get(k) for k in
                                       ("bachelors_only", "masters", "professional", "doctorate")]))
        edu_uni = current.get("education_universe") or 0
        current["pct_bachelors_or_higher"] = round(edu_total / edu_uni * 100, 1) if edu_uni else None
        # Derived: owner-occupancy rate
        oo = current.get("owner_occupied") or 0
        occ = current.get("occupied_units") or 0
        current["owner_occupancy_pct"] = round(oo / occ * 100, 1) if occ else None
        # Derived: vacancy rate
        vu = current.get("vacant_units") or 0
        hu = current.get("housing_units") or 0
        current["vacancy_pct"] = round(vu / hu * 100, 1) if hu else None
        current["county_name"] = record.get("NAME")
        current["year"] = YEAR
    except Exception as e:
        return jsonify({"error": f"Census ACS fetch failed: {type(e).__name__}: {e}"}), 502

    # 5-year history for growth trends
    history = []
    for yr in range(YEAR - 4, YEAR + 1):
        try:
            hist_params = {
                "get": "B01003_001E,B23025_004E,B25077_001E",   # pop, employed, home value
                "for": f"county:{county_fips_only}",
                "in":  f"state:{state_fips}",
            }
            if census_key:
                hist_params["key"] = census_key
            r = _rq.get(f"https://api.census.gov/data/{yr}/acs/acs5", params=hist_params, timeout=10)
            if r.status_code == 200:
                rows = r.json()
                if len(rows) >= 2:
                    d = dict(zip(rows[0], rows[1]))
                    history.append({
                        "year":       yr,
                        "population": int(d["B01003_001E"]) if d.get("B01003_001E", "").lstrip("-").isdigit() else None,
                        "employed":   int(d["B23025_004E"]) if d.get("B23025_004E", "").lstrip("-").isdigit() else None,
                        "home_value": int(d["B25077_001E"]) if d.get("B25077_001E", "").lstrip("-").isdigit() else None,
                    })
        except Exception: continue

    # Compute YoY growth rates from history
    growth = {}
    if len(history) >= 2:
        first, last = history[0], history[-1]
        years_span = last["year"] - first["year"]
        if years_span > 0:
            for k in ("population", "employed", "home_value"):
                if first.get(k) and last.get(k) and first[k] > 0:
                    cagr = ((last[k] / first[k]) ** (1 / years_span) - 1) * 100
                    growth[k + "_cagr_pct"] = round(cagr, 2)
                    growth[k + "_total_pct"] = round((last[k] / first[k] - 1) * 100, 1)

    return jsonify({
        "county_name": county_name,
        "county_fips": fips_str,
        "current": current,
        "history": history,
        "growth": growth,
        "sources": [
            f"Census Bureau ACS 5-year ({YEAR}, with {YEAR-4}-{YEAR} history)",
        ],
    })


@acq_bp.route("/api/acq/projects/<pid>/fred", methods=["GET"])
@_login_required
def acq_api_projects_fred(pid):
    """Pull Houston-MSA + Texas + national economic indicators from FRED.

    Requires FRED_API_KEY env var. Returns each series with current value,
    prior-year value, 5-year CAGR, and a short time series for sparkline rendering.
    """
    import requests as _rq

    api_key = os.environ.get("FRED_API_KEY", "").strip()
    if not api_key:
        return jsonify({"error": "FRED_API_KEY not configured on the server. "
                                  "Add it in Railway Variables (free at fred.stlouisfed.org)."
                        }), 503

    guard = _acq_guard()
    if guard:
        return guard
    uid = _acq_owner()
    conn = get_db()
    try:
        proj = acq_store.get_object(conn, "project", pid, uid, _acq_is_admin())
    finally:
        conn.close()
    if not proj:
        return jsonify({"error": "project not found"}), 404

    def fetch_series(series_id):
        """Return list of recent observations + computed stats."""
        try:
            r = _rq.get("https://api.stlouisfed.org/fred/series/observations",
                        params={
                            "series_id":            series_id,
                            "api_key":              api_key,
                            "file_type":            "json",
                            "sort_order":           "desc",
                            "limit":                60,    # ~5 years of monthly data
                        }, timeout=12)
            r.raise_for_status()
            obs = r.json().get("observations", [])
            obs = [{"date": o["date"], "value": (float(o["value"]) if o["value"] not in (".", None) else None)}
                    for o in obs]
            obs = [o for o in obs if o["value"] is not None]
            if not obs:
                return {"error": "no observations"}
            current = obs[0]
            # Find a value ~1 year ago and ~5 years ago for trend
            from datetime import datetime as _dt
            current_dt = _dt.strptime(current["date"], "%Y-%m-%d")
            year_ago = next((o for o in obs
                              if (current_dt - _dt.strptime(o["date"], "%Y-%m-%d")).days >= 360), None)
            five_year_ago = next((o for o in obs
                                   if (current_dt - _dt.strptime(o["date"], "%Y-%m-%d")).days >= 1800), None)

            stats = {"current": current["value"], "current_date": current["date"]}
            if year_ago:
                stats["yoy_pct"] = round((current["value"] / year_ago["value"] - 1) * 100, 2) \
                                    if year_ago["value"] != 0 else None
                stats["year_ago_value"] = year_ago["value"]
                stats["year_ago_date"] = year_ago["date"]
            if five_year_ago and five_year_ago["value"] > 0:
                years = (current_dt - _dt.strptime(five_year_ago["date"], "%Y-%m-%d")).days / 365.25
                if years > 0:
                    stats["five_year_cagr_pct"] = round(
                        ((current["value"] / five_year_ago["value"]) ** (1/years) - 1) * 100, 2)
                    stats["five_year_total_pct"] = round(
                        (current["value"] / five_year_ago["value"] - 1) * 100, 1)

            # Sparkline: last 24 observations in chronological order (oldest first)
            sparkline = list(reversed(obs[:24]))
            stats["sparkline"] = [{"date": o["date"], "value": o["value"]} for o in sparkline]
            return stats
        except Exception as e:
            return {"error": str(e)[:200]}

    out = []
    # Run fetches in parallel — FRED limits to 120/min, we're doing 8.
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = {pool.submit(fetch_series, sid): (sid, label, fmt)
                    for sid, label, fmt in _FRED_HOUSTON_SERIES}
        for fut in futures:
            sid, label, fmt = futures[fut]
            try:
                data = fut.result()
            except Exception as e:
                data = {"error": str(e)[:200]}
            out.append({
                "series_id": sid,
                "label":     label,
                "format":    fmt,
                "data":      data,
            })
    return jsonify({
        "indicators": out,
        "source":     "Federal Reserve Bank of St. Louis (FRED)",
        "msa":        "Houston-The Woodlands-Sugar Land MSA (26420)",
        "notes":      "All FRED series IDs included for verification — query directly on fred.stlouisfed.org.",
    })


@acq_bp.route("/api/acq/projects/<pid>/pdf")
@_login_required
def acq_api_projects_pdf(pid):
    """Generate a presentation-grade Acquisitions Summary PDF.

    Multi-page report with:
      • Cover page
      • Executive summary + project map image
      • Constraint bar chart + table
      • Yield analysis
      • Elevation + topography
      • Market context (Census + FRED)
      • Tract roster
      • Sources / methodology

    All charts rendered via matplotlib (Agg backend), then embedded in
    reportlab. Uses cached analysis (must run /analyze first) and refetches
    elevation/market/FRED data live.
    """
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import matplotlib.patches as mpatches
    from matplotlib.patches import Polygon as MplPoly
    from reportlab.lib.pagesizes import letter
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.units import inch
    from reportlab.lib import colors
    from reportlab.platypus import (SimpleDocTemplate, Paragraph, Spacer, Table,
                                       TableStyle, PageBreak, Image as RLImage,
                                       KeepTogether)
    from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_RIGHT
    from shapely.geometry import shape as shp_shape, mapping as shp_mapping
    from shapely.ops import unary_union
    import requests as _rq

    guard = _acq_guard()
    if guard:
        return guard
    uid = _acq_owner()
    conn = get_db()
    try:
        proj = acq_store.get_object(conn, "project", pid, uid, _acq_is_admin())
    finally:
        conn.close()
    if not proj:
        return jsonify({"error": "project not found"}), 404

    analysis = proj.get("analysis_cache")
    if not analysis:
        return jsonify({"error": "Run the analysis first (click 'Run Acquisition Analysis' on the project page)."}), 400

    # Ember brand
    EMBER_ORANGE = "#F25929"
    EMBER_BLUE   = "#13344E"
    GRAY_700     = "#6B7B8B"
    GRAY_300     = "#DDE3E8"
    GRAY_100     = "#F6F8FA"
    OK_GREEN     = "#2E7D32"
    WARN_RED     = "#C62828"

    def _save_fig_to_buf(fig):
        b = io.BytesIO()
        fig.savefig(b, format="png", dpi=150, bbox_inches="tight",
                    facecolor="white", edgecolor="none")
        plt.close(fig)
        b.seek(0)
        return b

    # ===== Chart 1: Project map (with satellite tiles + constraints overlay) =====
    def render_project_map(proj_union, constraint_geoms=None, tracts_list=None):
        from shapely.geometry import MultiPolygon, Polygon as SP, LineString as LS
        from shapely.geometry import MultiLineString as MLS
        import math
        fig, ax = plt.subplots(figsize=(7.5, 5.5), dpi=160)
        ax.set_facecolor("#F0F4F8")

        # Bounds + padding
        minx, miny, maxx, maxy = proj_union.bounds
        pad_x = (maxx - minx) * 0.20 or 0.01
        pad_y = (maxy - miny) * 0.20 or 0.01
        west, east   = minx - pad_x, maxx + pad_x
        south, north = miny - pad_y, maxy + pad_y

        # Try to fetch a satellite tile basemap via Esri World Imagery — a simple
        # XYZ tile composite. Skips silently on failure (no internet, throttling).
        try:
            from PIL import Image as _PILImage
            import math as _math
            def lonlat_to_tile(lon, lat, z):
                lat_r = _math.radians(lat)
                n = 2 ** z
                xt = int((lon + 180) / 360 * n)
                yt = int((1 - _math.log(_math.tan(lat_r) + 1/_math.cos(lat_r)) / _math.pi) / 2 * n)
                return xt, yt
            def tile_to_lonlat(x, y, z):
                n = 2 ** z
                lon = x / n * 360 - 180
                lat = _math.degrees(_math.atan(_math.sinh(_math.pi * (1 - 2 * y / n))))
                return lon, lat
            # Choose zoom that gives ~6-12 tiles across — readable but not too many fetches
            zoom = 12
            while zoom > 6:
                x0, y0 = lonlat_to_tile(west, north, zoom)
                x1, y1 = lonlat_to_tile(east, south, zoom)
                if (x1 - x0 + 1) * (y1 - y0 + 1) <= 30: break
                zoom -= 1
            x0, y0 = lonlat_to_tile(west, north, zoom)
            x1, y1 = lonlat_to_tile(east, south, zoom)
            cols = x1 - x0 + 1
            rows = y1 - y0 + 1
            composite = _PILImage.new("RGB", (cols * 256, rows * 256))
            for tx in range(x0, x1 + 1):
                for ty in range(y0, y1 + 1):
                    try:
                        url = f"https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{zoom}/{ty}/{tx}"
                        tr = requests.get(url, timeout=5)
                        if tr.status_code == 200:
                            tile_img = _PILImage.open(io.BytesIO(tr.content))
                            composite.paste(tile_img, ((tx - x0) * 256, (ty - y0) * 256))
                    except Exception: continue
            # Compute the geographic bounds of the composite
            lon_w, lat_n = tile_to_lonlat(x0, y0, zoom)
            lon_e, lat_s = tile_to_lonlat(x1 + 1, y1 + 1, zoom)
            ax.imshow(composite, extent=(lon_w, lon_e, lat_s, lat_n), aspect="auto", zorder=0)
        except Exception:
            pass

        # Constraint overlays (drawn UNDER tract boundary so the tract pops)
        if constraint_geoms:
            from shapely.geometry import shape as _shp
            cstyles = {
                "floodplain":   ("#3380ff", "#0040aa", 0.35, 1.0, "polygon"),
                "wetlands":     ("#33a06f", "#005c2e", 0.40, 1.0, "polygon"),
                "transmission": ("#9c27b0", "#9c27b0", 0,    2.5, "line"),
                "streams":      ("#1976d2", "#1976d2", 0,    1.5, "line"),
                "pipelines":    ("#F25929", "#F25929", 0,    2.0, "line"),
            }
            for key, fc in (constraint_geoms or {}).items():
                if not fc: continue
                fillc, edgec, alpha, lw, kind = cstyles.get(key, ("#888", "#444", 0.3, 1, "polygon"))
                feats = fc.get("features") if "features" in fc else [fc]
                for feat in feats:
                    g = feat.get("geometry") if isinstance(feat, dict) else None
                    if not g: continue
                    try:
                        s = _shp(g)
                        if s.geom_type == "Polygon":
                            xs, ys = s.exterior.xy
                            ax.fill(xs, ys, color=fillc, alpha=alpha, edgecolor=edgec, linewidth=0.8, zorder=2)
                        elif s.geom_type == "MultiPolygon":
                            for sp in s.geoms:
                                xs, ys = sp.exterior.xy
                                ax.fill(xs, ys, color=fillc, alpha=alpha, edgecolor=edgec, linewidth=0.8, zorder=2)
                        elif s.geom_type == "LineString":
                            xs, ys = s.xy
                            ax.plot(xs, ys, color=edgec, linewidth=lw, alpha=0.85, zorder=3)
                        elif s.geom_type == "MultiLineString":
                            for sl in s.geoms:
                                xs, ys = sl.xy
                                ax.plot(xs, ys, color=edgec, linewidth=lw, alpha=0.85, zorder=3)
                    except Exception: continue

        # Individual tract boundaries (so user can see assemblage)
        if tracts_list:
            from shapely.geometry import shape as _shp
            for t in tracts_list:
                if not t.get("geometry"): continue
                try:
                    s = _shp(t["geometry"])
                    if s.geom_type == "Polygon":
                        xs, ys = s.exterior.xy
                        ax.plot(xs, ys, color="#FFFF00", linewidth=1.0, alpha=0.7, zorder=4)
                    elif s.geom_type == "MultiPolygon":
                        for sp in s.geoms:
                            xs, ys = sp.exterior.xy
                            ax.plot(xs, ys, color="#FFFF00", linewidth=1.0, alpha=0.7, zorder=4)
                except Exception: continue

        # PROJECT UNION boundary (bold orange)
        def _add_poly_outline(p):
            xs, ys = p.exterior.xy
            ax.fill(xs, ys, color=EMBER_ORANGE, alpha=0.18, zorder=5)
            ax.plot(xs, ys, color=EMBER_ORANGE, linewidth=3.0, zorder=6)
        if isinstance(proj_union, MultiPolygon):
            for p in proj_union.geoms: _add_poly_outline(p)
        elif isinstance(proj_union, SP):
            _add_poly_outline(proj_union)

        ax.set_xlim(west, east)
        ax.set_ylim(south, north)
        ax.set_aspect("equal")

        # N arrow + scale-ish text
        ax.text(0.97, 0.97, "N\n▲", transform=ax.transAxes, ha="center", va="top",
                fontsize=12, color="#FFFFFF", fontweight="bold",
                bbox=dict(boxstyle="round,pad=0.3", facecolor=EMBER_BLUE, edgecolor="white"))

        ax.tick_params(labelsize=7, colors=GRAY_700)
        for spine in ax.spines.values():
            spine.set_edgecolor(GRAY_300)
        ax.set_title(f"Project · {analysis['tract_count']} tract(s) · {analysis['gross_acres']:,.0f} ac gross  ·  Floodplain/Wetlands/Transmission/Streams/Pipelines overlaid",
                      fontsize=9, color=EMBER_BLUE, pad=10, fontweight="bold")
        ax.set_xlabel("")
        ax.set_ylabel("")
        return _save_fig_to_buf(fig)

    # ===== Chart 2: Constraints bar chart =====
    def render_constraints_chart(constraints, gross):
        fig, ax = plt.subplots(figsize=(7.0, 3.0), dpi=150)
        labels = ["Floodplain", "Wetlands", "Trans. ROW", "Streams\n(50ft)", "Pipelines\n(50ft)"]
        keys = ["floodplain", "wetlands", "transmission_row", "stream_buffers", "pipeline_easements"]
        acres = []
        for k in keys:
            c = constraints.get(k) or {}
            acres.append(0 if c.get("error") else c.get("acres", 0))
        pcts = [(a / gross * 100) if gross > 0 else 0 for a in acres]

        bars = ax.barh(labels, acres, color=EMBER_BLUE, edgecolor=EMBER_ORANGE, linewidth=1.0, height=0.6)
        for bar, a, p in zip(bars, acres, pcts):
            width = bar.get_width()
            ax.text(width + max(acres) * 0.01, bar.get_y() + bar.get_height()/2,
                    f"  {a:,.1f} ac ({p:.1f}%)", va="center", fontsize=9, color=EMBER_BLUE)
        ax.invert_yaxis()
        ax.set_xlabel("Acres affected", fontsize=9, color=GRAY_700)
        ax.tick_params(labelsize=9, colors=GRAY_700)
        ax.set_xlim(0, max(max(acres) * 1.40, 1))
        for spine in ["top", "right"]:
            ax.spines[spine].set_visible(False)
        for spine in ["bottom", "left"]:
            ax.spines[spine].set_edgecolor(GRAY_300)
        ax.set_title("Net-out constraints (overlapping geometries counted once in net developable)",
                      fontsize=10, color=EMBER_BLUE, pad=8, fontweight="bold")
        return _save_fig_to_buf(fig)

    # ===== Chart 3: Gross → Net developable donut =====
    def render_developable_donut(gross, net):
        fig, ax = plt.subplots(figsize=(3.0, 3.0), dpi=150)
        unavail = max(gross - net, 0)
        if gross > 0:
            sizes = [net, unavail]
            labels = [f"Developable\n{net:,.0f} ac", f"Constrained\n{unavail:,.0f} ac"]
            colors_pie = [OK_GREEN, GRAY_300]
            wedges, _ = ax.pie(sizes, colors=colors_pie, startangle=90, counterclock=False,
                                wedgeprops={"width": 0.35, "edgecolor": "white", "linewidth": 2})
            pct = (net / gross * 100)
            ax.text(0, 0.05, f"{pct:.0f}%", ha="center", va="center",
                    fontsize=22, color=EMBER_ORANGE, fontweight="bold")
            ax.text(0, -0.18, "developable", ha="center", va="center",
                    fontsize=8, color=GRAY_700)
            # legend at bottom
            ax.legend(wedges, labels, loc="upper center", bbox_to_anchor=(0.5, -0.05),
                       ncol=2, frameon=False, fontsize=8)
        ax.set_aspect("equal")
        return _save_fig_to_buf(fig)

    # ===== Chart 4: FRED trend chart (Houston HPI + Texas HPI + Case-Shiller) =====
    def render_hpi_trend(fred_data):
        if not fred_data: return None
        fig, ax = plt.subplots(figsize=(7.0, 3.0), dpi=150)
        plotted_any = False
        for indicator, color, label in [
            ("ATNHPIUS26420Q", EMBER_ORANGE, "Houston HPI"),
            ("TXSTHPI",         EMBER_BLUE,   "Texas HPI"),
            ("CSUSHPINSA",      GRAY_700,     "U.S. 20-city HPI"),
        ]:
            ind = next((x for x in fred_data.get("indicators", [])
                        if x.get("series_id") == indicator), None)
            if not ind or ind.get("data", {}).get("error"): continue
            sparkline = ind["data"].get("sparkline") or []
            if len(sparkline) < 2: continue
            xs = list(range(len(sparkline)))
            ys = [p["value"] for p in sparkline]
            ax.plot(xs, ys, color=color, linewidth=2.0, label=label, marker="o", markersize=2)
            plotted_any = True
        if not plotted_any: return None
        ax.set_title("Home Price Index — 24-period trend (FRED)",
                      fontsize=10, color=EMBER_BLUE, fontweight="bold", pad=8)
        ax.tick_params(labelsize=8, colors=GRAY_700)
        ax.legend(loc="upper left", fontsize=8, frameon=False)
        ax.grid(True, linestyle=":", color=GRAY_300, linewidth=0.5)
        for spine in ["top", "right"]:
            ax.spines[spine].set_visible(False)
        for spine in ["bottom", "left"]:
            ax.spines[spine].set_edgecolor(GRAY_300)
        ax.set_xlabel("Periods (most-recent first → newest right)", fontsize=8, color=GRAY_700)
        ax.set_ylabel("Index value", fontsize=8, color=GRAY_700)
        return _save_fig_to_buf(fig)

    # ===== Gather all data (analysis is cached; elevation/market/FRED fetched fresh) =====
    tract_geoms = []
    for t in proj.get("tracts") or []:
        g = t.get("geometry")
        if g:
            try: tract_geoms.append(shp_shape(g))
            except Exception as e: print(f"[{request.endpoint}] tract geometry skipped: {e}", flush=True)
    proj_union = unary_union(tract_geoms) if tract_geoms else None

    # These four cards are their own endpoints. The standalone app called them
    # by building synthetic WSGI environments and pushing request contexts,
    # with a dead no-op block above it - all of that is unnecessary here. We
    # are already inside a request with a valid session, so they can simply be
    # called, and each is wrapped so one failing card cannot lose the report.
    def _card(fn, label, *a):
        try:
            resp = fn(*a)
            if isinstance(resp, tuple):
                resp = resp[0]
            return resp.get_json() if hasattr(resp, "get_json") else None
        except Exception as e:
            print(f"[acq-pdf] {label} card skipped: {type(e).__name__}: {e}", flush=True)
            return {"error": f"{label} skipped: {e}"}

    elev_data = (_card(acq_api_elevation_profile, "elevation",
                       shp_mapping(proj_union))
                 if proj_union else None)
    market_data = _card(acq_api_projects_market, "market", pid)
    fred_data = _card(acq_api_projects_fred, "FRED", pid)

    submarket_data = _card(acq_api_projects_submarket, "submarket", pid)

    # ===== Build PDF =====
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=letter,
                              leftMargin=0.6*inch, rightMargin=0.6*inch,
                              topMargin=0.6*inch, bottomMargin=0.6*inch,
                              title=f"{proj.get('name','Project')} — Acquisitions Summary",
                              author="Ember Acquisitions")

    styles = getSampleStyleSheet()
    body = ParagraphStyle('body', parent=styles['Normal'], fontSize=10, leading=14)
    body_center = ParagraphStyle('body_c', parent=body, alignment=TA_CENTER)
    title_xl = ParagraphStyle('title_xl', parent=styles['Heading1'], fontSize=32,
                                 textColor=colors.HexColor(EMBER_ORANGE),
                                 alignment=TA_CENTER, spaceAfter=8, leading=38)
    cover_sub = ParagraphStyle('cover_sub', parent=styles['Heading2'], fontSize=14,
                                  textColor=colors.HexColor(EMBER_BLUE), alignment=TA_CENTER,
                                  spaceAfter=4)
    h1 = ParagraphStyle('h1', parent=styles['Heading1'], fontSize=18,
                          textColor=colors.HexColor(EMBER_ORANGE),
                          spaceBefore=4, spaceAfter=8)
    h2 = ParagraphStyle('h2', parent=styles['Heading2'], fontSize=13,
                          textColor=colors.HexColor(EMBER_BLUE),
                          spaceBefore=12, spaceAfter=6)
    label = ParagraphStyle('label', parent=styles['Normal'], fontSize=8,
                              textColor=colors.HexColor(GRAY_700), leading=11)
    callout = ParagraphStyle('callout', parent=body, fontSize=11,
                                textColor=colors.HexColor(EMBER_BLUE), leftIndent=10,
                                spaceBefore=6, spaceAfter=6,
                                borderColor=colors.HexColor(EMBER_ORANGE), borderWidth=0,
                                leftBorderColor=colors.HexColor(EMBER_ORANGE),
                                leftBorderWidth=3, leftBorderPadding=6)

    def section_break(s):
        return [PageBreak()]

    def kpi_box(label_str, value_str, color=EMBER_BLUE):
        return [
            [Paragraph(f'<font size=8 color="{GRAY_700}">{label_str}</font>', label)],
            [Paragraph(f'<font size=22 color="{color}"><b>{value_str}</b></font>', body)],
        ]

    story = []

    # ============ PAGE 1: COVER ============
    story.append(Spacer(1, 1.4*inch))
    story.append(Paragraph("EMBER ACQUISITIONS", cover_sub))
    story.append(Spacer(1, 0.3*inch))
    story.append(Paragraph(proj.get("name", "(unnamed project)"), title_xl))
    story.append(Paragraph("Acquisitions Summary", cover_sub))
    story.append(Spacer(1, 0.5*inch))

    # Headline KPIs (centered)
    kpi_data = [
        [
            Paragraph(f'<para alignment="center"><font size=9 color="{GRAY_700}">GROSS</font><br/><font size=22 color="{EMBER_BLUE}"><b>{analysis["gross_acres"]:,.0f}</b></font><br/><font size=9 color="{GRAY_700}">acres</font></para>', body),
            Paragraph(f'<para alignment="center"><font size=9 color="{GRAY_700}">DEVELOPABLE</font><br/><font size=22 color="{EMBER_ORANGE}"><b>{analysis["net_developable_acres"]:,.0f}</b></font><br/><font size=9 color="{GRAY_700}">{analysis["net_developable_pct"]:.0f}% of gross</font></para>', body),
            Paragraph(f'<para alignment="center"><font size=9 color="{GRAY_700}">EST. LOTS</font><br/><font size=22 color="{EMBER_BLUE}"><b>{analysis.get("yield_estimates",{}).get("total_lots",0):,}</b></font><br/><font size=9 color="{GRAY_700}">@ {analysis.get("yield_estimates",{}).get("assumptions",{}).get("units_per_acre",3.5):.1f} units/ac</font></para>', body),
            Paragraph(f'<para alignment="center"><font size=9 color="{GRAY_700}">TRACTS</font><br/><font size=22 color="{EMBER_BLUE}"><b>{analysis["tract_count"]}</b></font><br/><font size=9 color="{GRAY_700}">combined</font></para>', body),
        ]
    ]
    kt = Table(kpi_data, colWidths=[1.6*inch]*4, rowHeights=[1.2*inch])
    kt.setStyle(TableStyle([
        ('BOX',         (0,0), (-1,-1), 1.0, colors.HexColor(GRAY_300)),
        ('INNERGRID',   (0,0), (-1,-1), 0.5, colors.HexColor(GRAY_300)),
        ('BACKGROUND',  (0,0), (-1,-1), colors.HexColor(GRAY_100)),
        ('VALIGN',      (0,0), (-1,-1), 'MIDDLE'),
    ]))
    story.append(kt)
    story.append(Spacer(1, 1.0*inch))
    story.append(Paragraph(f'<para alignment="center"><font size=10 color="{GRAY_700}">Prepared {datetime.now().strftime("%B %d, %Y")}</font></para>', body))
    story.append(PageBreak())

    # ============ PAGE 2: EXECUTIVE SUMMARY (Map + Donut) ============
    story.append(Paragraph("Executive Summary", h1))

    if proj_union:
        try:
            map_buf = render_project_map(
                proj_union,
                constraint_geoms=analysis.get("constraint_geoms"),
                tracts_list=proj.get("tracts") or [],
            )
            story.append(RLImage(map_buf, width=7.2*inch, height=5.3*inch))
        except Exception as e:
            story.append(Paragraph(f"<i>Map render failed: {e}</i>", label))

    # Side-by-side: donut + key callouts
    try:
        donut_buf = render_developable_donut(analysis["gross_acres"], analysis["net_developable_acres"])
        donut_img = RLImage(donut_buf, width=2.6*inch, height=2.6*inch)
    except Exception:
        donut_img = Paragraph("", body)

    callouts_html = []
    # Build narrative callouts based on the data
    if analysis["net_developable_pct"] >= 75:
        callouts_html.append(f"<b>Clean site.</b> {analysis['net_developable_pct']:.0f}% of gross acres are unconstrained — minimal floodplain/wetland deductions.")
    elif analysis["net_developable_pct"] >= 50:
        callouts_html.append(f"<b>Moderate constraints.</b> {(100-analysis['net_developable_pct']):.0f}% of acreage is encumbered. Confirm constraint geometry before underwriting.")
    else:
        callouts_html.append(f"<b>Heavy constraints.</b> Only {analysis['net_developable_pct']:.0f}% of gross is developable. Validate density assumptions against zoning + drainage requirements.")

    c = analysis.get("constraints", {})
    if (c.get("floodplain") or {}).get("pct", 0) > 10:
        callouts_html.append(f"<b>{c['floodplain']['pct']:.0f}% in 100-yr floodplain.</b> Significant fill/grading or out-conveyance needed.")
    if (c.get("wetlands") or {}).get("pct", 0) > 5:
        callouts_html.append(f"<b>{c['wetlands']['pct']:.0f}% wetlands (NWI).</b> Confirm USACE jurisdictional status before disturbance.")
    if (c.get("transmission_row") or {}).get("line_count", 0) > 0:
        callouts_html.append(f"<b>{c['transmission_row']['line_count']} transmission line(s)</b> cross the project — 150ft ROW assumed.")
    if (c.get("pipeline_easements") or {}).get("pipeline_count", 0) > 0:
        callouts_html.append(f"<b>{c['pipeline_easements']['pipeline_count']} pipeline(s)</b> cross the project — verify easement widths against RRC records.")

    callout_para = "".join([f"• {x}<br/><br/>" for x in callouts_html])
    callout_cell = Paragraph(callout_para, callout)

    exec_table = Table([[donut_img, callout_cell]], colWidths=[2.8*inch, 4.4*inch])
    exec_table.setStyle(TableStyle([
        ('VALIGN', (0,0), (-1,-1), 'MIDDLE'),
        ('LEFTPADDING',  (0,0), (-1,-1), 0),
        ('RIGHTPADDING', (0,0), (-1,-1), 0),
    ]))
    story.append(exec_table)
    story.append(PageBreak())

    # ============ PAGE 3: CONSTRAINT ANALYSIS ============
    story.append(Paragraph("Constraints & Net Developable", h1))
    story.append(Paragraph(
        f"<b>Gross:</b> {analysis['gross_acres']:,.1f} ac  ·  "
        f"<b>Net developable:</b> <font color=\"{EMBER_ORANGE}\"><b>{analysis['net_developable_acres']:,.1f} ac</b></font>  ·  "
        f"<b>Yield:</b> {analysis['net_developable_pct']:.1f}%",
        body))
    story.append(Spacer(1, 8))

    try:
        chart_buf = render_constraints_chart(analysis.get("constraints", {}), analysis["gross_acres"])
        story.append(RLImage(chart_buf, width=7.0*inch, height=3.0*inch))
    except Exception as e:
        story.append(Paragraph(f"<i>Chart failed: {e}</i>", label))

    constraint_rows = [["Constraint", "Acres", "% of gross", "Detail / assumption"]]
    constraint_labels = {
        "floodplain":         ("FEMA 100-yr floodplain",  "feature_count", "polygons (Zone A/AE/VE)"),
        "wetlands":           ("Wetlands (USFWS NWI)",    "feature_count", "polygons"),
        "transmission_row":   ("Transmission ROW",        "line_count",    "lines × 150ft ROW assumed"),
        "stream_buffers":     ("Stream buffers",          "stream_count",  "NHD streams × 50ft each side"),
        "pipeline_easements": ("Pipeline easements",      "pipeline_count","RRC pipelines × 50ft each side"),
    }
    for key, (label_str, count_key, count_unit) in constraint_labels.items():
        cc = analysis.get("constraints", {}).get(key) or {}
        if cc.get("error"):
            constraint_rows.append([label_str, "—", "—", f"err: {cc['error'][:50]}"])
        else:
            cnt = cc.get(count_key, 0)
            detail = f"{cnt} {count_unit}" if cnt else "none detected"
            constraint_rows.append([label_str, f"{cc.get('acres', 0):,.1f}",
                                     f"{cc.get('pct', 0):.1f}%", detail])
    constraint_rows.append([
        Paragraph(f"<b>Net developable (union of constraints subtracted)</b>", body),
        Paragraph(f"<b>{analysis['net_developable_acres']:,.1f}</b>", body),
        Paragraph(f"<b>{analysis['net_developable_pct']:.1f}%</b>", body),
        "Overlapping constraints counted once",
    ])
    t = Table(constraint_rows, colWidths=[1.9*inch, 1.0*inch, 1.0*inch, 3.0*inch])
    t.setStyle(TableStyle([
        ('BACKGROUND', (0,0), (-1,0), colors.HexColor(EMBER_BLUE)),
        ('TEXTCOLOR',  (0,0), (-1,0), colors.white),
        ('FONTNAME',   (0,0), (-1,0), 'Helvetica-Bold'),
        ('FONTSIZE',   (0,0), (-1,-1), 9),
        ('GRID',       (0,0), (-1,-1), 0.5, colors.HexColor(GRAY_300)),
        ('VALIGN',     (0,0), (-1,-1), 'MIDDLE'),
        ('PADDING',    (0,0), (-1,-1), 5),
        ('BACKGROUND', (0,-1), (-1,-1), colors.HexColor("#FFF4ED")),
    ]))
    story.append(Spacer(1, 8))
    story.append(t)
    story.append(PageBreak())

    # ============ PAGE 4: YIELD ESTIMATES ============
    story.append(Paragraph("Yield (pro-forma)", h1))
    ye = analysis.get("yield_estimates", {})
    breakdown = ye.get("breakdown", [])
    weighted_density = ye.get("weighted_density", 0)
    total_lots = ye.get("total_lots", 0)

    story.append(Paragraph(
        f"Pro-forma yield with multi-product mix · weighted density <b>{weighted_density} u/ac</b> · "
        f"applied to <b>{analysis['net_developable_acres']:,.0f} net developable ac</b>.",
        body))
    story.append(Spacer(1, 12))

    yield_kpi = Table([
        [
            Paragraph(f'<para alignment="center"><font size=9 color="{GRAY_700}">TOTAL LOTS</font><br/><br/>'
                       f'<font size=42 color="{EMBER_ORANGE}"><b>{total_lots:,}</b></font>'
                       f'<br/><font size=10 color="{GRAY_700}">across {len(breakdown)} product type{"s" if len(breakdown)!=1 else ""} · {weighted_density} u/ac weighted</font></para>', body),
        ]
    ], colWidths=[6.8*inch], rowHeights=[1.8*inch])
    yield_kpi.setStyle(TableStyle([
        ('BOX',         (0,0), (-1,-1), 1.0, colors.HexColor(GRAY_300)),
        ('INNERGRID',   (0,0), (-1,-1), 0.5, colors.HexColor(GRAY_300)),
        ('BACKGROUND',  (0,0), (-1,-1), colors.HexColor(GRAY_100)),
        ('VALIGN',      (0,0), (-1,-1), 'MIDDLE'),
    ]))
    story.append(yield_kpi)
    story.append(Spacer(1, 16))

    # Product mix breakdown
    story.append(Paragraph("Product mix", h2))
    mix_rows = [["Product", "Density (u/ac)", "Allocation", "Acres", "Lots"]]
    for b in breakdown:
        mix_rows.append([
            b.get("label", "—"),
            f"{b.get('units_per_acre', 0):.1f}",
            f"{b.get('allocation_pct', 0):.0f}%",
            f"{b.get('acres', 0):,.1f}",
            f"{b.get('lots', 0):,}",
        ])
    mix_rows.append([
        Paragraph("<b>Total</b>", body), "",
        Paragraph(f"<b>{ye.get('total_allocation_pct', 0):.0f}%</b>", body),
        Paragraph(f"<b>{analysis['net_developable_acres']:,.1f}</b>", body),
        Paragraph(f"<b>{total_lots:,}</b>", body),
    ])
    mt = Table(mix_rows, colWidths=[2.4*inch, 1.3*inch, 1.1*inch, 1.0*inch, 1.0*inch])
    mt.setStyle(TableStyle([
        ('BACKGROUND', (0,0), (-1,0),  colors.HexColor(EMBER_BLUE)),
        ('TEXTCOLOR',  (0,0), (-1,0),  colors.white),
        ('FONTNAME',   (0,0), (-1,0),  'Helvetica-Bold'),
        ('FONTSIZE',   (0,0), (-1,-1), 10),
        ('GRID',       (0,0), (-1,-1), 0.5, colors.HexColor(GRAY_300)),
        ('ALIGN',      (1,1), (-1,-1), 'RIGHT'),
        ('PADDING',    (0,0), (-1,-1), 6),
        ('BACKGROUND', (0,-1),(-1,-1), colors.HexColor("#FFF4ED")),
    ]))
    story.append(mt)
    story.append(Spacer(1, 16))

    # Sensitivity table — show what happens at different densities
    story.append(Paragraph("Density sensitivity", h2))
    sens_rows = [["Units per acre", "Estimated lots", "Implied lot size (sqft)"]]
    for try_upa in [2.5, 3.0, 3.5, 4.0, 5.0, 6.0]:
        try_lots = int(round(analysis["net_developable_acres"] * try_upa))
        try_sqft = 43560 / try_upa if try_upa > 0 else 0
        sens_rows.append([f"{try_upa:.1f}", f"{try_lots:,}", f"{try_sqft:,.0f}"])
    st = Table(sens_rows, colWidths=[2.2*inch, 2.2*inch, 2.4*inch])
    st.setStyle(TableStyle([
        ('BACKGROUND', (0,0), (-1,0), colors.HexColor(EMBER_BLUE)),
        ('TEXTCOLOR',  (0,0), (-1,0), colors.white),
        ('FONTNAME',   (0,0), (-1,0), 'Helvetica-Bold'),
        ('FONTSIZE',   (0,0), (-1,-1), 10),
        ('GRID',       (0,0), (-1,-1), 0.5, colors.HexColor(GRAY_300)),
        ('VALIGN',     (0,0), (-1,-1), 'MIDDLE'),
        ('PADDING',    (0,0), (-1,-1), 6),
        ('ALIGN',      (0,0), (-1,-1), 'CENTER'),
    ]))
    story.append(st)
    story.append(PageBreak())

    # ============ PAGE 5: ELEVATION / TOPOGRAPHY ============
    if elev_data and not elev_data.get("error"):
        story.append(Paragraph("Topography & Elevation", h1))
        slope_mean = elev_data.get("slope_mean_pct", 0)
        slope_max  = elev_data.get("slope_max_pct", 0)
        rng_ft     = elev_data.get("range_ft", 0)
        drainage   = elev_data.get("drainage_dir", "—")

        if slope_mean < 0.5:
            slope_label = "very flat — engineered grading required for drainage"
            slope_color = WARN_RED
        elif slope_mean < 2.0:
            slope_label = "gentle — standard grading"
            slope_color = OK_GREEN
        elif slope_mean < 5.0:
            slope_label = "moderate — engineered grading"
            slope_color = "#F59E0B"
        else:
            slope_label = "significant relief — natural drainage; verify floodplain on low side"
            slope_color = WARN_RED

        elev_kpi = Table([
            [
                Paragraph(f'<para alignment="center"><font size=9 color="{GRAY_700}">ELEVATION RANGE</font><br/><font size=18 color="{EMBER_BLUE}"><b>{rng_ft:.0f} ft</b></font><br/><font size=8 color="{GRAY_700}">{elev_data.get("min_ft",0):.0f} – {elev_data.get("max_ft",0):.0f} ft</font></para>', body),
                Paragraph(f'<para alignment="center"><font size=9 color="{GRAY_700}">MEAN SLOPE</font><br/><font size=18 color="{slope_color}"><b>{slope_mean:.2f}%</b></font><br/><font size=8 color="{GRAY_700}">max {slope_max:.2f}%</font></para>', body),
                Paragraph(f'<para alignment="center"><font size=9 color="{GRAY_700}">DRAINAGE</font><br/><font size=14 color="{EMBER_BLUE}"><b>{drainage}</b></font></para>', body),
            ]
        ], colWidths=[2.3*inch]*3, rowHeights=[1.0*inch])
        elev_kpi.setStyle(TableStyle([
            ('BOX',         (0,0), (-1,-1), 0.5, colors.HexColor(GRAY_300)),
            ('INNERGRID',   (0,0), (-1,-1), 0.5, colors.HexColor(GRAY_300)),
            ('BACKGROUND',  (0,0), (-1,-1), colors.HexColor(GRAY_100)),
            ('VALIGN',      (0,0), (-1,-1), 'MIDDLE'),
        ]))
        story.append(elev_kpi)
        story.append(Spacer(1, 6))
        story.append(Paragraph(f"<b>Site character:</b> {slope_label}", callout))

        # ----- Render an elevation heatmap + contours image -----
        try:
            import numpy as _np
            grid = elev_data.get("grid")
            bbox = elev_data.get("bbox")
            if grid and bbox:
                Z = _np.array([[(v if v is not None else _np.nan) for v in row] for row in grid])
                minx, miny, maxx, maxy = bbox
                figmap, axm = plt.subplots(figsize=(7.0, 4.5), dpi=150)
                im = axm.imshow(Z, extent=(minx, maxx, miny, maxy), origin='lower',
                                 cmap='terrain', aspect='auto', alpha=0.85)
                # Contour lines on top
                contours = elev_data.get("contours") or []
                for c in contours:
                    for line in c.get("lines") or []:
                        xs = [p[0] for p in line]
                        ys = [p[1] for p in line]
                        axm.plot(xs, ys, color='black', linewidth=0.5, alpha=0.45)
                # Project boundary outline
                if proj_union:
                    if proj_union.geom_type == 'MultiPolygon':
                        for p in proj_union.geoms:
                            xs, ys = p.exterior.xy
                            axm.plot(xs, ys, color=EMBER_ORANGE, linewidth=2.5)
                    else:
                        xs, ys = proj_union.exterior.xy
                        axm.plot(xs, ys, color=EMBER_ORANGE, linewidth=2.5)
                # High/low markers
                hl = elev_data.get("highest_latlon")
                ll = elev_data.get("lowest_latlon")
                if hl: axm.plot(hl[1], hl[0], 'r^', markersize=10, markeredgecolor='white', markeredgewidth=1)
                if ll: axm.plot(ll[1], ll[0], 'bv', markersize=10, markeredgecolor='white', markeredgewidth=1)
                axm.set_xlabel("Longitude", fontsize=8, color=GRAY_700)
                axm.set_ylabel("Latitude", fontsize=8, color=GRAY_700)
                axm.tick_params(labelsize=7, colors=GRAY_700)
                cbar = figmap.colorbar(im, ax=axm, shrink=0.85)
                cbar.set_label("Elevation (ft)", fontsize=8, color=GRAY_700)
                cbar.ax.tick_params(labelsize=7, colors=GRAY_700)
                axm.set_title(f"Elevation surface · contour interval {elev_data.get('contour_interval_ft', '?')} ft · ▲ high {elev_data.get('highest_ft',0):.0f}ft  ▼ low {elev_data.get('lowest_ft',0):.0f}ft",
                                fontsize=9, color=EMBER_BLUE, pad=8, fontweight="bold")
                story.append(Spacer(1, 8))
                story.append(RLImage(_save_fig_to_buf(figmap), width=7.0*inch, height=4.5*inch))
        except Exception as e:
            story.append(Paragraph(f"<i>Elevation map render failed: {e}</i>", label))

        # ----- N-S and E-W cross-section profiles -----
        try:
            ns = elev_data.get("ns_profile") or []
            ew = elev_data.get("ew_profile") or []
            if ns and ew:
                figcross, (ax_ns, ax_ew) = plt.subplots(1, 2, figsize=(7.0, 2.3), dpi=150)
                ns_x = [p.get("distance_ft", 0) for p in ns]
                ns_y = [p.get("elev_ft") for p in ns]
                ew_x = [p.get("distance_ft", 0) for p in ew]
                ew_y = [p.get("elev_ft") for p in ew]
                ax_ns.fill_between(ns_x, ns_y, color=EMBER_ORANGE, alpha=0.20)
                ax_ns.plot(ns_x, ns_y, color=EMBER_ORANGE, linewidth=1.5)
                ax_ns.set_title("North–South cross-section", fontsize=9, color=EMBER_BLUE, fontweight="bold")
                ax_ew.fill_between(ew_x, ew_y, color=EMBER_BLUE, alpha=0.20)
                ax_ew.plot(ew_x, ew_y, color=EMBER_BLUE, linewidth=1.5)
                ax_ew.set_title("East–West cross-section", fontsize=9, color=EMBER_BLUE, fontweight="bold")
                for ax in (ax_ns, ax_ew):
                    ax.set_xlabel("Distance (ft)", fontsize=7, color=GRAY_700)
                    ax.set_ylabel("Elevation (ft)", fontsize=7, color=GRAY_700)
                    ax.tick_params(labelsize=7, colors=GRAY_700)
                    ax.grid(True, linestyle=":", color=GRAY_300, linewidth=0.5)
                    for spine in ["top", "right"]: ax.spines[spine].set_visible(False)
                figcross.tight_layout()
                story.append(Spacer(1, 6))
                story.append(RLImage(_save_fig_to_buf(figcross), width=7.0*inch, height=2.3*inch))
        except Exception as e:
            pass

        # ----- Hypsometric histogram (acres by elevation band) -----
        try:
            hist = elev_data.get("histogram") or []
            if hist:
                fighist, axh = plt.subplots(figsize=(7.0, 2.2), dpi=150)
                centers = [h.get("center_ft", 0) for h in hist]
                widths = [(h.get("high_ft", 0) - h.get("low_ft", 0)) for h in hist]
                acres = [h.get("acres", 0) for h in hist]
                axh.bar(centers, acres, width=widths, color=EMBER_BLUE, edgecolor="white", linewidth=0.5)
                axh.set_title("Acres by elevation band (hypsometric)", fontsize=9, color=EMBER_BLUE, fontweight="bold")
                axh.set_xlabel("Elevation (ft)", fontsize=7, color=GRAY_700)
                axh.set_ylabel("Acres", fontsize=7, color=GRAY_700)
                axh.tick_params(labelsize=7, colors=GRAY_700)
                axh.grid(True, linestyle=":", color=GRAY_300, linewidth=0.5, axis='y')
                for spine in ["top", "right"]: axh.spines[spine].set_visible(False)
                fighist.tight_layout()
                story.append(Spacer(1, 6))
                story.append(RLImage(_save_fig_to_buf(fighist), width=7.0*inch, height=2.2*inch))
        except Exception:
            pass

        story.append(Spacer(1, 6))
        story.append(Paragraph(
            f"Sampled via USGS 3DEP at {elev_data.get('sample_count', '?')} grid points across the project. "
            f"Median elevation {elev_data.get('median_ft', 0):.0f} ft.",
            label))
        story.append(PageBreak())

    # ============ SUBMARKET PAGE (tract + school district) ============
    if submarket_data and not submarket_data.get("error"):
        story.append(Paragraph("Submarket Profile", h1))
        # School districts
        sds = submarket_data.get("school_districts") or []
        if sds:
            sd_rows = [["School district", "Type", "GEOID"]]
            for sd in sds:
                sd_rows.append([sd.get("name") or "—", sd.get("type") or "—",
                                  sd.get("geoid") or "—"])
            t_sd = Table(sd_rows, colWidths=[3.5*inch, 1.5*inch, 2.0*inch])
            t_sd.setStyle(TableStyle([
                ('BACKGROUND', (0,0), (-1,0), colors.HexColor(EMBER_BLUE)),
                ('TEXTCOLOR',  (0,0), (-1,0), colors.white),
                ('FONTNAME',   (0,0), (-1,0), 'Helvetica-Bold'),
                ('FONTSIZE',   (0,0), (-1,-1), 10),
                ('GRID',       (0,0), (-1,-1), 0.5, colors.HexColor(GRAY_300)),
                ('VALIGN',     (0,0), (-1,-1), 'MIDDLE'),
                ('PADDING',    (0,0), (-1,-1), 6),
            ]))
            story.append(t_sd)
            story.append(Spacer(1, 10))

        # Tract-level aggregates
        sumd = submarket_data.get("tract_summary") or {}
        if sumd and sumd.get("tract_count"):
            story.append(Paragraph(
                f"Census tract aggregates · <b>{sumd.get('tract_count')}</b> tract(s) containing the project · "
                f"population-weighted averages where applicable", body))
            story.append(Spacer(1, 6))
            sm_rows = [["Indicator", "Submarket"]]
            def _fmt(v, kind="int", prefix=""):
                if v is None: return "—"
                if kind == "float":  return f"{prefix}{v:,.1f}"
                if kind == "pct":    return f"{v:.1f}%"
                if kind == "dollar": return f"${v:,.0f}"
                return f"{prefix}{v:,.0f}"
            sm_rows.append(["Population",                  _fmt(sumd.get("total_population") or sumd.get("population"))])
            sm_rows.append(["Median age",                  _fmt(sumd.get("median_age"), "float")])
            sm_rows.append(["Households",                  _fmt(sumd.get("households"))])
            sm_rows.append(["Avg household size",          _fmt(sumd.get("avg_household_size"), "float")])
            sm_rows.append(["Median household income",     _fmt(sumd.get("median_household_income"), "dollar")])
            sm_rows.append(["Per capita income",           _fmt(sumd.get("per_capita_income"), "dollar")])
            sm_rows.append(["Median home value",           _fmt(sumd.get("median_home_value"), "dollar")])
            sm_rows.append(["Median rent",                 _fmt(sumd.get("median_rent"), "dollar")])
            sm_rows.append(["Housing units",               _fmt(sumd.get("housing_units"))])
            sm_rows.append(["Owner-occupancy %",           _fmt(sumd.get("owner_occupancy_pct"), "pct")])
            sm_rows.append(["Vacancy %",                   _fmt(sumd.get("vacancy_pct"), "pct")])
            sm_rows.append(["% Bachelor's+ (25+)",         _fmt(sumd.get("pct_bachelors_or_higher"), "pct")])
            sm_rows.append(["Employed",                    _fmt(sumd.get("employed"))])
            sm_rows.append(["Median year built",           _fmt(sumd.get("median_year_built"))])
            t_sm = Table(sm_rows, colWidths=[3.8*inch, 3.2*inch])
            t_sm.setStyle(TableStyle([
                ('BACKGROUND', (0,0), (-1,0), colors.HexColor(EMBER_BLUE)),
                ('TEXTCOLOR',  (0,0), (-1,0), colors.white),
                ('FONTNAME',   (0,0), (-1,0), 'Helvetica-Bold'),
                ('FONTSIZE',   (0,0), (-1,-1), 10),
                ('GRID',       (0,0), (-1,-1), 0.5, colors.HexColor(GRAY_300)),
                ('VALIGN',     (0,0), (-1,-1), 'MIDDLE'),
                ('ALIGN',      (1,1), (-1,-1), 'RIGHT'),
                ('PADDING',    (0,0), (-1,-1), 5),
            ]))
            story.append(t_sm)

        # Caveats
        caveats = submarket_data.get("caveats") or []
        if caveats:
            story.append(Spacer(1, 10))
            for c in caveats:
                story.append(Paragraph(f"<i>• {c}</i>", label))
        story.append(PageBreak())

    # ============ PAGE 6: MARKET CONTEXT ============
    if market_data and not market_data.get("error"):
        story.append(Paragraph("Market Context", h1))
        cur = market_data.get("current", {})
        gro = market_data.get("growth", {})
        story.append(Paragraph(
            f"<b>{market_data.get('county_name','?')} County</b> · Census ACS 5-year ({cur.get('year','—')})",
            body))
        story.append(Spacer(1, 8))

        demo_rows = [["Indicator", "Current", "5-yr change", "5-yr CAGR"]]
        def _row(label_str, key, prefix="", fmt_kind="int"):
            v = cur.get(key)
            tot = gro.get(f"{key}_total_pct")
            cagr = gro.get(f"{key}_cagr_pct")
            if v is None: v_str = "—"
            elif fmt_kind == "float": v_str = f"{prefix}{v:,.1f}"
            elif fmt_kind == "pct":   v_str = f"{v:.1f}%"
            else: v_str = f"{prefix}{v:,.0f}"
            tot_str = f"{tot:+.1f}%" if tot is not None else "—"
            cagr_str = f"{cagr:+.1f}%" if cagr is not None else "—"
            demo_rows.append([label_str, v_str, tot_str, cagr_str])
        # Population
        _row("Population", "population")
        _row("Median age", "median_age", "", "float")
        _row("Households", "households")
        _row("Avg household size", "avg_household_size", "", "float")
        # Income
        _row("Median household income", "median_household_income", "$")
        _row("Per capita income", "per_capita_income", "$")
        # Housing
        _row("Housing units", "housing_units")
        _row("Median home value", "median_home_value", "$")
        _row("Median rent", "median_rent", "$")
        _row("Owner-occupancy", "owner_occupancy_pct", "", "pct")
        _row("Vacancy rate", "vacancy_pct", "", "pct")
        # Labor / education
        _row("Employed", "employed")
        _row("% Bachelor's+ (25+)", "pct_bachelors_or_higher", "", "pct")
        t = Table(demo_rows, colWidths=[2.2*inch, 1.8*inch, 1.5*inch, 1.5*inch])
        t.setStyle(TableStyle([
            ('BACKGROUND', (0,0), (-1,0), colors.HexColor(EMBER_BLUE)),
            ('TEXTCOLOR',  (0,0), (-1,0), colors.white),
            ('FONTNAME',   (0,0), (-1,0), 'Helvetica-Bold'),
            ('FONTSIZE',   (0,0), (-1,-1), 9),
            ('GRID',       (0,0), (-1,-1), 0.5, colors.HexColor(GRAY_300)),
            ('VALIGN',     (0,0), (-1,-1), 'MIDDLE'),
            ('ALIGN',      (1,1), (-1,-1), 'RIGHT'),
            ('PADDING',    (0,0), (-1,-1), 5),
        ]))
        story.append(t)
        story.append(Spacer(1, 14))

    # FRED indicators + HPI trend chart
    if fred_data and not fred_data.get("error"):
        story.append(Paragraph("Economic Indicators (FRED)", h2))
        try:
            hpi_buf = render_hpi_trend(fred_data)
            if hpi_buf:
                story.append(RLImage(hpi_buf, width=7.0*inch, height=3.0*inch))
                story.append(Spacer(1, 8))
        except Exception:
            pass

        fred_rows = [["Indicator", "Current", "YoY", "5-yr total", "5-yr CAGR"]]
        for ind in fred_data.get("indicators", []):
            d = ind.get("data") or {}
            if d.get("error"):
                fred_rows.append([ind["label"], "err", "—", "—", "—"])
                continue
            cur_v = d.get("current")
            fmt = ind.get("format", "")
            if fmt == "percent":   cur_str = f"{cur_v:.2f}%" if cur_v is not None else "—"
            elif fmt == "index":    cur_str = f"{cur_v:.1f}"   if cur_v is not None else "—"
            elif fmt == "thousands":cur_str = f"{cur_v:,.1f}K" if cur_v is not None else "—"
            elif fmt == "count":    cur_str = f"{cur_v:,.0f}"  if cur_v is not None else "—"
            else:                   cur_str = f"{cur_v:.2f}"   if cur_v is not None else "—"
            yoy = d.get("yoy_pct")
            five_total = d.get("five_year_total_pct")
            five_cagr = d.get("five_year_cagr_pct")
            fred_rows.append([
                ind["label"],
                cur_str,
                f"{yoy:+.2f}%" if yoy is not None else "—",
                f"{five_total:+.1f}%" if five_total is not None else "—",
                f"{five_cagr:+.2f}%" if five_cagr is not None else "—",
            ])
        t = Table(fred_rows, colWidths=[2.5*inch, 1.2*inch, 1.0*inch, 1.1*inch, 1.2*inch])
        t.setStyle(TableStyle([
            ('BACKGROUND', (0,0), (-1,0), colors.HexColor(EMBER_BLUE)),
            ('TEXTCOLOR',  (0,0), (-1,0), colors.white),
            ('FONTNAME',   (0,0), (-1,0), 'Helvetica-Bold'),
            ('FONTSIZE',   (0,0), (-1,-1), 9),
            ('GRID',       (0,0), (-1,-1), 0.5, colors.HexColor(GRAY_300)),
            ('VALIGN',     (0,0), (-1,-1), 'MIDDLE'),
            ('ALIGN',      (1,1), (-1,-1), 'RIGHT'),
            ('PADDING',    (0,0), (-1,-1), 5),
        ]))
        story.append(t)
        story.append(PageBreak())

    # ============ PAGE 7: TRACT ROSTER ============
    story.append(Paragraph("Tracts in this Project", h1))
    tract_rows = [["#", "Owner", "Prop ID", "County", "Acres"]]
    for i, tt in enumerate(proj.get("tracts") or [], 1):
        tract_rows.append([
            str(i),
            (tt.get("owner_name") or "—")[:48],
            tt.get("prop_id") or "—",
            tt.get("county") or "—",
            f"{(tt.get('acres') or 0):,.1f}",
        ])
    # Append total row
    tract_rows.append([
        Paragraph(f"<b>Total ({len(proj.get('tracts') or [])} tracts)</b>", body),
        "", "", "",
        Paragraph(f"<b>{analysis['gross_acres']:,.1f}</b>", body),
    ])
    tt_table = Table(tract_rows, colWidths=[0.4*inch, 3.0*inch, 1.4*inch, 1.4*inch, 0.8*inch])
    tt_table.setStyle(TableStyle([
        ('BACKGROUND', (0,0), (-1,0), colors.HexColor(EMBER_BLUE)),
        ('TEXTCOLOR',  (0,0), (-1,0), colors.white),
        ('FONTNAME',   (0,0), (-1,0), 'Helvetica-Bold'),
        ('FONTSIZE',   (0,0), (-1,-1), 9),
        ('GRID',       (0,0), (-1,-1), 0.5, colors.HexColor(GRAY_300)),
        ('ALIGN',      (0,0), (0,-1),  'CENTER'),
        ('ALIGN',      (-1,1),(-1,-1), 'RIGHT'),
        ('VALIGN',     (0,0), (-1,-1), 'MIDDLE'),
        ('PADDING',    (0,0), (-1,-1), 4),
        ('BACKGROUND', (0,-1), (-1,-1), colors.HexColor("#FFF4ED")),
    ]))
    story.append(tt_table)
    story.append(Spacer(1, 18))

    # ---- Per-tract detail -------------------------------------------------
    # A project stores only prop_id / county / owner_name / acres / geometry.
    # The appraisal-district attributes the tract drawer shows are fetched live
    # per parcel and never persisted, which is why they were absent here.
    # Resolve each tract the same way the tract sheet does: last-search cache
    # first (it carries the spatial enrichment too), then the parcel cache with
    # one batched HCAD/MCAD overlay across all of them.
    DETAIL_CAP = 25
    _ptracts = proj.get("tracts") or []
    _detail_for = _ptracts[:DETAIL_CAP]

    _resolved = {}
    try:
        _cache_feats = ((_last_search_cache.get("data") or {}).get("tracts") or {}).get("features") or []
        _by_pid = {str((f.get("properties") or {}).get("Prop_ID") or "").strip(): f
                   for f in _cache_feats}
        _need = []
        for _tt in _detail_for:
            _pid = str(_tt.get("prop_id") or "").strip()
            if _pid and _pid in _by_pid:
                _resolved[_pid] = _by_pid[_pid]
            elif _pid:
                _need.append(_pid)

        if _need:
            import acq_parcels as _pc
            _found = []
            for _pid in _need:
                try:
                    _f = _pc.find_parcel_by_pid(_pid)
                except Exception as e:
                    print(f"[project-pdf] parcel lookup failed for {_pid}: {e}", flush=True)
                    _f = None
                if _f:
                    _resolved[_pid] = _f
                    _found.append(_f)
            # One overlay call for all of them - per-tract calls would make a
            # 20-tract assemblage unbearably slow.
            if _found:
                _fc = {"type": "FeatureCollection", "features": _found}
                try: hcad_live_overlay(_fc)
                except Exception as e: print(f"[project-pdf] hcad overlay failed: {e}", flush=True)
                try: mcad_live_overlay(_fc)
                except Exception as e: print(f"[project-pdf] mcad overlay failed: {e}", flush=True)
    except Exception as e:
        print(f"[project-pdf] tract detail resolution failed: {e}", flush=True)

    if _detail_for:
        story.append(Paragraph("Tract Detail", h2))
        story.append(Paragraph(
            "Appraisal-district record for each tract, refreshed live at export. "
            "Fields shown as \u2014 are not carried by the source for that parcel.",
            label))
        story.append(Spacer(1, 8))

    def _kv2(lbl, val):
        return [Paragraph(f"<b>{lbl}</b>", body),
                Paragraph(str(val) if val not in (None, "", 0) else "\u2014", body)]

    for _i, _tt in enumerate(_detail_for, 1):
        _pid = str(_tt.get("prop_id") or "").strip()
        _feat = _resolved.get(_pid) or {}
        _pr = _feat.get("properties") or {}

        _mkt  = _pr.get("MARKET_VAL") or _pr.get("total_market_val")
        _appr = _pr.get("total_appraised_val")
        _taxv = _pr.get("tax_value")
        try:
            _src = _tract_source_label(_pr) if _pr else "project record only"
        except Exception:
            _src = "\u2014"

        _left = [
            _kv2("Owner",        _pr.get("OWNER_NAME") or _tt.get("owner_name") or ""),
            _kv2("Mailing",      _pr.get("MAIL_ADDR") or ""),
            _kv2("Site address", _pr.get("SITUS_ADDR") or ""),
            _kv2("Legal desc",   (_pr.get("LEGAL_DESC") or "")[:120]),
            _kv2("Acres",        f"{(_tt.get('acres') or 0):,.1f}"),
            _kv2("County",       _tt.get("county") or _pr.get("_county") or ""),
            _kv2("ETJ / city",   _pr.get("_city_etj") or ""),
            _kv2("School dist",  _pr.get("_school_dist") or ""),
        ]
        _right = [
            _kv2("Prop ID",       _pid),
            _kv2("Source",        _src),
            _kv2("Owner since",   _pr.get("_owner_since") or ""),
            # "Tax year", not "HCAD tax year": the label column is an inch wide
            # and the longer form wraps onto two lines, and it is MCAD anyway
            # for Montgomery parcels.
            _kv2("Tax year",      _pr.get("_hcad_tax_year") or ""),
            _kv2("Market value",  f"${_mkt:,.0f}" if _mkt else ""),
            _kv2("Appraised",     f"${_appr:,.0f}" if _appr else ""),
            _kv2("Taxable",       f"${_taxv:,.0f}" if _taxv else ""),
            _kv2("Flood %",       _pr.get("_flood_pct") or ""),
        ]

        def _sub(rows):
            _t = Table(rows, colWidths=[1.0*inch, 2.4*inch])
            _t.setStyle(TableStyle([
                ("VALIGN", (0,0), (-1,-1), "TOP"),
                ("LEFTPADDING", (0,0), (-1,-1), 0),
                ("RIGHTPADDING", (0,0), (-1,-1), 4),
                ("TOPPADDING", (0,0), (-1,-1), 1),
                ("BOTTOMPADDING", (0,0), (-1,-1), 1),
            ]))
            return _t

        _hdr = Paragraph(
            f'<font color="{EMBER_ORANGE}"><b>{_i}.</b></font> '
            f'<b>{(_pr.get("OWNER_NAME") or _tt.get("owner_name") or "\u2014")[:60]}</b>'
            f' <font color="{GRAY_700}" size=8>&nbsp;&nbsp;{_pid or ""}</font>', body)

        _outer = Table([[_sub(_left), _sub(_right)]], colWidths=[3.5*inch, 3.5*inch])
        _outer.setStyle(TableStyle([
            ("VALIGN", (0,0), (-1,-1), "TOP"),
            ("LEFTPADDING", (0,0), (-1,-1), 0),
            ("RIGHTPADDING", (0,0), (-1,-1), 6),
        ]))
        story.append(KeepTogether([_hdr, Spacer(1, 3), _outer, Spacer(1, 12)]))

    if len(_ptracts) > DETAIL_CAP:
        # Say so rather than letting the report look complete when it is not.
        story.append(Paragraph(
            f"Detail shown for the first {DETAIL_CAP} of {len(_ptracts)} tracts. "
            "The roster above covers all of them.", label))

    story.append(Spacer(1, 18))

    # ============ FINAL: SOURCES + METHODOLOGY ============
    story.append(Paragraph("Sources & Methodology", h2))
    story.append(Paragraph(
        "<b>Parcel data:</b> TxGIO StratMap (statewide), refreshed weekly. "
        "Owner names verified live via HCAD (Harris) / MCAD (Montgomery) for those counties.<br/>"
        "<b>Floodplain:</b> FEMA National Flood Hazard Layer, Zone A/AE/AH/AO/AR/A99/V/VE (100-yr).<br/>"
        "<b>Wetlands:</b> USFWS National Wetlands Inventory (NWI). All wetland classes included.<br/>"
        "<b>Transmission:</b> HIFLD Electric Power Transmission Lines. 150ft ROW assumed (75ft each side).<br/>"
        "<b>Streams:</b> USGS National Hydrography Dataset (NHD). 50ft riparian buffer each side.<br/>"
        "<b>Pipelines:</b> Texas Railroad Commission. 50ft easement each side.<br/>"
        "<b>Elevation:</b> USGS 3DEP, sampled at a 25×25-grid across the project polygon.<br/>"
        "<b>Demographics:</b> U.S. Census Bureau ACS 5-year estimates (county-level).<br/>"
        "<b>Economic indicators:</b> Federal Reserve Bank of St. Louis (FRED).",
        label))
    story.append(Spacer(1, 10))
    story.append(Paragraph(
        "<i>Constraint acreages reflect the overlapping geometries detected within the project boundary. "
        "Net developable subtracts the UNION of constraints to avoid double-counting. Yield estimates assume "
        "100% of net developable area yields lots — actual yields depend on roads, drainage, amenities, open "
        "space, and entitlement. Treat all numbers as planning-grade until verified by a survey and "
        "civil-engineering yield study. Computed " + analysis.get("computed_at", "—") + ".</i>",
        label))
    story.append(Spacer(1, 8))
    story.append(Paragraph(
        f'<para alignment="center"><font color="{GRAY_700}" size=8>Prepared by Ember Acquisitions · {datetime.now().strftime("%B %Y")}</font></para>',
        body))

    doc.build(story)
    buf.seek(0)
    _acq_log("project_pdf", {"id": pid, "name": proj.get("name"),
                                 "had_elevation": bool(elev_data and not elev_data.get("error")),
                                 "had_market":    bool(market_data and not market_data.get("error")),
                                 "had_fred":      bool(fred_data and not fred_data.get("error"))})

    safe_name = "".join(c for c in (proj.get("name") or "project") if c.isalnum() or c in " -_")[:40].strip() or "project"
    return send_file(buf, mimetype="application/pdf",
                     as_attachment=True,
                     download_name=f"{safe_name} — Acquisitions Summary.pdf")


# Elevation. The project PDF calls the profile endpoint directly, and the map's
# draw-a-line tool calls the line endpoint.

@acq_bp.route("/api/acq/elevation-line", methods=["POST"])
@_login_required
def acq_api_elevation_line():
    """Sample USGS 3DEP elevation along a polyline. Returns ordered samples
    with cumulative distance, ready to plot as a profile chart.

    Request: {"line": [[lat, lon], [lat, lon], ...], "samples": 60 (optional)}
    """
    body = request.get_json(force=True) or {}
    line = body.get("line") or []
    if len(line) < 2:
        return jsonify({"error": "need at least 2 points"}), 400
    n_samples = max(10, min(int(body.get("samples", 60)), 150))

    # Walk the line, build interpolated sample points spaced evenly by cumulative distance
    import math
    def haversine_ft(lat1, lon1, lat2, lon2):
        R = 20902231.0   # earth radius in feet
        p1, p2 = math.radians(lat1), math.radians(lat2)
        dp = math.radians(lat2 - lat1); dl = math.radians(lon2 - lon1)
        a = math.sin(dp/2)**2 + math.cos(p1) * math.cos(p2) * math.sin(dl/2)**2
        return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1-a))

    # Total length + per-segment cumulative distance
    seg_dists = [0.0]
    for i in range(1, len(line)):
        lat1, lon1 = line[i-1]
        lat2, lon2 = line[i]
        seg_dists.append(seg_dists[-1] + haversine_ft(lat1, lon1, lat2, lon2))
    total_ft = seg_dists[-1]
    if total_ft < 1:
        return jsonify({"error": "line too short"}), 400

    # Generate sample points evenly along the line
    samples = []   # list of (distance_ft, lat, lon)
    for i in range(n_samples):
        d = total_ft * i / (n_samples - 1)
        # Find which segment d falls in
        for j in range(1, len(seg_dists)):
            if seg_dists[j] >= d:
                seg_start = seg_dists[j-1]
                seg_len = seg_dists[j] - seg_start
                t = (d - seg_start) / seg_len if seg_len > 0 else 0
                lat1, lon1 = line[j-1]; lat2, lon2 = line[j]
                lat = lat1 + (lat2 - lat1) * t
                lon = lon1 + (lon2 - lon1) * t
                samples.append((d, lat, lon))
                break

    # Query USGS 3DEP in parallel
    def fetch_one(s):
        d, lat, lon = s
        try:
            r = requests.get(ENDPOINTS["usgs_elevation"], params={
                "x": lon, "y": lat, "units": "Feet", "wkid": 4326,
            }, timeout=8)
            r.raise_for_status()
            v = r.json().get("value")
            return (d, lat, lon, float(v) if v is not None else None)
        except Exception:
            return (d, lat, lon, None)

    with ThreadPoolExecutor(max_workers=12) as pool:
        results = list(pool.map(fetch_one, samples))

    valid = [(d, e) for d, lat, lon, e in results if e is not None]
    if not valid:
        return jsonify({"error": "no elevation data (USGS 3DEP may be down)"}), 502
    elevs = [e for _, e in valid]
    return jsonify({
        "samples": [{"distance_ft": round(d, 1), "lat": lat, "lon": lon,
                      "elev_ft": round(e, 1) if e is not None else None}
                     for d, lat, lon, e in results],
        "total_length_ft": round(total_ft, 1),
        "total_length_mi": round(total_ft / 5280, 3),
        "min_ft": round(min(elevs), 1),
        "max_ft": round(max(elevs), 1),
        "range_ft": round(max(elevs) - min(elevs), 1),
        "drop_ft": round(elevs[0] - elevs[-1], 1),   # positive = downhill end-to-end
    })


@acq_bp.route("/api/acq/elevation-profile", methods=["POST"])
@_login_required
def acq_api_elevation_profile(geometry=None):
    """Sample USGS 3DEP elevation at a DENSE grid inside the supplied polygon,
    then return a rich analysis package:

      • summary stats: min / max / range / median / mean / slope%
      • drainage direction (compass) + magnitude
      • highest- and lowest-point locations on the tract
      • CONTOUR LINES at auto-chosen interval (2/5/10/20 ft), clipped to polygon
      • N-S and E-W cross-section profiles through tract centroid
      • hypsometric histogram (acres per elevation band)

    Grid density auto-scales with tract size: 15x15 for tiny tracts (<10 ac),
    25x25 default, 30x30 for large tracts (>1000 ac).

    Request: {"geometry": <GeoJSON Polygon or MultiPolygon>}

    `geometry` may also be passed directly, which is how the project PDF calls
    this: it is already inside a GET request that carries no body, so reading
    the body would 400.
    """
    if geometry is not None:
        body = {"geometry": geometry}
    else:
        body = request.get_json(force=True) or {}
    geom_json = body.get("geometry")
    if not geom_json:
        return jsonify({"error": "geometry required"}), 400

    from shapely.geometry import shape as shp_shape, Point, LineString
    import numpy as np
    try:
        geom = shp_shape(geom_json)
    except Exception as e:
        return jsonify({"error": f"bad geometry: {e}"}), 400
    if geom.is_empty:
        return jsonify({"error": "empty geometry"}), 400

    minx, miny, maxx, maxy = geom.bounds

    # ---- Grid density auto-scales with tract size ----
    # Cheap acreage estimate from shapely's planar area (rough, but fine for sizing)
    lat_mid = (miny + maxy) / 2
    lat_to_ft = 364320.0
    lon_to_ft = lat_to_ft * np.cos(np.radians(lat_mid))
    approx_acres = geom.area * lat_to_ft * lon_to_ft / 43560.0
    if approx_acres < 10:    GRID = 15
    elif approx_acres > 1000: GRID = 30
    else:                    GRID = 25

    # ---- Build FULL bbox grid (we'll keep "inside polygon" as a separate mask).
    # Sampling outside the bbox edges helps contour lines come out cleanly along
    # the polygon boundary — we clip them afterward via shapely.
    grid_points = []
    for i in range(GRID):
        for j in range(GRID):
            lat = miny + (maxy - miny) * (i + 0.5) / GRID
            lon = minx + (maxx - minx) * (j + 0.5) / GRID
            grid_points.append((i, j, lat, lon))

    # ---- Bulk sample USGS 3DEP via getSamples — but SPLIT the grid into
    # parallel batches. The endpoint scales linearly with point count, so 4
    # parallel batches of ~150 points each finish in ~1/4 the time of a single
    # 625-point call. Falls back to per-point endpoint if all batches fail.
    BULK_URL = "https://elevation.nationalmap.gov/arcgis/rest/services/3DEPElevation/ImageServer/getSamples"
    results = [None] * len(grid_points)
    M_TO_FT = 3.28084
    BATCH_SIZE = 160
    batches = [(start, grid_points[start:start + BATCH_SIZE])
               for start in range(0, len(grid_points), BATCH_SIZE)]

    def _fetch_batch(batch_start, batch_pts):
        try:
            geom_param = json.dumps({
                "points": [[gp[3], gp[2]] for gp in batch_pts],   # [lon, lat]
                "spatialReference": {"wkid": 4326},
            })
            r = requests.post(BULK_URL, data={
                "geometry": geom_param,
                "geometryType": "esriGeometryMultipoint",
                "returnFirstValueOnly": "true",
                "interpolation": "RSP_BilinearInterpolation",
                "f": "json",
            }, timeout=30)
            r.raise_for_status()
            j_data = r.json()
            if "error" in j_data:
                return False
            samples = j_data.get("samples") or []
            for k, s in enumerate(samples):
                v = s.get("value")
                try:
                    fv = float(v) * M_TO_FT if v not in (None, "", "NoData") else None
                except (ValueError, TypeError):
                    fv = None
                i, j, lat, lon = batch_pts[k]
                results[batch_start + k] = (i, j, lat, lon, fv)
            return True
        except Exception as e:
            print(f"[elevation] bulk batch failed at {batch_start}: {e}", flush=True)
            return False

    with ThreadPoolExecutor(max_workers=len(batches)) as pool:
        ok_flags = list(pool.map(lambda b: _fetch_batch(b[0], b[1]), batches))

    if not all(ok_flags):
        # Fallback: per-point single endpoint, parallelized
        session = requests.Session()
        def fetch_one(p):
            i, j, lat, lon = p
            try:
                r = session.get(ENDPOINTS["usgs_elevation"], params={
                    "x": lon, "y": lat, "units": "Feet", "wkid": 4326,
                }, timeout=10)
                r.raise_for_status()
                v = r.json().get("value")
                return (i, j, lat, lon, float(v) if v is not None else None)
            except Exception:
                return (i, j, lat, lon, None)
        with ThreadPoolExecutor(max_workers=30) as pool:
            results = list(pool.map(fetch_one, grid_points))

    # Fill in any None slots that the bulk call didn't return (rare edge case
    # — e.g. service returns fewer samples than requested) so downstream code
    # can safely unpack every entry.
    for k in range(len(results)):
        if results[k] is None:
            i, j, lat, lon = grid_points[k]
            results[k] = (i, j, lat, lon, None)

    # ---- Build numpy arrays ----
    Z   = np.full((GRID, GRID), np.nan, dtype=float)
    LAT = np.zeros((GRID, GRID), dtype=float)
    LON = np.zeros((GRID, GRID), dtype=float)
    inside_mask = np.zeros((GRID, GRID), dtype=bool)
    for i, j, lat, lon, e in results:
        LAT[i, j] = lat
        LON[i, j] = lon
        if e is not None: Z[i, j] = e
        if geom.contains(Point(lon, lat)): inside_mask[i, j] = True

    valid_inside_mask = inside_mask & ~np.isnan(Z)
    inside_z = Z[valid_inside_mask]
    if inside_z.size == 0:
        return jsonify({"error": "no elevation data returned (USGS 3DEP may be down or polygon too small)"}), 502

    min_e   = float(np.min(inside_z))
    max_e   = float(np.max(inside_z))
    median  = float(np.median(inside_z))
    mean    = float(np.mean(inside_z))
    range_e = max_e - min_e

    # ---- Slope (in %) via numpy gradient on the filled grid ----
    cell_dx_ft = (maxx - minx) / GRID * lon_to_ft
    cell_dy_ft = (maxy - miny) / GRID * lat_to_ft
    Z_filled = np.where(np.isnan(Z), mean, Z)
    dy_arr, dx_arr = np.gradient(Z_filled, cell_dy_ft, cell_dx_ft)
    slope_mag = np.sqrt(dx_arr**2 + dy_arr**2)   # ft/ft (rise/run)
    slope_mean_pct = float(np.mean(slope_mag[valid_inside_mask])) * 100
    slope_max_pct  = float(np.max(slope_mag[valid_inside_mask])) * 100

    # ---- Drainage direction = compass bearing of mean -gradient (downhill) ----
    flow_x_mean = -float(np.mean(dx_arr[valid_inside_mask]))   # E component
    flow_y_mean = -float(np.mean(dy_arr[valid_inside_mask]))   # N component
    flow_mag    = float(np.sqrt(flow_x_mean**2 + flow_y_mean**2))
    drainage_dir = "flat (no clear gradient)"
    if flow_mag > 0.001:   # >= ~0.1% slope
        angle = float(np.degrees(np.arctan2(flow_x_mean, flow_y_mean)))
        if angle < 0: angle += 360
        compass = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
                   "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"]
        drainage_dir = compass[int((angle + 11.25) % 360 / 22.5)]

    # ---- Highest / lowest point locations ----
    # Mask out cells outside polygon by setting them to NaN for argmax/argmin
    inside_or_nan = np.where(valid_inside_mask, Z, np.nan)
    i_hi, j_hi = np.unravel_index(np.nanargmax(inside_or_nan), Z.shape)
    i_lo, j_lo = np.unravel_index(np.nanargmin(inside_or_nan), Z.shape)

    def _quadrant_label(i, j):
        """Human label for where in the tract this cell sits."""
        ns_thr = GRID * 0.30
        ew_thr = GRID * 0.30
        ns = "north" if (i - GRID/2) > ns_thr else ("south" if (GRID/2 - i) > ns_thr else "")
        ew = "east"  if (j - GRID/2) > ew_thr else ("west"  if (GRID/2 - j) > ew_thr else "")
        if ns and ew:    return f"{ns}{ew} corner"
        if ns:           return f"{ns} side"
        if ew:           return f"{ew} side"
        return "center"

    highest_loc = _quadrant_label(i_hi, j_hi)
    lowest_loc  = _quadrant_label(i_lo, j_lo)
    highest_latlon = [float(LAT[i_hi, j_hi]), float(LON[i_hi, j_hi])]
    lowest_latlon  = [float(LAT[i_lo, j_lo]), float(LON[i_lo, j_lo])]

    # ---- Choose contour interval based on total relief ----
    if   range_e < 8:   interval = 1
    elif range_e < 20:  interval = 2
    elif range_e < 80:  interval = 5
    elif range_e < 200: interval = 10
    else:               interval = 20

    # ---- Generate contour lines via matplotlib, then clip to polygon ----
    contours = []
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        levels = list(np.arange(np.floor(min_e/interval)*interval,
                                  np.ceil(max_e/interval + 1)*interval,
                                  interval))
        fig, ax = plt.subplots()
        cs = ax.contour(LON, LAT, Z, levels=levels)
        plt.close(fig)

        for level, allsegs in zip(cs.levels, cs.allsegs):
            for seg in allsegs:
                if len(seg) < 2: continue
                line = LineString([(float(x), float(y)) for x, y in seg])
                try:
                    clipped = line.intersection(geom)
                except Exception:
                    continue
                if clipped.is_empty: continue

                if clipped.geom_type == "LineString":
                    lines_to_add = [clipped]
                elif clipped.geom_type == "MultiLineString":
                    lines_to_add = list(clipped.geoms)
                else:
                    lines_to_add = [g for g in getattr(clipped, "geoms", [])
                                    if g.geom_type == "LineString"]

                for ln in lines_to_add:
                    if len(ln.coords) < 2: continue
                    contours.append({
                        "elev_ft": round(float(level), 1),
                        "coords": [[round(x, 6), round(y, 6)] for x, y in ln.coords],
                    })
    except Exception as e:
        print(f"[elevation] contour generation failed: {e}", flush=True)

    # ---- Cross-section profiles: middle row (E-W) and middle column (N-S) ----
    mid_row = GRID // 2
    mid_col = GRID // 2
    ew_profile = []
    ns_profile = []
    for j in range(GRID):
        if not np.isnan(Z[mid_row, j]):
            ew_profile.append({
                "frac": round(j / (GRID - 1), 3),
                "elev_ft": round(float(Z[mid_row, j]), 1),
                "inside": bool(inside_mask[mid_row, j]),
            })
    for i in range(GRID):
        if not np.isnan(Z[i, mid_col]):
            ns_profile.append({
                "frac": round(i / (GRID - 1), 3),
                "elev_ft": round(float(Z[i, mid_col]), 1),
                "inside": bool(inside_mask[i, mid_col]),
            })

    # ---- Hypsometric histogram: acres per elevation band ----
    poly_area_sqft = float(geom.area * lat_to_ft * lon_to_ft)
    poly_acres_calc = poly_area_sqft / 43560.0
    cells_inside = int(np.sum(valid_inside_mask))
    acres_per_cell = poly_acres_calc / cells_inside if cells_inside else 0.0

    bands = {}
    for z in inside_z:
        band_low = int(np.floor(z / interval) * interval)
        bands[band_low] = bands.get(band_low, 0.0) + acres_per_cell
    histogram = [
        {
            "low":   k,
            "high":  k + interval,
            "acres": round(v, 2),
            "pct":   round(v / poly_acres_calc * 100, 1) if poly_acres_calc else 0,
        }
        for k, v in sorted(bands.items())
    ]

    return jsonify({
        # core stats
        "min_ft":     round(min_e, 1),
        "max_ft":     round(max_e, 1),
        "median_ft":  round(median, 1),
        "mean_ft":    round(mean, 1),
        "range_ft":   round(range_e, 1),
        "sample_count": cells_inside,
        "polygon_acres": round(poly_acres_calc, 1),
        "grid_size": GRID,
        "bbox": [minx, miny, maxx, maxy],
        # slope & drainage
        "slope_mean_pct": round(slope_mean_pct, 2),
        "slope_max_pct":  round(slope_max_pct,  2),
        "drainage_dir":   drainage_dir,
        # high/low point
        "highest_ft":      round(max_e, 1),
        "lowest_ft":       round(min_e, 1),
        "highest_loc":     highest_loc,
        "lowest_loc":      lowest_loc,
        "highest_latlon":  highest_latlon,
        "lowest_latlon":   lowest_latlon,
        # rich payload
        "contour_interval_ft": interval,
        "contours":            contours,
        "ns_profile":          ns_profile,
        "ew_profile":          ew_profile,
        "histogram":           histogram,
        # legacy (back-compat — drop later)
        "grid": [[None if np.isnan(Z[i, j]) else round(float(Z[i, j]), 1)
                  for j in range(GRID)] for i in range(GRID)],
    })
