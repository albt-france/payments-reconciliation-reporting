"""
Build excel/Monthly_Close_Pack.xlsx : a working month-end close pack (October 2026, SIMULATED data).

Step 1 (openpyxl): data sheets as Excel Tables, live formulas (XLOOKUP, INDEX/MATCH, SUMIFS, COUNTIFS,
                   MAXIFS, EDATE), named input cells, data validation, conditional formatting.
Step 2 (Excel via COM, Windows only): real PivotTables + slicers, full recalculation, error scan.

    python src/build_excel.py
"""
from pathlib import Path

import duckdb
from openpyxl import Workbook
from openpyxl.formatting.rule import CellIsRule, FormulaRule
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.workbook.defined_name import DefinedName
from openpyxl.worksheet.datavalidation import DataValidation
from openpyxl.worksheet.table import Table, TableStyleInfo

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "excel" / "Monthly_Close_Pack.xlsx"
CLOSE_MONTH = "2026-10-01"

# ---- style (same palette as the Tableau dashboards) --------------------------------
FONT = "Arial"
BLUE, GREY, RED, INK = "2A78D6", "898781", "D03B3B", "0B0B0B"
PAGE = PatternFill("solid", fgColor="F4F4F2")
INPUT_FILL = PatternFill("solid", fgColor="FFF2B3")       # yellow = you may change this
HEAD_FILL = PatternFill("solid", fgColor="E1E0D9")
RED_FILL = PatternFill("solid", fgColor="F8D7D7")
GREEN_FILL = PatternFill("solid", fgColor="D6F0DF")
AMBER_FILL = PatternFill("solid", fgColor="FCE8C3")
thin = Side(style="thin", color="E1E0D9")
BOX = Border(left=thin, right=thin, top=thin, bottom=thin)
EUR = '#,##0.00 "€";-#,##0.00 "€";"-"'
EUR0 = '#,##0 "€";-#,##0 "€";"-"'
PCT = '0.0%;-0.0%;"-"'
PCT3 = '0.000%;-0.000%;"-"'
DT = "yyyy-mm-dd hh:mm:ss"
MONTH = "mmm yyyy"
FOOTER = "SIMULATED data · REAL daily prices (Binance, ECB) · Independent case study, not affiliated with Coinspaid"


def f(size=10, bold=False, color=INK, italic=False):
    return Font(name=FONT, size=size, bold=bold, color=color, italic=italic)


def header(ws, title, subtitle):
    ws.sheet_view.showGridLines = False
    ws["B2"] = title
    ws["B2"].font = f(16, True)
    ws["B3"] = subtitle
    ws["B3"].font = f(10, color=GREY)
    ws["L2"] = "SIMULATED"
    ws["L2"].font = f(9, True, "FFFFFF")
    ws["L2"].fill = PatternFill("solid", fgColor=RED)
    ws["L2"].alignment = Alignment(horizontal="center")
    ws.column_dimensions["A"].width = 2


def add_table(ws, name, df, start_row=1, start_col=1, formats=None, style="TableStyleLight1"):
    """Write a DataFrame as an Excel Table (structured references work on it)."""
    cols = list(df.columns)
    for j, c in enumerate(cols):
        cell = ws.cell(start_row, start_col + j, c)
        cell.font = f(10, True)
    for i, row in enumerate(df.itertuples(index=False), start=start_row + 1):
        for j, v in enumerate(row):
            ws.cell(i, start_col + j, None if v != v else v)  # NaN -> empty
    last_col = ws.cell(start_row, start_col + len(cols) - 1).column_letter
    first_col = ws.cell(start_row, start_col).column_letter
    ref = f"{first_col}{start_row}:{last_col}{start_row + len(df)}"
    t = Table(displayName=name, ref=ref)
    t.tableStyleInfo = TableStyleInfo(name=style, showRowStripes=True)
    ws.add_table(t)
    for j, c in enumerate(cols):
        col = ws.cell(start_row, start_col + j).column_letter
        ws.column_dimensions[col].width = max(12, min(28, len(c) + 4))
        if formats and c in formats:
            for i in range(start_row + 1, start_row + len(df) + 1):
                ws[f"{col}{i}"].number_format = formats[c]
    return ref


def name(wb, nm, ref, comment=None):
    wb.defined_names[nm] = DefinedName(nm, attr_text=ref, comment=comment)


# =====================================================================================
# 1. Data from the reconciliation database
# =====================================================================================
con = duckdb.connect(str(ROOT / "data" / "recon.duckdb"), read_only=True)
partner = con.execute(f"""
    SELECT p.stmt_id, p.ref, p.value_ts_local, p.currency, p.amount_crypto, p.amount_eur, p.fees_eur
    FROM partner_clean p
    WHERE date_trunc('month', p.value_ts_utc) = DATE '{CLOSE_MONTH}'
       OR date_trunc('month', p.value_date_local) = DATE '{CLOSE_MONTH}'
    ORDER BY p.value_ts_local""").df()
