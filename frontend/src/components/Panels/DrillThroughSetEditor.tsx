import { useEffect, useMemo, useRef, useState } from "react";
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
import { recordDelete, recordUpdate } from "../Builder/emitDrawerHistory";
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

function drillThroughSetPayload(
  set: DrillThroughSet,
  measureId: string,
): Record<string, unknown> {
  return {
    source_table_id: set.source_table_id,
    detail_columns: set.detail_columns ?? null,
    joined_dimension_ids: set.joined_dimension_ids ?? null,
    row_limit_override: set.row_limit_override ?? null,
    source_join_path: set.source_join_path ?? null,
    __measure_id: measureId,
  };
}

// Bug-5935 (F-019-04): mirrors DRILL_MAX_ROW_LIMIT in
// tessallite/shared/drill_limits.py, the single source of truth the shared
// schema and query-router's runtime clamp both read. Kept in sync manually
// because this is a TypeScript file and cannot import the Python constant;
// it exists purely to fail the save client-side before the round trip.
const ROW_LIMIT_OVERRIDE_MAX = 10000;

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
  const [pruneNotice, setPruneNotice] = useState<string | null>(null);

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
    setPruneNotice(null);
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

  // Bug-5933 (F-019-02): query-router's drill builder can only project
  // model DIMENSION names in its semantic SQL — a physical column with no
  // dimension defined over it raises DRILL_DETAIL_COLUMN_NOT_PROJECTABLE at
  // drill time even though it looks like a normal column here. Restrict the
  // picker to physical columns that have a dimension over them on the
  // effective table, so the editor cannot offer a selection the runtime
  // (and now model-service, see measures.py _validate_detail_columns) will
  // reject.
  const projectableColumnIds = useMemo(() => {
    const ids = new Set<string>();
    for (const d of dimsQuery.data ?? []) {
      if (d.source_table_id === effectiveTableId && d.source_column_id) {
        ids.add(d.source_column_id);
      }
    }
    return ids;
  }, [dimsQuery.data, effectiveTableId]);

  const physicalColumns = useMemo(
    () =>
      (tableAttrs.data ?? []).filter(
        (a) => a.kind === "physical" && projectableColumnIds.has(a.id),
      ),
    [tableAttrs.data, projectableColumnIds],
  );

  // Bug-5933 (F-019-02) follow-up, found in review: a set saved before this
  // fix (or whose dimension was later deleted) can carry a detail_columns id
  // that is no longer projectable. Left in local state, that id is invisible
  // in the Autocomplete (it is filtered out of `physicalColumns`) but still
  // gets resubmitted on the next Save, surfacing a confusing
  // DRILL_DETAIL_COLUMN_NOT_PROJECTABLE error listing a raw UUID the user
  // never selected and cannot see in the UI. Prune stale ids once the table's
  // columns and the model's dimensions have both loaded, and tell the user
  // why, instead of silently carrying an invisible value forward.
  const prunedForMeasureRef = useRef<string | null>(null);
  useEffect(() => {
    if (!drillQuery.data) return;
    if (tableAttrs.data === undefined || dimsQuery.data === undefined) return;
    if (prunedForMeasureRef.current === measure.id) return;
    // Guard against a race between the two data sources `physicalColumns`
    // depends on: `tableAttrs`/`dimsQuery` key off `effectiveTableId`, which
    // itself derives from `sourceTableId` state — populated by a SEPARATE
    // effect only after `drillQuery.data` arrives. If `tableAttrs` for the
    // measure's INTRINSIC table happens to resolve before that population
    // effect commits, this effect can fire in the same commit where
    // `drillQuery.data` first shows an override `source_table_id`, comparing
    // the override's detail columns against the intrinsic table's columns —
    // wrongly pruning valid ones. Only prune once `effectiveTableId` agrees
    // with the loaded set's actual table, so `physicalColumns` is guaranteed
    // to be computed for the same table the loaded `detailColumns` belong to.
    const loadedTableId = drillQuery.data.source_table_id ?? measure.source_table_id ?? null;
    if (loadedTableId !== effectiveTableId) return;
    prunedForMeasureRef.current = measure.id;
    // Read the SERVER-loaded detail_columns (drillQuery.data), not the local
    // `detailColumns` state — found in review: on the commit where
    // drillQuery.data first arrives, the separate populate effect (above)
    // has only QUEUED setDetailColumns(...); this effect's closure would
    // still see the pre-hydration `detailColumns` (`[]` on first load), so
    // computing `stale` from local state made this a permanent no-op on the
    // common (no-override, warm-cache) path — the exact case it exists to
    // catch. `drillQuery.data.detail_columns` is available synchronously in
    // the same render, so it is not subject to the same one-render lag.
    const validIds = new Set(physicalColumns.map((c) => c.id));
    const loadedDetailColumns = drillQuery.data.detail_columns ?? [];
    const stale = loadedDetailColumns.filter((id) => !validIds.has(id));
    if (stale.length === 0) return;
    setDetailColumns((prev) => prev.filter((id) => validIds.has(id)));
    setPruneNotice(
      t("drillThrough.staleDetailColumnsPruned", { count: stale.length }),
    );
  }, [
    drillQuery.data,
    tableAttrs.data,
    dimsQuery.data,
    physicalColumns,
    effectiveTableId,
    measure.id,
    measure.source_table_id,
    t,
  ]);

  const updateMut = useMutation({
    mutationFn: (vars: { data: DrillThroughSetUpdate; prior: Record<string, unknown> }) =>
      measuresApi.updateDrillThroughSet(projectId, modelId, measure.id, vars.data),
    onSuccess: (data, variables) => {
      recordUpdate(
        "drillThroughSet",
        measure.id,
        variables.prior,
        { ...variables.data, __measure_id: measure.id },
      );
      qc.setQueryData(
        ["drillThroughSet", projectId, modelId, measure.id],
        data,
      );
      setError(null);
    },
    onError: (err) => setError(extractError(err)),
  });

  const resetMut = useMutation({
    mutationFn: (prior: Record<string, unknown>) =>
      measuresApi.resetDrillThroughSet(projectId, modelId, measure.id),
    onSuccess: (data: DrillThroughSet, prior) => {
      recordDelete("drillThroughSet", measure.id, prior);
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
      // Bug-5935 (F-019-04): the runtime clamps every drill page to
      // DRILL_MAX_ROW_LIMIT (tessallite/shared/drill_limits.py, currently
      // 10,000) and the shared schema now rejects a save above that ceiling
      // (DrillThroughSetUpdate.row_limit_override Field(le=...)). Reject the
      // same range here so the user sees the problem before saving instead
      // of after a silent runtime clamp.
      if (
        !Number.isFinite(parsed) ||
        parsed <= 0 ||
        !Number.isInteger(parsed) ||
        parsed > ROW_LIMIT_OVERRIDE_MAX
      ) {
        setError(t("drillThrough.rowLimitError", { max: ROW_LIMIT_OVERRIDE_MAX }));
        return;
      }
      limit = parsed;
    }
    if (overrideRequiresPath && pathOptions.length > 1 && !sourceJoinPath) {
      setError(t("drillThrough.multipleJoinPaths"));
      return;
    }
    const data: DrillThroughSetUpdate = {
      source_table_id: sourceTableId,
      detail_columns: detailColumns.length > 0 ? detailColumns : null,
      joined_dimension_ids:
        joinedDimensionIds.length > 0 ? joinedDimensionIds : null,
      row_limit_override: limit,
      source_join_path:
        overrideRequiresPath && sourceJoinPath ? sourceJoinPath : null,
    };
    updateMut.mutate({
      data,
      prior: drillQuery.data
        ? drillThroughSetPayload(drillQuery.data, measure.id)
        : { __measure_id: measure.id },
    });
  };

  const onReset = async () => {
    const ok = await confirm({
      title: t("drillThrough.resetTitle"),
      message: t("drillThrough.resetMessage"),
      confirmLabel: t("drillThrough.reset"),
      destructive: false,
    });
    if (ok) {
      resetMut.mutate(
        drillQuery.data
          ? drillThroughSetPayload(drillQuery.data, measure.id)
          : { __measure_id: measure.id },
      );
    }
  };

  return (
    <Stack spacing={2} sx={{ p: 2, bgcolor: ui.mutedBg }}>
      <Typography variant="subtitle2">{t("drillThrough.configuration")}</Typography>

      {error && <Alert severity="error">{error}</Alert>}
      {!error && pruneNotice && (
        <Alert severity="warning" onClose={() => setPruneNotice(null)}>
          {pruneNotice}
        </Alert>
      )}

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
        helperText={t("drillThrough.rowLimitHelp", { max: ROW_LIMIT_OVERRIDE_MAX })}
        inputProps={{ min: 1, max: ROW_LIMIT_OVERRIDE_MAX, step: 1 }}
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
