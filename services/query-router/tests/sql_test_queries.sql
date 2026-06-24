-- ==========================================================================
-- SQL Test Query Set for query-router integration testing.
--
-- Format: each query is preceded by a comment line "-- QXXX label"
-- where XXX is a unique ID and label is a short description.
-- Queries are separated by semicolons.
--
-- The test harness (test_batch_queries.py) sends each query through
-- the JDBC gateway (port 5433) via psycopg2.  The gateway enforces
-- persona scope, so queries must NOT SELECT hidden columns directly
-- (payment_id, customer_id, etc.).  Hidden columns may still appear
-- in WHERE clauses and inside aggregate functions like SUM().
--
-- To add a new test query:
--   1. Add a "-- QXXX description" comment
--   2. Add the SQL (ending with semicolon)
--   3. Run: pytest tests/test_batch_queries.py -m e2e -v
-- ==========================================================================

-- =========================================================================
-- Q11: Scalar expressions and passthrough patterns
-- =========================================================================

-- Q11.10 CASE expression with string mapping
SELECT payment_reference,
       payment_status,
       CASE
           WHEN payment_status = 'SUCCESS' THEN 'POSITIVE_OUTCOME'
           WHEN payment_status IN ('FAILED', 'CANCELLED', 'EXPIRED') THEN 'NEGATIVE_OUTCOME'
           ELSE 'OTHER_OUTCOME'
       END AS outcome_group
FROM modely
ORDER BY payment_reference
LIMIT 50;

-- Q11.11 COALESCE with fallback
SELECT payment_reference,
       COALESCE(merchant_name, counterparty_name, 'UNKNOWN_PARTY') AS resolved_party_name
FROM modely
ORDER BY payment_reference
LIMIT 50;

-- Q11.12 CAST expressions
SELECT payment_reference,
       CAST(transaction_amount AS NUMERIC(20,2)) AS txn_amount_casted,
       CAST(transaction_count AS BIGINT) AS txn_count_bigint
FROM modely
ORDER BY payment_reference
LIMIT 50;

-- Q11.13 NULLIF
SELECT payment_reference,
       NULLIF(transaction_currency, billing_currency) AS currency_difference_indicator
FROM modely
ORDER BY payment_reference
LIMIT 50;

-- Q11.14 CASE with IS NULL and LIKE
SELECT payment_reference,
       account_number_masked,
       CASE
           WHEN account_number_masked IS NULL THEN 'NO_ACCOUNT'
           WHEN account_number_masked LIKE '%%****%%' THEN 'MASKED'
           ELSE 'OTHER_FORMAT'
       END AS account_mask_category
FROM modely
ORDER BY payment_reference
LIMIT 50;

-- Q11.15 WHERE with dimension filter and multiple measures in SELECT
SELECT payment_reference, transaction_amount, net_amount
FROM modely
WHERE payment_status = 'SUCCESS'
ORDER BY payment_reference
LIMIT 50;

-- Q11.16 CASE with boolean flag logic
SELECT payment_reference,
       success_flag,
       failure_flag,
       CASE
           WHEN success_flag = TRUE AND failure_flag = FALSE THEN 'CONSISTENT_SUCCESS'
           WHEN success_flag = FALSE AND failure_flag = TRUE THEN 'CONSISTENT_FAILURE'
           WHEN success_flag = FALSE AND failure_flag = FALSE THEN 'UNSET'
           ELSE 'INCONSISTENT_FLAGS'
       END AS flag_consistency
FROM modely
ORDER BY payment_reference
LIMIT 50;

-- Q11.17 COALESCE across measure columns
SELECT payment_reference,
       COALESCE(base_amount, settlement_amount, transaction_amount) AS preferred_amount
FROM modely
ORDER BY payment_reference
LIMIT 50;

-- Q11.18 WHERE with COALESCE date comparison
SELECT payment_reference, business_date
FROM modely
WHERE business_date IS NOT NULL
ORDER BY payment_reference
LIMIT 50;

-- =========================================================================
-- Q12: Physical JOINs to dimension tables (xfail — blocked on business view)
-- =========================================================================

-- Q12.01 LEFT JOIN to dim_customer_type
SELECT pt.payment_reference, pt.customer_type, dct.customer_type_name
FROM modely pt
LEFT JOIN demo_data.dim_customer_type dct
  ON pt.customer_type = dct.customer_type_code
ORDER BY pt.payment_reference
LIMIT 50;

-- Q12.02 LEFT JOIN to dim_customer_segment
SELECT pt.payment_reference, pt.customer_segment, dcs.customer_segment_name
FROM modely pt
LEFT JOIN demo_data.dim_customer_segment dcs
  ON pt.customer_segment = dcs.customer_segment_code
ORDER BY pt.payment_reference
LIMIT 50;

-- Q12.03 LEFT JOIN to dim_event_type
SELECT pt.payment_reference, pt.event_type, det.event_type_name, det.financial_impact
FROM modely pt
LEFT JOIN demo_data.dim_event_type det
  ON pt.event_type = det.event_type_code
ORDER BY pt.payment_reference
LIMIT 50;

-- Q12.04 LEFT JOIN to dim_payment_status
SELECT pt.payment_reference, pt.payment_status, dps.payment_status_name, dps.terminal_flag, dps.success_flag
FROM modely pt
LEFT JOIN demo_data.dim_payment_status dps
  ON pt.payment_status = dps.payment_status_code
ORDER BY pt.payment_reference
LIMIT 50;

-- Q12.05 LEFT JOIN to dim_lifecycle_stage
SELECT pt.payment_reference, pt.lifecycle_stage, dls.lifecycle_stage_name
FROM modely pt
LEFT JOIN demo_data.dim_lifecycle_stage dls
  ON pt.lifecycle_stage = dls.lifecycle_stage_code
ORDER BY pt.payment_reference
LIMIT 50;

-- Q12.06 LEFT JOIN to dim_account_type
SELECT pt.payment_reference, pt.account_type, dat.account_type_name
FROM modely pt
LEFT JOIN demo_data.dim_account_type dat
  ON pt.account_type = dat.account_type_code
ORDER BY pt.payment_reference
LIMIT 50;

-- Q12.07 LEFT JOIN to dim_channel_code
SELECT pt.payment_reference, pt.channel_code, dcc.channel_name, dcc.digital_flag
FROM modely pt
LEFT JOIN demo_data.dim_channel_code dcc
  ON pt.channel_code = dcc.channel_code
ORDER BY pt.payment_reference
LIMIT 50;

-- =========================================================================
-- Q13: Aggregates with GROUP BY
-- =========================================================================

-- Q13.01 Global aggregates no GROUP BY
SELECT COUNT(*) AS txn_count,
       SUM(transaction_amount) AS total_transaction_amount,
       SUM(net_amount) AS total_net_amount
FROM modely;

-- Q13.02 GROUP BY date with ORDER BY and LIMIT
SELECT business_date,
       COUNT(*) AS txn_count,
       SUM(transaction_amount) AS total_amount
FROM modely
GROUP BY business_date
ORDER BY business_date
LIMIT 100;

-- Q13.03 GROUP BY dimension with AVG
SELECT event_type,
       COUNT(*) AS txn_count,
       SUM(transaction_amount) AS total_amount,
       AVG(transaction_amount) AS avg_amount
FROM modely
GROUP BY event_type
ORDER BY event_type;

-- Q13.04 GROUP BY payment_status
SELECT payment_status,
       COUNT(*) AS txn_count,
       SUM(transaction_amount) AS total_amount
FROM modely
GROUP BY payment_status
ORDER BY payment_status;

-- Q13.05 GROUP BY with multiple SUM
SELECT payment_status,
       COUNT(*) AS txn_count,
       SUM(transaction_amount) AS total_amount,
       SUM(net_amount) AS total_net_amount
FROM modely
GROUP BY payment_status
ORDER BY payment_status;

