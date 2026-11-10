-- =============================================================================
-- 05_kpis_monthly.sql : management KPIs built on RECONCILED data
--
-- Definitions (EUR)
--   Processed volume = valid deposits (duplicates and reversed premature credits removed)
--   Revenue          = deposit fees + conversion fees + payout fees charged to merchants
--   Costs            = network costs (address sweeps + crypto payouts)
--                      + partner fees (per the PARTNER statement, not the ledger: the ledger misses some)
--                      - FX result (partner EUR received - EUR given to the merchant at the invoice rate)
--   Gross margin     = Revenue - network costs - partner fees + FX result
--   Take rate        = Revenue / Processed volume
-- =============================================================================

-- Valid deposits, with network and day ------------------------------------------
CREATE OR REPLACE TABLE deposits_valid AS
SELECT
    l.entry_id, l.tx_hash, l.invoice_id, l.merchant_id, l.currency, c.network,
    l.amount AS amount_crypto, l.amount_eur, l.booked_ts,
    CAST(l.booked_ts AS DATE) AS d,
    l.amount_eur * p.eurusd_ecb AS amount_usd
FROM ledger_clean l
JOIN chain_clean c USING (tx_hash)
LEFT JOIN price_eur p ON p.d = CAST(l.booked_ts AS DATE) AND p.currency = l.currency
WHERE l.entry_type = 'deposit'
  AND NOT l.is_duplicate
  AND l.entry_id NOT IN (SELECT ref FROM ledger_clean WHERE entry_type = 'reversal');

-- Network cost of a day is shared between that day's deposits on that network (by count)
CREATE OR REPLACE TABLE network_cost_daily AS
SELECT CAST(booked_ts AS DATE) AS d, split_part(ref, '-', 3) AS network, sum(amount_eur) AS cost_eur
FROM ledger_clean WHERE entry_type = 'network_cost' GROUP BY ALL;

CREATE OR REPLACE TABLE network_cost_alloc AS
WITH cnt AS (SELECT d, network, count(*) AS n FROM deposits_valid GROUP BY ALL)
SELECT dv.entry_id, c.cost_eur / cnt.n AS network_cost_eur
FROM deposits_valid dv
JOIN cnt USING (d, network)
JOIN network_cost_daily c USING (d, network);

-- One row per revenue/cost event, at the grain day x merchant x currency x network --------
CREATE OR REPLACE TABLE pnl_events AS
-- deposits (volume) and their allocated network cost
SELECT dv.d, dv.merchant_id, dv.currency, dv.network,
       1 AS deposits_n, dv.amount_crypto AS volume_crypto, dv.amount_eur AS volume_eur, dv.amount_usd AS volume_usd,
       0.0 AS fee_deposit_eur, 0.0 AS fee_conversion_eur, 0.0 AS fee_payout_eur,
       coalesce(nc.network_cost_eur, 0) AS network_cost_eur, 0.0 AS partner_fee_eur, 0.0 AS fx_result_eur,
       0.0 AS converted_eur
FROM deposits_valid dv
LEFT JOIN network_cost_alloc nc USING (entry_id)
UNION ALL
-- network costs on days without any deposit on that network: kept, on an 'UNALLOCATED' line
SELECT c.d, 'UNALLOCATED', 'n/a', c.network, 0, 0, 0, 0, 0, 0, 0, c.cost_eur, 0, 0, 0
FROM network_cost_daily c
WHERE NOT EXISTS (SELECT 1 FROM deposits_valid dv WHERE dv.d = c.d AND dv.network = c.network)
UNION ALL
-- deposit and conversion fees (linked to the deposit by tx_hash for currency / network)
SELECT CAST(f.booked_ts AS DATE), f.merchant_id, c.currency, c.network,
       0, 0, 0, 0,
       CASE WHEN f.fee_type = 'deposit'    THEN f.amount_eur ELSE 0 END,
       CASE WHEN f.fee_type = 'conversion' THEN f.amount_eur ELSE 0 END,
       0, 0, 0, 0, 0
FROM ledger_clean f
JOIN chain_clean c USING (tx_hash)
WHERE f.entry_type = 'fee' AND f.fee_type IN ('deposit', 'conversion')
UNION ALL
-- payout fees (no network: attached to the payout currency)
SELECT CAST(booked_ts AS DATE), merchant_id, currency, 'PAYOUT',
       0, 0, 0, 0, 0, 0, amount_eur, 0, 0, 0, 0
