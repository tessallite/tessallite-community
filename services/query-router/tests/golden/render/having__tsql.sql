SELECT [f].[region] AS [region], SUM([f].[amount]) AS [amount] FROM [demo].[sales] AS [f] GROUP BY [f].[region] HAVING SUM([f].[amount]) > 100
