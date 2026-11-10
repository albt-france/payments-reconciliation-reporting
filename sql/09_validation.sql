-- =============================================================================
-- 09_validation.sql : score the pipeline against the hidden answer key
-- planted_issues lists every break planted on purpose by the generator.
-- The pipeline (01-08) never reads it. Here we compare:
--   recall    = planted breaks found / planted breaks           ("did we miss any?")
--   precision = found breaks that were planted / found breaks   ("did we raise false alarms?")
--   status_ok = found breaks whose open/resolved status is right
-- =============================================================================
CREATE OR REPLACE TABLE validation_detail AS
SELECT
    coalesce(p.issue_type, b.break_type) AS issue_type,
    coalesce(p.record_id, b.record_id)   AS record_id,
    p.issue_id IS NOT NULL               AS is_planted,
    b.break_id IS NOT NULL               AS is_found,
    p.resolved_ts IS NULL OR p.resolved_ts >= getvariable('snapshot_ts') AS planted_open,
    b.status = 'open'                    AS found_open,
    p.amount_eur                         AS planted_amount_eur,
    b.amount_eur                         AS found_amount_eur
FROM raw_planted_issues p
FULL JOIN breaks b
       ON b.break_type = p.issue_type AND b.record_id = p.record_id;

CREATE OR REPLACE TABLE validation AS
SELECT
    issue_type,
    count(*) FILTER (WHERE is_planted)                    AS planted,
    count(*) FILTER (WHERE is_found)                      AS found,
    count(*) FILTER (WHERE is_planted AND is_found)       AS true_positives,
    count(*) FILTER (WHERE is_planted AND NOT is_found)   AS missed,
    count(*) FILTER (WHERE is_found AND NOT is_planted)   AS false_alarms,
    round(count(*) FILTER (WHERE is_planted AND is_found) / nullif(count(*) FILTER (WHERE is_planted), 0), 4) AS recall,
    round(count(*) FILTER (WHERE is_planted AND is_found) / nullif(count(*) FILTER (WHERE is_found), 0), 4)   AS "precision",
    round(count(*) FILTER (WHERE is_planted AND is_found AND planted_open = found_open)
          / nullif(count(*) FILTER (WHERE is_planted AND is_found), 0), 4) AS status_ok
FROM validation_detail
GROUP BY ROLLUP (issue_type)
ORDER BY issue_type NULLS LAST;
