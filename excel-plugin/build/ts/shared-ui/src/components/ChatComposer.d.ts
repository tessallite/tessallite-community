export interface ChatComposerProps {
    onSend: (text: string) => void;
    onAbort?: () => void;
    isStreaming: boolean;
    disabled?: boolean;
    initialText?: string;
    maxChars?: number;
    placeholder?: string;
}
export declare function ChatComposer({ onSend, onAbort, isStreaming, disabled, initialText, maxChars, placeholder: placeholderProp, }: ChatComposerProps): import("react").JSX.Element;
