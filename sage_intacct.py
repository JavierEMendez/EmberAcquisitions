"""
Sage Intacct XML API client for EmberApps.

Direct integration — no Claude / MCP dependency at runtime. The Flask
app authenticates with company + user credentials from env vars, caches
the session ~45 min, and exposes functions to pull entities, reporting
periods, and trial balances. Designed to be defensive: missing creds
or transient Sage errors degrade to a clear empty state in the UI
rather than 500s.

Background: the accountant currently pulls trial balances by hand and
feeds them to an LLM to produce the close package. He hits Sage UI
session timeouts, but the XML API doesn't share that limit — once a
session is issued it lasts the full hour regardless of activity.
Combined with the Postgres cache layer in `intacct_tb_cache`, the
dashboard renders from the last successful pull even when Sage is
slow or down for a moment.

Environment variables required:
    INTACCT_SENDER_ID
    INTACCT_SENDER_PASSWORD
    INTACCT_USER_ID
    INTACCT_USER_PASSWORD
    INTACCT_COMPANY_ID

Optional:
    INTACCT_API_URL   default: https://api.intacct.com/ia/xml/xmlgw.phtml
    INTACCT_BOOK      default: "ACCRUAL"

Public API:
    is_configured()                 → bool
    list_entities()                 → list[{id, name}]
    list_periods(closed_only=True)  → list[{name, start, end, type}]
    get_trial_balance(entity_id, period_name) → list[account dict]
"""
from __future__ import annotations

import os
import time
import uuid
import logging
import threading
from datetime import datetime
from typing import Optional

import requests
from xml.etree import ElementTree as ET

log = logging.getLogger(__name__)


# ─── Exceptions ──────────────────────────────────────────────────────────────
class IntacctConfigurationError(RuntimeError):
    """Raised when the env vars needed for Sage auth aren't set.
    Routes catch this and render a clear "configure credentials" empty
    state instead of letting it become a 500."""


class IntacctAPIError(RuntimeError):
    """Raised when Sage returns a non-success response, or when network
    calls fail after all retries."""


# ─── Constants ───────────────────────────────────────────────────────────────
API_URL_DEFAULT = "https://api.intacct.com/ia/xml/xmlgw.phtml"
BOOK_DEFAULT = "ACCRUAL"
SESSION_TTL_SECONDS = 45 * 60  # Sage gives ~1hr; refresh at 45min to be safe
DEFAULT_TIMEOUT = 60
DEFAULT_RETRIES = 2
DEFAULT_PAGE_SIZE = 1000

_CREDENTIAL_KEYS = (
    "INTACCT_SENDER_ID",
    "INTACCT_SENDER_PASSWORD",
    "INTACCT_USER_ID",
    "INTACCT_USER_PASSWORD",
    "INTACCT_COMPANY_ID",
)


# ─── Session cache (per-worker, in-memory) ───────────────────────────────────
# Each Gunicorn / uvicorn worker maintains its own session cache. That's
# fine — Sage allows multiple concurrent sessions per user and the cost
# of an extra login on cold-start is negligible.
_session_lock = threading.Lock()
_session_cache: dict = {"id": None, "endpoint": None, "expires_at": 0.0}


# ─── Helpers ─────────────────────────────────────────────────────────────────
def is_configured() -> bool:
    """True iff all required credentials are present. Used by routes to
    decide between calling the API and rendering an empty state."""
    return all(os.environ.get(k) for k in _CREDENTIAL_KEYS)


def _required(key: str) -> str:
    v = os.environ.get(key)
    if not v:
        raise IntacctConfigurationError(
            f"Missing Sage Intacct credential: {key}. "
            "Set it in Railway env vars (Settings → Variables)."
        )
    return v


def _api_url() -> str:
    return os.environ.get("INTACCT_API_URL") or API_URL_DEFAULT


def _book() -> str:
    return os.environ.get("INTACCT_BOOK") or BOOK_DEFAULT


def _xml_escape(s) -> str:
    """Minimal XML attribute/text escaping. Sage rejects malformed
    payloads outright, so we never want stray `&` or `<` in user
    input (period names, location ids) breaking the envelope."""
    if s is None:
        return ""
    return (
        str(s)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&apos;")
    )


def _new_control_id() -> str:
    return uuid.uuid4().hex[:16]


def _login_xml() -> str:
    return (
        "<login>"
        f"<userid>{_xml_escape(_required('INTACCT_USER_ID'))}</userid>"
        f"<companyid>{_xml_escape(_required('INTACCT_COMPANY_ID'))}</companyid>"
        f"<password>{_xml_escape(_required('INTACCT_USER_PASSWORD'))}</password>"
        "</login>"
    )


def _session_xml(session_id: str) -> str:
    return f"<sessionid>{_xml_escape(session_id)}</sessionid>"


def _build_envelope(content_xml: str, auth_xml: str) -> str:
    """Wrap function content in the canonical request envelope."""
    sender_id = _required("INTACCT_SENDER_ID")
    sender_pw = _required("INTACCT_SENDER_PASSWORD")
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        "<request>"
        "<control>"
        f"<senderid>{_xml_escape(sender_id)}</senderid>"
        f"<password>{_xml_escape(sender_pw)}</password>"
        f"<controlid>{_new_control_id()}</controlid>"
        "<uniqueid>false</uniqueid>"
        "<dtdversion>3.0</dtdversion>"
        "<includewhitespace>false</includewhitespace>"
        "</control>"
        "<operation>"
        f"<authentication>{auth_xml}</authentication>"
        f"<content>{content_xml}</content>"
        "</operation>"
        "</request>"
    )


def _post(xml_body: str, url: Optional[str] = None,
          timeout: int = DEFAULT_TIMEOUT, retries: int = DEFAULT_RETRIES) -> str:
    """POST one envelope, with retry-on-5xx and exponential backoff."""
    target = url or _api_url()
    last_err: Optional[Exception] = None
    for attempt in range(retries + 1):
        try:
            resp = requests.post(
                target,
                data=xml_body,
                timeout=timeout,
                headers={"Content-Type": "application/xml"},
            )
            if resp.status_code >= 500:
                last_err = IntacctAPIError(
                    f"HTTP {resp.status_code} from Sage: {resp.text[:200]}"
                )
                time.sleep(1.5 * (attempt + 1))
                continue
            if not resp.ok:
                raise IntacctAPIError(
                    f"HTTP {resp.status_code} from Sage: {resp.text[:500]}"
                )
            return resp.text
        except requests.RequestException as e:
            last_err = e
            log.warning("Sage POST attempt %d failed: %s", attempt + 1, e)
            time.sleep(1.5 * (attempt + 1))
    raise IntacctAPIError(
        f"Sage API failed after {retries + 1} attempts: {last_err}"
    )


