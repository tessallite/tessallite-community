SELECT `f`.`region` AS `region`, SUM(`f`.`amount`) AS `total` FROM `demo`.`sales` AS `f` GROUP BY `f`.`region`
