-- =============================================================================
-- 02_match_invoice_chain.sql : invoice (gateway) <-> on-chain transfer (explorer)
--
-- Match key : same deposit address + same network + same currency
--             + transfer seen between invoice creation and creation + 7 days.
-- Then, per invoice:
--   transfers seen BEFORE expiry are ADDED UP (customers sometimes split a payment)
--     sum within 0.5 % of the invoice  -> exact
--     sum below                         -> underpayment
--     sum above                         -> overpayment
--   nothing before expiry, first transfer after it -> late_payment
--   any other transfer to an already-used address  -> address_reuse
-- Transfers that match no invoice:
--   address known on ANOTHER network  -> wrong_network (e.g. USDT sent on BSC to an Ethereum address)
--   address known, outside the window -> address_reuse
--   address unknown                   -> unknown_address
-- =============================================================================

-- 1. Candidate pairs -----------------------------------------------------------
CREATE OR REPLACE TABLE cand AS
SELECT
    c.tx_hash,
    i.invoice_id,
    i.merchant_id,
    c.seen_ts,
    i.expiry_ts,
    c.amount_crypto AS tx_amount,
    i.amount_crypto AS invoice_amount
FROM chain_clean c
JOIN invoices_clean i
  ON  c.to_address = i.pay_address
  AND c.network    = i.network
  AND c.currency   = i.currency
  AND c.seen_ts >= i.created_ts
  AND c.seen_ts <  i.created_ts + to_days(CAST(getvariable('match_window_days') AS INTEGER));

-- 2. One row per invoice: what was paid, and when --------------------------------
CREATE OR REPLACE TABLE m_invoices AS
WITH per_invoice AS (
    SELECT
        invoice_id,
        count(*)       FILTER (WHERE seen_ts <= expiry_ts) AS n_tx_in_window,
        sum(tx_amount) FILTER (WHERE seen_ts <= expiry_ts) AS paid_in_window,
        min(seen_ts)   FILTER (WHERE seen_ts <= expiry_ts) AS first_seen_ts,
        min(seen_ts)   FILTER (WHERE seen_ts >  expiry_ts) AS first_late_ts
    FROM cand
    GROUP BY invoice_id
)
SELECT
    i.*,
    coalesce(p.n_tx_in_window, 0) AS n_tx_in_window,
    p.paid_in_window,
    coalesce(p.first_seen_ts, p.first_late_ts) AS first_seen_ts,
    CASE
        WHEN p.n_tx_in_window > 0 THEN
            CASE
                WHEN p.paid_in_window < i.amount_crypto * (1 - getvariable('amount_tol_pct')) THEN 'underpayment'
                WHEN p.paid_in_window > i.amount_crypto * (1 + getvariable('amount_tol_pct')) THEN 'overpayment'
                ELSE 'exact'
            END
        WHEN p.first_late_ts IS NOT NULL THEN 'late_payment'
        ELSE 'unpaid'
    END AS payment_class
FROM invoices_clean i
LEFT JOIN per_invoice p USING (invoice_id);

-- 3. One row per on-chain transfer, with its class -------------------------------
CREATE OR REPLACE TABLE m_invoice_chain AS
WITH tx_class AS (
    SELECT
        c.tx_hash,
        c.invoice_id,
        c.merchant_id,
        CASE
            WHEN c.seen_ts <= c.expiry_ts THEN mi.payment_class           -- part of the payment
            WHEN mi.payment_class = 'late_payment' AND c.seen_ts = mi.first_seen_ts THEN 'late_payment'
            ELSE 'address_reuse'                                         -- extra transfer to a used address
        END AS match_class
    FROM cand c
    JOIN m_invoices mi USING (invoice_id)
),
-- addresses we issued, on any network, to explain transfers that matched nothing
known_address AS (
    SELECT pay_address, network, any_value(merchant_id) AS merchant_id
    FROM invoices_clean
    GROUP BY ALL
),
unmatched AS (
    SELECT
        c.tx_hash,
        NULL AS invoice_id,
        coalesce(same_net.merchant_id, other_net.merchant_id) AS merchant_id,
        CASE
            WHEN same_net.pay_address  IS NOT NULL THEN 'address_reuse'   -- right network, outside the window
            WHEN other_net.pay_address IS NOT NULL THEN 'wrong_network'
            ELSE 'unknown_address'
        END AS match_class
    FROM chain_clean c
    LEFT JOIN known_address same_net
           ON same_net.pay_address = c.to_address AND same_net.network = c.network
    LEFT JOIN (SELECT pay_address, any_value(merchant_id) AS merchant_id FROM invoices_clean GROUP BY 1) other_net
           ON other_net.pay_address = c.to_address
    WHERE c.tx_hash NOT IN (SELECT tx_hash FROM cand)
),
all_tx AS (
    SELECT * FROM tx_class
    UNION ALL
    SELECT * FROM unmatched
)
SELECT
    c.*,
    a.invoice_id,
    a.merchant_id,
    a.match_class,
    i.created_ts   AS invoice_created_ts,
    i.amount_crypto AS invoice_amount,
    -- value in EUR: the invoice's locked rate when there is one, else the real daily price
    c.amount_crypto * coalesce(i.rate_used, p.price_eur) AS amount_eur
FROM chain_clean c
JOIN all_tx a USING (tx_hash)
LEFT JOIN invoices_clean i USING (invoice_id)
LEFT JOIN price_eur p ON p.d = CAST(c.seen_ts AS DATE) AND p.currency = c.currency;
