import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Alert,
  Box,
  Button,
  Dialog,
  DialogActions,
  DialogContent,
  DialogTitle,
  IconButton,
  Stack,
  Tab,
  Tabs,
  TextField,
  Typography,
} from "@mui/material";
import DeleteIcon from "@mui/icons-material/DeleteOutline";
import AddIcon from "@mui/icons-material/Add";
import { aliasMapApi, AliasMap } from "../../api/client";
import { useT } from "../../i18n";

type Row = { phrase: string; canonical: string };

export default function AliasMapDialog({
  open,
  onClose,
  projectId,
  modelId,
}: {
  open: boolean;
  onClose: () => void;
  projectId: string;
  modelId: string;
}) {
  const t = useT();
  const qc = useQueryClient();
  const [tab, setTab] = useState<"editor" | "import">("editor");
  const [rows, setRows] = useState<Row[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [importText, setImportText] = useState("");
  const [importMode, setImportMode] = useState<"merge" | "replace">("merge");

  const aliasMapQuery = useQuery({
    queryKey: ["alias-map", projectId, modelId],
    queryFn: () => aliasMapApi.get(projectId, modelId),
    enabled: open && Boolean(projectId && modelId),
  });

  useEffect(() => {
    if (!open) return;
    if (aliasMapQuery.data) {
      const initial = Object.entries(aliasMapQuery.data).map(
        ([phrase, canonical]) => ({ phrase, canonical }),
      );
      setRows(initial);
    } else if (aliasMapQuery.data === null || aliasMapQuery.data === undefined) {
      setRows([]);
    }
  }, [aliasMapQuery.data, open]);

  const replaceMutation = useMutation({
    mutationFn: (next: AliasMap) =>
      aliasMapApi.replace(projectId, modelId, next),
    onSuccess: () => {
      setError(null);
      qc.invalidateQueries({ queryKey: ["alias-map", projectId, modelId] });
    },
    onError: (err: { response?: { data?: { detail?: string } } }) => {
      setError(err.response?.data?.detail ?? t("aliasMap.saveFailed"));
    },
  });

  const importMutation = useMutation({
    mutationFn: (payload: { map: AliasMap; mode: "replace" | "merge" }) =>
      aliasMapApi.importJson(projectId, modelId, payload.map, payload.mode),
    onSuccess: () => {
      setError(null);
      setImportText("");
      qc.invalidateQueries({ queryKey: ["alias-map", projectId, modelId] });
      setTab("editor");
    },
    onError: (err: { response?: { data?: { detail?: string } } }) => {
      setError(err.response?.data?.detail ?? t("aliasMap.importFailed"));
    },
  });

  function addRow() {
    setRows((r) => [...r, { phrase: "", canonical: "" }]);
  }

  function updateRow(idx: number, patch: Partial<Row>) {
    setRows((r) => r.map((row, i) => (i === idx ? { ...row, ...patch } : row)));
  }

  function removeRow(idx: number) {
    setRows((r) => r.filter((_, i) => i !== idx));
  }

  function handleSave() {
    setError(null);
    const out: AliasMap = {};
    for (const r of rows) {
      const k = r.phrase.trim();
      const v = r.canonical.trim();
      if (!k && !v) continue;
      if (!k || !v) {
        setError(t("aliasMap.rowValidationError"));
        return;
      }
      if (out[k]) {
        setError(t("aliasMap.duplicatePhraseError", { phrase: k }));
        return;
      }
      out[k] = v;
    }
    replaceMutation.mutate(out);
  }

  function handleImport() {
    setError(null);
    let parsed: AliasMap;
    try {
      const raw = JSON.parse(importText);
      if (!raw || typeof raw !== "object" || Array.isArray(raw)) {
        throw new Error(t("aliasMap.jsonRootObjectError"));
      }
      parsed = {};
      for (const [k, v] of Object.entries(raw)) {
        if (typeof v !== "string") {
          throw new Error(t("aliasMap.valueMustBeString", { key: k }));
        }
        parsed[k] = v;
      }
    } catch (e) {
      setError(t("aliasMap.invalidJson", { error: (e as Error).message }));
      return;
    }
    importMutation.mutate({ map: parsed, mode: importMode });
  }

  return (
    <Dialog open={open} onClose={onClose} maxWidth="md" fullWidth>
      <DialogTitle>{t("aliasMap.title")}</DialogTitle>
      <DialogContent dividers>
        <Tabs
          value={tab}
          onChange={(_, v) => setTab(v as "editor" | "import")}
          sx={{ mb: 2 }}
        >
          <Tab value="editor" label={t("aliasMap.tabEditor")} />
          <Tab value="import" label={t("aliasMap.tabBulkImport")} />
        </Tabs>

        {error && (
          <Alert severity="error" sx={{ mb: 2 }}>
            {error}
          </Alert>
        )}

        {tab === "editor" && (
          <>
            <Alert severity="info" sx={{ mb: 2 }}>
              {t("aliasMap.editorInfo")}
            </Alert>

            <Stack spacing={1.25}>
              {rows.map((row, idx) => (
                <Box
                  key={idx}
                  sx={{
                    display: "grid",
                    gridTemplateColumns: "1fr 1fr auto",
                    gap: 1,
                    alignItems: "center",
                  }}
                >
                  <TextField
                    size="small"
                    label={t("aliasMap.phraseLabel")}
                    value={row.phrase}
                    onChange={(e) => updateRow(idx, { phrase: e.target.value })}
                  />
                  <TextField
                    size="small"
                    label={t("aliasMap.canonicalLabel")}
                    placeholder={t("aliasMap.canonicalPlaceholder")}
                    value={row.canonical}
                    onChange={(e) =>
                      updateRow(idx, { canonical: e.target.value })
                    }
                  />
                  <IconButton size="small" onClick={() => removeRow(idx)}>
                    <DeleteIcon fontSize="small" />
                  </IconButton>
                </Box>
              ))}

              {rows.length === 0 && (
                <Typography color="text.secondary" variant="body2">
                  {t("aliasMap.noAliases")}
                </Typography>
              )}

              <Box>
                <Button
                  size="small"
                  startIcon={<AddIcon />}
                  onClick={addRow}
                >
                  {t("aliasMap.addRow")}
                </Button>
              </Box>
            </Stack>
          </>
        )}

        {tab === "import" && (
          <Stack spacing={2}>
            <Alert severity="info">
              {t("aliasMap.importInfo")}
            </Alert>
            <TextField
              label={t("aliasMap.modeLabel")}
              size="small"
              select
              SelectProps={{ native: true }}
              value={importMode}
              onChange={(e) =>
                setImportMode(e.target.value as "merge" | "replace")
              }
              sx={{ width: 200 }}
            >
              <option value="merge">{t("aliasMap.modeMerge")}</option>
              <option value="replace">{t("aliasMap.modeReplace")}</option>
            </TextField>
            <TextField
              label={t("aliasMap.jsonLabel")}
              multiline
              minRows={10}
              value={importText}
              onChange={(e) => setImportText(e.target.value)}
              placeholder={t("aliasMap.jsonPlaceholder")}
            />
          </Stack>
        )}
      </DialogContent>
      <DialogActions>
        <Button onClick={onClose}>{t("common.close")}</Button>
        {tab === "editor" ? (
          <Button
            variant="contained"
            onClick={handleSave}
            disabled={replaceMutation.isPending}
          >
            {t("common.save")}
          </Button>
        ) : (
          <Button
            variant="contained"
            onClick={handleImport}
            disabled={importMutation.isPending || !importText.trim()}
          >
            {t("common.import")}
          </Button>
        )}
      </DialogActions>
    </Dialog>
  );
}
