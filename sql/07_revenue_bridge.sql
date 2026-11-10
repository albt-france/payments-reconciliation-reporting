-- =============================================================================
-- 07_revenue_bridge.sql : why did revenue move between two months?
--
-- Revenue R = V x T  (V = processed volume, T = take rate), split by merchant segment s:
--   R = V x sum_s ( w_s x t_s )      w_s = segment share of volume, t_s = segment take rate
--
-- R1 - R0 = (V1 - V0) x T0                          volume effect   : more / less volume at last month's rate
--         + V1 x sum_s (w1_s - w0_s) x t0_s         mix effect      : volume moved to cheaper / pricier segments
--         + V1 x sum_s  w1_s x (t1_s - t0_s)        take-rate effect: prices changed inside each segment
-- The three effects add up EXACTLY to R1 - R0 (checked in 08_controls.sql).
-- Long format (one row per step and segment) so Tableau can draw a waterfall.
-- =============================================================================
CREATE OR REPLACE TABLE revenue_bridge AS
WITH seg AS (
    SELECT f.month, d.segment, sum(f.volume_eur) AS v, sum(f.revenue_eur) AS r
    FROM fact_monthly f JOIN dim_merchants d USING (merchant_id)
    GROUP BY ALL
),
tot AS (SELECT month, sum(v) AS V, sum(r) AS R FROM seg GROUP BY month),
pairs AS (
    SELECT s1.month, s1.segment,
           s0.v / t0.V AS w0, s1.v / t1.V AS w1,
           s0.r / s0.v AS t0, s1.r / s1.v AS t1,
           t0.V AS V0, t1.V AS V1, t0.R AS R0, t1.R AS R1
    FROM seg s1
    JOIN seg s0 ON s0.segment = s1.segment AND s0.month = s1.month - INTERVAL 1 MONTH
    JOIN tot t0 ON t0.month = s0.month
    JOIN tot t1 ON t1.month = s1.month
),
steps AS (
    SELECT month, 1 AS step_order, 'Revenue previous month' AS step, 'All' AS segment, any_value(R0) AS value
    FROM pairs GROUP BY month
    UNION ALL
    SELECT month, 2, 'Volume effect', 'All', any_value((V1 - V0) * R0 / V0) FROM pairs GROUP BY month
    UNION ALL
    SELECT month, 3, 'Mix effect', segment, V1 * (w1 - w0) * t0 FROM pairs
    UNION ALL
    SELECT month, 4, 'Take-rate effect', segment, V1 * w1 * (t1 - t0) FROM pairs
    UNION ALL
    SELECT month, 5, 'Revenue this month', 'All', any_value(R1) FROM pairs GROUP BY month
)
SELECT month, (month - INTERVAL 1 MONTH)::DATE AS previous_month, step_order, step, segment, value
FROM steps
ORDER BY month, step_order, segment;
