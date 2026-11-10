"""Write notebooks/checks.ipynb (short validation notebook) and execute it so outputs are saved."""
from pathlib import Path

import nbformat as nbf
from nbconvert.preprocessors import ExecutePreprocessor

ROOT = Path(__file__).resolve().parents[1]
nb = nbf.v4.new_notebook()
md, code = nbf.v4.new_markdown_cell, nbf.v4.new_code_cell

nb.cells = [
    md("# Reconciliation checks\n"
       "Short notebook: scores the SQL pipeline against the planted breaks, shows the control totals, "
       "and draws 4 control charts. **SIMULATED transactions, REAL daily prices.** "
       "Run `python src/generate_sim.py` then `python src/run_sql.py` first."),
    code("import duckdb, pandas as pd, matplotlib.pyplot as plt\n"
         "from pathlib import Path\n"
         "ROOT = Path.cwd().parent if Path.cwd().name == 'notebooks' else Path.cwd()\n"
         "con = duckdb.connect(str(ROOT / 'data' / 'recon.duckdb'), read_only=True)\n"
         "q = lambda sql: con.execute(sql).df()\n"
         "BLUE, GREY, RED = '#2A78D6', '#898781', '#D03B3B'\n"
         "plt.rcParams.update({'font.size': 10, 'axes.spines.top': False, 'axes.spines.right': False})"),
    md("## 1. Did the pipeline find the planted breaks?\n"
       "`recall` = share of planted breaks found; `precision` = share of found breaks that were really planted."),
    code("q('SELECT * FROM validation')"),
    md("## 2. Do all control totals tie out?\n`residual` must be 0 on every line."),
    code("q('SELECT control, unit, round(difference, 2) AS difference, round(explained, 2) AS explained, residual, result FROM controls')"),
    md("## 3. Control charts"),
    code("v = q(\"SELECT issue_type, planted, true_positives, missed FROM validation WHERE issue_type IS NOT NULL ORDER BY planted\")\n"
         "fig, ax = plt.subplots(figsize=(8, 4))\n"
         "ax.barh(v.issue_type, v.true_positives, color=BLUE, label='found')\n"
         "ax.barh(v.issue_type, v.missed, left=v.true_positives, color=RED, label='missed')\n"
         "ax.set_title('Planted breaks found by the pipeline', loc='left', fontweight='bold'); ax.legend(frameon=False)\n"
         "fig.tight_layout(); fig.savefig(ROOT / 'docs' / 'check_validation.png', dpi=150)"),
    code("k = q('SELECT month, revenue_eur, gross_margin_pct FROM kpis_monthly')\n"
         "fig, ax = plt.subplots(figsize=(8, 3.5))\n"
         "ax.bar(k.month, k.revenue_eur / 1e3, width=20, color=BLUE); ax.set_ylabel('Revenue (k EUR)')\n"
         "ax2 = ax.twinx(); ax2.plot(k.month, k.gross_margin_pct * 100, color=RED, marker='o'); ax2.set_ylabel('Gross margin %')\n"
         "ax.set_title('Revenue grows every quarter; margin swings with network fees', loc='left', fontweight='bold')\n"
         "fig.tight_layout(); fig.savefig(ROOT / 'docs' / 'check_revenue_margin.png', dpi=150)"),
    code("b = q(\"\"\"SELECT date_trunc('month', f.date) AS month, sum(f.volume_crypto) AS btc_received,\n"
         "              sum(f.volume_eur) AS eur_value, avg(p.price_eur) AS btc_price\n"
         "       FROM fact_daily_currency f JOIN price_eur p ON p.d = f.date AND p.currency = 'BTC'\n"
         "       WHERE f.currency = 'BTC' GROUP BY 1 ORDER BY 1\"\"\")\n"
         "b = b.set_index('month'); idx = b / b.iloc[0] * 100\n"
         "fig, ax = plt.subplots(figsize=(8, 3.5))\n"
         "ax.plot(idx.index, idx.btc_received, color=GREY, label='BTC received (coins)')\n"
         "ax.plot(idx.index, idx.eur_value, color=BLUE, lw=2, label='EUR value of BTC received')\n"
         "ax.plot(idx.index, idx.btc_price, color=RED, ls='--', label='BTC price (real)')\n"
         "ax.set_title('Feb 2026: more BTC received, worth less in EUR (index Nov 2024 = 100)', loc='left', fontweight='bold')\n"
         "ax.legend(frameon=False); fig.tight_layout(); fig.savefig(ROOT / 'docs' / 'check_btc_feb2026.png', dpi=150)"),
    code("a = q(\"\"\"SELECT age_bucket, count(*) AS breaks, sum(amount_at_stake_eur) AS eur FROM breaks\n"
         "       WHERE status = 'open' GROUP BY 1 ORDER BY CASE age_bucket WHEN '0-2 d' THEN 1 WHEN '3-7 d' THEN 2\n"
         "       WHEN '8-30 d' THEN 3 ELSE 4 END\"\"\")\n"
         "fig, ax = plt.subplots(figsize=(6, 3))\n"
         "ax.bar(a.age_bucket, a.eur / 1e3, color=[BLUE, BLUE, BLUE, RED])\n"
         "ax.set_ylabel('Open amount (k EUR)')\n"
         "ax.set_title('Most of the open amount is older than 30 days', loc='left', fontweight='bold')\n"
         "fig.tight_layout(); fig.savefig(ROOT / 'docs' / 'check_break_ageing.png', dpi=150); a"),
    md("## 4. Files for Tableau\nWritten by `src/run_sql.py` to `data/output/` (CSV)."),
    code("pd.DataFrame([(f.name, sum(1 for _ in open(f, encoding='utf-8')) - 1) for f in sorted((ROOT / 'data' / 'output').glob('*.csv'))],\n"
         "             columns=['file', 'rows'])"),
]
path = ROOT / "notebooks" / "checks.ipynb"
ExecutePreprocessor(timeout=300, kernel_name="python3").preprocess(nb, {"metadata": {"path": str(ROOT / "notebooks")}})
nbf.write(nb, path)
print("written", path)
