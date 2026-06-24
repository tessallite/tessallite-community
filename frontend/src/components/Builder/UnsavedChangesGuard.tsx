import { useEffect } from "react";
import { useT } from "../../i18n";
import { useBlocker } from "react-router-dom";
import { useModelEditorStore } from "../../store/useModelEditorStore";
import { useConfirm } from "../Confirm";

/**
 * Mounts inside the Model Builder. While there are unsaved edits:
 *
 *   - Closing the browser tab fires the native confirm dialog
 *     (text controlled by the browser; modern browsers ignore custom
 *     strings for security reasons).
 *
 *   - Navigating to another React Router route opens the Tessallite
 *     ConfirmDialog asking the user to confirm the discard.
 *
 * When the model is clean both guards are no-ops.
 */
export default function UnsavedChangesGuard() {
  const t = useT();
  const isDirty = useModelEditorStore((s) => s.isDirty);
  const confirm = useConfirm();

  // Native beforeunload — fires for tab close, browser back/forward to a
  // different origin, page refresh, etc.
  useEffect(() => {
    if (!isDirty) return;
    const handler = (e: BeforeUnloadEvent) => {
      e.preventDefault();
      e.returnValue = t("unsavedChanges.nativeWarning");
      return t("unsavedChanges.nativeWarning");
    };
    window.addEventListener("beforeunload", handler);
    return () => window.removeEventListener("beforeunload", handler);
  }, [isDirty]);

  // React Router intra-app navigation — opens the Tessallite dialog.
  const blocker = useBlocker(({ currentLocation, nextLocation }) =>
    isDirty && currentLocation.pathname !== nextLocation.pathname,
  );

  useEffect(() => {
    if (blocker.state !== "blocked") return;
    let cancelled = false;
    void (async () => {
      const ok = await confirm({
        title: t("unsavedChanges.discardTitle"),
        message: t("unsavedChanges.discardMessage"),
        confirmLabel: t("unsavedChanges.discardLabel"),
        destructive: true,
      });
      if (cancelled) return;
      if (ok) blocker.proceed?.();
      else blocker.reset?.();
    })();
    return () => {
      cancelled = true;
    };
  }, [blocker, confirm]);

  return null;
}