def _extract_all_errors(root: ET.Element) -> list:
    """Walk the entire response tree for <error> nodes and return their
    metadata as 'errno | description | description2 | correction' strings.
    Sage puts error info in different XPaths depending on which validation
    failed (control / authentication / individual operation result), so we
    search broadly rather than guessing the path."""
    out = []
    for err in root.iter("error"):
        errno = err.findtext("errorno") or ""
        d1 = err.findtext("description") or ""
        d2 = err.findtext("description2") or ""
        cor = err.findtext("correction") or ""
        parts = [x.strip() for x in (errno, d1, d2, cor) if x and x.strip()]
        if parts:
            out.append(" | ".join(parts))
    return out


def _check_status(root: ET.Element) -> None:
    """Raise IntacctAPIError on any non-success status in the standard
    response envelope. Sage's success/failure is signalled at three levels
    (control, authentication, individual operation result), and a failure
    at any level swallows everything after it. We pull *all* <error>
    descriptions from anywhere in the tree, then surface them; if no
    description is found, we include a short raw-XML excerpt so we can
    debug what Sage actually returned."""
    def _excerpt():
        try:
            s = ET.tostring(root, encoding="unicode")
            return s[:600] + ("…" if len(s) > 600 else "")
        except Exception:
            return "<could not serialize response>"

    errs = _extract_all_errors(root)
    errs_str = "; ".join(errs) if errs else ""

    # Control level
    ctl_status = root.findtext("./control/status")
    if ctl_status and ctl_status != "success":
        msg = errs_str or f"control failed (no description). Raw: {_excerpt()}"
        raise IntacctAPIError(f"Sage control error: {msg}")

    # Authentication level
    auth_status = root.findtext("./operation/authentication/status")
    if auth_status and auth_status != "success":
        msg = errs_str or f"authentication failed (no description). Raw: {_excerpt()}"
        raise IntacctAPIError(f"Sage authentication error: {msg}")

    # Operation-result level — Sage sometimes returns status=failure on
    # the result envelope with the error description sitting INSIDE the
    # operation result rather than at the control/auth level. Catch
    # those too.
    for result in root.findall("./operation/result"):
        rstatus = result.findtext("status")
        if rstatus and rstatus != "success":
            sub = _extract_all_errors(result)
            msg = "; ".join(sub) if sub else (errs_str or f"operation failed. Raw: {_excerpt()}")
            raise IntacctAPIError(f"Sage operation error: {msg}")

    # Any operation errormessage that isn't nested under a result
    for err in root.findall(".//result/errormessage/error"):
        desc = err.findtext("description") or err.findtext("description2") or ""
        corr = err.findtext("correction") or ""
        if desc or corr:
            raise IntacctAPIError(
                f"Sage operation error: {desc}{(' — ' + corr) if corr else ''}"
            )


# ─── Session management ──────────────────────────────────────────────────────
def get_session(force_refresh: bool = False) -> tuple[str, str]:
    """Return (session_id, endpoint_url). Cached for SESSION_TTL_SECONDS.

    Sage's `getAPISession` exchanges full credentials for a session id
    that's valid ~1 hour. We refresh proactively at 45 min so we never
    serve a request with a session that expires mid-flight.
    """
    now = time.time()
    with _session_lock:
        if (
            not force_refresh
            and _session_cache["id"]
            and _session_cache["expires_at"] > now + 60
        ):
            return _session_cache["id"], _session_cache["endpoint"]

        content = (
            f'<function controlid="{_new_control_id()}">'
            "<getAPISession/>"
            "</function>"
        )
        body = _build_envelope(content, _login_xml())
        xml_text = _post(body)
        root = ET.fromstring(xml_text)
        _check_status(root)

        sid = root.findtext("./operation/result/data/api/sessionid")
        endpoint = root.findtext("./operation/result/data/api/endpoint")
        if not sid:
            raise IntacctAPIError("getAPISession returned no session id")

        _session_cache["id"] = sid
        _session_cache["endpoint"] = endpoint or _api_url()
        _session_cache["expires_at"] = now + SESSION_TTL_SECONDS
        log.info("Sage session acquired, endpoint=%s", endpoint)
        return sid, _session_cache["endpoint"]


def _call(content_xml: str, timeout: int = DEFAULT_TIMEOUT) -> ET.Element:
    """Execute a content payload against the cached session. If the
    response signals an expired session, retry once with a fresh login."""
    for attempt in range(2):
        sid, endpoint = get_session(force_refresh=(attempt > 0))
        body = _build_envelope(content_xml, _session_xml(sid))
        xml_text = _post(body, url=endpoint, timeout=timeout)
        root = ET.fromstring(xml_text)
        # Retry once if auth itself failed (session expired mid-cache window).
        auth_status = root.findtext("./operation/authentication/status")
        if attempt == 0 and auth_status and auth_status != "success":
            log.info("Sage session looks expired, refreshing once")
            continue
        _check_status(root)
        return root
    raise IntacctAPIError("Sage call failed after one session refresh retry")


# ─── inspect (object schema introspection) ───────────────────────────────────
def _inspect_fields(object_name: str) -> dict:
    """Use Sage's <inspect> to return the list of field IDs that exist
    on an object. Returns {'fields': [...], 'error': '...'} so the
    caller can see why inspect returned nothing (often a permission
    issue — inspect needs a higher role than read in some setups)."""
    content = (
        f'<function controlid="{_new_control_id()}">'
        f"<inspect>"
        f"<object>{_xml_escape(object_name)}</object>"
        f"</inspect>"
        f"</function>"
    )
    try:
        root = _call(content)
    except IntacctAPIError as e:
        return {"fields": [], "error": str(e)[:300]}
    # inspect's response shape varies a bit between Sage versions —
    # walk for any <Field>/<ID> or <Fields>/<Field> nodes.
    ids = []
    for fld in root.iter():
        if fld.tag in ("Field", "field"):
            for tag in ("ID", "id", "name", "NAME"):
                v = fld.findtext(tag)
                if v:
                    ids.append(v)
                    break
    return {"fields": sorted(set(ids)), "error": None}


def _probe_field_validity(object_name: str, field: str) -> dict:
    """Test whether a field exists on an object by trying it as a
    filter with a junk value. Sage's response distinguishes
    "field doesn't exist" (XL03000010) from other errors, so we can
    detect field validity without needing inspect privileges."""
    try:
        _query(
            object_name,
            ["RECORDNO"],
            filter_pairs=[(field, "__probe_value__")],
            page_size=1,
            max_pages=1,
        )
        return {"valid": True}
    except IntacctAPIError as e:
        msg = str(e)
        # XL03000010 with the field name in it = the field doesn't exist.
        if "XL03000010" in msg and field.lower() in msg.lower():
            return {"valid": False, "error": msg[:300]}
        # Any other error means the field is valid but something else
        # is wrong (bad value, missing required filter, etc.) — still
        # tells us the field exists.
        return {"valid": True, "note": f"other error: {msg[:200]}"}


