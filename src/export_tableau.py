"""
Export the Tableau-ready CSVs (data/output/tableau/) from data/recon.duckdb.
One file per Tableau data source; merchant attributes and real prices are already joined in,
so each data source is a single, simple table.

    python src/export_tableau.py
"""
import json
from pathlib import Path

import duckdb

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "data" / "output" / "tableau"

QUERIES = {
    # month x merchant x coin x network, with merchant attributes
    "business": """
        SELECT f.month, f.merchant_id, coalesce(d.segment, 'Unallocated') AS segment, d.country,
               CAST(d.signup_date AS DATE) AS signup_date, d.account_manager, f.currency, f.network,
               CASE WHEN f.currency IN ('USDT', 'USDC') THEN 'Stablecoin' ELSE 'Volatile' END AS coin_type,
               f.invoices_created, f.invoices_paid, f.invoices_underpaid, f.deposits_n,
               round(f.volume_crypto, 8) AS volume_crypto, round(f.volume_eur, 2) AS volume_eur,
               round(f.volume_usd, 2) AS volume_usd, round(f.fee_deposit_eur, 2) AS fee_deposit_eur,
               round(f.fee_conversion_eur, 2) AS fee_conversion_eur, round(f.fee_payout_eur, 2) AS fee_payout_eur,
               round(f.revenue_eur, 2) AS revenue_eur, round(f.network_cost_eur, 2) AS network_cost_eur,
               round(f.partner_fee_eur, 2) AS partner_fee_eur, round(f.fx_result_eur, 2) AS fx_result_eur,
               round(f.gross_margin_eur, 2) AS gross_margin_eur
        FROM fact_monthly f LEFT JOIN dim_merchants d USING (merchant_id)
        ORDER BY f.month, f.merchant_id, f.currency, f.network""",
    # one row per reconciliation break
    "breaks": """
        SELECT b.break_id, b.break_type, b.source_pair, b.record_id, b.merchant_id,
               coalesce(d.segment, 'Unknown') AS segment, b.currency, b.network,
               round(b.amount_eur, 2) AS amount_eur, round(b.amount_at_stake_eur, 2) AS amount_at_stake_eur,
               CAST(date_trunc('second', b.break_ts) AS TIMESTAMP) AS break_ts, b.break_date,
               CAST(date_trunc('second', b.resolved_ts) AS TIMESTAMP) AS resolved_ts,
               b.status, b.age_days, coalesce(b.age_bucket, 'n/a') AS age_bucket
        FROM breaks b LEFT JOIN dim_merchants d USING (merchant_id)
        ORDER BY b.break_ts""",
    "recon_daily": """
        SELECT d AS date, source_pair, records, matched, round(amount_eur, 2) AS amount_eur,
               round(matched_eur, 2) AS matched_eur
        FROM recon_daily WHERE d <= DATE '2026-10-31' ORDER BY 1, 2""",
    # day x coin x network, with the REAL daily price of the coin
    "crypto_daily": """
        SELECT f.date, f.currency, f.network,
               CASE WHEN f.currency IN ('USDT', 'USDC') THEN 'Stablecoin' ELSE 'Volatile' END AS coin_type,
               f.transactions, round(f.volume_crypto, 8) AS volume_crypto, f.volume_eur, f.revenue_eur,
               f.fx_result_eur, f.invoices_paid, f.invoices_underpaid, f.avg_minutes_to_pay,
               f.avg_minutes_to_credit, f.avg_network_fee_eur,
               round(p.price_eur, 6) AS price_eur, round(p.price_usd, 6) AS price_usd
        FROM fact_daily_currency f
        LEFT JOIN price_eur p ON p.d = f.date AND p.currency = f.currency
        ORDER BY 1, 2, 3""",
    "bridge": """
        SELECT month, previous_month, step_order, step,
               CASE step_order WHEN 1 THEN '1 Last month' WHEN 2 THEN '2 Volume'
                    WHEN 3 THEN '3 Mix' WHEN 4 THEN '4 Take rate' ELSE '5 This month' END AS step_label,
               segment, round(value, 2) AS value
        FROM revenue_bridge ORDER BY month, step_order, segment""",
    # P&L lines in long format (one row per line), for stacked revenue / cost charts
    "pnl_long": """
        WITH f AS (
            SELECT f.*, coalesce(d.segment, 'Unallocated') AS segment
            FROM fact_monthly f LEFT JOIN dim_merchants d USING (merchant_id)
        ),
        rate AS (SELECT month, sum(volume_usd) / sum(volume_eur) AS usd_per_eur FROM fact_monthly GROUP BY 1),
        long AS (
            SELECT month, segment, currency, network, 'Deposit fees' AS line, 'Revenue' AS kind, fee_deposit_eur AS amount FROM f
            UNION ALL SELECT month, segment, currency, network, 'Conversion fees', 'Revenue', fee_conversion_eur FROM f
            UNION ALL SELECT month, segment, currency, network, 'Payout fees', 'Revenue', fee_payout_eur FROM f
            UNION ALL SELECT month, segment, currency, network, 'Network costs', 'Cost', network_cost_eur FROM f
            UNION ALL SELECT month, segment, currency, network, 'Partner fees', 'Cost', partner_fee_eur FROM f
            UNION ALL SELECT month, segment, currency, network, 'FX loss', 'Cost', -fx_result_eur FROM f
        )
        SELECT l.month, l.segment, l.currency, l.network, l.line, l.kind,
               round(sum(l.amount), 2) AS amount_eur, round(any_value(r.usd_per_eur), 6) AS usd_per_eur
        FROM long l JOIN rate r USING (month)
        GROUP BY ALL ORDER BY 1, 2, 3, 4, 5""",
    "kpis": "SELECT * FROM kpis_monthly ORDER BY month",
}


