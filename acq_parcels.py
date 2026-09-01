"""Statewide Texas parcel cache for the Acquisitions GIS tab.

Ported unchanged from the standalone Acquisitions GIS app apart from the data
directory, which now honours ACQ_DATA_DIR. SQLite rather than Postgres on
purpose: this is a 2.7M-row read-mostly geometry cache rebuilt from TxGIO, not
application state, and its R-Tree index is what makes a bbox parcel query
sublinear. Keeping it out of Postgres also keeps it out of backups it would
dominate.

Original module notes follow.

Local parcel cache (SQLite + R-Tree) for the Houston-metro counties.

The cache mirrors TxGIO StratMap statewide parcels for a configurable list of
counties (`HOUSTON_METRO_COUNTIES` below). Searches query the local DB instead
of hitting StratMap live every time — turning a 25-second StratMap pull into a
~1-second SQLite query.

Architecture:
  - Counties are bootstrapped one at a time by spatial query against StratMap.
  - Each parcel is stored with its full polygon as WKB (binary geometry).
  - An R-Tree virtual table indexes the bbox of each parcel for fast spatial
    filtering. A search runs: r-tree bbox filter -> shapely exact intersect.
  - Per-county `cache_meta` rows track when each county was last refreshed,
    so a background thread can pull fresh county data weekly without blocking.

HCAD/MCAD live overlays still run on top of cached results, so OWNER_NAME and
acreage stay current even though the parcel geometry / legal description is
cached. The cache is essentially a fast index, not a source of truth for owners.
"""
from __future__ import annotations

import sqlite3
import time
import threading
from pathlib import Path

# 10-county Houston metro. FIPS codes for spatial lookup against Census TIGER.
# Tweak this list to expand coverage. Each addition takes ~3-10 minutes to bootstrap.
HOUSTON_METRO_COUNTIES = [
    ("48201", "Harris"),
    ("48157", "Fort Bend"),
    ("48339", "Montgomery"),
    ("48039", "Brazoria"),
    ("48167", "Galveston"),
    ("48291", "Liberty"),
    ("48473", "Waller"),
    ("48071", "Chambers"),
    ("48015", "Austin"),
    ("48407", "San Jacinto"),
    # Outer growth-corridor ring (added for tracts north/northwest of metro)
    ("48185", "Grimes"),
    ("48471", "Walker"),
    ("48313", "Madison"),
    ("48477", "Washington"),
]

REFRESH_INTERVAL_DAYS = 7   # auto-refresh each county weekly

# The statewide parcel cache is ~1.3 GB of SQLite + R-Tree, far too large to
# live in the image or in Postgres. On Railway it belongs on a mounted volume;
# ACQ_DATA_DIR points at that mount. Locally it falls back to ./storage, which
# is gitignored, so a dev checkout behaves the same as the standalone app did.
import os as _os

_DATA_DIR = Path(_os.environ.get("ACQ_DATA_DIR") or (Path(__file__).parent / "storage"))
_DB_PATH = _DATA_DIR / "parcels_cache.db"
_db_lock = threading.Lock()


def _conn():
    _DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(str(_DB_PATH), timeout=30, isolation_level=None)
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA synchronous=NORMAL")
    return c


def init_db():
    """Create tables + R-Tree index if they don't exist. Idempotent."""
    with _db_lock, _conn() as c:
        c.executescript("""
            -- Identity is (county_fips, prop_id, geom_key).
            --
            -- Prop_ID alone is not unique: it repeats across counties, which
            -- made each county overwrite the last one's parcels. It also is not
            -- unique WITHIN a county, which is subtler — Prop_ID is an appraisal
            -- ACCOUNT number, and one account routinely covers several separate
            -- tracts. Rancho La Laguna's Waller account 7282 is a 173-acre tract
            -- and a 94-acre tract; Baldridge Enterprises has four under one id.
            -- Keying on (county, prop_id) kept one tract per account and dropped
            -- the rest: 771 of 5,330 parcels, 14.5%, in a single 3-mile search.
            --
            -- geom_key is a hash of the parcel geometry, so two tracts under one
            -- account are distinct rows while the SAME parcel returned by two
            -- overlapping tiles still collapses to one.
            CREATE TABLE IF NOT EXISTS parcels (
                rowid       INTEGER PRIMARY KEY AUTOINCREMENT,
                prop_id     TEXT NOT NULL,
                geom_key    TEXT,
                county_fips TEXT,
                county_name TEXT,
                owner_name  TEXT,
                mail_addr   TEXT,
                situs_addr  TEXT,
                legal_desc  TEXT,
                gis_area    REAL,
                legal_area  REAL,
                shape_wkb   BLOB,
                updated_at  INTEGER,
                UNIQUE (county_fips, prop_id, geom_key)
            );
            CREATE INDEX IF NOT EXISTS ix_parcels_county ON parcels(county_fips);
            CREATE INDEX IF NOT EXISTS ix_parcels_propid ON parcels(prop_id);

            -- R-Tree spatial index. Query by bbox first (sublinear), then exact
            -- intersection in shapely. Keyed by parcels.rowid for fast joins.
            CREATE VIRTUAL TABLE IF NOT EXISTS parcels_rtree USING rtree(
                id INTEGER PRIMARY KEY,
                minx REAL, maxx REAL,
                miny REAL, maxy REAL
            );

            CREATE TABLE IF NOT EXISTS cache_meta (
                county_fips        TEXT PRIMARY KEY,
                county_name        TEXT,
                bootstrapped_at    INTEGER,    -- first-ever load
                last_refreshed_at  INTEGER,    -- most recent refresh
                parcel_count       INTEGER,
                status             TEXT        -- 'pending', 'loading', 'fresh', 'partial', 'stale', 'error'
            );

            -- Which tiles of a county are already loaded, so an interrupted
            -- bootstrap can pick up where it stopped instead of re-downloading
            -- the county. Harris is ~1.4 GB fetched at ~0.4 MB/s; losing an
            -- hour of that to a restart is the difference between a background
            -- chore and one nobody is willing to start.
            CREATE TABLE IF NOT EXISTS cache_tiles (
                county_fips  TEXT NOT NULL,
                grid_sig     TEXT NOT NULL,   -- invalidates if the grid changes
                tile_index   INTEGER NOT NULL,
                parcels      INTEGER,
                failures     INTEGER,
                done_at      INTEGER,
                PRIMARY KEY (county_fips, grid_sig, tile_index)
            );
        """)


    # Heal a database created before parcel identity became (county, prop_id).
    try:
        with _db_lock, _conn() as _c:
            _migrate_propid_unique(_c)
    except Exception as _e:
        print(f"[cache] parcels migration skipped: {_e}", flush=True)

    # Identity widened from (county, prop_id) to include geometry.
    try:
        with _db_lock, _conn() as _c:
            _migrate_geom_key(_c)
    except Exception as _e:
        print(f"[cache] geom_key migration skipped: {_e}", flush=True)

    # Once per process: a county still marked 'loading' was killed, not failed.
    global _loading_healed
    if not _loading_healed:
        _loading_healed = True
        try:
            with _db_lock, _conn() as _c:
                _heal_stuck_loading(_c)
        except Exception as _e:
            print(f"[cache] stuck-loading heal skipped: {_e}", flush=True)

_loading_healed = False


