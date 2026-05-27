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


def _resolve_period_recordno(period_name: str) -> str:
    """Look up the RECORDNO for a reporting period given its name.
    Sage's GLACCOUNTBALANCE.PERIOD filter sometimes expects the period
    NAME, sometimes the RECORDNO — depends on instance configuration.
    Returns empty string if not found (caller can fall back to name)."""
    if not period_name:
        return ""
    try:
        rows = _read_by_query(
            "REPORTINGPERIOD",
            "",  # status filter doesn't work via query string
            "RECORDNO,NAME",
            pagesize=1000,
            max_pages=2,
        )
        for r in rows:
            if (r.get("NAME") or "").strip() == period_name.strip():
                return (r.get("RECORDNO") or "").strip()
    except IntacctAPIError:
        pass
    return ""


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

    The diagnose endpoint confirmed: TRIALBALANCE doesn't exist on
    this Sage instance via any operation, but GLACCOUNTBALANCE does
    (via the modern <query> operation). GLACCOUNTBALANCE has the
    balances we need (BEGINBAL, ENDBAL, DEBIT, CREDIT) but NOT the
    account title — that lives on GLACCOUNT (the chart of accounts).
    So we issue two queries and join in Python.

    The caller decides MTD vs YTD by picking the right period name
    (e.g., "Month Ended March 2026" vs "Calendar Year Ended December
    2026" vs a YTD-style custom period).
    """
    # 1) Account balances for the requested entity + period.
    # Field names confirmed via /api/financials/diagnose:
    #   PERIOD     ← period filter (NOT PERIODNAME / REPORTINGPERIOD)
    #   LOCATIONID ← entity filter
    #   ACCOUNTNO  ← account number (select)
    #   ENDBAL     ← closing balance (select)
    # BOOKID is a valid field but a silent <status>failure</status>
    # came back when we included it as a filter — likely the env-set
    # value ("ACCRUAL") doesn't match this instance's book id. Dropped
    # for Phase 1; if the user has multiple books and ACCRUAL ends up
    # being the wrong default, the filter can be put back once we
    # discover the right book identifier.
    #
    # PERIOD takes either the period NAME or its RECORDNO depending
    # on the instance — try RECORDNO first (more reliable across
    # configurations), fall back to NAME if lookup fails.
    period_value = _resolve_period_recordno(period_name) or period_name
    balances = _query(
        "GLACCOUNTBALANCE",
        fields=["ACCOUNTNO", "ENDBAL"],
        filter_pairs=[
            ("PERIOD",     period_value),
            ("LOCATIONID", entity_id),
        ],
    )

    # 2) Chart of accounts → ACCOUNTNO → TITLE map. Same data every
    # call so we could cache, but readByQuery on GLACCOUNT is fast
    # enough (~1300 records) that uncached is fine for MVP.
    coa_titles = _coa_titles_cached()

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

    out = []
    for r in balances:
        no = (r.get("ACCOUNTNO") or "").strip()
        if not no:
            continue
        out.append({
            "no":     no,
            "name":   coa_titles.get(no, ""),
            # open/debit/credit unavailable on this Sage's GLACCOUNTBALANCE
            # — Phase 1 BS doesn't use these, Phase 2 will discover them.
            "open":   0.0,
            "debit":  0.0,
            "credit": 0.0,
            "close":  _f(r.get("ENDBAL")),
        })
    return out


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

    return {
        "ok":            True,
        "probes":        out_probes,
        "schema":        schema_probes,
        "field_probes":  field_probes,
        "hint": (
            "Read `field_probes` to find which fields exist on GLACCOUNTBALANCE. "
            "Each probe sends the field with a junk value; valid:true means the "
            "field name is real (regardless of whether the value matched). The "
            "first valid period-style field (PERIODNAME / REPORTINGPERIOD / "
            "PERIOD / FISCALPERIOD) is what get_trial_balance should use."
        ),
    }
