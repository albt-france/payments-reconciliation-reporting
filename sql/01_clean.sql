-- =============================================================================
-- 01_clean.sql : one clean, typed version of each source
--   * every timestamp in UTC (the partner reports in its local time)
--   * one naming standard for networks (gateway says "TRC20", explorer says "tron")
--   * EVM tx hashes (0x...) in lower case: a legacy connector stored some in upper case.
--     ONLY hex hashes: Solana hashes are base58 and case-SENSITIVE (lower-casing them breaks the match)
--   * duplicates flagged with ROW_NUMBER(), never silently deleted
-- =============================================================================

-- Network names: map every system's label to one standard ------------------
CREATE OR REPLACE TABLE invoices_clean AS
SELECT
    invoice_id,
    merchant_id,
    created_ts,
    expiry_ts,
    upper(currency) AS currency,
    CASE
        WHEN network = 'BTC'                       THEN 'BITCOIN'
        WHEN network = 'LTC'                       THEN 'LITECOIN'
        WHEN network IN ('ETH', 'ERC20')           THEN 'ETHEREUM'
        WHEN network = 'BEP20'                     THEN 'BSC'
        WHEN network IN ('TRX', 'TRC20')           THEN 'TRON'
        WHEN network IN ('SOL', 'SPL')             THEN 'SOLANA'
    END AS network,
    pay_address,
    amount_crypto,
    amount_eur_at_rate,
    rate_used,
    status
FROM raw_invoices;

CREATE OR REPLACE TABLE chain_clean AS
SELECT
    CASE WHEN tx_hash LIKE '0x%' THEN lower(tx_hash) ELSE tx_hash END AS tx_hash,
    upper(network) AS network,          -- 'tron' -> 'TRON'
    upper(currency) AS currency,
    to_address,
    amount_crypto,
    seen_ts,
    block_ts,
    confirmations,
    network_fee,
    -- confirmations each network needs before we may credit the merchant (policy)
    CASE upper(network)
        WHEN 'BITCOIN' THEN 2  WHEN 'LITECOIN' THEN 6  WHEN 'ETHEREUM' THEN 12
        WHEN 'BSC' THEN 15     WHEN 'TRON' THEN 19     WHEN 'SOLANA' THEN 32
    END AS required_confirmations
FROM raw_chain_tx
WHERE seen_ts < getvariable('snapshot_ts');

-- Ledger: normalised hashes + duplicate flag --------------------------------
-- A deposit is a duplicate when the same tx_hash was credited twice.
-- ROW_NUMBER keeps the first booking (rn = 1) and flags the others (rn > 1).
CREATE OR REPLACE TABLE ledger_clean AS
WITH base AS (
    SELECT * REPLACE (CASE WHEN tx_hash LIKE '0x%' THEN lower(tx_hash) ELSE tx_hash END AS tx_hash)
    FROM raw_ledger
)
SELECT
    base.*,
    CASE WHEN entry_type = 'deposit'
         THEN ROW_NUMBER() OVER (PARTITION BY entry_type, tx_hash ORDER BY booked_ts, entry_id)
    END AS deposit_rank,
    (entry_type = 'deposit'
     AND ROW_NUMBER() OVER (PARTITION BY entry_type, tx_hash ORDER BY booked_ts, entry_id) > 1) AS is_duplicate
FROM base;

-- Partner statements: local time (Europe/Tallinn) -> UTC --------------------
-- timezone(zone, ts) reads a naive timestamp as local time in that zone and returns a UTC instant.
CREATE OR REPLACE TABLE partner_clean AS
SELECT
    stmt_id,
    ref,
    CAST(value_ts_local AS TIMESTAMP) AS value_ts_local,
    CAST(value_date AS DATE) AS value_date_local,
    timezone(getvariable('partner_tz'), CAST(value_ts_local AS TIMESTAMP))::TIMESTAMP AS value_ts_utc,
    upper(currency_sold) AS currency,
    amount_crypto,
    amount_eur,
    fees_eur
FROM raw_partner_statements;

CREATE OR REPLACE TABLE payouts_clean AS SELECT * FROM raw_payouts;

CREATE OR REPLACE TABLE dim_merchants AS
SELECT merchant_id, merchant_name, segment, country, signup_date, account_manager,
       deposit_fee_pct, conversion_fee_pct, payout_fee_pct, auto_convert_share
FROM raw_merchants;

-- Daily EUR price per currency, to value transfers that have no invoice rate
CREATE OR REPLACE TABLE price_eur AS
SELECT CAST(date AS DATE) AS d, currency, price_eur, price_usd, eurusd_ecb FROM prices_daily;
