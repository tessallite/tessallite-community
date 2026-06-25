import {
  Alert,
  Button,
  CircularProgress,
  Dialog,
  DialogActions,
  DialogContent,
  DialogTitle,
  FormControl,
  InputLabel,
  MenuItem,
  Select,
  TextField,
  Typography,
} from "@mui/material";
import type { ConnectionCreate } from "../../api/types";
import { CONN_FIELDS, isFieldVisible } from "../connectionFields";
import { renderConnFields } from "../renderConnFields";
import { useT } from "../../i18n";

export interface ConnectionDialogProps {
  open: boolean;
  mode: "create" | "edit";
  name: string;
  onNameChange: (v: string) => void;
  connType: string;
  onConnTypeChange?: (v: ConnectionCreate["connection_type"]) => void;
  fields: Record<string, string>;
  onFieldChange: (k: string, v: string) => void;
  testResult: string | null;
  isError: boolean;
  isSaving: boolean;
  isTesting: boolean;
  onTest: () => void;
  onSave: () => void;
  onClose: () => void;
  passwordHint?: boolean;
}

const CONNECTION_TYPE_LABELS: Record<string, string> = {
  postgresql: "connectionType.postgresql",
  bigquery: "connectionType.bigquery",
  hadoop_spark: "connectionType.hadoopSpark",
  redshift: "connectionType.redshift",
  snowflake: "connectionType.snowflake",
  sqlserver: "connectionType.sqlserver",
  jdbc: "connectionType.jdbc",
};

export default function ConnectionDialog({
  open,
  mode,
  name,
  onNameChange,
  connType,
  onConnTypeChange,
  fields,
  onFieldChange,
  testResult,
  isError,
  isSaving,
  isTesting,
  onTest,
  onSave,
  onClose,
  passwordHint = false,
}: ConnectionDialogProps) {
  const t = useT();
  const defs = CONN_FIELDS[connType] ?? [];

  function isValid(): boolean {
    if (!name) return false;
    for (const f of defs) {
      if (!f.required || !isFieldVisible(f, fields)) continue;
      if (f.type === "password" && mode === "edit") continue;
      if (!(fields[f.key] ?? (f as { defaultValue?: string }).defaultValue ?? "")) return false;
    }
    return true;
  }

  return (
    <Dialog open={open} onClose={onClose} maxWidth="sm" fullWidth>
      <DialogTitle>{mode === "create" ? t("connectionDialog.addTitle") : t("connectionDialog.editTitle")}</DialogTitle>
      <DialogContent>
        <TextField
          label={t("connectionDialog.displayNameLabel")}
          fullWidth
          margin="normal"
          value={name}
          onChange={(e) => onNameChange(e.target.value)}
          autoFocus
        />
        <FormControl fullWidth margin="normal">
          <InputLabel>{t("connectionDialog.connectionTypeLabel")}</InputLabel>
          <Select
            value={connType}
            label={t("connectionDialog.connectionTypeLabel")}
            onChange={(e) => {
              if (onConnTypeChange) {
                onConnTypeChange(e.target.value as ConnectionCreate["connection_type"]);
              }
            }}
          >
            {Object.entries(CONNECTION_TYPE_LABELS).map(([v, label]) => (
              <MenuItem key={v} value={v}>{t(label)}</MenuItem>
            ))}
          </Select>
        </FormControl>
        {renderConnFields(defs, fields, onFieldChange, t)}
        {passwordHint && (
          <Typography variant="caption" color="text.secondary" display="block" sx={{ mt: 1 }}>
            {t("connectionDialog.passwordHint")}
          </Typography>
        )}
        {testResult && (
          <Alert severity={testResult === "success" ? "success" : "error"} sx={{ mt: 1 }}>
            {testResult === "success"
              ? t("connectionDialog.testPassed")
              : t("connectionDialog.testFailed", { error: testResult })}
          </Alert>
        )}
        {isError && (
          <Alert severity="error" sx={{ mt: 1 }}>
            {mode === "create" ? t("connectionDialog.createFailed") : t("connectionDialog.updateFailed")}
          </Alert>
        )}
      </DialogContent>
      <DialogActions>
        <Button onClick={onClose}>{t("connectionDialog.cancelButton")}</Button>
        <Button
          variant="outlined"
          onClick={onTest}
          disabled={!isValid() || isTesting}
        >
          {isTesting ? <CircularProgress size={18} /> : t("connectionDialog.testButton")}
        </Button>
        <Button
          variant="contained"
          onClick={onSave}
          disabled={!isValid() || isSaving}
        >
          {isSaving ? <CircularProgress size={18} /> : mode === "create" ? t("connectionDialog.createButton") : t("connectionDialog.saveButton")}
        </Button>
      </DialogActions>
    </Dialog>
  );
}