# A loader refreshes cache_meta.loading_heartbeat after every tile. Anything
# older than this is a run that is no longer alive. Generous on purpose: a
# single Harris tile can take minutes, and wrongly declaring a live load dead
# is worse than leaving a genuinely dead one marked 'loading' a while longer.
STALE_LOADING_SEC = 900


def _geom_key(wkb_bytes) -> str:
    """Stable short hash of a parcel's geometry.

    Distinguishes two different tracts sharing an appraisal account number from
    the same tract returned twice by overlapping tiles — the first must be two
    rows, the second must be one.
    """
    import hashlib
    return hashlib.sha1(wkb_bytes).hexdigest()[:16]


def _ensure_heartbeat_column(conn):
    """Add cache_meta.loading_heartbeat to databases created before it existed."""
    cols = [r[1] for r in conn.execute("PRAGMA table_info(cache_meta)")]
    if "loading_heartbeat" not in cols:
        conn.execute("ALTER TABLE cache_meta ADD COLUMN loading_heartbeat INTEGER")
    return "loading_heartbeat" not in cols


def _heal_stuck_loading(conn):
    """Clear 'loading' rows left behind by a process that died mid-bootstrap.

    A kill raises nothing, so bootstrap_county's except branch never sets
    'error' and the county sits at 'loading' forever. The admin page then shows
    it as work in progress, which is the same shape of bug as a half-loaded
    county reporting 'fresh'.

    Liveness comes from cache_meta.loading_heartbeat, which the loader refreshes
    after each tile -- a row is only healed once that has gone stale. Presence
    of 'loading' alone is not enough: this runs from init_db, so every other
    process that opens the cache would otherwise declare a healthy in-flight
    bootstrap dead. That is not hypothetical; it downgraded a live Harris load
    the first time a second process started while one was running, and on
    Railway every gunicorn worker start would do the same.

    A county with rows is marked 'partial' (it has data, but an unknown amount
    is missing); one with none is marked 'error'.
    """
    _ensure_heartbeat_column(conn)
    cutoff = int(time.time()) - STALE_LOADING_SEC
    rows = conn.execute(
        "SELECT county_fips, county_name FROM cache_meta "
        " WHERE status='loading' "
        "   AND (loading_heartbeat IS NULL OR loading_heartbeat < ?)",
        (cutoff,)).fetchall()
    for fips, name in rows:
        n = conn.execute("SELECT COUNT(*) FROM parcels WHERE county_fips=?",
                         (fips,)).fetchone()[0]
        conn.execute("UPDATE cache_meta SET status=?, parcel_count=? WHERE county_fips=?",
                     ("partial" if n else "error", n, fips))
        print(f"[cache] {name}: bootstrap died mid-load, {n:,} parcels present "
              f"-- marked {'partial' if n else 'error'}, re-bootstrap to complete",
              flush=True)
    return len(rows)


def _migrate_propid_unique(conn):
    """Rebuild `parcels` if it still declares prop_id as globally UNIQUE.

    That constraint plus INSERT OR REPLACE meant a county loaded later silently
    replaced an earlier county's parcels wherever Prop_IDs collided - 22% of the
    cache in practice. Rowids are preserved so parcels_rtree stays valid.
    """
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='parcels'").fetchone()
    if not row or not row[0]:
        return False
    sql = row[0]
    if "UNIQUE (county_fips, prop_id)" in sql or "UNIQUE(county_fips, prop_id)" in sql:
        return False        # already migrated
    if "prop_id     TEXT UNIQUE" not in sql and "prop_id TEXT UNIQUE" not in sql:
        return False        # some other shape; leave it alone

    print("[cache] migrating parcels: prop_id UNIQUE -> UNIQUE(county_fips, prop_id)",
          flush=True)
    before = conn.execute("SELECT COUNT(*) FROM parcels").fetchone()[0]
    conn.execute("PRAGMA foreign_keys=OFF")
    conn.execute("BEGIN")
    try:
        conn.execute("""
            CREATE TABLE parcels_migrated (
                rowid       INTEGER PRIMARY KEY,
                prop_id     TEXT NOT NULL,
                county_fips TEXT,
                county_name TEXT,
                owner_name  TEXT,
                mail_addr   TEXT,
                situs_addr  TEXT,
                legal_desc  TEXT,
                gis_area    REAL,
                legal_area  REAL,
                shape_wkb   BLOB,
                updated_at  INTEGER,
                UNIQUE (county_fips, prop_id)
            )
        """)
        conn.execute("""
            INSERT INTO parcels_migrated
                (rowid, prop_id, county_fips, county_name, owner_name, mail_addr,
                 situs_addr, legal_desc, gis_area, legal_area, shape_wkb, updated_at)
            SELECT rowid, prop_id, county_fips, county_name, owner_name, mail_addr,
                   situs_addr, legal_desc, gis_area, legal_area, shape_wkb, updated_at
              FROM parcels
        """)
        conn.execute("DROP TABLE parcels")
        conn.execute("ALTER TABLE parcels_migrated RENAME TO parcels")
        conn.execute("CREATE INDEX IF NOT EXISTS ix_parcels_county ON parcels(county_fips)")
        conn.execute("CREATE INDEX IF NOT EXISTS ix_parcels_owner  ON parcels(owner_name)")
        conn.execute("CREATE INDEX IF NOT EXISTS ix_parcels_owner_nocase "
                     "ON parcels(owner_name COLLATE NOCASE)")
        conn.execute("CREATE INDEX IF NOT EXISTS ix_parcels_propid ON parcels(prop_id)")
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    after = conn.execute("SELECT COUNT(*) FROM parcels").fetchone()[0]
    print(f"[cache] migration done: {before:,} rows in, {after:,} rows out. "
          f"Re-bootstrap each county to recover parcels lost to the old constraint.",
          flush=True)
    return True


