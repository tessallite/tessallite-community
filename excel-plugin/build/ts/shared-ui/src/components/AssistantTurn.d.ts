import type { TurnResponse } from "../types/turn";
import type { TraceVisibility } from "./TraceStrip";
export interface AssistantTurnProps {
    turn: TurnResponse;
    resultRows?: Record<string, unknown>[];
    visibility?: TraceVisibility;
    onRephrase?: (originalMessage: string) => void;
    onResend?: (prompt: string) => void;
    onFeedback?: (vote: "up" | "down") => void;
    feedbackEnabled?: boolean;
    onOpenTrace?: () => void;
    suggestedQuestions?: string[];
    onSelectQuestion?: (q: string) => void;
    echartsTheme?: Record<string, unknown>;
    chartsCss?: string;
    actionsDisabled?: boolean;
}
export declare function AssistantTurn({ turn, resultRows, visibility, onRephrase, onResend, onFeedback, feedbackEnabled, onOpenTrace, suggestedQuestions, onSelectQuestion, echartsTheme, chartsCss, actionsDisabled, }: AssistantTurnProps): import("react").JSX.Element;
