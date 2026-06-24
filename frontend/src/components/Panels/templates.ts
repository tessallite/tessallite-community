export interface KpiTemplate {
  name: string;
  display_name: string;
  description: string;
  display_folder: string;
  expression: string;
  kpi_type: "simple_measure" | "ratio" | "variance" | "growth_rate" | "moving_window" | "composite";
  direction: "higher_is_better" | "lower_is_better" | "closer_is_better";
  status_graphic: string;
  trend_graphic: string;
  category: string;
}

export interface NamedSetTemplate {
  name: string;
  display_name: string;
  description: string;
  display_folder: string;
  set_expression: string;
  builder_type: "fixed" | "dynamic_top_n" | "filtered" | "advanced_mdx";
  category: string;
}

export const KPI_TEMPLATES: KpiTemplate[] = [
  // ---- Financial ----
  {
    name: "revenue_vs_target",
    display_name: "Revenue vs Target",
    description: "Tracks actual revenue against the target. Green when on track, red when below 80%.",
    display_folder: "Financial",
    expression: 'safe_div(measure("Revenue"), measure("Revenue Target"))',
    kpi_type: "ratio",
    direction: "higher_is_better",
    status_graphic: "Traffic Light",
    trend_graphic: "Standard Arrow",
    category: "Financial",
  },
  {
    name: "gross_margin_pct",
    display_name: "Gross Margin %",
    description: "Gross profit as a percentage of revenue. Alerts when margin drops below target.",
    display_folder: "Financial",
    expression: 'safe_div(measure("Gross Profit"), measure("Revenue"))',
    kpi_type: "ratio",
    direction: "higher_is_better",
    status_graphic: "Gauge",
    trend_graphic: "Standard Arrow",
    category: "Financial",
  },
  {
    name: "cost_variance",
    display_name: "Cost Variance",
    description: "Actual cost vs budgeted cost. Green when under budget, red when over.",
    display_folder: "Financial",
    expression: 'measure("Budgeted Cost") - measure("Actual Cost")',
    kpi_type: "variance",
    direction: "higher_is_better",
    status_graphic: "Reversed Gauge",
    trend_graphic: "Standard Arrow",
    category: "Financial",
  },
  {
    name: "operating_profit",
    display_name: "Operating Profit",
    description: "Operating profit vs target. Tracks profitability after operating expenses.",
    display_folder: "Financial",
    expression: 'measure("Operating Profit")',
    kpi_type: "simple_measure",
    direction: "higher_is_better",
    status_graphic: "Traffic Light",
    trend_graphic: "Standard Arrow",
    category: "Financial",
  },

  // ---- Sales ----
  {
    name: "sales_pipeline",
    display_name: "Sales Pipeline",
    description: "Pipeline value vs quota. Monitors sales funnel health.",
    display_folder: "Sales",
    expression: 'safe_div(measure("Pipeline Value"), measure("Sales Quota"))',
    kpi_type: "ratio",
    direction: "higher_is_better",
    status_graphic: "Thermometer",
    trend_graphic: "Standard Arrow",
    category: "Sales",
  },
  {
    name: "conversion_rate",
    display_name: "Conversion Rate",
    description: "Lead-to-customer conversion rate vs target. Key sales efficiency metric.",
    display_folder: "Sales",
    expression: 'safe_div(measure("Conversions"), measure("Leads"))',
    kpi_type: "ratio",
    direction: "higher_is_better",
    status_graphic: "Traffic Light",
    trend_graphic: "Standard Arrow",
    category: "Sales",
  },
  {
    name: "average_deal_size",
    display_name: "Average Deal Size",
    description: "Average revenue per closed deal vs target.",
    display_folder: "Sales",
    expression: 'safe_div(measure("Revenue"), measure("Deals Closed"))',
    kpi_type: "ratio",
    direction: "higher_is_better",
    status_graphic: "Traffic Light",
    trend_graphic: "Standard Arrow",
    category: "Sales",
  },

  // ---- Customer ----
  {
    name: "customer_satisfaction",
    display_name: "Customer Satisfaction (CSAT)",
    description: "Customer satisfaction score vs target. Alerts when below threshold.",
    display_folder: "Customer",
    expression: 'measure("CSAT Score")',
    kpi_type: "simple_measure",
    direction: "higher_is_better",
    status_graphic: "Smiley Face",
    trend_graphic: "Standard Arrow",
    category: "Customer",
  },
  {
    name: "churn_rate",
    display_name: "Churn Rate",
    description: "Customer churn rate. Lower is better — green when below target.",
    display_folder: "Customer",
    expression: 'measure("Churn Rate")',
    kpi_type: "simple_measure",
    direction: "lower_is_better",
    status_graphic: "Reversed Gauge",
    trend_graphic: "Standard Arrow",
    category: "Customer",
  },
  {
    name: "nps_score",
    display_name: "Net Promoter Score (NPS)",
    description: "NPS score vs target. Tracks customer loyalty and advocacy.",
    display_folder: "Customer",
    expression: 'measure("NPS")',
    kpi_type: "simple_measure",
    direction: "higher_is_better",
    status_graphic: "Smiley Face",
    trend_graphic: "Standard Arrow",
    category: "Customer",
  },

  // ---- Operations ----
  {
    name: "on_time_delivery",
    display_name: "On-Time Delivery %",
    description: "Percentage of orders delivered on time. Target: 95%+.",
    display_folder: "Operations",
    expression: 'safe_div(measure("On Time Deliveries"), measure("Total Deliveries"))',
    kpi_type: "ratio",
    direction: "higher_is_better",
    status_graphic: "Traffic Light",
    trend_graphic: "Standard Arrow",
    category: "Operations",
  },
  {
    name: "inventory_turnover",
    display_name: "Inventory Turnover",
    description: "How often inventory is sold and replaced. Higher is generally better.",
    display_folder: "Operations",
    expression: 'safe_div(measure("Cost of Goods Sold"), measure("Average Inventory"))',
    kpi_type: "ratio",
    direction: "higher_is_better",
    status_graphic: "Cylinder",
    trend_graphic: "Standard Arrow",
    category: "Operations",
  },

  // ---- HR / People ----
  {
    name: "employee_retention",
    display_name: "Employee Retention Rate",
    description: "Percentage of employees retained over the period. Higher is better.",
    display_folder: "People",
    expression: 'safe_div(measure("Retained Employees"), measure("Total Employees"))',
    kpi_type: "ratio",
    direction: "higher_is_better",
    status_graphic: "Traffic Light",
    trend_graphic: "Standard Arrow",
    category: "People",
  },
  {
    name: "headcount_vs_plan",
    display_name: "Headcount vs Plan",
    description: "Actual headcount vs planned headcount. Tracks hiring progress.",
    display_folder: "People",
    expression: 'safe_div(measure("Headcount"), measure("Planned Headcount"))',
    kpi_type: "ratio",
    direction: "higher_is_better",
    status_graphic: "Traffic Light",
    trend_graphic: "Standard Arrow",
    category: "People",
  },
];

