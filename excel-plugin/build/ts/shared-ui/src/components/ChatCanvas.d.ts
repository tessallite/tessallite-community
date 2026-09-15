import type { TurnResponse } from "../types/turn";
import type { TraceVisibility } from "./TraceStrip";
export interface ChatCanvasProps {
    /** Parent-owned authority transition gate. */
    disabled?: boolean;
    visibility?: TraceVisibility;
    onFeedback?: (turnId: string, vote: "up" | "down") => void;
    feedbackEnabled?: boolean;
    onOpenTrace?: (turn: TurnResponse) => void;
    maxChars?: number;
    composerPlaceholder?: string;
    echartsTheme?: Record<string, unknown>;
    chartsCss?: string;
    showModelPicker?: boolean;
    headerSlot?: React.ReactNode;
    renderTurnActions?: (turn: TurnResponse, resultRows?: Record<string, unknown>[]) => React.ReactNode;
}
export declare function ChatCanvas({ disabled, visibility, onFeedback, feedbackEnabled, onOpenTrace, maxChars, composerPlaceholder, echartsTheme, chartsCss, showModelPicker, headerSlot, renderTurnActions, }: ChatCanvasProps): import("react").JSX.Element;
