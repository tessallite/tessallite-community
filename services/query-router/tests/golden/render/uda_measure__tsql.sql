SELECT [f].[region] AS [region], SUM(([f].[amount] - [f].[qty])) AS [profit] FROM [demo].[sales] AS [f] GROUP BY [f].[region]
