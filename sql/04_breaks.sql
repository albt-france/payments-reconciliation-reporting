-- =============================================================================
-- 04_breaks.sql : one table of every reconciliation break (base of dashboard 4)
--   break_type, source pair, record, amount in EUR, date, status, age
-- A break is RESOLVED when the ledger shows the fix:
--   under/over payment -> a refund entry           late / wrong network / reuse -> a (manual) deposit
--   duplicate / premature credit -> a reversal      rate gap -> an fx_adjustment
--   missing partner fee -> the fee booked late      timezone cut-off -> cleared by the UTC conversion
-- =============================================================================
CREATE OR REPLACE TABLE breaks AS
WITH
refunds   AS (SELECT ref AS invoice_id, min(booked_ts) AS ts FROM ledger_clean WHERE entry_type = 'refund'        GROUP BY 1),
credits   AS (SELECT tx_hash,           min(booked_ts) AS ts FROM ledger_clean WHERE entry_type = 'deposit'       GROUP BY 1),
reversals AS (SELECT ref AS entry_id,   min(booked_ts) AS ts FROM ledger_clean WHERE entry_type = 'reversal'      GROUP BY 1),
fx_adj    AS (SELECT ref,               min(booked_ts) AS ts FROM ledger_clean WHERE entry_type = 'fx_adjustment' GROUP BY 1),

b_payment AS (   -- under / over payments: one row per invoice
    SELECT payment_class AS break_type, 'Invoices vs Chain' AS source_pair, invoice_id AS record_id,
           merchant_id, currency, network,
           abs(paid_in_window - amount_crypto) * rate_used AS amount_eur,
           first_seen_ts AS break_ts, r.ts AS resolved_ts
    FROM m_invoices
    LEFT JOIN refunds r USING (invoice_id)
    WHERE payment_class IN ('underpayment', 'overpayment')
),
b_chain AS (     -- money on-chain that no invoice explains (or arrived too late)
    SELECT match_class, 'Invoices vs Chain', tx_hash, merchant_id, currency, network,
           amount_eur, seen_ts, c.ts
    FROM m_invoice_chain
    LEFT JOIN credits c USING (tx_hash)
    WHERE match_class IN ('late_payment', 'wrong_network', 'address_reuse', 'unknown_address')
),
b_ledger AS (    -- duplicates and premature credits
    SELECT match_class, 'Ledger vs Chain', entry_id, merchant_id, currency, network,
           amount_eur, booked_ts, r.ts
    FROM m_ledger_chain
    LEFT JOIN reversals r USING (entry_id)
    WHERE match_class <> 'matched'
),
b_rate AS (      -- conversion executed far from the rate the merchant was given
    SELECT 'rate_gap', 'Ledger vs Partner', ref, merchant_id, currency, NULL,
           fx_diff_eur, booked_ts, f.ts
    FROM m_ledger_partner
    LEFT JOIN fx_adj f USING (ref)
    WHERE match_class = 'rate_gap'
),
b_other_partner AS (
    SELECT match_class, 'Ledger vs Partner', ref, merchant_id, currency, NULL,
           coalesce(partner_eur, ledger_eur), coalesce(booked_ts, value_ts_utc), NULL::TIMESTAMP
    FROM m_ledger_partner
    WHERE match_class IN ('missing_in_ledger', 'missing_at_partner')
),
b_fee AS (       -- partner fee missing in the ledger at close (booked late, or still missing)
    SELECT 'missing_partner_fee', 'Ledger vs Partner', f.ref, lp.merchant_id, lp.currency, NULL,
           f.fees_eur, f.value_ts_utc, CASE WHEN f.match_class = 'booked_late' THEN f.booked_ts END
    FROM m_partner_fees f
    LEFT JOIN m_ledger_partner lp USING (ref)
    WHERE f.match_class IN ('missing_partner_fee', 'booked_late')
),
b_tz AS (        -- looks like a month-end break in local time, disappears in UTC
    SELECT 'timezone_cutoff', 'Ledger vs Partner', ref, merchant_id, currency, NULL,
           partner_eur, booked_ts, booked_ts
    FROM m_ledger_partner
    WHERE month_cutoff_naive AND NOT month_cutoff_after_utc
),
b_payout AS (
    SELECT match_class, 'Ledger vs Payouts', payout_id, merchant_id, NULL, NULL,
           coalesce(payout_amount, ledger_amount), NULL::TIMESTAMP, NULL::TIMESTAMP
    FROM m_payouts
    WHERE match_class NOT IN ('matched', 'failed_not_booked')
),
all_breaks AS (
    SELECT * FROM b_payment
    UNION ALL SELECT * FROM b_chain
    UNION ALL SELECT * FROM b_ledger
    UNION ALL SELECT * FROM b_rate
    UNION ALL SELECT * FROM b_other_partner
    UNION ALL SELECT * FROM b_fee
    UNION ALL SELECT * FROM b_tz
    UNION ALL SELECT * FROM b_payout
)
SELECT
    'BRK' || lpad(CAST(ROW_NUMBER() OVER (ORDER BY break_ts, record_id) AS VARCHAR), 6, '0') AS break_id,
    break_type,
    source_pair,
    record_id,
    merchant_id,
    currency,
    network,
    round(amount_eur, 2)                    AS amount_eur,
    round(abs(amount_eur), 2)               AS amount_at_stake_eur,
    break_ts,
    CAST(break_ts AS DATE)                  AS break_date,
    resolved_ts,
    CASE
        WHEN break_type = 'timezone_cutoff'                                     THEN 'auto_cleared'
        WHEN resolved_ts IS NOT NULL AND resolved_ts < getvariable('snapshot_ts') THEN 'resolved'
        ELSE 'open'
    END AS status,
    -- open: age today ; resolved: days it took to fix
    date_diff('day', break_ts, coalesce(resolved_ts, getvariable('snapshot_ts'))) AS age_days
FROM all_breaks;

ALTER TABLE breaks ADD COLUMN age_bucket VARCHAR;
UPDATE breaks SET age_bucket = CASE
    WHEN status <> 'open' THEN NULL
    WHEN age_days <= 2  THEN '0-2 d'
    WHEN age_days <= 7  THEN '3-7 d'
    WHEN age_days <= 30 THEN '8-30 d'
    ELSE '> 30 d' END;

-- Daily match rate per source pair (how much of each day reconciles automatically)
CREATE OR REPLACE TABLE recon_daily AS
SELECT CAST(seen_ts AS DATE) AS d, 'Invoices vs Chain' AS source_pair,
       count(*) AS records, count(*) FILTER (WHERE match_class = 'exact') AS matched,
       sum(amount_eur) AS amount_eur, sum(amount_eur) FILTER (WHERE match_class = 'exact') AS matched_eur
FROM m_invoice_chain GROUP BY ALL
UNION ALL
SELECT CAST(booked_ts AS DATE), 'Ledger vs Chain',
       count(*), count(*) FILTER (WHERE match_class = 'matched'),
       sum(amount_eur), sum(amount_eur) FILTER (WHERE match_class = 'matched')
FROM m_ledger_chain GROUP BY ALL
UNION ALL
SELECT CAST(coalesce(booked_ts, value_ts_utc) AS DATE), 'Ledger vs Partner',
       count(*), count(*) FILTER (WHERE match_class = 'matched'),
       sum(coalesce(partner_eur, ledger_eur)), sum(partner_eur) FILTER (WHERE match_class = 'matched')
FROM m_ledger_partner GROUP BY ALL
ORDER BY 1, 2;
