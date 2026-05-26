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


def _check_status(root: ET.Element) -> None:
    """Raise IntacctAPIError on any non-success status in the standard
    response envelope. Sage's success/failure is signalled at three
    levels (control, authentication, individual operation result), and
    a failure at any level swallows everything after it. We check all
    three and surface the first useful error description we find."""
    # Control level
    ctl_status = root.findtext("./control/status")
    if ctl_status and ctl_status != "success":
        msg = root.findtext("./errormessage/error/description") or "control failed"
        raise IntacctAPIError(f"Sage control error: {msg}")

    # Authentication level
    auth_status = root.findtext("./operation/authentication/status")
    if auth_status and auth_status != "success":
        msg = (
            root.findtext("./operation/errormessage/error/description")
            or root.findtext("./operation/errormessage/error/correction")
            or "authentication failed"
        )
        raise IntacctAPIError(f"Sage authentication error: {msg}")

    # Operation-result level — first error wins
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
    display name. Each dict: {'id': LOCATIONID, 'name': ENTITYNAME}."""
    rows = _read_by_query(
        "LOCATION",
        "STATUS = 'active' AND LOCATIONTYPE = 'E'",
        "LOCATIONID,NAME,ENTITYNAME,STATUS,LOCATIONTYPE",
    )
    out = []
    for r in rows:
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
    manageable — ~5 years of monthlies fits in 60."""
    rows = _read_by_query(
        "REPORTINGPERIOD",
        "STATUS = 'active'",
        "RECORDNO,NAME,START_DATE,END_DATE,STATUS,REPORTINGPERIODTYPE",
    )
    today = datetime.utcnow().date()
    out = []
    for r in rows:
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

    Sage's TRIALBALANCE object exposes opening + debit + credit + closing
    per account for the requested period. The caller decides MTD vs YTD
    by picking the right period name (e.g., "Month Ended March 2026" vs
    "Quarter Ended March 2026" or a YTD-style custom period).
    """
    book = _book()
    query = (
        f"REPORTINGPERIODNAME = '{period_name}' "
        f"AND LOCATIONID = '{entity_id}' "
        f"AND BOOKID = '{book}'"
    )
    rows = _read_by_query(
        "TRIALBALANCE",
        query,
        "ACCT_NO,ACCT_TITLE,OPENING_BALANCE,DEBIT,CREDIT,CLOSING_BALANCE,"
        "LOCATION,LOCATIONID,REPORTINGPERIODNAME,BOOKID",
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
    Never raises; returns {'ok': bool, 'message': str, 'sessionid_prefix': str}."""
    if not is_configured():
        missing = [k for k in _CREDENTIAL_KEYS if not os.environ.get(k)]
        return {"ok": False, "message": f"Missing env vars: {', '.join(missing)}"}
    try:
        sid, endpoint = get_session(force_refresh=True)
        return {
            "ok": True,
            "message": f"Connected to {endpoint}",
            "sessionid_prefix": sid[:8] + "…",
        }
    except (IntacctConfigurationError, IntacctAPIError) as e:
        return {"ok": False, "message": str(e)}
    except Exception as e:
        return {"ok": False, "message": f"Unexpected error: {e}"}
