"""FRP Excel builder — renders the BS / IS / SCF as the same .xlsx
workbook the accountant's frp_builder.py script generates.

Ported from frp_builder.py. Account classification predicates are
imported from tb_parser so the dashboard's on-screen render and
this Excel export use the same rules — change once, both stay
consistent.

DIFFERENCE FROM frp_builder.py: totals are pre-computed server-side
and written as concrete VALUES (not formulas). This guarantees the
workbook displays correctly in any viewer (Excel, Numbers,
LibreOffice, Google Drive preview, etc.) without depending on the
viewer to evaluate `=B10+B11+B12+B13`-style formulas. The FRP is a
snapshot report — live formulas aren't required.
"""
from __future__ import annotations
from io import BytesIO

from openpyxl import Workbook
from openpyxl.styles import Font, Alignment, Border, Side
from openpyxl.utils import get_column_letter

from tb_parser import (
    RELATED_PARTY_ACCTS,
    is_cash, is_restricted_bonds, is_receivables, is_prepaids,
    is_land, is_development, is_contra_sales, is_contra_mud,
    is_promissory, is_ppe,
    is_other_current_liab, is_other_op_liab,
    is_dev_loan, is_bond_lt, is_members_eq, is_income_stmt,
    is_re_invest,
    sum_accounts, get_prefix,
    _normalize_accounts,
)


# ─── Styling constants ───────────────────────────────────────────
NUM_FMT = '#,##0;(#,##0);"-"'
FONT_HEADER    = Font(name="Arial", size=11, bold=True)
FONT_DATA      = Font(name="Arial", size=10)
FONT_DATA_BOLD = Font(name="Arial", size=10, bold=True)
FONT_TITLE     = Font(name="Arial", size=12, bold=True)
THIN_BOTTOM    = Border(bottom=Side(style="thin"))
DOUBLE_BOTTOM  = Border(bottom=Side(style="double"))


# ─── Sheet/line helpers ──────────────────────────────────────────
def _setup_sheet(ws, title_lines, col_headers, col_widths):
    """Write title rows, column headers, set column widths.
    Returns the row number where the first data line should go."""
    row = 1
    for line in title_lines:
        c = ws.cell(row=row, column=1, value=line)
        c.font = FONT_TITLE
        ws.merge_cells(start_row=row, start_column=1,
                       end_row=row, end_column=len(col_widths))
        ws.cell(row=row, column=1).alignment = Alignment(horizontal="center")
        row += 1
    row += 1  # blank line
    if col_headers:
        for i, hdr in enumerate(col_headers):
            c = ws.cell(row=row, column=i + 1, value=hdr)
            c.font = FONT_HEADER
            c.alignment = Alignment(horizontal="center", wrap_text=True)
            c.border = THIN_BOTTOM
        row += 1
    for i, w in enumerate(col_widths):
        ws.column_dimensions[get_column_letter(i + 1)].width = w
    return row


def _write_line(ws, row, label, values, indent=0, bold=False,
                underline=None, number_format=NUM_FMT):
    """Write a labeled line with one or more numeric values. Returns
    the next row number. `values` can contain floats (rounded to int)
    or formula strings starting with '='."""
    prefix = "  " * indent
    c = ws.cell(row=row, column=1, value=prefix + label)
    c.font = FONT_DATA_BOLD if bold else FONT_DATA
    c.alignment = Alignment(horizontal="left")
    for i, val in enumerate(values):
        cell = ws.cell(row=row, column=2 + i)
        if isinstance(val, str) and val.startswith("="):
            cell.value = val
        elif isinstance(val, (int, float)):
            cell.value = round(float(val))
        else:
            cell.value = val
        cell.font = FONT_DATA_BOLD if bold else FONT_DATA
        cell.number_format = number_format
        cell.alignment = Alignment(horizontal="right")
        if underline == "thin":
            cell.border = THIN_BOTTOM
        elif underline == "double":
            cell.border = DOUBLE_BOTTOM
    return row + 1


