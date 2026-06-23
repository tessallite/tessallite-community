import { useEffect, useMemo, useRef, useState } from "react";
import { useT } from "../../../../i18n";
import {
  Autocomplete,
  Box,
  Button,
  Chip,
  CircularProgress,
  FormControl,
  IconButton,
  InputLabel,
  ListSubheader,
  Menu,
  MenuItem,
  Paper,
  Popover,
  Select,
  Stack,
  TextField,
  Tooltip,
  Typography,
} from "@mui/material";
import FilterAltIcon from "@mui/icons-material/FilterAlt";
import CloseIcon from "@mui/icons-material/Close";
import AddIcon from "@mui/icons-material/Add";
import WarningAmberIcon from "@mui/icons-material/WarningAmber";
import { queryRouterApiClient } from "../../../../api/client";
import type { Dimension } from "../../../../api/types";
import {
  SLICER_OP_LABELS,
  slicerNeedsValues,
  type Slicer,
  type SlicerOp,
} from "../types";

function quoteIdent(name: string): string {
  return `"${name.replace(/"/g, '""')}"`;
}

type Props = {
  projectId: string;
  modelId: string;
  modelSlug: string;
  dimensions: Dimension[];
  slicers: Slicer[];
  personaId?: string | null;
  disabledReasons?: Record<string, string>;
  onChange: (next: Slicer[]) => void;
};

function chipSummary(slicer: Slicer, dim: Dimension | undefined, t: (key: string, vars?: Record<string, string>) => string): string {
  const name = dim?.display_name || dim?.name || t("slicer.unknown");
  if (slicer.op === "is_null") return `${name} ${t("slicer.isNull")}`;
  if (slicer.op === "is_not_null") return `${name} ${t("slicer.isNotNull")}`;
  if (slicer.op === "between") {
    const [a, b] = slicer.values;
    return `${name} ${a ?? t("slicer.unknown")}${t("slicer.betweenSeparator")}${b ?? t("slicer.unknown")}`;
  }
  if (slicer.values.length === 0) return `${name} ${t(SLICER_OP_LABELS[slicer.op])} ...`;
  if (slicer.op === "in" && slicer.values.length > 2) {
    return `${name}${t("slicer.inPrefix")}${slicer.values.slice(0, 2).join(", ")}${t("slicer.overflowLabel", { n: String(slicer.values.length - 2) })})`;
  }
  return `${name} ${t(SLICER_OP_LABELS[slicer.op])} ${slicer.values.join(", ")}`;
}

type EditorProps = {
  projectId: string;
  modelId: string;
  modelSlug: string;
  dim: Dimension;
  slicer: Slicer;
  personaId?: string | null;
  disabledReason?: string;
  onChange: (next: Slicer) => void;
  onClose: () => void;
  onRemove: () => void;
};

const SINGLE_VALUE_OPS: SlicerOp[] = ["eq", "ne", "gt", "gte", "lt", "lte"];

