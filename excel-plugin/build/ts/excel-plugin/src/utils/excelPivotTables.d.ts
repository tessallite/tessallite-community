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
export declare class PivotFieldResolutionError extends Error {
    readonly unresolvedByZone: Record<keyof PivotFieldMapping, string[]>;
    constructor(unresolvedByZone: Record<keyof PivotFieldMapping, string[]>);
}
export declare function findPivotHierarchy(hierarchyMap: Map<string, Excel.PivotHierarchy>, field: string): Excel.PivotHierarchy | undefined;
export declare function resolvePivotFieldMapping(hierarchyMap: Map<string, Excel.PivotHierarchy>, fieldMapping: PivotFieldMapping): ResolvedPivotFieldMapping;
