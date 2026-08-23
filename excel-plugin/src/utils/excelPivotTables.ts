/// <reference types="office-js" />

export interface PivotFieldMapping {
  rowFields: string[];
  columnFields: string[];
  dataFields: string[];
  filterFields: string[];
}

export interface ResolvedPivotFieldMapping {
  rowFields: Excel.PivotHierarchy[];
  columnFields: Excel.PivotHierarchy[];
  dataFields: Excel.PivotHierarchy[];
  filterFields: Excel.PivotHierarchy[];
}

const PIVOT_ZONE_LABELS: Record<keyof PivotFieldMapping, string> = {
  rowFields: 'Rows',
  columnFields: 'Columns',
  dataFields: 'Values',
  filterFields: 'Filters',
};

export class PivotFieldResolutionError extends Error {
  readonly unresolvedByZone: Record<keyof PivotFieldMapping, string[]>;

  constructor(unresolvedByZone: Record<keyof PivotFieldMapping, string[]>) {
    const details = (Object.keys(unresolvedByZone) as Array<keyof PivotFieldMapping>)
      .filter(zone => unresolvedByZone[zone].length > 0)
      .map(zone => `${PIVOT_ZONE_LABELS[zone]}: ${unresolvedByZone[zone].join(', ')}`)
      .join('; ');
    super(`Cannot create local PivotTable because Excel did not expose requested fields. ${details}`);
    this.name = 'PivotFieldResolutionError';
    this.unresolvedByZone = unresolvedByZone;
  }
}

export function findPivotHierarchy(
  hierarchyMap: Map<string, Excel.PivotHierarchy>,
  field: string,
): Excel.PivotHierarchy | undefined {
  // F-36: Exact match only (case-insensitive, trimmed). Fuzzy includes() removed.
  const exact = hierarchyMap.get(field);
  if (exact) return exact;
  const normalised = field.toLowerCase().trim();
  for (const [name, hier] of hierarchyMap) {
    if (name.toLowerCase().trim() === normalised) return hier;
  }
  return undefined;
}

export function resolvePivotFieldMapping(
  hierarchyMap: Map<string, Excel.PivotHierarchy>,
  fieldMapping: PivotFieldMapping,
): ResolvedPivotFieldMapping {
  const unresolvedByZone: Record<keyof PivotFieldMapping, string[]> = {
    rowFields: [],
    columnFields: [],
    dataFields: [],
    filterFields: [],
  };

  const resolveZone = (zone: keyof PivotFieldMapping): Excel.PivotHierarchy[] => {
    const resolved: Excel.PivotHierarchy[] = [];
    for (const field of fieldMapping[zone]) {
      const hierarchy = findPivotHierarchy(hierarchyMap, field);
      if (hierarchy) {
        resolved.push(hierarchy);
      } else {
        unresolvedByZone[zone].push(field);
      }
    }
    return resolved;
  };

  const resolved = {
    rowFields: resolveZone('rowFields'),
    columnFields: resolveZone('columnFields'),
    dataFields: resolveZone('dataFields'),
    filterFields: resolveZone('filterFields'),
  };

  if ((Object.keys(unresolvedByZone) as Array<keyof PivotFieldMapping>).some(zone => unresolvedByZone[zone].length > 0)) {
    throw new PivotFieldResolutionError(unresolvedByZone);
  }

  return resolved;
}