ledger = con.execute(f"""
    SELECT l.entry_id, l.ref, l.merchant_id, d.segment, l.entry_type, l.currency, l.amount,
           l.amount_eur, l.booked_ts AS booked_ts_utc
    FROM ledger_clean l LEFT JOIN dim_merchants d USING (merchant_id)
    WHERE l.entry_type IN ('conversion', 'partner_fee', 'fx_adjustment')
      AND (l.ref IN (SELECT ref FROM partner_clean
                     WHERE date_trunc('month', value_ts_utc) = DATE '{CLOSE_MONTH}'
                        OR date_trunc('month', value_date_local) = DATE '{CLOSE_MONTH}')
           OR (l.entry_type = 'conversion' AND date_trunc('month', l.booked_ts) = DATE '{CLOSE_MONTH}'))
    ORDER BY l.booked_ts, l.entry_id""").df()
# segment on fee / adjustment lines comes from their conversion
seg = ledger[ledger.entry_type == "conversion"].set_index("ref")["segment"]
ledger["segment"] = ledger["segment"].fillna(ledger["ref"].map(seg))
ledger["merchant_id"] = ledger["merchant_id"].fillna(
    ledger["ref"].map(ledger[ledger.entry_type == "conversion"].set_index("ref")["merchant_id"]))
kpi = con.execute("""
    SELECT month, volume_eur, transactions, active_merchants, stablecoin_share,
           fee_deposit_eur, fee_conversion_eur, fee_payout_eur, revenue_eur,
           network_cost_eur, partner_fee_eur, fx_result_eur, gross_margin_eur
    FROM kpis_monthly ORDER BY month""").df()
controls = con.execute("SELECT control, unit, source_a, source_b, difference, explained, residual, result FROM controls").df()
con.close()
for c in ["value_ts_local"]:
    partner[c] = partner[c].dt.to_pydatetime()
ledger["booked_ts_utc"] = ledger["booked_ts_utc"].dt.to_pydatetime()
kpi["month"] = kpi["month"].dt.to_pydatetime() if hasattr(kpi["month"], "dt") else kpi["month"]
recon_refs = partner[["ref"]].rename(columns={"ref": "Ref"})
print(f"partner lines {len(partner):,} | ledger lines {len(ledger):,}")

wb = Workbook()
wb.properties.creator = "albt"
wb.properties.title = "Monthly Close Pack - SIMULATED data"

# =====================================================================================
# README
# =====================================================================================
ws = wb.active
ws.title = "README"
header(ws, "Monthly Close Pack - October 2026", "Crypto payments: partner statement vs ledger, P&L and close checklist")
rows = [
    ("What this is", "A working month-end close file. Every number on Recon, P&L and Checklist is a live formula."),
    ("Data", "SIMULATED transactions generated in Python (seed 42); REAL daily crypto prices. Not Coinspaid data."),
    ("", ""),
    ("Sheet", "Purpose"),
    ("Inputs", "Close month, tolerance, partner time zone. Yellow cells are the only cells to edit."),
    ("P&L", "Revenue, costs, gross margin, take rate: close month vs previous month vs same month last year."),
    ("Recon", "Each partner statement line matched to the ledger (XLOOKUP, INDEX/MATCH, SUMIFS, MAXIFS)."),
    ("Pivot", "PivotTables with slicers: breaks by segment, currency and status."),
    ("Checklist", "Close steps, owner, status (drop-down) and an automatic check per step."),
    ("Data", "Partner statement extract (partner LOCAL time, Europe/Tallinn) + UTC conversion column."),
    ("Ledger", "Ledger extract: conversions, partner fees, FX adjustments (UTC)."),
    ("KPI_Data", "Monthly KPIs from the SQL pipeline (sql/05_kpis_monthly.sql)."),
    ("Controls", "Control totals from the SQL pipeline (sql/08_controls.sql)."),
    ("", ""),
    ("Legend", "Yellow fill + blue text = input you can change · black = formula · red fill = needs action"),
]
for i, (a, b) in enumerate(rows, start=5):
    ws[f"B{i}"], ws[f"C{i}"] = a, b
    ws[f"B{i}"].font = f(10, True)
    ws[f"C{i}"].font = f(10)
ws["B8"].font = ws["C8"].font = f(10, True, GREY)
ws["B21"] = FOOTER
ws["B21"].font = f(8, color=GREY, italic=True)
ws.column_dimensions["B"].width = 16
ws.column_dimensions["C"].width = 110

