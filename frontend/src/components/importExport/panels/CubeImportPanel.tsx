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
  type CubeImportResponse,
  cubeImportApi,
} from "../../../api/importExportApi";
import { useT } from "../../../i18n";

type Props = { projectId: string };

export default function CubeImportPanel({ projectId }: Props) {
  const t = useT();
  const [file, setFile] = useState<File | null>(null);
  const [result, setResult] = useState<CubeImportResponse | null>(null);
  const queryClient = useQueryClient();

  const importMut = useMutation({
    mutationFn: (f: File) => cubeImportApi.importCube(projectId, f),
    onSuccess: (data) => {
      setResult(data);
      queryClient.invalidateQueries({ queryKey: ["models"] });
    },
  });

  if (result) {
    return (
      <Box>
        <Alert severity="success" sx={{ mb: 1 }}>
          {t(result.models_created === 1 ? "importDialog.cubeImportedSingular" : "importDialog.cubeImportedPlural", { parsed: String(result.models_parsed), ...(result.models_created !== 1 ? { created: String(result.models_created) } : {}) })}
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
        {result.warnings.length > 0 && (
          <Alert severity="warning" sx={{ mt: 1 }}>
            <Typography variant="subtitle2" sx={{ mb: 0.5 }}>
              {t("importDialog.warningsCount", { count: String(result.warnings.length) })}
            </Typography>
            {result.warnings.map((w, i) => (
              <Typography key={i} variant="body2" sx={{ mb: 0.5 }}>
                {w}
              </Typography>
            ))}
          </Alert>
        )}
      </Box>
    );
  }

  return (
    <Stack spacing={2}>
      <Typography variant="body2" color="text.secondary">
        {t("importDialog.cubeDescription")}
      </Typography>
      <Box>
        <Button variant="outlined" component="label">
          {file ? file.name : t("importDialog.chooseCubeFile")}
          <input
            type="file"
            accept=".yml,.yaml,.zip"
            hidden
            onChange={(e) => {
              setFile(e.target.files?.[0] ?? null);
              setResult(null);
            }}
          />
        </Button>
      </Box>
      {importMut.isError && (
        <Alert severity="error">
          {(importMut.error as Error)?.message || t("importDialog.importError")}
        </Alert>
      )}
      <Box display="flex" justifyContent="flex-end" pt={1}>
        <Button
          variant="contained"
          disabled={!file || importMut.isPending}
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
