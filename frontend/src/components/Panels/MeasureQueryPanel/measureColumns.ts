/**
 * Measure-selection model for the pivot panel.
 *
 * A pivot can show the same underlying measure under several aggregate
 * functions (e.g. Revenue as SUM, AVG and MAX) plus a built-in record count.
 * Each selection is expanded into a "column measure": a synthetic ``Measure``
 * whose ``name`` is a unique SELECT alias so the existing pivot / totals / grid
 * code (all keyed by ``measure.name``) keeps working without change.
 */
import type { Measure } from "../../../api/types";

export const RECORD_COUNT_ID = "__record_count__";
export const RECORD_COUNT_NAME = "__record_count__";

export type Translate = (key: string, params?: Record<string, string>) => string;

/** One user-chosen (measure, aggregate-function) pair. Order matters. */
export type MeasureSel = { measureId: string; agg: string };

/** A synthetic measure representing one selection, rendered as one column. */
export type PivotColumnMeasure = Measure & {
  _alias: string;       // unique SELECT alias === overridden ``name``
  _agg: string;         // aggregate function (upper-case)
  _baseName: string;    // underlying source column name (for SQL)
  _measureId: string;   // underlying measure id (for drill / format)
  _scratchpad?: boolean;
  _recordCount?: boolean;
};

/** Aggregate functions a user can pick for a standard measure. */
export const AGG_OPTIONS = ["SUM", "AVG", "MIN", "MAX", "COUNT", "COUNT_DISTINCT"] as const;
export type AggOption = (typeof AGG_OPTIONS)[number];

/** Build the localized synthetic "Record Count" measure (COUNT(*)). */
export function recordCountMeasure(t: Translate): Measure {
  return {
    id: RECORD_COUNT_ID,
    name: RECORD_COUNT_NAME,
    display_name: t("pickerBar.recordCount"),
    description: null,
    source_column_id: null,
    source_column_name: null,
    source_table_id: null,
    user_defined_attribute_id: null,
    user_defined_attribute_name: null,
    measure_type: "standard",
    expression: null,
    default_agg: "count",
    data_type: "integer",
    format: "integer",
    is_additive: true,
    redundant_partner: null,
  };
}

function aggLabel(t: Translate, agg: string): string {
  return t(`pivot.agg.${agg.toLowerCase()}`);
}

/**
 * Deterministic, unique SELECT alias for a selection. The occurrence index keeps
 * aliases unique even when the same (measure, agg) pair is added twice.
 */
export function columnAlias(baseName: string, agg: string, idx: number): string {
  return `${baseName}__${agg.toLowerCase()}__${idx}`;
}

/**
 * Expand ordered selections into column measures. ``baseMeasures`` must already
 * include the synthetic record-count measure when it is selectable.
 */
export function buildColumnMeasures(
  selections: MeasureSel[],
  baseMeasures: Measure[],
  t: Translate,
): PivotColumnMeasure[] {
  const byId = new Map(baseMeasures.map((m) => [m.id, m]));
  const columns: PivotColumnMeasure[] = [];
  selections.forEach((sel, idx) => {
    const base = byId.get(sel.measureId);
    if (!base) return;
    const isRecordCount = base.id === RECORD_COUNT_ID;
    const isScratchpad = (base as { _scratchpad?: boolean })._scratchpad === true;
    const agg = (isRecordCount ? "COUNT" : sel.agg || base.default_agg || "SUM").toUpperCase();
    const alias = columnAlias(base.name, agg, idx);
    const display = isRecordCount
      ? base.display_name
      : t("pivot.measureWithAgg", { name: base.display_name || base.name, agg: aggLabel(t, agg) });
    columns.push({
      ...base,
      name: alias,
      display_name: display,
      _alias: alias,
      _agg: agg,
      _baseName: base.name,
      _measureId: base.id,
      _scratchpad: isScratchpad,
      _recordCount: isRecordCount,
    });
  });
  return columns;
}