# =====================================================================================
# Inputs (named cells)
# =====================================================================================
ws = wb.create_sheet("Inputs")
header(ws, "Inputs", "Change the yellow cells; every sheet recalculates")
inputs = [
    ("CloseMonth", "Close month (first day)", None, MONTH, "Pick a month from the list (KPI_Data)."),
    ("PrevMonth", "Previous month", "=EDATE(CloseMonth,-1)", MONTH, "Formula, do not type."),
    ("LastYearMonth", "Same month last year", "=EDATE(CloseMonth,-12)", MONTH, "Formula, do not type."),
    ("Tolerance", "FX tolerance (partner vs ledger EUR)", 0.02, PCT, "Above this gap a conversion is a 'rate gap' break."),
    ("FeeTolerance", "Fee tolerance (EUR)", 0.005, EUR, "Rounding allowed between partner fee and ledger fee."),
    ("UTCOffsetSummer", "Partner offset to UTC, summer (hours)", 3, "0", "Europe/Tallinn = UTC+3 in summer (EEST)."),
    ("UTCOffsetWinter", "Partner offset to UTC, winter (hours)", 2, "0", "Europe/Tallinn = UTC+2 in winter (EET)."),
    ("DSTEndLocal", "Summer time ends (partner local time)", None, DT, "Last Sunday of October 2026, 04:00 local."),
]
from datetime import datetime  # noqa: E402

for i, (nm, label, value, fmt, note) in enumerate(inputs, start=5):
    ws[f"B{i}"], ws[f"D{i}"] = label, note
    ws[f"B{i}"].font = f(10, True)
    ws[f"D{i}"].font = f(9, color=GREY)
    cell = ws[f"C{i}"]
    if nm == "CloseMonth":
        value = datetime(2026, 10, 1)
    if nm == "DSTEndLocal":
        value = datetime(2026, 10, 25, 4, 0)
    cell.value = value
    cell.number_format = fmt
    cell.border = BOX
    if not (isinstance(value, str) and value.startswith("=")):
        cell.fill, cell.font = INPUT_FILL, f(10, True, "0000FF")
    else:
        cell.font = f(10)
    name(wb, nm, f"Inputs!$C${i}")
ws.column_dimensions["B"].width = 40
ws.column_dimensions["C"].width = 22
ws.column_dimensions["D"].width = 60

# =====================================================================================
# KPI_Data, Controls, Data, Ledger  (data tables)
# =====================================================================================
ws = wb.create_sheet("KPI_Data")
add_table(ws, "tblKPI", kpi, formats={"month": MONTH, "stablecoin_share": PCT, **{c: EUR0 for c in kpi.columns if c.endswith("_eur")}})
name(wb, "MonthList", f"KPI_Data!$A$2:$A${len(kpi) + 1}")
dv = DataValidation(type="list", formula1="=MonthList", allow_blank=False, showDropDown=False)
wb["Inputs"].add_data_validation(dv)
dv.add("C5")

ws = wb.create_sheet("Controls")
add_table(ws, "tblControls", controls)
ws.column_dimensions["A"].width = 52

ws = wb.create_sheet("Data")
add_table(ws, "tblPartner", partner, formats={"value_ts_local": DT, "amount_eur": EUR, "fees_eur": EUR})
# calculated column: partner local time -> UTC
n_p = len(partner) + 1
ws.cell(1, 8, "value_ts_utc").font = f(10, True)
for r in range(2, n_p + 1):
    ws.cell(r, 8, "=tblPartner[[#This Row],[value_ts_local]]-IF(tblPartner[[#This Row],[value_ts_local]]>=DSTEndLocal,"
                  "UTCOffsetWinter,UTCOffsetSummer)/24").number_format = DT
ws.tables["tblPartner"].ref = f"A1:H{n_p}"
ws.column_dimensions["H"].width = 22
ws.freeze_panes = "A2"

ws = wb.create_sheet("Ledger")
add_table(ws, "tblLedger", ledger, formats={"booked_ts_utc": DT, "amount_eur": EUR})
n_l = len(ledger) + 1
ws.cell(1, 10, "in_partner_statement").font = f(10, True)
for r in range(2, n_l + 1):
    ws.cell(r, 10, '=IF(tblLedger[[#This Row],[entry_type]]<>"conversion","n/a",'
                   'IF(COUNTIF(tblPartner[ref],tblLedger[[#This Row],[ref]])>0,"yes","MISSING"))')
ws.tables["tblLedger"].ref = f"A1:J{n_l}"
ws.column_dimensions["J"].width = 22
ws.freeze_panes = "A2"

