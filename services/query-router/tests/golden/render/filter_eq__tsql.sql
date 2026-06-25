SELECT [f].[region] AS [region], SUM([f].[amount]) AS [amount] FROM [demo].[sales] AS [f] WHERE [f].[region] = 'EMEA' GROUP BY [f].[region]
