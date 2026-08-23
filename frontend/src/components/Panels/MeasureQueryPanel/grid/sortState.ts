import type { PivotSort } from "../../../../api/client";
import type { Measure } from "../../../../api/types";

export type ResolvedPivotSort = {
  measureName: string;
  measureIndex: number;
  ckIndex: number | "grand";
  dir: "asc" | "desc";
};

function tupleKey(parts: string[]): string {
  return JSON.stringify(parts);
}

export function pivotSortMeasureIdentity(
  measures: Measure[],
  index: number,
): PivotSort["measure"] {
  const measure = measures[index] as Measure & { _measureId?: string; _agg?: string };
  const measureId = measure._measureId ?? measure.id;
  const aggregation = (measure._agg ?? measure.default_agg ?? "SUM").toUpperCase();
  let occurrence = 0;
  for (let priorIndex = 0; priorIndex < index; priorIndex += 1) {
    const prior = measures[priorIndex] as Measure & { _measureId?: string; _agg?: string };
    if (
      (prior._measureId ?? prior.id) === measureId &&
      (prior._agg ?? prior.default_agg ?? "SUM").toUpperCase() === aggregation
    ) occurrence += 1;
  }
  return { measureId, aggregation, occurrence };
}

export function resolvePivotSort(
  sort: PivotSort | null,
  measures: Measure[],
  columnKeys: string[][],
): ResolvedPivotSort | null {
  if (!sort) return null;
  const measureIndex = measures.findIndex((candidate, index) => {
    const identity = pivotSortMeasureIdentity(measures, index);
    return identity.measureId === sort.measure.measureId &&
      identity.aggregation === sort.measure.aggregation.toUpperCase() &&
      identity.occurrence === sort.measure.occurrence;
  });
  if (measureIndex < 0) return null;
  if (sort.target.kind === "grand") {
    return {
      measureName: measures[measureIndex].name,
      measureIndex,
      ckIndex: "grand",
      dir: sort.direction,
    };
  }
  const ckIndex = columnKeys.findIndex(
    (key) => tupleKey(key) === tupleKey(sort.target.kind === "column" ? sort.target.columnKey : []),
  );
  if (ckIndex < 0) return null;
  return {
    measureName: measures[measureIndex].name,
    measureIndex,
    ckIndex,
    dir: sort.direction,
  };
}
