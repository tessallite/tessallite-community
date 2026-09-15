export type ChartTypeRecommendation = 'line' | 'columnClustered' | 'barClustered' | 'pie' | 'doughnut';
export interface ChartRecommendation {
    chartType: ChartTypeRecommendation;
    confidence: 'high' | 'medium' | 'low';
    reason: string;
}
export declare function mapAgentChartType(agentType: string | null | undefined): ChartTypeRecommendation | null;
export declare function getChartTypeEnum(type: ChartTypeRecommendation): Excel.ChartType;
interface ChartAnnotation {
    measures?: Record<string, {
        title: string;
        type: string;
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
/**
 * Bug-7416: the query-router's plugin-execute annotation always returns an
 * empty `timeDimensions` map (`_build_annotation` hardcodes `{}`), so a
 * time-series result was classified as an ordinary categorical dimension and
 * `recommendChartType` never chose a line chart.
 *
 * The plugin already knows which dimensions are time dimensions
 * (`Dimension.is_time_dimension`, loaded for the model). This pure helper
 * reclassifies any annotation `dimensions` entry whose dimension name is a
 * known time dimension into `timeDimensions`, so the downstream chart
 * recommender and axis logic (which already read `timeDimensions`) see the
 * time axis. Idempotent and non-mutating: returns a new annotation object.
 *
 * `timeDimensionNames` is the set of technical dimension names the model marks
 * `is_time_dimension`. Matching is on the annotation KEY (the technical name),
 * not the display title, since the backend keys `dimensions` by name.
 */
export declare function enrichAnnotationTimeDimensions(annotation: ChartAnnotation | undefined, timeDimensionNames: Iterable<string>): ChartAnnotation | undefined;
export declare function recommendChartType(headers: string[], rows: (string | number)[][], annotation?: {
    measures?: Record<string, {
        title: string;
        type: string;
    }>;
    dimensions?: Record<string, {
        title: string;
        type: string;
    }>;
    timeDimensions?: Record<string, {
        title: string;
        type: string;
    }>;
}): ChartRecommendation;
export declare function separateColumns(headers: string[], rows: (string | number)[][], annotation?: {
    measures?: Record<string, {
        title: string;
        type: string;
    }>;
    dimensions?: Record<string, {
        title: string;
        type: string;
    }>;
    timeDimensions?: Record<string, {
        title: string;
        type: string;
    }>;
}): {
    chartHeaders: string[];
    chartRows: (string | number)[][];
};
/**
 * Bug-6733: chart creation is split into a critical core (data range, chart
 * object, position, title) and non-critical axis formatting. The core is
 * synced first so the chart exists regardless of whether axis-title writes
 * fail on certain Excel hosts / chart types. The caller
 * (`useExcel.insertChart`) syncs the core, then applies axis formatting in
 * a separate non-fatal sync -- so a post-insert axis error never propagates
 * as "Insert failed" when the chart was actually created.
 */
export declare function createChartOnSheet(chartType: Excel.ChartType, sheet: Excel.Worksheet, headers: string[], rows: (string | number)[][], annotation?: {
    measures?: Record<string, {
        title: string;
        type: string;
    }>;
    dimensions?: Record<string, {
        title: string;
        type: string;
    }>;
    timeDimensions?: Record<string, {
        title: string;
        type: string;
    }>;
}, title?: string): {
    chart: Excel.Chart;
    chartHeaders: string[];
};
/**
 * Bug-6733: non-critical axis formatting extracted from the chart creation
 * path. If this throws on `context.sync()` (e.g. pie charts that do not
 * support category axes in some Excel hosts), the chart itself is already
 * persisted. Called by `useExcel.insertChart` inside a try-catch after the
 * core chart sync succeeds.
 *
 * R1 Finding 2: `fallbackMeasureHeaders` restores the pre-refactor
 * behaviour where the value-axis title fell back to `chartHeaders.slice(1)`
 * (the actual column names from the data range) when `annotation.measures`
 * is absent. Without it, the Ask-Tessallite and KPI-panel chart paths
 * (which pass no annotation) would show a generic "Value" axis title
 * instead of the real measure name.
 */
export declare function applyChartAxisFormatting(chart: Excel.Chart, annotation?: {
    measures?: Record<string, {
        title: string;
        type: string;
    }>;
    dimensions?: Record<string, {
        title: string;
        type: string;
    }>;
    timeDimensions?: Record<string, {
        title: string;
        type: string;
    }>;
}, fallbackMeasureHeaders?: string[]): void;
/**
 * Bug-9737: the Agent conversation turn carries no `annotation` field (unlike
 * the plugin-execute response ReportBuilder reads), so every Ask & Insert
 * chart/pivot call site was omitting `annotation` entirely and falling back
 * to `separateColumns`'s `typeof val === 'number'` heuristic. The Agent API
 * returns measure values as JSON strings (e.g. `"23332917.80"`), which always
 * fails that check -- every column got classified as a dimension, collapsing
 * the chart to one concatenated "Category" column with no measure series
 * (an empty chart). `turn.citations` already carries the real measure/
 * dimension classification per column (`kind`, `name`, `display_name`); this
 * builds the same `{measures, dimensions}` shape the annotation-aware path
 * already handles correctly, keyed by the citation's technical `name` (which
 * matches the `query_result_sample` row keys used as chart headers).
 */
export declare function buildAnnotationFromCitations(citations: Array<{
    kind: string;
    name: string;
    display_name: string;
}> | null | undefined): {
    measures?: Record<string, {
        title: string;
        type: string;
    }>;
    dimensions?: Record<string, {
        title: string;
        type: string;
    }>;
} | undefined;
export {};
