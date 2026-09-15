import type { Measure } from '../types/tessallite';
export interface ResultAnnotation {
    measures?: Record<string, {
        title: string;
        type: string;
        format?: string;
    }>;
    dimensions?: Record<string, {
        title: string;
        type: string;
    }>;
    timeDimensions?: Record<string, {
        title: string;
        type: string;
    }>;
}
export interface TableDrillMetadata {
    formatTokens: Record<string, string>;
    measureColumns: Record<string, string>;
    dimensionColumns: Record<string, string>;
}
export declare function buildTableDrillMetadata(annotation: ResultAnnotation | undefined, measures?: Measure[]): TableDrillMetadata;
