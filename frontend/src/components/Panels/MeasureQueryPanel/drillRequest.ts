import type {
  Dimension,
  DrillThroughFilter,
  DrillThroughRequest,
} from "../../../api/types";
import type { PivotColumnMeasure } from "./measureColumns";
import type { CellCoord, Slicer } from "./types";

export function buildInitialGroupingLevels(
  coord: CellCoord,
  rowDims: Dimension[],
  colDims: Dimension[],
): DrillThroughFilter[] {
  const levels: DrillThroughFilter[] = [];
  rowDims.slice(0, coord.rowValues.length).forEach((dimension, index) => {
    levels.push({ column: dimension.name, op: "eq", value: coord.rowValues[index] });
  });
  colDims.slice(0, coord.colValues.length).forEach((dimension, index) => {
    levels.push({ column: dimension.name, op: "eq", value: coord.colValues[index] });
  });
  return levels;
}

export function buildSlicerFilters(
  slicers: Slicer[],
  dimensionsById: Map<string, Dimension>,
): DrillThroughFilter[] {
  const filters: DrillThroughFilter[] = [];
  for (const slicer of slicers) {
    const dimension = dimensionsById.get(slicer.dimensionId);
    if (!dimension) continue;
    const op = slicer.op === "ne" ? "neq" : slicer.op;
    if (op === "is_null" || op === "is_not_null") {
      filters.push({ column: dimension.name, op });
    } else if (op === "in") {
      const values = slicer.values.filter((value) => value.length > 0);
      if (values.length > 0) filters.push({ column: dimension.name, op, value: values });
    } else if (op === "between") {
      if (slicer.values.length >= 2 && slicer.values[0] && slicer.values[1]) {
        filters.push({ column: dimension.name, op, value: [slicer.values[0], slicer.values[1]] });
      }
    } else if (slicer.values[0]) {
      filters.push({ column: dimension.name, op, value: slicer.values[0] });
    }
  }
  return filters;
}

type DrillInvocationInput = {
  measure: PivotColumnMeasure;
  groupingLevels: DrillThroughFilter[];
  filters: DrillThroughFilter[];
  limit: number;
  cursor?: string | null;
  hierarchyId?: string | null;
  forceLive?: boolean;
  overrideAggregation?: string | null;
};

export function buildDrillInvocation(input: DrillInvocationInput): {
  measureId: string;
  request: DrillThroughRequest;
} {
  const defaultAggregation = (input.measure.default_agg ?? "SUM").toUpperCase();
  const selectedAggregation = input.measure._agg?.toUpperCase();
  const overrideAggregation = input.overrideAggregation ?? (
    selectedAggregation &&
    selectedAggregation !== defaultAggregation &&
    !input.measure._scratchpad
      ? selectedAggregation
      : null
  );
  return {
    measureId: input.measure._measureId ?? input.measure.id,
    request: {
      grouping_levels: input.groupingLevels,
      ...(input.filters.length > 0 ? { filters: input.filters } : {}),
      limit: input.limit,
      ...(input.cursor ? { cursor: input.cursor } : {}),
      ...(input.hierarchyId ? { hierarchy_id: input.hierarchyId } : {}),
      ...(input.forceLive ? { force_route: "source" as const } : {}),
      ...(overrideAggregation ? { override_agg: overrideAggregation } : {}),
    },
  };
}