def tableau_type(duck_type: str) -> str:
    t = duck_type.upper()
    if t == "DATE":
        return "date"
    if t.startswith("TIMESTAMP"):
        return "datetime"
    if t in ("BIGINT", "INTEGER", "HUGEINT", "SMALLINT", "TINYINT", "UBIGINT"):
        return "integer"
    if t in ("DOUBLE", "FLOAT", "REAL") or t.startswith("DECIMAL"):
        return "real"
    if t == "BOOLEAN":
        return "boolean"
    return "string"


def write_hyper(csv_path: Path, hyper_path: Path, schema: list) -> None:
    """Tableau Public only saves workbooks built on extracts: write each CSV into a .hyper extract
    (table "Extract"."Extract", the name Tableau expects)."""
    from tableauhyperapi import (Connection, CreateMode, HyperProcess, SqlType, TableDefinition,
                                 TableName, Telemetry, escape_string_literal)
    sql_type = {"date": SqlType.date(), "datetime": SqlType.timestamp(), "integer": SqlType.big_int(),
                "real": SqlType.double(), "boolean": SqlType.bool(), "string": SqlType.text()}
    table = TableDefinition(TableName("Extract", "Extract"),
                            [TableDefinition.Column(c, sql_type[t]) for c, t in schema])
    with HyperProcess(telemetry=Telemetry.DO_NOT_SEND_USAGE_DATA_TO_TABLEAU) as hyper:
        with Connection(hyper.endpoint, str(hyper_path), CreateMode.CREATE_AND_REPLACE) as conn:
            conn.catalog.create_schema("Extract")
            conn.catalog.create_table(table)
            conn.execute_command(f"COPY {table.table_name} FROM {escape_string_literal(str(csv_path))} "
                                 "WITH (format csv, NULL '', delimiter ',', header)")


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(ROOT / "data" / "recon.duckdb"), read_only=True)
    schemas = {}
    for name, sql in QUERIES.items():
        path = OUT / f"{name}.csv"
        con.execute(f"COPY ({sql}) TO '{path.as_posix()}' (HEADER, DELIMITER ',', TIMESTAMPFORMAT '%Y-%m-%d %H:%M:%S')").fetchall()
        n = con.execute(f"SELECT count(*) FROM ({sql})").fetchone()[0]
        schema = [(c, tableau_type(t)) for c, t, *_ in con.execute(f"DESCRIBE ({sql})").fetchall()]
        schemas[name] = schema
        write_hyper(path, OUT / f"{name}.hyper", schema)
        print(f"{name:14s} {n:>8,d} rows  -> {name}.csv + {name}.hyper")
    con.close()
    (OUT / "schemas.json").write_text(json.dumps(schemas, indent=1))


if __name__ == "__main__":
    main()
