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
import re
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


# ─── readReport — invoke a saved Sage report via API ─────────────────────────
#
# This is the path Sage's own UI uses for the Trial Balance report. We
# call <readReport> with the report's name + parameters, Sage runs it
# server-side, and returns the rows. Bypasses the entire GLENTRY-
# aggregation problem because the report IS the canonical TB.
#
# Catch: <readReport> works only with REPORTS THAT ARE SAVED in Sage.
# Standard built-in reports might not be invokable by their UI name
# alone — they typically need to be re-saved as a custom report with
# a known path. So we try a list of candidate names and the first one
# that returns rows wins. If none work, the accountant needs to save
# a custom TB report in Sage UI named "API_TB" (or set
# INTACCT_TB_REPORT_NAME env var to match whatever they named it).
def _read_report(
    report_name: str,
    arguments: dict = None,
    page_size: int = 1000,
    max_pages: int = 60,
    wait_seconds: int = 0,
    poll_interval: float = 2.0,
    poll_timeout_seconds: int = 90,
    report_type: Optional[str] = None,
) -> dict:
    """Invoke <readReport> for `report_name`, poll until DONE, return all rows.

    Custom (memorized) Sage reports — including the API_TB Trial Balance
    we use here — are async. The first <readReport> returns a <report_results>
    element with a REPORTID and STATUS=PENDING. We then poll <readMore> using
    that REPORTID (camelCase <reportId>, NOT <resultId> — different element
    than readByQuery pagination) until the status flips to DONE and rows
    start arriving in <data>. The earlier implementation looked for <data>
    on the first call and bailed when it found a PENDING <report_results>
    block instead, which is why this returned 0 rows and the team fell back
    to GLENTRY-aggregation.

    type="interactive" is required for custom/memorized reports per the
    Sage developer docs. Omitting it targets "original" reports only.

    Returns {'rows': [...], 'columns': [...], 'error': str-or-None}.
    """
    args_xml = ""
    if arguments:
        for k, v in arguments.items():
            args_xml += f"<{_xml_escape(k)}>{_xml_escape(str(v))}</{_xml_escape(k)}>"
    arguments_block = f"<arguments>{args_xml}</arguments>" if args_xml else ""

    out_rows: list[dict] = []
    out_cols: list[str] = []
    report_id: Optional[str] = None
    status: str = "PENDING"

    # Step 1: submit. Returns either a PENDING report_results (custom reports)
    # or a data block with rows (some sync standard reports).
    # type="interactive" targets Custom Report Writer reports. Omit it to target
    # "original" reports including MEMORIZED standard reports (which is what
    # API_TB is — a memorized Trial Balance). We try the no-type variant first
    # since memorized standard reports are the common case for accounting use.
    type_attr = f' type="{_xml_escape(report_type)}"' if report_type else ""
    submit = (
        f'<function controlid="{_new_control_id()}">'
        f'<readReport{type_attr}>'
        f"<report>{_xml_escape(report_name)}</report>"
        f"<waitTime>{int(wait_seconds)}</waitTime>"
        f"<pagesize>{int(page_size)}</pagesize>"
        f"{arguments_block}"
        "</readReport>"
        "</function>"
    )
    try:
        root = _call(submit)
    except IntacctAPIError as e:
        return {"rows": out_rows, "columns": out_cols, "error": str(e)[:500]}

    # Try to extract a REPORTID + STATUS first (async path).
    # Responses contain multiple <STATUS> elements (control/auth/result are all
    # 'success'); the REPORT's status is the LAST one — nested deeper inside
    # <data>/<report_results> or <data>/<report>/<data>. Take the last hit.
    statuses_found = []
    for node in root.iter():
        tag = node.tag.upper()
        if tag == "REPORTID" and (node.text or "").strip():
            report_id = node.text.strip()
        elif tag == "STATUS" and (node.text or "").strip():
            statuses_found.append(node.text.strip().upper())
    # The REPORT's status is the last STATUS — skip the control/auth/result ones
    # that are all 'success'. If only success values exist, the report hasn't
    # reported a status yet; default to PENDING.
    report_statuses = [s for s in statuses_found if s not in ("SUCCESS",)]
    if report_statuses:
        status = report_statuses[-1]
    elif statuses_found:
        status = statuses_found[-1]

    # Harvest data ONLY if this is a sync response with real rows. For async
    # reports, the first response wraps a <report_results> meta block inside
    # <data> — that's NOT a TB row, it's the job handle. If STATUS is set
    # (PENDING/etc.), trust that signal and skip harvesting; the polling loop
    # below will fetch the real rows once the report finishes.
    data = root.find("./operation/result/data")
    if data is not None and not report_id:
        # Sync path — no REPORTID returned, so the data IS the rows
        for row_node in list(data):
            row_dict = {child.tag: (child.text or "") for child in row_node}
            if not out_cols:
                out_cols = list(row_dict.keys())
            out_rows.append(row_dict)
        try:
            remaining = int(data.attrib.get("numremaining", "0") or "0")
        except ValueError:
            remaining = 0
        report_id = data.attrib.get("resultId") or None  # legacy/sync fallback
        if remaining <= 0 and out_rows:
            return {"rows": out_rows, "columns": out_cols, "error": None}

    if not report_id:
        return {
            "rows": out_rows, "columns": out_cols,
            "error": f"readReport returned no REPORTID and no rows; raw: {ET.tostring(root).decode()[:500]}",
        }

    # Step 2: poll readMore until status is DONE or rows arrive.
    waited = 0.0
    while status in ("PENDING", "INPROGRESS", "INWAIT") and not out_rows:
        if waited >= poll_timeout_seconds:
            return {
                "rows": out_rows, "columns": out_cols,
                "error": f"readReport timed out after {poll_timeout_seconds}s (status={status}, REPORTID={report_id})",
            }
        time.sleep(poll_interval)
        waited += poll_interval
        poll_xml = (
            f'<function controlid="{_new_control_id()}">'
            f"<readMore><reportId>{_xml_escape(report_id)}</reportId></readMore>"
            "</function>"
        )
        try:
            root = _call(poll_xml)
        except IntacctAPIError as e:
            return {"rows": out_rows, "columns": out_cols, "error": str(e)[:500]}

        # Refresh status from response — take the LAST non-'SUCCESS' STATUS
        # (control/auth/result statuses are always 'success'; the report's
        # actual status is nested deeper and reported separately).
        statuses_found = [
            n.text.strip().upper() for n in root.iter()
            if n.tag.upper() == "STATUS" and (n.text or "").strip()
        ]
        report_statuses = [s for s in statuses_found if s != "SUCCESS"]
        if report_statuses:
            status = report_statuses[-1]
        elif statuses_found:
            status = statuses_found[-1]

        # Only harvest data once the report is DONE. While PENDING, the data
        # block contains a placeholder structure (data > report > data > STATUS)
        # that's not real rows. Harvesting it would break out of the wait loop
        # with junk and return early without the actual TB rows.
        if status not in ("PENDING", "INPROGRESS", "INWAIT"):
            data = root.find("./operation/result/data")
            if data is not None:
                for row_node in _iter_report_rows(data):
                    row_dict = {child.tag: (child.text or "") for child in row_node}
                    if not out_cols:
                        out_cols = list(row_dict.keys())
                    out_rows.append(row_dict)

        if status in ("FAILED", "ERROR", "CANCELLED"):
            return {"rows": out_rows, "columns": out_cols, "error": f"readReport status={status}"}

    # Step 3: paginate remaining pages via readMore until numremaining=0.
    for _ in range(max_pages):
        data = root.find("./operation/result/data")
        if data is None:
            break
        try:
            remaining = int(data.attrib.get("numremaining", "0") or "0")
        except ValueError:
            remaining = 0
        if remaining <= 0:
            break
        poll_xml = (
            f'<function controlid="{_new_control_id()}">'
            f"<readMore><reportId>{_xml_escape(report_id)}</reportId></readMore>"
            "</function>"
        )
        try:
            root = _call(poll_xml)
        except IntacctAPIError as e:
            return {"rows": out_rows, "columns": out_cols, "error": str(e)[:500]}
        data = root.find("./operation/result/data")
        if data is None:
            break
        for row_node in _iter_report_rows(data):
            row_dict = {child.tag: (child.text or "") for child in row_node}
            if not out_cols:
                out_cols = list(row_dict.keys())
            out_rows.append(row_dict)

    return {"rows": out_rows, "columns": out_cols, "error": None}


