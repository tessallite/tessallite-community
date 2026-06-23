SELECT `country_code` AS `country_code`, (`risk_score__sum` * 1.0 / NULLIF(`risk_score__count`, 0)) AS `risk_score` FROM `aggregates`.`agg_golden`