# ─── Discover queryable GLENTRY fields (cached) ───────────────────────────
#
# Beyond the core 6 (ACCOUNTNO, AMOUNT, TR_TYPE, STATE, ENTRY_DATE,
# LOCATION) there are likely Sage fields that identify ADJUSTING /
# BEGINNING-BALANCE / RESTATEMENT journals — JOURNAL, BATCHNO,
# RECORDTYPE, etc. We need them to differentiate the "true history"
# entries from the year-end snapshot JEs that are double-counting
# cumulative balances (the Land 2x bug).
#
# Probed once per process and cached on the module — probes are cheap
# (1-row queries) but we don't want to repeat them on every refresh.
_GLENTRY_EXTRA_CANDIDATES = [
    # Journal source / type indicators
    "JOURNAL", "JOURNALSYMBOL", "JOURNAL_NO", "BATCHNO", "BATCH_NO",
    "BATCH_TITLE", "BATCHTITLE", "RECORDTYPE", "SOURCE", "SOURCETYPE",
    "ENTRYTYPE",
    # Document / reference
    "DOCUMENT", "DOCUMENTNO", "REFERENCENO", "REFERENCE",
    # Description / memo
    "DESCRIPTION", "ENTRY_DESCRIPTION", "ENTRYDESC", "MEMO",
    # Created / modified for forensic ordering
    "WHENCREATED", "WHENMODIFIED", "CREATEDDATETIME",
]
_glentry_extra_valid: list[str] = None  # cache; None = not yet probed


def _discover_glentry_extra_fields() -> list[str]:
    """Return the subset of _GLENTRY_EXTRA_CANDIDATES that this Sage
    instance actually exposes. Cached after first call."""
    global _glentry_extra_valid
    if _glentry_extra_valid is not None:
        return _glentry_extra_valid
    valid = []
    for fld in _GLENTRY_EXTRA_CANDIDATES:
        try:
            r = _probe_field_validity("GLENTRY", fld)
            if r.get("valid"):
                valid.append(fld)
        except Exception as e:
            log.warning("probe GLENTRY.%s raised: %s", fld, e)
    log.info("GLENTRY extra valid fields discovered: %s", valid)
    _glentry_extra_valid = valid
    return valid


# ─── Modern <query> operation (structured filter) ───────────────────────────
def _query(
    object_name: str,
    fields: list,
    filter_pairs: list = None,
    page_size: int = 1000,
    max_pages: int = 20,
) -> list[dict]:
    """Sage's modern <query> operation (Intacct 30.0+). Required for
    aggregate / analytic objects like TRIALBALANCE that aren't exposed
    via readByQuery or get_list. Uses structured XML filters instead
    of a query-string.

    filter_pairs: list of (field, value) tuples joined with AND. Each
                  becomes an <expression> with operator "=".

    Pagination via <queryMore><resultId>...</resultId></queryMore>
    when Sage returns numremaining > 0.
    """
    filter_pairs = filter_pairs or []
    fields_xml = "<select>" + "".join(
        f"<field>{_xml_escape(f)}</field>" for f in fields
    ) + "</select>"

    # Sage's <query> filter children are direct comparison ops
    # (<equalto>, <lessthan>, etc.) or logical combinators (<and>, <or>).
    # Each comparison op takes <field> + <value> as direct children —
    # NO <expression> wrapper, NO <operator> sub-element. The previous
    # <expression><field>/<operator>/<value> shape was wrong; Sage
    # told us exactly what it accepts in the error response.
    def _equalto(field, value):
        return (
            "<equalto>"
            f"<field>{_xml_escape(field)}</field>"
            f"<value>{_xml_escape(value)}</value>"
            "</equalto>"
        )

    if not filter_pairs:
        filter_xml = ""
    elif len(filter_pairs) == 1:
        f, v = filter_pairs[0]
        filter_xml = f"<filter>{_equalto(f, v)}</filter>"
    else:
        # Multiple conditions joined with <and>.
        inner = "".join(_equalto(f, v) for f, v in filter_pairs)
        filter_xml = f"<filter><and>{inner}</and></filter>"

    out: list[dict] = []
    result_id = None
    for _ in range(max_pages):
        if result_id is None:
            content = (
                f'<function controlid="{_new_control_id()}">'
                "<query>"
                f"<object>{_xml_escape(object_name)}</object>"
                f"{fields_xml}"
                f"{filter_xml}"
                f"<pagesize>{int(page_size)}</pagesize>"
                "</query>"
                "</function>"
            )
        else:
            content = (
                f'<function controlid="{_new_control_id()}">'
                f"<queryMore><resultId>{_xml_escape(result_id)}</resultId></queryMore>"
                "</function>"
            )
        root = _call(content)
        data = root.find("./operation/result/data")
        if data is None:
            break
        result_id = data.attrib.get("resultId") or None
        try:
            remaining = int(data.attrib.get("numremaining", "0") or "0")
        except ValueError:
            remaining = 0
        for row in list(data):
            d = {child.tag: (child.text or "") for child in row}
            out.append(d)
        if not result_id or remaining <= 0:
            break
    return out


# ─── Generic get_list (legacy operation) ─────────────────────────────────────
def _get_list(
    object_name: str,
    filter_pairs: list,
    fields: list,
    maxitems: int = 1000,
) -> list[dict]:
    """Sage's legacy `get_list` operation. Used for objects that
    aren't exposed via the newer `readByQuery` — `trialbalance` is the
    canonical case (readByQuery on uppercase TRIALBALANCE returns
    "Object definition not found").

    filter_pairs: list of (field, value) tuples joined with implicit AND.
    fields:       list of field names to request; empty = all.
    maxitems:     up to 1000 (Sage's cap for this operation).
    """
    filter_xml = ""
    if filter_pairs:
        clauses = "".join(
            f'<equalto field="{_xml_escape(f)}" value="{_xml_escape(v)}"/>'
            for f, v in filter_pairs
        )
        # Multiple equalto siblings inside <filter> are ANDed by Sage.
        filter_xml = f"<filter>{clauses}</filter>"
    fields_xml = ""
    if fields:
        fields_xml = "<fields>" + "".join(
            f"<field>{_xml_escape(f)}</field>" for f in fields
        ) + "</fields>"
    # showprivate is only valid for a subset of objects and Sage rejects
    # the whole envelope at control level if you pass it on others.
    # Safer to omit — we don't need privacy-flagged records.
    content = (
        f'<function controlid="{_new_control_id()}">'
        f'<get_list object="{_xml_escape(object_name)}" '
        f'maxitems="{int(maxitems)}">'
        f'{filter_xml}{fields_xml}'
        f'</get_list>'
        f'</function>'
    )
    root = _call(content)
    data = root.find("./operation/result/data")
    if data is None:
        return []
    out = []
    for row in list(data):
        d = {child.tag: (child.text or "") for child in row}
        out.append(d)
    return out


