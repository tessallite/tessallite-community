import type { TurnResponse } from "../types/turn";
interface JudgeBlockCardProps {
    turn: TurnResponse;
    onResend?: (prompt: string) => void;
    disabled?: boolean;
}
export declare function JudgeBlockCard({ onResend, disabled, }: JudgeBlockCardProps): import("react").JSX.Element;
export {};
