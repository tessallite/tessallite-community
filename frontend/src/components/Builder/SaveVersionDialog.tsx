import { useEffect, useState } from "react";
import {
  Dialog,
  DialogTitle,
  DialogContent,
  DialogActions,
  Button,
  TextField,
  Typography,
  RadioGroup,
  FormControlLabel,
  Radio,
  Box,
} from "@mui/material";
import { useT } from "../../i18n";

type SaveMode = "layout" | "all";

interface Props {
  open: boolean;
  busy?: boolean;
  isDirty: boolean;
  onClose: () => void;
  onSave: (mode: SaveMode, summary?: string) => void;
}

const MAX_SUMMARY = 280;

/**
 * Two-option save dialog: layout-only (canvas positions/notes) or full model
 * version (layout + schema snapshot). The default selection tracks isDirty so
 * users landing here with unsaved model changes get "Save All" pre-selected.
 */
export default function SaveVersionDialog({ open, busy, isDirty, onClose, onSave }: Props) {
  const t = useT();
  const [summary, setSummary] = useState("");
  const [mode, setMode] = useState<SaveMode>("all");

  // Reset state every time the dialog opens: clear the summary draft and
  // pick the default radio based on whether the model has unsaved changes.
  // isDirty is intentionally read only at open-time (not in deps) so a
  // background state change cannot reset the user's in-flight radio choice.
  useEffect(() => {
    if (open) {
      setSummary("");
      setMode(isDirty ? "all" : "layout");
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open]);

  const submit = () => {
    if (mode === "layout") {
      onSave("layout");
    } else {
      const trimmed = summary.trim();
      onSave("all", trimmed === "" ? undefined : trimmed.slice(0, MAX_SUMMARY));
    }
  };

  return (
    <Dialog open={open} onClose={busy ? undefined : onClose} maxWidth="sm" fullWidth>
      <DialogTitle>
        {mode === "all"
          ? t("versions.saveDialogTitle")
          : t("versions.saveLayoutDialogTitle")}
      </DialogTitle>
      <DialogContent>
        <RadioGroup
          value={mode}
          onChange={(e) => setMode(e.target.value as SaveMode)}
        >
          <FormControlLabel
            value="layout"
            control={<Radio data-testid="save-mode-layout" />}
            label={
              <Box>
                <Typography variant="body1">
                  {t("versions.saveLayoutOnly")}
                </Typography>
                <Typography variant="body2" color="text.secondary">
                  {t("versions.saveLayoutOnlyDescription")}
                </Typography>
              </Box>
            }
            sx={{ alignItems: "flex-start", mb: 1 }}
          />
          <FormControlLabel
            value="all"
            control={<Radio data-testid="save-mode-all" />}
            label={
              <Box>
                <Typography variant="body1">
                  {t("versions.saveAllChanges")}
                </Typography>
                <Typography variant="body2" color="text.secondary">
                  {t("versions.saveAllChangesDescription")}
                </Typography>
              </Box>
            }
            sx={{ alignItems: "flex-start" }}
          />
        </RadioGroup>
        {mode === "all" && (
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
              if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) {
                e.preventDefault();
                submit();
              }
            }}
            data-testid="save-version-summary"
            sx={{ mt: 2 }}
          />
        )}
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
          {mode === "all"
            ? t("versions.saveVersionButton")
            : t("versions.saveLayoutDialogTitle")}
        </Button>
      </DialogActions>
    </Dialog>
  );
}
