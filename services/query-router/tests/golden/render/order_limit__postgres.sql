SELECT "f"."region" AS "region", SUM("f"."amount") AS "amount" FROM "demo"."sales" AS "f" GROUP BY "f"."region" ORDER BY "region" ASC LIMIT 10 OFFSET 5
