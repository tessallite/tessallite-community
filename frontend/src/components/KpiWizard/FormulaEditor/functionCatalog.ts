/**
 * KPI DSL function catalog -- metadata for all supported functions.
 *
 * Used by the function picker sidebar and autocomplete provider.
 * Pure data, no React imports.
 *
 * Bug-7243: this catalog is the consumer of the backend FUNCTION_REGISTRY
 * (shared/semantic/kpi_expression.py). Every function the backend registers via
 * _reg(...) must appear here or the advanced picker hides it from users. Keep the
 * two in lockstep — functionCatalog.test.ts pins the full name set.
 */

export type DslFunctionCategory =
  | "references"
  | "safe_division"
  | "arithmetic"
  | "aggregation"
  | "analytics"
  | "conditional"
  | "period_comparison"
  | "accumulation"
  | "growth";

export interface DslParamDef {
  name: string;
  type: "string" | "number" | "expression" | "grain" | "measure_ref" | "kpi_ref";
  description: string;
}

export interface DslFunctionDef {
  name: string;
  category: DslFunctionCategory;
  signature: string;
  descriptionKey: string;
  descriptionFallback: string;
  parameters: DslParamDef[];
  example: string;
  insertSnippet: string;
}

export const CATEGORY_ORDER: DslFunctionCategory[] = [
  "references",
  "safe_division",
  "arithmetic",
  "aggregation",
  "analytics",
  "conditional",
  "period_comparison",
  "accumulation",
  "growth",
];

export const CATEGORY_LABELS: Record<DslFunctionCategory, { key: string; fallback: string }> = {
  references:        { key: "kpis.formula.categoryReferences",       fallback: "References" },
  safe_division:     { key: "kpis.formula.categorySafeDivision",     fallback: "Safe Division" },
  arithmetic:        { key: "kpis.formula.categoryArithmetic",       fallback: "Arithmetic" },
  aggregation:       { key: "kpis.formula.categoryAggregation",      fallback: "Aggregation" },
  analytics:         { key: "kpis.formula.categoryAnalytics",        fallback: "Analytics" },
  conditional:       { key: "kpis.formula.categoryConditional",      fallback: "Conditional" },
  period_comparison: { key: "kpis.formula.categoryPeriodComparison", fallback: "Period Comparison" },
  accumulation:      { key: "kpis.formula.categoryAccumulation",     fallback: "Accumulation" },
  growth:            { key: "kpis.formula.categoryGrowth",           fallback: "Growth" },
};

