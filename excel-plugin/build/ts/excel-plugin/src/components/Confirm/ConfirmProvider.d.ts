import { ReactNode } from 'react';
/**
 * R3 (alert-mechanism audit, 2026-08-25): useExcel's confirmLargeResult had a
 * ConfirmGuard abstraction clearly meant to inject a styled dialog, but every
 * real caller (App.tsx, ReportBuilder.tsx) passed `undefined` for it, so it
 * always fell through to a native, unstyled window.confirm() -- the one
 * place in the task pane that looked nothing like the rest of the plugin.
 * This mirrors ToastProvider's exact pattern (Bug-6734: one policy, one
 * context, one rendered element) so every "are you sure?" in the plugin goes
 * through the same styled Dialog.
 */
type ConfirmFn = (message: string) => Promise<boolean>;
export declare function useConfirm(): ConfirmFn;
export declare function ConfirmProvider({ children }: {
    children: ReactNode;
}): import("react/jsx-runtime").JSX.Element;
export {};