# ─── Generic readByQuery + pagination ────────────────────────────────────────
def _read_by_query(
    obj: str,
    query: str,
    fields: str,
    pagesize: int = DEFAULT_PAGE_SIZE,
    max_pages: int = 50,
) -> list[dict]:
    """Iterate Sage's readByQuery + readMore until exhausted.
    Returns a list of {field: text} dicts. Stops at `max_pages` as a
    runaway guard — at 1000 rows/page that's 50k records, plenty for
    any single TB pull."""
    out: list[dict] = []
    result_id: Optional[str] = None
    for page in range(max_pages):
        if result_id is None:
            content = (
                f'<function controlid="{_new_control_id()}">'
                "<readByQuery>"
                f"<object>{_xml_escape(obj)}</object>"
                f"<query>{_xml_escape(query) if query else ''}</query>"
                f"<fields>{_xml_escape(fields)}</fields>"
                f"<pagesize>{int(pagesize)}</pagesize>"
                "</readByQuery>"
                "</function>"
            )
        else:
            content = (
                f'<function controlid="{_new_control_id()}">'
                f"<readMore><resultId>{_xml_escape(result_id)}</resultId></readMore>"
                "</function>"
            )
        root = _call(content)
        data = root.find("./operation/result/data")
        if data is None:
            break
        result_id = data.attrib.get("resultId") or None
        try:
            remaining = int(data.attrib.get("numremaining", "0") or "0")
        except ValueError:
            remaining = 0

        for row in list(data):
            d = {child.tag: (child.text or "") for child in row}
            out.append(d)

        if not result_id or remaining <= 0:
            break
    return out


# ─── Public API ──────────────────────────────────────────────────────────────
def list_entities() -> list[dict]:
    """All active top-level entities (LOCATIONTYPE='E'). Sorted by
    display name. Each dict: {'id': LOCATIONID, 'name': ENTITYNAME}.

    Note: STATUS filter is applied in Python rather than as part of
    the Sage query. /api/financials/diagnose showed STATUS='active'
    returns 0 rows via readByQuery even though the field is populated
    with that exact value — a quirk of Sage's legacy query syntax for
    certain "status-like" fields. Filtering server-side on
    LOCATIONTYPE='E' (which DOES work) gets us a small enough set to
    filter the remaining ~5 inactive rows in Python.
    """
    rows = _read_by_query(
        "LOCATION",
        "LOCATIONTYPE = 'E'",
        "LOCATIONID,NAME,ENTITYNAME,STATUS,LOCATIONTYPE",
    )
    out = []
    for r in rows:
        if (r.get("STATUS") or "").strip().lower() != "active":
            continue
        out.append({
            "id":   r.get("LOCATIONID", ""),
            "name": (r.get("ENTITYNAME") or r.get("NAME") or r.get("LOCATIONID", "")).strip(),
        })
    out.sort(key=lambda x: x["name"].lower())
    return out


def list_periods(closed_only: bool = True, max_periods: int = 60) -> list[dict]:
    """Active reporting periods sorted oldest → newest. By default,
    only returns periods whose END_DATE is before today (closed).
    Filtered to the most recent `max_periods` to keep the dropdown
    manageable — ~5 years of monthlies fits in 60.

    Same STATUS-filter caveat as list_entities() — applied in Python."""
    rows = _read_by_query(
        "REPORTINGPERIOD",
        "",  # unfiltered; STATUS filter applied below
        "RECORDNO,NAME,START_DATE,END_DATE,STATUS,REPORTINGPERIODTYPE",
    )
    today = datetime.utcnow().date()
    out = []
    for r in rows:
        if (r.get("STATUS") or "").strip().lower() != "active":
            continue
        start_s = (r.get("START_DATE") or "").strip()
        end_s = (r.get("END_DATE") or "").strip()
        try:
            start = datetime.strptime(start_s, "%m/%d/%Y").date() if start_s else None
            end = datetime.strptime(end_s, "%m/%d/%Y").date() if end_s else None
        except (TypeError, ValueError):
            continue
        if closed_only and (end is None or end >= today):
            continue
        # Filter out very-long-range periods (e.g., "All Time" spanning 90 years)
        if start and end and (end - start).days > 400:
            continue
        out.append({
            "name":  r.get("NAME", ""),
            "start": start.isoformat() if start else None,
            "end":   end.isoformat() if end else None,
            "type":  r.get("REPORTINGPERIODTYPE", ""),
        })
    out.sort(key=lambda x: x["end"] or "")
    return out[-max_periods:]


def _resolve_period_info(period_name: str) -> dict:
    """Look up the full record for a reporting period given its name.
    Returns {'recordno': ..., 'start_date': MM/DD/YYYY, 'end_date': MM/DD/YYYY}
    or {} if not found."""
    if not period_name:
        return {}
    try:
        rows = _read_by_query(
            "REPORTINGPERIOD",
            "",
            "RECORDNO,NAME,START_DATE,END_DATE",
            pagesize=1000,
            max_pages=2,
        )
        for r in rows:
            if (r.get("NAME") or "").strip() == period_name.strip():
                return {
                    "recordno":   (r.get("RECORDNO") or "").strip(),
                    "start_date": (r.get("START_DATE") or "").strip(),
                    "end_date":   (r.get("END_DATE") or "").strip(),
                }
    except IntacctAPIError:
        pass
    return {}


