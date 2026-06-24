SELECT `d`.`country_name` AS `country_name`, SUM(`f`.`amount`) AS `amount` FROM `demo`.`sales` AS `f` INNER JOIN `demo`.`country` AS `d` ON `f`.`country_id` = `d`.`id` GROUP BY `d`.`country_name`
