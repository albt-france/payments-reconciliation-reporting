"""
Build tableau/CryptoPaymentsReporting.twb : 5 dashboards on the reconciled data (SIMULATED transactions,
REAL prices). Run src/export_tableau.py first (it writes the CSV + .hyper extracts).

    python src/build_tableau.py

Every calculated field below is the same one you would type in Tableau (Analysis > Create Calculated Field).
"""
import json
import zipfile
import sys
from datetime import date
from pathlib import Path

from twb import DataSource, Dashboard, Sheet, Workbook

ROOT = Path(__file__).resolve().parents[1]
DATA = (ROOT / "data" / "output" / "tableau").as_posix()
OUT = ROOT / "tableau" / "CryptoPaymentsReporting.twb"
SCHEMAS = json.loads((ROOT / "data" / "output" / "tableau" / "schemas.json").read_text())

BLUE, LIGHT, GREY, RED, AMBER, GREEN, INK = "#2a78d6", "#86b6ef", "#898781", "#d03b3b", "#eda100", "#1baf7a", "#0b0b0b"
P = "[Parameters]"
SEL = f"{P}.[Selected Month]"
CCY = f'(IF {P}.[Reporting Currency] = "USD" THEN "$" ELSE "€" END)'
FOOTER = "SIMULATED transactions  ·  REAL daily prices (Binance, ECB)  ·  Independent case study, not affiliated with Coinspaid"

wb = Workbook()

