import { useEffect, useMemo, useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import {
  Alert,
  Box,
  Button,
  CircularProgress,
  FormControl,
  InputLabel,
  Link,
  MenuItem,
  Select,
  Stack,
  TextField,
  Typography,
} from "@mui/material";
import { modelTablesApi, modelsApi, dimensionsApi, measuresApi } from "../../../api/client";
import type { ModelTable } from "../../../api/types";
import type { RenamePreviewRow } from "./AttributeRenameDialog";
import AttributeRenameDialog from "./AttributeRenameDialog";
import { useT } from "../../../i18n";

type TableTypeValue = "fact" | "dim_aggregate" | "dim_detail" | "calendar" | "unclassified";

interface Props {
  projectId: string;
  modelId: string;
  sourceId: string;
  table: ModelTable;
}

export default function GeneralTab({ projectId, modelId, sourceId, table }: Props) {
  const t = useT();
  const qc = useQueryClient();
  const [aliasDraft, setAliasDraft] = useState(table.alias);
  const [displayNameDraft, setDisplayNameDraft] = useState(table.display_name);
  const [typeDraft, setTypeDraft] = useState<TableTypeValue>(
    (table.table_type as TableTypeValue) || "unclassified",
  );

  // Rename dialog state.
  const [renameOpen, setRenameOpen] = useState(false);
  const [renameRows, setRenameRows] = useState<RenamePreviewRow[]>([]);
  const [renameTaken, setRenameTaken] = useState<Set<string>>(new Set());
  const [oldAlias, setOldAlias] = useState<string>("");
  const [renameApplying, setRenameApplying] = useState(false);
  const [renameSuccess, setRenameSuccess] = useState(false);

  // Re-hydrate when the dialog is reopened against a different table.
  useEffect(() => {
    setAliasDraft(table.alias);
    setDisplayNameDraft(table.display_name);
    setTypeDraft((table.table_type as TableTypeValue) || "unclassified");
  }, [table.id, table.alias, table.display_name, table.table_type]);

  const saveMutation = useMutation({
    mutationFn: async () => {
      const patch: { table_type?: TableTypeValue; alias?: string; display_name?: string } = {};
      if (typeDraft !== table.table_type) patch.table_type = typeDraft;
      const aliasTrim = aliasDraft.trim();
      if (aliasTrim && aliasTrim !== table.alias) patch.alias = aliasTrim;
      const displayTrim = displayNameDraft.trim();
      if (displayTrim && displayTrim !== table.display_name) patch.display_name = displayTrim;
      if (Object.keys(patch).length === 0) return null;
      await modelTablesApi.update(projectId, modelId, sourceId, table.id, patch);
      return patch;
    },
    onSuccess: async (patch) => {
      if (!patch) return;
      const displayTrim = displayNameDraft.trim();
      if (displayTrim && displayTrim !== table.display_name) {
        window.dispatchEvent(
          new CustomEvent("canvas-history-action", {
            detail: {
              action: {
                type: "rename",
                tableId: table.id,
                sourceId,
                oldDisplayName: table.display_name,
                newDisplayName: displayTrim,
              },
            },
          }),
        );
      }
      qc.invalidateQueries({ queryKey: ["modelTables", projectId, modelId, sourceId] });
      qc.invalidateQueries({ queryKey: ["allModelTables", projectId, modelId] });
      qc.invalidateQueries({ queryKey: ["joins", projectId, modelId] });
      qc.invalidateQueries({ queryKey: ["dimensions", projectId, modelId] });
      qc.invalidateQueries({ queryKey: ["measures", projectId, modelId] });

      // If alias changed, run the rename sweep.
      if (patch.alias) {
        try {
          const preview = await modelTablesApi.renamePreview(
            projectId, modelId, sourceId, table.id, patch.alias,
          );
          if (preview.length > 0) {
            // Build taken set: all model attr names excluding the candidates.
            const candidateIds = new Set(preview.map((r) => r.id));
            const [dims, meas] = await Promise.all([
              dimensionsApi.list(projectId, modelId),
              measuresApi.list(projectId, modelId),
            ]);
            const taken = new Set<string>([
              ...dims.filter((d) => !candidateIds.has(d.id)).map((d) => d.name.toLowerCase()),
              ...meas.filter((m) => !candidateIds.has(m.id)).map((m) => m.name.toLowerCase()),
            ]);
            setOldAlias(table.alias);
            setRenameRows(preview as RenamePreviewRow[]);
            setRenameTaken(taken);
            setRenameOpen(true);
          }
        } catch {
          // Preview failure is non-blocking; the alias change already succeeded.
        }
      }
    },
  });

  async function handleRenameApply(
    final: Array<{ type: string; id: string; name: string }>,
  ) {
    // Filter to rows that actually changed.
    const changed = final.filter((f) => {
      const row = renameRows.find((r) => r.id === f.id);
      return row && f.name !== row.current_name;
    });
    if (changed.length === 0) {
      setRenameOpen(false);
      return;
    }
    setRenameApplying(true);
    try {
      await modelsApi.bulkRenameAttributes(projectId, modelId, changed);
      qc.invalidateQueries({ queryKey: ["dimensions", projectId, modelId] });
      qc.invalidateQueries({ queryKey: ["measures", projectId, modelId] });
      setRenameOpen(false);
      setRenameSuccess(true);
    } finally {
      setRenameApplying(false);
    }
  }

  async function handleRenameRevert() {
    // Patch alias back to the old value.
    setRenameOpen(false);
    try {
      await modelTablesApi.update(projectId, modelId, sourceId, table.id, { alias: oldAlias });
      qc.invalidateQueries({ queryKey: ["modelTables", projectId, modelId, sourceId] });
      qc.invalidateQueries({ queryKey: ["allModelTables", projectId, modelId] });
    } catch {
      // Best-effort.
    }
  }

  const aliasTrim = aliasDraft.trim();
  const displayTrim = displayNameDraft.trim();
  const aliasChanged = !!aliasTrim && aliasTrim !== table.alias;
  const displayChanged = !!displayTrim && displayTrim !== table.display_name;
  const typeChanged = typeDraft !== table.table_type;
  const dirty = aliasChanged || displayChanged || typeChanged;

  // takenNames for the dialog is precomputed; use a stable ref.
  const stableTaken = useMemo(() => renameTaken, [renameTaken]);

  return (
    <Stack spacing={1.5} sx={{ mt: 1 }}>
      <Typography variant="caption" color="text.secondary">
        {t("tableEditGeneral.physicalLabel")} <span style={{ opacity: 0.7 }}>{table.physical_name}</span>
      </Typography>
      <TextField
        label={t("tableEditGeneral.aliasLabel")}
        size="small"
        fullWidth
        value={aliasDraft}
        onChange={(e) => setAliasDraft(e.target.value)}
        helperText={t("tableEditGeneral.aliasHelperText")}
      />
      <TextField
        label={t("tableEditGeneral.displayNameLabel")}
        size="small"
        fullWidth
        value={displayNameDraft}
        onChange={(e) => setDisplayNameDraft(e.target.value)}
        helperText={t("tableEditGeneral.displayNameHelperText")}
      />
      <FormControl size="small" fullWidth>
        <InputLabel>{t("tableEditGeneral.typeLabel")}</InputLabel>
        <Select
          value={typeDraft}
          label={t("tableEditGeneral.typeLabel")}
          onChange={(e) => setTypeDraft(e.target.value as TableTypeValue)}
        >
          <MenuItem value="fact">{t("tableEditGeneral.typeFact")}</MenuItem>
          <MenuItem value="dim_aggregate">{t("tableEditGeneral.typeDimAggregate")}</MenuItem>
          <MenuItem value="dim_detail">{t("tableEditGeneral.typeDimDetail")}</MenuItem>
          <MenuItem value="calendar">{t("tableEditGeneral.typeCalendar")}</MenuItem>
          <MenuItem value="unclassified">{t("tableEditGeneral.typeUnclassified")}</MenuItem>
        </Select>
      </FormControl>
      <Typography variant="caption" color="text.secondary">
        {t("tableEditGeneral.typeHelperText")}{" "}
        <Link href="/help/modelling/dimension-aliases.html" target="_blank" rel="noopener">
          {t("tableEditGeneral.learnMore")}
        </Link>
      </Typography>
      {saveMutation.isError && (
        <Alert severity="error">
          {(saveMutation.error as { response?: { data?: { detail?: string } } })?.response?.data
            ?.detail ?? t("tableEditGeneral.saveFailed")}
        </Alert>
      )}
      {renameSuccess && (
        <Alert severity="success" onClose={() => setRenameSuccess(false)}>
          Attribute names updated. Redeploy the model for changes to take effect in connected BI tools.
        </Alert>
      )}
      <Box display="flex" justifyContent="flex-end">
        <Button
          variant="contained"
          size="small"
          onClick={() => saveMutation.mutate()}
          disabled={!dirty || saveMutation.isPending}
        >
          {saveMutation.isPending ? <CircularProgress size={16} /> : t("tableEditGeneral.saveButton")}
        </Button>
      </Box>

      <AttributeRenameDialog
        open={renameOpen}
        mode="alias-change"
        rows={renameRows}
        takenNames={stableTaken}
        applying={renameApplying}
        onApply={handleRenameApply}
        onKeep={() => setRenameOpen(false)}
        onRevert={handleRenameRevert}
      />
    </Stack>
  );
}