-- Q13.07 Multi-column GROUP BY
SELECT customer_type,
       customer_segment,
       COUNT(*) AS txn_count,
       SUM(transaction_amount) AS total_amount
FROM modely
GROUP BY customer_type, customer_segment
ORDER BY customer_type, customer_segment;

-- Q13.10 GROUP BY with AVG and MAX
SELECT risk_decision,
       COUNT(*) AS txn_count,
       AVG(risk_score) AS avg_risk_score,
       MAX(risk_score) AS max_risk_score
FROM modely
GROUP BY risk_decision
ORDER BY risk_decision;

-- Q13.13 GROUP BY boolean flags
SELECT success_flag,
       failure_flag,
       COUNT(*) AS txn_count
FROM modely
GROUP BY success_flag, failure_flag
ORDER BY success_flag DESC, failure_flag DESC;

-- Q13.15 HAVING with OR on aggregates
SELECT business_date,
       SUM(refund_amount) AS total_refund_amount,
       SUM(chargeback_amount) AS total_chargeback_amount
FROM modely
GROUP BY business_date
HAVING SUM(refund_amount) > 0 OR SUM(chargeback_amount) > 0
ORDER BY business_date;

-- Q13.18 Multi-column GROUP BY with SUM
SELECT product_code,
       service_type,
       COUNT(*) AS txn_count,
       SUM(transaction_amount) AS total_amount
FROM modely
GROUP BY product_code, service_type
ORDER BY product_code, service_type;

-- =========================================================================
-- Q20: Compound aggregate expressions (Bug-875 regression tests)
-- =========================================================================

-- Q20.01 Compound aggregate: division
SELECT SUM(fee_amount)/SUM(base_amount) FROM modely;

-- Q20.02 Compound aggregate: multiply with literal
SELECT SUM(fee_amount)*SUM(base_amount)*0.001 FROM modely;

-- Q20.03 Compound aggregate: subtract
SELECT SUM(transaction_amount) - SUM(fee_amount) AS net_amount FROM modely;

-- Q20.04 Passthrough: SELECT 1 (Bug-877 regression)
SELECT 1;

-- Q20.05 Passthrough: current_timestamp
SELECT CURRENT_TIMESTAMP;

-- Q20.06 Compound aggregate with GROUP BY
SELECT business_date,
       SUM(fee_amount)/SUM(base_amount) AS fee_ratio
FROM modely
GROUP BY business_date
ORDER BY business_date
LIMIT 20;

-- Q20.07 Mixed compound and simple aggregates
SELECT SUM(transaction_amount) AS total_amount,
       SUM(fee_amount)/SUM(base_amount) AS fee_ratio,
       COUNT(*) AS txn_count
FROM modely;

-- Q20.08 Subquery wrapper over compound aggregate (Bug-878 regression)
SELECT * FROM (SELECT SUM(fee_amount)/SUM(base_amount) AS fee_ratio FROM modely) t;

-- Q20.09 Subquery wrapper over simple aggregate
SELECT * FROM (SELECT SUM(transaction_amount) AS total FROM modely) subq;

-- =========================================================================
-- Q30: EXTRACT, date/time, and type-casting functions
-- =========================================================================

-- Q30.01 EXTRACT year from literal timestamp multiplied by aggregate
SELECT EXTRACT(YEAR FROM TO_TIMESTAMP('2023-07-06 11:05:00', 'YYYY-MM-DD HH24:MI:SS'))
       * (SELECT SUM(base_amount) FROM modely)
FROM modely
LIMIT 1;

-- Q30.02 EXTRACT month from business_date with GROUP BY
SELECT EXTRACT(MONTH FROM business_date) AS biz_month,
       COUNT(*) AS txn_count,
       SUM(transaction_amount) AS total_amount
FROM modely
WHERE business_date IS NOT NULL
GROUP BY EXTRACT(MONTH FROM business_date)
ORDER BY biz_month;

-- Q30.03 EXTRACT day-of-week with CASE label
SELECT CASE EXTRACT(DOW FROM business_date)
           WHEN 0 THEN 'Sunday'
           WHEN 1 THEN 'Monday'
           WHEN 2 THEN 'Tuesday'
           WHEN 3 THEN 'Wednesday'
           WHEN 4 THEN 'Thursday'
           WHEN 5 THEN 'Friday'
           WHEN 6 THEN 'Saturday'
       END AS day_name,
       COUNT(*) AS txn_count
FROM modely
WHERE business_date IS NOT NULL
GROUP BY EXTRACT(DOW FROM business_date)
ORDER BY EXTRACT(DOW FROM business_date);

-- Q30.04 DATE_TRUNC to month with aggregate
SELECT DATE_TRUNC('month', business_date) AS month_start,
       SUM(transaction_amount) AS total_amount,
       AVG(transaction_amount) AS avg_amount
FROM modely
WHERE business_date IS NOT NULL
GROUP BY DATE_TRUNC('month', business_date)
ORDER BY month_start
LIMIT 12;

-- Q30.05 TO_CHAR date formatting
SELECT TO_CHAR(business_date, 'YYYY-Q') AS fiscal_quarter,
       COUNT(*) AS txn_count
FROM modely
WHERE business_date IS NOT NULL
GROUP BY TO_CHAR(business_date, 'YYYY-Q')
ORDER BY fiscal_quarter;

-- Q30.06 EXTRACT year and quarter combined
SELECT EXTRACT(YEAR FROM business_date) AS yr,
       EXTRACT(QUARTER FROM business_date) AS qtr,
       SUM(transaction_amount) AS total_amount
FROM modely
WHERE business_date IS NOT NULL
GROUP BY EXTRACT(YEAR FROM business_date), EXTRACT(QUARTER FROM business_date)
ORDER BY yr, qtr;

-- Q30.07 CAST aggregate to numeric with precision
SELECT CAST(SUM(transaction_amount) AS NUMERIC(18,2)) AS total_precise,
       CAST(AVG(transaction_amount) AS NUMERIC(18,4)) AS avg_precise
FROM modely;

-- Q30.08 Nested CAST inside COALESCE inside aggregate
SELECT SUM(CAST(COALESCE(fee_amount, 0) AS NUMERIC(18,2))) AS total_fees_safe
FROM modely;

-- =========================================================================
-- Q31: String functions
-- =========================================================================

-- Q31.01 UPPER and LOWER on dimension columns
SELECT UPPER(payment_status) AS status_upper,
       LOWER(event_type) AS event_lower,
       COUNT(*) AS txn_count
FROM modely
GROUP BY UPPER(payment_status), LOWER(event_type)
ORDER BY status_upper, event_lower;

-- Q31.02 CONCAT and string operators
SELECT payment_reference,
       CONCAT(payment_status, ' / ', event_type) AS status_event
FROM modely
ORDER BY payment_reference
LIMIT 20;

-- Q31.03 SUBSTRING and LENGTH
SELECT payment_reference,
       SUBSTRING(payment_reference FROM 1 FOR 4) AS prefix,
       LENGTH(payment_reference) AS ref_length
FROM modely
ORDER BY payment_reference
LIMIT 20;

-- Q31.04 TRIM and REPLACE
SELECT payment_reference,
       TRIM(BOTH ' ' FROM payment_status) AS status_trimmed,
       REPLACE(payment_status, 'SUCCESS', 'OK') AS status_replaced
FROM modely
ORDER BY payment_reference
LIMIT 20;

-- Q31.05 LEFT and RIGHT string extraction
SELECT payment_reference,
       LEFT(payment_reference, 3) AS first_3,
       RIGHT(payment_reference, 3) AS last_3
FROM modely
ORDER BY payment_reference
LIMIT 20;

-- Q31.06 POSITION / STRPOS
SELECT payment_reference,
       POSITION('_' IN payment_reference) AS underscore_pos
