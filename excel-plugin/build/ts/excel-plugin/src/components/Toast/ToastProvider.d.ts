import { ReactNode } from 'react';
type ToastSeverity = 'success' | 'error' | 'info' | 'warning';
/**
 * Bug-6734: one policy for every toast, enforced here so no call site can
 * override or forget. success/info auto-dismiss in 5 000 ms; warnings in
 * 10 000 ms; errors persist until the user clicks the close control.
 * Exported for unit tests to verify the policy mapping.
 */
export declare const AUTO_DISMISS_MS: Record<ToastSeverity, number | null>;
interface ToastContextValue {
    showToast: (message: string, severity?: ToastSeverity) => void;
}
export declare function useToast(): ToastContextValue;
export declare function ToastProvider({ children }: {
    children: ReactNode;
}): import("react/jsx-runtime").JSX.Element;
export {};