export const DSL_FUNCTIONS: DslFunctionDef[] = [
  // --- References ---
  {
    name: "measure",
    category: "references",
    signature: 'measure(name)',
    descriptionKey: "kpis.formula.fn.measure.desc",
    descriptionFallback: "Reference a measure from the model by name.",
    parameters: [{ name: "name", type: "measure_ref", description: "Measure name" }],
    example: 'measure("Revenue")  ->  SUM of Revenue column',
    insertSnippet: 'measure("$1")',
  },
  {
    name: "kpi",
    category: "references",
    signature: 'kpi(name)',
    descriptionKey: "kpis.formula.fn.kpi.desc",
    descriptionFallback: "Reference another KPI by name. Creates a dependency.",
    parameters: [{ name: "name", type: "kpi_ref", description: "KPI name" }],
    example: 'kpi("Conversion Rate")  ->  value of Conversion Rate KPI',
    insertSnippet: 'kpi("$1")',
  },
  {
    name: "literal",
    category: "references",
    signature: "literal(value)",
    descriptionKey: "kpis.formula.fn.literal.desc",
    descriptionFallback: "A constant numeric value.",
    parameters: [{ name: "value", type: "number", description: "Numeric constant" }],
    example: "literal(100)  ->  100",
    insertSnippet: "literal($1)",
  },
  {
    name: "dimension",
    category: "references",
    signature: "dimension(name)",
    descriptionKey: "kpis.formula.fn.dimension.desc",
    descriptionFallback: "Reference a dimension attribute from the model by name.",
    parameters: [{ name: "name", type: "string", description: "Dimension name" }],
    example: 'dimension("Region")  ->  the Region dimension attribute',
    insertSnippet: 'dimension("$1")',
  },

  // --- Safe Division ---
  {
    name: "safe_div",
    category: "safe_division",
    signature: "safe_div(numerator, denominator)",
    descriptionKey: "kpis.formula.fn.safe_div.desc",
    descriptionFallback: "Divide safely. Returns blank if the denominator is zero or missing.",
    parameters: [
      { name: "numerator", type: "expression", description: "Top of the fraction" },
      { name: "denominator", type: "expression", description: "Bottom of the fraction" },
    ],
    example: 'safe_div(measure("Profit"), measure("Revenue"))  ->  0.15  (or blank if Revenue = 0)',
    insertSnippet: "safe_div($1, $2)",
  },
  {
    name: "safe_ratio",
    category: "safe_division",
    signature: "safe_ratio(numerator, denominator)",
    descriptionKey: "kpis.formula.fn.safe_ratio.desc",
    descriptionFallback: "Same as safe_div. Divide safely, returning blank on zero denominator.",
    parameters: [
      { name: "numerator", type: "expression", description: "Top of the fraction" },
      { name: "denominator", type: "expression", description: "Bottom of the fraction" },
    ],
    example: 'safe_ratio(measure("Orders"), measure("Visits"))  ->  0.042',
    insertSnippet: "safe_ratio($1, $2)",
  },
  {
    name: "div",
    category: "safe_division",
    signature: "div(numerator, denominator, fallback)",
    descriptionKey: "kpis.formula.fn.div.desc",
    descriptionFallback: "Divide with an explicit fallback value when the denominator is zero.",
    parameters: [
      { name: "numerator", type: "expression", description: "Top of the fraction" },
      { name: "denominator", type: "expression", description: "Bottom of the fraction" },
      { name: "fallback", type: "expression", description: "Value to return when denominator is zero" },
    ],
    example: 'div(measure("A"), measure("B"), literal(0))  ->  A/B or 0',
    insertSnippet: "div($1, $2, $3)",
  },

  // --- Arithmetic ---
  {
    name: "abs",
    category: "arithmetic",
    signature: "abs(value)",
    descriptionKey: "kpis.formula.fn.abs.desc",
    descriptionFallback: "Absolute value. Removes the negative sign.",
    parameters: [{ name: "value", type: "expression", description: "Numeric expression" }],
    example: "abs(literal(-5))  ->  5",
    insertSnippet: "abs($1)",
  },
  {
    name: "round",
    category: "arithmetic",
    signature: "round(value, decimals)",
    descriptionKey: "kpis.formula.fn.round.desc",
    descriptionFallback: "Round to a specified number of decimal places.",
    parameters: [
      { name: "value", type: "expression", description: "Number to round" },
      { name: "decimals", type: "number", description: "Decimal places" },
    ],
    example: "round(literal(3.14159), literal(2))  ->  3.14",
    insertSnippet: "round($1, literal($2))",
  },
  {
    name: "min_of",
    category: "arithmetic",
    signature: "min_of(a, b)",
    descriptionKey: "kpis.formula.fn.min_of.desc",
    descriptionFallback: "Return the smaller of two values.",
    parameters: [
      { name: "a", type: "expression", description: "First value" },
      { name: "b", type: "expression", description: "Second value" },
    ],
    example: "min_of(literal(10), literal(20))  ->  10",
    insertSnippet: "min_of($1, $2)",
  },
  {
    name: "max_of",
    category: "arithmetic",
    signature: "max_of(a, b)",
    descriptionKey: "kpis.formula.fn.max_of.desc",
    descriptionFallback: "Return the larger of two values.",
    parameters: [
      { name: "a", type: "expression", description: "First value" },
      { name: "b", type: "expression", description: "Second value" },
    ],
    example: "max_of(literal(10), literal(20))  ->  20",
    insertSnippet: "max_of($1, $2)",
  },
  {
    name: "coalesce",
    category: "arithmetic",
    signature: "coalesce(a, b, ...)",
    descriptionKey: "kpis.formula.fn.coalesce.desc",
    descriptionFallback: "Return the first non-blank value from the arguments.",
    parameters: [
      { name: "a", type: "expression", description: "First candidate" },
      { name: "b", type: "expression", description: "Fallback candidate" },
    ],
    example: 'coalesce(measure("Revenue"), literal(0))  ->  Revenue or 0',
    insertSnippet: "coalesce($1, $2)",
  },

  // --- Aggregation ---
  {
    name: "sum",
    category: "aggregation",
    signature: "sum(expression)",
    descriptionKey: "kpis.formula.fn.sum.desc",
    descriptionFallback: "Sum the expression across the current grouping.",
    parameters: [{ name: "expression", type: "expression", description: "Value to sum" }],
    example: 'sum(measure("Revenue"))  ->  total Revenue',
    insertSnippet: "sum($1)",
  },
  {
    name: "avg",
    category: "aggregation",
    signature: "avg(expression)",
    descriptionKey: "kpis.formula.fn.avg.desc",
    descriptionFallback: "Average the expression across the current grouping.",
    parameters: [{ name: "expression", type: "expression", description: "Value to average" }],
    example: 'avg(measure("Order Value"))  ->  average order value',
    insertSnippet: "avg($1)",
  },
  {
    name: "min",
    category: "aggregation",
    signature: "min(expression)",
    descriptionKey: "kpis.formula.fn.min.desc",
    descriptionFallback: "Smallest value of the expression across the current grouping.",
    parameters: [{ name: "expression", type: "expression", description: "Value to reduce" }],
    example: 'min(measure("Price"))  ->  lowest price',
    insertSnippet: "min($1)",
  },
  {
    name: "max",
    category: "aggregation",
    signature: "max(expression)",
    descriptionKey: "kpis.formula.fn.max.desc",
    descriptionFallback: "Largest value of the expression across the current grouping.",
    parameters: [{ name: "expression", type: "expression", description: "Value to reduce" }],
    example: 'max(measure("Price"))  ->  highest price',
    insertSnippet: "max($1)",
  },
  {
    name: "count",
    category: "aggregation",
    signature: "count(expression?)",
    descriptionKey: "kpis.formula.fn.count.desc",
    descriptionFallback: "Count rows, or non-blank values of the optional expression.",
    parameters: [
      { name: "expression", type: "expression", description: "Optional value to count" },
    ],
    example: 'count(measure("Orders"))  ->  number of orders',
    insertSnippet: "count($1)",
  },
  {
    name: "count_distinct",
    category: "aggregation",
    signature: "count_distinct(expression)",
    descriptionKey: "kpis.formula.fn.count_distinct.desc",
    descriptionFallback: "Count the distinct values of the expression.",
    parameters: [
      { name: "expression", type: "expression", description: "Value to count distinctly" },
    ],
    example: 'count_distinct(dimension("Customer"))  ->  unique customers',
    insertSnippet: "count_distinct($1)",
  },

  // --- Analytics ---
  {
    name: "share_of_total",
    category: "analytics",
    signature: "share_of_total(expression)",
    descriptionKey: "kpis.formula.fn.share_of_total.desc",
    descriptionFallback: "The expression's share of the overall total (0-1).",
    parameters: [
      { name: "expression", type: "expression", description: "Value to compare to the total" },
    ],
    example: 'share_of_total(measure("Revenue"))  ->  0.18  (18% of total Revenue)',
    insertSnippet: "share_of_total($1)",
  },
  {
    name: "rank_over",
    category: "analytics",
    signature: "rank_over(expression)",
    descriptionKey: "kpis.formula.fn.rank_over.desc",
    descriptionFallback: "Rank of the expression within the current grouping (1 = highest).",
    parameters: [
      { name: "expression", type: "expression", description: "Value to rank" },
    ],
    example: 'rank_over(measure("Revenue"))  ->  1, 2, 3 ...',
    insertSnippet: "rank_over($1)",
  },

  // --- Conditional ---
  {
    name: "if_then_else",
    category: "conditional",
    signature: "if_then_else(condition, then_value, else_value)",
    descriptionKey: "kpis.formula.fn.if_then_else.desc",
    descriptionFallback: "Choose between two values based on a condition.",
    parameters: [
      { name: "condition", type: "expression", description: "Boolean condition (e.g. a > b)" },
      { name: "then_value", type: "expression", description: "Value when condition is true" },
      { name: "else_value", type: "expression", description: "Value when condition is false" },
    ],
    example: 'if_then_else(measure("X") > literal(0), measure("X"), literal(0))',
    insertSnippet: "if_then_else($1 > $2, $3, $4)",
  },
  {
    name: "sla_condition",
    category: "conditional",
    signature: 'sla_condition(value, comparator, threshold, then_value, else_value)',
    descriptionKey: "kpis.formula.fn.sla_condition.desc",
    descriptionFallback:
      "Compare a value against a threshold with an explicit comparator and return one of two values (used for SLA pass/fail scoring).",
    parameters: [
      { name: "value", type: "expression", description: "Value to test" },
      { name: "comparator", type: "string", description: 'Comparator: ">=", "<=", ">", "<", "==", "!="' },
      { name: "threshold", type: "expression", description: "Threshold to compare against" },
      { name: "then_value", type: "expression", description: "Value when the comparison holds" },
      { name: "else_value", type: "expression", description: "Value when it does not" },
    ],
    example:
      'sla_condition(measure("Uptime"), ">=", literal(0.99), literal(1), literal(0))',
    insertSnippet: 'sla_condition($1, "$2", $3, $4, $5)',
  },

  // --- Period Comparison ---
  {
    name: "prior_period",
    category: "period_comparison",
    signature: 'prior_period(expression, grain)',
    descriptionKey: "kpis.formula.fn.prior_period.desc",
    descriptionFallback: "Get the value from the previous time period.",
    parameters: [
      { name: "expression", type: "expression", description: "Value to compare" },
      { name: "grain", type: "grain", description: "Period grain: day, week, month, quarter, year" },
    ],
    example: 'prior_period(measure("Revenue"), "month")  ->  last month\'s Revenue',
    insertSnippet: 'prior_period($1, "$2")',
  },
  {
    name: "pct_change",
    category: "period_comparison",
    signature: 'pct_change(expression, grain)',
    descriptionKey: "kpis.formula.fn.pct_change.desc",
    descriptionFallback: "Percentage change vs the previous period.",
    parameters: [
      { name: "expression", type: "expression", description: "Value to compare" },
      { name: "grain", type: "grain", description: "Period grain: day, week, month, quarter, year" },
    ],
    example: 'pct_change(measure("Revenue"), "month")  ->  0.12 (12% growth)',
    insertSnippet: 'pct_change($1, "$2")',
  },
  {
    name: "lag",
    category: "period_comparison",
    signature: "lag(expression, n, grain)",
    descriptionKey: "kpis.formula.fn.lag.desc",
    descriptionFallback: "Get the value from N periods ago.",
    parameters: [
      { name: "expression", type: "expression", description: "Value to look back" },
      { name: "n", type: "number", description: "Number of periods" },
      { name: "grain", type: "grain", description: "Period grain" },
    ],
    example: 'lag(measure("Revenue"), literal(3), "month")  ->  Revenue 3 months ago',
    insertSnippet: 'lag($1, literal($2), "$3")',
  },
  {
    name: "lead",
    category: "period_comparison",
    signature: "lead(expression, n, grain)",
    descriptionKey: "kpis.formula.fn.lead.desc",
    descriptionFallback: "Get the value from N periods ahead (forecast).",
    parameters: [
      { name: "expression", type: "expression", description: "Value to look ahead" },
      { name: "n", type: "number", description: "Number of periods" },
      { name: "grain", type: "grain", description: "Period grain" },
    ],
    example: 'lead(measure("Revenue"), literal(1), "month")  ->  next month\'s Revenue',
    insertSnippet: 'lead($1, literal($2), "$3")',
  },
  {
    name: "fiscal_period_to_date",
    category: "period_comparison",
    signature: 'fiscal_period_to_date(expression, grain)',
    descriptionKey: "kpis.formula.fn.fiscal_period_to_date.desc",
    descriptionFallback: "Accumulate from fiscal year start to the current date.",
    parameters: [
      { name: "expression", type: "expression", description: "Value to accumulate" },
      { name: "grain", type: "grain", description: "Fiscal period grain" },
    ],
    example: 'fiscal_period_to_date(measure("Revenue"), "year")  ->  fiscal YTD Revenue',
    insertSnippet: 'fiscal_period_to_date($1, "$2")',
  },

  // --- Accumulation ---
  {
    name: "period_to_date",
    category: "accumulation",
    signature: 'period_to_date(expression, grain)',
    descriptionKey: "kpis.formula.fn.period_to_date.desc",
    descriptionFallback: "Running total from the start of the period to the current date.",
    parameters: [
      { name: "expression", type: "expression", description: "Value to accumulate" },
      { name: "grain", type: "grain", description: "Period grain: month, quarter, year" },
    ],
    example: 'period_to_date(measure("Revenue"), "year")  ->  YTD Revenue',
    insertSnippet: 'period_to_date($1, "$2")',
  },
  {
    name: "moving_avg",
    category: "accumulation",
    signature: "moving_avg(expression, grain, window_size)",
    descriptionKey: "kpis.formula.fn.moving_avg.desc",
    descriptionFallback: "Average over the last N periods.",
    parameters: [
      { name: "expression", type: "expression", description: "Value to average" },
      { name: "grain", type: "grain", description: "Period grain" },
      { name: "window_size", type: "number", description: "Number of periods" },
    ],
    example: 'moving_avg(measure("Revenue"), "month", literal(3))  ->  3-month rolling average',
    insertSnippet: 'moving_avg($1, "$2", literal($3))',
  },
  {
    name: "trailing_sum",
    category: "accumulation",
    signature: "trailing_sum(expression, n, grain)",
    descriptionKey: "kpis.formula.fn.trailing_sum.desc",
    descriptionFallback: "Sum over the last N periods.",
    parameters: [
      { name: "expression", type: "expression", description: "Value to sum" },
      { name: "n", type: "number", description: "Number of periods" },
      { name: "grain", type: "grain", description: "Period grain" },
    ],
    example: 'trailing_sum(measure("Revenue"), literal(12), "month")  ->  trailing 12-month total',
    insertSnippet: 'trailing_sum($1, literal($2), "$3")',
  },

  // --- Growth ---
  {
    name: "cagr",
    category: "growth",
    signature: "cagr(expression, years)",
    descriptionKey: "kpis.formula.fn.cagr.desc",
    descriptionFallback: "Compound annual growth rate of a measure over N years using time-series data.",
    parameters: [
      { name: "expression", type: "expression", description: "Measure or expression to compute CAGR for" },
      { name: "years", type: "number", description: "Number of years to look back" },
    ],
    example: 'cagr(measure("Revenue"), 3)  ->  CAGR over 3 years',
    insertSnippet: 'cagr(measure("$1"), $2)',
  },
];

/** All known function names for tokenizer keyword list. */
export const DSL_FUNCTION_NAMES: string[] = DSL_FUNCTIONS.map((f) => f.name);
