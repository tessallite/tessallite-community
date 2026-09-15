/**
 * Opens the answer chart in an Office dialog window.
 *
 * A task pane is a few hundred pixels wide and Office has no API to resize it,
 * so the shared chat UI's in-pane maximise can never exceed the pane.
 * `displayDialogAsync` opens a real window sized to the screen.
 *
 * A dialog cannot read add-in storage or call the API — Office allows it only
 * `messageParent`/`addHandlerAsync` — so the child says when it is listening and
 * the parent posts the chart back. Needs DialogApi 1.2 for `messageChild`.
 */
import { type VisualArtifact } from "@tessallite/shared-ui";
export interface VisualActionData {
    artifact: VisualArtifact | null;
    rows: Record<string, unknown>[];
}
/**
 * Resolve the dataset used by every rendered-turn action. Persisted visual
 * artifacts survive a reload even when the turn's result sample does not, so
 * parse the artifact before the empty-data gate and use its rows as fallback.
 */
export declare function resolveVisualActionData(renderedOutput: string | null | undefined, resultRows?: Record<string, unknown>[]): VisualActionData;
/**
 * Whether this turn shows a chart that can be popped out.
 *
 * Deliberately the same predicate `AssistantTurn.hasChart` uses, so the control
 * appears for exactly the charts on screen. Do not substitute the Excel insert
 * recommendation (`recommendChartType`): it answers "what should we write into
 * the sheet?", not "is a chart displayed?", and the two disagree — a pie-shaped
 * result renders a chart while the insert recommendation says table.
 */
export declare function turnHasPopoutChart(artifact: {
    chart_type?: string | null;
} | null | undefined, rows: Record<string, unknown>[] | undefined): boolean;
export interface ChartPopoutPayload {
    /** Parsed `tessallite.visual.v1` artifact, when the turn carried one. */
    artifact?: Record<string, unknown> | null;
    /** Result rows, used to derive a chart when there is no artifact. */
    rows?: Record<string, unknown>[];
    title?: string;
}
/** Sent by the dialog once it is listening. */
export declare const POPOUT_READY = "tessallite:chart-popout:ready";
/** False in a plain browser, and on hosts too old to message the dialog. */
export declare function isChartPopoutSupported(): boolean;
/**
 * @param onError told why the window did not open, so the click is never a
 *   silent no-op. Office allows one dialog per add-in, so the common case is a
 *   second pop-out while the first is still open (12007).
 */
export declare function openChartPopout(payload: ChartPopoutPayload, onError?: (reason: string) => void): void;
