import type { Dimension, FieldCompatibilityIssue, FieldCompatibilityResponse } from '../types/tessallite';
import type { ZoneItem } from '../components/ReportBuilder/ZoneMappingGrid';

export interface DimensionCompatibilityState {
  disabled: boolean;
  messages: string[];
  compatibleDimensionNames: string[];
}

export interface ZoneCompatibilityIssue extends FieldCompatibilityIssue {
  measureName: string;
  dimensionName: string;
}

export interface ZoneCompatibilityResult {
  selectedMeasureIds: string[];
  selectedDimensionIds: string[];
  blocking: boolean;
  issues: ZoneCompatibilityIssue[];
  compatibleDimensionNames: string[];
  unavailableByDimensionId: Record<string, DimensionCompatibilityState>;
}

function compact(ids: Array<string | null | undefined>): string[] {
  return [...new Set(ids.filter((id): id is string => Boolean(id)))].sort();
}

function displayDimensionName(dimension: Dimension | undefined, fallback: string): string {
  return dimension?.display_name || dimension?.name || fallback;
}

function authorizedDimensionNames(dimensions: Dimension[]): Set<string> {
  const names = new Set<string>();
  for (const dimension of dimensions) {
    names.add(dimension.name);
    names.add(dimension.display_name);
  }
  return names;
}

function resolveDimensionId(item: ZoneItem, dimensions: Dimension[]): string | null {
  if (item.zone === 'values') return null;
  const direct = dimensions.find(d => d.id === item.id);
  if (direct) return direct.id;

  if (item.bindDimension) {
    const bound = dimensions.find(d => d.id === item.bindDimension || d.name === item.bindDimension);
    return bound?.id ?? null;
  }

  return null;
}

function isVerifiedIncompatibility(issue: FieldCompatibilityIssue): boolean {
  return (
    issue.code !== 'SEMANTIC_COMPATIBILITY_NOT_ANALYZED' &&
    issue.code !== 'AMBIGUOUS_JOIN_PATH' &&
    issue.severity !== 'warning'
  );
}

export function selectedCompatibilityIds(items: ZoneItem[], dimensions: Dimension[]) {
  return {
    measureIds: compact(items.filter(item => item.zone === 'values').map(item => item.id)),
    dimensionIds: compact(items.map(item => resolveDimensionId(item, dimensions))),
  };
}

export function evaluateZoneFieldCompatibility(args: {
  items: ZoneItem[];
  dimensions: Dimension[];
  matrix?: FieldCompatibilityResponse | null;
}): ZoneCompatibilityResult {
  const { measureIds, dimensionIds } = selectedCompatibilityIds(args.items, args.dimensions);
  const dimensionById = new Map(args.dimensions.map(d => [d.id, d]));
  const authorizedNames = authorizedDimensionNames(args.dimensions);
  const issues: ZoneCompatibilityIssue[] = [];
  const compatibleDimensionNames = new Set<string>();

  if (args.matrix) {
    for (const measureId of measureIds) {
      const measureEntry = args.matrix.measures[measureId];
      if (!measureEntry) continue;

      for (const dimensionId of dimensionIds) {
        const issue = measureEntry.incompatible_dimensions?.[dimensionId];
        if (!issue) continue;
        if (!isVerifiedIncompatibility(issue)) continue;
        issues.push({
          ...issue,
          measureName: measureEntry.name || measureId,
          dimensionName: displayDimensionName(dimensionById.get(dimensionId), dimensionId),
        });
        for (const name of issue.compatible_dimension_names ?? []) {
          if (!authorizedNames.has(name)) continue;
          if (name) compatibleDimensionNames.add(name);
        }
      }
    }

    for (const name of args.matrix.multi_measure?.common_dimension_names ?? []) {
      if (!authorizedNames.has(name)) continue;
      if (name) compatibleDimensionNames.add(name);
    }
  }

  return {
    selectedMeasureIds: measureIds,
    selectedDimensionIds: dimensionIds,
    blocking: issues.length > 0,
    issues,
    compatibleDimensionNames: [...compatibleDimensionNames].sort(),
    unavailableByDimensionId: dimensionCompatibilityById(args.matrix, measureIds, args.dimensions),
  };
}

export function dimensionCompatibilityById(
  matrix: FieldCompatibilityResponse | null | undefined,
  selectedMeasureIds: string[],
  dimensions: Dimension[],
): Record<string, DimensionCompatibilityState> {
  const result: Record<string, DimensionCompatibilityState> = {};
  const measureIds = compact(selectedMeasureIds);
  const authorizedNames = authorizedDimensionNames(dimensions);

  for (const dimension of dimensions) {
    const messages = new Set<string>();
    const compatibleDimensionNames = new Set<string>();

    if (matrix && measureIds.length > 0) {
      for (const measureId of measureIds) {
        const issue = matrix.measures[measureId]?.incompatible_dimensions?.[dimension.id];
        if (!issue) continue;
        if (!isVerifiedIncompatibility(issue)) continue;
        if (issue.message) messages.add(issue.message);
        for (const name of issue.compatible_dimension_names ?? []) {
          if (!authorizedNames.has(name)) continue;
          if (name) compatibleDimensionNames.add(name);
        }
      }
    }

    result[dimension.id] = {
      disabled: messages.size > 0,
      messages: [...messages],
      compatibleDimensionNames: [...compatibleDimensionNames].sort(),
    };
  }

  return result;
}

export function formatZoneCompatibilityMessages(result: ZoneCompatibilityResult): string[] {
  const unique = new Set<string>();
  for (const issue of result.issues) {
    unique.add(`${issue.dimensionName} with ${issue.measureName}: ${issue.message}`);
  }
  return [...unique];
}
