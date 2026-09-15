export interface VisualArtifact {
    kind: "tessallite.visual.v1";
    renderer: "echarts";
    chart_type: string | null;
    columns: string[];
    rows: Record<string, unknown>[];
    palette?: string;
    size?: "sm" | "md" | "lg";
    include_table?: boolean;
    legacy_html?: string;
}
export declare function parseVisualArtifact(value: string | null | undefined): VisualArtifact | null;
export declare function VisualArtifactBlock({ artifact, echartsTheme, heightOverride, }: {
    artifact: VisualArtifact;
    echartsTheme?: Record<string, unknown>;
    /**
     * Replaces the computed canvas height. Used by the maximised view, which
     * needs the chart to fill the dialog rather than sit at its inline size.
     * The existing ResizeObserver re-lays the chart out when this changes.
     */
    heightOverride?: number | string;
}): import("react").JSX.Element | null;
