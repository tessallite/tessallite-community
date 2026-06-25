import {
  Box,
  Button,
  Checkbox,
  Chip,
  FormControl,
  FormControlLabel,
  InputLabel,
  MenuItem,
  Select,
  TextField,
  Typography,
} from "@mui/material";
import { isFieldVisible, type ConnField } from "./connectionFields";

/**
 * Renders a list of ConnField definitions as MUI form controls.
 * Shared by Connections page and SourcesPanel / TargetPanel.
 */
export function renderConnFields(
  fields: ConnField[],
  values: Record<string, string>,
  onChange: (key: string, val: string) => void,
  t: (key: string) => string = (k) => k,
) {
  return fields
    .filter((f) => isFieldVisible(f, values))
    .map((f) => {
      if (f.type === "select") {
        return (
          <FormControl key={f.key} fullWidth margin="normal">
            <InputLabel>{t(f.label)}</InputLabel>
            <Select
              value={values[f.key] ?? f.defaultValue ?? ""}
              label={t(f.label)}
              onChange={(e) => onChange(f.key, e.target.value)}
            >
              {f.options?.map((o) => (
                <MenuItem key={o.value} value={o.value}>
                  {t(o.label)}
                </MenuItem>
              ))}
            </Select>
          </FormControl>
        );
      }

      if (f.type === "checkbox") {
        const checked = (values[f.key] ?? f.defaultValue ?? "") === "true";
        return (
          <Box key={f.key} sx={{ mt: 1 }}>
            <FormControlLabel
              control={
                <Checkbox
                  checked={checked}
                  onChange={(e) => onChange(f.key, e.target.checked ? "true" : "false")}
                />
              }
              label={t(f.label)}
            />
            {f.helperText && (
              <Typography variant="caption" color="text.secondary" display="block" sx={{ ml: 4 }}>
                {t(f.helperText)}
              </Typography>
            )}
          </Box>
        );
      }

      if (f.type === "file") {
        return (
          <Box key={f.key} sx={{ mt: 2, mb: 1 }}>
            <Typography variant="body2" fontWeight={600} mb={0.5}>
              {t(f.label)} {f.required && t("connFields.requiredIndicator")}
            </Typography>
            {f.helperText && (
              <Typography
                variant="caption"
                color="text.secondary"
                display="block"
                mb={1}
              >
                {t(f.helperText)}
              </Typography>
            )}
            <Button
              variant="outlined"
              component="label"
              size="small"
              sx={{ mr: 1 }}
            >
              {t("connFields.uploadJson")}
              <input
                type="file"
                accept=".json"
                hidden
                onChange={(e) => {
                  const file = e.target.files?.[0];
                  if (!file) return;
                  const reader = new FileReader();
                  reader.onload = () =>
                    onChange(f.key, reader.result as string);
                  reader.readAsText(file);
                }}
              />
            </Button>
            {values[f.key] && (
              <Chip
                label={t("connFields.fileLoaded")}
                size="small"
                color="success"
                sx={{ ml: 1 }}
              />
            )}
            <Typography
              variant="caption"
              color="text.secondary"
              display="block"
              mt={1}
            >
              {t("connFields.pasteJson")}
            </Typography>
            <TextField
              fullWidth
              margin="dense"
              multiline
              rows={3}
              value={values[f.key] ?? ""}
              onChange={(e) => onChange(f.key, e.target.value)}
              placeholder={t("connFields.jsonPlaceholder")}
              size="small"
            />
          </Box>
        );
      }

      return (
        <TextField
          key={f.key}
          label={`${t(f.label)}${f.required ? ` ${t("connFields.requiredIndicator")}` : ""}`}
          fullWidth
          margin="normal"
          type={
            f.type === "password"
              ? "password"
              : f.type === "number"
                ? "number"
                : "text"
          }
          value={values[f.key] ?? ""}
          onChange={(e) => onChange(f.key, e.target.value)}
          placeholder={f.placeholder}
          helperText={f.helperText ? t(f.helperText) : undefined}
        />
      );
    });
}