def _iter_report_rows(data_elem):
    """Yield row elements from a readReport/readMore <data> block.

    Sage nests the rows differently depending on which call returned them:
      readReport sync (rare):
        <data><row>…</row><row>…</row></data>
      readMore (DONE) — what API_TB actually returns:
        <data count=N><report>
          <data>…row…</data>   ← each row is itself a <data> element
          <data>…row…</data>
          …
          <STATUS>DONE</STATUS>  ← terminator, skip it
        </report></data>

    The row container is <report> (when present); rows are its <data>
    children. <STATUS> and any other non-<data> children are skipped.
    """
    if data_elem is None:
        return
    report_elem = data_elem.find("./report")
    container = report_elem if report_elem is not None else data_elem
    for child in list(container):
        # In the readMore-DONE case each row is itself tagged <data>; in the
        # sync case rows can be <row> or another tag. Skip STATUS/metadata.
        if child.tag.upper() in ("STATUS",):
            continue
        yield child


def list_saved_reports() -> dict:
    """Enumerate saved reports on this Sage instance so we can find an
    already-saved TB report (or confirm none exists, in which case the
    accountant needs to save one).

    Sage Intacct exposes reports through several possible object names
    depending on version/license. Probe each and return whichever ones
    return rows.
    """
    candidates = [
        # Most likely object names for "saved/custom reports". Sage UI
        # labels them "Memorized Reports" — that's the canonical object.
        "MEMORIZEDREPORT",
        "MEMORIZED_REPORT",
        "MEMORIZEDREPORTS",
        "REPORT",
        "SAVEDREPORT",
        "CUSTOMREPORT",
        "REPORTDEFINITION",
        "GLREPORTDEFINITION",
        "PLATFORM_REPORT",
        # Possibly the underlying platform object
        "MYI_REPORT",
        "PLATFORM_MYI_REPORT",
    ]
    results = []
    for obj in candidates:
        attempt = {"object": obj}
        try:
            rows = _read_by_query(obj, "", "RECORDNO,NAME,REPORTID,DESCRIPTION",
                                  pagesize=50, max_pages=1)
            attempt["ok"]    = True
            attempt["count"] = len(rows)
            attempt["sample"] = rows[:20]
        except IntacctAPIError as e:
            attempt["ok"]    = False
            attempt["error"] = str(e)[:400]
        results.append(attempt)
    return {"object_probes": results}


