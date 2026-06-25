import type {
  Dimension,
  ExecuteResponse,
  FieldCompatibilityIssue,
  FieldCompatibilityResponse,
  Measure,
} from "../../../api/types";
import type { CellCoord, PivotModel, Slicer } from "./types";

// F-019-16: ``nullLabel`` lets the caller pass a localized "(null)" string
// (``t("pivot.nullValue")``) rather than the hardcoded literal. Used for both
// the rendered grid label and the pivot key, so display and keying stay
// consistent within a render.
export function renderDimValue(value: unknown, nullLabel = "(null)"): string {
  if (value === null || value === undefined) return nullLabel;
  if (typeof value === "string") return value;
  if (typeof value === "number" || typeof value === "boolean") return String(value);
  if (value instanceof Date) return value.toISOString();
  return JSON.stringify(value);
}

// F-019-07: a collision-free composite key. The previous ``parts.join("")``
// merged distinct tuples whose elements concatenated ambiguously (e.g.
// ["ab","c"] and ["a","bc"] both became "abc"), silently overwriting a cell
// and understating totals. JSON.stringify is unambiguous for any string
// content, including values that themselves contain the old "||" separator.
function keyJoin(parts: string[]): string {
  return JSON.stringify(parts);
}

// F-019-13: numeric-aware tuple ordering. The default order is over rendered
// strings, which sorts numeric members as 1, 10, 11, 2, … . When the raw
// values for a position are both numbers, compare them numerically so months,
// quarters and day-numbers render in natural order without a manual sort
// click. Falls back to string compare (the rendered label) otherwise.
function compareTuples(
  a: string[],
  b: string[],
  rawA?: unknown[],
  rawB?: unknown[],
): number {
  const n = Math.min(a.length, b.length);
  for (let i = 0; i < n; i++) {
    const ra = rawA?.[i];
    const rb = rawB?.[i];
    if (typeof ra === "number" && typeof rb === "number") {
      if (ra < rb) return -1;
      if (ra > rb) return 1;
      continue;
    }
    if (a[i] < b[i]) return -1;
    if (a[i] > b[i]) return 1;
  }
  return a.length - b.length;
}

/**
 * Pivot a flat result set into a row×col grid.
 *
 * Keys are ordered tuples (one element per selected row/col dim), sorted
 * numerically when the position holds numbers and lexicographically
 * otherwise (F-019-13). Duplicate-dimension selection between rows and cols
 * is already guarded in the SQL synthesiser; pivot assumes the flat rows
 * contain every selected dim column exactly once.
 */
export function computePivot(
  response: ExecuteResponse,
  measure: Measure,
  rowDims: Dimension[],
  colDims: Dimension[],
  extraMeasures: Measure[] = [],
  nullLabel = "(null)",
): PivotModel {
  const rowCols = rowDims.map((d) => d.name);
  const colCols = colDims.map((d) => d.name);
  const measureCol = measure.name;
  const allMeasureNames = [measure.name, ...extraMeasures.map((m) => m.name)];

  const rowTupleByKey = new Map<string, string[]>();
  const colTupleByKey = new Map<string, string[]>();
  // F-019-13: keep the raw value tuples so the default sort can compare
  // numbers numerically rather than by their rendered string.
  const rowRawByKey = new Map<string, unknown[]>();
  const colRawByKey = new Map<string, unknown[]>();
  const byKey = new Map<string, CellCoord>();

  for (const r of response.rows) {
    const rec = r as Record<string, unknown>;
    const rowValues = rowCols.map((c) => rec[c]);
    const colValues = colCols.map((c) => rec[c]);
    const rowKey = rowValues.map((v) => renderDimValue(v, nullLabel));
    const colKey = colValues.map((v) => renderDimValue(v, nullLabel));
    const rk = keyJoin(rowKey);
    const ck = keyJoin(colKey);
    if (rowCols.length > 0) {
      rowTupleByKey.set(rk, rowKey);
      rowRawByKey.set(rk, rowValues);
    }
    if (colCols.length > 0) {
      colTupleByKey.set(ck, colKey);
      colRawByKey.set(ck, colValues);
    }
    const measureValues: Record<string, unknown> = {};
    for (const mn of allMeasureNames) {
      measureValues[mn] = rec[mn];
    }
    byKey.set(`${rk}||${ck}`, {
      rowKey,
      colKey,
      rowValues,
      colValues,
      measureValue: rec[measureCol],
      measureValues,
    });
  }

  const rowKeys = rowCols.length
    ? Array.from(rowTupleByKey.values()).sort((a, b) =>
        compareTuples(a, b, rowRawByKey.get(keyJoin(a)), rowRawByKey.get(keyJoin(b))),
      )
    : [[] as string[]];
  const colKeys = colCols.length
    ? Array.from(colTupleByKey.values()).sort((a, b) =>
        compareTuples(a, b, colRawByKey.get(keyJoin(a)), colRawByKey.get(keyJoin(b))),
      )
    : [[] as string[]];

  return { rowCols, colCols, rowKeys, colKeys, byKey };
}

