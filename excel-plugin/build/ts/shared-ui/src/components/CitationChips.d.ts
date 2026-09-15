import type { Citation } from "../types/turn";
interface CitationChipsProps {
    citations: Citation[];
    onClick?: (citation: Citation, index: number) => void;
}
export declare function formatValue(value: unknown): string | null;
export declare function CitationChips({ citations, onClick }: CitationChipsProps): import("react").JSX.Element | null;
export {};
