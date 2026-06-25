SELECT `f`.`region` AS `region`, SUM(`f`.`amount`) AS `amount` FROM `demo`.`sales` AS `f` GROUP BY `region` ORDER BY `region` ASC NULLS LAST LIMIT 10 OFFSET 5