# =====================================================================================
# Recon
# =====================================================================================
ws = wb.create_sheet("Recon", 2)
header(ws, "Partner statement vs ledger - conversions", "Every line of the partner statement matched to the ledger by reference")
T0 = 16  # table header row
R = "tblRecon[[#This Row],[{}]]".format
recon_cols = [
    ("Ref", None, None),
    ("Currency", '=INDEX(tblPartner[currency],MATCH({ref},tblPartner[ref],0))', None),
    ("Segment", '=IFERROR(INDEX(tblLedger[segment],MATCH({ref},tblLedger[ref],0)),"n/a")', None),
    ("Partner time UTC", "=_xlfn.XLOOKUP({ref},tblPartner[ref],tblPartner[value_ts_utc])", DT),
    ("Partner local date", "=INT(_xlfn.XLOOKUP({ref},tblPartner[ref],tblPartner[value_ts_local]))", "yyyy-mm-dd"),
    ("Close month UTC", "=DATE(YEAR({utc}),MONTH({utc}),1)", MONTH),
    ("Partner EUR", "=_xlfn.XLOOKUP({ref},tblPartner[ref],tblPartner[amount_eur])", EUR),
    ("Partner fee", "=_xlfn.XLOOKUP({ref},tblPartner[ref],tblPartner[fees_eur])", EUR),
    ("In ledger", '=COUNTIFS(tblLedger[ref],{ref},tblLedger[entry_type],"conversion")>0', None),
    ("Ledger EUR", '=SUMIFS(tblLedger[amount_eur],tblLedger[ref],{ref},tblLedger[entry_type],"conversion")', EUR),
    ("Ledger booked UTC", '=IF({inl},_xlfn.MAXIFS(tblLedger[booked_ts_utc],tblLedger[ref],{ref},tblLedger[entry_type],"conversion"),"")', DT),
    ("FX diff EUR", "={peur}-{leur}", EUR),
    ("FX diff pct", '=IF({leur}=0,"",{diff}/{leur})', PCT3),
    ("FX adjustment posted", '=SUMIFS(tblLedger[amount_eur],tblLedger[ref],{ref},tblLedger[entry_type],"fx_adjustment")', EUR),
    ("Ledger partner fee", '=SUMIFS(tblLedger[amount_eur],tblLedger[ref],{ref},tblLedger[entry_type],"partner_fee")', EUR),
    ("Status", '=IF(NOT({inl}),"Missing in ledger",IF(ABS({diff})>Tolerance*{leur},'
               'IF({adj}<>0,"Rate gap - adjusted","Rate gap - open"),"Matched"))', None),
    ("Fee status", '=IF({lfee}=0,"Fee missing",IF(ABS({lfee}-{pfee})>FeeTolerance,"Fee mismatch","OK"))', None),
    ("Cutoff flag", '=IF(AND({inl},DATE(YEAR({locd}),MONTH({locd}),1)<>DATE(YEAR({lbk}),MONTH({lbk}),1)),'
                    '"Cut-off (timezone)","")', None),
]
tokens = dict(ref=R("Ref"), utc=R("Partner time UTC"), peur=R("Partner EUR"), pfee=R("Partner fee"),
              inl=R("In ledger"), leur=R("Ledger EUR"), diff=R("FX diff EUR"), adj=R("FX adjustment posted"),
              lfee=R("Ledger partner fee"), locd=R("Partner local date"), lbk=R("Ledger booked UTC"))
for j, (col, formula, fmt) in enumerate(recon_cols, start=2):
    ws.cell(T0, j, col).font = f(10, True)
    letter = ws.cell(T0, j).column_letter
    ws.column_dimensions[letter].width = 20 if j > 2 else 14
    for i, ref in enumerate(recon_refs["Ref"], start=T0 + 1):
        c = ws.cell(i, j, ref if formula is None else formula.format(**tokens))
        if fmt:
            c.number_format = fmt
last_row = T0 + len(recon_refs)
last_col = ws.cell(T0, 1 + len(recon_cols)).column_letter
t = Table(displayName="tblRecon", ref=f"B{T0}:{last_col}{last_row}")
t.tableStyleInfo = TableStyleInfo(name="TableStyleLight1", showRowStripes=True)
ws.add_table(t)
ws.freeze_panes = f"C{T0 + 1}"
ws.column_dimensions["B"].width = 26

# summary block (close month by UTC date)
ws["B5"], ws["C5"], ws["D5"], ws["E5"] = "Status (close month, UTC)", "Lines", "FX diff EUR", "Share of lines"
for c in "BCDE":
    ws[f"{c}5"].font, ws[f"{c}5"].fill = f(10, True), HEAD_FILL
for i, s in enumerate(["Matched", "Rate gap - adjusted", "Rate gap - open", "Missing in ledger"], start=6):
    ws[f"B{i}"] = s
    ws[f"C{i}"] = f'=COUNTIFS(tblRecon[Status],B{i},tblRecon[Close month UTC],CloseMonth)'
    ws[f"D{i}"] = f'=SUMIFS(tblRecon[FX diff EUR],tblRecon[Status],B{i},tblRecon[Close month UTC],CloseMonth)'
    ws[f"E{i}"] = f"=IF(C$10=0,0,C{i}/C$10)"
    ws[f"D{i}"].number_format, ws[f"E{i}"].number_format = EUR, PCT
ws["B10"], ws["C10"], ws["D10"] = "Total", "=SUM(C6:C9)", "=SUM(D6:D9)"
ws["D10"].number_format = EUR
ws["B10"].font = ws["C10"].font = ws["D10"].font = f(10, True)