def pull_tb_via_report(
    entity_id: str,
    period_name: str,
    report_name_override: Optional[str] = None,
) -> dict:
    """Pull a Trial Balance directly from Sage's report engine.

    Returns:
        {
          'ok':          bool,
          'report_name': str-or-None,  — which report name succeeded
          'accounts':    list-of-dicts (num/name/opening/debit/credit/closing),
          'attempts':    list of {report_name, status, error/row_count},
          'message':     str (human summary).
        }

    Tries report_name_override (or INTACCT_TB_REPORT_NAME env var) first,
    then a list of fallbacks. The accountant should save a custom TB
    report in Sage UI named API_TB (or whatever matches the env var)
    with the appropriate reporting-period parameter so this becomes
    reliable. Until then we attempt common names.
    """
    candidate_names: list[str] = []
    primary = report_name_override or os.getenv("INTACCT_TB_REPORT_NAME") or ""
    primary = primary.strip()
    if primary:
        candidate_names.append(primary)
    # Sage memorized reports live under the owner's namespace. Even when
    # marked Public, API access may require an owner.name prefix.
    # GPD-March-2026 setup: accountant pbraud memorized "API_TB" as
    # Public. Try multiple addressing forms before giving up.
    owner_envs = (
        os.getenv("INTACCT_TB_REPORT_OWNER") or "pbraud"  # accountant username from screenshot
    ).strip()
    raw = ["API_TB", "API Trial Balance", "Trial Balance",
           "GL Trial Balance", "Standard Trial Balance"]
    for n in raw:
        candidate_names.append(n)
        if owner_envs:
            # Owner-prefixed variants Sage docs suggest for cross-user
            # access to memorized reports
            candidate_names.append(f"{owner_envs}.{n}")
            candidate_names.append(f"{owner_envs}/{n}")
    # de-dupe preserving order
    seen = set()
    candidate_names = [n for n in candidate_names if not (n in seen or seen.add(n))]

    attempts: list[dict] = []
    # API_TB rejects OWNER and LOCATIONID as invalid arguments — the report
    # is either pre-configured for an entity or returns all entities (caller
    # filters). Pass only the period; entity_id is used post-fetch to filter.
    args = {
        "REPORTINGPERIOD": period_name,
    }
    # Try each name with both report-type variants:
    #   no type attr  → memorized standard reports (the common case — what API_TB likely is)
    #   type="interactive" → Custom Report Writer reports
    type_variants = [None, "interactive"]
    for name in candidate_names:
        for rtype in type_variants:
            label = f"readReport(type={rtype or 'standard'})"
            log.info("pull_tb_via_report: trying %s(%r)", label, name)
            result = _read_report(name, arguments=args, report_type=rtype)
            attempt_info = {
                "report_name": name,
                "operation":   label,
                "row_count":   len(result["rows"]),
                "error":       result["error"],
            }
            attempts.append(attempt_info)
            if result["error"]:
                log.warning("%s(%r) error: %s", label, name, result["error"])
                continue
            if not result["rows"]:
                log.warning("%s(%r) returned 0 rows", label, name)
                continue

            accounts = _coerce_report_rows_to_accounts(result["rows"])
            if accounts:
                return {
                    "ok":          True,
                    "report_name": name,
                    "operation":   label,
                    "accounts":    accounts,
                    "attempts":    attempts,
                    "message":     f"Pulled {len(accounts)} accounts from {label}({name!r}).",
                }
            # rows came back but couldn't be coerced to TB account shape
            log.warning(
                "%s(%r) returned %d rows but none parseable as TB accounts; columns=%s",
                label, name, len(result["rows"]), result["columns"],
            )
            attempt_info["parse_failed"] = True
            attempt_info["columns"]      = result["columns"]

    return {
        "ok":          False,
        "report_name": None,
        "accounts":    [],
        "attempts":    attempts,
        "message": (
            "No saved Sage report returned a parseable Trial Balance. "
            "Have your Sage administrator save a custom TB report in Sage UI "
            "(name it 'API_TB' or set INTACCT_TB_REPORT_NAME) with the "
            "Owner/Reporting Period as a runtime parameter, then retry."
        ),
    }


