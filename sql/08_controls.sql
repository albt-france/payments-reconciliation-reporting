-- =============================================================================
-- 08_controls.sql : control totals ("does everything tie out?")
--
-- Each control compares two sources:  difference = source_a - source_b
-- explained = the part of that difference covered by known, listed breaks / adjustments
-- residual  = difference - explained   -> must be 0.00, otherwise something is unexplained
-- =============================================================================
CREATE OR REPLACE TABLE controls AS
WITH
-- C1: confirmed on-chain receipts vs net deposits credited in the ledger (per coin, in coin units)
c1_chain AS (
    SELECT currency, sum(amount_crypto) AS a FROM chain_clean
    WHERE confirmations >= required_confirmations GROUP BY 1
),
c1_ledger AS (
    SELECT currency,
           sum(amount) FILTER (WHERE entry_type = 'deposit')
         - coalesce(sum(amount) FILTER (WHERE entry_type = 'reversal'), 0) AS b
    FROM ledger_clean WHERE entry_type IN ('deposit', 'reversal') GROUP BY 1
),
c1_expl AS (
    SELECT currency, sum(x) AS e FROM (
        -- + confirmed money on-chain that was never credited (open late / wrong network / reuse, in transit)
        SELECT currency, amount_crypto AS x FROM m_chain_not_credited
        WHERE confirmations >= required_confirmations
        UNION ALL
        -- - money credited twice, or credited before the chain confirmed it, and not reversed yet
        SELECT currency, -ledger_amount FROM m_ledger_chain
        WHERE match_class IN ('ledger_duplicate', 'premature_credit')
          AND entry_id NOT IN (SELECT ref FROM ledger_clean WHERE entry_type = 'reversal')
    ) GROUP BY 1
),
c1 AS (
    SELECT 'C1 Chain receipts vs ledger deposits - ' || c.currency AS control, c.currency AS unit,
           c.a AS source_a, l.b AS source_b, coalesce(e.e, 0) AS explained, 1e-6 AS tolerance
    FROM c1_chain c JOIN c1_ledger l USING (currency) LEFT JOIN c1_expl e USING (currency)
),
-- C2: EUR received from the partner vs EUR given to merchants (ledger conversions)
c2 AS (
    SELECT 'C2 Partner EUR vs ledger conversions' AS control, 'EUR' AS unit,
           (SELECT sum(amount_eur) FROM partner_clean) AS source_a,
           (SELECT sum(amount_eur) FROM ledger_clean WHERE entry_type = 'conversion') AS source_b,
           -- FX result within tolerance (auto) + rate gaps (fx_adjustment posted or still open)
           (SELECT sum(coalesce(partner_eur, 0) - coalesce(ledger_eur, 0)) FROM m_ledger_partner) AS explained,
           0.005 AS tolerance
),
c2b AS (
    SELECT 'C2b Rate gaps: posted fx_adjustments + open gaps' AS control, 'EUR' AS unit,
           (SELECT sum(fx_diff_eur) FROM m_ledger_partner WHERE match_class = 'rate_gap') AS source_a,
           (SELECT sum(amount_eur) FROM ledger_clean WHERE entry_type = 'fx_adjustment') AS source_b,
           (SELECT sum(amount_eur) FROM breaks WHERE break_type = 'rate_gap' AND status = 'open') AS explained,
           0.005 AS tolerance
),
-- C3: partner fees on the statement vs partner fees booked
c3 AS (
    SELECT 'C3 Partner fees: statement vs ledger' AS control, 'EUR' AS unit,
           (SELECT sum(fees_eur) FROM partner_clean) AS source_a,
           (SELECT sum(amount_eur) FROM ledger_clean WHERE entry_type = 'partner_fee') AS source_b,
           (SELECT sum(fees_eur) FROM m_partner_fees WHERE match_class = 'missing_partner_fee') AS explained,
           0.005 AS tolerance
),
-- C4: completed payouts vs payouts booked
c4 AS (
    SELECT 'C4 Payouts system vs ledger payouts' AS control, 'EUR' AS unit,
           (SELECT sum(amount_eur) FROM payouts_clean WHERE status = 'completed') AS source_a,
           (SELECT sum(amount_eur) FROM ledger_clean WHERE entry_type = 'payout') AS source_b,
           coalesce((SELECT sum(amount_eur) FROM breaks WHERE source_pair = 'Ledger vs Payouts'), 0) AS explained,
           0.005 AS tolerance
),
-- C5: October 2026 close: partner total by LOCAL date vs by UTC date
c5 AS (
    SELECT 'C5 Oct-2026 cut-off: partner local month vs UTC month' AS control, 'EUR' AS unit,
           (SELECT sum(amount_eur) FROM partner_clean
             WHERE date_trunc('month', value_date_local) = DATE '2026-10-01') AS source_a,
           (SELECT sum(amount_eur) FROM partner_clean
             WHERE date_trunc('month', value_ts_utc) = DATE '2026-10-01') AS source_b,
           -- items that the local clock moves INTO October minus those it pushes OUT to November
           (SELECT coalesce(sum(partner_eur) FILTER (WHERE date_trunc('month', value_date_local) = DATE '2026-10-01'), 0)
                 - coalesce(sum(partner_eur) FILTER (WHERE date_trunc('month', value_ts_utc) = DATE '2026-10-01'), 0)
              FROM m_ledger_partner WHERE month_cutoff_naive) AS explained,
           0.005 AS tolerance
),
-- C6: revenue reported in the KPI table vs fee entries in the ledger (completeness of the build)
c6 AS (
    SELECT 'C6 KPI revenue vs ledger fee entries' AS control, 'EUR' AS unit,
           (SELECT sum(revenue_eur) FROM fact_monthly) AS source_a,
           (SELECT sum(amount_eur) FROM ledger_clean
             WHERE entry_type = 'fee' AND booked_ts < TIMESTAMP '2026-11-01') AS source_b,
           0.0 AS explained,
           0.005 AS tolerance
),
-- C7: network costs allocated to merchants vs booked
c7 AS (
    SELECT 'C7 Network costs allocated vs booked' AS control, 'EUR' AS unit,
           (SELECT sum(network_cost_eur) FROM fact_monthly) AS source_a,
           (SELECT sum(amount_eur) FROM ledger_clean
             WHERE entry_type = 'network_cost' AND booked_ts < TIMESTAMP '2026-11-01') AS source_b,
           0.0 AS explained,
           0.005 AS tolerance
),
-- C8: revenue bridge effects add up to the revenue change, every month
c8 AS (
    SELECT 'C8 Revenue bridge effects = revenue change' AS control, 'EUR' AS unit,
           sum(value) FILTER (WHERE step_order IN (2, 3, 4)) AS source_a,
           sum(value) FILTER (WHERE step_order = 5) - sum(value) FILTER (WHERE step_order = 1) AS source_b,
           0.0 AS explained,
           0.005 AS tolerance
    FROM revenue_bridge
),
all_c AS (
    SELECT * FROM c1 UNION ALL SELECT * FROM c2 UNION ALL SELECT * FROM c2b UNION ALL SELECT * FROM c3
    UNION ALL SELECT * FROM c4 UNION ALL SELECT * FROM c5 UNION ALL SELECT * FROM c6
    UNION ALL SELECT * FROM c7 UNION ALL SELECT * FROM c8
)
SELECT
    control, unit,
    round(source_a, 6) AS source_a,
    round(source_b, 6) AS source_b,
    round(source_a - source_b, 6) AS difference,
    round(explained, 6) AS explained,
    round(source_a - source_b - explained, 6) AS residual,
    CASE WHEN abs(source_a - source_b - explained) <= tolerance THEN 'PASS' ELSE 'FAIL' END AS result
FROM all_c
ORDER BY control;