ws["G5"], ws["H5"] = "Tie-out (close month, UTC)", "EUR"
ws["G5"].font = ws["H5"].font = f(10, True)
ws["G5"].fill = ws["H5"].fill = HEAD_FILL
tie = [("Partner statement total", "=SUMIFS(tblRecon[Partner EUR],tblRecon[Close month UTC],CloseMonth)"),
       ("Ledger conversions total", "=SUMIFS(tblRecon[Ledger EUR],tblRecon[Close month UTC],CloseMonth)"),
       ("Difference", "=H6-H7"),
       ("Explained by FX differences", "=D10"),
       ("Residual (must be 0.00)", "=ROUND(H8-H9,2)")]
for i, (a, b) in enumerate(tie, start=6):
    ws[f"G{i}"], ws[f"H{i}"] = a, b
    ws[f"H{i}"].number_format = EUR
ws["G10"].font = ws["H10"].font = f(10, True)

ws["J5"], ws["K5"], ws["L5"] = "Other checks", "Lines", "EUR"
for c in "JKL":
    ws[f"{c}5"].font, ws[f"{c}5"].fill = f(10, True), HEAD_FILL
ws["J6"], ws["K6"], ws["L6"] = ("Partner fee missing in ledger",
                                '=COUNTIFS(tblRecon[Fee status],"Fee missing",tblRecon[Close month UTC],CloseMonth)',
                                '=SUMIFS(tblRecon[Partner fee],tblRecon[Fee status],"Fee missing",tblRecon[Close month UTC],CloseMonth)')
ws["J7"], ws["K7"], ws["L7"] = ("Month-end cut-off (timezone)",
                                '=COUNTIFS(tblRecon[Cutoff flag],"Cut-off (timezone)")',
                                '=SUMIFS(tblRecon[Partner EUR],tblRecon[Cutoff flag],"Cut-off (timezone)")')
ws["J8"], ws["K8"], ws["L8"] = ("Ledger conversions missing at partner",
                                '=COUNTIFS(tblLedger[in_partner_statement],"MISSING")', "")
ws["J9"], ws["K9"] = "Match rate (lines)", "=IF(C10=0,0,C6/C10)"
ws["K9"].number_format = PCT
for r in (6, 7):
    ws[f"L{r}"].number_format = EUR
ws["B12"] = ("Read me: a line is a 'rate gap' when |partner EUR - ledger EUR| > Tolerance x ledger EUR. "
             "Cut-off lines are booked in one month in UTC but dated the next month in the partner's local time.")
ws["B12"].font = f(9, color=GREY, italic=True)
ws["B14"] = FOOTER
ws["B14"].font = f(8, color=GREY, italic=True)
ws.column_dimensions["G"].width = 30
ws.column_dimensions["J"].width = 36
# conditional formatting
rng_status = f"Q{T0 + 1}:Q{last_row}"
ws.conditional_formatting.add(rng_status, CellIsRule(operator="equal", formula=['"Rate gap - open"'], fill=RED_FILL))
ws.conditional_formatting.add(rng_status, CellIsRule(operator="equal", formula=['"Missing in ledger"'], fill=RED_FILL))
ws.conditional_formatting.add(rng_status, CellIsRule(operator="equal", formula=['"Rate gap - adjusted"'], fill=AMBER_FILL))
ws.conditional_formatting.add(f"R{T0 + 1}:R{last_row}", CellIsRule(operator="equal", formula=['"Fee missing"'], fill=RED_FILL))
ws.conditional_formatting.add(f"N{T0 + 1}:N{last_row}",
                              FormulaRule(formula=[f'AND(N{T0 + 1}<>"",ABS(N{T0 + 1})>Tolerance)'], font=Font(color=RED, bold=True)))
ws.conditional_formatting.add("H10", CellIsRule(operator="notEqual", formula=["0"], fill=RED_FILL))
ws.conditional_formatting.add("H10", CellIsRule(operator="equal", formula=["0"], fill=GREEN_FILL))
ws.conditional_formatting.add("K6:K8", CellIsRule(operator="greaterThan", formula=["0"], fill=RED_FILL))

# =====================================================================================
# P&L
# =====================================================================================
ws = wb.create_sheet("P&L", 2)
header(ws, "P&L - close month vs previous month vs last year", "Reconciled figures (EUR). Change the month on the Inputs sheet.")
heads = ["Line", "Close month", "Previous month", "Change", "Change %", "Same month LY", "YoY %"]
for j, h in enumerate(heads, start=2):
    c = ws.cell(5, j, h)
    c.font, c.fill, c.alignment = f(10, True), HEAD_FILL, Alignment(horizontal="center")
ws["C6"], ws["D6"], ws["G6"] = "=CloseMonth", "=PrevMonth", "=LastYearMonth"
for c in ("C6", "D6", "G6"):
    ws[c].number_format, ws[c].font, ws[c].alignment = MONTH, f(10, True, GREY), Alignment(horizontal="center")