def _coerce_report_rows_to_accounts(rows: list[dict]) -> list[dict]:
    """Map report rows (with unknown column naming) to the standard
    account dict shape used elsewhere: num/name/opening/debit/credit/closing."""
    if not rows:
        return []

    def _f(s):
        if not s:
            return 0.0
        s = str(s).strip()
        neg = s.startswith("(") and s.endswith(")")
        s = s.strip("()").replace(",", "").replace("$", "")
        try:
            return -float(s) if neg else float(s)
        except (TypeError, ValueError):
            return 0.0

    # Build lookup of column name -> canonical key. We compare lowercase
    # without spaces/underscores to be lenient about Sage's naming
    # variations across report definitions.
    def _norm(s):
        return re.sub(r"[\s_]+", "", s.strip().lower())

    synonyms = {
        "num":     {"accountno", "accountnumber", "accountnum", "accno", "account",
                    "glaccountno", "glaccountnumber"},
        "name":    {"accountname", "accounttitle", "name", "title", "description"},
        "opening": {"openingbalance", "beginningbalance", "beginbalance",
                    "openingbal", "openbal", "beginbal"},
        # Sage uses TOTDEBIT/TOTCREDIT in readReport output; aliases cover
        # readByQuery (TOTALDEBIT) and the trial-balance variants.
        "debit":   {"debit", "debits", "totaldebit", "totaldebits", "totdebit",
                    "currentdebit"},
        "credit":  {"credit", "credits", "totalcredit", "totalcredits", "totcredit",
                    "currentcredit"},
        "closing": {"closingbalance", "endingbalance", "endbalance", "closingbal",
                    "closebal", "endbal", "currentbalance", "balance"},
    }

    first_row = rows[0]
    col_map: dict[str, str] = {}
    for col in first_row.keys():
        n = _norm(col)
        for canonical, alts in synonyms.items():
            if n in alts:
                col_map[canonical] = col
                break

    # Need at minimum num + closing to be useful.
    if "num" not in col_map or "closing" not in col_map:
        return []

    accounts = []
    for r in rows:
        num = (r.get(col_map.get("num", "")) or "").strip()
        if not num or not num[0].isdigit():
            continue
        accounts.append({
            "num":     num,
            "name":    (r.get(col_map.get("name", "")) or "").strip() if "name" in col_map else "",
            "opening": _f(r.get(col_map.get("opening", ""))) if "opening" in col_map else 0.0,
            "debit":   _f(r.get(col_map.get("debit", "")))   if "debit"   in col_map else 0.0,
            "credit":  _f(r.get(col_map.get("credit", ""))) if "credit"   in col_map else 0.0,
            "closing": _f(r.get(col_map.get("closing", ""))),
        })
    return accounts


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
    # Commitment-side JE filter. Sage Intacct's construction-PO
    # workflow posts in (up to) four steps that all hit WIP/AP:
    #   1. 2-PO open commitment:                  DR WIP / CR AP
    #   2. 3-Vendor Invoice ... Batch:            DR AP  / CR WIP  ← commit-reversal
    #   3. 3-Vendor Invoice ... Batch Summary:    DR WIP / CR AP   ← THE ACTUAL BILL
    #   4. Move COMMITMENTS to CJ_VI / CJ_PO:     DR AP  / CR WIP  ← cleanup
    # Sage's TB report counts only step 3 (the actual bill). Raw
    # GLENTRY counts all four; if we don't strip 1, 2, and 4 we
    # double-count by the value of every open commitment.
    #
    # Plus PLACEHOLDER AJEs ("Placeholder Commitment" — back-dated
    # year-end JEs the accountant uses to lock in estimated balances).
    #
    # Matching is case-insensitive against BATCHTITLE ONLY. Restricting
    # to BATCHTITLE avoids false-positives we'd get from substring
    # hits in DESCRIPTION/DOCUMENT on legitimate bills.
    #
    # Modes:
    #   "contains"    — substring match anywhere in BATCHTITLE
    #   "startswith"  — BATCHTITLE begins with pattern
    #   "vinv_clear"  — special: starts with "3-vendor invoice" AND
    #                   does NOT end with "summary entry" (catches
    #                   the commit-reversal side without touching the
    #                   actual bill entries).
    EXCLUDE_BATCH_PATTERNS = [
        # (mode, pattern, reason)
        ("contains",    "placeholder",  "placeholder AJE"),
        ("contains",    "commitment",   "commitment batch (open/move/true-up)"),
        ("startswith",  "2-po",         "PO commitment open"),
        # "X Batch" without "Summary Entry" suffix = commit-reversal
        # side. The "Summary Entry" sibling is the actual transaction.
        ("non_summary", "3-vendor invoice", "vendor-invoice commit-reversal (X Batch)"),
        # NOTE: previously also excluded "startswith close po",
        # "contains  to cj_po", and "non_summary change order".
        # The inverse calibration against GPD March 2026 FRP found
        # that un-excluding those 3 reasons cuts loss in half
        # (1.89M → 1.10M) without disturbing any of the 7 anchor
        # accounts (Land, Bonds, Dev Loan, MUD Receivable, etc.).
        # Interpretation: PO closures, CJ_PO migrations, and Change
        # Orders are real construction events that the FRP includes.
        # Only true commitment OPENS (2-PO, placeholder, vendor-invoice
        # X Batch clearing, and "Move COMMITMENTS" batches) should
        # stay excluded.
        #
        # The remaining 1.10M of AP shortfall after this relaxation
        # appears to be a finer-grained pattern we can't catch
        # without accountant input on the specific batch convention.
        #
        # Also tried and reverted: "contains reclass" + "startswith
        # correcting" — those caught hundreds of legitimate routine
        # monthly reclassification batches and broke many other BS
        # lines that previously reconciled.
    ]
    def _excluded_batch_reason(e: dict) -> Optional[str]:
        bt = (e.get("BATCHTITLE") or "").strip().lower()
        if not bt:
            return None
        for mode, pat, why in EXCLUDE_BATCH_PATTERNS:
            if mode == "contains" and pat in bt:
                return why
            if mode == "startswith" and bt.startswith(pat):
                return why
            if mode == "non_summary":
                if bt.startswith(pat) and not bt.endswith("summary entry"):
                    return why
        return None
    placeholder_filtered: list[dict] = []  # name kept for backward-compat in diag
    exclude_reason_counts: dict[str, int] = {}
    # Track per-(reason, account) signed contribution of every entry we
    # EXCLUDE so the inverse-calibration can later test "what if we
    # un-excluded reason X, would AP match FRP?".
    exclude_reason_sums: dict[str, dict[str, float]] = {}
    sums: dict[str, float] = {}
    # per_loc_sums tracks each account's contribution by LOCATION so the
    # diagnostic can show "this $71M Land balance came $35M from loc A and
    # $35M from loc B" — i.e., reveal parent+child double-posting visibly.
    per_loc_sums: dict[str, dict[str, float]] = {}
    # per_when_created_sums: {YYYY-MM-DD: {account: signed_contribution}}
    # — supports the FRP-cutoff calibration. If we don't know what
    # WHENCREATED date the accountant generated the FRP on, we can
    # binary-search dates and find the one that produces matching
    # numbers.
    per_when_created_sums: dict[str, dict[str, float]] = {}
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
            # Capture signed contribution per (reason, account) so the
            # inverse calibration can test re-including individual
            # reasons. Need amount * tr_type computed up-front since we
            # 'continue' before the main aggregation runs them.
            ex_acct = (e.get("ACCOUNTNO") or "").strip()
            ex_amt  = _f(e.get("AMOUNT"))
            ex_tr_raw = (e.get("TR_TYPE") or "").strip()
            if ex_acct and ex_tr_raw:
                try:
                    ex_tr = int(float(ex_tr_raw))
                    ex_signed = ex_amt * ex_tr
                    rbucket = exclude_reason_sums.setdefault(excl_reason, {})
                    rbucket[ex_acct] = rbucket.get(ex_acct, 0.0) + ex_signed
                except (TypeError, ValueError):
                    pass
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
        # Bucket by WHENCREATED date (YYYY-MM-DD) for the calibration
        # search. Format in source: "MM/DD/YYYY HH:MM:SS" or "MM/DD/YYYY".
        wc_str = (e.get("WHENCREATED") or "").strip()
        if wc_str:
            try:
                wc_dt = datetime.strptime(wc_str.split()[0], "%m/%d/%Y")
                wc_day = wc_dt.strftime("%Y-%m-%d")
                daybucket = per_when_created_sums.setdefault(wc_day, {})
                daybucket[no] = daybucket.get(no, 0.0) + signed
            except (ValueError, IndexError):
                pass
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
    get_trial_balance._last_per_when_created_sums = per_when_created_sums
    get_trial_balance._last_exclude_reason_sums   = exclude_reason_sums
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


