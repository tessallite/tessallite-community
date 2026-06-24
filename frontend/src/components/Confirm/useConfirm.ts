import { createContext, useCallback, useContext, useState } from "react";
import type { ConfirmDialogProps } from "./ConfirmDialog";

/**
 * Promise-returning confirmation helper.
 *
 * Usage:
 *
 *     const confirm = useConfirm();
 *     const ok = await confirm({
 *       title: "Delete dimension?",
 *       message: "This cannot be undone.",
 *     });
 *     if (ok) deleteDimension(id);
 *
 * For typed-name confirmations:
 *
 *     const ok = await confirm({
 *       mode: "typed-name",
 *       title: "Delete project?",
 *       message: "Every model in this project is removed.",
 *       confirmText: project.slug,
 *     });
 */
export type ConfirmOptions = Omit<
  ConfirmDialogProps,
  "open" | "onConfirm" | "onCancel" | "busy" | "errorMessage"
>;

type ConfirmFn = (opts: ConfirmOptions) => Promise<boolean>;

export const ConfirmContext = createContext<ConfirmFn | null>(null);

export function useConfirm(): ConfirmFn {
  const fn = useContext(ConfirmContext);
  if (!fn) {
    throw new Error(
      "useConfirm must be used inside <ConfirmProvider>; wrap App.tsx if you see this error.",
    );
  }
  return fn;
}

/** Internal — the provider's state slice. */
export type PendingConfirm = {
  opts: ConfirmOptions;
  resolve: (ok: boolean) => void;
};

/** Hook used by the provider; returns the state + the confirm function. */
export function useConfirmState() {
  const [pending, setPending] = useState<PendingConfirm | null>(null);

  const confirm = useCallback<ConfirmFn>((opts) => {
    return new Promise<boolean>((resolve) => {
      setPending({ opts, resolve });
    });
  }, []);

  const handleConfirm = useCallback(() => {
    if (!pending) return;
    pending.resolve(true);
    setPending(null);
  }, [pending]);

  const handleCancel = useCallback(() => {
    if (!pending) return;
    pending.resolve(false);
    setPending(null);
  }, [pending]);

  return { pending, confirm, handleConfirm, handleCancel };
}