def get_trial_balance(entity_id: str, period_name: str,
                      timeout: int = DEFAULT_TIMEOUT) -> list[dict]:
    """Pull the trial balance for one entity + named reporting period.

    Returns one dict per GL account that has activity in or a balance
    as of the period:
        {'no':    '10001',
         'name':  'Cash - Ember Group - 0642',
         'open':  0.0,
         'debit': 0.0,
         'credit': 0.0,
         'close': 0.0}

    Implementation note: after exhaustive probing this Sage instance
    won't return data from GLACCOUNTBALANCE (filtered queries silently
    fail despite the user having admin rights and the object existing
    in the schema). The workable path is aggregating GLENTRY directly —
    sum AMOUNT per account across all posted entries up through the
    period end date. Slower (~10–30s for a multi-year entity, paginated
    through thousands of rows) but reliable since GLENTRY is queryable
    on every Sage instance.

    Trade-off accepted because: (a) result is cached in
    intacct_tb_cache so we only pay the cost on Refresh, and (b) Phase 1
    only needs closing balances for the Balance Sheet, which is exactly
    what summing AMOUNT-to-date gives us.
    """
    # Look up the period's end date so we can cut off the aggregation.
    period_info = _resolve_period_info(period_name)
    period_end_str = (period_info.get("end_date") or "").strip()
    period_end: Optional[datetime] = None
    if period_end_str:
        try:
            period_end = datetime.strptime(period_end_str, "%m/%d/%Y")
        except ValueError:
            log.warning("Could not parse period end_date %r", period_end_str)

    # GLENTRY field-validity probes definitively settled the schema:
    #   LOCATIONID  → exists but "cannot be queried" (Sage's exact wording)
    #   LOCATION    → THE queryable entity-filter field on GLENTRY
    #   STATE       → enum: Draft / Submitted / Approved / Posted / etc.
    #   TR_TYPE     → enum: 1 (debit) / -1 (credit), used to sign AMOUNT
    #   ENTRY_DATE  → MM/DD/YYYY date field
    #   AMOUNT      → unsigned magnitude; sign comes from TR_TYPE
    #   ACCOUNTNO   → valid selectable
    #
    # Single-clause filter only — multi-clause readByQuery has been
    # unreliable on this instance (e.g., LOCATION.STATUS='active'
    # returning 0 rows). State + date filtering happens in Python after
    # the fetch.
    # Sage multi-entity: the chosen LOCATIONID is the top-level entity,
    # but actual GL transactions are usually posted to its sub-locations
    # (LOCATIONTYPE='C' rows whose PARENTID matches the entity). Filtering
    # GLENTRY by just the parent ID returns only inter-entity / eliminations.
    # Resolve the full set of locations under this entity and query
    # each in turn, then aggregate.
    sub_locations = _entity_location_set(entity_id)
    log.info("get_trial_balance: entity=%s expanded to %d location(s): %s",
             entity_id, len(sub_locations), sub_locations)

    # Discover which extra fields are queryable on GLENTRY (journal
    # source, batch, document, description) so we can spot the
    # year-end snapshot JEs that are causing the BS doubling.
    extra_fields = _discover_glentry_extra_fields()
    select_clause = ",".join(
        ["ACCOUNTNO", "AMOUNT", "TR_TYPE", "STATE", "ENTRY_DATE", "LOCATION"]
        + extra_fields
    )
    entries: list[dict] = []
    for loc in sub_locations:
        try:
            page = _read_by_query(
                "GLENTRY",
                f"LOCATION = '{loc}'",
                select_clause,
                pagesize=1000,
                max_pages=100,
            )
            log.info("get_trial_balance: LOCATION=%s → %d entries", loc, len(page))
            entries.extend(page)
        except IntacctAPIError as e:
            log.warning("get_trial_balance: GLENTRY query for LOCATION=%s failed: %s", loc, e)
    # Stash the field list so the diagnostic can show what we queried.
    get_trial_balance._last_extra_fields = extra_fields

    log.info("get_trial_balance: fetched %d total GL entries across %d locations",
             len(entries), len(sub_locations))

    # If we got 0 entries for this LOCATION value, sample some
    # unfiltered rows to learn what LOCATION values Sage actually
    # uses on GLENTRY — that tells us whether our entity_id format
    # is wrong (e.g., LOCATIONID vs LOCATION-RECORDNO).
    if not entries:
        try:
            sample = _read_by_query(
                "GLENTRY", "",
                "ACCOUNTNO,LOCATION",
                pagesize=20, max_pages=1,
            )
            distinct_locations = sorted({
                (s.get("LOCATION") or "").strip()
                for s in sample if s.get("LOCATION")
            })
            log.warning(
                "get_trial_balance: 0 entries matched LOCATION='%s'. "
                "Sample LOCATION values from unfiltered GLENTRY: %s",
                entity_id, distinct_locations,
            )
            # Stash on the function for the route to surface
            get_trial_balance._last_sample_locations = distinct_locations
        except Exception as e:
            log.warning("sample LOCATION fetch failed: %s", e)
            get_trial_balance._last_sample_locations = []

    def _f(s) -> float:
        if not s:
            return 0.0
        s = str(s).strip()
        neg = s.startswith("(") and s.endswith(")")
        s = s.strip("()").replace(",", "")
        try:
            return -float(s) if neg else float(s)
        except (TypeError, ValueError):
            return 0.0

    # Aggregate: signed_amount = AMOUNT * TR_TYPE per entry, then sum
    # by account. TR_TYPE = 1 (debit) or -1 (credit). Sum gives the
    # standard signed TB balance; frp_mapping flips sign on Liab/Equity
    # sections at display time.
    #
    # Filters re-enabled now that the multi-location fan-out is in:
    #   STATE       == 'Posted'  — formal closed TB excludes Draft /
    #                              Submitted / Approved entries that
    #                              show as huge inflations on AP etc.
    #   ENTRY_DATE  <= period_end — "as of period end" balance, not
    #                              "as of today".
    # If a fetched entry is missing TR_TYPE we DROP it (rather than
    # default to 1 = debit) — defaulting was inflating one-sided
    # imports. AMOUNT alone, unsigned, contributes nothing to a TB.
    # Commitment-side JE filter. Two distinct patterns this catches:
    #
    # 1. PLACEHOLDER AJEs (BATCHTITLE = "Placeholder Commitment") —
    #    accountant locks in an estimated balance pending real docs.
    #    Already-discovered example: a $35.57M 12/31/2025 entry that
    #    duplicated the entire Land balance.
    #
    # 2. COMMITMENT-TRACKING JEs (construction PO workflow):
    #    - "2-PO DEV: ..." opens a commitment: DR WIP / CR AP for a
    #       future obligation under a Purchase Order.
    #    - "3-Vendor Invoice ..." books the ACTUAL bill: DR WIP / CR AP.
    #    - "Move COMMITMENTS ..." / "Close PO ..." reverse the
    #       commitment side once the actual arrives.
    #    Sage's TB report counts only the actuals (#2 in the chain).
    #    Raw GLENTRY counts every step, so we double-book by the value
    #    of OUTSTANDING commitments — $78-80M of inflation each on
    #    Development WIP and Trade Payables on this entity.
    #
    # Patterns are matched case-insensitively against BATCHTITLE,
    # DESCRIPTION, and DOCUMENT. "contains" = substring; "startswith"
    # = prefix (used where we need to be more specific to avoid
    # catching real vendor-invoice batch names).
    EXCLUDE_BATCH_PATTERNS = [
        # (mode, pattern, reason)
        ("contains",   "placeholder", "placeholder AJE"),
        ("contains",   "commit",      "commitment batch"),
        ("contains",   "cj_po",       "commitment journal PO move"),
        ("startswith", "2-po",        "PO commitment open"),
        ("startswith", "close po",    "PO commitment closure"),
    ]
    def _excluded_batch_reason(e: dict) -> Optional[str]:
        for fld in ("BATCHTITLE", "DESCRIPTION", "DOCUMENT"):
            v = (e.get(fld) or "").strip().lower()
            if not v:
                continue
            for mode, pat, why in EXCLUDE_BATCH_PATTERNS:
                if mode == "contains" and pat in v:
                    return why
                if mode == "startswith" and v.startswith(pat):
                    return why
        return None
    placeholder_filtered: list[dict] = []  # name kept for backward-compat in diag
    exclude_reason_counts: dict[str, int] = {}
    sums: dict[str, float] = {}
    # per_loc_sums tracks each account's contribution by LOCATION so the
    # diagnostic can show "this $71M Land balance came $35M from loc A and
    # $35M from loc B" — i.e., reveal parent+child double-posting visibly.
    per_loc_sums: dict[str, dict[str, float]] = {}
    # per_year_sums: {account: {year: {"dr": x, "cr": y, "count": n}}}
    # — surfaces whether the same balance is being re-stated each fiscal
    # year (the strongest theory for the 2x Land doubling: opening BB
    # journal entries replaying cumulative balances).
    per_year_sums: dict[str, dict[str, dict[str, float]]] = {}
    # per_batchtitle_sums: {account: {batchtitle: {"dr": x, "cr": y, "count": n}}}
    # — surfaces which JE batches are driving each account's balance.
    # The Placeholder filter caught one pattern; this'll reveal the
    # next one (Dev+AP each over by ~$80M after Placeholder removal —
    # there's another paired-AJE convention in use).
    per_batchtitle_sums: dict[str, dict[str, dict[str, float]]] = {}
    # per_account_samples: {account: [{entry_date, amount, tr_type, signed, state}]}
    # capped at 8 per account so the payload stays bounded.
    per_account_samples: dict[str, list[dict]] = {}
    SAMPLE_CAP = 8
    state_counter: dict[str, int] = {}
    skipped_no_tr_type = 0
    posted_kept = posted_dropped = date_dropped = 0
    for e in entries:
        st = (e.get("STATE") or "").strip()
        if st:
            state_counter[st] = state_counter.get(st, 0) + 1
        if st and st != "Posted":
            posted_dropped += 1
            continue
        excl_reason = _excluded_batch_reason(e)
        if excl_reason:
            exclude_reason_counts[excl_reason] = (
                exclude_reason_counts.get(excl_reason, 0) + 1
            )
            # Track the first 25 filtered entries so the diagnostic can
            # show the user EXACTLY what was excluded and why.
            if len(placeholder_filtered) < 25:
                placeholder_filtered.append({
                    "accountno":  (e.get("ACCOUNTNO") or "").strip(),
                    "entry_date": (e.get("ENTRY_DATE") or "").strip(),
                    "amount":     _f(e.get("AMOUNT")),
                    "tr_type":    (e.get("TR_TYPE") or "").strip(),
                    "batchtitle": (e.get("BATCHTITLE") or "").strip(),
                    "batch_no":   (e.get("BATCH_NO") or "").strip(),
                    "description": (e.get("DESCRIPTION") or "").strip(),
                    "document":   (e.get("DOCUMENT") or "").strip(),
                    "reason":     excl_reason,
                })
            continue
        ed_str = (e.get("ENTRY_DATE") or "").strip()
        ed_obj = None
        if ed_str:
            try:
                ed_obj = datetime.strptime(ed_str, "%m/%d/%Y")
            except ValueError:
                pass
        if period_end and ed_obj and ed_obj > period_end:
            date_dropped += 1
            continue
        no = (e.get("ACCOUNTNO") or "").strip()
        if not no:
            continue
        amount = _f(e.get("AMOUNT"))
        tr_type_raw = (e.get("TR_TYPE") or "").strip()
        if not tr_type_raw:
            skipped_no_tr_type += 1
            continue
        try:
            tr_type = int(float(tr_type_raw))
        except (TypeError, ValueError):
            skipped_no_tr_type += 1
            continue
        signed = amount * tr_type
        sums[no] = sums.get(no, 0.0) + signed
        loc = (e.get("LOCATION") or "").strip() or "(unknown)"
        bucket = per_loc_sums.setdefault(no, {})
        bucket[loc] = bucket.get(loc, 0.0) + signed
        # Per-year DR/CR tally — using ENTRY_DATE's year, or "?" for
        # entries with no parseable date.
        yr = str(ed_obj.year) if ed_obj else "?"
        yrbuckets = per_year_sums.setdefault(no, {})
        ybuck = yrbuckets.setdefault(yr, {"dr": 0.0, "cr": 0.0, "count": 0})
        if tr_type > 0:
            ybuck["dr"] += amount
        else:
            ybuck["cr"] += amount
        ybuck["count"] += 1
        # Per-BATCHTITLE DR/CR tally so we can see "of this $83M AP
        # balance, $X came from batch 'Foo' and $Y from 'Bar'".
        bt = (e.get("BATCHTITLE") or "(none)").strip() or "(blank)"
        btbuckets = per_batchtitle_sums.setdefault(no, {})
        bbuck = btbuckets.setdefault(bt, {"dr": 0.0, "cr": 0.0, "count": 0})
        if tr_type > 0:
            bbuck["dr"] += amount
        else:
            bbuck["cr"] += amount
        bbuck["count"] += 1
        # Up to 8 sample raw entries per account — sorted later by date
        # client-side via inspection if needed; capture order of arrival.
        slist = per_account_samples.setdefault(no, [])
        if len(slist) < SAMPLE_CAP:
            sample = {
                "entry_date": ed_str,
                "amount":     amount,
                "tr_type":    tr_type,
                "signed":     signed,
                "state":      st,
                "location":   loc,
            }
            # Tack on whichever extra fields Sage exposes — JOURNAL,
            # BATCHNO, RECORDTYPE, DESCRIPTION etc. — so we can spot
            # year-end snapshot/restatement JEs in the sample dump.
            for fld in extra_fields:
                v = e.get(fld)
                if v is not None:
                    sample[fld] = v
            slist.append(sample)
        posted_kept += 1
    # Stash diagnostics so the refresh route can surface them.
    get_trial_balance._last_per_location_sums = per_loc_sums
    get_trial_balance._last_per_year_sums    = per_year_sums
    get_trial_balance._last_account_samples  = per_account_samples
    get_trial_balance._last_placeholder_filtered = placeholder_filtered
    get_trial_balance._last_per_batchtitle_sums  = per_batchtitle_sums
    get_trial_balance._last_exclude_reason_counts = exclude_reason_counts
    log.info(
        "get_trial_balance: excluded JE counts by reason: %s (first %d entries retained for diag)",
        exclude_reason_counts, len(placeholder_filtered),
    )
    log.info(
        "get_trial_balance: %d entries → kept=%d  post-filtered=%d  date-filtered=%d "
        "tr_type-missing=%d  → %d accounts; state counts: %s",
        len(entries), posted_kept, posted_dropped, date_dropped,
        skipped_no_tr_type, len(sums), state_counter,
    )

    # Join with the chart of accounts for titles (in-process cache).
    coa_titles = _coa_titles_cached()

    out = []
    for no, total in sums.items():
        out.append({
            "no":     no,
            "name":   coa_titles.get(no, ""),
            "open":   0.0,
            "debit":  0.0,
            "credit": 0.0,
            "close":  total,
        })
    return out


