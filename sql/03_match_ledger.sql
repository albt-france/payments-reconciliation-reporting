-- =============================================================================
-- 03_match_ledger.sql : the internal ledger against the outside world
--   a) ledger deposits      <-> on-chain transfers   (key: tx_hash)
--   b) ledger conversions   <-> partner statements   (key: ref)
--   c) ledger partner fees  <-> partner statements   (key: ref)
--   d) ledger payouts       <-> payouts system       (key: payout_id)
-- =============================================================================

-- a) Deposits vs chain ---------------------------------------------------------
CREATE OR REPLACE TABLE m_ledger_chain AS
SELECT
    l.entry_id,
    l.tx_hash,
    l.invoice_id,
    l.merchant_id,
    l.currency,
    l.amount       AS ledger_amount,
    l.amount_eur,
    l.booked_ts,
    l.is_duplicate,
    c.amount_crypto AS chain_amount,
    c.network,
    c.confirmations,
    c.required_confirmations,
    CASE
        WHEN c.tx_hash IS NULL                              THEN 'not_on_chain'
        WHEN l.is_duplicate                                 THEN 'ledger_duplicate'
        -- internal system says "credited", the chain does not have enough confirmations
        WHEN c.confirmations < c.required_confirmations     THEN 'premature_credit'
        WHEN abs(l.amount - c.amount_crypto) > 1e-8         THEN 'amount_mismatch'
        ELSE 'matched'
    END AS match_class
FROM ledger_clean l
LEFT JOIN chain_clean c USING (tx_hash)
WHERE l.entry_type = 'deposit';

-- On-chain transfers that the ledger never credited
CREATE OR REPLACE TABLE m_chain_not_credited AS
SELECT m.*
FROM m_invoice_chain m
WHERE m.tx_hash NOT IN (SELECT tx_hash FROM ledger_clean WHERE entry_type = 'deposit' AND tx_hash IS NOT NULL);

-- b) Conversions vs partner statement --------------------------------------------
CREATE OR REPLACE TABLE m_ledger_partner AS
SELECT
    coalesce(l.ref, p.ref)   AS ref,
    l.entry_id,
    l.merchant_id,
    l.tx_hash,
    l.currency,
    l.amount                 AS ledger_crypto,
    l.amount_eur             AS ledger_eur,
    l.booked_ts,
    p.stmt_id,
    p.amount_eur             AS partner_eur,
    p.fees_eur               AS partner_fees_eur,
    p.value_ts_local,
    p.value_date_local,
    p.value_ts_utc,
    p.amount_eur - l.amount_eur AS fx_diff_eur,           -- + = FX gain for us, - = FX loss
    CASE
        WHEN l.ref IS NULL THEN 'missing_in_ledger'
        WHEN p.ref IS NULL THEN 'missing_at_partner'
        WHEN abs(p.amount_eur - l.amount_eur) > getvariable('fx_tol_pct') * l.amount_eur THEN 'rate_gap'
        ELSE 'matched'
    END AS match_class,
    -- month-end cut-off: a naive close on the partner's LOCAL date puts it in another month
    date_trunc('month', l.booked_ts) <> date_trunc('month', p.value_date_local) AS month_cutoff_naive,
    date_trunc('month', l.booked_ts) <> date_trunc('month', p.value_ts_utc)     AS month_cutoff_after_utc,
    CAST(l.booked_ts AS DATE) <> p.value_date_local                              AS date_shift_naive
FROM (SELECT * FROM ledger_clean WHERE entry_type = 'conversion') l
FULL JOIN partner_clean p ON l.ref = p.ref;

-- c) Partner fees ----------------------------------------------------------------
CREATE OR REPLACE TABLE m_partner_fees AS
SELECT
    p.ref,
    p.fees_eur,
    p.value_ts_utc,
    f.entry_id,
    f.amount_eur AS ledger_fee_eur,
    f.booked_ts,
    CASE
        WHEN f.ref IS NULL                                     THEN 'missing_partner_fee'
        WHEN f.booked_ts > p.value_ts_utc + INTERVAL 1 DAY     THEN 'booked_late'
        WHEN abs(f.amount_eur - p.fees_eur) > 0.005            THEN 'fee_amount_mismatch'
        ELSE 'matched'
    END AS match_class
FROM partner_clean p
LEFT JOIN (SELECT * FROM ledger_clean WHERE entry_type = 'partner_fee') f USING (ref);

-- d) Payouts ----------------------------------------------------------------------
CREATE OR REPLACE TABLE m_payouts AS
SELECT
    coalesce(p.payout_id, l.ref) AS payout_id,
    p.merchant_id,
    p.status,
    p.amount   AS payout_amount,
    l.amount   AS ledger_amount,
    CASE
        WHEN p.payout_id IS NULL                              THEN 'ledger_without_payout'
        WHEN p.status = 'completed' AND l.ref IS NULL         THEN 'completed_not_booked'
        WHEN p.status <> 'completed' AND l.ref IS NOT NULL    THEN 'failed_but_booked'
        WHEN p.status <> 'completed'                          THEN 'failed_not_booked'   -- correct
        WHEN abs(p.amount - l.amount) > 1e-8                  THEN 'amount_mismatch'
        ELSE 'matched'
    END AS match_class
FROM payouts_clean p
FULL JOIN (SELECT * FROM ledger_clean WHERE entry_type = 'payout') l ON p.payout_id = l.ref;
