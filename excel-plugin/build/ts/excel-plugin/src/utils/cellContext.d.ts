/** A single drill-through cell coordinate, in the backend's request shape
 * (DrillThroughFilter — drill_routes.py): {column, op, value}. */
export interface DrillFilter {
    column: string;
    op: string;
    value: unknown;
}
export interface CellContext {
    type: 'cube-formula' | 'plugin-table' | 'unknown';
    measureId?: string;
    measureName?: string;
    /** Cell coordinates — the row's dimension values — as backend grouping levels. */
    groupingLevels?: DrillFilter[];
    /** Slicer/member filters that constrain the selected value but are not row coordinates. */
    filters?: DrillFilter[];
    /** @deprecated Use groupingLevels for row coordinates and filters for slicers. */
    drillFilters?: DrillFilter[];
    projectId?: string;
    modelId?: string;
    personaId?: string;
    conversationId?: string;
    turnId?: string;
    unavailableReason?: string;
}
export interface MeasureLookup {
    byName: Map<string, string>;
    byDisplayName: Map<string, string>;
}
export declare function buildDrillRequestContext(ctx: CellContext, fallbackPersonaId?: string | null): Record<string, unknown>;
export declare function resolveCellContext(address: string, value: unknown, formula: string, measureLookup?: MeasureLookup): Promise<CellContext>;