def look(col, month_name):
    return f"=IFERROR(INDEX(tblKPI[{col}],MATCH({month_name},tblKPI[month],0)),NA())"


lines = [  # (label, kpi column or formula over rows, format, bold, is_cost)
    ("Deposit fees", "fee_deposit_eur", EUR0, False),
    ("Conversion fees", "fee_conversion_eur", EUR0, False),
    ("Payout fees", "fee_payout_eur", EUR0, False),
    ("Revenue", "=SUM({c}7:{c}9)", EUR0, True),
    ("Network costs", "network_cost_eur", EUR0, False),
    ("Partner fees", "partner_fee_eur", EUR0, False),
    ("FX result (gain + / loss -)", "fx_result_eur", EUR0, False),
    ("Gross margin", "={c}10-{c}11-{c}12+{c}13", EUR0, True),
    ("Gross margin %", '=IF({c}10=0,"",{c}14/{c}10)', PCT, False),
    ("", None, None, False),
    ("Processed volume", "volume_eur", EUR0, False),
    ("Transactions", "transactions", "#,##0", False),
    ("Take rate (revenue / volume)", '=IF({c}17=0,"",{c}10/{c}17)', PCT3, True),
    ("Active merchants", "active_merchants", "#,##0", False),
    ("Stablecoin share of volume", "stablecoin_share", PCT, False),
]
for i, (label, src, fmt, bold) in enumerate(lines, start=7):
    ws[f"B{i}"] = label
    ws[f"B{i}"].font = f(10, bold)
    if src is None:
        continue
    for col, month_name in (("C", "CloseMonth"), ("D", "PrevMonth"), ("G", "LastYearMonth")):
        ws[f"{col}{i}"] = src.format(c=col) if src.startswith("=") else look(src, month_name)
        ws[f"{col}{i}"].number_format = fmt
        ws[f"{col}{i}"].font = f(10, bold)
    pct_line = fmt in (PCT, PCT3)
    ws[f"E{i}"] = f"=IFERROR(C{i}-D{i},\"\")"
    ws[f"E{i}"].number_format = '+0.00" pt";-0.00" pt"' if pct_line else ('+#,##0;-#,##0;"-"' if fmt != EUR0 else '+#,##0 "€";-#,##0 "€";"-"')
    if pct_line:
        ws[f"E{i}"] = f'=IFERROR((C{i}-D{i})*100,"")'
        ws[f"F{i}"] = ""
        ws[f"H{i}"] = ""
    else:
        ws[f"F{i}"] = f'=IFERROR(C{i}/D{i}-1,"")'
        ws[f"H{i}"] = f'=IFERROR(C{i}/G{i}-1,"")'
        ws[f"F{i}"].number_format = ws[f"H{i}"].number_format = '+0.0%;-0.0%;"-"'
    for col in "BCDEFGH":
        ws[f"{col}{i}"].border = Border(bottom=thin)
for i in (10, 14):
    for col in "BCDEFGH":
        ws[f"{col}{i}"].fill = PatternFill("solid", fgColor="EEF4FC")
# costs that grow faster than revenue in red, growth in blue
for rr in ("F10", "F14", "F17", "F18"):  # revenue, margin, volume, transactions falling > 10 %
    ws.conditional_formatting.add(rr, CellIsRule(operator="lessThan", formula=["-0.1"], font=Font(color=RED, bold=True)))
for rr in ("H10", "H14", "H17", "H18"):
    ws.conditional_formatting.add(rr, CellIsRule(operator="lessThan", formula=["0"], font=Font(color=RED, bold=True)))
ws["B23"] = ("Definitions: Revenue = fees charged to merchants. Gross margin = revenue - network costs - partner fees "
             "+ FX result. Partner fees come from the partner statement (the ledger misses some).")
ws["B23"].font = f(9, color=GREY, italic=True)
ws["B24"] = '=IF(ABS(F10)>0.1,"Revenue moved more than 10% vs last month: explain it in the close note.","Revenue change within 10%.")'
ws["B24"].font = f(10, True, BLUE)
ws["B26"] = FOOTER
ws["B26"].font = f(8, color=GREY, italic=True)
ws.column_dimensions["B"].width = 34
for col in "CDEFGH":
    ws.column_dimensions[col].width = 17

# =====================================================================================
# Pivot (filled by Excel in step 2)
# =====================================================================================
ws = wb.create_sheet("Pivot", 4)
header(ws, "Breaks by segment, currency and status", "PivotTables on tblRecon - use the slicers to filter")
ws["B4"] = "Refresh: Data > Refresh All (after changing data or inputs)."
ws["B4"].font = f(9, color=GREY, italic=True)

