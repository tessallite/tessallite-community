import { useState } from "react";
import {
  Alert,
  Box,
  Button,
  CircularProgress,
  List,
  ListItem,
  ListItemText,
  Stack,
  Typography,
} from "@mui/material";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import {
  type DbtImportResponse,
  dbtImportApi,
} from "../../../api/importExportApi";
import { useT } from "../../../i18n";
import ImportWarningAlerts from "../ImportWarningAlerts";

type Props = { projectId: string };

export default function DbtImportPanel({ projectId }: Props) {
  const t = useT();
  const [file, setFile] = useState<File | null>(null);
  const [result, setResult] = useState<DbtImportResponse | null>(null);
  // F-020-03 / G-020-03: preview the loss/warning report (dry-run) BEFORE the
  // models are created, so the migration engineer sees what will not transfer.
  const [preview, setPreview] = useState<DbtImportResponse | null>(null);
  const queryClient = useQueryClient();

  const previewMut = useMutation({
    mutationFn: (f: File) => dbtImportApi.importDbt(projectId, f, true),
    onSuccess: (data) => setPreview(data),
  });

  const importMut = useMutation({
    mutationFn: (f: File) => dbtImportApi.importDbt(projectId, f),
    onSuccess: (data) => {
      setResult(data);
      setPreview(null);
      queryClient.invalidateQueries({ queryKey: ["models"] });
    },
  });

  if (result) {
    return (
      <Box>
        <Alert severity="success" sx={{ mb: 1 }}>
          {t(result.models_created === 1 ? "importDialog.dbtImportedSingular" : "importDialog.dbtImportedPlural", { parsed: String(result.models_parsed), ...(result.models_created !== 1 ? { created: String(result.models_created) } : {}) })}
        </Alert>
        {result.model_names.length > 0 && (
          <>
            <Typography variant="subtitle2" sx={{ mb: 0.5 }}>
              {t("importDialog.modelsFound")}
            </Typography>
            <List dense disablePadding>
              {result.model_names.map((name) => (
                <ListItem key={name} disableGutters sx={{ py: 0 }}>
                  <ListItemText primary={name} />
                </ListItem>
              ))}
            </List>
          </>
        )}
        <ImportWarningAlerts warnings={result.warnings} t={t} />
      </Box>
    );
  }

  return (
    <Stack spacing={2}>
      <Typography variant="body2" color="text.secondary">
        {t("importDialog.dbtDescription")}
      </Typography>
      <Box>
        <Button variant="outlined" component="label">
          {file ? file.name : t("importDialog.chooseDbtFile")}
          <input
            type="file"
            accept=".yml,.yaml,.zip"
            hidden
            onChange={(e) => {
              setFile(e.target.files?.[0] ?? null);
              setResult(null);
              setPreview(null);
            }}
          />
        </Button>
      </Box>
      {(previewMut.isError || importMut.isError) && (
        <Alert severity="error">
          {((previewMut.error || importMut.error) as Error)?.message ||
            t("importDialog.importError")}
        </Alert>
      )}
      {preview && (
        <Box>
          <Alert severity="info" sx={{ mb: 1 }}>
            {t("importDialog.previewSummary", {
              parsed: String(preview.models_parsed),
            })}
          </Alert>
          {preview.model_names.length > 0 && (
            <>
              <Typography variant="subtitle2" sx={{ mb: 0.5 }}>
                {t("importDialog.modelsFound")}
              </Typography>
              <List dense disablePadding>
                {preview.model_names.map((name) => (
                  <ListItem key={name} disableGutters sx={{ py: 0 }}>
                    <ListItemText primary={name} />
                  </ListItem>
                ))}
              </List>
            </>
          )}
          <ImportWarningAlerts warnings={preview.warnings} t={t} />
        </Box>
      )}
      <Box display="flex" justifyContent="flex-end" gap={1} pt={1}>
        <Button
          variant="outlined"
          disabled={!file || previewMut.isPending}
          onClick={() => file && previewMut.mutate(file)}
        >
          {previewMut.isPending ? (
            <CircularProgress size={18} color="inherit" />
          ) : (
            t("importDialog.previewButton")
          )}
        </Button>
        <Button
          variant="contained"
          // F-020-03: require a preview of the current file first.
          disabled={!file || !preview || importMut.isPending}
          onClick={() => file && importMut.mutate(file)}
        >
          {importMut.isPending ? (
            <CircularProgress size={18} color="inherit" />
          ) : (
            t("importDialog.importButton")
          )}
        </Button>
      </Box>
    </Stack>
  );
}
