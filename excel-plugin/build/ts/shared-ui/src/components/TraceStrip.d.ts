import type { TurnResponse } from "../types/turn";
export interface TraceVisibility {
    showThoughtProcess: boolean;
    showSemanticQuery: boolean;
    showPhysicalQuery: boolean;
}
export declare function TraceStrip({ turn, visibility, }: {
    turn: TurnResponse;
    visibility: TraceVisibility;
}): import("react").JSX.Element | null;
