import { useMemo, useState } from "react";
import {
  Alert,
  Box,
  Button,
  Checkbox,
  CircularProgress,
  FormControlLabel,
  MenuItem,
  Stack,
  TextField,
  Typography,
} from "@mui/material";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { connectionsApi } from "../../../api/client";
import {
  type ExportBundle,
  importExportApi,
} from "../../../api/importExportApi";
import { useT } from "../../../i18n";
import { extractStubsFromBundle, readFileAsText } from "../helpers";

type Props = {
  projectId: string;
  onImported?: (modelId: string) => void;
};

export default function ModelImportPanel({ projectId, onImported }: Props) {
  const t = useT();
  const qc = useQueryClient();
  const [bundle, setBundle] = useState<ExportBundle | null>(null);
  const [parseError, setParseError] = useState<string | null>(null);
  const [targetSlug, setTargetSlug] = useState("");
  const [targetName, setTargetName] = useState("");
  const [deployImmediately, setDeployImmediately] = useState(false);
  const [mapping, setMapping] = useState<Record<string, string>>({});

  const connectionsQ = useQuery({
    queryKey: ["connections", projectId],
    queryFn: () => connectionsApi.list(projectId),
  });

  const stubs = useMemo(
    () => (bundle ? extractStubsFromBundle(bundle) : []),
    [bundle],
  );

  async function handleFile(e: React.ChangeEvent<HTMLInputElement>) {
    const f = e.target.files?.[0];
    if (!f) return;
    try {
      const text = await readFileAsText(f);
      const parsed = JSON.parse(text) as ExportBundle;
      if (parsed.export_format !== "tessallite-model/v1") {
        throw new Error(t("importDialog.unrecognizedFormat", { format: parsed.export_format }));
      }
      setBundle(parsed);
      setParseError(null);
      setTargetSlug(parsed.model_slug || "");
      setTargetName(parsed.model_display_name || "");
    } catch (err) {
      setBundle(null);
      setParseError(err instanceof Error ? err.message : String(err));
    }
  }

  const importMut = useMutation({
    mutationFn: () => {
      if (!bundle) throw new Error("No bundle loaded");
      return importExportApi.importModel(projectId, {
        bundle,
        target_project_id: projectId,
        target_slug: targetSlug || null,
        target_display_name: targetName || null,
        connection_mapping: mapping,
        deploy_immediately: deployImmediately,
      });
    },
    onSuccess: (resp) => {
      qc.invalidateQueries({ queryKey: ["models", projectId] });
      onImported?.(resp.model_id);
    },
  });

  const allMapped = stubs.every((s) => Boolean(mapping[s.id]));
  const canSubmit = Boolean(bundle) && allMapped && !importMut.isPending;

  return (
    <Stack spacing={2}>
      <Typography variant="body2" color="text.secondary">
        {t("importDialog.modelDescription")}
      </Typography>
      <Button variant="outlined" component="label">
        {t("importDialog.chooseModelFile")}
        <input
          type="file"
          accept="application/json,.json,.tessallite.json"
          hidden
          onChange={handleFile}
        />
      </Button>
      {parseError && <Alert severity="error">{parseError}</Alert>}

      {bundle && (
        <>
          <TextField
            label={t("importDialog.slugLabel")}
            value={targetSlug}
            onChange={(e) => setTargetSlug(e.target.value)}
            helperText={t("importDialog.slugHelp")}
          />
          <TextField
            label={t("importDialog.displayNameLabel")}
            value={targetName}
            onChange={(e) => setTargetName(e.target.value)}
          />

          <Typography variant="subtitle2">{t("importDialog.rebindConnections")}</Typography>
          <Typography variant="body2" color="text.secondary">
            {t("importDialog.rebindDesc")}
          </Typography>
          {connectionsQ.isLoading && <CircularProgress size={20} />}
          {connectionsQ.data &&
            stubs.map((s) => {
              const compatible = connectionsQ.data.filter(
                (c) =>
                  !s.connection_type ||
                  c.connection_type === s.connection_type,
              );
              return (
                <Box
                  key={s.id}
                  sx={{
                    display: "grid",
                    gridTemplateColumns: "1fr 1fr",
                    gap: 1,
                  }}
                >
                  <Typography variant="body2" sx={{ alignSelf: "center" }}>
                    {s.role === "source" ? t("importDialog.sourceLabel") : t("importDialog.targetLabel")}:{" "}
                    {s.display_name || s.id}
                    {s.connection_type ? ` (${s.connection_type})` : ""}
                  </Typography>
                  <TextField
                    select
                    size="small"
                    value={mapping[s.id] || ""}
                    onChange={(e) =>
                      setMapping((m) => ({ ...m, [s.id]: e.target.value }))
                    }
                  >
                    {compatible.length === 0 && (
                      <MenuItem value="" disabled>
                        {t("importDialog.noCompatibleConn")}
                      </MenuItem>
                    )}
                    {compatible.map((c) => (
                      <MenuItem key={c.id} value={c.id}>
                        {c.display_name}
                      </MenuItem>
                    ))}
                  </TextField>
                </Box>
              );
            })}

          <FormControlLabel
            control={
              <Checkbox
                checked={deployImmediately}
                onChange={(_, v) => setDeployImmediately(v)}
              />
            }
            label={t("importDialog.deployImmediately")}
          />

          {importMut.isError && (
            <Alert severity="error">
              {(importMut.error as Error)?.message || t("importDialog.importError")}
            </Alert>
          )}

          <Box display="flex" justifyContent="flex-end" pt={1}>
            <Button
              variant="contained"
              disabled={!canSubmit}
              onClick={() => importMut.mutate()}
            >
              {importMut.isPending ? (
                <CircularProgress size={18} color="inherit" />
              ) : (
                t("importDialog.importButton")
              )}
            </Button>
          </Box>
        </>
      )}
    </Stack>
  );
}