# ---------------------------------------------------------------------------------------------------
# Parameters
# ---------------------------------------------------------------------------------------------------
months = [date(2024 + (10 + i) // 12, (10 + i) % 12 + 1, 1) for i in range(24)]
wb.params.add("Selected Month", "date", "#2026-10-01#", "list", members=[f"#{m.isoformat()}#" for m in months],
              fmt="*mmmm yyyy")
wb.params.add("Reporting Currency", "string", '"EUR"', "list", members=['"EUR"', '"USD"'])
wb.params.add("Top N", "integer", "10", "range", rng=(5, 50, 5))
wb.params.add("Decline Threshold", "real", "-0.3", "range", rng=(-0.9, 0, 0.05), fmt="p0%")
wb.params.add("Materiality EUR", "real", "10.0", "range", rng=(0, 500, 5))


def ds(key, caption, captions=None, formats=None, dims=()):
    d = DataSource(f"federated.{key}", caption, DATA, f"{key}.csv", SCHEMAS[key], dims=dims,
                   captions=captions, formats=formats)
    wb.datasources.append(d)
    return d


# ---------------------------------------------------------------------------------------------------
# Data sources
# ---------------------------------------------------------------------------------------------------
biz = ds("business", "Business (monthly)", captions={
    "revenue_eur": "Revenue (EUR)", "volume_eur": "Processed volume (EUR)", "gross_margin_eur": "Gross margin (EUR)",
    "merchant_id": "Merchant", "segment": "Segment", "currency": "Coin", "network": "Network", "month": "Month"})
pnl = ds("pnl_long", "P&L lines", captions={"line": "P&L line", "month": "Month"})
brg = ds("bridge", "Revenue bridge", captions={"step_label": "Step"}, dims=("step_order",))
brk = ds("breaks", "Reconciliation breaks", captions={
    "break_type": "Break type", "amount_at_stake_eur": "Amount at stake (EUR)", "break_date": "Break date",
    "age_bucket": "Age", "merchant_id": "Merchant", "break_id": "Break", "status": "Status"})
rec = ds("recon_daily", "Recon daily match", captions={"source_pair": "Source pair", "date": "Day"})
cry = ds("crypto_daily", "Crypto daily (real prices)", captions={
    "currency": "Coin", "network": "Network", "date": "Day", "volume_crypto": "Coins received",
    "volume_eur": "EUR value received"})

# ---------------------------------------------------------------------------------------------------
# Calculated fields: Business
# ---------------------------------------------------------------------------------------------------
biz.dim("Is selected month", f"[month] = {SEL}", "boolean")
biz.dim("In last 12 months", f"[month] <= {SEL} AND [month] > DATEADD('month', -12, {SEL})", "boolean")
biz.dim("Is coin row", '[network] <> "PAYOUT" AND [segment] <> "Unallocated"', "boolean")
biz.calc("Take rate", "SUM([revenue_eur]) / SUM([volume_eur])", fmt="p0.00%")
biz.calc("Gross margin %", "SUM([gross_margin_eur]) / SUM([revenue_eur])", fmt="p0.0%")
# reporting currency: EUR, or USD at the real daily ECB rate (volume_usd was converted day by day)
biz.calc("FX factor", f'IF {P}.[Reporting Currency] = "USD" THEN SUM([volume_usd]) / SUM([volume_eur]) ELSE 1 END')
biz.calc("Revenue (ccy)", "SUM([revenue_eur]) * [FX factor]", fmt="n#,##0")

# selected month / previous month / same month last year
for key, expr in [("Rev", "[revenue_eur]"), ("Vol", "[volume_eur]"), ("GM", "[gross_margin_eur]"),
                  ("VolUSD", "[volume_usd]")]:
    biz.calc(f"{key} sel", f"SUM(IF [month] = {SEL} THEN {expr} END)")
    biz.calc(f"{key} prev", f"SUM(IF [month] = DATEADD('month', -1, {SEL}) THEN {expr} END)")
    biz.calc(f"{key} LY", f"SUM(IF [month] = DATEADD('month', -12, {SEL}) THEN {expr} END)")
biz.calc("Stable sel", f'SUM(IF [month] = {SEL} AND [coin_type] = "Stablecoin" THEN [volume_eur] END) / [Vol sel]')
biz.calc("Stable prev", f"SUM(IF [month] = DATEADD('month', -1, {SEL}) AND [coin_type] = \"Stablecoin\" "
                        f"THEN [volume_eur] END) / [Vol prev]")
biz.calc("Merchants sel", f"COUNTD(IF [month] = {SEL} AND [deposits_n] > 0 THEN [merchant_id] END)")
biz.calc("Merchants prev", f"COUNTD(IF [month] = DATEADD('month', -1, {SEL}) AND [deposits_n] > 0 THEN [merchant_id] END)")
biz.calc("Rate sel", f'IF {P}.[Reporting Currency] = "USD" THEN [VolUSD sel] / [Vol sel] ELSE 1 END')
biz.calc("Revenue sel (ccy)", "[Rev sel] * [Rate sel]", fmt="n#,##0")


def pct_change(name, now, before, unit="%"):
    """Two text fields (up in blue, down in red) so the arrow gets its colour."""
    if unit == "%":
        chg = f"({now} / {before} - 1)"
        txt = f'STR(ROUND(ABS({chg}) * 100, 1)) + " % vs previous month"'
    else:  # change in points for ratios
        chg = f"({now} - {before})"
        txt = f'STR(ROUND(ABS({chg}) * 100, 2)) + " pt vs previous month"'
    biz.calc(f"{name} up", f'IF {chg} >= 0 THEN "▲ " + {txt} END', "string", "measure", "nominal")
    biz.calc(f"{name} down", f'IF {chg} < 0 THEN "▼ " + {txt} END', "string", "measure", "nominal")


biz.calc("KPI revenue", f'{CCY} + STR(ROUND([Revenue sel (ccy)] / 1000, 1)) + "k"', "string", "measure", "nominal")
pct_change("Revenue", "[Rev sel]", "[Rev prev]")
biz.calc("Revenue YoY txt", 'STR(ROUND(([Rev sel] / [Rev LY] - 1) * 100, 1)) + " % year on year"', "string", "measure", "nominal")
biz.calc("KPI volume", f'{CCY} + STR(ROUND([Vol sel] * [Rate sel] / 1000000, 2)) + "M"', "string", "measure", "nominal")
pct_change("Volume", "[Vol sel]", "[Vol prev]")
biz.calc("Volume YoY txt", 'STR(ROUND(([Vol sel] / [Vol LY] - 1) * 100, 1)) + " % year on year"', "string", "measure", "nominal")
biz.calc("KPI take rate", 'STR(ROUND([Rev sel] / [Vol sel] * 100, 3)) + " %"', "string", "measure", "nominal")
pct_change("Take rate", "([Rev sel] / [Vol sel])", "([Rev prev] / [Vol prev])", "pt")
biz.calc("KPI margin", 'STR(ROUND([GM sel] / [Rev sel] * 100, 1)) + " %"', "string", "measure", "nominal")
pct_change("Margin", "([GM sel] / [Rev sel])", "([GM prev] / [Rev prev])", "pt")
biz.calc("KPI merchants", "STR([Merchants sel])", "string", "measure", "nominal")
pct_change("Merchants", "[Merchants sel]", "[Merchants prev]")
biz.calc("KPI stablecoins", 'STR(ROUND([Stable sel] * 100, 1)) + " %"', "string", "measure", "nominal")
pct_change("Stable", "[Stable sel]", "[Stable prev]", "pt")

# Table calculation: growth vs the same month last year (LOOKUP 12 months back along the month axis)
biz.calc("Revenue YoY % (table calc)",
         "(SUM([revenue_eur]) - LOOKUP(SUM([revenue_eur]), -12)) / ABS(LOOKUP(SUM([revenue_eur]), -12))", fmt="p0%")

# LOD expressions
biz.calc("Avg revenue per merchant (INCLUDE)", "AVG({INCLUDE [merchant_id] : SUM([revenue_eur])})", fmt="n#,##0")
biz.calc("Running % of revenue", "RUNNING_SUM(SUM([revenue_eur])) / TOTAL(SUM([revenue_eur]))", fmt="p0%")
biz.calc("Merchant rank", "INDEX()")
biz.calc("In Top N", f"RANK(SUM([revenue_eur])) <= {P}.[Top N]", "boolean", "measure", "nominal")
biz.calc("Rev last 3m (FIXED)", "{FIXED [merchant_id] : SUM(IF [month] > DATEADD('month', -3, " + SEL +
         ") AND [month] <= " + SEL + " THEN [revenue_eur] END)}")
biz.calc("Rev prior 3m (FIXED)", "{FIXED [merchant_id] : SUM(IF [month] > DATEADD('month', -6, " + SEL +
         ") AND [month] <= DATEADD('month', -3, " + SEL + ") THEN [revenue_eur] END)}")
biz.calc("3m change", "MIN([Rev last 3m (FIXED)]) / MIN([Rev prior 3m (FIXED)]) - 1", fmt="p0%")
biz.calc("Decline flag", f'IF [3m change] <= {P}.[Decline Threshold] AND MIN([Rev prior 3m (FIXED)]) >= 1000 '
                         'THEN "Decline alert" ELSE "Stable or growing" END', "string", "measure", "nominal")
biz.calc("Prior 3m", "MIN([Rev prior 3m (FIXED)])")
biz.calc("Segment attr", "ATTR([segment])", "string", "measure", "nominal")
biz.calc("In Top N prior 3m", f"RANK(MIN([Rev prior 3m (FIXED)])) <= {P}.[Top N]", "boolean", "measure", "nominal")
biz.calc("Watch text", '"€" + STR(ROUND(MIN([Rev prior 3m (FIXED)]) / 1000, 1)) + "k  →  €" + '
                       'STR(ROUND(MIN([Rev last 3m (FIXED)]) / 1000, 1)) + "k   (" + '
                       'STR(ROUND([3m change] * 100, 0)) + " %)"', "string", "measure", "nominal")
biz.dim("First month (FIXED)", "{FIXED [merchant_id] : MIN(IF [deposits_n] > 0 THEN [month] END)}", "date")
biz.dim("Cohort", 'IF [signup_date] < #2024-11-01# THEN "Before Nov 2024" ELSE '
                  'STR(YEAR([First month (FIXED)])) + "-" + RIGHT("0" + STR(MONTH([First month (FIXED)])), 2) END')
biz.dim("Months since first", "DATEDIFF('month', [First month (FIXED)], [month])", "integer")
biz.calc("Cohort size (FIXED)", "{FIXED [Cohort] : COUNTD([merchant_id])}")
biz.calc("Retention", "COUNTD(IF [deposits_n] > 0 THEN [merchant_id] END) / MIN([Cohort size (FIXED)])", fmt="p0%")
biz.dim("Cohort in scope", '[Cohort] <> "Before Nov 2024" AND [First month (FIXED)] <= #2025-10-01# '
                           'AND [Months since first] >= 0 AND [Months since first] <= 12', "boolean")

# ---------------------------------------------------------------------------------------------------
# Calculated fields: P&L lines, bridge, breaks, recon, crypto
# ---------------------------------------------------------------------------------------------------
pnl.dim("In last 12 months", f"[month] <= {SEL} AND [month] > DATEADD('month', -12, {SEL})", "boolean")
pnl.dim("Is revenue", '[kind] = "Revenue"', "boolean")
pnl.dim("Is cost", '[kind] = "Cost"', "boolean")
pnl.calc("Amount (ccy)", f'SUM([amount_eur] * (IF {P}.[Reporting Currency] = "USD" THEN [usd_per_eur] ELSE 1 END))',
         fmt="n#,##0")

brg.dim("Is selected month", f"[month] = {SEL}", "boolean")
brg.calc("WF delta", "IF MIN([step_order]) = 5 THEN 0 ELSE SUM([value]) END")
brg.calc("WF end", "IF MIN([step_order]) = 5 THEN SUM([value]) ELSE RUNNING_SUM([WF delta]) END", fmt="n#,##0")
brg.calc("WF size", "-SUM([value])")
brg.calc("WF color", 'IF MIN([step_order]) = 1 OR MIN([step_order]) = 5 THEN "Revenue" '
                     'ELSEIF SUM([value]) >= 0 THEN "Increase" ELSE "Decrease" END', "string", "measure", "nominal")
brg.calc("WF label", '(IF MIN([step_order]) = 1 OR MIN([step_order]) = 5 THEN "€" ELSEIF SUM([value]) >= 0 '
                     'THEN "+€" ELSE "-€" END) + STR(ROUND(ABS(SUM([value])) / 1000, 1)) + "k"', "string", "measure", "nominal")

brk.dim("Is open", '[status] = "open"', "boolean")
brk.dim("Above materiality", f"[amount_at_stake_eur] >= {P}.[Materiality EUR]", "boolean")
brk.dim("Age", 'CASE [age_bucket] WHEN "0-2 d" THEN "a) 0-7 days" WHEN "3-7 d" THEN "a) 0-7 days" '
               'WHEN "8-30 d" THEN "b) 8-30 days" WHEN "> 30 d" THEN "c) over 30 days" ELSE "n/a" END')
brk.dim("Status label", 'CASE [status] WHEN "resolved" THEN "1 Resolved" WHEN "auto_cleared" '
                        'THEN "2 Auto-cleared (time zone)" ELSE "3 Open" END')
brk.calc("Open amount", 'SUM(IF [status] = "open" THEN [amount_at_stake_eur] END)', fmt="n#,##0")
brk.calc("Open breaks", 'SUM(IF [status] = "open" THEN 1 ELSE 0 END)')
brk.calc("Share over 30 days", 'SUM(IF [status] = "open" AND [age_bucket] = "> 30 d" THEN [amount_at_stake_eur] END) '
                               '/ [Open amount]', fmt="p0%")
brk.calc("Breaks", "COUNT([break_id])")
brk.calc("KPI open amount", '"€" + STR(ROUND([Open amount] / 1000, 1)) + "k"', "string", "measure", "nominal")
brk.calc("KPI open breaks", "STR([Open breaks])", "string", "measure", "nominal")
brk.calc("KPI over 30", 'STR(ROUND([Share over 30 days] * 100, 0)) + " %"', "string", "measure", "nominal")

rec.calc("Match rate", "SUM([matched]) / SUM([records])", fmt="p0.00%")
rec.calc("Not matched %", "1 - SUM([matched]) / SUM([records])", fmt="p0.0%")
rec.calc("Records per source (FIXED)", "{FIXED [source_pair] : SUM([records])}", fmt="n#,##0")
rec.calc("KPI match", 'STR(ROUND([Match rate] * 100, 2)) + " %"', "string", "measure", "nominal")
rec.calc("Source text", 'STR(ROUND([Match rate] * 100, 2)) + " % matched of " + '
                        'STR(ROUND(MIN([Records per source (FIXED)]) / 1000, 0)) + "k records"', "string", "measure", "nominal")

cry.dim("Is BTC", '[currency] = "BTC"', "boolean")
cry.calc("Share of month (EXCLUDE)", "SUM([volume_eur]) / SUM({EXCLUDE [currency] : SUM([volume_eur])})", fmt="p0%")
cry.calc("Underpaid rate", "SUM([invoices_underpaid]) / SUM([invoices_paid])", fmt="p0.00%")
cry.calc("Avg network fee (EUR)", "AVG([avg_network_fee_eur])", fmt="n#,##0.00")
cry.calc("Minutes to credit", "AVG([avg_minutes_to_credit])", fmt="n#,##0")
cry.calc("BTC price (EUR)", "AVG([price_eur])", fmt="n#,##0")

# ---------------------------------------------------------------------------------------------------
# Worksheets
# ---------------------------------------------------------------------------------------------------
S = wb.sheets.append


def kpi(name, d, caption, value, up=None, down=None, extra=None):
    runs = [(caption, dict(bold=True, size=9, color=GREY)), ("\n", {}), ("{" + value + "}", dict(bold=True, size=22))]
    if up:
        runs += [("\n", {}), ("{" + up + "}", dict(size=9, color=BLUE)), ("{" + down + "}", dict(size=9, color=RED))]
    if extra:
        runs += [("\n", {}), ("{" + extra + "}", dict(size=9, color=GREY))]
    S(Sheet(name, d, mark="Text", label_runs=runs))


# --- 1 Executive overview
kpi("KPI Revenue", biz, "REVENUE", "usr:KPI revenue:nk", "usr:Revenue up:nk", "usr:Revenue down:nk", "usr:Revenue YoY txt:nk")
kpi("KPI Volume", biz, "PROCESSED VOLUME", "usr:KPI volume:nk", "usr:Volume up:nk", "usr:Volume down:nk", "usr:Volume YoY txt:nk")
kpi("KPI Take rate", biz, "TAKE RATE (REVENUE / VOLUME)", "usr:KPI take rate:nk", "usr:Take rate up:nk", "usr:Take rate down:nk")
kpi("KPI Margin", biz, "GROSS MARGIN %", "usr:KPI margin:nk", "usr:Margin up:nk", "usr:Margin down:nk")
kpi("KPI Merchants", biz, "ACTIVE MERCHANTS", "usr:KPI merchants:nk", "usr:Merchants up:nk", "usr:Merchants down:nk")
kpi("KPI Stablecoins", biz, "STABLECOIN SHARE OF VOLUME", "usr:KPI stablecoins:nk", "usr:Stable up:nk", "usr:Stable down:nk")
S(Sheet("Revenue and margin trend", biz, title="Revenue (bars) and gross margin % (line), last 12 months",
        mark="Bar", cols=["tmn:month:qk"], rows=["usr:Revenue (ccy):qk"], mark_color=BLUE,
        dual=("usr:Gross margin %:qk", "Line", RED), filters=[("none:In last 12 months:nk", "true", None)],
        axis_titles={"tmn:month:qk": " "}))
S(Sheet("Revenue YoY", biz, title="Revenue growth vs same month last year (table calculation)",
        mark="Bar", cols=["tmn:month:qk"], rows=["usr:Revenue YoY % (table calc):qk"], mark_color=BLUE,
        axis_titles={"tmn:month:qk": " ", "usr:Revenue YoY % (table calc):qk": "YoY growth"}))
S(Sheet("Revenue by segment", biz, title="Revenue by segment, selected month", mark="Bar",
        rows=["none:segment:nk"], cols=["usr:Revenue sel (ccy):qk"], mark_color=BLUE, labels=True,
        axis_titles={"usr:Revenue sel (ccy):qk": "Revenue, selected month"},
        sort=("none:segment:nk", "usr:Revenue sel (ccy):qk")))

# --- 2 Revenue & margin
S(Sheet("Revenue by fee type", pnl, title="Revenue by fee type, last 12 months", mark="Bar",
        cols=["tmn:month:qk"], rows=["usr:Amount (ccy):qk"], color="none:line:nk",
        color_map={'"Deposit fees"': BLUE, '"Conversion fees"': LIGHT, '"Payout fees"': GREY},
        filters=[("none:Is revenue:nk", "true", None), ("none:In last 12 months:nk", "true", None)], axis_titles={"tmn:month:qk": " "}))
S(Sheet("Costs by type", pnl, title="Costs by type, last 12 months: network fees drive the swings", mark="Bar",
        cols=["tmn:month:qk"], rows=["usr:Amount (ccy):qk"], color="none:line:nk",
        color_map={'"Network costs"': RED, '"Partner fees"': AMBER, '"FX loss"': GREY},
        filters=[("none:Is cost:nk", "true", None), ("none:In last 12 months:nk", "true", None)], axis_titles={"tmn:month:qk": " "}))
S(Sheet("Revenue bridge", brg, title="Revenue bridge: previous month to selected month", mark="GanttBar",
        cols=["none:step_label:nk"], rows=["usr:WF end:qk"], size="usr:WF size:qk", mark_color=BLUE,
        label_runs=[("{usr:WF label:nk}", dict(size=9))],
        filters=[("none:Is selected month:nk", "true", None)],
        axis_range={"usr:WF end:qk": dict(min="50000", range_type="fixedmin")},
        axis_titles={"usr:WF end:qk": "Revenue (EUR)", "none:step_label:nk": " "}))
S(Sheet("Margin by segment and coin", biz, title="Gross margin % by segment and coin, last 12 months", mark="Square",
        rows=["none:segment:nk"], cols=["none:currency:nk"], color="usr:Gross margin %:qk", text="usr:Gross margin %:qk",
        filters=[("none:In last 12 months:nk", "true", None), ("none:Is coin row:nk", "true", None)]))
S(Sheet("Revenue per merchant", biz, title="Average revenue per merchant, selected month (INCLUDE)", mark="Bar",
        rows=["none:segment:nk"], cols=["usr:Avg revenue per merchant (INCLUDE):qk"], mark_color=BLUE, labels=True,
        sort=("none:segment:nk", "usr:Avg revenue per merchant (INCLUDE):qk"),
        filters=[("none:Is selected month:nk", "true", None)]))

# --- 3 Merchants
S(Sheet("Top merchants", biz, title="Top N merchants, last 12 months (orange = revenue falling). Click one.",
        mark="Bar", rows=["none:merchant_id:nk"], cols=["sum:revenue_eur:qk"], color="usr:3m change:qk",
        sort=("none:merchant_id:nk", "sum:revenue_eur:qk"), context=("none:In last 12 months:nk",),
        filters=[("none:In last 12 months:nk", "true", None),
                 ("none:merchant_id:nk", "top", ("[Parameters].[Top N]", "SUM([revenue_eur])"))],
        axis_titles={"sum:revenue_eur:qk": "Revenue, last 12 months (EUR)"}))
S(Sheet("Merchant trend", biz, title="Monthly revenue of the merchant(s) clicked (all merchants if none)", mark="Line",
        cols=["tmn:month:qk"], rows=["sum:revenue_eur:qk"], mark_color=BLUE, axis_titles={"tmn:month:qk": " "}))
S(Sheet("Decline watchlist", biz, title="Watchlist: revenue prior 3 months → last 3 months (orange = falling)",
        mark="Text", rows=["none:merchant_id:nk"], color="usr:3m change:qk",
        label_runs=[("{usr:Segment attr:nk}   {usr:Watch text:nk}", dict(size=9))],
        sort=("none:merchant_id:nk", "usr:Prior 3m:qk"),
        filters=[("none:merchant_id:nk", "top", ("[Parameters].[Top N]", "MIN([Rev prior 3m (FIXED)])"))]))
S(Sheet("Pareto", biz, title="Revenue concentration (Pareto), last 12 months", mark="Area",
        cols=["usr:Merchant rank:qk"], rows=["usr:Running % of revenue:qk"], detail=["none:merchant_id:nk"],
        mark_color=BLUE, sort=("none:merchant_id:nk", "sum:revenue_eur:qk"),
        tc={"usr:Merchant rank:qk": ("Field", "none:merchant_id:nk"),
            "usr:Running % of revenue:qk": ("Field", "none:merchant_id:nk")},
        filters=[("none:In last 12 months:nk", "true", None)]))
S(Sheet("Cohorts", biz, title="Share of merchants still active, by month of first payment (LOD FIXED)", mark="Square",
        rows=["none:Cohort:nk"], cols=["none:Months since first:ok"], color="usr:Retention:qk", text="usr:Retention:qk",
        filters=[("none:Cohort in scope:nk", "true", None)]))

# --- 4 Reconciliation
kpi("KPI Open amount", brk, "OPEN AMOUNT AT STAKE", "usr:KPI open amount:nk")
kpi("KPI Open breaks", brk, "OPEN BREAKS", "usr:KPI open breaks:nk")
kpi("KPI Over 30", brk, "OF OPEN AMOUNT IS OLDER THAN 30 DAYS", "usr:KPI over 30:nk")
kpi("KPI Match rate", rec, "RECORDS MATCHED AUTOMATICALLY", "usr:KPI match:nk")
S(Sheet("Open breaks by type", brk, title="Open breaks by type and age (EUR at stake)", mark="Bar",
        rows=["none:break_type:nk"], cols=["usr:Open amount:qk"], color="none:Age:nk",
        color_map={'"1. 0-2 days"': LIGHT, '"2. 3-7 days"': BLUE, '"3. 8-30 days"': AMBER, '"4. over 30 days"': RED},
        sort=("none:break_type:nk", "usr:Open amount:qk"), filters=[("none:Is open:nk", "true", None)]))
S(Sheet("Breaks per month", brk, title="Breaks found per month, by status", mark="Bar",
        cols=["tmn:break_date:qk"], rows=["usr:Breaks:qk"], color="none:Status label:nk", axis_titles={"tmn:break_date:qk": " "},
        color_map={'"open"': RED, '"resolved"': BLUE, '"auto_cleared"': GREY}))
S(Sheet("Match by source", rec, title="Match rate by source pair (FIXED total records)", mark="Text",
        rows=["none:source_pair:nk"], label_runs=[("{usr:Source text:nk}", dict(size=10))]))
S(Sheet("Match rate trend", rec, title="Share of records NOT matched automatically, by month", mark="Line",
        cols=["tmn:date:qk"], rows=["usr:Not matched %:qk"], color="none:source_pair:nk", axis_titles={"tmn:date:qk": " "},
        color_map={'"Invoices vs Chain"': BLUE, '"Ledger vs Chain"': AMBER, '"Ledger vs Partner"': GREY}))
S(Sheet("Open breaks detail", brk, title="Open breaks above the materiality threshold (export: Download > Crosstab)",
        mark="Text", rows=["none:break_id:nk", "none:break_type:nk", "none:merchant_id:nk", "none:Age:nk"],
        text="sum:amount_at_stake_eur:qk", sort=("none:break_id:nk", "sum:amount_at_stake_eur:qk"),
        filters=[("none:Is open:nk", "true", None), ("none:Above materiality:nk", "true", None)]))

# --- 5 Crypto mix & volatility
S(Sheet("BTC received vs price", cry, title="BTC received (coins, bars) vs BTC price in EUR (line, real)", mark="Bar",
        cols=["tmn:date:qk"], rows=["sum:volume_crypto:qk"], mark_color=GREY, dual=("usr:BTC price (EUR):qk", "Line", RED),
        axis_titles={"tmn:date:qk": " "},
        filters=[("none:Is BTC:nk", "true", None)]))
S(Sheet("EUR value of BTC", cry, title="EUR value of the BTC received", mark="Bar",
        cols=["tmn:date:qk"], rows=["sum:volume_eur:qk"], mark_color=BLUE, filters=[("none:Is BTC:nk", "true", None)],
        axis_titles={"tmn:date:qk": " "}))
S(Sheet("Coin mix", cry, title="Share of EUR volume by coin (EXCLUDE): stablecoins keep rising", mark="Area",
        cols=["tmn:date:qk"], rows=["usr:Share of month (EXCLUDE):qk"], color="none:currency:nk", axis_titles={"tmn:date:qk": " "},
        color_map={'"USDT"': GREEN, '"USDC"': "#8fd3b6", '"BTC"': AMBER, '"ETH"': "#6250d6", '"LTC"': GREY,
                   '"TRX"': RED, '"BNB"': "#e87ba4", '"SOL"': LIGHT}))
S(Sheet("Minutes to credit", cry, title="Minutes from invoice to merchant credit, by network", mark="Bar",
        rows=["none:network:nk"], cols=["usr:Minutes to credit:qk"], mark_color=BLUE, labels=True,
        sort=("none:network:nk", "usr:Minutes to credit:qk")))
S(Sheet("Underpayment vs fee", cry, title="Underpaid invoices vs network fee (one dot = network x month)", mark="Circle",
        cols=["usr:Avg network fee (EUR):qk"], rows=["usr:Underpaid rate:qk"], color="none:network:nk",
        detail=["tmn:date:ok"]))

# ---------------------------------------------------------------------------------------------------
# Dashboards (1366 x 768, floating layout)
# ---------------------------------------------------------------------------------------------------


def chart_with_legend(d, sheet, src, field, x, y, w, h, lw=140):
    """Chart card with its colour legend in a narrow column on the right."""
    d.sheet(sheet, x, y, w - lw, h)
    d.legend(sheet, f"[{src.name}].[{field}]", x + w - lw, y, lw, h)


def frame(name, title, subtitle, params=()):
    d = Dashboard(name)
    x = 1236
    for pname, w in reversed(params):
        x -= w + 8
        d.param(pname, x, 12, w, 56)
    d.text([(title, dict(bold=True, size=16)), ("\n", {}), (subtitle, dict(size=10, color=GREY))], 16, 8, x - 24, 66)
    d.text([("SIMULATED", dict(bold=True, size=10, color="#ffffff", align="1"))], 1250, 22, 100, 28, bg=RED)
    d.text([(FOOTER, dict(size=8, color=GREY))], 16, 742, 1334, 22)
    wb.dashboards.append(d)
    return d


d1 = frame("1 Executive overview",
           "October 2026: revenue +42 % year on year, take rate steady at 1.5 %",
           "Monthly KPIs on reconciled data. Pick a month and a reporting currency. September margin fell to 22 % on network fees.",
           [("Selected Month", 170), ("Reporting Currency", 150)])
for i, n in enumerate(["KPI Revenue", "KPI Volume", "KPI Take rate", "KPI Margin", "KPI Merchants", "KPI Stablecoins"]):
    d1.sheet(n, 16 + i * 223, 80, 215, 118, title=False)
d1.sheet("Revenue and margin trend", 16, 206, 860, 530)
d1.sheet("Revenue YoY", 884, 206, 466, 262)
d1.sheet("Revenue by segment", 884, 474, 466, 262)

d2 = frame("2 Revenue & margin",
           "Growth is volume-driven; network fees, not prices, move the margin",
           "Sep to Oct 2026: +€7.3k from volume, +€0.2k from mix, -€0.7k from take rate. Network costs peaked in Sep 2026.",
           [("Selected Month", 170), ("Reporting Currency", 150)])
chart_with_legend(d2, "Revenue by fee type", pnl, "none:line:nk", 16, 80, 664, 326)
chart_with_legend(d2, "Costs by type", pnl, "none:line:nk", 688, 80, 662, 326)
d2.sheet("Revenue bridge", 16, 412, 520, 324)
d2.sheet("Margin by segment and coin", 544, 412, 500, 324)
d2.sheet("Revenue per merchant", 1052, 412, 298, 324)

d3 = frame("3 Merchants",
           "Top 20 merchants = 39 % of revenue; M0172 fell from 12 % to 0.2 %",
           "Top N and decline alert (last 3 months vs previous 3) are parameters. Click a merchant to see its trend.",
           [("Selected Month", 160), ("Top N", 120), ("Decline Threshold", 150)])
d3.sheet("Top merchants", 16, 80, 420, 326)
d3.sheet("Merchant trend", 444, 80, 420, 326)
d3.sheet("Decline watchlist", 872, 80, 478, 326, fit="fit-width")
d3.sheet("Pareto", 16, 412, 540, 324)
d3.sheet("Cohorts", 564, 412, 786, 324)
wb.filter_action("Show merchant trend", "3 Merchants", "Top merchants",
                 exclude=("Decline watchlist", "Pareto", "Cohorts"))

d4 = frame("4 Reconciliation & data quality",
           "Totals tie out to €0.00, but €177k of breaks are open, 85 % for over 30 days",
           "6 sources, 4.2 M rows, 16 control totals at €0.00 after adjustments. Materiality filters the detail list.",
           [("Materiality EUR", 170)])
for i, n in enumerate(["KPI Open amount", "KPI Open breaks", "KPI Over 30", "KPI Match rate"]):
    d4.sheet(n, 16 + i * 335, 80, 327, 100, title=False)
chart_with_legend(d4, "Open breaks by type", brk, "none:Age:nk", 16, 188, 440, 270, lw=118)
chart_with_legend(d4, "Breaks per month", brk, "none:Status label:nk", 464, 188, 540, 270, lw=150)
d4.sheet("Match by source", 1012, 188, 338, 270, fit="fit-width")
chart_with_legend(d4, "Match rate trend", rec, "none:source_pair:nk", 16, 466, 440, 270, lw=128)
d4.sheet("Open breaks detail", 464, 466, 886, 270, fit="fit-width")

d5 = frame("5 Crypto mix & volatility",
           "Feb 2026: BTC fell 25 %; we received 24 % more BTC, worth 6 % less in EUR",
           "iGaming and Forex players send a familiar number of coins, so a price drop cuts EUR revenue. Prices are real.")
d5.sheet("BTC received vs price", 16, 80, 664, 326)
d5.sheet("EUR value of BTC", 688, 80, 662, 326)
chart_with_legend(d5, "Coin mix", cry, "none:currency:nk", 16, 412, 560, 324, lw=90)
d5.sheet("Minutes to credit", 584, 412, 340, 324)
chart_with_legend(d5, "Underpayment vs fee", cry, "none:network:nk", 932, 412, 418, 324, lw=100)

OUT.parent.mkdir(parents=True, exist_ok=True)
wb.write(OUT)
if "--previews" in sys.argv:  # one copy per dashboard, opening on that dashboard (for screenshots)
    for i in range(len(wb.dashboards)):
        wb.active = i
        wb.write(OUT.with_name(f"_preview_{i + 1}.twb"))
print(f"written {OUT}  ({len(wb.sheets)} sheets, {len(wb.dashboards)} dashboards)")

# Packaged workbook (.twbx = zip): the workbook + its extracts, with relative paths, opens on any computer
twbx = OUT.with_suffix(".twbx")
xml = OUT.read_text(encoding="utf-8").replace(DATA, "Data/Extracts")
with zipfile.ZipFile(twbx, "w", zipfile.ZIP_DEFLATED) as z:
    z.writestr(OUT.name, xml)
    for f in sorted(Path(DATA).glob("*")):
        if f.suffix in (".hyper", ".csv"):
            z.write(f, f"Data/Extracts/{f.name}")
print(f"written {twbx}  ({twbx.stat().st_size / 1e6:.1f} MB)")