def calibrate_when_created_cutoff(
    accounts: list[dict],
    per_when_created_sums: dict,
    targets: dict[str, float],
) -> dict:
    """Reverse-engineer the FRP snapshot date.

    Sage's TB report excludes entries created after the FRP snapshot
    date. We don't know the date directly, but we can search for it:
    for each candidate WHENCREATED cutoff D, compute the close balance
    per account as if we'd filtered WHENCREATED > D. Find D that
    minimizes the total |actual - target| across the supplied target
    accounts.

    Args:
        accounts: result of get_trial_balance (current close balances).
        per_when_created_sums: get_trial_balance._last_per_when_created_sums,
            i.e., {YYYY-MM-DD: {account_no: signed_contribution}}.
        targets: {account_no: target_signed_close} for accounts whose
            FRP value we know.

    Returns:
        {
          "best_cutoff":  "YYYY-MM-DD" or None,
          "loss_at_best": float,
          "loss_no_cutoff": float,
          "balances_at_best": {account_no: signed_close},
          "trace": [{"cutoff": "...", "loss": ..., "balances": {...}}, ...]
            — every candidate date evaluated, for diagnostic.
        }
    """
    # Current balances (no cutoff applied) — pull from accounts list.
    current = {a["no"]: a["close"] for a in accounts}

    def _loss(balances: dict) -> float:
        return sum(abs(balances.get(a, 0.0) - t) for a, t in targets.items())

    # Restrict to dates after period_end (cutoff < period_end doesn't
    # make sense — the FRP is for "March 2026" so the snapshot is on or
    # after March 31). Days from per_when_created_sums.keys() are
    # WHEN entries were CREATED, can be any date in the system.
    dates_desc = sorted(per_when_created_sums.keys(), reverse=True)
    if not dates_desc:
        return {
            "best_cutoff":      None,
            "loss_at_best":     _loss(current),
            "loss_no_cutoff":   _loss(current),
            "balances_at_best": {a: current.get(a, 0.0) for a in targets},
            "trace":            [],
        }

    # Walk dates_desc, subtracting each day's contributions. At each
    # step balances reflect "include only entries WHENCREATED <= D".
    balances = {a: current.get(a, 0.0) for a in targets}
    loss_no_cutoff = _loss(balances)
    best_date    = None  # None means "no cutoff = current state"
    best_loss    = loss_no_cutoff
    best_balances = dict(balances)
    trace: list[dict] = [{
        "cutoff":   "(no cutoff)",
        "loss":     loss_no_cutoff,
        "balances": dict(balances),
    }]
    # dates_desc[0] is the latest day; cutoff = dates_desc[0] = "no cutoff".
    # To exclude entries created on dates_desc[0], we subtract them →
    # next cutoff = dates_desc[1].
    for i in range(len(dates_desc) - 1):
        day_to_exclude = dates_desc[i]
        new_cutoff     = dates_desc[i + 1]
        day_contribs   = per_when_created_sums[day_to_exclude]
        for acct in targets:
            balances[acct] -= day_contribs.get(acct, 0.0)
        loss = _loss(balances)
        trace.append({
            "cutoff":   new_cutoff,
            "loss":     loss,
            "balances": dict(balances),
        })
        if loss < best_loss:
            best_loss     = loss
            best_date     = new_cutoff
            best_balances = dict(balances)
    return {
        "best_cutoff":      best_date,
        "loss_at_best":     best_loss,
        "loss_no_cutoff":   loss_no_cutoff,
        "balances_at_best": best_balances,
        "trace":            trace,
    }