# ─── Balance Sheet ───────────────────────────────────────────────
def build_balance_sheet(ws, monthly_accounts, full_name, date_label):
    """Render the BS into the given worksheet using pre-computed
    totals as VALUES (not formulas) so the workbook displays correctly
    in any viewer."""
    accounts = _normalize_accounts(monthly_accounts)
    title_lines = [full_name, "Balance Sheet", date_label]
    col_widths = [50, 18]
    row = _setup_sheet(ws, title_lines, ["", "Balance"], col_widths)
    row += 1

    def gs(predicate): return sum_accounts(accounts, predicate, "closing")

    # ASSETS — Current
    row = _write_line(ws, row, "Assets", [], bold=True); row += 1
    row = _write_line(ws, row, "Current Assets", [], indent=1, bold=True)

    cash         = gs(is_cash)
    restricted   = gs(is_restricted_bonds)
    receivables  = gs(is_receivables)
    prepaids     = gs(is_prepaids)
    if cash != 0:        row = _write_line(ws, row, "Cash and Cash Equivalents",         [cash],        indent=2)
    if restricted != 0:  row = _write_line(ws, row, "Restricted Funds - Bonds",          [restricted],  indent=2)
    if receivables != 0: row = _write_line(ws, row, "Receivables, Net",                  [receivables], indent=2)
    if prepaids != 0:    row = _write_line(ws, row, "Prepaids and Other Current Assets", [prepaids],    indent=2)
    total_ca = cash + restricted + receivables + prepaids
    row = _write_line(ws, row, "Total Current Assets", [total_ca],
                     indent=1, bold=True, underline="thin")
    row += 1

    # ASSETS — Non-Current / Real Estate, Net
    row = _write_line(ws, row, "Non-Current Assets", [], indent=1, bold=True)
    row = _write_line(ws, row, "Real Estate, Net", [], indent=2, bold=True)
    land         = gs(is_land)
    development  = gs(is_development)
    contra_sales = gs(is_contra_sales)
    contra_mud   = gs(is_contra_mud)
    if land != 0:         row = _write_line(ws, row, "Land",                                  [land],         indent=3)
    if development != 0:  row = _write_line(ws, row, "Development",                           [development],  indent=3)
    if contra_sales != 0: row = _write_line(ws, row, "Contra Real Estate - Sales",            [contra_sales], indent=3)
    if contra_mud != 0:   row = _write_line(ws, row, "Contra Real Estate - MUD Receivable",   [contra_mud],   indent=3)
    re_net = land + development + contra_sales + contra_mud
    row = _write_line(ws, row, "Real Estate, Net", [re_net],
                     indent=2, bold=True, underline="thin")

    promissory = gs(is_promissory)
    ppe        = gs(is_ppe)
    if promissory != 0: row = _write_line(ws, row, "Promissory Note - MUD Board fee",    [promissory], indent=2)
    if ppe != 0:        row = _write_line(ws, row, "Property, Plant and Equipment, Net", [ppe],        indent=2)
    total_nca = re_net + promissory + ppe
    row = _write_line(ws, row, "Total Non-Current Assets", [total_nca],
                     indent=1, bold=True, underline="thin")
    row += 1

    total_assets = total_ca + total_nca
    row = _write_line(ws, row, "Total Assets", [total_assets],
                     bold=True, underline="double")
    row += 1

    # LIABILITIES + EQUITY
    row = _write_line(ws, row, "Liabilities and Equity", [], bold=True); row += 1
    row = _write_line(ws, row, "Current Liabilities", [], indent=1, bold=True)
    row = _write_line(ws, row, "Accounts Payable, Net", [], indent=2, bold=True)

    trade_pay = -gs(lambda p: p == 20030)
    retention = -gs(lambda p: p == 20060)
    rp_pay    = -gs(lambda p: p in RELATED_PARTY_ACCTS)
    tax_pay   = -gs(lambda p: p == 20108)
    if trade_pay != 0: row = _write_line(ws, row, "Trade Payables",         [trade_pay], indent=3)
    if retention != 0: row = _write_line(ws, row, "Retention",              [retention], indent=3)
    if rp_pay != 0:    row = _write_line(ws, row, "Related Party Payables", [rp_pay],    indent=3)
    if tax_pay != 0:   row = _write_line(ws, row, "Taxes Payable",          [tax_pay],   indent=3)
    other_cl = sum(-a["closing"] for a in accounts
                   if is_other_current_liab(get_prefix(a["num"])))
    if other_cl != 0:  row = _write_line(ws, row, "Other Current Liabilities", [other_cl], indent=3)
    ap_net = trade_pay + retention + rp_pay + tax_pay + other_cl
    row = _write_line(ws, row, "Accounts Payable, Net", [ap_net],
                     indent=2, bold=True, underline="thin")

    earnest = -gs(lambda p: p == 21060)
    bond_st = -gs(lambda p: p == 21015)
    if earnest != 0: row = _write_line(ws, row, "Builder Earnest Money",     [earnest], indent=2)
    if bond_st != 0: row = _write_line(ws, row, "Bond Payable - Short Term", [bond_st], indent=2)
    total_cl = ap_net + earnest + bond_st
    row = _write_line(ws, row, "Total Current Liabilities", [total_cl],
                     indent=1, bold=True, underline="thin")
    row += 1

    row = _write_line(ws, row, "Non-Current Liabilities", [], indent=1, bold=True)
    dev_loan = -gs(is_dev_loan)
    bond_lt  = -gs(is_bond_lt)
    deferred = -gs(lambda p: p == 20020)
    if dev_loan != 0: row = _write_line(ws, row, "Development Loan Payable", [dev_loan], indent=2)
    if bond_lt != 0:  row = _write_line(ws, row, "Bond Payable - Long Term", [bond_lt],  indent=2)
    if deferred != 0: row = _write_line(ws, row, "Deferred Income",          [deferred], indent=2)
    total_ncl = dev_loan + bond_lt + deferred
    row = _write_line(ws, row, "Total Non-Current Liabilities", [total_ncl],
                     indent=1, bold=True, underline="thin")
    row += 1

    total_liab = total_cl + total_ncl
    row = _write_line(ws, row, "Total Liabilities", [total_liab],
                     indent=1, bold=True, underline="thin")
    row += 1

    # Equity
    row = _write_line(ws, row, "Equity", [], indent=1, bold=True)
    meq = -sum(a["closing"] for a in accounts
               if is_members_eq(get_prefix(a["num"])))
    re_30500 = gs(lambda p: p == 30500)
    re_30550 = gs(lambda p: p == 30550)
    is_sum   = gs(is_income_stmt)
    retained = -(re_30500 + re_30550 + is_sum)
    if meq != 0: row = _write_line(ws, row, "Members' Equity",   [meq],      indent=2)
    row = _write_line(ws, row, "Retained Earnings", [retained], indent=2)
    total_eq = meq + retained
    row = _write_line(ws, row, "Total Equity", [total_eq],
                     indent=1, bold=True, underline="thin")
    row += 1

    total_liab_eq = total_liab + total_eq
    row = _write_line(ws, row, "Total Liabilities and Equity", [total_liab_eq],
                     bold=True, underline="double")
    return {
        "total_assets":   total_assets,
        "total_liab_eq":  total_liab_eq,
        "footing":        total_assets - total_liab_eq,
    }