FROM ledger_clean
WHERE entry_type = 'fee' AND fee_type = 'payout'
  AND ref IN (SELECT payout_id FROM payouts_clean WHERE status = 'completed')
UNION ALL
-- conversions: partner fee and FX result, from the reconciled ledger-partner match
SELECT CAST(lp.booked_ts AS DATE), lp.merchant_id, lp.currency, c.network,
       0, 0, 0, 0, 0, 0, 0, 0, lp.partner_fees_eur, lp.fx_diff_eur, lp.ledger_eur
FROM m_ledger_partner lp
JOIN chain_clean c USING (tx_hash)
WHERE lp.match_class IN ('matched', 'rate_gap');

-- Monthly fact table for Tableau (month x merchant x currency x network) -----------------
CREATE OR REPLACE TABLE fact_monthly AS
WITH inv AS (
    SELECT date_trunc('month', created_ts)::DATE AS month, merchant_id, currency, network,
           count(*) AS invoices_created,
           count(*) FILTER (WHERE payment_class IN ('exact', 'underpayment', 'overpayment', 'late_payment')) AS invoices_paid,
           count(*) FILTER (WHERE payment_class = 'underpayment') AS invoices_underpaid
    FROM m_invoices GROUP BY ALL
),
pnl AS (
    SELECT date_trunc('month', d)::DATE AS month, merchant_id, currency, network,
           sum(deposits_n) AS deposits_n, sum(volume_crypto) AS volume_crypto,
           sum(volume_eur) AS volume_eur, sum(volume_usd) AS volume_usd,
           sum(fee_deposit_eur) AS fee_deposit_eur, sum(fee_conversion_eur) AS fee_conversion_eur,
           sum(fee_payout_eur) AS fee_payout_eur, sum(network_cost_eur) AS network_cost_eur,
           sum(partner_fee_eur) AS partner_fee_eur, sum(fx_result_eur) AS fx_result_eur,
           sum(converted_eur) AS converted_eur
    FROM pnl_events
    WHERE d <= DATE '2026-10-31'
    GROUP BY ALL
)
SELECT
    month, merchant_id, currency, network,
    coalesce(inv.invoices_created, 0)  AS invoices_created,
    coalesce(inv.invoices_paid, 0)     AS invoices_paid,
    coalesce(inv.invoices_underpaid, 0) AS invoices_underpaid,
    coalesce(deposits_n, 0)            AS deposits_n,
    coalesce(volume_crypto, 0)         AS volume_crypto,
    coalesce(volume_eur, 0)  AS volume_eur,
    coalesce(volume_usd, 0)  AS volume_usd,
    coalesce(fee_deposit_eur, 0)    AS fee_deposit_eur,
    coalesce(fee_conversion_eur, 0) AS fee_conversion_eur,
    coalesce(fee_payout_eur, 0)     AS fee_payout_eur,
    coalesce(fee_deposit_eur + fee_conversion_eur + fee_payout_eur, 0) AS revenue_eur,
    coalesce(network_cost_eur, 0)   AS network_cost_eur,
    coalesce(partner_fee_eur, 0)    AS partner_fee_eur,
    coalesce(fx_result_eur, 0)      AS fx_result_eur,
    coalesce(fee_deposit_eur + fee_conversion_eur + fee_payout_eur
                   - network_cost_eur - partner_fee_eur + fx_result_eur, 0) AS gross_margin_eur,
    coalesce(converted_eur, 0)      AS converted_eur
FROM pnl
FULL JOIN inv USING (month, merchant_id, currency, network)
WHERE month <= DATE '2026-10-01';