# =====================================================================================
# Checklist
# =====================================================================================
ws = wb.create_sheet("Checklist", 5)
header(ws, "Month-end close checklist", "Status is a drop-down; 'Auto check' reads the Recon, P&L and Controls sheets")
cl_heads = ["#", "Step", "Owner", "Due (working day)", "Status", "Auto check", "Comment"]
steps = [
    ("Extract all sources (gateway, explorer, ledger, partner, payouts)", "Data & Reporting", "WD1", "Done",
     '="Partner lines: "&FIXED(COUNTA(tblPartner[ref]),0)&" · ledger lines: "&FIXED(COUNTA(tblLedger[entry_id]),0)'),
    ("Convert partner local time to UTC", "Data & Reporting", "WD1", "Done",
     '=IF(Recon!K7>0,"OK - "&Recon!K7&" cut-off lines moved to the right month","OK - no cut-off line")'),
    ("Reconcile partner statement vs ledger conversions", "Data & Reporting", "WD2", "In progress",
     '=IF(Recon!H10=0,"PASS - residual 0.00","FAIL - residual "&FIXED(Recon!H10,2))'),
    ("Post FX adjustments for open rate gaps", "Treasury Ops", "WD2", "In progress",
     '=IF(Recon!C8=0,"PASS - no open rate gap","ACTION - "&Recon!C8&" open rate gaps, "&FIXED(Recon!D8,2)&" EUR")'),
    ("Book partner fees missing in the ledger", "Finance", "WD3", "Not started",
     '=IF(Recon!K6=0,"PASS","ACTION - "&Recon!K6&" fees, "&FIXED(Recon!L6,2)&" EUR")'),
    ("Chain receipts vs ledger deposits (pipeline C1)", "Data & Reporting", "WD2", "Done",
     '=IF(COUNTIFS(tblControls[control],"C1*",tblControls[result],"FAIL")=0,"PASS","FAIL")'),
    ("Payouts system vs ledger (pipeline C4)", "Treasury Ops", "WD2", "Done",
     '=INDEX(tblControls[result],MATCH("C4*",tblControls[control],0))'),
    ("Review P&L variances above 10%", "Data & Reporting", "WD3", "Not started",
     "='P&L'!B24"),
    ("Sign-off of the close pack", "Head of Finance", "WD4", "Not started", '=IF(COUNTIF(F6:F13,"Done")=8,"Ready for sign-off","Waiting for open steps")'),
]
for j, h in enumerate(cl_heads, start=2):
    c = ws.cell(5, j, h)
    c.font = f(10, True)
for i, (step, owner, due, status, check) in enumerate(steps, start=6):
    ws[f"B{i}"], ws[f"C{i}"], ws[f"D{i}"], ws[f"E{i}"], ws[f"F{i}"], ws[f"G{i}"] = i - 5, step, owner, due, status, check
    ws[f"F{i}"].fill, ws[f"F{i}"].font = INPUT_FILL, f(10, True, "0000FF")
t = Table(displayName="tblChecklist", ref=f"B5:H{5 + len(steps)}")
t.tableStyleInfo = TableStyleInfo(name="TableStyleLight1", showRowStripes=False)
ws.add_table(t)
dv = DataValidation(type="list", formula1='"Not started,In progress,Done,Blocked"', allow_blank=False)
ws.add_data_validation(dv)
dv.add(f"F6:F{5 + len(steps)}")
rng = f"F6:F{5 + len(steps)}"
ws.conditional_formatting.add(rng, CellIsRule(operator="equal", formula=['"Done"'], fill=GREEN_FILL))
ws.conditional_formatting.add(rng, CellIsRule(operator="equal", formula=['"Blocked"'], fill=RED_FILL))
ws.conditional_formatting.add(rng, CellIsRule(operator="equal", formula=['"In progress"'], fill=AMBER_FILL))
ws.conditional_formatting.add(f"G6:G{5 + len(steps)}",
                              FormulaRule(formula=['OR(LEFT(G6,6)="ACTION",LEFT(G6,4)="FAIL")'], font=Font(color=RED, bold=True)))
ws["C17"] = "Completion"
ws["D17"] = f'=COUNTIF(F6:F{5 + len(steps)},"Done")/ROWS(F6:F{5 + len(steps)})'
ws["D17"].number_format = PCT
ws["C17"].font = ws["D17"].font = f(10, True)
ws["B19"] = FOOTER
ws["B19"].font = f(8, color=GREY, italic=True)
for col, wdt in zip("BCDEFGH", [5, 62, 20, 18, 14, 52, 30]):
    ws.column_dimensions[col].width = wdt

# fonts everywhere + page background on report sheets
for sh in wb.worksheets:
    for row in sh.iter_rows():
        for c in row:
            if c.font.name != FONT:
                c.font = Font(name=FONT, size=c.font.size or 10, bold=c.font.bold, color=c.font.color, italic=c.font.italic)

OUT.parent.mkdir(parents=True, exist_ok=True)
wb.save(OUT)
print(f"step 1 saved {OUT}")

# =====================================================================================
# Step 2: Excel itself adds PivotTables + slicers, recalculates, checks for errors
# =====================================================================================
try:
    import pythoncom
    import win32com.client as win32
except ImportError:
    print("pywin32 not installed: skip PivotTables (create them by hand, see README)")
    raise SystemExit