def inverse_calibrate_exclude_reasons(
    accounts: list[dict],
    exclude_reason_sums: dict[str, dict[str, float]],
    targets: dict[str, float],
) -> dict:
    """Find which subset of currently-excluded reasons to RE-INCLUDE
    such that the resulting balances best match FRP targets.

    The forward calibration (calibrate_when_created_cutoff) only
    excludes MORE — useful if our filter is too loose. This inverse
    finds a relaxation — useful when (as in GPD March 2026) our filter
    is too AGGRESSIVE: every WHENCREATED cutoff makes things worse,
    meaning the missing balance lives inside currently-excluded
    entries.

    Brute-forces 2^N combinations of exclude reasons (where N =
    number of distinct reasons). Each combo represents "un-exclude
    these reasons" — i.e., add their contributions back to the
    balances.
    """
    current = {a["no"]: a["close"] for a in accounts}
    reasons = list(exclude_reason_sums.keys())
    n = len(reasons)

    def _loss(balances: dict) -> float:
        return sum(abs(balances.get(a, 0.0) - t) for a, t in targets.items())

    base_loss = _loss(current)
    best_combo: tuple = ()
    best_loss = base_loss
    best_balances = dict(current)
    all_evaluations = []

    # 2^n combinations — n ≤ 7 in practice, so 128 max. Fine.
    for mask in range(1 << n):
        # Determine which reasons to un-exclude in this mask
        unexcl = [reasons[i] for i in range(n) if (mask >> i) & 1]
        balances = dict(current)
        for r in unexcl:
            for acct, contrib in exclude_reason_sums[r].items():
                balances[acct] = balances.get(acct, 0.0) + contrib
        loss = _loss(balances)
        all_evaluations.append({
            "unexcluded_reasons": unexcl,
            "loss":               loss,
            "balances":           {a: balances.get(a, 0.0) for a in targets},
        })
        if loss < best_loss:
            best_loss     = loss
            best_combo    = tuple(unexcl)
            best_balances = balances

    # Sort all evaluations by loss for the user to inspect.
    all_evaluations.sort(key=lambda x: x["loss"])

    return {
        "best_unexcluded":  list(best_combo),
        "loss_at_best":     best_loss,
        "loss_baseline":    base_loss,
        "balances_at_best": {a: best_balances.get(a, 0.0) for a in targets},
        "top_combos":       all_evaluations[:20],
    }


def get_trial_balance_with_cutoff(
    accounts: list[dict],
    per_when_created_sums: dict,
    cutoff_yyyymmdd: str,
) -> list[dict]:
    """Recompute the TB applying an additional WHENCREATED <= cutoff
    filter. Uses the per_when_created_sums data stashed by the latest
    get_trial_balance call (no Sage round-trip)."""
    # Reconstruct close balance from per-day buckets, keeping only
    # days <= cutoff.
    sums: dict[str, float] = {a["no"]: 0.0 for a in accounts}
    for day, day_contribs in per_when_created_sums.items():
        if day > cutoff_yyyymmdd:
            continue
        for acct, contrib in day_contribs.items():
            sums[acct] = sums.get(acct, 0.0) + contrib
    name_by_no = {a["no"]: a.get("name", "") for a in accounts}
    return [
        {
            "no":     no,
            "name":   name_by_no.get(no, ""),
            "open":   0.0,
            "debit":  0.0,
            "credit": 0.0,
            "close":  total,
        }
        for no, total in sums.items()
    ]


def location_to_entity_map() -> dict[str, str]:
    """Map every active LOCATIONID to the entity-level LOCATIONID it rolls up to.

    Sage's multi-entity structure has LOCATIONTYPE='E' rows (entities, what
    list_entities() returns) with LOCATIONTYPE='C' children whose PARENTID
    points back to the entity. TB exports key sections by the child code
    (e.g., "DEV_GPD LLC"), but the dashboard dropdown picks by entity id
    (e.g., "16 - GPD"). This map lets the upload route mirror each TB
    section under the entity_id that the dropdown will look up later.

    Returns a dict where each LOCATIONID maps to itself (if it's an entity)
    or to its parent's LOCATIONID (if it's a child). Inactive locations
    are skipped. On API errors returns whatever's been built so far —
    callers should treat a missing key as "no mapping known."
    """
    try:
        rows = _read_by_query(
            "LOCATION",
            "",  # STATUS filter applied below to match list_entities behavior
            "LOCATIONID,LOCATIONTYPE,PARENTID,STATUS",
            pagesize=1000,
            max_pages=4,
        )
    except IntacctAPIError as e:
        log.warning("location_to_entity_map: query failed: %s", e)
        return {}
    out: dict[str, str] = {}
    for r in rows:
        if (r.get("STATUS") or "").strip().lower() != "active":
            continue
        lid = (r.get("LOCATIONID") or "").strip()
        ltype = (r.get("LOCATIONTYPE") or "").strip().upper()
        parent = (r.get("PARENTID") or "").strip()
        if not lid:
            continue
        if ltype == "E":
            out[lid] = lid
        elif parent:
            out[lid] = parent
        # else: child with no parent — skip; we have no mapping
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


