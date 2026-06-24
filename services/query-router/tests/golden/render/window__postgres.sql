SELECT region, SUM(amount) OVER (PARTITION BY region) AS running FROM "demo"."sales"