export const NAMED_SET_TEMPLATES: NamedSetTemplate[] = [
  // ---- Time Intelligence ----
  {
    name: "last_12_months",
    display_name: "Last 12 Months",
    description: "Rolling 12-month window from the current month. Automatically shifts forward.",
    display_folder: "Time Intelligence",
    set_expression:
      "LastPeriods(12, [Date].[Calendar].[Month].&[CurrentMonth])",
    builder_type: "advanced_mdx",
    category: "Time Intelligence",
  },
  {
    name: "year_to_date",
    display_name: "Year to Date",
    description: "All periods from the start of the current year to now.",
    display_folder: "Time Intelligence",
    set_expression:
      "PeriodsToDate([Date].[Calendar].[Year], [Date].[Calendar].CurrentMember)",
    builder_type: "advanced_mdx",
    category: "Time Intelligence",
  },
  {
    name: "same_period_last_year",
    display_name: "Same Period Last Year",
    description: "The equivalent period from the prior year, for year-over-year comparison.",
    display_folder: "Time Intelligence",
    set_expression:
      "ParallelPeriod([Date].[Calendar].[Year], 1, [Date].[Calendar].CurrentMember)",
    builder_type: "advanced_mdx",
    category: "Time Intelligence",
  },
  {
    name: "quarter_to_date",
    display_name: "Quarter to Date",
    description: "All periods from the start of the current quarter to now.",
    display_folder: "Time Intelligence",
    set_expression:
      "PeriodsToDate([Date].[Calendar].[Quarter], [Date].[Calendar].CurrentMember)",
    builder_type: "advanced_mdx",
    category: "Time Intelligence",
  },
  {
    name: "trailing_30_days",
    display_name: "Trailing 30 Days",
    description: "Rolling 30-day window ending at the current date.",
    display_folder: "Time Intelligence",
    set_expression:
      "LastPeriods(30, [Date].[Calendar].[Date].&[Today])",
    builder_type: "advanced_mdx",
    category: "Time Intelligence",
  },

  // ---- Product / Category ----
  {
    name: "top_10_products_by_revenue",
    display_name: "Top 10 Products by Revenue",
    description: "The 10 products generating the most revenue. Dynamic — recalculates on each query.",
    display_folder: "Product Analysis",
    set_expression:
      "TopCount([Product].[Product Name].Members, 10, [Measures].[Revenue])",
    builder_type: "advanced_mdx",
    category: "Product",
  },
  {
    name: "bottom_5_products_by_units",
    display_name: "Bottom 5 Products by Units Sold",
    description: "The 5 lowest-selling products by unit volume. Useful for discontinuation analysis.",
    display_folder: "Product Analysis",
    set_expression:
      "BottomCount([Product].[Product Name].Members, 5, [Measures].[Units Sold])",
    builder_type: "advanced_mdx",
    category: "Product",
  },
  {
    name: "products_above_average_margin",
    display_name: "Products Above Average Margin",
    description: "Products whose margin exceeds the overall average. Identifies high performers.",
    display_folder: "Product Analysis",
    set_expression:
      "Filter([Product].[Product Name].Members, [Measures].[Margin Pct] > Avg([Product].[Product Name].Members, [Measures].[Margin Pct]))",
    builder_type: "advanced_mdx",
    category: "Product",
  },

  // ---- Geography ----
  {
    name: "top_5_regions_by_revenue",
    display_name: "Top 5 Regions by Revenue",
    description: "The 5 highest-revenue regions. Useful for regional sales dashboards.",
    display_folder: "Geography",
    set_expression:
      "TopCount([Geography].[Region].Members, 5, [Measures].[Revenue])",
    builder_type: "advanced_mdx",
    category: "Geography",
  },
  {
    name: "underperforming_regions",
    display_name: "Underperforming Regions",
    description: "Regions where revenue is below 80% of their target.",
    display_folder: "Geography",
    set_expression:
      "Filter([Geography].[Region].Members, [Measures].[Revenue] < [Measures].[Revenue Target] * 0.8)",
    builder_type: "advanced_mdx",
    category: "Geography",
  },

  // ---- Customer Segmentation ----
  {
    name: "top_20_customers",
    display_name: "Top 20 Customers",
    description: "The 20 customers with the highest total revenue.",
    display_folder: "Customer Segments",
    set_expression:
      "TopCount([Customer].[Customer Name].Members, 20, [Measures].[Revenue])",
    builder_type: "advanced_mdx",
    category: "Customer",
  },
  {
    name: "new_customers_this_quarter",
    display_name: "New Customers This Quarter",
    description: "Customers whose first order date falls within the current quarter.",
    display_folder: "Customer Segments",
    set_expression:
      "Filter([Customer].[Customer Name].Members, [Measures].[First Order Date] >= [Date].[Calendar].CurrentMember.Parent.FirstChild)",
    builder_type: "advanced_mdx",
    category: "Customer",
  },
  {
    name: "high_value_accounts",
    display_name: "High Value Accounts",
    description: "Customers with lifetime revenue in the top 10% percentile.",
    display_folder: "Customer Segments",
    set_expression:
      "Filter([Customer].[Customer Name].Members, [Measures].[Lifetime Revenue] >= TopCount([Customer].[Customer Name].Members, 1, [Measures].[Lifetime Revenue]).Item(0).Item(0) * 0.1)",
    builder_type: "advanced_mdx",
    category: "Customer",
  },

  // ---- Inventory / Supply Chain ----
  {
    name: "low_stock_items",
    display_name: "Low Stock Items",
    description: "Products with inventory below the reorder point.",
    display_folder: "Inventory",
    set_expression:
      "Filter([Product].[Product Name].Members, [Measures].[Stock On Hand] < [Measures].[Reorder Point])",
    builder_type: "advanced_mdx",
    category: "Operations",
  },
  {
    name: "slow_moving_inventory",
    display_name: "Slow Moving Inventory",
    description: "Products with fewer than 5 units sold in the last 90 days.",
    display_folder: "Inventory",
    set_expression:
      "Filter([Product].[Product Name].Members, (Sum(LastPeriods(90, [Date].[Calendar].[Date].&[Today]), [Measures].[Units Sold])) < 5)",
    builder_type: "advanced_mdx",
    category: "Operations",
  },
];

export const KPI_CATEGORIES = [...new Set(KPI_TEMPLATES.map((t) => t.category))];
export const NS_CATEGORIES = [...new Set(NAMED_SET_TEMPLATES.map((t) => t.category))];
