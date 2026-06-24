import { useEffect, useState } from "react";
import {
  Dialog,
  DialogTitle,
  DialogContent,
  DialogActions,
  Button,
  TextField,
  Typography,
} from "@mui/material";
import { useT } from "../../i18n";

interface Props {
  open: boolean;
  busy?: boolean;
  onClose: () => void;
  /** Called with the optional trimmed summary (undefined when left blank). */
  onSave: (summary: string | undefined) => void;
}

const MAX_SUMMARY = 280;

/**
 * F-013-16: collects the optional one-line summary the Save API already
 * accepts. Without this dialog the Versions history showed "N/A" for every
 * human save because the toolbar never prompted for a summary.
 */
export default function SaveVersionDialog({ open, busy, onClose, onSave }: Props) {
  const t = useT();
  const [summary, setSummary] = useState("");

  // Reset the field every time the dialog is reopened so a previous draft
  // never leaks into the next save.
  useEffect(() => {
    if (open) setSummary("");
  }, [open]);

  const submit = () => {
    const trimmed = summary.trim();
    onSave(trimmed === "" ? undefined : trimmed.slice(0, MAX_SUMMARY));
  };

  return (
    <Dialog open={open} onClose={busy ? undefined : onClose} maxWidth="sm" fullWidth>
      <DialogTitle>{t("versions.saveDialogTitle")}</DialogTitle>
      <DialogContent>
        <Typography variant="body2" color="text.secondary" sx={{ mb: 1.5 }}>
          {t("versions.saveDialogDescription")}
        </Typography>
        <TextField
          autoFocus
          fullWidth
          multiline
          minRows={2}
          maxRows={4}
          inputProps={{ maxLength: MAX_SUMMARY }}
          label={t("versions.summaryLabel")}
          placeholder={t("versions.summaryPlaceholder")}
          value={summary}
          onChange={(e) => setSummary(e.target.value)}
          onKeyDown={(e) => {
            // Ctrl/Cmd+Enter saves; plain Enter inserts a newline (multiline).
            if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) {
              e.preventDefault();
              submit();
            }
          }}
          data-testid="save-version-summary"
        />
      </DialogContent>
      <DialogActions>
        <Button onClick={onClose} disabled={busy}>
          {t("common.cancel")}
        </Button>
        <Button
          variant="contained"
          onClick={submit}
          disabled={busy}
          data-testid="save-version-confirm"
        >
          {t("versions.saveVersionButton")}
        </Button>
      </DialogActions>
    </Dialog>
  );
}