export function cellLookupKey(rowKey: string[], colKey: string[]): string {
  return `${keyJoin(rowKey)}||${keyJoin(colKey)}`;
}

const SECURITY_REASON_CODES = new Set([
  "PERSONA_FIELD_UNAVAILABLE",
  "HIDDEN_FIELD_UNAVAILABLE",
  "UNKNOWN_FIELD",
]);

function isBlockingCompatibilityIssue(issue: FieldCompatibilityIssue): boolean {
  return issue.severity !== "warning" && issue.code !== "AMBIGUOUS_JOIN_PATH";
}

export type PivotCompatibilityAction =
  | "keep_common_dimensions"
  | "split_pivot"
  | "remove_incompatible_dimensions";

export type PivotCompatibilityIssue = FieldCompatibilityIssue & {
  dimensionName?: string;
  measureName?: string;
  location: "row" | "column" | "slicer";
};

export type PivotCompatibilityConflict = {
  measureId: string;
  measureName: string;
  incompatibleDimensionIds: string[];
  incompatibleDimensionNames: string[];
  compatibleDimensionNames: string[];
};

export type PivotCompatibilityEvaluation = {
  status: "neutral" | "compatible" | "incompatible";
  selectedIssues: PivotCompatibilityIssue[];
  disabledDimensionReasons: Record<string, string>;
  commonDimensionIds: string[];
  commonDimensionNames: string[];
  conflictsByMeasure: PivotCompatibilityConflict[];
  noCommonDimensions: boolean;
  actions: Record<PivotCompatibilityAction, boolean>;
  incompatibleDimensionIds: string[];
  hasVerifiedIncompatibilities: boolean;
};

export function evaluatePivotCompatibility({
  measureIds,
  rowDimIds,
  colDimIds,
  slicers,
  dimensions,
  matrix,
}: {
  measureIds: string[];
  rowDimIds: string[];
  colDimIds: string[];
  slicers: Slicer[];
  dimensions: Dimension[];
  matrix: FieldCompatibilityResponse | null | undefined;
}): PivotCompatibilityEvaluation {
  const selectedMeasureIds = [...new Set(measureIds.filter(Boolean))];
  if (selectedMeasureIds.length === 0 || !matrix) {
    return neutralPivotCompatibility();
  }

  const dimensionsById = new Map(dimensions.map((d) => [d.id, d]));
  const selectedDimensionLocations = new Map<string, "row" | "column" | "slicer">();
  for (const id of rowDimIds) selectedDimensionLocations.set(id, "row");
  for (const id of colDimIds) {
    if (!selectedDimensionLocations.has(id)) selectedDimensionLocations.set(id, "column");
  }
  for (const slicer of slicers) {
    if (!selectedDimensionLocations.has(slicer.dimensionId)) {
      selectedDimensionLocations.set(slicer.dimensionId, "slicer");
    }
  }

  const disabledDimensionReasons: Record<string, string> = {};
  const selectedIssues: PivotCompatibilityIssue[] = [];
  const incompatibleDimensionIds = new Set<string>();

  for (const measureId of selectedMeasureIds) {
    const measureEntry = matrix.measures[measureId];
    if (!measureEntry) continue;
    for (const [dimensionId, issue] of Object.entries(measureEntry.incompatible_dimensions)) {
      if (isBlockingCompatibilityIssue(issue)) {
        const reason = disabledDimensionReasons[dimensionId];
        disabledDimensionReasons[dimensionId] = reason ? `${reason}\n${issue.message}` : issue.message;
      }

      const location = selectedDimensionLocations.get(dimensionId);
      if (!location) continue;
      if (isBlockingCompatibilityIssue(issue)) {
        incompatibleDimensionIds.add(dimensionId);
      }
      selectedIssues.push({
        ...issue,
        measureName: measureEntry.name ?? undefined,
        dimensionName: securitySafeDimensionName(issue, dimensionsById.get(dimensionId)),
        location,
      });
    }
  }

  const selectedMeasureEntries = selectedMeasureIds
    .map((measureId) => matrix.measures[measureId])
    .filter((entry): entry is NonNullable<typeof entry> => Boolean(entry));

  const commonDimensionIds =
    selectedMeasureIds.length > 1 && matrix.multi_measure
      ? matrix.multi_measure.common_dimension_ids
      : intersectCompatibleDimensionIds(selectedMeasureEntries);

  const commonDimensionNames =
    selectedMeasureIds.length > 1 && matrix.multi_measure
      ? matrix.multi_measure.common_dimension_names
      : selectedMeasureEntries[0]?.compatible_dimension_ids
          .map((dimensionId) => dimensionsById.get(dimensionId)?.display_name)
          .filter((name): name is string => Boolean(name)) ?? [];

  const conflictsByMeasure = buildConflictsByMeasure(
    selectedMeasureIds,
    selectedDimensionLocations,
    dimensionsById,
    matrix,
  );

  const hasVerifiedIncompatibilities = selectedIssues.some(isBlockingCompatibilityIssue);
  const isMultiMeasure = selectedMeasureIds.length > 1;

  return {
    status: hasVerifiedIncompatibilities ? "incompatible" : "compatible",
    selectedIssues,
    disabledDimensionReasons,
    commonDimensionIds,
    commonDimensionNames,
    conflictsByMeasure,
    noCommonDimensions: isMultiMeasure && commonDimensionIds.length === 0,
    actions: {
      keep_common_dimensions: isMultiMeasure && commonDimensionIds.length > 0,
      split_pivot: isMultiMeasure,
      remove_incompatible_dimensions: hasVerifiedIncompatibilities,
    },
    incompatibleDimensionIds: [...incompatibleDimensionIds],
    hasVerifiedIncompatibilities,
  };
}

