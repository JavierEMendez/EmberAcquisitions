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

    Uses Sage's modern <query> operation (introduced in Intacct 30.0)
    against the TRIALBALANCE object. TRIALBALANCE is *not* available
    via readByQuery ("Object definition not found") or get_list
    ("'trialbalance' is not in the enumeration of allowed objects"),
    so query is the only path. The filter uses Sage's structured
    expression syntax — <equalto> in a get_list filter, which I tried
    first, isn't valid (Sage expects <expression><field>/<operator>/
    <value> nodes).

    The caller decides MTD vs YTD by picking the right period name
    (e.g., "Month Ended March 2026" vs "Calendar Year Ended December
    2026" vs a YTD-style custom period).
    """
    rows = _query(
        "TRIALBALANCE",
        fields=[
            "ACCT_NO", "ACCT_TITLE",
            "OPENING_BALANCE", "DEBIT", "CREDIT", "CLOSING_BALANCE",
            "LOCATION", "LOCATIONID",
            "REPORTINGPERIODNAME", "BOOKID",
        ],
        filter_pairs=[
            ("REPORTINGPERIODNAME", period_name),
            ("LOCATIONID",          entity_id),
            ("BOOKID",              _book()),
        ],
    )

    def _f(s: str) -> float:
        if not s:
            return 0.0
        s = s.strip()
        # Sage uses parens for negatives in some object responses
        neg = s.startswith("(") and s.endswith(")")
        s = s.strip("()").replace(",", "")
        try:
            return -float(s) if neg else float(s)
        except (TypeError, ValueError):
            return 0.0

    out = []
    for r in rows:
        out.append({
            "no":     (r.get("ACCT_NO") or "").strip(),
            "name":   (r.get("ACCT_TITLE") or "").strip(),
            "open":   _f(r.get("OPENING_BALANCE") or ""),
            "debit":  _f(r.get("DEBIT") or ""),
            "credit": _f(r.get("CREDIT") or ""),
            "close":  _f(r.get("CLOSING_BALANCE") or ""),
        })
    return out


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
                "ok":      True,
                "count":   len(rows),
                "sample":  rows[:3],
            })
        except IntacctAPIError as e:
            out_probes.append({
                "label":  label,
                "object": obj,
                "query":  query,
                "ok":     False,
                "error":  str(e),
            })
        except Exception as e:
            out_probes.append({
                "label":  label,
                "object": obj,
                "query":  query,
                "ok":     False,
                "error":  f"Unexpected: {e}",
            })

    return {
        "ok":     True,
        "probes": out_probes,
        "hint": (
            "Interpretation: if every LOCATION probe returns 0 but USERINFO works, "
            "the API user is missing Read permission on the Company object. "
            "If unfiltered LOCATION returns >0 but the filtered ones return 0, "
            "the filter is wrong for this Sage instance. If unfiltered returns "
            "exactly 1, the user is entity-scoped and can only see its own entity."
        ),
    }