xl = win32.DispatchEx("Excel.Application")
xl.Visible, xl.DisplayAlerts = False, False
try:
    book = xl.Workbooks.Open(str(OUT))
    sh = book.Worksheets("Pivot")
    XL_DATABASE, XL_ROW, XL_COL, XL_DATA, XL_PAGE = 1, 1, 2, 4, 3
    XL_COUNT, XL_SUM = -4112, -4157
    cache = book.PivotCaches().Create(SourceType=XL_DATABASE, SourceData="tblRecon")

    pt1 = cache.CreatePivotTable(TableDestination=sh.Range("B7"), TableName="pvtStatusBySegment")
    pt1.PivotFields("Segment").Orientation = XL_ROW
    pt1.PivotFields("Status").Orientation = XL_COL
    df1 = pt1.AddDataField(pt1.PivotFields("Ref"), "Lines", XL_COUNT)
    pt1.PivotFields("Close month UTC").Orientation = XL_PAGE

    pt2 = cache.CreatePivotTable(TableDestination=sh.Range("B22"), TableName="pvtCurrency")
    pt2.PivotFields("Currency").Orientation = XL_ROW
    d2a = pt2.AddDataField(pt2.PivotFields("Partner EUR"), "Partner EUR ", XL_SUM)
    d2b = pt2.AddDataField(pt2.PivotFields("FX diff EUR"), "FX diff EUR ", XL_SUM)
    d2c = pt2.AddDataField(pt2.PivotFields("Ref"), "Lines ", XL_COUNT)

    pt3 = cache.CreatePivotTable(TableDestination=sh.Range("B40"), TableName="pvtFeeStatus")
    pt3.PivotFields("Fee status").Orientation = XL_ROW
    d3 = pt3.AddDataField(pt3.PivotFields("Partner fee"), "Partner fees ", XL_SUM)
    pt3.AddDataField(pt3.PivotFields("Ref"), "Lines  ", XL_COUNT)

    for pt, row_name in ((pt1, "Segment"), (pt2, "Currency"), (pt3, "Fee status")):
        pt.CompactLayoutRowHeader = row_name
        pt.CompactLayoutColumnHeader = "Status"
        pt.GrandTotalName = "Total"
    # number formats written with this PC's own separators (works in French and English Excel)
    intl = xl.International  # tuple of regional settings (1-based in VBA)
    dec, thou = intl[2], intl[3]
    for d, decimals in ((d2a, 0), (d2b, 2), (d3, 2)):
        rng = d.DataRange.Resize(d.DataRange.Rows.Count + 1)  # + grand total row
        rng.NumberFormatLocal = f'#{thou}##0{dec + "0" * decimals if decimals else ""} "€"'
    for pt in (pt1, pt2, pt3):
        pt.HasAutoFormat = False  # keep column widths when the pivots refresh
    for col, wdt in zip("BCDEFG", [16, 16, 18, 18, 12, 10]):
        sh.Columns(col).ColumnWidth = wdt
    left0 = sh.Range("I7").Left
    for i, fld in enumerate(["Currency", "Status", "Segment"]):
        sc = book.SlicerCaches.Add2(pt1, fld)
        sc.PivotTables.AddPivotTable(pt2)
        sc.PivotTables.AddPivotTable(pt3)
        slc = sc.Slicers.Add(sh, pythoncom.Missing, f"slc{fld}", fld)
        slc.Top, slc.Left, slc.Width, slc.Height = sh.Range("B7").Top, left0 + i * 180, 170, 200

    xl.CalculateFull()
    book.RefreshAll()
    xl.CalculateFull()

    # error scan: every formula cell of every sheet
    errors = {}
    for ws_ in book.Worksheets:
        try:
            cells = ws_.UsedRange.SpecialCells(-4123, 16)  # xlCellTypeFormulas, xlErrors
            errors[ws_.Name] = cells.Address
        except Exception:
            pass  # no error cell on this sheet
    book.Worksheets("README").Activate()
    book.Save()
    print("step 2 done: pivots + slicers + recalc")
    print("formula errors:", errors or "none")
    r = book.Worksheets("Recon")
    print("Recon summary:", [(r.Range(f"B{i}").Value, r.Range(f"C{i}").Value, round(r.Range(f"D{i}").Value or 0, 2)) for i in range(6, 11)])
    print("Residual:", r.Range("H10").Value, "| cut-offs:", r.Range("K7").Value, "| fees missing:", r.Range("K6").Value)
    p = book.Worksheets("P&L")
    print("P&L revenue:", p.Range("C10").Value, p.Range("D10").Value, p.Range("F10").Value)
    book.Close(SaveChanges=False)
finally:
    xl.Quit()

# remove personal traces Excel writes on save (author, file path, pivot "refreshed by")
from sanitize_xlsx import sanitize  # noqa: E402

sanitize(str(OUT))
print("sanitized", OUT)
