"""Persistence for the Acquisitions GIS tab.

The standalone app kept everything in one ``storage/searches.json`` rewritten
whole under a file lock. That does not belong in EmberApps, which already has
Postgres, so this module replaces it.

It deliberately stores each record as a JSONB blob rather than normalising ten
entity types into ten tables. Two reasons:

  * The route code ported from the standalone app manipulates plain dicts -
    ``proj["tracts"].append({...})``, ``t.get("netout_assumptions")``. Keeping
    the same shape means that logic ports across unchanged instead of being
    rewritten against columns, which is where a port of this size would
    otherwise spend its bugs.
  * These are documents, not relational data. A project is a name plus a list
    of tracts plus an analysis cache; nothing joins across them.

The parcel cache stays in SQLite - see acq_parcels. It is a rebuildable 1.3 GB
geometry cache, not application state, and it would dominate Postgres backups.

Ownership follows EmberApps: ``owner_id`` is ``users.id``. Admins see
everything, which matches how the rest of the portal behaves.
"""

from __future__ import annotations

import json
import uuid


# Every kind the standalone store held, minus two that EmberApps already owns:
# ``users`` (Postgres ``users``) and ``audit_log`` (``activity_log``).
KINDS = (
    "project",
    "search",
    "folder",
    "tract_pin",
    "note",
    "polygon",
    "favorite",
    "outreach",
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS acq_objects (
    kind        TEXT        NOT NULL,
    id          TEXT        NOT NULL,
    owner_id    INTEGER     REFERENCES users(id) ON DELETE CASCADE,
    data        JSONB       NOT NULL,
    created_at  TIMESTAMP   DEFAULT NOW(),
    updated_at  TIMESTAMP   DEFAULT NOW(),
    PRIMARY KEY (kind, id)
);
CREATE INDEX IF NOT EXISTS ix_acq_objects_kind_owner
    ON acq_objects (kind, owner_id);
-- Tract pins, notes and outreach are all looked up by the parcel they hang
-- off, across every owner, so index that path out of the blob.
CREATE INDEX IF NOT EXISTS ix_acq_objects_prop
    ON acq_objects ((data->>'prop_id'))
    WHERE data ? 'prop_id';
"""


def new_id(prefix: str = "") -> str:
    return f"{prefix}{uuid.uuid4().hex[:12]}"


def _rows_to_dicts(rows):
    """RealDictCursor hands JSONB back as a dict already; older psycopg2 and a
    plain-text column would hand back a string. Tolerate both rather than
    depending on which one is installed."""
    out = []
    for r in rows or []:
        d = r["data"]
        if isinstance(d, str):
            d = json.loads(d)
        d = dict(d)
        d.setdefault("id", r["id"])
        d["_owner_id"] = r["owner_id"]
        out.append(d)
    return out


def list_objects(conn, kind, owner_id=None, include_all=False):
    """Records of one kind. ``include_all`` is the admin view."""
    cur = conn.cursor()
    try:
        if include_all or owner_id is None:
            cur.execute(
                "SELECT id, owner_id, data FROM acq_objects WHERE kind = %s"
                " ORDER BY updated_at DESC", (kind,))
        else:
            cur.execute(
                "SELECT id, owner_id, data FROM acq_objects"
                " WHERE kind = %s AND owner_id = %s ORDER BY updated_at DESC",
                (kind, owner_id))
        return _rows_to_dicts(cur.fetchall())
    finally:
        cur.close()


def get_object(conn, kind, obj_id, owner_id=None, include_all=False):
    """One record, or None. Passing ``owner_id`` without ``include_all`` makes
    someone else's record read as missing rather than forbidden - the caller
    then 404s, which leaks less than a 403."""
    cur = conn.cursor()
    try:
        cur.execute(
            "SELECT id, owner_id, data FROM acq_objects WHERE kind = %s AND id = %s",
            (kind, obj_id))
        row = cur.fetchone()
    finally:
        cur.close()
    if not row:
        return None
    if not include_all and owner_id is not None and row["owner_id"] not in (None, owner_id):
        return None
    return _rows_to_dicts([row])[0]


def put_object(conn, kind, obj, owner_id):
    """Insert or replace. Returns the stored record with its id filled in."""
    obj = dict(obj)
    obj.pop("_owner_id", None)
    obj_id = str(obj.get("id") or new_id())
    obj["id"] = obj_id
    cur = conn.cursor()
    try:
        cur.execute(
            "INSERT INTO acq_objects (kind, id, owner_id, data)"
            " VALUES (%s, %s, %s, %s)"
            " ON CONFLICT (kind, id) DO UPDATE"
            "   SET data = EXCLUDED.data, updated_at = NOW()",
            (kind, obj_id, owner_id, json.dumps(obj, default=str)))
        conn.commit()
    finally:
        cur.close()
    obj["_owner_id"] = owner_id
    return obj


def delete_object(conn, kind, obj_id, owner_id=None, include_all=False):
    """Returns True if a row went away."""
    cur = conn.cursor()
    try:
        if include_all or owner_id is None:
            cur.execute("DELETE FROM acq_objects WHERE kind = %s AND id = %s",
                        (kind, obj_id))
        else:
            cur.execute(
                "DELETE FROM acq_objects WHERE kind = %s AND id = %s"
                "  AND (owner_id = %s OR owner_id IS NULL)",
                (kind, obj_id, owner_id))
        n = cur.rowcount
        conn.commit()
        return bool(n)
    finally:
        cur.close()


def find_by_prop(conn, kind, prop_ids, owner_id=None, include_all=False):
    """Records of one kind attached to any of these parcels, keyed by prop_id.

    Backs the map's note and outreach indicators, which ask about a whole
    screenful of parcels at once - one query rather than one per parcel.
    """
    ids = [str(p) for p in (prop_ids or []) if str(p).strip()]
    if not ids:
        return {}
    cur = conn.cursor()
    try:
        if include_all or owner_id is None:
            cur.execute(
                "SELECT id, owner_id, data FROM acq_objects"
                " WHERE kind = %s AND data->>'prop_id' = ANY(%s)", (kind, ids))
        else:
            cur.execute(
                "SELECT id, owner_id, data FROM acq_objects"
                " WHERE kind = %s AND data->>'prop_id' = ANY(%s)"
                "   AND (owner_id = %s OR owner_id IS NULL)",
                (kind, ids, owner_id))
        rows = _rows_to_dicts(cur.fetchall())
    finally:
        cur.close()
    out = {}
    for r in rows:
        out.setdefault(str(r.get("prop_id")), []).append(r)
    return out