# ─── BVA discovery (read-only) ───────────────────────────────────────────────
# Candidate GL dimensions that might carry a job-cost line like
# "Baethe Rd Pavement" (Project / Task / Cost code / Class). Probed against
# the live instance so we only query the ones that actually exist.
_COST_DIM_CANDIDATES = [
    "PROJECTID", "PROJECTDIMKEY", "PROJECT_NO", "PROJECTNAME",
    "TASKID", "TASKDIMKEY", "TASKNO", "TASKNAME",
    "COSTTYPEID", "COSTTYPE", "COSTCODE", "COSTCODEID",
    "CLASSID", "CLASSDIMKEY", "DEPARTMENTID", "ITEMID",
]


def bva_discovery(entity_id: str | None = None, sample_rows: int = 300) -> dict:
    """Read-only introspection to design the Budget-vs-Actuals template.

    Without an entity → just the entity list (so we can map GPD / EMtor /
    WRRD / WRG → LOCATIONID). With an entity → which cost dimensions exist on
    GLENTRY, distinct sample values for each (where 'Baethe Rd Pavement' type
    lines should surface), and which commitment/PO objects are reachable.
    Nothing is written; every probe is wrapped so partial results still return.
    """
    if not is_configured():
        raise IntacctConfigurationError("Sage Intacct is not configured on this server.")

    out: dict = {"entities": list_entities(), "entity": entity_id}
    if not entity_id:
        return out

    try:
        locs = _entity_location_set(entity_id)
    except Exception as e:
        locs = [entity_id]
        out["location_set_error"] = str(e)[:200]
    out["locations"] = locs
    probe_loc = locs[0] if locs else entity_id

    # 1) Which candidate dims are actually QUERYABLE in a select. Sage rejects
    #    some (e.g. DEPARTMENTID) with XL03000006, and one bad field zeroes the
    #    whole query — so test each individually and keep only those that work.
    queryable: list[str] = []
    for f in _COST_DIM_CANDIDATES:
        try:
            _query("GLENTRY", [f], [("LOCATION", probe_loc)], page_size=1, max_pages=1)
            queryable.append(f)
        except Exception:
            continue
    out["glentry_cost_fields"] = queryable

    # 2) Sample GL entries across the entity's locations; collect distinct
    #    values per queryable dimension so we can see where cost lines live.
    select = ["ACCOUNTNO", "AMOUNT", "LOCATION"] + queryable
    rows: list[dict] = []
    for loc in locs[:8]:
        try:
            rows += _query("GLENTRY", select, [("LOCATION", loc)],
                           page_size=sample_rows, max_pages=1)
        except Exception as e:
            out.setdefault("query_errors", []).append(f"{loc}: {str(e)[:160]}")
    out["sample_count"] = len(rows)

    distinct: dict = {}
    for f in (queryable + ["ACCOUNTNO"]):
        vals = sorted({(r.get(f) or "").strip() for r in rows if (r.get(f) or "").strip()})
        distinct[f] = vals[:100]
    out["distinct_values"] = distinct

    # 3) Commitments — query (not inspect) a few PO rows to see if Purchasing
    #    carries open commitments and which dimensions they hold.
    commit: dict = {}
    probes = [
        ("PODOCUMENT",      ["DOCID", "DOCNO", "STATE", "TOTAL", "TOTALENTERED"]),
        ("PODOCUMENTENTRY", ["DOCID", "ITEMID", "ITEMNAME", "TOTAL", "PROJECTID", "TASKID", "LOCATIONID"]),
        ("PURCHASING",      ["RECORDNO", "STATE", "TOTAL"]),
    ]
    for obj, flds in probes:
        try:
            sample = _query(obj, flds, [], page_size=3, max_pages=1)
            commit[obj] = {"reachable": True, "rows_seen": len(sample), "sample": sample[:3]}
        except Exception as e:
            commit[obj] = {"reachable": False, "error": str(e)[:200]}
    out["commitment_objects"] = commit
    return out


# ─── BVA actuals + commitments (by PROJECT × COSTTYPE) ───────────────────────
def _glentry_signed(r: dict):
    """Signed GL amount: AMOUNT × TR_TYPE (debit +1 / credit -1). Returns None
    when TR_TYPE is missing (those entries are dropped, per get_trial_balance)."""
    tr = (r.get("TR_TYPE") or "").strip()
    if not tr:
        return None
    try:
        return float(r.get("AMOUNT") or 0) * float(tr)
    except (TypeError, ValueError):
        return None


def bva_actuals(entity_id: str) -> dict:
    """Actual cost to date by (PROJECTID, COSTTYPEID) for an entity's locations.
    Returns {(project, costtype): {project, project_name, task, costtype,
    actual}}. Only cost-coded entries (those carrying a project or cost type)
    are summed; cash/AP/debt lines fall out naturally."""
    if not is_configured():
        raise IntacctConfigurationError("Sage Intacct is not configured on this server.")
    locs = _entity_location_set(entity_id)
    select = ["PROJECTID", "PROJECTNAME", "TASKNAME", "COSTTYPEID", "AMOUNT", "TR_TYPE"]
    agg: dict = {}
    for loc in locs:
        try:
            rows = _query("GLENTRY", select, [("LOCATION", loc)],
                          page_size=1000, max_pages=100)
        except Exception as e:
            log.warning("bva_actuals: GLENTRY LOCATION=%s failed: %s", loc, e)
            continue
        for r in rows:
            proj = (r.get("PROJECTID") or "").strip()
            ct = (r.get("COSTTYPEID") or "").strip()
            if not (proj or ct):
                continue
            signed = _glentry_signed(r)
            if signed is None:
                continue
            key = (proj, ct)
            e = agg.setdefault(key, {"project": proj, "project_name": "",
                                     "task": "", "costtype": ct, "actual": 0.0})
            e["actual"] += signed
            if not e["project_name"] and (r.get("PROJECTNAME") or "").strip():
                e["project_name"] = r["PROJECTNAME"].strip()
            if not e["task"] and (r.get("TASKNAME") or "").strip():
                e["task"] = r["TASKNAME"].strip()
    return agg