FROM modely
WHERE payment_reference LIKE '%%_%%'
ORDER BY payment_reference
LIMIT 20;

-- =========================================================================
-- Q32: Math functions
-- =========================================================================

-- Q32.01 ABS, ROUND, CEIL, FLOOR on aggregates
SELECT ABS(SUM(transaction_amount) - SUM(net_amount)) AS abs_diff,
       ROUND(AVG(transaction_amount), 2) AS avg_rounded,
       CEIL(AVG(risk_score)) AS avg_risk_ceil,
       FLOOR(AVG(risk_score)) AS avg_risk_floor
FROM modely;

-- Q32.02 POWER and SQRT on aggregates
SELECT SQRT(SUM(transaction_amount)) AS sqrt_total,
       POWER(AVG(transaction_amount), 2) AS avg_squared
FROM modely;

-- Q32.03 MOD on aggregate
SELECT MOD(COUNT(*)::INTEGER, 7) AS remainder_7
FROM modely;

-- Q32.04 GREATEST / LEAST across measure aggregates
SELECT GREATEST(SUM(transaction_amount), SUM(net_amount)) AS larger_total,
       LEAST(SUM(transaction_amount), SUM(net_amount)) AS smaller_total
FROM modely;

-- Q32.05 Chained math: percentage calculation
SELECT ROUND(
         SUM(fee_amount) * 100.0 / NULLIF(SUM(transaction_amount), 0),
         2
       ) AS fee_pct
FROM modely;

-- Q32.06 SIGN and ABS on compound expression
SELECT payment_status,
       SIGN(SUM(transaction_amount) - SUM(net_amount)) AS diff_sign,
       ABS(SUM(transaction_amount) - SUM(net_amount)) AS diff_abs
FROM modely
GROUP BY payment_status
ORDER BY payment_status;

-- =========================================================================
-- Q33: Scalar subqueries in SELECT
-- =========================================================================

-- Q33.01 Scalar subquery: each row's amount as % of global total
SELECT payment_reference,
       transaction_amount,
       transaction_amount * 100.0
         / (SELECT SUM(transaction_amount) FROM modely) AS pct_of_total
FROM modely
ORDER BY transaction_amount DESC
LIMIT 20;

-- Q33.02 Scalar subquery: deviation from global average
SELECT payment_reference,
       transaction_amount,
       transaction_amount - (SELECT AVG(transaction_amount) FROM modely) AS dev_from_avg
FROM modely
ORDER BY ABS(transaction_amount - (SELECT AVG(transaction_amount) FROM modely)) DESC
LIMIT 20;

-- Q33.03 Multiple scalar subqueries
SELECT (SELECT COUNT(*) FROM modely) AS total_rows,
       (SELECT SUM(transaction_amount) FROM modely) AS total_amount,
       (SELECT AVG(transaction_amount) FROM modely) AS avg_amount;

-- Q33.04 Scalar subquery in CASE
SELECT payment_reference,
       transaction_amount,
       CASE
           WHEN transaction_amount > (SELECT AVG(transaction_amount) FROM modely) THEN 'ABOVE_AVG'
           ELSE 'AT_OR_BELOW_AVG'
       END AS relative_position
FROM modely
ORDER BY payment_reference
LIMIT 30;

-- =========================================================================
-- Q34: Nested subqueries (multi-level derived tables)
-- =========================================================================

-- Q34.01 Two-level nested subquery
SELECT * FROM (
    SELECT status_group, total_amount
    FROM (
        SELECT payment_status AS status_group,
               SUM(transaction_amount) AS total_amount
        FROM modely
        GROUP BY payment_status
    ) AS inner_agg
    WHERE total_amount > 0
) AS outer_filter
ORDER BY total_amount DESC;

-- Q34.02 Three-level nested subquery
SELECT * FROM (
    SELECT * FROM (
        SELECT * FROM (
            SELECT payment_status,
                   COUNT(*) AS cnt
            FROM modely
            GROUP BY payment_status
        ) AS l1
    ) AS l2
) AS l3
ORDER BY cnt DESC;

-- Q34.03 Nested subquery with compound expression in inner
SELECT grand_total, fee_ratio FROM (
    SELECT SUM(transaction_amount) AS grand_total,
           SUM(fee_amount) / NULLIF(SUM(base_amount), 0) AS fee_ratio
    FROM modely
) AS summary;

-- Q34.04 Subquery with LIMIT in inner
SELECT * FROM (
    SELECT payment_reference, transaction_amount
    FROM modely
    ORDER BY transaction_amount DESC
    LIMIT 10
) AS top_10
ORDER BY transaction_amount ASC;

-- =========================================================================
-- Q35: Window functions
-- =========================================================================

-- Q35.01 ROW_NUMBER
SELECT payment_reference,
       transaction_amount,
       ROW_NUMBER() OVER (ORDER BY transaction_amount DESC) AS rn
FROM modely
LIMIT 20;

-- Q35.02 RANK and DENSE_RANK by payment_status
SELECT payment_reference,
       payment_status,
       transaction_amount,
       RANK() OVER (PARTITION BY payment_status ORDER BY transaction_amount DESC) AS rnk,
       DENSE_RANK() OVER (PARTITION BY payment_status ORDER BY transaction_amount DESC) AS drnk
FROM modely
LIMIT 30;

-- Q35.03 LAG and LEAD
SELECT payment_reference,
       business_date,
       transaction_amount,
       LAG(transaction_amount, 1) OVER (ORDER BY business_date, payment_reference) AS prev_amount,
       LEAD(transaction_amount, 1) OVER (ORDER BY business_date, payment_reference) AS next_amount
FROM modely
WHERE business_date IS NOT NULL
LIMIT 20;

-- Q35.04 Running total with SUM window
SELECT payment_reference,
       business_date,
       transaction_amount,
       SUM(transaction_amount) OVER (ORDER BY business_date, payment_reference
                                     ROWS UNBOUNDED PRECEDING) AS running_total
FROM modely
WHERE business_date IS NOT NULL
LIMIT 30;

-- Q35.05 NTILE (quartile bucketing)
SELECT payment_reference,
       transaction_amount,
       NTILE(4) OVER (ORDER BY transaction_amount) AS quartile
FROM modely
LIMIT 30;

-- Q35.06 PERCENT_RANK and CUME_DIST
SELECT payment_reference,
       transaction_amount,
       ROUND(CAST(PERCENT_RANK() OVER (ORDER BY transaction_amount) AS NUMERIC), 4) AS pct_rank,
       ROUND(CAST(CUME_DIST() OVER (ORDER BY transaction_amount) AS NUMERIC), 4) AS cume_dist
FROM modely
LIMIT 20;

-- Q35.07 Window function with PARTITION BY and ROWS frame
SELECT payment_status,
       payment_reference,
       transaction_amount,
       AVG(transaction_amount) OVER (
           PARTITION BY payment_status
           ORDER BY payment_reference
           ROWS BETWEEN 2 PRECEDING AND 2 FOLLOWING
       ) AS moving_avg_5
FROM modely
LIMIT 30;

-- =========================================================================
-- Q36: CTEs (Common Table Expressions)
-- =========================================================================

-- Q36.01 Simple CTE
WITH totals AS (
    SELECT payment_status,
           SUM(transaction_amount) AS total_amount,
           COUNT(*) AS txn_count
    FROM modely
    GROUP BY payment_status
)
SELECT * FROM totals ORDER BY total_amount DESC;

-- Q36.02 Multiple CTEs
WITH status_totals AS (
    SELECT payment_status,
           SUM(transaction_amount) AS status_total
    FROM modely
    GROUP BY payment_status
),
grand_total AS (
    SELECT SUM(transaction_amount) AS overall_total
    FROM modely
)
SELECT st.payment_status,
       st.status_total,
       ROUND(st.status_total * 100.0 / gt.overall_total, 2) AS pct