function SlicerEditor({
  projectId,
  modelId,
  modelSlug,
  dim,
  slicer,
  personaId,
  disabledReason,
  onChange,
  onClose,
  onRemove,
}: EditorProps) {
  const t = useT();
  const [search, setSearch] = useState("");
  const [debouncedSearch, setDebouncedSearch] = useState("");
  const debounceRef = useRef<ReturnType<typeof setTimeout>>();
  const [options, setOptions] = useState<string[]>([]);
  const [loading, setLoading] = useState(false);
  const [truncated, setTruncated] = useState(false);
  const [fetchError, setFetchError] = useState<string | null>(null);

  const dt = (dim.data_type ?? "").toUpperCase();
  const isDate = !!dim.is_time_dim &&
    (dt.includes("DATE") || dt.includes("TIMESTAMP") || dt.includes("DATETIME"));

  function handleSearchInput(value: string) {
    setSearch(value);
    clearTimeout(debounceRef.current);
    debounceRef.current = setTimeout(() => setDebouncedSearch(value), 300);
  }

  useEffect(() => {
    if (disabledReason) {
      setOptions([]);
      setTruncated(false);
      setFetchError(null);
      setLoading(false);
      return;
    }
    if (!slicerNeedsValues(slicer.op) || slicer.op === "between" || isDate) return;
    if (SINGLE_VALUE_OPS.includes(slicer.op) && slicer.op !== "eq") return;
    let cancelled = false;
    setLoading(true);
    setFetchError(null);

    const col = quoteIdent(dim.name);
    const limit = 51;
    let sql = `SELECT DISTINCT ${col} FROM ${quoteIdent(modelSlug)} WHERE ${col} IS NOT NULL`;
    if (debouncedSearch) {
      const escaped = debouncedSearch.replace(/'/g, "''");
      sql += ` AND CAST(${col} AS VARCHAR) LIKE '%${escaped}%'`;
    }
    sql += ` ORDER BY ${col} LIMIT ${limit}`;

    queryRouterApiClient
      .execute({
        model_id: modelId,
        raw_query: sql,
        dialect: "postgresql",
        force_route: "source",
      }, personaId)
      .then((r) => {
        if (cancelled) return;
        const strs = r.rows
          .map((row) => {
            const rec = row as Record<string, unknown>;
            const val = Object.values(rec)[0];
            return val !== null && val !== undefined ? String(val) : null;
          })
          .filter((v): v is string => v !== null);
        setOptions(strs.slice(0, 50));
        setTruncated(strs.length > 50);
      })
      .catch((err: unknown) => {
        if (cancelled) return;
        const detail = (err as { response?: { data?: { detail?: string } } })
          ?.response?.data?.detail;
        setFetchError(detail || (err instanceof Error ? err.message : t("errors.requestFailed")));
        setOptions([]);
        setTruncated(false);
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [modelId, modelSlug, dim.name, debouncedSearch, slicer.op, isDate, personaId, disabledReason]);

  const multi = slicer.op === "in";
  const needsValues = slicerNeedsValues(slicer.op);
  const isSingleValueOp = SINGLE_VALUE_OPS.includes(slicer.op);

  function setOp(op: SlicerOp) {
    const keepValues = slicerNeedsValues(op);
    const nextValues = keepValues
      ? op === "between"
        ? slicer.values.slice(0, 2)
        : op === "in"
          ? slicer.values
          : slicer.values.slice(0, 1)
      : [];
    onChange({ ...slicer, op, values: nextValues });
  }

  const inputType = isDate ? "date" : undefined;

  return (
    <Paper sx={{ p: 2, width: 360, display: "flex", flexDirection: "column", gap: 1.5 }}>
      <Stack direction="row" alignItems="center" justifyContent="space-between">
        <Typography variant="subtitle2">
          {dim.display_name || dim.name}
          {isDate && (
            <Typography component="span" variant="caption" color="text.secondary" ml={0.5}>
              {t("slicer.dateSuffix")}
            </Typography>
          )}
        </Typography>
        <IconButton size="small" onClick={onClose} aria-label={t("slicer.closeAriaLabel")}>
          <CloseIcon fontSize="small" />
        </IconButton>
      </Stack>

      <FormControl size="small">
        <InputLabel>{t("slicer.operatorLabel")}</InputLabel>
        <Select
          label={t("slicer.operatorLabel")}
          value={slicer.op}
          onChange={(e) => setOp(e.target.value as SlicerOp)}
        >
          {(Object.keys(SLICER_OP_LABELS) as SlicerOp[]).map((op) => (
            <MenuItem key={op} value={op}>
              {t(SLICER_OP_LABELS[op])}
            </MenuItem>
          ))}
        </Select>
      </FormControl>

      {needsValues && slicer.op === "between" && (
        <Stack direction="row" gap={1}>
          <TextField
            size="small"
            label={t("slicer.fromLabel")}
            type={inputType}
            InputLabelProps={isDate ? { shrink: true } : undefined}
            value={slicer.values[0] ?? ""}
            onChange={(e) =>
              onChange({
                ...slicer,
                values: [e.target.value, slicer.values[1] ?? ""],
              })
            }
          />
          <TextField
            size="small"
            label={t("slicer.toLabel")}
            type={inputType}
            InputLabelProps={isDate ? { shrink: true } : undefined}
            value={slicer.values[1] ?? ""}
            onChange={(e) =>
              onChange({
                ...slicer,
                values: [slicer.values[0] ?? "", e.target.value],
              })
            }
          />
        </Stack>
      )}

      {needsValues && slicer.op === "like" && (
        <TextField
          size="small"
          label={t("slicer.patternLabel")}
          value={slicer.values[0] ?? ""}
          onChange={(e) => onChange({ ...slicer, values: [e.target.value] })}
        />
      )}

      {needsValues && isSingleValueOp && slicer.op !== "eq" && (
        <TextField
          size="small"
          label={t("slicer.valueLabel")}
          type={inputType}
          InputLabelProps={isDate ? { shrink: true } : undefined}
          value={slicer.values[0] ?? ""}
          onChange={(e) => onChange({ ...slicer, values: [e.target.value] })}
        />
      )}

      {needsValues && (slicer.op === "eq" || slicer.op === "in") && !isDate && (
        <Autocomplete<string, boolean, false, true>
          size="small"
          multiple={multi}
          freeSolo
          loading={loading}
          options={options}
          value={(multi ? slicer.values : (slicer.values[0] ?? "")) as never}
          onChange={(_, v) => {
            if (multi) {
              onChange({ ...slicer, values: (v as string[]) ?? [] });
            } else {
              onChange({ ...slicer, values: v ? [v as string] : [] });
            }
          }}
          onInputChange={(_, v) => handleSearchInput(v)}
          renderInput={(params) => (
            <TextField
              {...params}
              label={multi ? t("slicer.valuesLabel") : t("slicer.valueLabel")}
              helperText={
                fetchError
                  ? fetchError
                  : truncated
                    ? t("slicer.first50")
                    : multi
                      ? t("slicer.typeToAdd")
                      : undefined
              }
              InputProps={{
                ...params.InputProps,
                endAdornment: (
                  <>
                    {loading && <CircularProgress size={16} />}
                    {params.InputProps.endAdornment}
                  </>
                ),
              }}
            />
          )}
        />
      )}

      {needsValues && slicer.op === "eq" && isDate && (
        <TextField
          size="small"
          label={t("slicer.dateLabel")}
          type="date"
          InputLabelProps={{ shrink: true }}
          value={slicer.values[0] ?? ""}
          onChange={(e) => onChange({ ...slicer, values: [e.target.value] })}
        />
      )}

      <Stack direction="row" justifyContent="space-between">
        <Button size="small" onClick={onRemove}>
          {t("slicer.remove")}
        </Button>
        <Button size="small" variant="contained" onClick={onClose}>
          {t("slicer.done")}
        </Button>
      </Stack>
    </Paper>
  );
}

export default function SlicerBar({
  projectId,
  modelId,
  modelSlug,
  dimensions,
  slicers,
  personaId,
  disabledReasons = {},
  onChange,
}: Props) {
  const t = useT();
  const [addAnchor, setAddAnchor] = useState<HTMLElement | null>(null);
  const [editingIdx, setEditingIdx] = useState<number | null>(null);
  const [editAnchor, setEditAnchor] = useState<HTMLElement | null>(null);

  const dimsById = useMemo(() => {
    const m = new Map<string, Dimension>();
    for (const d of dimensions) m.set(d.id, d);
    return m;
  }, [dimensions]);

  const usedIds = useMemo(() => new Set(slicers.map((s) => s.dimensionId)), [slicers]);
  const addableDims = useMemo(
    () => dimensions.filter((d) => !usedIds.has(d.id)),
    [dimensions, usedIds],
  );
  const addableGroups = useMemo(() => {
    const groups = new Map<string, { label: string; dims: Dimension[] }>();
    for (const d of addableDims) {
      const key = d.source_table_alias ?? d.source_table_id ?? "_";
      const label = d.source_table_display_name ?? d.source_table_alias ?? "";
      const entry = groups.get(key) ?? { label, dims: [] };
      entry.dims.push(d);
      groups.set(key, entry);
    }
    return Array.from(groups.values()).sort((a, b) =>
      a.label.localeCompare(b.label),
    );
  }, [addableDims]);
  const showAddGroupHeaders = addableGroups.length > 1;

  function handleAdd(dim: Dimension) {
    const anchor = addAnchor;
    setAddAnchor(null);
    const defaultOp: SlicerOp = dim.is_time_dim ? "between" : "eq";
    const next: Slicer = { dimensionId: dim.id, op: defaultOp, values: [] };
    onChange([...slicers, next]);
    setEditingIdx(slicers.length);
    setEditAnchor(anchor);
  }

  function handleUpdate(idx: number, next: Slicer) {
    const copy = slicers.slice();
    copy[idx] = next;
    onChange(copy);
  }

  function handleRemove(idx: number) {
    const copy = slicers.slice();
    copy.splice(idx, 1);
    onChange(copy);
    setEditingIdx(null);
    setEditAnchor(null);
  }

  const editingSlicer =
    editingIdx !== null && editingIdx < slicers.length ? slicers[editingIdx] : null;
  const editingDim = editingSlicer ? dimsById.get(editingSlicer.dimensionId) : null;

  return (
    <Stack direction="row" spacing={0.5} alignItems="center" sx={{ flexWrap: "wrap" }}>
      <FilterAltIcon fontSize="small" color="action" />
      {slicers.map((s, i) => {
        const disabledReason = disabledReasons[s.dimensionId];
        const chip = (
          <Chip
            key={`${s.dimensionId}-${i}`}
            size="small"
            icon={disabledReason ? <WarningAmberIcon /> : undefined}
            color={disabledReason ? "warning" : "default"}
            label={chipSummary(s, dimsById.get(s.dimensionId), t)}
            onClick={(e) => {
              setEditingIdx(i);
              setEditAnchor(e.currentTarget as HTMLElement);
            }}
            onDelete={() => handleRemove(i)}
          />
        );
        return disabledReason ? (
          <Tooltip key={`${s.dimensionId}-${i}`} title={disabledReason}>
            {chip}
          </Tooltip>
        ) : chip;
      })}
      <Box>
        <Button
          size="small"
          variant="outlined"
          startIcon={<AddIcon fontSize="small" />}
          disabled={addableDims.length === 0}
          onClick={(e) => setAddAnchor(e.currentTarget)}
        >
          {t("slicer.addFilter")}
        </Button>
        <Menu
          open={Boolean(addAnchor)}
          anchorEl={addAnchor}
          onClose={() => setAddAnchor(null)}
          PaperProps={{ sx: { maxHeight: 320 } }}
        >
          {addableDims.length === 0 && (
            <MenuItem disabled>{t("slicer.noDimsLeft")}</MenuItem>
          )}
          {addableGroups.flatMap((g) => {
            const items = g.dims.map((d) => {
              const disabledReason = disabledReasons[d.id];
              const item = (
                <MenuItem
                key={d.id}
                dense
                disabled={Boolean(disabledReason)}
                onClick={() => handleAdd(d)}
                sx={{ pl: showAddGroupHeaders ? 3 : 2 }}
              >
                {d.display_name || d.name}
                {disabledReason && (
                  <WarningAmberIcon fontSize="small" sx={{ ml: 0.5, color: "warning.main", fontSize: 14 }} />
                )}
              </MenuItem>
              );
              return disabledReason ? (
                <Tooltip key={d.id} title={disabledReason} placement="right" arrow>
                  <span>{item}</span>
                </Tooltip>
              ) : item;
            });
            if (!showAddGroupHeaders) return items;
            return [
              <ListSubheader key={`${g.label}-h`} sx={{ lineHeight: "1.6em" }}>
                {g.label}
              </ListSubheader>,
              ...items,
            ];
          })}
        </Menu>
      </Box>

      <Popover
        open={editingSlicer !== null && editAnchor !== null}
        anchorEl={editAnchor}
        onClose={() => {
          setEditingIdx(null);
          setEditAnchor(null);
        }}
        anchorOrigin={{ vertical: "bottom", horizontal: "left" }}
      >
        {editingSlicer && editingDim && editingIdx !== null && (
          <SlicerEditor
            projectId={projectId}
            modelId={modelId}
            modelSlug={modelSlug}
            dim={editingDim}
            slicer={editingSlicer}
            personaId={personaId}
            disabledReason={disabledReasons[editingDim.id]}
            onChange={(next) => handleUpdate(editingIdx, next)}
            onClose={() => {
              setEditingIdx(null);
              setEditAnchor(null);
            }}
            onRemove={() => handleRemove(editingIdx)}
          />
        )}
      </Popover>
    </Stack>
  );
}
