import { useState } from "react";
import {
  Alert,
  Box,
  Button,
  CircularProgress,
  FormControl,
  InputLabel,
  List,
  ListItem,
  ListItemText,
  MenuItem,
  Select,
  Stack,
  TextField,
  Typography,
} from "@mui/material";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import {
  type CatalogImportResponse,
  type CatalogType,
  catalogImportApi,
} from "../../../api/importExportApi";
import { useT } from "../../../i18n";

type Props = { projectId: string };

const CATALOG_TYPES: { value: CatalogType; i18nKey: string }[] = [
  { value: "datahub", i18nKey: "importDialog.catalogTypeDatahub" },
  { value: "openmetadata", i18nKey: "importDialog.catalogTypeOpenmetadata" },
  { value: "alation", i18nKey: "importDialog.catalogTypeAlation" },
];

export default function CatalogImportPanel({ projectId }: Props) {
  const t = useT();
  const [catalogType, setCatalogType] = useState<CatalogType>("datahub");
  const [apiUrl, setApiUrl] = useState("");
  const [apiToken, setApiToken] = useState("");
  const [datasetFilter, setDatasetFilter] = useState("");
  const [modelName, setModelName] = useState("");
  const [result, setResult] = useState<CatalogImportResponse | null>(null);
  const queryClient = useQueryClient();

  const importMut = useMutation({
    mutationFn: () =>
      catalogImportApi.importCatalog(projectId, {
        catalog_type: catalogType,
        api_url: apiUrl.trim(),
        api_token: apiToken,
        dataset_filter: datasetFilter.trim() || undefined,
        model_name: modelName.trim() || undefined,
      }),
    onSuccess: (data) => {
      setResult(data);
      queryClient.invalidateQueries({ queryKey: ["models"] });
    },
  });

  if (result) {
    return (
      <Box>
        <Alert severity="success" sx={{ mb: 1 }}>
          {t("importDialog.catalogImported", {
            models: String(result.models_created),
            tables: String(result.tables_imported),
            measures: String(result.measures_imported),
            dimensions: String(result.dimensions_imported),
          })}
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
              {t("importDialog.warningsCount", {
                count: String(result.warnings.length),
              })}
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

  const canSubmit = apiUrl.trim().length > 0 && apiToken.length > 0;

  return (
    <Stack spacing={2}>
      <Typography variant="body2" color="text.secondary">
        {t("importDialog.catalogDescription")}
      </Typography>
      <FormControl fullWidth size="small">
        <InputLabel>{t("importDialog.catalogTypeLabel")}</InputLabel>
        <Select
          value={catalogType}
          label={t("importDialog.catalogTypeLabel")}
          onChange={(e) => {
            setCatalogType(e.target.value as CatalogType);
            setResult(null);
          }}
        >
          {CATALOG_TYPES.map((c) => (
            <MenuItem key={c.value} value={c.value}>
              {t(c.i18nKey)}
            </MenuItem>
          ))}
        </Select>
      </FormControl>
      <TextField
        size="small"
        fullWidth
        label={t("importDialog.catalogApiUrl")}
        placeholder="https://catalog.example.com"
        value={apiUrl}
        onChange={(e) => setApiUrl(e.target.value)}
      />
      <TextField
        size="small"
        fullWidth
        type="password"
        label={t("importDialog.catalogApiToken")}
        value={apiToken}
        onChange={(e) => setApiToken(e.target.value)}
      />
      <TextField
        size="small"
        fullWidth
        label={t("importDialog.catalogDatasetFilter")}
        helperText={t("importDialog.catalogDatasetFilterHelp")}
        value={datasetFilter}
        onChange={(e) => setDatasetFilter(e.target.value)}
      />
      <TextField
        size="small"
        fullWidth
        label={t("importDialog.catalogModelName")}
        value={modelName}
        onChange={(e) => setModelName(e.target.value)}
      />
      {importMut.isError && (
        <Alert severity="error">
          {(importMut.error as Error)?.message || t("importDialog.importError")}
        </Alert>
      )}
      <Box display="flex" justifyContent="flex-end" pt={1}>
        <Button
          variant="contained"
          disabled={!canSubmit || importMut.isPending}
          onClick={() => importMut.mutate()}
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
