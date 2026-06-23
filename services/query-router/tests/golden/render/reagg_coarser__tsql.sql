SELECT [country_code] AS [country_code], SUM([amount__sum]) AS [amount] FROM [aggregates].[agg_golden] GROUP BY [country_code]
