interface RenderedOutputProps {
    html: string;
    chartsCss?: string;
    /**
     * Bug-6584: optional URL of a served charts stylesheet. Only when this is a
     * non-empty string does the iframe emit a `<link rel="stylesheet">`. Hosts
     * that inline the stylesheet via `chartsCss` (the frontend, conversational
     * client, and Excel task-pane plugin all do) must NOT set this — a hardcoded
     * `/charts.min.css` link 404s in the Excel host, which does not serve it.
     */
    chartsCssHref?: string;
}
export declare function RenderedOutput({ html, chartsCss, chartsCssHref }: RenderedOutputProps): import("react").JSX.Element | null;
export {};
