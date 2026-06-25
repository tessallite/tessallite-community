import { useEffect, useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Alert,
  Autocomplete,
  Box,
  Button,
  CircularProgress,
  FormControl,
  InputLabel,
  MenuItem,
  Select,
  Stack,
  TextField,
  Typography,
} from "@mui/material";
import { measuresApi } from "../../api/client";
import { useDimensions, useTableAttributes } from "../../api/hooks";
import type {
  DrillJoinPath,
  DrillThroughSet,
  DrillThroughSetUpdate,
  Measure,
  ModelTable,
} from "../../api/types";
import { useConfirm } from "../Confirm";
import { useT } from "../../i18n";
import { ui } from "../../theme/tokens";

type Props = {
  projectId: string;
  modelId: string;
  measure: Measure;
  tables: ModelTable[];
};

type ApiErrorBody = {
  code?: string;
  message?: string;
  invalid_ids?: string[];
};

export function DrillThroughSetEditor({
  projectId,
  modelId,
  measure,
  tables,
}: Props) {
  const qc = useQueryClient();
  const confirm = useConfirm();
  const t = useT();

  function extractError(err: unknown): string {
    const anyErr = err as { response?: { data?: { detail?: ApiErrorBody | string } } };
    const detail = anyErr?.response?.data?.detail;
    if (typeof detail === "string") return detail;
    if (detail && typeof detail === "object") {
      const code = detail.code ? `[${detail.code}] ` : "";
      return `${code}${detail.message ?? t("drillThrough.validationFailed")}`;
    }
    return t("drillThrough.saveFailed");
  }

  const drillQuery = useQuery({
    queryKey: ["drillThroughSet", projectId, modelId, measure.id],
    queryFn: () =>
      measuresApi.getDrillThroughSet(projectId, modelId, measure.id),
    enabled: measure.measure_type !== "calculated",
  });

  const [sourceTableId, setSourceTableId] = useState<string | null>(null);
  const [detailColumns, setDetailColumns] = useState<string[]>([]);
  const [joinedDimensionIds, setJoinedDimensionIds] = useState<string[]>([]);
  const [rowLimitOverride, setRowLimitOverride] = useState<string>("");
  const [sourceJoinPath, setSourceJoinPath] = useState<string[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!drillQuery.data) return;
    setSourceTableId(drillQuery.data.source_table_id);
    setDetailColumns(drillQuery.data.detail_columns ?? []);
    setJoinedDimensionIds(drillQuery.data.joined_dimension_ids ?? []);
    setRowLimitOverride(
      drillQuery.data.row_limit_override == null
        ? ""
        : String(drillQuery.data.row_limit_override),
    );
    setSourceJoinPath(drillQuery.data.source_join_path ?? null);
    setError(null);
  }, [drillQuery.data]);

  const effectiveTableId = useMemo(() => {
    if (sourceTableId) return sourceTableId;
    return measure.source_table_id ?? null;
  }, [sourceTableId, measure.source_table_id]);

  const tableAttrs = useTableAttributes(
    projectId,
    modelId,
    effectiveTableId ?? "",
  );
  const dimsQuery = useDimensions(projectId, modelId);

  const overrideRequiresPath =
    sourceTableId != null && sourceTableId !== measure.source_table_id;
  const joinPathsQuery = useQuery({
    queryKey: [
      "drillJoinPaths",
      projectId,
      modelId,
      measure.id,
      sourceTableId ?? "",
    ],
    queryFn: () =>
      measuresApi.listDrillJoinPaths(
        projectId,
        modelId,
        measure.id,
        sourceTableId!,
      ),
    enabled: overrideRequiresPath,
  });

  const tableLabel = (id: string): string =>
    tables.find((t) => t.id === id)?.physical_name ?? id;

  const formatPath = (path: DrillJoinPath): string => {
    if (path.hops.length === 0) return t("drillThrough.noHops");
    const labels = [tableLabel(path.hops[0].left_table_id)];
    let cursor = path.hops[0].left_table_id;
    for (const hop of path.hops) {
      const next =
        hop.left_table_id === cursor ? hop.right_table_id : hop.left_table_id;
      labels.push(tableLabel(next));
      cursor = next;
    }
    return `${labels.join(" → ")}  [${path.cardinality_hint}]`;
  };

  const pathOptions = joinPathsQuery.data?.paths ?? [];
  const selectedPathIndex = useMemo(() => {
    if (!sourceJoinPath || pathOptions.length === 0) return -1;
    return pathOptions.findIndex(
      (p) =>
        p.hops.length === sourceJoinPath.length &&
        p.hops.every((h, i) => h.join_id === sourceJoinPath[i]),
    );
  }, [sourceJoinPath, pathOptions]);

  const physicalColumns = useMemo(
    () => (tableAttrs.data ?? []).filter((a) => a.kind === "physical"),
    [tableAttrs.data],
  );

  const updateMut = useMutation({
    mutationFn: (data: DrillThroughSetUpdate) =>
      measuresApi.updateDrillThroughSet(projectId, modelId, measure.id, data),
    onSuccess: (data) => {
      qc.setQueryData(
        ["drillThroughSet", projectId, modelId, measure.id],
        data,
      );
      setError(null);
    },
    onError: (err) => setError(extractError(err)),
  });

  const resetMut = useMutation({
    mutationFn: () =>
      measuresApi.resetDrillThroughSet(projectId, modelId, measure.id),
    onSuccess: (data: DrillThroughSet) => {
      qc.setQueryData(
        ["drillThroughSet", projectId, modelId, measure.id],
        data,
      );
      setError(null);
    },
    onError: (err) => setError(extractError(err)),
  });

  if (measure.measure_type === "calculated") {
    return (
      <Alert severity="info">
        {t("drillThrough.formulaMeasuresInfo")}
      </Alert>
    );
  }

  if (drillQuery.isLoading) {
    return (
      <Box sx={{ display: "flex", justifyContent: "center", py: 2 }}>
        <CircularProgress size={24} />
      </Box>
    );
  }

  if (drillQuery.isError) {
    return (
      <Alert severity="error">
        {t("drillThrough.loadFailed", { error: extractError(drillQuery.error) })}
      </Alert>
    );
  }

  const onSave = () => {
    let limit: number | null = null;
    if (rowLimitOverride.trim() !== "") {
      const parsed = Number(rowLimitOverride);
      if (!Number.isFinite(parsed) || parsed <= 0 || !Number.isInteger(parsed)) {
        setError(t("drillThrough.rowLimitError"));
        return;
      }
      limit = parsed;
    }
    if (overrideRequiresPath && pathOptions.length > 1 && !sourceJoinPath) {
      setError(t("drillThrough.multipleJoinPaths"));
      return;
    }
    updateMut.mutate({
      source_table_id: sourceTableId,
      detail_columns: detailColumns.length > 0 ? detailColumns : null,
      joined_dimension_ids:
        joinedDimensionIds.length > 0 ? joinedDimensionIds : null,
      row_limit_override: limit,
      source_join_path:
        overrideRequiresPath && sourceJoinPath ? sourceJoinPath : null,
    });
  };

  const onReset = async () => {
    const ok = await confirm({
      title: t("drillThrough.resetTitle"),
      message: t("drillThrough.resetMessage"),
      confirmLabel: t("drillThrough.reset"),
      destructive: false,
    });
    if (ok) resetMut.mutate();
  };

  return (
    <Stack spacing={2} sx={{ p: 2, bgcolor: ui.mutedBg }}>
      <Typography variant="subtitle2">{t("drillThrough.configuration")}</Typography>

      {error && <Alert severity="error">{error}</Alert>}

      <FormControl size="small" fullWidth>
        <InputLabel id={`dt-source-${measure.id}`}>{t("drillThrough.sourceTableOverride")}</InputLabel>
        <Select
          labelId={`dt-source-${measure.id}`}
          label={t("drillThrough.sourceTableOverride")}
          value={sourceTableId ?? ""}
          onChange={(e) => {
            const v = e.target.value === "" ? null : (e.target.value as string);
            setSourceTableId(v);
            // Reset path whenever the override changes — old hops won't apply.
            setSourceJoinPath(null);
          }}
        >
          <MenuItem value="">
            <em>{t("drillThrough.useImplicitFact")}</em>
          </MenuItem>
          {tables.map((t) => (
            <MenuItem key={t.id} value={t.id}>
              {t.physical_name}
            </MenuItem>
          ))}
        </Select>
      </FormControl>

      {overrideRequiresPath && (
        <FormControl size="small" fullWidth>
          <InputLabel id={`dt-path-${measure.id}`}>{t("drillThrough.joinPathToFact")}</InputLabel>
          <Select
            labelId={`dt-path-${measure.id}`}
            label={t("drillThrough.joinPathToFact")}
            value={selectedPathIndex >= 0 ? String(selectedPathIndex) : ""}
            disabled={joinPathsQuery.isLoading || pathOptions.length === 0}
            onChange={(e) => {
              const idx = e.target.value === "" ? -1 : Number(e.target.value);
              if (idx < 0 || idx >= pathOptions.length) {
                setSourceJoinPath(null);
                return;
              }
              setSourceJoinPath(
                pathOptions[idx].hops.map((h) => h.join_id),
              );
            }}
          >
            {pathOptions.length === 0 && !joinPathsQuery.isLoading && (
              <MenuItem value="" disabled>
                <em>{t("drillThrough.noJoinPath")}</em>
              </MenuItem>
            )}
            {pathOptions.map((p, idx) => (
              <MenuItem key={idx} value={String(idx)}>
                {formatPath(p)}
              </MenuItem>
            ))}
          </Select>
        </FormControl>
      )}

      <Autocomplete
        multiple
        size="small"
        options={physicalColumns}
        getOptionLabel={(o) => o.name}
        value={physicalColumns.filter((c) => detailColumns.includes(c.id))}
        onChange={(_, newValue) =>
          setDetailColumns(newValue.map((v) => v.id))
        }
        loading={tableAttrs.isLoading}
        renderInput={(params) => (
          <TextField
            {...params}
            label={t("drillThrough.detailColumns")}
            placeholder={t("drillThrough.selectColumns")}
          />
        )}
      />

      <Autocomplete
        multiple
        size="small"
        options={dimsQuery.data ?? []}
        getOptionLabel={(o) => o.display_name || o.name}
        value={
          (dimsQuery.data ?? []).filter((d) =>
            joinedDimensionIds.includes(d.id),
          )
        }
        onChange={(_, newValue) =>
          setJoinedDimensionIds(newValue.map((v) => v.id))
        }
        loading={dimsQuery.isLoading}
        renderInput={(params) => (
          <TextField
            {...params}
            label={t("drillThrough.joinedDimensions")}
            placeholder={t("drillThrough.addDimensions")}
          />
        )}
      />

      <TextField
        size="small"
        label={t("drillThrough.rowLimitOverride")}
        type="number"
        value={rowLimitOverride}
        onChange={(e) => setRowLimitOverride(e.target.value)}
        helperText={t("drillThrough.rowLimitHelp")}
        inputProps={{ min: 1, step: 1 }}
      />

      <Stack direction="row" spacing={1} justifyContent="flex-end">
        <Button
          variant="outlined"
          color="warning"
          onClick={onReset}
          disabled={resetMut.isPending || updateMut.isPending}
        >
          {t("drillThrough.resetToDefaults")}
        </Button>
        <Button
          variant="contained"
          onClick={onSave}
          disabled={updateMut.isPending || resetMut.isPending}
        >
          {updateMut.isPending ? t("common.saving") : t("common.save")}
        </Button>
      </Stack>
    </Stack>
  );
}

export default DrillThroughSetEditor;