def _entity_location_set(entity_id: str) -> list[str]:
    """Return the entity's LOCATIONID plus every sub-location LOCATIONID
    that lists it as PARENTID. In Sage's multi-entity setup, GL postings
    typically land on sub-locations (LOCATIONTYPE='C') with the chosen
    entity as parent, so a TB query needs to fan out across all of them.

    Single-level fan-out (one parent → its direct children). If your
    Sage instance nests deeper (grandchildren), we'd recurse — but the
    Ember COA structure observed so far is flat one level under the
    entity, so a single pass is enough.
    """
    out = [entity_id]
    try:
        children = _read_by_query(
            "LOCATION",
            f"PARENTID = '{entity_id}'",
            "LOCATIONID,LOCATIONTYPE,STATUS,PARENTID",
            pagesize=1000,
            max_pages=2,
        )
        for c in children:
            cid = (c.get("LOCATIONID") or "").strip()
            status = (c.get("STATUS") or "").strip().lower()
            if cid and status == "active":
                out.append(cid)
    except IntacctAPIError as e:
        log.warning("_entity_location_set: child-location lookup failed for %s: %s",
                    entity_id, e)
    # De-dup while preserving order
    seen = set()
    deduped = []
    for x in out:
        if x and x not in seen:
            seen.add(x)
            deduped.append(x)
    return deduped


