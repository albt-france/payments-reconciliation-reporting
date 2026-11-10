-- =============================================================================
-- 06_merchant_cohorts.sql : merchant cohorts by month of first payment
--
-- Two censoring traps avoided:
--   * LEFT censoring : merchants live before Nov 2024 have no true "first month" in our data
--                      -> grouped in one cohort 'Before Nov 2024', not counted as new.
--   * RIGHT censoring: a cohort from Aug 2026 cannot have a month-6 value yet
--                      -> cells after the last full month (Oct 2026) are not produced,
--                         so a young cohort never looks like it "churned to 0".
-- =============================================================================
CREATE OR REPLACE TABLE cohorts AS
WITH active AS (   -- one row per merchant per month with at least one valid deposit
    SELECT merchant_id, month, sum(revenue_eur) AS revenue_eur, sum(volume_eur) AS volume_eur
    FROM fact_monthly
    WHERE deposits_n > 0
    GROUP BY ALL
),
first_month AS (
    SELECT a.merchant_id, min(a.month) AS first_month, any_value(d.signup_date) AS signup_date
    FROM active a JOIN dim_merchants d USING (merchant_id)
    GROUP BY a.merchant_id
),
labelled AS (
    SELECT merchant_id, first_month,
           CASE WHEN signup_date < DATE '2024-11-01' THEN 'Before Nov 2024'
                ELSE strftime(first_month, '%Y-%m') END AS cohort
    FROM first_month
),
cohort_size AS (
    SELECT cohort, min(first_month) AS cohort_start, count(*) AS cohort_merchants
    FROM labelled GROUP BY cohort
)
SELECT
    l.cohort,
    cs.cohort_start,
    a.month,
    date_diff('month', cs.cohort_start, a.month) AS months_since_first,
    cs.cohort_merchants,
    count(DISTINCT a.merchant_id) AS active_merchants,
    count(DISTINCT a.merchant_id) / cs.cohort_merchants AS retention_pct,
    sum(a.revenue_eur) AS revenue_eur,
    sum(a.volume_eur)  AS volume_eur
FROM active a
JOIN labelled l USING (merchant_id)
JOIN cohort_size cs USING (cohort)
WHERE a.month <= DATE '2026-10-01'        -- last full month: no partial-month cells
GROUP BY l.cohort, cs.cohort_start, a.month, cs.cohort_merchants
ORDER BY cs.cohort_start, a.month;
