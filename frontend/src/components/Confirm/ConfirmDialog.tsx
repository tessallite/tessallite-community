import { useEffect, useState } from "react";
import {
  Alert,
  Box,
  Button,
  CircularProgress,
  Dialog,
  DialogActions,
  DialogContent,
  DialogContentText,
  DialogTitle,
  TextField,
  Typography,
} from "@mui/material";
import { useT } from "../../i18n";

/**
 * Two-tier confirmation dialog.
 *
 * - mode="simple": plain Yes/No prompt for low-risk deletes
 *   (dimension, measure, join, UDA, refresh policy, etc.).
 *
 * - mode="typed-name": the destructive button stays disabled until
 *   the user types `confirmText` exactly. Used for high-risk deletes
 *   (model, project, workspace, tenant, user, version revert).
 *
 * The component is purely controlled and is normally driven by
 * `useConfirm()` rather than mounted directly.
 */
export type ConfirmDialogProps = {
  open: boolean;
  mode?: "simple" | "typed-name";
  title: string;
  message: string | React.ReactNode;
  confirmText?: string;          // typed-name mode: the literal the user must match
  confirmLabel?: string;          // button label, default "Delete"
  cancelLabel?: string;           // default "Cancel"
  destructive?: boolean;          // colours the confirm button red, default true
  consequences?: React.ReactNode; // optional callout listing what will break
  busy?: boolean;                 // shows spinner on confirm button
  errorMessage?: string;          // shown above the buttons if the action failed
  onConfirm: () => void | Promise<void>;
  onCancel: () => void;
};

export default function ConfirmDialog({
  open,
  mode = "simple",
  title,
  message,
  confirmText,
  confirmLabel,
  cancelLabel,
  destructive = true,
  consequences,
  busy = false,
  errorMessage,
  onConfirm,
  onCancel,
}: ConfirmDialogProps) {
  const t = useT();
  const resolvedConfirmLabel = confirmLabel ?? t("confirm.defaultDelete");
  const resolvedCancelLabel = cancelLabel ?? t("confirm.defaultCancel");

  const [typed, setTyped] = useState("");

  // Reset the typed value every time the dialog re-opens so a stale
  // value from a previous use doesn't carry over.
  useEffect(() => {
    if (open) setTyped("");
  }, [open]);

  const requireTyped = mode === "typed-name" && Boolean(confirmText);
  const typedOk = !requireTyped || typed.trim() === (confirmText ?? "").trim();
  const confirmDisabled = busy || !typedOk;

  return (
    <Dialog
      open={open}
      onClose={busy ? undefined : onCancel}
      maxWidth="xs"
      fullWidth
      // Prevent the dialog from auto-focusing the first button so
      // typed-name mode can autofocus its text input instead.
      disableRestoreFocus
    >
      <DialogTitle>{title}</DialogTitle>
      <DialogContent>
        <DialogContentText component="div">{message}</DialogContentText>

        {consequences ? (
          <Alert severity="warning" sx={{ mt: 2 }}>
            {consequences}
          </Alert>
        ) : null}

        {requireTyped ? (
          <Box sx={{ mt: 2 }}>
            <Typography variant="body2" sx={{ mb: 1 }}>
              {t("confirm.typeLabel")}{" "}
              <Box component="code" sx={{ fontFamily: "monospace", fontWeight: 600 }}>
                {confirmText}
              </Box>{" "}
              {t("confirm.toConfirm")}
            </Typography>
            <TextField
              autoFocus
              fullWidth
              size="small"
              value={typed}
              onChange={(e) => setTyped(e.target.value)}
              placeholder={confirmText}
              disabled={busy}
              inputProps={{ "data-testid": "confirm-typed-input" }}
            />
          </Box>
        ) : null}

        {errorMessage ? (
          <Alert severity="error" sx={{ mt: 2 }}>
            {errorMessage}
          </Alert>
        ) : null}
      </DialogContent>
      <DialogActions>
        <Button onClick={onCancel} disabled={busy}>
          {resolvedCancelLabel}
        </Button>
        <Button
          onClick={onConfirm}
          variant="contained"
          color={destructive ? "error" : "primary"}
          disabled={confirmDisabled}
          data-testid="confirm-action-button"
        >
          {busy ? <CircularProgress size={16} color="inherit" /> : resolvedConfirmLabel}
        </Button>
      </DialogActions>
    </Dialog>
  );
}
