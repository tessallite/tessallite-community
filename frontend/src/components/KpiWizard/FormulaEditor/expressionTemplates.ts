/**
 * KPI expression templates -- preset starting points for common patterns.
 *
 * The `?` placeholders prompt the user to fill in a measure or KPI name.
 */

export interface ExpressionTemplate {
  id: string;
  labelKey: string;
  labelFallback: string;
  expression: string;
  placeholderCount: number;
}

export const EXPRESSION_TEMPLATES: ExpressionTemplate[] = [
  {
    id: "simple",
    labelKey: "kpis.formula.tpl.simple",
    labelFallback: "Simple measure",
    expression: 'measure("?")',
    placeholderCount: 1,
  },
  {
    id: "ratio",
    labelKey: "kpis.formula.tpl.ratio",
    labelFallback: "Safe ratio",
    expression: 'safe_div(measure("?"), measure("?"))',
    placeholderCount: 2,
  },
  {
    id: "variance",
    labelKey: "kpis.formula.tpl.variance",
    labelFallback: "Value vs comparison",
    expression: 'measure("?") - measure("?")',
    placeholderCount: 2,
  },
  {
    id: "growth",
    labelKey: "kpis.formula.tpl.growth",
    labelFallback: "Month-over-month growth",
    expression: 'pct_change(measure("?"), "month")',
    placeholderCount: 1,
  },
  {
    id: "moving_avg",
    labelKey: "kpis.formula.tpl.movingAvg",
    labelFallback: "Rolling 3-month average",
    expression: 'moving_avg(measure("?"), "month", literal(3))',
    placeholderCount: 1,
  },
  {
    id: "yoy",
    labelKey: "kpis.formula.tpl.yoy",
    labelFallback: "Year-over-year growth",
    expression: 'pct_change(measure("?"), "year")',
    placeholderCount: 1,
  },
  {
    id: "ptd",
    labelKey: "kpis.formula.tpl.ptd",
    labelFallback: "Year-to-date total",
    expression: 'period_to_date(measure("?"), "year")',
    placeholderCount: 1,
  },
  {
    id: "trailing",
    labelKey: "kpis.formula.tpl.trailing",
    labelFallback: "Trailing 12-month sum",
    expression: 'trailing_sum(measure("?"), literal(12), "month")',
    placeholderCount: 1,
  },
  {
    id: "conditional",
    labelKey: "kpis.formula.tpl.conditional",
    labelFallback: "Conditional value",
    expression: 'if_then_else(measure("?") > literal(0), measure("?"), literal(0))',
    placeholderCount: 2,
  },
];
