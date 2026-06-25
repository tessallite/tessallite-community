import type { Measure } from "../../../api/types";

export const PIVOT_MAX_CELLS = 50_000;
export const PIVOT_MAX_ROW_DIMS = 3;
export const PIVOT_MAX_COL_DIMS = 3;

export type CellCoord = {
  rowKey: string[];
  colKey: string[];
  rowValues: unknown[];
  colValues: unknown[];
  measureValue: unknown;
  measureValues?: Record<string, unknown>;
};

export type PivotModel = {
  rowCols: string[];
  colCols: string[];
  rowKeys: string[][];
  colKeys: string[][];
  byKey: Map<string, CellCoord>;
};

export type DrillContext = {
  measure: Measure;
  coord: CellCoord;
};

export type SlicerOp =
  | "eq"
  | "ne"
  | "gt"
  | "gte"
  | "lt"
  | "lte"
  | "in"
  | "between"
  | "like"
  | "is_null"
  | "is_not_null";

export type Slicer = {
  dimensionId: string;
  op: SlicerOp;
  values: string[];
};

export const SLICER_OP_LABELS: Record<SlicerOp, string> = {
  eq: "slicerOp.equals",
  ne: "slicerOp.notEqual",
  gt: "slicerOp.greaterThan",
  gte: "slicerOp.greaterThanOrEqual",
  lt: "slicerOp.lessThan",
  lte: "slicerOp.lessThanOrEqual",
  in: "slicerOp.in",
  between: "slicerOp.between",
  like: "slicerOp.like",
  is_null: "slicerOp.isNull",
  is_not_null: "slicerOp.isNotNull",
};

export function slicerNeedsValues(op: SlicerOp): boolean {
  return op !== "is_null" && op !== "is_not_null";
}
