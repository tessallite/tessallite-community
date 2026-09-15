import type { Citation } from "../types/turn";
export interface CitationProvenanceDialogProps {
    citation: Citation | null;
    open: boolean;
    onClose: () => void;
    onOpenTrace?: () => void;
}
/**
 * Bug-8181 (checkable citations) — clicking a citation chip previously opened
 * the same generic technical trace drawer for every chip, regardless of
 * which one was clicked (F-104-04: "citation chips are not directly
 * checkable citations" — a user cannot verify a metric's definition, route,
 * or exact supporting slice from a semantic label alone). This dialog is the
 * consumer-facing provenance view for ONE citation: its business definition,
 * the value, the route that served it, and the filter/grain slice that
 * produced it — with technical trace inspection as an optional secondary
 * action, not the only action.
 */
export declare function CitationProvenanceDialog({ citation, open, onClose, onOpenTrace, }: CitationProvenanceDialogProps): import("react").JSX.Element | null;
