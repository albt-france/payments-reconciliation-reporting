"""
Run the reconciliation pipeline (sql/01..09) in DuckDB and export the outputs for Tableau and Excel.

    python src/run_sql.py

- Reads the six SIMULATED sources from data/clean/*.parquet and the REAL prices CSV.
- Never reads planted_issues except in 09_validation.sql (the answer key, used only to score the pipeline).
- Writes data/recon.duckdb (all tables) and data/output/*.csv (the tables Tableau and Excel use).
"""
import time
from pathlib import Path

import duckdb

ROOT = Path(__file__).resolve().parents[1]
CLEAN = ROOT / "data" / "clean"
OUTPUT = ROOT / "data" / "output"
SQL = ROOT / "sql"

# Parameters (also exposed as Tableau / Excel parameters)
PARAMS = {
    "snapshot_ts": "TIMESTAMP '2026-11-01 06:00:00'",  # when all sources were extracted (UTC)
    "amount_tol_pct": 0.005,     # invoice vs on-chain amount: differences under 0.5 % are rounding
    "fx_tol_pct": 0.02,          # ledger vs partner EUR amount: up to 2 % is normal FX slippage
    "match_window_days": 7,      # how long after invoice creation a payment can still be matched
    "partner_tz": "'Europe/Tallinn'",
}

# Tables exported for Tableau / Excel
EXPORTS = ["fact_monthly", "fact_daily_currency", "kpis_monthly", "breaks", "recon_daily", "cohorts",
           "revenue_bridge", "controls", "validation", "dim_merchants", "prices_daily"]


def main() -> None:
    db_path = ROOT / "data" / "recon.duckdb"
    db_path.unlink(missing_ok=True)
    con = duckdb.connect(str(db_path))
    con.execute("SET TimeZone = 'UTC'")
    for k, v in PARAMS.items():
        con.execute(f"SET VARIABLE {k} = {v}")

    # raw sources = exactly what each system gave us
    for t in ["merchants", "invoices", "chain_tx", "ledger", "partner_statements", "payouts", "planted_issues"]:
        con.execute(f"CREATE VIEW raw_{t} AS SELECT * FROM read_parquet('{(CLEAN / f'{t}.parquet').as_posix()}')")
    prices_csv = (ROOT / "data" / "raw" / "prices" / "prices_daily.csv").as_posix()
    con.execute(f"CREATE TABLE prices_daily AS SELECT * FROM read_csv('{prices_csv}', header=true)")

    for f in sorted(SQL.glob("*.sql")):
        t0 = time.time()
        con.execute(f.read_text(encoding="utf-8"))
        print(f"{f.name:32s} {time.time() - t0:5.1f}s")

    OUTPUT.mkdir(parents=True, exist_ok=True)
    for t in EXPORTS:
        # tidy CSVs for Tableau: decimals rounded (6 dp), timestamps to the second.
        # (tables in the database keep full precision, so the controls are not affected)
        cols = []
        for col, typ in con.execute(f"SELECT column_name, data_type FROM information_schema.columns "
                                    f"WHERE table_name = '{t}' ORDER BY ordinal_position").fetchall():
            if typ in ("DOUBLE", "FLOAT") or typ.startswith("DECIMAL"):
                cols.append(f'round("{col}", 6) AS "{col}"')
            elif typ.startswith("TIMESTAMP"):
                cols.append(f'CAST(date_trunc(\'second\', "{col}") AS TIMESTAMP) AS "{col}"')
            else:
                cols.append(f'"{col}"')
        con.execute(f"COPY (SELECT {', '.join(cols)} FROM {t}) TO '{(OUTPUT / f'{t}.csv').as_posix()}' "
                    f"(HEADER, DELIMITER ',', TIMESTAMPFORMAT '%Y-%m-%d %H:%M:%S')").fetchall()
        n = con.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
        print(f"  exported {t:22s} {n:>9,d} rows")

    print("\nCONTROLS")
    print(con.execute("SELECT control, unit, source_a, source_b, explained, residual, result FROM controls").df().to_string(index=False))
    print("\nVALIDATION vs planted issues")
    print(con.execute("SELECT * FROM validation").df().to_string(index=False))
    con.close()


if __name__ == "__main__":
    main()