FROM status_totals st
CROSS JOIN grand_total gt
ORDER BY st.status_total DESC;

-- Q36.03 CTE referencing another CTE
WITH raw_data AS (
    SELECT payment_status,
           transaction_amount
    FROM modely
),
aggregated AS (
    SELECT payment_status,
           SUM(transaction_amount) AS total,
           COUNT(*) AS cnt
    FROM raw_data
    GROUP BY payment_status
)
SELECT payment_status, total, cnt,
       ROUND(total / NULLIF(cnt, 0), 2) AS avg_per_txn
FROM aggregated
ORDER BY total DESC;

-- Q36.04 CTE with window function
WITH ranked AS (
    SELECT payment_reference,
           payment_status,
           transaction_amount,
           ROW_NUMBER() OVER (PARTITION BY payment_status
                              ORDER BY transaction_amount DESC) AS rn
    FROM modely
)
SELECT payment_reference, payment_status, transaction_amount
FROM ranked
WHERE rn <= 3
ORDER BY payment_status, rn;

-- =========================================================================
-- Q37: Set operations (UNION, INTERSECT, EXCEPT)
-- =========================================================================

-- Q37.01 UNION ALL of two aggregates
SELECT 'SUCCESS' AS category,
       SUM(transaction_amount) AS total
FROM modely
WHERE payment_status = 'SUCCESS'
UNION ALL
SELECT 'OTHER' AS category,
       SUM(transaction_amount) AS total
FROM modely
WHERE payment_status <> 'SUCCESS';

-- Q37.02 UNION of dimension values from different filters
SELECT DISTINCT payment_status AS value, 'status' AS source
FROM modely
WHERE transaction_amount > 1000
UNION
SELECT DISTINCT event_type AS value, 'event' AS source
FROM modely
WHERE transaction_amount > 1000
ORDER BY source, value;

-- Q37.03 EXCEPT: statuses that have no high-value transactions
SELECT DISTINCT payment_status FROM modely
EXCEPT
SELECT DISTINCT payment_status FROM modely
WHERE transaction_amount > 10000
ORDER BY payment_status;

-- Q37.04 INTERSECT: statuses common to high and low value
SELECT DISTINCT payment_status FROM modely
WHERE transaction_amount > 5000
INTERSECT
SELECT DISTINCT payment_status FROM modely
WHERE transaction_amount < 100
ORDER BY payment_status;

-- =========================================================================
-- Q38: Subqueries in WHERE / EXISTS / IN
-- =========================================================================

-- Q38.01 IN subquery
SELECT payment_reference, transaction_amount
FROM modely
WHERE payment_status IN (
    SELECT DISTINCT payment_status
    FROM modely
    WHERE transaction_amount > 5000
)
ORDER BY payment_reference
LIMIT 20;

-- Q38.02 NOT IN subquery
SELECT payment_reference, transaction_amount
FROM modely
WHERE event_type NOT IN (
    SELECT DISTINCT event_type
    FROM modely
    WHERE transaction_amount > 10000
)
ORDER BY payment_reference
LIMIT 20;

-- Q38.03 EXISTS subquery (correlated)
SELECT m1.payment_reference, m1.transaction_amount
FROM modely m1
WHERE EXISTS (
    SELECT 1 FROM modely m2
    WHERE m2.payment_status = m1.payment_status
      AND m2.transaction_amount > 5000
)
ORDER BY m1.payment_reference
LIMIT 20;

-- Q38.04 Scalar subquery in WHERE
SELECT payment_reference, transaction_amount
FROM modely
WHERE transaction_amount > (SELECT AVG(transaction_amount) FROM modely)
ORDER BY transaction_amount DESC
LIMIT 20;

-- =========================================================================
-- Q39: Complex compound expressions and edge cases
-- =========================================================================

-- Q39.01 CASE wrapping aggregates
SELECT CASE
           WHEN SUM(transaction_amount) > 1000000 THEN 'HIGH_VOLUME'
           WHEN SUM(transaction_amount) > 100000 THEN 'MEDIUM_VOLUME'
           ELSE 'LOW_VOLUME'
       END AS volume_tier,
       SUM(transaction_amount) AS total
FROM modely;

-- Q39.02 Aggregate inside COALESCE
SELECT COALESCE(SUM(fee_amount), 0) AS total_fees,
       COALESCE(SUM(refund_amount), 0) AS total_refunds,
       COALESCE(SUM(chargeback_amount), 0) AS total_chargebacks
FROM modely;

-- Q39.03 Nested function calls: ROUND(ABS(SUM(...) - SUM(...)))
SELECT ROUND(ABS(SUM(transaction_amount) - SUM(net_amount)), 2) AS abs_diff_rounded
FROM modely;

-- Q39.04 NULLIF preventing division by zero in compound aggregate
SELECT SUM(fee_amount) / NULLIF(SUM(transaction_amount), 0) AS safe_ratio
FROM modely;

-- Q39.05 Boolean expression with aggregates
SELECT COUNT(*) AS total_txns,
       SUM(CASE WHEN payment_status = 'SUCCESS' THEN 1 ELSE 0 END) AS success_count,
       ROUND(
           SUM(CASE WHEN payment_status = 'SUCCESS' THEN 1 ELSE 0 END) * 100.0
           / NULLIF(COUNT(*), 0),
           2
       ) AS success_rate_pct
FROM modely;

-- Q39.06 Multi-level arithmetic with aggregates and literals
SELECT SUM(transaction_amount) * 1.1 - SUM(fee_amount) * 2.0
       + SUM(net_amount) / 3.0 AS complex_calc
FROM modely;

-- Q39.07 Aggregate with FILTER (PostgreSQL extension)
SELECT COUNT(*) FILTER (WHERE payment_status = 'SUCCESS') AS success_count,
       COUNT(*) FILTER (WHERE payment_status = 'FAILED') AS failed_count,
       SUM(transaction_amount) FILTER (WHERE payment_status = 'SUCCESS') AS success_amount
FROM modely;

-- Q39.08 DISTINCT inside aggregate
SELECT COUNT(DISTINCT payment_status) AS distinct_statuses,
       COUNT(DISTINCT event_type) AS distinct_events,
       SUM(transaction_amount) AS total_amount
FROM modely;

-- Q39.09 Mixed window + aggregate in derived table
SELECT * FROM (
    SELECT payment_status,
           SUM(transaction_amount) AS total,
           RANK() OVER (ORDER BY SUM(transaction_amount) DESC) AS rnk
    FROM modely
    GROUP BY payment_status
) AS ranked_statuses
ORDER BY rnk;

-- Q39.10 EXTRACT in scalar subquery multiplied by aggregate
SELECT EXTRACT(YEAR FROM CURRENT_DATE)
       * (SELECT SUM(base_amount) FROM modely) AS year_times_total
FROM modely
LIMIT 1;

-- Q39.11 Deeply nested function composition
SELECT ROUND(
         CAST(
           SQRT(ABS(SUM(transaction_amount) - SUM(net_amount)))
           AS NUMERIC
         ),
         4
       ) AS deep_nested_calc
FROM modely;

-- Q39.12 CASE with multiple WHEN on aggregates and GROUP BY
SELECT payment_status,
       CASE
           WHEN COUNT(*) > 1000 THEN 'VERY_HIGH'
           WHEN COUNT(*) > 100 THEN 'HIGH'
           WHEN COUNT(*) > 10 THEN 'MEDIUM'
           ELSE 'LOW'
       END AS frequency_tier,
       COUNT(*) AS cnt
FROM modely
GROUP BY payment_status
ORDER BY cnt DESC;

-- Q39.13 Concatenation of aggregate results
SELECT 'Total: ' || CAST(SUM(transaction_amount) AS TEXT)
       || ' | Count: ' || CAST(COUNT(*) AS TEXT) AS summary_line
FROM modely;

