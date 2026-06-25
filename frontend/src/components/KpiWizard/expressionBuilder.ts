/**
 * Builds a KPI DSL expression string from wizard form inputs.
 *
 * Each KPI type has a canonical expression pattern. The wizard generates
 * the expression automatically; the user can then edit it in the formula
 * editor if needed.
 */
import type { KpiType } from "../../api/types";
import type { KpiWizardFormState } from "./types";

function q(name: string): string {
  return `"${name.replace(/"/g, '""')}"`;
}

/**
 * Build an expression from the wizard form state.
 * Returns an empty string if the form doesn't have enough inputs.
 */
export function buildExpression(form: KpiWizardFormState, measures: { id: string; name: string }[]): string {
  const kpiType = form.kpi_type as KpiType;
  if (!kpiType) return "";

  const measureName = (id: string) => {
    const m = measures.find((x) => x.id === id);
    return m ? m.name : "";
  };

  switch (kpiType) {
    case "simple_measure": {
      const valueName = measureName(form.primaryMeasure);
      if (!valueName) return "";
      return `measure(${q(valueName)})`;
    }

    case "ratio": {
      // Expects numerator in primaryMeasure, denominator in secondaryMeasure
      const numName = measureName(form.primaryMeasure);
      const denName = measureName(form.secondaryMeasure);
      if (!numName || !denName) return "";
      return `safe_div(measure(${q(numName)}), measure(${q(denName)}))`;
    }

    case "variance": {
      const valueName = measureName(form.primaryMeasure);
      const compName = measureName(form.secondaryMeasure);
      if (!valueName || !compName) return "";
      return `measure(${q(valueName)}) - measure(${q(compName)})`;
    }

    case "growth_rate": {
      const valueName = measureName(form.primaryMeasure);
      const grain = form.trend_period || "month";
      if (!valueName) return "";
      return `pct_change(measure(${q(valueName)}), "${grain}")`;
    }

    case "moving_window": {
      const valueName = measureName(form.primaryMeasure);
      const grain = form.trend_period || "month";
      const windowSize = form.trend_sparkline_periods || "3";
      if (!valueName) return "";
      return `moving_avg(measure(${q(valueName)}), "${grain}", literal(${windowSize}))`;
    }

    case "composite":
      // Composite expressions are built from child KPI references + weights.
      // This is handled differently — the user selects child KPIs and weights
      // and the expression is assembled from those.
      return "";

    default:
      return "";
  }
}

/**
 * Build a target expression from the wizard form state.
 */
export function buildTargetExpression(form: KpiWizardFormState, measures: { id: string; name: string }[]): string {
  if (!form.target_type) return "";

  const measureName = (id: string) => {
    const m = measures.find((x) => x.id === id);
    return m ? m.name : "";
  };

  switch (form.target_type) {
    case "none":
      return "";

    case "static":
      if (!form.target_value) return "";
      return `literal(${form.target_value})`;

    case "measure": {
      const name = measureName(form.target_measure_id);
      if (!name) return "";
      return `measure(${q(name)})`;
    }

    case "prior_period": {
      // Use the value measure with prior_period
      const valueName = measureName(form.primaryMeasure);
      const grain = form.target_period || form.trend_period || "month";
      if (!valueName) return "";
      return `prior_period(measure(${q(valueName)}), "${grain}")`;
    }

    case "expression":
      return form.target_expression;

    default:
      return "";
  }
}

// ---------------------------------------------------------------------------
// Template measure reference helpers
// ---------------------------------------------------------------------------

const MEASURE_REF_RE = /measure\(\s*"([^"]+)"\s*\)/g;

/**
 * Extract unique measure names referenced via `measure("...")` in an expression.
 */
export function extractMeasureReferences(expression: string): string[] {
  const names: string[] = [];
  let m: RegExpExecArray | null;
  const re = new RegExp(MEASURE_REF_RE.source, MEASURE_REF_RE.flags);
  while ((m = re.exec(expression)) !== null) {
    const name = m[1].replace(/""/g, '"');
    if (!names.includes(name)) names.push(name);
  }
  return names;
}

/**
 * Rewrite an expression by replacing template measure names with mapped names.
 *
 * @param expression  The original template expression.
 * @param mapping     Map from template measure name → replacement measure name.
 * @returns           The rewritten expression.
 */
export function rewriteMeasureReferences(
  expression: string,
  mapping: Record<string, string>,
): string {
  return expression.replace(
    new RegExp(MEASURE_REF_RE.source, MEASURE_REF_RE.flags),
    (full, captured: string) => {
      const original = captured.replace(/""/g, '"');
      const replacement = mapping[original];
      if (replacement) {
        return `measure(${q(replacement)})`;
      }
      return full;
    },
  );
}