def _migrate_geom_key(conn):
    """Rebuild `parcels` if identity is still (county_fips, prop_id).

    Prop_ID is an appraisal ACCOUNT number and one account often covers several
    separate tracts, so that constraint silently kept one tract per account —
    771 of 5,330 parcels, 14.5%, in one 3-mile search. Identity is now
    (county_fips, prop_id, geom_key).

    Rowids are preserved so parcels_rtree stays valid. The rebuild backfills
    geom_key from the stored geometry, which makes the constraint correct going
    forward; it cannot invent the tracts already dropped, so each county still
    needs a re-bootstrap to recover them. cache_meta is marked 'partial' to say
    so rather than leaving counties looking complete.
    """
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='parcels'").fetchone()
    if not row or not row[0]:
        return False
    sql = row[0]
    if "geom_key" in sql:
        return False                       # already migrated

    print("[cache] migrating parcels: identity now (county, prop_id, geometry)",
          flush=True)
    before = conn.execute("SELECT COUNT(*) FROM parcels").fetchone()[0]
    conn.execute("BEGIN")
    try:
        conn.execute("""
            CREATE TABLE parcels_migrated (
                rowid       INTEGER PRIMARY KEY,
                prop_id     TEXT NOT NULL,
                geom_key    TEXT,
                county_fips TEXT,
                county_name TEXT,
                owner_name  TEXT,
                mail_addr   TEXT,
                situs_addr  TEXT,
                legal_desc  TEXT,
                gis_area    REAL,
                legal_area  REAL,
                shape_wkb   BLOB,
                updated_at  INTEGER,
                UNIQUE (county_fips, prop_id, geom_key)
            )
        """)
        rows = conn.execute("""
            SELECT rowid, prop_id, county_fips, county_name, owner_name, mail_addr,
                   situs_addr, legal_desc, gis_area, legal_area, shape_wkb, updated_at
              FROM parcels
        """).fetchall()
        conn.executemany("""
            INSERT INTO parcels_migrated
                (rowid, prop_id, geom_key, county_fips, county_name, owner_name,
                 mail_addr, situs_addr, legal_desc, gis_area, legal_area,
                 shape_wkb, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, [(r[0], r[1], _geom_key(r[10]) if r[10] else None, r[2], r[3], r[4],
               r[5], r[6], r[7], r[8], r[9], r[10], r[11]) for r in rows])
        conn.execute("DROP TABLE parcels")
        conn.execute("ALTER TABLE parcels_migrated RENAME TO parcels")
        for ddl in (
            "CREATE INDEX IF NOT EXISTS ix_parcels_county ON parcels(county_fips)",
            "CREATE INDEX IF NOT EXISTS ix_parcels_propid ON parcels(prop_id)",
            "CREATE INDEX IF NOT EXISTS ix_parcels_owner  ON parcels(owner_name)",
            "CREATE INDEX IF NOT EXISTS ix_parcels_owner_nocase "
            "  ON parcels(owner_name COLLATE NOCASE)",
        ):
            conn.execute(ddl)
        # The data is now correctly keyed but still short the dropped tracts.
        conn.execute("UPDATE cache_meta SET status='partial' WHERE status='fresh'")
        conn.execute("DELETE FROM cache_tiles")     # force a full re-fetch
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    after = conn.execute("SELECT COUNT(*) FROM parcels").fetchone()[0]
    print(f"[cache] migration done: {before:,} rows preserved ({after:,} after). "
          f"Counties marked 'partial' — re-bootstrap to recover the tracts the "
          f"old key dropped.", flush=True)
    return True


def cache_status():
    """Return per-county cache state for the admin UI / status checks."""
    init_db()
    with _db_lock, _conn() as c:
        rows = c.execute("""
            SELECT county_fips, county_name, bootstrapped_at, last_refreshed_at,
                   parcel_count, status
              FROM cache_meta
        """).fetchall()
    seen = {r[0]: r for r in rows}
    out = []
    now = int(time.time())
    for fips, name in HOUSTON_METRO_COUNTIES:
        r = seen.get(fips)
        if r:
            age_days = (now - (r[3] or 0)) / 86400 if r[3] else None
            out.append({
                "county_fips": fips, "county_name": name,
                "bootstrapped_at": r[2], "last_refreshed_at": r[3],
                "parcel_count": r[4] or 0, "status": r[5] or "unknown",
                "age_days": round(age_days, 1) if age_days is not None else None,
            })
        else:
            out.append({
                "county_fips": fips, "county_name": name,
                "bootstrapped_at": None, "last_refreshed_at": None,
                "parcel_count": 0, "status": "pending", "age_days": None,
            })
    return out


def _get_county_polygon(county_fips: str):
    """Pull a county's boundary polygon from Census TIGER. Returns a SIMPLIFIED
    shapely geom — TIGER polygons have thousands of vertices and Esri's REST API
    rejects overly-complex polygon parameters (HTTP 400). 0.003° tolerance is
    ~300m of edge slop, fine for county-level filtering."""
    from shapely.geometry import shape as shp_shape
    # Lazy import to avoid circular dep with app.py
    from acq_gis import arcgis_query, ENDPOINTS
    state_fips = county_fips[:2]
    county_only = county_fips[2:]
    fc = arcgis_query(
        ENDPOINTS["counties"],
        where=f"STATE='{state_fips}' AND COUNTY='{county_only}'",
        out_fields="*",
        page_size=10, max_pages=1, parallel_pagination=False,
    )
    features = fc.get("features") or []
    if not features:
        raise RuntimeError(f"Census TIGER returned no county for FIPS {county_fips}")
    poly = shp_shape(features[0]["geometry"])
    # Simplify aggressively. preserve_topology=True keeps the shape valid.
    simplified = poly.simplify(0.003, preserve_topology=True)
    if not simplified.is_empty and simplified.is_valid:
        return simplified
    return poly


TILE_SIZE_DEG = 0.15   # ~10-mile squares; small enough for Esri to accept


# Tiles at or above this parcel count are split before they are fetched.
# Between the largest tile that succeeded (64,921) and the smallest that was
# refused (84,375), so it splits every tile that would 400 without splitting
# any that would have worked.
DENSE_TILE_PARCELS = 75_000


def _iter_tile_features(bbox, out_fields, attempts=3, depth=0, max_depth=3, label=""):
    """Yield (features, failures) batches for one bbox tile.

    A tile that fails is not a tile that is empty, and the loader used to treat
    them the same: count the failure, move on, leave a hole. Harris lost 11 of
    32 tiles that way in a single run and came back with 602,126 parcels
    instead of roughly 1.5M.

    The two failure modes want different handling, so this does both:

      transient   'All strategies failed' -- the service was briefly unreachable
                  or throttling. Six consecutive tiles failed this way in one
                  run, which is a window in time, not a property of those tiles.
                  Retried with backoff.

      refused     HTTP 400 'Unable to perform query' on the densest urban tiles,
                  reproducible run to run. The same envelope split into
                  quadrants succeeds, so a tile that still fails after its
                  retries is subdivided rather than dropped.

    Yields rather than returns so a tile that subdivides several levels deep
    costs one quadrant of memory, not the whole subtree.
    """
    from acq_gis import arcgis_query, ENDPOINTS, _count_query

    # Split a tile the service is going to refuse BEFORE spending ~290s finding
    # out. Measured across the Harris grid: every tile that returned HTTP 400
    # held at least 84,375 parcels, and every tile that succeeded held at most
    # 64,921. A count query costs seconds and separates the two cleanly.
    #
    # A count that fails tells us nothing, so fall through and try the fetch --
    # this is an optimisation, not a gate.
    if depth < max_depth:
        try:
            n = _count_query(ENDPOINTS["parcels"], None, bbox, "1=1", 60)
            if isinstance(n, int) and n > DENSE_TILE_PARCELS:
                minx, miny, maxx, maxy = bbox
                mx, my = (minx + maxx) / 2.0, (miny + maxy) / 2.0
                print(f"  [cache] {label}: {n:,} parcels -- splitting before fetch",
                      flush=True)
                for qi, quad in enumerate(((minx, miny, mx, my), (mx, miny, maxx, my),
                                           (minx, my, mx, maxy), (mx, my, maxx, maxy))):
                    yield from _iter_tile_features(quad, out_fields, attempts,
                                                   depth + 1, max_depth,
                                                   f"{label}.{qi + 1}")
                return
        except Exception:
            pass

    err = None
    tried = 0
    for attempt in range(attempts):
        tried += 1
        try:
            fc = arcgis_query(ENDPOINTS["parcels"], bbox=bbox, out_fields=out_fields,
                              page_size=2000, max_pages=200)
            yield (fc.get("features", []) or [], 0)
            return
        except Exception as e:
            err = e
            # A real ArcGIS error is deterministic -- _post_query does not retry
            # these either. Re-issuing the identical query only burns time; the
            # tile needs to be smaller, not attempted again.
            if "ArcGIS error" in str(e):
                break
            if attempt < attempts - 1:
                time.sleep(2 * (2 ** attempt))      # 2s, then 4s

    if depth >= max_depth:
        print(f"  [cache] {label}: giving up at depth {depth} after "
              f"{tried} attempt{'s' if tried != 1 else ''}: {str(err)[:90]}",
              flush=True)
        yield ([], 1)
        return

    minx, miny, maxx, maxy = bbox
    mx, my = (minx + maxx) / 2.0, (miny + maxy) / 2.0
    print(f"  [cache] {label}: {str(err)[:60]} -- splitting into quadrants",
          flush=True)
    for qi, quad in enumerate(((minx, miny, mx, my), (mx, miny, maxx, my),
                               (minx, my, mx, maxy), (mx, my, maxx, maxy))):
        yield from _iter_tile_features(quad, out_fields, attempts, depth + 1,
                                       max_depth, f"{label}.{qi + 1}")


def _insert_tile_batch(c, feats, poly_test, seen, county_fips, county_name, now):
    """Insert one batch of StratMap features. Returns (inserted, skipped_outside).

    Runs inside its own transaction so a load that dies keeps the tiles it has
    already committed.
    """
    from shapely.geometry import shape as shp_shape
    from shapely.wkb import dumps as wkb_dumps
    from acq_gis import to_float

    inserted = skipped_outside = 0
    c.execute("BEGIN")
    try:
        for f in feats:
            if not f.get("geometry"):
                continue
            props = f["properties"] or {}
            pid = str(props.get("Prop_ID") or "").strip()
            if not pid:
                continue
            try:
                g = shp_shape(f["geometry"])
            except Exception:
                continue
            if g.is_empty:
                continue
            wkb = wkb_dumps(g)
            gkey = _geom_key(wkb)
            # Dedup on the parcel, not the account. Overlapping tiles return the
            # same geometry and collapse here; two tracts under one Prop_ID have
            # different geometry and both survive.
            if (pid, gkey) in seen:
                continue
            # Drop parcels whose centroid is outside the actual county polygon --
            # cleanup of tile-edge artifacts from neighbouring counties
            try:
                if not poly_test.contains(g.centroid):
                    skipped_outside += 1
                    continue
            except Exception:
                pass                        # if the test fails, keep it -- safer
            seen.add((pid, gkey))
            gminx, gminy, gmaxx, gmaxy = g.bounds
            cur = c.execute("""
                INSERT OR REPLACE INTO parcels
                (prop_id, geom_key, county_fips, county_name, owner_name, mail_addr,
                 situs_addr, legal_desc, gis_area, legal_area, shape_wkb, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (pid, gkey, county_fips, county_name,
                  props.get("OWNER_NAME"), props.get("MAIL_ADDR"),
                  props.get("SITUS_ADDR"), props.get("LEGAL_DESC"),
                  to_float(props.get("GIS_AREA")), to_float(props.get("LEGAL_AREA")),
                  wkb, now))
            c.execute("""
                INSERT OR REPLACE INTO parcels_rtree (id, minx, maxx, miny, maxy)
                VALUES (?, ?, ?, ?, ?)
            """, (cur.lastrowid, gminx, gmaxx, gminy, gmaxy))
            inserted += 1
        c.execute("COMMIT")
    except Exception:
        c.execute("ROLLBACK")
        raise
    return inserted, skipped_outside


def bootstrap_county(county_fips: str, county_name: str, on_progress=None,
                     resume: bool = True) -> dict:
    """Pull all StratMap parcels for one county and load them into SQLite.

    Strategy: split the county's bounding box into ~10-mile bbox tiles and query
    StratMap with each tile separately. This works around Esri's polygon-complexity
    limit (Harris's TIGER polygon has 10,000+ vertices even after simplification,
    causing HTTP 400 on the spatial query). Tiles are clean envelope queries.
    Parcels whose centroid falls outside the actual county polygon are dropped
    (cleanup for tile-edge artifacts from neighbor counties).

    on_progress(pct, msg) is called as the load runs, for status display.

    resume=True picks up an interrupted load: if the county's last attempt did
    not finish and the tile grid is unchanged, tiles already recorded as done
    are skipped and the county's existing rows are kept. A county that is
    already 'fresh' always reloads from scratch, so a deliberate refresh still
    drops parcels that have disappeared upstream.
    """
    # The per-tile fetch and insert now live in _iter_tile_features and
    # _insert_tile_batch, which carry their own imports.
    from shapely.geometry import box as shp_box

    init_db()
    t0 = time.time()

    # Mark the county as 'loading' so the status UI shows it
    with _db_lock, _conn() as c:
        _ensure_heartbeat_column(c)
        c.execute("""
            INSERT INTO cache_meta (county_fips, county_name, status, loading_heartbeat)
            VALUES (?, ?, 'loading', ?)
            ON CONFLICT(county_fips) DO UPDATE SET
                status='loading', loading_heartbeat=excluded.loading_heartbeat
        """, (county_fips, county_name, int(time.time())))

    try:
        if on_progress: on_progress(2, f"Fetching {county_name} County boundary…")
        poly = _get_county_polygon(county_fips)
        minx, miny, maxx, maxy = poly.bounds

        # Build the tile grid covering the county bbox
        tiles = []
        x = minx
        while x < maxx:
            y = miny
            while y < maxy:
                tile = (x, y, min(x + TILE_SIZE_DEG, maxx), min(y + TILE_SIZE_DEG, maxy))
                if poly.intersects(shp_box(*tile)):
                    tiles.append(tile)
                y += TILE_SIZE_DEG
            x += TILE_SIZE_DEG
        # A grid signature so a resume can only reuse tiles cut the same way.
        # Change TILE_SIZE_DEG or the county boundary and every prior tile is
        # discarded rather than silently mismatched.
        grid_sig = f"{TILE_SIZE_DEG}:{len(tiles)}:" + ",".join(
            f"{v:.4f}" for v in (minx, miny, maxx, maxy))

        prior = None
        with _db_lock, _conn() as c0:
            row = c0.execute("SELECT status FROM cache_meta WHERE county_fips=?",
                             (county_fips,)).fetchone()
            prior = row[0] if row else None
            done_tiles = set()
            if resume and prior in ("partial", "error", "loading"):
                done_tiles = {r[0] for r in c0.execute(
                    "SELECT tile_index FROM cache_tiles "
                    " WHERE county_fips=? AND grid_sig=?", (county_fips, grid_sig))}
            if not done_tiles:
                # Not resuming: drop any stale tile record for this county.
                c0.execute("DELETE FROM cache_tiles WHERE county_fips=?", (county_fips,))

        resuming = bool(done_tiles)
        prior_failures = 0
        if resuming:
            have = 0
            with _db_lock, _conn() as c0:
                have = c0.execute("SELECT COUNT(*) FROM parcels WHERE county_fips=?",
                                  (county_fips,)).fetchone()[0]
                # Failures from the tiles being skipped still count against this
                # county. Without this a resumed run reports 'fresh' while the
                # holes an earlier attempt left are still there.
                prior_failures = c0.execute(
                    "SELECT COALESCE(SUM(failures), 0) FROM cache_tiles "
                    " WHERE county_fips=? AND grid_sig=?",
                    (county_fips, grid_sig)).fetchone()[0] or 0
            print(f"[cache] {county_name}: resuming — {len(done_tiles)}/{len(tiles)} "
                  f"tiles already loaded, {have:,} parcels kept", flush=True)
            if on_progress:
                on_progress(5, f"Resuming {county_name}: {len(done_tiles)}/{len(tiles)} "
                               f"tiles already done…")
        elif on_progress:
            on_progress(5, f"Querying {len(tiles)} bbox tiles for {county_name}…")

        # Fetch AND insert one tile at a time. Each tile uses a clean envelope
        # query (no polygon complexity issues); pages within a tile run in
        # parallel.
        #
        # Streaming per tile rather than accumulating the county is deliberate.
        # This used to build one `all_features` list holding every parcel in the
        # county before inserting any of them. For Harris -- ~1.5M parcels of
        # polygon geometry -- that ran the process to 3.5 GB and it was killed
        # mid-load. A kill is not an exception, so the except branch below never
        # ran, and the county sat pinned at 'loading' with half its parcels and
        # no error anywhere. Peak memory is now one tile.
        tile_failures = 0
        fetched = 0
        inserted = 0
        skipped_outside = 0
        seen = set()
        now = int(time.time())

        # 1.5M point-in-polygon tests against a raw county polygon is the other
        # half of Harris's runtime. Preparing the polygon builds an index once.
        try:
            from shapely.prepared import prep as _prep
            poly_test = _prep(poly)
        except Exception:
            poly_test = poly

        with _db_lock, _conn() as c:
            if resuming:
                tile_failures += prior_failures
                # Keep what is already loaded, and seed the dedup set from it so
                # tile-edge overlaps are not counted twice.
                for pid, gkey in c.execute(
                        "SELECT prop_id, geom_key FROM parcels WHERE county_fips=?",
                        (county_fips,)):
                    seen.add((pid, gkey))
                inserted = len(seen)
            else:
                # Clear any prior rows for this county before re-insert
                c.execute("""
                    DELETE FROM parcels_rtree WHERE id IN (
                        SELECT rowid FROM parcels WHERE county_fips = ?
                    )
                """, (county_fips,))
                c.execute("DELETE FROM parcels WHERE county_fips = ?", (county_fips,))

            FIELDS = ("Prop_ID,OWNER_NAME,LEGAL_DESC,SITUS_ADDR,MAIL_ADDR,"
                      "LEGAL_AREA,GIS_AREA")
            for i, tile_bbox in enumerate(tiles):
                if i in done_tiles:
                    continue
                label = f"{county_name} tile {i+1}/{len(tiles)}"
                tile_ins = tile_fail = 0
                for feats, failed in _iter_tile_features(tile_bbox, FIELDS, label=label):
                    tile_failures += failed
                    fetched += len(feats)
                    ins, skip = _insert_tile_batch(c, feats, poly_test, seen,
                                                   county_fips, county_name, now)
                    inserted += ins
                    tile_ins += ins
                    tile_fail += failed
                    skipped_outside += skip
                    feats = None        # release the batch before fetching the next

                    # Beat per batch, not per tile. A tile that fails after ~290s
                    # and then subdivides into quadrants can run many minutes,
                    # and beating only at the tile boundary let the gap approach
                    # STALE_LOADING_SEC -- at which point another process opening
                    # the cache would declare this live load dead. Measured at
                    # 520s between beats on Harris before this moved inside.
                    c.execute("UPDATE cache_meta SET loading_heartbeat=? "
                              " WHERE county_fips=?", (int(time.time()), county_fips))

                # Record the tile as done only once its batches are committed,
                # so an interruption mid-tile re-fetches that tile rather than
                # skipping a partial one.
                c.execute("""
                    INSERT OR REPLACE INTO cache_tiles
                    (county_fips, grid_sig, tile_index, parcels, failures, done_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                """, (county_fips, grid_sig, i, tile_ins, tile_fail, int(time.time())))

                if on_progress:
                    pct = 5 + int(90 * (i + 1) / max(1, len(tiles)))
                    on_progress(pct,
                                f"Tile {i+1}/{len(tiles)} - {inserted:,} parcels loaded...")

            if not inserted:
                raise RuntimeError(f"No parcels returned across {len(tiles)} tiles")

            # A county whose tiles partly failed is NOT fresh. The loader
            # deletes the county's rows before re-inserting, so a run that lost
            # tiles leaves a hole — and marking that 'fresh' is how a county
            # ends up looking healthy while a third of it is absent. Harris lost
            # 12 of 32 tiles to a DNS blip and reported fresh regardless.
            final_status = "partial" if tile_failures else "fresh"
            c.execute("""
                INSERT INTO cache_meta
                (county_fips, county_name, bootstrapped_at, last_refreshed_at,
                 parcel_count, status)
                VALUES (?, ?, COALESCE((SELECT bootstrapped_at FROM cache_meta WHERE county_fips=?), ?),
                        ?, ?, ?)
                ON CONFLICT(county_fips) DO UPDATE SET
                    county_name=excluded.county_name,
                    last_refreshed_at=excluded.last_refreshed_at,
                    parcel_count=excluded.parcel_count,
                    status=excluded.status,
                    loading_heartbeat=NULL
            """, (county_fips, county_name, county_fips, now, now, inserted, final_status))

        elapsed = round(time.time() - t0, 1)
        msg = (f"{county_name}: {inserted:,} parcels in {elapsed}s "
               f"(tiles={len(tiles)}, skipped-out-of-county={skipped_outside}"
               + (f", tile-failures={tile_failures}" if tile_failures else "") + ")")
        if on_progress: on_progress(100, msg)
        print(f"[cache] {msg}", flush=True)
        return {"county_fips": county_fips, "county_name": county_name,
                "parcel_count": inserted, "elapsed_sec": elapsed,
                "tiles": len(tiles), "tile_failures": tile_failures,
                "status": final_status,
                "complete": tile_failures == 0}
    except Exception as e:
        with _db_lock, _conn() as c:
            c.execute("UPDATE cache_meta SET status='error', loading_heartbeat=NULL "
                      " WHERE county_fips=?", (county_fips,))
        raise


def query_parcels_in_polygon(buffer_wgs, min_acres=0, max_acres=1e12):
    """Return parcels intersecting the buffer with StratMap-acres in [min, max].
    Uses R-Tree bbox pre-filter then shapely exact intersect. Fast (< 1s typical)."""
    from shapely.wkb import loads as wkb_loads
    from shapely.geometry import mapping as shp_mapping

    init_db()
    bbox = buffer_wgs.bounds
    minx, miny, maxx, maxy = bbox

    with _db_lock, _conn() as c:
        # R-Tree bbox filter first — drops from millions to thousands
        cur = c.execute("""
            SELECT p.prop_id, p.county_name, p.owner_name, p.mail_addr, p.situs_addr,
                   p.legal_desc, p.gis_area, p.legal_area, p.shape_wkb
              FROM parcels p
              JOIN parcels_rtree r ON p.rowid = r.id
             WHERE r.maxx >= ? AND r.minx <= ?
               AND r.maxy >= ? AND r.miny <= ?
        """, (minx, maxx, miny, maxy))
        candidates = cur.fetchall()

    features = []
    for row in candidates:
        try:
            g = wkb_loads(row[8])
        except Exception:
            continue
        if g.is_empty or not g.intersects(buffer_wgs):
            continue
        gis_area = row[6] or 0
        legal_area = row[7] or 0
        acres = gis_area or legal_area or 0
        # Server-side acreage filter is done by Shape_Area in the live path;
        # here we have the StratMap acres directly so we can pre-filter against
        # the user's wide guesstimate range. But StratMap is unreliable for
        # re-platted parcels, so we keep the band VERY wide and let the post-
        # overlay re-filter in run_search() apply the user's precise range.
        # Just dropping obvious outliers cuts work significantly.
        if acres > 0 and acres < min_acres * 0.05:
            continue   # way too small — even StratMap can't be that wrong
        features.append({
            "type": "Feature",
            "properties": {
                "Prop_ID":    row[0],
                "_county":    row[1],
                "OWNER_NAME": row[2],
                "MAIL_ADDR":  row[3],
                "SITUS_ADDR": row[4],
                "LEGAL_DESC": row[5],
                "GIS_AREA":   row[6],
                "LEGAL_AREA": row[7],
                "Acres":      round(acres, 1),
            },
            "geometry": shp_mapping(g),
        })

    return {"type": "FeatureCollection", "features": features}


def coverage_for_polygon(buffer_wgs) -> list:
    """Return which counties this buffer touches and their cache status.
    Used by the search path to decide cache-vs-live."""
    from acq_gis import arcgis_query, ENDPOINTS

    # arcgis_query expects a shapely polygon (NOT a GeoJSON dict)
    fc = arcgis_query(
        ENDPOINTS["counties"],
        geometry_polygon=buffer_wgs,
        out_fields="STATE,COUNTY,BASENAME,NAME",
        page_size=20, max_pages=2, parallel_pagination=False,
    )
    out = []
    status = {row["county_fips"]: row for row in cache_status()}
    for f in fc.get("features", []):
        p = f.get("properties") or {}
        fips = (p.get("STATE") or "") + (p.get("COUNTY") or "")
        name = p.get("BASENAME") or p.get("NAME") or ""
        st = status.get(fips)
        out.append({
            "county_fips": fips,
            "county_name": name,
            "cached":   bool(st and st["status"] == "fresh"),
            "status":   st["status"] if st else "not-tracked",
            "age_days": st["age_days"] if st else None,
        })
    return out


def is_fully_cached(coverage_list) -> bool:
    """True when every county in coverage is 'fresh'."""
    return bool(coverage_list) and all(c["cached"] for c in coverage_list)


def find_parcel_by_pid(pid: str, county: str = None, strict: bool = False):
    """Direct lookup of a single parcel by Prop_ID. Returns a GeoJSON Feature
    (with shape geometry) or None.

    Prop_IDs are NOT globally unique — the same numeric ID can exist in
    different counties' StratMap data (e.g. 188287 is a 2,362-ac ranch in
    Fort Bend AND a 0.1-ac residential lot in Galveston). Always pass the
    `county` hint when you have it so we disambiguate to the right parcel.
    Without it, we return whatever sqlite happens to find first — that's the
    bug pattern that sent "Full tract page" to the wrong tract.

    When `strict=True` AND a county hint is given, return None if no parcel
    matches the county — do NOT fall back to a wrong-county match. This is
    critical because some outreach records reference counties that aren't in
    the cache at all (Grimes, Walker, etc.) — silently returning a different
    county's parcel for the same prop_id produces the "wrong tract" bug."""
    from shapely.wkb import loads as wkb_loads
    from shapely.geometry import mapping as shp_mapping
    init_db()
    if not pid:
        return None
    with _db_lock, _conn() as c:
        row = None
        if county:
            row = c.execute("""
                SELECT prop_id, county_name, owner_name, mail_addr, situs_addr,
                       legal_desc, gis_area, legal_area, shape_wkb
                  FROM parcels
                 WHERE prop_id = ? AND LOWER(county_name) = LOWER(?) LIMIT 1
            """, (pid, county.strip())).fetchone()
            if not row and strict:
                # Caller insists on this county — refuse to return a different one.
                return None
        if not row:
            # Fallback: no county hint, or hint didn't match — return any match
            # (better to surface SOMETHING than 404 on a known-good prop_id).
            row = c.execute("""
                SELECT prop_id, county_name, owner_name, mail_addr, situs_addr,
                       legal_desc, gis_area, legal_area, shape_wkb
                  FROM parcels WHERE prop_id = ? LIMIT 1
            """, (pid,)).fetchone()
    if not row:
        return None
    try:
        g = wkb_loads(row[8])
    except Exception:
        return None
    return {
        "type": "Feature",
        "properties": {
            "Prop_ID":    row[0],
            "_county":    row[1],
            "OWNER_NAME": row[2],
            "MAIL_ADDR":  row[3],
            "SITUS_ADDR": row[4],
            "LEGAL_DESC": row[5],
            "GIS_AREA":   row[6],
            "LEGAL_AREA": row[7],
            "Acres":      round((row[6] or row[7] or 0), 1),
        },
        "geometry": shp_mapping(g),
    }


# Corporate suffixes and filler that carry no identifying signal. Stripped before
# comparing names so "GRAND PRAIRIE DEV LLC" and "GRAND PRAIRIE DEVELOPMENT, L.L.C."
# reduce to the same distinctive tokens.
_OWNER_NOISE = {
    "LLC", "LC", "INC", "INCORPORATED", "CORP", "CORPORATION", "CO",
    "COMPANY", "LP", "LLP", "LTD", "LIMITED", "PARTNERSHIP", "PARTNERS",
    "TRUST", "TRUSTEE", "TRUSTEES", "ETAL", "ET", "AL", "THE", "OF", "AND",
    "FAMILY", "REVOCABLE", "LIVING", "ESTATE", "PROPERTIES", "PROPERTY",
    "HOLDINGS", "HOLDING", "INVESTMENTS", "INVESTMENT", "GROUP", "ENTERPRISES",
}


def _norm_owner(name):
    """Uppercase, drop punctuation, collapse whitespace.

    This is what stops an extra space or a stray comma from mattering:
    "SMITH , JOHN  A" and "SMITH JOHN A" both normalise to "SMITH JOHN A".
    """
    import re as _re
    t = (name or "").upper()
    t = _re.sub(r"[^A-Z0-9 ]+", " ", t)
    return _re.sub(r" +", " ", t).strip()


def _owner_tokens(name):
    """Distinctive tokens - normalised, minus corporate filler."""
    return [t for t in _norm_owner(name).split() if t not in _OWNER_NOISE and len(t) > 1]


def _owner_similarity(a, b):
    """0..1 similarity between two owner names.

    Blends whole-string ratio with token overlap, so a one-letter typo still
    scores high while a shared corporate suffix alone does not - "SMITH LLC" and
    "JONES LLC" have no distinctive token in common.
    """
    from difflib import SequenceMatcher
    na, nb = _norm_owner(a), _norm_owner(b)
    if not na or not nb:
        return 0.0
    if na == nb:
        return 1.0
    whole = SequenceMatcher(None, na, nb).ratio()
    ta, tb = set(_owner_tokens(a)), set(_owner_tokens(b))
    if not ta or not tb:
        return whole
    # Count a token as shared if it matches exactly or near-exactly, so a typo
    # inside one word still lands.
    inter = 0
    for x in ta:
        if x in tb:
            inter += 1
            continue
        if any(SequenceMatcher(None, x, y).ratio() >= 0.86 for y in tb):
            inter += 1
    tok = inter / max(len(ta), len(tb))
    # No distinctive token in common means it is not the same entity, however
    # similar the raw strings look. Without this cap a substring match dragged in
    # "SHADY PEMBERTON" for a search on "EMBER", and "KATY FARMS LP" scored 0.76
    # against "HOCKLEY FARMS LP" purely on the shared word FARMS.
    if inter == 0:
        return min(whole, 0.60)
    return 0.5 * whole + 0.5 * tok


def find_parcels_by_owner(owner_query: str, exact: bool = False, limit: int = 500,
                          min_score: float = 0.72):
    """Find every cached parcel owned by an entity matching `owner_query`.

    Matching runs in widening passes so a name that is merely *close* still
    lands. An extra space, a comma, a dropped "DEVELOPMENT" or a single-letter
    typo previously returned nothing:

      1. exact on the raw name        - fast, indexed
      2. normalised match             - punctuation and whitespace insensitive
      3. LIKE %query%                 - substring
      4. token pass                   - LIKE on each distinctive token, then
                                        score every candidate and keep those at
                                        or above `min_score`

    Results are deduped by prop_id and carry `match_score` and `match_pass` so
    the caller can show why a row matched. exact=True stops after pass 1.
    """
    from shapely.wkb import loads as wkb_loads
    init_db()
    if not owner_query or not owner_query.strip():
        return {"parcels": [], "total_count": 0, "total_acres": 0, "by_county": []}
    q_raw = owner_query.strip()
    q = q_raw.upper()
    qn = _norm_owner(owner_query)
    COLS = ("prop_id, county_name, owner_name, mail_addr, situs_addr, "
            "legal_desc, gis_area, legal_area, shape_wkb")

    seen, rows, passes = set(), [], {}
    owner_seen = set()

    def _add(fetched, label):
        for r in fetched:
            if r[0] in seen:
                continue
            seen.add(r[0])
            rows.append(r)
            passes[r[0]] = label

    import re as _re
    _boundary = _re.compile(r"(?:^| )" + _re.escape(qn) + r"(?: |$)") if qn else None

    def _candidate_pass_label(owner_name, label):
        sc = _owner_similarity(owner_query, owner_name)
        if sc >= min_score:
            return label
        if _boundary and _boundary.search(_norm_owner(owner_name)):
            return "partial"
        return None

    def _add_owner_name_candidates(c, owner_names, label):
        for owner_name in owner_names:
            if not owner_name or owner_name in owner_seen:
                continue
            owner_seen.add(owner_name)
            accepted_label = _candidate_pass_label(owner_name, label)
            if not accepted_label:
                continue
            _add(c.execute("SELECT " + COLS + " FROM parcels WHERE owner_name = ?"
                           " ORDER BY gis_area DESC LIMIT ?",
                           (owner_name, limit)).fetchall(), accepted_label)

    # Each LIKE '%x%' is a full scan of ~2.7M rows, so widen only when the
    # previous pass came up short and avoid forcing SQLite to sort those scans.
    # Running all four every time made a simple lookup take 20 seconds.
    ENOUGH = 5
    OWNER_NAME_CANDIDATE_CAP = 5000

    with _db_lock, _conn() as c:
        _add(c.execute("SELECT " + COLS + " FROM parcels WHERE owner_name = ? COLLATE NOCASE"
                       " ORDER BY gis_area DESC LIMIT ?", (q_raw, limit)).fetchall(), "exact")
        exact_count = len(rows)
        if not exact:
            if len(rows) < ENOUGH and qn and qn != q:
                like = "%" + "%".join(qn.split()) + "%"
                owners = [r[0] for r in c.execute(
                    "SELECT owner_name FROM parcels"
                    " WHERE owner_name LIKE ? COLLATE NOCASE"
                    " GROUP BY owner_name LIMIT ?",
                    (like, OWNER_NAME_CANDIDATE_CAP)).fetchall()]
                _add_owner_name_candidates(c, owners, "normalised")
            if len(rows) < ENOUGH:
                owners = [r[0] for r in c.execute(
                    "SELECT owner_name FROM parcels"
                    " WHERE owner_name LIKE ? COLLATE NOCASE"
                    " GROUP BY owner_name LIMIT ?",
                    ("%" + q + "%", OWNER_NAME_CANDIDATE_CAP)).fetchall()]
                _add_owner_name_candidates(c, owners, "contains")
            # Longest distinctive tokens are the best cheap proxy for rarity.
            toks = sorted(set(_owner_tokens(owner_query)), key=len, reverse=True)[:2]
            # If the user searched a specific owner name and we already found
            # exact rows, still do one small variant pass. That catches the
            # common parcel-cache reality where one tract has a typo, middle
            # initial shuffle, or punctuation/spacing drift. Keep one-token
            # partial searches on the old narrower path, and skip the extra
            # scans once the exact owner already filled the response.
            specific_owner_variant_pass = bool(
                0 < exact_count < limit and len(toks) >= 2
            )
            if len(rows) < ENOUGH or specific_owner_variant_pass:
                for t in toks:
                    owners = [r[0] for r in c.execute(
                        "SELECT owner_name FROM parcels"
                        " WHERE owner_name LIKE ? COLLATE NOCASE"
                        " GROUP BY owner_name LIMIT ?",
                        ("%" + t + "%", OWNER_NAME_CANDIDATE_CAP)).fetchall()]
                    _add_owner_name_candidates(c, owners, "fuzzy")

    # Every non-exact pass is score-filtered, with one deliberate exception:
    # a substring that lands on whole token boundaries is a real hit even when
    # the overall score is low, because that is what a partial-name search is.
    # Searching "EMBER" should find "EMBER GROUP LLC" and "MANDELL DAVID & EMBER"
    # but not "SHADY PEMBERTON" or "MEMBERS CHOICE CREDIT UNION", where the
    # letters only appear mid-word. Require the right boundary too, otherwise
    # "EMBER" also matches unrelated names like "EMBERG".
    scored = []
    for r in rows:
        label = passes.get(r[0], "fuzzy")
        if label == "exact":
            scored.append((1.0, label, r))
            continue
        sc = _owner_similarity(owner_query, r[2])
        if sc < min_score:
            if not (_boundary and _boundary.search(_norm_owner(r[2]))):
                continue
            label = "partial"
            sc = max(sc, min_score)
        scored.append((sc, label, r))
    scored.sort(key=lambda x: (-x[0], -(x[2][6] or x[2][7] or 0)))
    scored = scored[:limit]
    rows = [x[2] for x in scored]
    score_by_pid = {x[2][0]: (round(x[0], 3), x[1]) for x in scored}

    parcels = []
    by_county = {}
    for r in rows:
        prop_id, county, owner, mail, situs, legal, gis_area, legal_area, wkb = r
        acres = round((gis_area or legal_area or 0), 1)
        # Compute centroid from WKB for "go to map" action
        try:
            g = wkb_loads(wkb)
            cent = g.centroid
            lat, lon = cent.y, cent.x
            bounds = g.bounds   # minx, miny, maxx, maxy for fitBounds
        except Exception:
            lat = lon = None
            bounds = None
        _sc, _pass = score_by_pid.get(prop_id, (None, None))
        parcels.append({
            "prop_id": prop_id, "county": county, "owner_name": owner,
            "mail_addr": mail, "situs_addr": situs, "legal_desc": legal,
            "acres": acres, "lat": lat, "lon": lon, "bounds": list(bounds) if bounds else None,
            "match_score": _sc, "match_pass": _pass,
        })
        cn = county or "?"
        by_county.setdefault(cn, {"count": 0, "acres": 0})
        by_county[cn]["count"] += 1
        by_county[cn]["acres"] += acres

    by_county_list = sorted(
        [{"county": k, "count": v["count"], "acres": round(v["acres"], 1)}
         for k, v in by_county.items()],
        key=lambda x: -x["acres"]
    )
    total_acres = round(sum(p["acres"] for p in parcels), 1)
    # Distinct spellings that matched, so the UI can show what the fuzzy pass
    # actually pulled in rather than silently blending them.
    variants = {}
    for pp in parcels:
        v = variants.setdefault(pp["owner_name"],
                                {"owner_name": pp["owner_name"], "count": 0,
                                 "acres": 0.0, "score": pp["match_score"]})
        v["count"] += 1
        v["acres"] += pp["acres"]
    variant_list = sorted(({**v, "acres": round(v["acres"], 1)} for v in variants.values()),
                          key=lambda x: -x["acres"])
    return {
        "parcels": parcels,
        "total_count": len(parcels),
        "total_acres": total_acres,
        "by_county": by_county_list,
        "variants": variant_list,
        "truncated": len(parcels) >= limit,
    }


def rtree_orphan_count(conn=None) -> int:
    """R-Tree entries pointing at parcels that no longer exist.

    Cheap approximation: every live parcel has exactly one R-Tree row, so the
    difference between the two counts is the orphan count. Exact enough to
    report, and it avoids the anti-join scan over millions of rows.
    """
    def _q(c):
        rt = c.execute("SELECT COUNT(*) FROM parcels_rtree").fetchone()[0]
        p  = c.execute("SELECT COUNT(*) FROM parcels").fetchone()[0]
        return max(0, rt - p)
    if conn is not None:
        return _q(conn)
    with _db_lock, _conn() as c:
        return _q(c)


def vacuum_rtree_orphans(on_progress=None) -> dict:
    """Delete R-Tree entries whose parcel row is gone.

    INSERT OR REPLACE on a table whose rowid is INTEGER PRIMARY KEY AUTOINCREMENT
    does not reuse the replaced row's rowid: the old row is deleted and a new one
    is inserted further up the sequence. The R-Tree row keyed to the old rowid is
    left behind, and nothing ever removed it.

    Under the pre-composite-unique schema, every cross-county Prop_ID collision
    took that path -- 445,758 of them -- so the index grew to roughly three
    times the number of parcels it indexed. Searches stayed correct, because
    query_parcels_in_polygon inner-joins parcels to the R-Tree and orphans match
    nothing, but every spatial query walked the dead entries first.

    Safe to run at any time; it only removes rows that can never match.
    """
    t0 = time.time()
    with _db_lock, _conn() as c:
        before = c.execute("SELECT COUNT(*) FROM parcels_rtree").fetchone()[0]
        parcels = c.execute("SELECT COUNT(*) FROM parcels").fetchone()[0]
        if on_progress:
            on_progress(5, f"Scanning {before:,} index entries against {parcels:,} parcels...")
        c.execute("BEGIN")
        try:
            c.execute("""
                DELETE FROM parcels_rtree
                 WHERE id NOT IN (SELECT rowid FROM parcels)
            """)
            c.execute("COMMIT")
        except Exception:
            c.execute("ROLLBACK")
            raise
        after = c.execute("SELECT COUNT(*) FROM parcels_rtree").fetchone()[0]

    removed = before - after
    elapsed = round(time.time() - t0, 1)
    msg = (f"R-Tree vacuum: removed {removed:,} orphaned entries "
           f"({before:,} -> {after:,}) in {elapsed}s")
    if on_progress:
        on_progress(100, msg)
    print(f"[cache] {msg}", flush=True)
    return {"before": before, "after": after, "removed": removed,
            "parcels": parcels, "elapsed_sec": elapsed}


_layer_edit_cache = {"at": 0, "value": None}


def upstream_last_edit(max_age_sec=3600):
    """Epoch seconds when StratMap last edited the parcel layer, or None.

    The layer is an annual-ish snapshot -- Stratmap25_landparcels_48, last
    edited 2026-06-04 as of this writing -- but the refresh loop re-downloaded
    every county weekly regardless. Harris alone is roughly 1.4 GB of polygon
    geometry pulled at about 0.4 MB/s, so that was hours of churn per county
    per week to arrive at byte-identical data, and every re-run was another
    chance to hit a partial load.

    One metadata request answers whether there is anything to fetch. Cached
    briefly so a sweep over 14 counties does not ask 14 times.
    """
    now = time.time()
    if _layer_edit_cache["value"] is not None and             now - _layer_edit_cache["at"] < max_age_sec:
        return _layer_edit_cache["value"]
    try:
        import requests
        from acq_gis import ENDPOINTS
        base = ENDPOINTS["parcels"].rsplit("/query", 1)[0]
        d = requests.get(base + "?f=json", timeout=30).json()
        ms = (d.get("editingInfo") or {}).get("dataLastEditDate")
        val = (ms / 1000.0) if isinstance(ms, (int, float)) else None
    except Exception as e:
        print(f"[cache] could not read upstream edit date: {e}", flush=True)
        val = None
    _layer_edit_cache.update({"at": now, "value": val})
    return val


def refresh_stale_counties(max_age_days=REFRESH_INTERVAL_DAYS, on_progress=None):
    """Background-worthy: find counties whose cache is older than max_age_days
    and re-bootstrap them. Skips counties that have never been bootstrapped."""
    status = cache_status()
    now = int(time.time())
    refreshed = []

    # Age alone is the wrong trigger. The upstream layer is an annual-ish
    # snapshot, so a county older than max_age_days is usually still identical
    # to the source -- re-downloading it costs hours and changes nothing. Ask
    # the service when it last changed and skip anything already newer.
    upstream = upstream_last_edit()
    for row in status:
        if row["last_refreshed_at"] is None:
            continue   # never bootstrapped — don't auto-bootstrap, leave to user
        # A county whose last load did not finish is refreshed regardless of
        # what upstream says: the gap is ours, not theirs.
        incomplete = row.get("status") in ("partial", "error")
        if upstream is not None and not incomplete                 and row["last_refreshed_at"] >= upstream:
            continue
        age = (now - row["last_refreshed_at"]) / 86400
        if age >= max_age_days or incomplete:

            try:
                r = bootstrap_county(row["county_fips"], row["county_name"], on_progress)
                refreshed.append(r)
            except Exception as e:
                print(f"[cache] refresh of {row['county_name']} failed: {e}", flush=True)
    return refreshed