-- Q39.14 ALL/ANY subquery comparison
SELECT payment_reference, transaction_amount
FROM modely
WHERE transaction_amount >= ALL (
    SELECT AVG(transaction_amount)
    FROM modely
    GROUP BY payment_status
)
ORDER BY transaction_amount DESC
LIMIT 10;

-- Q39.15 Lateral-style cross join with VALUES
SELECT m.payment_reference,
       m.transaction_amount,
       v.multiplier,
       m.transaction_amount * v.multiplier AS scaled
FROM modely m
CROSS JOIN (VALUES (1.0), (1.1), (1.25)) AS v(multiplier)
ORDER BY m.payment_reference, v.multiplier
LIMIT 30;

-- =========================================================================
-- Q40: Compound aggregate stress tests (SUM/SUM client regression)
-- =========================================================================

-- Q40.01 Bare SUM/SUM ratio (exact client pattern that broke)
SELECT SUM(fee_amount) / SUM(transaction_amount) FROM modely;

-- Q40.02 SUM/SUM with alias
SELECT SUM(fee_amount) / SUM(transaction_amount) AS fee_rate FROM modely;

-- Q40.03 SUM/SUM with NULLIF guard
SELECT SUM(fee_amount) / NULLIF(SUM(transaction_amount), 0) AS safe_fee_rate
FROM modely;

-- Q40.04 Multiple compound ratios in same SELECT
SELECT SUM(fee_amount) / NULLIF(SUM(transaction_amount), 0) AS fee_rate,
       SUM(net_amount) / NULLIF(SUM(base_amount), 0) AS net_ratio,
       SUM(refund_amount) / NULLIF(SUM(transaction_amount), 0) AS refund_rate
FROM modely;

-- Q40.05 Compound aggregate with GROUP BY
SELECT payment_status,
       SUM(fee_amount) / NULLIF(SUM(transaction_amount), 0) AS fee_rate,
       SUM(net_amount) / NULLIF(SUM(base_amount), 0) AS net_ratio
FROM modely
GROUP BY payment_status
ORDER BY payment_status;

-- Q40.06 Compound aggregate with GROUP BY and HAVING
SELECT payment_status,
       SUM(fee_amount) / NULLIF(SUM(transaction_amount), 0) AS fee_rate
FROM modely
GROUP BY payment_status
HAVING SUM(transaction_amount) > 0
ORDER BY fee_rate DESC;

-- Q40.07 Compound aggregate with ROUND wrapping
SELECT ROUND(SUM(fee_amount) / NULLIF(SUM(transaction_amount), 0), 4) AS fee_rate_4dp
FROM modely;

-- Q40.08 Compound aggregate: SUM * SUM
SELECT SUM(fee_amount) * SUM(transaction_count) AS fee_times_count
FROM modely;

-- Q40.09 Compound aggregate: SUM - SUM
SELECT SUM(transaction_amount) - SUM(fee_amount) - SUM(refund_amount) AS true_net
FROM modely;

-- Q40.10 Compound aggregate: three-way ratio
SELECT SUM(fee_amount) / NULLIF(SUM(transaction_amount), 0)
       / NULLIF(SUM(transaction_count), 0) AS fee_per_txn_per_dollar
FROM modely;

-- Q40.11 Compound aggregate with CASE outcome
SELECT CASE
           WHEN SUM(fee_amount) / NULLIF(SUM(transaction_amount), 0) > 0.05 THEN 'HIGH_FEE'
           WHEN SUM(fee_amount) / NULLIF(SUM(transaction_amount), 0) > 0.02 THEN 'MEDIUM_FEE'
           ELSE 'LOW_FEE'
       END AS fee_category,
       SUM(fee_amount) / NULLIF(SUM(transaction_amount), 0) AS fee_rate
FROM modely;

-- Q40.12 Compound aggregate inside subquery
SELECT * FROM (
    SELECT SUM(fee_amount) / NULLIF(SUM(transaction_amount), 0) AS fee_rate,
           SUM(net_amount) / NULLIF(SUM(base_amount), 0) AS net_ratio
    FROM modely
) AS rates;

-- Q40.13 Compound aggregate in CTE
WITH rates AS (
    SELECT payment_status,
           SUM(fee_amount) / NULLIF(SUM(transaction_amount), 0) AS fee_rate,
           COUNT(*) AS txn_count
    FROM modely
    GROUP BY payment_status
)
SELECT payment_status, fee_rate, txn_count
FROM rates
ORDER BY fee_rate DESC;

-- Q40.14 Mixed simple and compound aggregates
SELECT COUNT(*) AS txn_count,
       SUM(transaction_amount) AS total_amount,
       AVG(transaction_amount) AS avg_amount,
       SUM(fee_amount) / NULLIF(SUM(transaction_amount), 0) AS fee_rate,
       SUM(net_amount) - SUM(fee_amount) AS adjusted_net
FROM modely;

-- Q40.15 Compound aggregate with multi-column GROUP BY
SELECT event_type,
       payment_status,
       SUM(fee_amount) / NULLIF(SUM(transaction_amount), 0) AS fee_rate,
       SUM(transaction_amount) - SUM(net_amount) AS amount_gap
FROM modely
GROUP BY event_type, payment_status
ORDER BY event_type, payment_status;

-- Q40.16 Nested ROUND(CAST(SUM/SUM))
SELECT ROUND(CAST(SUM(fee_amount) AS NUMERIC) / NULLIF(CAST(SUM(base_amount) AS NUMERIC), 0), 6)
       AS precise_rate
FROM modely;

-- Q40.17 AVG/AVG compound
SELECT AVG(transaction_amount) / NULLIF(AVG(risk_score), 0) AS amount_per_risk_unit
FROM modely;

-- Q40.18 COUNT ratio
SELECT CAST(COUNT(*) FILTER (WHERE payment_status = 'SUCCESS') AS NUMERIC)
       / NULLIF(COUNT(*), 0) AS success_rate
FROM modely;

-- Q40.19 Compound aggregate with ORDER BY on the ratio
SELECT payment_status,
       SUM(fee_amount) / NULLIF(SUM(transaction_amount), 0) AS fee_rate
FROM modely
GROUP BY payment_status
ORDER BY SUM(fee_amount) / NULLIF(SUM(transaction_amount), 0) DESC;

-- Q40.20 ABS of compound aggregate difference
SELECT ABS(SUM(fee_amount) / NULLIF(SUM(transaction_amount), 0)
         - SUM(refund_amount) / NULLIF(SUM(transaction_amount), 0)) AS rate_gap
FROM modely;

-- =========================================================================
-- Q41: Complex expression compositions
-- =========================================================================

-- Q41.01 Percentage with ROUND and CAST
SELECT ROUND(100.0 * SUM(CASE WHEN payment_status = 'SUCCESS' THEN transaction_amount ELSE 0 END)
       / NULLIF(SUM(transaction_amount), 0), 2) AS success_amount_pct
FROM modely;

-- Q41.02 Weighted average
SELECT SUM(transaction_amount * risk_score) / NULLIF(SUM(transaction_amount), 0)
       AS weighted_avg_risk
FROM modely;

-- Q41.03 Variance approximation using aggregates
SELECT AVG(transaction_amount * transaction_amount)
       - POWER(AVG(transaction_amount), 2) AS variance_approx
FROM modely;

-- Q41.04 Coefficient of variation
SELECT SQRT(
         AVG(transaction_amount * transaction_amount)
         - POWER(AVG(transaction_amount), 2)
       ) / NULLIF(AVG(transaction_amount), 0) AS coeff_of_variation
FROM modely;

-- Q41.05 Multiple CASE-wrapped aggregates in one SELECT
SELECT CASE WHEN SUM(transaction_amount) > 0 THEN 'POSITIVE' ELSE 'ZERO_OR_NEG' END AS amount_sign,
       CASE WHEN AVG(risk_score) > 50 THEN 'RISKY' ELSE 'SAFE' END AS risk_label,
       CASE WHEN COUNT(*) > 1000 THEN 'HIGH_VOL' ELSE 'LOW_VOL' END AS volume_label