# ─── Income Statement ────────────────────────────────────────────
def build_income_statement(ws, monthly_accounts, ytd_accounts, full_name, date_label):
    """Render IS with pre-computed values."""
    monthly_accounts = _normalize_accounts(monthly_accounts)
    ytd_accounts     = _normalize_accounts(ytd_accounts)
    title_lines = [full_name, "Income Statement", date_label]
    col_widths = [50, 18, 18]
    col_headers = ["", f"Current Month\n{date_label}", f"Year To Date\n{date_label}"]
    row = _setup_sheet(ws, title_lines, col_headers, col_widths)
    row += 1

    def both(predicate, flip=False):
        m = y = 0.0
        for a in monthly_accounts:
            if predicate(get_prefix(a["num"])):
                chg = a["closing"] - a["opening"]
                m += -chg if flip else chg
        for a in ytd_accounts:
            if predicate(get_prefix(a["num"])):
                chg = a["closing"] - a["opening"]
                y += -chg if flip else chg
        return m, y

    # REVENUE — Operating / Real Estate
    row = _write_line(ws, row, "Revenue", [], bold=True)
    row = _write_line(ws, row, "Operating Revenue", [], indent=1, bold=True)
    row = _write_line(ws, row, "Real Estate Revenue", [], indent=2, bold=True)

    lot_m, lot_y     = both(lambda p: 40000 <= p <= 40999, flip=True)
    mkt_m, mkt_y     = both(lambda p: p == 41020,          flip=True)
    fence_m, fence_y = both(lambda p: p == 41001,          flip=True)
    if lot_m or lot_y:     row = _write_line(ws, row, "Lot Sales Revenue",    [lot_m, lot_y],     indent=3)
    if mkt_m or mkt_y:     row = _write_line(ws, row, "Marketing Fee Income", [mkt_m, mkt_y],     indent=3)
    if fence_m or fence_y: row = _write_line(ws, row, "Fence Credits",        [fence_m, fence_y], indent=3)
    re_rev_m = lot_m + mkt_m + fence_m
    re_rev_y = lot_y + mkt_y + fence_y
    row = _write_line(ws, row, "Total Real Estate Revenue", [re_rev_m, re_rev_y],
                     indent=2, bold=True, underline="thin")

    dev_m, dev_y = both(lambda p: 42000 <= p <= 46999, flip=True)
    if dev_m or dev_y:
        row = _write_line(ws, row, "Development/Management Income", [dev_m, dev_y], indent=2)
    op_rev_m = re_rev_m + dev_m
    op_rev_y = re_rev_y + dev_y
    row = _write_line(ws, row, "Total Operating Revenue", [op_rev_m, op_rev_y],
                     indent=1, bold=True, underline="thin")
    total_rev_m = op_rev_m
    total_rev_y = op_rev_y
    row = _write_line(ws, row, "Total Revenue", [total_rev_m, total_rev_y],
                     bold=True, underline="thin")
    row += 1

    # EXPENSES
    row = _write_line(ws, row, "Expenses", [], bold=True)
    row = _write_line(ws, row, "Operating Expenses", [], indent=1, bold=True)
    cos_m, cos_y       = both(lambda p: 50000 <= p <= 59999)
    mexp_m, mexp_y     = both(lambda p: 70001 <= p <= 70039)
    ga_m, ga_y         = both(lambda p: (60000 <= p <= 69999) or (70040 <= p <= 79999))
    if cos_m or cos_y:   row = _write_line(ws, row, "Cost of Sales - Real Estate", [cos_m, cos_y],   indent=2)
    if mexp_m or mexp_y: row = _write_line(ws, row, "Marketing and Advertising",   [mexp_m, mexp_y], indent=2)
    if ga_m or ga_y:     row = _write_line(ws, row, "General & Administrative",    [ga_m, ga_y],     indent=2)
    op_exp_m = cos_m + mexp_m + ga_m
    op_exp_y = cos_y + mexp_y + ga_y
    row = _write_line(ws, row, "Total Operating Expenses", [op_exp_m, op_exp_y],
                     indent=1, bold=True, underline="thin")
    total_exp_m = op_exp_m
    total_exp_y = op_exp_y
    row = _write_line(ws, row, "Total Expenses", [total_exp_m, total_exp_y],
                     bold=True, underline="thin")
    row += 1

    # Other Income (Expense), Net
    int_m, int_y = both(lambda p: p == 90031, flip=True)
    ft_m,  ft_y  = both(lambda p: p == 80015)
    other_m = int_m - ft_m   # franchise tax flipped to negative (it's an expense)
    other_y = int_y - ft_y
    has_other = (int_m or int_y) or (ft_m or ft_y)
    if has_other:
        row = _write_line(ws, row, "Other Income (Expense), Net", [], bold=True)
        if int_m or int_y:
            row = _write_line(ws, row, "Interest Income", [int_m, int_y], indent=1)
        if ft_m or ft_y:
            row = _write_line(ws, row, "Franchise Tax",   [-ft_m, -ft_y], indent=1)
        row = _write_line(ws, row, "Total Other Income (Expense), Net", [other_m, other_y],
                         bold=True, underline="thin")
        row += 1

    # Net Income
    ni_m = total_rev_m - total_exp_m + other_m
    ni_y = total_rev_y - total_exp_y + other_y
    row = _write_line(ws, row, "Net Income", [ni_m, ni_y],
                     bold=True, underline="double")
    return {"net_income_m": ni_m, "net_income_y": ni_y}


