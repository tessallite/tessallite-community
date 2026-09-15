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
export declare function selectedCompatibilityIds(items: ZoneItem[], dimensions: Dimension[]): {
    measureIds: string[];
    dimensionIds: string[];
};
export declare function evaluateZoneFieldCompatibility(args: {
    items: ZoneItem[];
    dimensions: Dimension[];
    matrix?: FieldCompatibilityResponse | null;
}): ZoneCompatibilityResult;
export declare function dimensionCompatibilityById(matrix: FieldCompatibilityResponse | null | undefined, selectedMeasureIds: string[], dimensions: Dimension[]): Record<string, DimensionCompatibilityState>;
export declare function formatZoneCompatibilityMessages(result: ZoneCompatibilityResult): string[];