FROM modely;

-- Q41.06 Nested GREATEST/LEAST on compound aggregates
SELECT GREATEST(
         SUM(fee_amount) / NULLIF(SUM(transaction_amount), 0),
         SUM(refund_amount) / NULLIF(SUM(transaction_amount), 0)
       ) AS higher_rate,
       LEAST(
         SUM(fee_amount) / NULLIF(SUM(transaction_amount), 0),
         SUM(refund_amount) / NULLIF(SUM(transaction_amount), 0)
       ) AS lower_rate
FROM modely;

-- Q41.07 COALESCE across compound aggregates
SELECT COALESCE(
         SUM(fee_amount) / NULLIF(SUM(transaction_amount), 0),
         SUM(refund_amount) / NULLIF(SUM(transaction_amount), 0),
         0
       ) AS first_non_null_rate
FROM modely;

-- Q41.08 String concatenation of formatted aggregates
SELECT 'Fee rate: ' || TO_CHAR(
         SUM(fee_amount) / NULLIF(SUM(transaction_amount), 0) * 100,
         'FM990.00'
       ) || '%%' AS fee_rate_text
FROM modely;

-- Q41.09 CASE WHEN on ratio with GROUP BY
SELECT payment_status,
       CASE
           WHEN SUM(transaction_amount) = 0 THEN 'NO_DATA'
           WHEN SUM(fee_amount) / SUM(transaction_amount) > 0.10 THEN 'EXCESSIVE'
           WHEN SUM(fee_amount) / SUM(transaction_amount) > 0.05 THEN 'HIGH'
           WHEN SUM(fee_amount) / SUM(transaction_amount) > 0.01 THEN 'NORMAL'
           ELSE 'LOW'
       END AS fee_tier,
       COUNT(*) AS txn_count
FROM modely
GROUP BY payment_status
ORDER BY payment_status;

-- Q41.10 Window function over compound aggregate in derived table
SELECT payment_status, fee_rate,
       RANK() OVER (ORDER BY fee_rate DESC) AS fee_rank
FROM (
    SELECT payment_status,
           SUM(fee_amount) / NULLIF(SUM(transaction_amount), 0) AS fee_rate
    FROM modely
    GROUP BY payment_status
) AS rates
ORDER BY fee_rank;

-- =========================================================================
-- Q42: Window functions with aggregates (single-table, must pass through)
-- =========================================================================

-- Q42.01 SUM OVER entire result set
SELECT payment_reference,
       transaction_amount,
       SUM(transaction_amount) OVER () AS grand_total,
       transaction_amount / NULLIF(SUM(transaction_amount) OVER (), 0) AS pct_of_total
FROM modely
LIMIT 20;

-- Q42.02 SUM OVER PARTITION BY dimension
SELECT payment_status,
       payment_reference,
       transaction_amount,
       SUM(transaction_amount) OVER (PARTITION BY payment_status) AS status_total
FROM modely
LIMIT 30;

-- Q42.03 Running average with ROWS frame
SELECT payment_reference,
       business_date,
       transaction_amount,
       AVG(transaction_amount) OVER (
           ORDER BY business_date, payment_reference
           ROWS BETWEEN 4 PRECEDING AND CURRENT ROW
       ) AS moving_avg_5
FROM modely
WHERE business_date IS NOT NULL
LIMIT 30;

-- Q42.04 Multiple window functions in one query
SELECT payment_reference,
       payment_status,
       transaction_amount,
       SUM(transaction_amount) OVER (PARTITION BY payment_status) AS status_total,
       COUNT(*) OVER (PARTITION BY payment_status) AS status_count,
       AVG(transaction_amount) OVER (PARTITION BY payment_status) AS status_avg,
       ROW_NUMBER() OVER (PARTITION BY payment_status ORDER BY transaction_amount DESC) AS rn
FROM modely
LIMIT 30;

-- Q42.05 Window frame: RANGE BETWEEN
SELECT payment_reference,
       business_date,
       transaction_amount,
       SUM(transaction_amount) OVER (
           ORDER BY business_date
           RANGE BETWEEN INTERVAL '7 days' PRECEDING AND CURRENT ROW
       ) AS rolling_7d_total
FROM modely
WHERE business_date IS NOT NULL
LIMIT 20;

-- Q42.06 FIRST_VALUE / LAST_VALUE
SELECT payment_reference,
       payment_status,
       transaction_amount,
       FIRST_VALUE(transaction_amount) OVER (
           PARTITION BY payment_status ORDER BY transaction_amount DESC
       ) AS highest_in_status,
       LAST_VALUE(transaction_amount) OVER (
           PARTITION BY payment_status ORDER BY transaction_amount DESC
           ROWS BETWEEN UNBOUNDED PRECEDING AND UNBOUNDED FOLLOWING
       ) AS lowest_in_status
FROM modely
LIMIT 30;

-- Q42.07 NTH_VALUE
SELECT payment_reference,
       payment_status,
       transaction_amount,
       NTH_VALUE(transaction_amount, 2) OVER (
           PARTITION BY payment_status ORDER BY transaction_amount DESC
           ROWS BETWEEN UNBOUNDED PRECEDING AND UNBOUNDED FOLLOWING
       ) AS second_highest
FROM modely
LIMIT 30;

-- Q42.08 CTE with window aggregate inside
WITH windowed AS (
    SELECT payment_reference,
           payment_status,
           transaction_amount,
           SUM(transaction_amount) OVER (PARTITION BY payment_status) AS status_total
    FROM modely
)
SELECT payment_status,
       COUNT(*) AS cnt,
       MAX(status_total) AS status_total
FROM windowed
GROUP BY payment_status
ORDER BY status_total DESC;

-- =========================================================================
-- Q43: HAVING clause edge cases
-- =========================================================================

-- Q43.01 HAVING with compound aggregate condition
SELECT payment_status,
       SUM(fee_amount) / NULLIF(SUM(transaction_amount), 0) AS fee_rate
FROM modely
GROUP BY payment_status
HAVING SUM(fee_amount) / NULLIF(SUM(transaction_amount), 0) > 0.01
ORDER BY fee_rate DESC;

-- Q43.02 HAVING with multiple conditions
SELECT event_type,
       COUNT(*) AS txn_count,
       SUM(transaction_amount) AS total_amount
FROM modely
GROUP BY event_type
HAVING COUNT(*) > 10
   AND SUM(transaction_amount) > 1000
ORDER BY total_amount DESC;

-- Q43.03 HAVING with CASE expression
SELECT payment_status,
       COUNT(*) AS cnt
FROM modely
GROUP BY payment_status
HAVING CASE WHEN COUNT(*) > 100 THEN TRUE ELSE FALSE END
ORDER BY cnt DESC;

-- Q43.04 HAVING referencing column not in SELECT
SELECT payment_status, COUNT(*) AS cnt
FROM modely
GROUP BY payment_status
HAVING SUM(transaction_amount) > AVG(transaction_amount) * 10
ORDER BY cnt DESC;

-- =========================================================================
-- Q44: Deeply nested and pathological expressions
-- =========================================================================

-- Q44.01 Five-level nested function call
SELECT ROUND(
         ABS(
           SQRT(
             POWER(
               SUM(transaction_amount) - SUM(net_amount),
               2
             )
           )
         ),
         2
       ) AS five_deep
FROM modely;

-- Q44.02 Compound aggregate inside COALESCE inside ROUND
SELECT ROUND(
         COALESCE(
           SUM(fee_amount) / NULLIF(SUM(transaction_amount), 0),
           0
         ) * 100,
         2
       ) AS fee_pct_safe
