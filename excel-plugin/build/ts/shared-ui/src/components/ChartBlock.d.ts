interface ChartBlockProps {
    rows: Record<string, unknown>[];
    echartsTheme?: Record<string, unknown>;
    /**
     * Replaces the computed canvas height. Used by the maximised view, which
     * needs the chart to fill the dialog rather than sit at its inline size.
     * The existing ResizeObserver re-lays the chart out when this changes.
     */
    heightOverride?: number | string;
}
export declare function ChartBlock({ rows, echartsTheme, heightOverride, }: ChartBlockProps): import("react").JSX.Element | null;
export {};
