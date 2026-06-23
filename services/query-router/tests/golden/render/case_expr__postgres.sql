SELECT CASE WHEN amount > 0 THEN 'pos' ELSE 'neg' END AS sign, COUNT(*) AS n FROM "demo"."sales" GROUP BY CASE WHEN amount > 0 THEN 'pos' ELSE 'neg' END