-- Company-level monthly KPIs, with MoM and YoY growth --------------------------------------
CREATE OR REPLACE TABLE kpis_monthly AS
WITH m AS (
    SELECT
        month,
        sum(volume_eur)        AS volume_eur,
        sum(volume_usd)        AS volume_usd,
        sum(deposits_n)        AS transactions,
        sum(invoices_created)  AS invoices_created,
        sum(fee_deposit_eur)   AS fee_deposit_eur,
        sum(fee_conversion_eur) AS fee_conversion_eur,
        sum(fee_payout_eur)    AS fee_payout_eur,
        sum(revenue_eur)       AS revenue_eur,
        sum(network_cost_eur)  AS network_cost_eur,
        sum(partner_fee_eur)   AS partner_fee_eur,
        sum(fx_result_eur)     AS fx_result_eur,
        sum(gross_margin_eur)  AS gross_margin_eur,
        count(DISTINCT merchant_id) FILTER (WHERE deposits_n > 0) AS active_merchants,
        sum(volume_eur) FILTER (WHERE currency IN ('USDT', 'USDC')) / sum(volume_eur) AS stablecoin_share
    FROM fact_monthly
    GROUP BY month
)
SELECT
    m.*,
    revenue_eur / volume_eur          AS take_rate,
    gross_margin_eur / revenue_eur    AS gross_margin_pct,
    revenue_eur / lag(revenue_eur)  OVER (ORDER BY month) - 1  AS revenue_mom,
    revenue_eur / lag(revenue_eur, 12) OVER (ORDER BY month) - 1 AS revenue_yoy,
    volume_eur  / lag(volume_eur)   OVER (ORDER BY month) - 1  AS volume_mom,
    volume_eur  / lag(volume_eur, 12) OVER (ORDER BY month) - 1 AS volume_yoy,
    transactions / lag(transactions) OVER (ORDER BY month) - 1 AS transactions_mom
FROM m
ORDER BY month;

-- Daily x currency x network (dashboard 5, related to the REAL daily prices) ---------------
CREATE OR REPLACE TABLE fact_daily_currency AS
WITH dep AS (
    SELECT d, currency, network, count(*) AS transactions, sum(amount_crypto) AS volume_crypto,
           sum(amount_eur) AS volume_eur
    FROM deposits_valid WHERE d <= DATE '2026-10-31' GROUP BY ALL
),
rev AS (
    SELECT d, currency, network, sum(fee_deposit_eur + fee_conversion_eur) AS revenue_eur,
           sum(fx_result_eur) AS fx_result_eur
    FROM pnl_events WHERE network <> 'PAYOUT' AND d <= DATE '2026-10-31' GROUP BY ALL
),
inv AS (
    SELECT CAST(created_ts AS DATE) AS d, currency, network,
           count(*) FILTER (WHERE payment_class IN ('exact', 'underpayment', 'overpayment')) AS invoices_paid,
           count(*) FILTER (WHERE payment_class = 'underpayment') AS invoices_underpaid,
           -- minutes from invoice creation to the first on-chain sighting
           avg(date_diff('second', created_ts, first_seen_ts) / 60.0)
               FILTER (WHERE payment_class = 'exact') AS avg_minutes_to_pay
    FROM m_invoices GROUP BY ALL
),
conf AS (   -- minutes from invoice creation to the merchant being credited (confirmations included)
    SELECT dv.d, dv.currency, dv.network,
           avg(date_diff('second', i.created_ts, dv.booked_ts) / 60.0) AS avg_minutes_to_credit
    FROM deposits_valid dv JOIN m_invoices i USING (invoice_id)
    WHERE i.payment_class = 'exact' GROUP BY ALL
),
fee AS (    -- network fee paid by customers, valued in EUR
    SELECT CAST(seen_ts AS DATE) AS d, c.currency, c.network, avg(c.network_fee * p.price_eur) AS avg_network_fee_eur
    FROM chain_clean c JOIN price_eur p ON p.d = CAST(c.seen_ts AS DATE) AND p.currency = c.currency
    GROUP BY ALL
)
SELECT
    dep.d AS date, dep.currency, dep.network,
    dep.transactions, dep.volume_crypto, round(dep.volume_eur, 2) AS volume_eur,
    round(rev.revenue_eur, 2) AS revenue_eur, round(rev.fx_result_eur, 2) AS fx_result_eur,
    inv.invoices_paid, inv.invoices_underpaid,
    round(inv.avg_minutes_to_pay, 2) AS avg_minutes_to_pay,
    round(conf.avg_minutes_to_credit, 2) AS avg_minutes_to_credit,
    round(fee.avg_network_fee_eur, 4) AS avg_network_fee_eur
FROM dep
LEFT JOIN rev  USING (d, currency, network)
LEFT JOIN inv  USING (d, currency, network)
LEFT JOIN conf USING (d, currency, network)
LEFT JOIN fee  USING (d, currency, network)
ORDER BY 1, 2, 3;
