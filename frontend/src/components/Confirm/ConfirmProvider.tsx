import ConfirmDialog from "./ConfirmDialog";
import { ConfirmContext, useConfirmState } from "./useConfirm";

/**
 * Wrap App.tsx in this provider so any descendant can call useConfirm().
 *
 * Exposes a single ConfirmDialog instance whose props are driven by
 * whichever component is currently waiting on a confirmation. Stacked
 * confirmations are not supported (the use case hasn't come up); the
 * second confirm() call would replace the first dialog's resolution.
 */
export default function ConfirmProvider({
  children,
}: {
  children: React.ReactNode;
}) {
  const { pending, confirm, handleConfirm, handleCancel } = useConfirmState();

  return (
    <ConfirmContext.Provider value={confirm}>
      {children}
      <ConfirmDialog
        open={pending !== null}
        mode={pending?.opts.mode}
        title={pending?.opts.title ?? ""}
        message={pending?.opts.message ?? ""}
        confirmText={pending?.opts.confirmText}
        confirmLabel={pending?.opts.confirmLabel}
        cancelLabel={pending?.opts.cancelLabel}
        destructive={pending?.opts.destructive}
        consequences={pending?.opts.consequences}
        onConfirm={handleConfirm}
        onCancel={handleCancel}
      />
    </ConfirmContext.Provider>
  );
}
