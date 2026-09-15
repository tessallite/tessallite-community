export type AutoChartKind = "bar" | "hbar" | "line" | "pie" | "metric";
export interface AutoChartSeries {
    name: string;
    values: (number | null)[];
}
export interface AutoChartSpec {
    kind: AutoChartKind;
    dimension: string;
    labels: string[];
    series: AutoChartSeries[];
    truncated?: {
        shown: number;
        total: number;
    };
}
export declare const ROW_DIMENSION = "__row";
export declare const MEASURES_DIMENSION = "__measures";
export declare const VALUE_SERIES_NAME = "__value";
export declare function buildAutoChartSpec(rows: Record<string, unknown>[], maxPoints?: number): AutoChartSpec | null;