_PO_LINE_CANDIDATES = [
    "RECORDNO", "DOCHDRNO", "ITEMID", "PROJECTID", "PROJECTNAME",
    "TASKID", "COSTTYPEID", "TOTAL", "AMOUNT", "STATE", "LOCATIONID",
]


def bva_commitments(entity_id: str) -> dict:
    """Total committed (PO) amount by (PROJECTID, COSTTYPEID) from PO lines —
    the FULL commitment incl. amounts already billed, so 'Committed − Actuals'
    reads as the open/unbilled remainder. Cancelled lines are dropped.
    Returns {(project, costtype): amount}; empty when PO lines aren't reachable
    or don't carry the cost dimensions."""
    if not is_configured():
        return {}
    qfields: list[str] = []
    for f in _PO_LINE_CANDIDATES:
        try:
            _query("PODOCUMENTENTRY", [f], [], page_size=1, max_pages=1)
            qfields.append(f)
        except Exception:
            continue
    if not ({"PROJECTID", "COSTTYPEID"} <= set(qfields)):
        return {}
    amount_field = "TOTAL" if "TOTAL" in qfields else ("AMOUNT" if "AMOUNT" in qfields else None)
    if not amount_field:
        return {}
    sel = [f for f in ["PROJECTID", "COSTTYPEID", amount_field, "STATE", "LOCATIONID"] if f in qfields]
    loc_set = set(_entity_location_set(entity_id))
    out: dict = {}
    try:
        rows = _query("PODOCUMENTENTRY", sel, [], page_size=1000, max_pages=50)
    except Exception as e:
        log.warning("bva_commitments: PODOCUMENTENTRY query failed: %s", e)
        return {}
    for r in rows:
        # If line carries a location, keep only this entity's locations.
        loc = (r.get("LOCATIONID") or "").strip()
        if loc and loc_set and loc not in loc_set:
            continue
        state = (r.get("STATE") or "").strip().lower()
        if state in ("cancelled", "canceled", "declined", "denied"):  # dead PO lines
            continue
        proj = (r.get("PROJECTID") or "").strip()
        ct = (r.get("COSTTYPEID") or "").strip()
        if not (proj or ct):
            continue
        try:
            amt = float(r.get(amount_field) or 0)
        except (TypeError, ValueError):
            amt = 0.0
        out[(proj, ct)] = out.get((proj, ct), 0.0) + amt
    return out


def _month_key(s) -> str:
    """GL ENTRY_DATE -> 'YYYY-MM'. Handles ISO (YYYY-MM-DD) and US (MM/DD/YYYY)."""
    s = (s or "").strip()
    if not s:
        return ""
    if len(s) >= 7 and s[4] == "-":
        return s[:7]
    if "/" in s:
        p = s.split("/")
        if len(p) == 3 and len(p[2][:4]) == 4:
            return "%s-%s" % (p[2][:4], p[0].zfill(2))
    return ""


def bva_actuals_monthly(entity_id: str) -> dict:
    """Actuals by (PROJECTID, TASKID) per month — for the 'actualize pro-forma'
    export. Returns {'rows': [{project, task, task_name, months:{'YYYY-MM': $}}],
    'months': [sorted YYYY-MM]}. Signed GL (AMOUNT × TR_TYPE)."""
    if not is_configured():
        raise IntacctConfigurationError("Sage Intacct is not configured on this server.")
    locs = _entity_location_set(entity_id)
    select = ["PROJECTID", "TASKID", "TASKNAME", "AMOUNT", "TR_TYPE", "ENTRY_DATE"]
    agg: dict = {}
    months: set = set()
    for loc in locs:
        try:
            rows = _query("GLENTRY", select, [("LOCATION", loc)],
                          page_size=1000, max_pages=100)
        except Exception as e:
            log.warning("bva_actuals_monthly: LOCATION=%s failed: %s", loc, e)
            continue
        for r in rows:
            proj = (r.get("PROJECTID") or "").strip()
            task = (r.get("TASKID") or "").strip()
            if not (proj or task):
                continue
            signed = _glentry_signed(r)
            if signed is None:
                continue
            mk = _month_key(r.get("ENTRY_DATE"))
            if not mk:
                continue
            key = (proj, task)
            e = agg.setdefault(key, {"project": proj, "task": task,
                                     "task_name": "", "months": {}})
            e["months"][mk] = e["months"].get(mk, 0.0) + signed
            if not e["task_name"] and (r.get("TASKNAME") or "").strip():
                e["task_name"] = r["TASKNAME"].strip()
            months.add(mk)
    return {"rows": sorted(agg.values(), key=lambda x: (x["project"], x["task"])),
            "months": sorted(months)}


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