FROM modely;

-- Q44.03 Multiple subqueries in single SELECT
SELECT (SELECT SUM(transaction_amount) FROM modely WHERE payment_status = 'SUCCESS')
       / NULLIF((SELECT SUM(transaction_amount) FROM modely), 0)
       AS success_ratio;

-- Q44.04 Correlated subquery in SELECT
SELECT payment_status,
       (SELECT COUNT(*) FROM modely m2 WHERE m2.payment_status = m1.payment_status) AS status_count,
       (SELECT SUM(transaction_amount) FROM modely m2 WHERE m2.payment_status = m1.payment_status) AS status_total
FROM modely m1
GROUP BY payment_status
ORDER BY payment_status;

-- Q44.05 UNION ALL of compound aggregates
SELECT 'fee_rate' AS metric,
       SUM(fee_amount) / NULLIF(SUM(transaction_amount), 0) AS value
FROM modely
UNION ALL
SELECT 'refund_rate' AS metric,
       SUM(refund_amount) / NULLIF(SUM(transaction_amount), 0) AS value
FROM modely
UNION ALL
SELECT 'chargeback_rate' AS metric,
       SUM(chargeback_amount) / NULLIF(SUM(transaction_amount), 0) AS value
FROM modely;

-- Q44.06 Subquery in FROM with compound aggregate, filtered by outer WHERE
SELECT fee_rate FROM (
    SELECT payment_status,
           SUM(fee_amount) / NULLIF(SUM(transaction_amount), 0) AS fee_rate
    FROM modely
    GROUP BY payment_status
) AS sub
WHERE fee_rate > 0
ORDER BY fee_rate DESC;

-- Q44.07 Arithmetic between scalar subqueries
SELECT (SELECT MAX(transaction_amount) FROM modely)
       - (SELECT MIN(transaction_amount) FROM modely)
       AS amount_range;

-- Q44.08 CASE with IN and compound subquery
SELECT CASE
           WHEN payment_status IN (
               SELECT payment_status FROM modely
               GROUP BY payment_status
               HAVING SUM(transaction_amount) > 100000
           ) THEN 'HIGH_VALUE_STATUS'
           ELSE 'OTHER'
       END AS status_tier,
       payment_reference,
       transaction_amount
FROM modely
ORDER BY payment_reference
LIMIT 20;

-- Q44.09 Double wrapping: subquery inside subquery inside main
SELECT * FROM (
    SELECT * FROM (
        SELECT payment_status,
               SUM(fee_amount) / NULLIF(SUM(transaction_amount), 0) AS rate
        FROM modely
        GROUP BY payment_status
    ) AS inner_rates
    WHERE rate > 0
) AS outer_rates
ORDER BY rate DESC;

-- Q44.10 Window function over compound aggregate result
SELECT payment_status,
       fee_rate,
       LAG(fee_rate) OVER (ORDER BY fee_rate) AS prev_rate,
       fee_rate - LAG(fee_rate) OVER (ORDER BY fee_rate) AS rate_change
FROM (
    SELECT payment_status,
           SUM(fee_amount) / NULLIF(SUM(transaction_amount), 0) AS fee_rate
    FROM modely
    GROUP BY payment_status
) AS rates
ORDER BY fee_rate;

-- =========================================================================
-- Q45: Real-world BI patterns and adversarial edge cases
-- =========================================================================

-- Q45.01 Pareto analysis: cumulative percentage of total
SELECT payment_status,
       SUM(transaction_amount) AS status_total,
       SUM(SUM(transaction_amount)) OVER (ORDER BY SUM(transaction_amount) DESC) AS running_total,
       SUM(SUM(transaction_amount)) OVER (ORDER BY SUM(transaction_amount) DESC)
           / NULLIF(SUM(SUM(transaction_amount)) OVER (), 0) AS cumulative_pct
FROM modely
GROUP BY payment_status
ORDER BY status_total DESC;

-- Q45.02 Conditional aggregation: pivot-style columns
SELECT payment_status,
       SUM(CASE WHEN event_type = 'AUTHORIZATION' THEN transaction_amount ELSE 0 END) AS auth_amount,
       SUM(CASE WHEN event_type = 'CAPTURE' THEN transaction_amount ELSE 0 END) AS capture_amount,
       SUM(CASE WHEN event_type = 'REFUND' THEN transaction_amount ELSE 0 END) AS refund_total,
       COUNT(CASE WHEN event_type = 'AUTHORIZATION' THEN 1 END) AS auth_count,
       COUNT(CASE WHEN event_type = 'CAPTURE' THEN 1 END) AS capture_count
FROM modely
GROUP BY payment_status
ORDER BY payment_status;

-- Q45.03 Ratio of ratios: fee-to-refund ratio vs fee-to-total ratio
SELECT payment_status,
       (SUM(fee_amount) / NULLIF(SUM(refund_amount), 0))
       / NULLIF(SUM(fee_amount) / NULLIF(SUM(transaction_amount), 0), 0) AS ratio_of_ratios
FROM modely
GROUP BY payment_status
HAVING SUM(refund_amount) > 0 AND SUM(transaction_amount) > 0
ORDER BY ratio_of_ratios DESC;

-- Q45.04 Z-score approximation: how far each status deviates from average
SELECT payment_status,
       SUM(transaction_amount) AS total,
       (SUM(transaction_amount) - AVG(SUM(transaction_amount)) OVER ())
           / NULLIF(STDDEV_POP(SUM(transaction_amount)) OVER (), 0) AS z_score
FROM modely
GROUP BY payment_status
ORDER BY z_score DESC;

-- Q45.05 Bin/bucket distribution using WIDTH_BUCKET
SELECT WIDTH_BUCKET(transaction_amount, 0, 10000, 10) AS bucket,
       COUNT(*) AS freq,
       MIN(transaction_amount) AS bucket_min,
       MAX(transaction_amount) AS bucket_max,
       AVG(transaction_amount) AS bucket_avg
FROM modely
GROUP BY WIDTH_BUCKET(transaction_amount, 0, 10000, 10)
ORDER BY bucket;

-- Q45.06 Same measure with multiple aggregations in one query
SELECT payment_status,
       SUM(transaction_amount) AS total,
       AVG(transaction_amount) AS average,
       MIN(transaction_amount) AS minimum,
       MAX(transaction_amount) AS maximum,
       COUNT(*) AS txn_count,
       SUM(transaction_amount) / NULLIF(COUNT(*), 0) AS manual_avg,
       MAX(transaction_amount) - MIN(transaction_amount) AS spread
FROM modely
GROUP BY payment_status
ORDER BY total DESC;

-- Q45.07 Boolean aggregation: CASE-based conditional counts
SELECT payment_status,
       COUNT(*) AS cnt,
       SUM(CASE WHEN event_type = 'AUTHORIZATION' THEN 1 ELSE 0 END) AS auth_count,
       SUM(CASE WHEN event_type = 'REFUND' THEN 1 ELSE 0 END) AS refund_count,
       ROUND(SUM(CASE WHEN event_type = 'REFUND' THEN 1 ELSE 0 END) * 100.0
           / NULLIF(COUNT(*), 0), 2) AS refund_pct
FROM modely
GROUP BY payment_status
ORDER BY cnt DESC;

-- Q45.08 FILTER clause on aggregates (PostgreSQL-specific)
SELECT payment_status,
       COUNT(*) AS total_count,
       COUNT(*) FILTER (WHERE success_flag = TRUE) AS success_count,
       SUM(transaction_amount) FILTER (WHERE event_type = 'AUTHORIZATION') AS auth_total,
       AVG(fee_amount) FILTER (WHERE fee_amount > 0) AS avg_nonzero_fee
FROM modely
GROUP BY payment_status
ORDER BY total_count DESC;