# Per-worker COA cache. Chart of accounts changes rarely, and a TB
# refresh shouldn't re-pull 1,300 accounts for the title join.
_coa_cache_lock = threading.Lock()
_coa_cache: dict = {"titles": None, "fetched_at": 0.0}
COA_CACHE_TTL_SECONDS = 30 * 60  # 30 min


def _coa_titles_cached() -> dict:
    """Return {ACCOUNTNO: TITLE} for the active chart of accounts.
    Cached in-process for 30 min — way longer than any single user
    session. Cheap to refresh if needed."""
    now = time.time()
    with _coa_cache_lock:
        if _coa_cache["titles"] is not None and _coa_cache["fetched_at"] > now - COA_CACHE_TTL_SECONDS:
            return _coa_cache["titles"]

    rows = _read_by_query(
        "GLACCOUNT",
        "",  # status filter doesn't work via readByQuery query string;
             # we just take everything since titles are stable anyway.
        "ACCOUNTNO,TITLE,STATUS",
        pagesize=1000,
        max_pages=5,
    )
    titles = {(r.get("ACCOUNTNO") or "").strip(): (r.get("TITLE") or "").strip() for r in rows}
    with _coa_cache_lock:
        _coa_cache["titles"] = titles
        _coa_cache["fetched_at"] = now
    return titles


# ─── Debug helper for one-off connectivity tests ─────────────────────────────
def ping() -> dict:
    """Smoke test — verifies env vars are set and Sage accepts our
    login. Used by the admin diagnostic route at /api/financials/ping.

    On failure, includes Sage's raw response excerpt so the admin can
    diagnose which credential is wrong (Sage puts the descriptive error
    in different XML paths depending on the failure mode — control vs
    auth, sender vs user, missing vs invalid). Never raises."""
    if not is_configured():
        missing = [k for k in _CREDENTIAL_KEYS if not os.environ.get(k)]
        return {"ok": False, "message": f"Missing env vars: {', '.join(missing)}"}

    # Do the login inline so we can keep the raw response in scope and
    # surface it to the caller when something goes wrong.
    content = (
        f'<function controlid="{_new_control_id()}">'
        "<getAPISession/>"
        "</function>"
    )
    try:
        body = _build_envelope(content, _login_xml())
    except IntacctConfigurationError as e:
        return {"ok": False, "message": str(e)}
    try:
        xml_text = _post(body)
    except IntacctAPIError as e:
        return {"ok": False, "message": f"Network/HTTP error: {e}"}
    except Exception as e:
        return {"ok": False, "message": f"Unexpected network error: {e}"}

    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as e:
        return {
            "ok": False,
            "message": f"Sage returned non-XML: {e}",
            "raw_response_excerpt": xml_text[:1500],
        }

    ctl_status = root.findtext("./control/status")
    auth_status = root.findtext("./operation/authentication/status")

    # Walk the whole response for any <error> nodes — Sage puts them at
    # different levels (./errormessage, ./operation/errormessage,
    # ./operation/result/errormessage) so search broadly.
    errors = []
    for err in root.iter("error"):
        errno = err.findtext("errorno") or ""
        d1 = err.findtext("description") or ""
        d2 = err.findtext("description2") or ""
        cor = err.findtext("correction") or ""
        parts = [x.strip() for x in (errno, d1, d2, cor) if x and x.strip()]
        if parts:
            errors.append(" | ".join(parts))

    if ctl_status == "success" and auth_status == "success":
        sid = root.findtext("./operation/result/data/api/sessionid") or ""
        endpoint = root.findtext("./operation/result/data/api/endpoint") or _api_url()
        # Update the in-memory cache so the next real call doesn't re-login
        with _session_lock:
            _session_cache["id"] = sid
            _session_cache["endpoint"] = endpoint
            _session_cache["expires_at"] = time.time() + SESSION_TTL_SECONDS
        return {
            "ok":                True,
            "message":           f"Connected to {endpoint}",
            "sessionid_prefix":  (sid[:8] + "…") if sid else "",
            "control_status":    ctl_status,
            "auth_status":       auth_status,
        }

    return {
        "ok":                  False,
        "message":             "Sage login failed — see details below",
        "control_status":      ctl_status,
        "auth_status":         auth_status,
        "sage_errors":         errors or ["No <error> nodes found in response"],
        "raw_response_excerpt": xml_text[:1500],
    }


