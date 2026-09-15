import type { TurnResponse } from "../types/turn";
import type { TraceVisibility } from "./TraceStrip";
export declare function TraceDrawer({ open, onClose, turn, visibility, }: {
    open: boolean;
    onClose: () => void;
    turn: TurnResponse | null;
    visibility: TraceVisibility;
}): import("react").JSX.Element | null;