-- Q45.09 Percentage of total using window in derived table
SELECT payment_status,
       total_amount,
       ROUND(total_amount * 100.0 / NULLIF(grand_total, 0), 2) AS pct_of_total
FROM (
    SELECT payment_status,
           SUM(transaction_amount) AS total_amount,
           SUM(SUM(transaction_amount)) OVER () AS grand_total
    FROM modely
    GROUP BY payment_status
) AS pcts
ORDER BY pct_of_total DESC;

-- Q45.10 Moving average and running sum in derived table
SELECT payment_status, total_amount,
       AVG(total_amount) OVER (ORDER BY total_amount ROWS BETWEEN 1 PRECEDING AND 1 FOLLOWING) AS moving_avg_3,
       SUM(total_amount) OVER (ORDER BY total_amount) AS running_sum
FROM (
    SELECT payment_status, SUM(transaction_amount) AS total_amount
    FROM modely
    GROUP BY payment_status
) AS base
ORDER BY total_amount;

-- Q45.11 Dense percentile ranking
SELECT payment_status,
       SUM(transaction_amount) AS total,
       PERCENT_RANK() OVER (ORDER BY SUM(transaction_amount)) AS pct_rank,
       CUME_DIST() OVER (ORDER BY SUM(transaction_amount)) AS cume_dist,
       NTILE(4) OVER (ORDER BY SUM(transaction_amount)) AS quartile
FROM modely
GROUP BY payment_status
ORDER BY total;

-- Q45.12 Gap analysis: difference from previous and next status
SELECT payment_status, total,
       total - LAG(total) OVER (ORDER BY total) AS gap_from_prev,
       LEAD(total) OVER (ORDER BY total) - total AS gap_to_next,
       COALESCE(total - LAG(total) OVER (ORDER BY total), 0)
           + COALESCE(LEAD(total) OVER (ORDER BY total) - total, 0) AS total_gap
FROM (
    SELECT payment_status, SUM(transaction_amount) AS total
    FROM modely
    GROUP BY payment_status
) sub
ORDER BY total;

-- Q45.13 GROUPING SETS: multiple granularities in one pass
SELECT COALESCE(payment_status, '(ALL)') AS payment_status,
       COALESCE(event_type, '(ALL)') AS event_type,
       SUM(transaction_amount) AS total_amount,
       COUNT(*) AS cnt
FROM modely
GROUP BY GROUPING SETS (
    (payment_status, event_type),
    (payment_status),
    ()
)
ORDER BY payment_status NULLS LAST, event_type NULLS LAST;

-- Q45.14 ROLLUP: subtotals and grand total
SELECT COALESCE(payment_status, '(TOTAL)') AS payment_status,
       COALESCE(event_type, '(SUBTOTAL)') AS event_type,
       SUM(transaction_amount) AS total,
       SUM(fee_amount) AS total_fees,
       SUM(fee_amount) / NULLIF(SUM(transaction_amount), 0) AS fee_rate
FROM modely
GROUP BY ROLLUP(payment_status, event_type)
ORDER BY payment_status NULLS LAST, event_type NULLS LAST;

-- Q45.15 CUBE: full cross-tabulation of two dimensions
SELECT COALESCE(payment_status, '(ALL)') AS ps,
       COALESCE(payment_method, '(ALL)') AS pm,
       SUM(transaction_amount) AS total,
       COUNT(*) AS cnt
FROM modely
GROUP BY CUBE(payment_status, payment_method)
ORDER BY ps NULLS LAST, pm NULLS LAST;

-- Q45.16 EXISTS subquery: statuses that have high-value transactions
SELECT DISTINCT payment_status
FROM modely m1
WHERE EXISTS (
    SELECT 1 FROM modely m2
    WHERE m2.payment_status = m1.payment_status
      AND m2.transaction_amount > 5000
)
ORDER BY payment_status;

-- Q45.17 NOT EXISTS: statuses with zero refunds
SELECT DISTINCT payment_status
FROM modely m1
WHERE NOT EXISTS (
    SELECT 1 FROM modely m2
    WHERE m2.payment_status = m1.payment_status
      AND m2.event_type = 'REFUND'
)
ORDER BY payment_status;

-- Q45.18 Lateral join simulation: top-N per group via derived table
SELECT sub.payment_status, sub.total_amount, sub.rn
FROM (
    SELECT payment_status,
           SUM(transaction_amount) AS total_amount,
           ROW_NUMBER() OVER (ORDER BY SUM(transaction_amount) DESC) AS rn
    FROM modely
    GROUP BY payment_status
) sub
WHERE sub.rn <= 3
ORDER BY sub.rn;

-- Q45.19 String aggregation with ordered output
SELECT payment_status,
       STRING_AGG(DISTINCT event_type, ', ' ORDER BY event_type) AS event_types,
       COUNT(DISTINCT event_type) AS distinct_events
FROM modely
GROUP BY payment_status
ORDER BY distinct_events DESC;

-- Q45.20 Mixed CTE pipeline: filter, aggregate, rank, then filter again
WITH base AS (
    SELECT payment_status, event_type, transaction_amount, fee_amount
    FROM modely
    WHERE transaction_amount > 100
),
agg AS (
    SELECT payment_status,
           SUM(transaction_amount) AS total,
           SUM(fee_amount) / NULLIF(SUM(transaction_amount), 0) AS fee_rate,
           COUNT(*) AS cnt
    FROM base
    GROUP BY payment_status
),
ranked AS (
    SELECT *,
           RANK() OVER (ORDER BY total DESC) AS rnk,
           fee_rate - AVG(fee_rate) OVER () AS rate_deviation
    FROM agg
)
SELECT payment_status, total, fee_rate, cnt, rnk, rate_deviation
FROM ranked
WHERE rnk <= 5
ORDER BY rnk;

-- Q45.21 Self-comparison: each status vs overall average
SELECT payment_status,
       SUM(transaction_amount) AS status_total,
       SUM(transaction_amount) - (SELECT AVG(sub.grp_total) FROM (
           SELECT SUM(transaction_amount) AS grp_total FROM modely GROUP BY payment_status
       ) sub) AS diff_from_avg_group
FROM modely
GROUP BY payment_status
ORDER BY diff_from_avg_group DESC;

-- Q45.22 Year-over-year style: same dimension, different filters, single row
SELECT
    (SELECT SUM(transaction_amount) FROM modely WHERE success_flag = TRUE) AS success_total,
    (SELECT SUM(transaction_amount) FROM modely WHERE success_flag = FALSE) AS non_success_total,
    (SELECT SUM(transaction_amount) FROM modely WHERE success_flag = TRUE)
    / NULLIF((SELECT SUM(transaction_amount) FROM modely), 0) AS success_share;

-- Q45.23 Multiple HAVING conditions with compound aggregates
SELECT event_type, payment_status,
       SUM(transaction_amount) AS total,
       SUM(fee_amount) / NULLIF(SUM(transaction_amount), 0) AS fee_rate
FROM modely
GROUP BY event_type, payment_status
HAVING SUM(transaction_amount) > 500
   AND SUM(fee_amount) / NULLIF(SUM(transaction_amount), 0) > 0.001
   AND COUNT(*) >= 5
ORDER BY fee_rate DESC
LIMIT 20;

-- Q45.24 DISTINCT ON (PostgreSQL): first row per group by ordering
SELECT DISTINCT ON (payment_status)
       payment_status,
       event_type,
       transaction_amount
FROM modely
ORDER BY payment_status, transaction_amount DESC;

-- Q45.25 Chained arithmetic: ((A+B)*C - D) / E pattern
SELECT payment_status,
       ((SUM(transaction_amount) + SUM(fee_amount)) * SUM(net_amount)
           - SUM(refund_amount))
       / NULLIF(SUM(settlement_amount), 0) AS composite_metric
FROM modely
GROUP BY payment_status
HAVING SUM(settlement_amount) > 0
ORDER BY composite_metric DESC;