def diagnose() -> dict:
    """Permission-probe diagnostic: run a series of readByQuery calls
    against different objects + filter combos so we can pinpoint where
    a user is missing read access. Used by /api/financials/diagnose.

    Returns one record per probe with the query, count, sample rows,
    and any errors. Stops at 5 rows per probe to keep the response
    light — we're testing connectivity, not pulling data."""
    if not is_configured():
        missing = [k for k in _CREDENTIAL_KEYS if not os.environ.get(k)]
        return {"ok": False, "message": f"Missing env vars: {', '.join(missing)}"}

    probes = [
        # (label, object, query, fields)
        ("USERINFO — who is the API user?",
         "USERINFO", "", "USER_ID,LOGINID,FIRST_NAME,LAST_NAME,EMAIL1,STATUS,USERTYPE"),
        ("LOCATION — unfiltered (any visible locations?)",
         "LOCATION", "", "LOCATIONID,NAME,LOCATIONTYPE,STATUS,PARENTID"),
        ("LOCATION — STATUS = 'active' only",
         "LOCATION", "STATUS = 'active'", "LOCATIONID,NAME,LOCATIONTYPE,STATUS"),
        ("LOCATION — LOCATIONTYPE = 'E' only",
         "LOCATION", "LOCATIONTYPE = 'E'", "LOCATIONID,NAME,LOCATIONTYPE,STATUS"),
        ("LOCATION — both filters (what /api/financials/entities does)",
         "LOCATION", "STATUS = 'active' AND LOCATIONTYPE = 'E'",
         "LOCATIONID,NAME,LOCATIONTYPE,STATUS"),
        ("REPORTINGPERIOD — unfiltered",
         "REPORTINGPERIOD", "", "RECORDNO,NAME,START_DATE,END_DATE,STATUS"),
        ("REPORTINGPERIOD — STATUS = 'active'",
         "REPORTINGPERIOD", "STATUS = 'active'", "RECORDNO,NAME,START_DATE,END_DATE,STATUS"),
    ]

    out_probes = []
    for label, obj, query, fields in probes:
        try:
            rows = _read_by_query(obj, query, fields, pagesize=5, max_pages=1)
            out_probes.append({
                "label":   label,
                "object":  obj,
                "query":   query,
                "via":     "readByQuery",
                "ok":      True,
                "count":   len(rows),
                "sample":  rows[:3],
            })
        except IntacctAPIError as e:
            out_probes.append({
                "label":  label,
                "object": obj,
                "query":  query,
                "via":    "readByQuery",
                "ok":     False,
                "error":  str(e)[:300],
            })
        except Exception as e:
            out_probes.append({
                "label":  label,
                "object": obj,
                "query":  query,
                "via":    "readByQuery",
                "ok":     False,
                "error":  f"Unexpected: {e}",
            })

    # ── Probe candidate trial-balance / account-balance objects via
    # the modern <query> operation. None of TRIALBALANCE / get_list
    # 'trialbalance' / readByQuery TRIALBALANCE worked, so we need to
    # find which object actually exposes balance data in this Sage
    # instance. Tries several known/plausible names.
    tb_candidates = [
        ("TRIALBALANCE",     "modern query — TRIALBALANCE (last attempt)"),
        ("GLACCOUNTBALANCE", "modern query — GLACCOUNTBALANCE (likely)"),
        ("ARBALANCE",        "modern query — ARBALANCE (control case)"),
        ("APBALANCE",        "modern query — APBALANCE (control case)"),
        ("GLTRIALBALANCE",   "modern query — GLTRIALBALANCE (alt naming)"),
    ]
    for obj, label in tb_candidates:
        try:
            rows = _query(obj, ["RECORDNO"], filter_pairs=[], page_size=5, max_pages=1)
            out_probes.append({
                "label":   label,
                "object":  obj,
                "via":     "query",
                "ok":      True,
                "count":   len(rows),
                "sample":  rows[:2],
            })
        except IntacctAPIError as e:
            out_probes.append({
                "label":  label,
                "object": obj,
                "via":    "query",
                "ok":     False,
                "error":  str(e)[:300],
            })

    # ── Inspect probes for GLACCOUNTBALANCE + GLACCOUNT (in case the
    # user account has inspect privileges) ──────────────────────────
    inspect_targets = ["GLACCOUNTBALANCE", "GLACCOUNT"]
    schema_probes = []
    for obj in inspect_targets:
        result = _inspect_fields(obj)
        schema_probes.append({
            "label":  f"inspect — {obj} fields",
            "object": obj,
            "via":    "inspect",
            "ok":     bool(result.get("fields")),
            "count":  len(result.get("fields") or []),
            "fields": result.get("fields") or [],
            "error":  result.get("error"),
        })

    # ── Field-validity probes on GLACCOUNTBALANCE. Tries common period
    # / book / location field names as filters with a junk value.
    # Sage's XL03000010 ("Field requested is not valid") tells us
    # definitively which fields don't exist; other errors mean the
    # field DOES exist but our test value isn't a match. Either way
    # we learn the schema without needing inspect privileges.
    field_candidates_glab = [
        # Period
        "PERIODNAME", "REPORTINGPERIOD", "REPORTINGPERIODNAME",
        "PERIOD", "FISCALPERIOD", "REPORTINGYEAR", "FISCALYEAR",
        # Location
        "LOCATIONID", "LOCATION_ID", "LOCATION",
        # Account
        "ACCOUNTNO", "ACCOUNT_NO", "ACCT_NO", "ACCOUNT",
        # Book
        "BOOK", "BOOKID", "REPORTINGBOOK",
        # Balance fields (informational — we expect these to be
        # filterable but the "not valid" path also covers select-time)
        "BEGINBAL", "ENDBAL", "DEBIT", "CREDIT",
    ]
    field_probes = []
    for field in field_candidates_glab:
        result = _probe_field_validity("GLACCOUNTBALANCE", field)
        field_probes.append({
            "object": "GLACCOUNTBALANCE",
            "field":  field,
            **result,
        })

    # ── GLENTRY field probes — now that we're aggregating GLENTRY,
    # we need to know which date / state / location fields are valid
    # so Phase 2 can filter posted/dated entries server-side instead
    # of pulling everything. Probes use a generic _probe_field_validity
    # helper which sends `<equalto>` with a junk value; XL03000010 with
    # the field name in it means the field doesn't exist.
    glentry_field_candidates = [
        # Date candidates
        "DATE", "POSTED_DATE", "ENTRY_DATE", "TRX_DATE", "BATCH_DATE",
        # Posted/state candidates
        "STATE", "POSTING_STATE", "POSTED", "STATUS",
        # Location/entity candidates
        "LOCATIONID", "LOCATION", "ENTITY",
        # Transaction type candidates
        "TR_TYPE", "POSTING_TYPE", "DEBITCREDIT",
        # Debit/credit candidates (might be separate from AMOUNT)
        "DEBITAMOUNT", "CREDITAMOUNT", "DEBIT_AMOUNT", "CREDIT_AMOUNT",
        # Book candidate
        "BOOK", "BOOKID",
    ]
    glentry_field_probes = []
    for field in glentry_field_candidates:
        result = _probe_field_validity("GLENTRY", field)
        glentry_field_probes.append({
            "object": "GLENTRY",
            "field":  field,
            **result,
        })

    # ── Permission probes — read access to financial-data objects.
    # If LOCATION + REPORTINGPERIOD work but GLENTRY / GLACCOUNT /
    # GLACCOUNTBALANCE all return 0 or fail, this user's role only
    # has read on org metadata, not on financial data. That's a
    # Sage-side configuration fix, not a code fix.
    perm_probes = []
    for label, obj, query, fields in [
        ("GLENTRY — any financial activity readable?", "GLENTRY", "", "RECORDNO,ACCOUNTNO,AMOUNT"),
        ("GLACCOUNT — chart of accounts readable?",    "GLACCOUNT", "", "RECORDNO,ACCOUNTNO,TITLE"),
    ]:
        try:
            rows = _read_by_query(obj, query, fields, pagesize=5, max_pages=1)
            perm_probes.append({
                "label":  label,
                "object": obj,
                "via":    "readByQuery",
                "ok":     True,
                "count":  len(rows),
                "sample": rows[:2],
            })
        except IntacctAPIError as e:
            perm_probes.append({
                "label":  label,
                "object": obj,
                "via":    "readByQuery",
                "ok":     False,
                "error":  str(e)[:300],
            })

    return {
        "ok":                   True,
        "probes":               out_probes,
        "schema":               schema_probes,
        "field_probes":         field_probes,
        "glentry_field_probes": glentry_field_probes,
        "perm_probes":          perm_probes,
        "hint": (
            "READING ORDER:\n"
            "1. `perm_probes` first — if GLENTRY and GLACCOUNT both return\n"
            "   0 rows or error, the user's role lacks read access to\n"
            "   financial-data objects (org-metadata roles like Location\n"
            "   admin do NOT include this).\n"
            "2. `field_probes` shows which GLACCOUNTBALANCE field names exist.\n"
            "3. Other probes confirm the entity / period dropdowns work."
        ),
    }