# ─── Statement of Cash Flows (indirect method) ───────────────────
def build_scf(ws, monthly_accounts, ytd_accounts, full_name, date_label):
    """Render SCF with pre-computed values."""
    monthly_accounts = _normalize_accounts(monthly_accounts)
    ytd_accounts     = _normalize_accounts(ytd_accounts)
    title_lines = [full_name, "Statement of Cash Flows", date_label]
    col_widths = [50, 18, 18]
    col_headers = ["", f"Current Month\n{date_label}", f"Year To Date\n{date_label}"]
    row = _setup_sheet(ws, title_lines, col_headers, col_widths)
    row += 1

    def chg_m(predicate):
        return sum(a["opening"] - a["closing"] for a in monthly_accounts
                   if predicate(get_prefix(a["num"])))
    def chg_y(predicate):
        return sum(a["opening"] - a["closing"] for a in ytd_accounts
                   if predicate(get_prefix(a["num"])))

    # Operating
    row = _write_line(ws, row, "Cash Flows from Operating Activities:", [], bold=True)
    ni_m, ni_y = chg_m(is_income_stmt), chg_y(is_income_stmt)
    row = _write_line(ws, row, "Net Income (Loss)", [ni_m, ni_y], indent=1)
    row += 1
    sum_m, sum_y = ni_m, ni_y

    wip_m, wip_y = chg_m(is_contra_sales), chg_y(is_contra_sales)
    mud_m, mud_y = chg_m(lambda p: p == 16902), chg_y(lambda p: p == 16902)
    has_noncash = abs(wip_m) > 0.005 or abs(wip_y) > 0.005
    has_mud     = abs(mud_m) > 0.005 or abs(mud_y) > 0.005
    if has_noncash:
        row = _write_line(ws, row, "  Adjustments for Non-Cash Items:", [], bold=True)
        row = _write_line(ws, row, "Cost of Sales - WIP Reclassification (non-cash)",
                         [wip_m, wip_y], indent=2)
        sum_m += wip_m; sum_y += wip_y
        if has_mud:
            row = _write_line(ws, row, "MUD Receivable - Bond Proceeds Adj (non-cash)",
                             [mud_m, mud_y], indent=2)
            sum_m += mud_m; sum_y += mud_y
        row += 1

    row = _write_line(ws, row, "Changes in Operating Assets and Liabilities:",
                     [], indent=1, bold=True)
    for label, pred in [
        ("Changes in Receivables",                       is_receivables),
        ("Changes in Prepaid Expenses and Other Assets", is_prepaids),
        ("Changes in Accounts Payable",                  lambda p: p == 20030),
        ("Changes in Retainage Payable",                 lambda p: p == 20060),
        ("Changes in Taxes Payable",                     lambda p: p == 20108),
        ("Changes in Related Party Payables",            lambda p: p in RELATED_PARTY_ACCTS),
        ("Changes in Earnest Money Deposits",            lambda p: p == 21060),
        ("Changes in Deferred Revenue",                  lambda p: p == 20020),
    ]:
        m, y = chg_m(pred), chg_y(pred)
        if abs(m) > 0.005 or abs(y) > 0.005:
            row = _write_line(ws, row, label, [m, y], indent=2)
            sum_m += m; sum_y += y
    oom = sum(a["opening"] - a["closing"] for a in monthly_accounts
              if is_other_op_liab(get_prefix(a["num"])))
    ooy = sum(a["opening"] - a["closing"] for a in ytd_accounts
              if is_other_op_liab(get_prefix(a["num"])))
    if abs(oom) > 0.005 or abs(ooy) > 0.005:
        row = _write_line(ws, row, "Changes in Other Operating Liabilities",
                         [oom, ooy], indent=2)
        sum_m += oom; sum_y += ooy
    net_op_m, net_op_y = sum_m, sum_y
    row = _write_line(ws, row, "Net Cash from Operating Activities",
                     [net_op_m, net_op_y], bold=True, underline="thin")
    row += 1

    # Investing
    row = _write_line(ws, row, "Cash Flows from Investing Activities:", [], bold=True)
    inv_m = inv_y = 0.0
    for label, pred in [
        ("Real Estate Development Expenditures",    is_re_invest),
        ("Purchases of Land",                       is_land),
        ("Changes in Restricted Cash (Bond Funds)", is_restricted_bonds),
        ("Purchases of PP&E",                       is_ppe),
        ("Changes in Promissory Note",              is_promissory),
    ]:
        m, y = chg_m(pred), chg_y(pred)
        if abs(m) > 0.005 or abs(y) > 0.005:
            row = _write_line(ws, row, label, [m, y], indent=1)
            inv_m += m; inv_y += y
    row = _write_line(ws, row, "Net Cash from Investing Activities",
                     [inv_m, inv_y], bold=True, underline="thin")
    row += 1

    # Financing
    row = _write_line(ws, row, "Cash Flows from Financing Activities:", [], bold=True)
    fin_m = fin_y = 0.0
    for label, pred in [
        ("Net Borrowings/(Repayments) - Development Loan", is_dev_loan),
        ("Changes in Bond Payable - Short Term",           lambda p: p == 21015),
        ("Changes in Bond Payable - Long Term, Net",       is_bond_lt),
        ("Member Contributions/Distributions",             is_members_eq),
    ]:
        m, y = chg_m(pred), chg_y(pred)
        if abs(m) > 0.005 or abs(y) > 0.005:
            row = _write_line(ws, row, label, [m, y], indent=1)
            fin_m += m; fin_y += y
    row = _write_line(ws, row, "Net Cash from Financing Activities",
                     [fin_m, fin_y], bold=True, underline="thin")
    row += 1

    # Summary
    net_change_m = net_op_m + inv_m + fin_m
    net_change_y = net_op_y + inv_y + fin_y
    row = _write_line(ws, row, "Net Change in Cash",
                     [net_change_m, net_change_y], bold=True, underline="thin")
    row += 1

    cash_open_m = sum_accounts(monthly_accounts, is_cash, "opening")
    cash_open_y = sum_accounts(ytd_accounts,     is_cash, "opening")
    row = _write_line(ws, row, "Cash, Beginning of Period",
                     [cash_open_m, cash_open_y])
    cash_close = sum_accounts(monthly_accounts, is_cash, "closing")
    row = _write_line(ws, row, "Cash, End of Period",
                     [cash_close, cash_close], underline="double")
    row += 1

    footing_m = cash_open_m + net_change_m - cash_close
    footing_y = cash_open_y + net_change_y - cash_close
    row = _write_line(ws, row, "Footing Check (should be 0)",
                     [footing_m, footing_y], bold=True)
    return {
        "net_change_m": net_change_m,
        "net_change_y": net_change_y,
        "footing_m":    footing_m,
        "footing_y":    footing_y,
    }


# ─── Public API ──────────────────────────────────────────────────
def build_workbook(full_name: str, monthly_accounts: list[dict],
                   ytd_accounts: list[dict], date_label: str) -> BytesIO:
    """Build a 3-sheet FRP workbook (BS / IS / SCF) and return it as
    a BytesIO ready to send to the user. Caller is responsible for
    seek(0) before sending if needed."""
    wb = Workbook()
    ws = wb.active
    ws.title = "Balance Sheet"
    build_balance_sheet(ws, monthly_accounts, full_name, date_label)

    if ytd_accounts:
        ws = wb.create_sheet("Income Statement")
        build_income_statement(ws, monthly_accounts, ytd_accounts,
                               full_name, date_label)
        ws = wb.create_sheet("Statement of Cash Flows")
        build_scf(ws, monthly_accounts, ytd_accounts, full_name, date_label)

    buf = BytesIO()
    wb.save(buf)
    buf.seek(0)
    return buf