function neutralPivotCompatibility(): PivotCompatibilityEvaluation {
  return {
    status: "neutral",
    selectedIssues: [],
    disabledDimensionReasons: {},
    commonDimensionIds: [],
    commonDimensionNames: [],
    conflictsByMeasure: [],
    noCommonDimensions: false,
    actions: {
      keep_common_dimensions: false,
      split_pivot: false,
      remove_incompatible_dimensions: false,
    },
    incompatibleDimensionIds: [],
    hasVerifiedIncompatibilities: false,
  };
}

function intersectCompatibleDimensionIds(
  measureEntries: Array<{ compatible_dimension_ids: string[] }>,
): string[] {
  if (measureEntries.length === 0) return [];
  const [first, ...rest] = measureEntries;
  return first.compatible_dimension_ids.filter((dimensionId) =>
    rest.every((entry) => entry.compatible_dimension_ids.includes(dimensionId)),
  );
}

function buildConflictsByMeasure(
  measureIds: string[],
  selectedDimensionLocations: Map<string, "row" | "column" | "slicer">,
  dimensionsById: Map<string, Dimension>,
  matrix: FieldCompatibilityResponse,
): PivotCompatibilityConflict[] {
  const out: PivotCompatibilityConflict[] = [];
  for (const measureId of measureIds) {
    const measureEntry = matrix.measures[measureId];
    if (!measureEntry) continue;
    const incompatibleDimensionIds: string[] = [];
    const incompatibleDimensionNames: string[] = [];
    for (const [dimensionId, issue] of Object.entries(measureEntry.incompatible_dimensions)) {
      if (!selectedDimensionLocations.has(dimensionId)) continue;
      if (!isBlockingCompatibilityIssue(issue)) continue;
      const name = securitySafeDimensionName(issue, dimensionsById.get(dimensionId));
      if (!name) continue;
      incompatibleDimensionIds.push(dimensionId);
      incompatibleDimensionNames.push(name);
    }
    if (incompatibleDimensionIds.length === 0) continue;
    out.push({
      measureId,
      measureName: measureEntry.name ?? measureId,
      incompatibleDimensionIds,
      incompatibleDimensionNames,
      compatibleDimensionNames: compatibleNamesFromIssues(measureEntry.incompatible_dimensions),
    });
  }
  return out;
}

function securitySafeDimensionName(
  issue: FieldCompatibilityIssue,
  dimension: Dimension | undefined,
): string | undefined {
  if (SECURITY_REASON_CODES.has(issue.code)) return undefined;
  return dimension?.display_name || dimension?.name;
}

function compatibleNamesFromIssues(
  issues: Record<string, FieldCompatibilityIssue>,
): string[] {
  const names: string[] = [];
  const seen = new Set<string>();
  for (const issue of Object.values(issues)) {
    for (const name of issue.compatible_dimension_names) {
      if (!seen.has(name)) {
        seen.add(name);
        names.push(name);
      }
    }
  }
  return names;
}
