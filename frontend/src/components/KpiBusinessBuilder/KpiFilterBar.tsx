import { useCallback, useEffect, useMemo, useRef, useState } from "react";
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
  Typography,
} from "@mui/material";
import FilterAltIcon from "@mui/icons-material/FilterAlt";
import CloseIcon from "@mui/icons-material/Close";
import AddIcon from "@mui/icons-material/Add";

import { useT } from "../../i18n";
import { queryRouterApiClient } from "../../api/client";
import type { Dimension } from "../../api/types";
import type { BusinessFilter, BusinessFilterOp } from "../../api/types_domains/kpis";
import { TIME_WINDOW_PRESETS } from "./businessDefinition";

function quoteIdent(name: string): string {
  return `"${name.replace(/"/g, '""')}"`;
}

const OP_LABELS: Record<BusinessFilterOp, string> = {
  eq: "kpiBusiness.opEq",
  ne: "kpiBusiness.opNe",
  gt: "kpiBusiness.opGt",
  gte: "kpiBusiness.opGte",
  lt: "kpiBusiness.opLt",
  lte: "kpiBusiness.opLte",
  in: "kpiBusiness.opIn",
  not_in: "kpiBusiness.opNotIn",
  between: "kpiBusiness.opBetween",
  like: "kpiBusiness.opLike",
  not_like: "kpiBusiness.opNotLike",
  is_null: "kpiBusiness.opIsNull",
  is_not_null: "kpiBusiness.opIsNotNull",
  top_n: "kpiBusiness.opTopN",
  bottom_n: "kpiBusiness.opBottomN",
};

const BUILDER_OPS: BusinessFilterOp[] = [
  "eq", "ne", "gt", "gte", "lt", "lte",
  "in", "not_in", "between",
  "like", "not_like",
  "is_null", "is_not_null",
];

const SINGLE_VALUE_OPS: BusinessFilterOp[] = ["eq", "ne", "gt", "gte", "lt", "lte"];
const NO_VALUE_OPS: BusinessFilterOp[] = ["is_null", "is_not_null"];
const N_OPS: BusinessFilterOp[] = ["top_n", "bottom_n"];

function needsValues(op: BusinessFilterOp): boolean {
  return !NO_VALUE_OPS.includes(op);
}

function chipSummary(
  filter: BusinessFilter,
  dim: Dimension | undefined,
  t: (key: string, vars?: Record<string, string>) => string,
): string {
  const name = dim?.display_name || dim?.name || filter.dimension_id;
  if (NO_VALUE_OPS.includes(filter.operator)) {
    return `${name} ${t(OP_LABELS[filter.operator])}`;
  }
  if (filter.operator === "between") {
    const vals = filter.values ?? [];
    return `${name} ${vals[0] ?? "?"}–${vals[1] ?? "?"}`;
  }
  if (N_OPS.includes(filter.operator)) {
    return `${name} ${t(OP_LABELS[filter.operator])} ${filter.n ?? "?"}`;
  }
  const vals = filter.values ?? (filter.value ? [filter.value] : []);
  if (vals.length === 0) return `${name} ${t(OP_LABELS[filter.operator])} ...`;
  if (filter.operator === "in" && vals.length > 2) {
    return `${name} in (${vals.slice(0, 2).join(", ")} +${vals.length - 2})`;
  }
  return `${name} ${t(OP_LABELS[filter.operator])} ${vals.join(", ")}`;
}

type EditorProps = {
  modelId: string;
  dim: Dimension;
  filter: BusinessFilter;
  onChange: (next: BusinessFilter) => void;
  onClose: () => void;
  onRemove: () => void;
};

function FilterEditor({ modelId, dim, filter, onChange, onClose, onRemove }: EditorProps) {
  const t = useT();
  const [search, setSearch] = useState("");
  const [debouncedSearch, setDebouncedSearch] = useState("");
  const debounceRef = useRef<ReturnType<typeof setTimeout>>();
  const [options, setOptions] = useState<string[]>([]);
  const [loading, setLoading] = useState(false);
  const [truncated, setTruncated] = useState(false);

  const dt = (dim.data_type ?? "").toUpperCase();
  const isDate =
    !!dim.is_time_dim &&
    (dt.includes("DATE") || dt.includes("TIMESTAMP") || dt.includes("DATETIME"));

  // Clear the debounce timer on unmount to prevent setState after unmount
  // (Bug-5510: leaked timer caused test flakes under parallel load).
  useEffect(() => () => clearTimeout(debounceRef.current), []);

  const handleSearchInput = useCallback((value: string) => {
    setSearch(value);
    clearTimeout(debounceRef.current);
    debounceRef.current = setTimeout(() => setDebouncedSearch(value), 300);
  }, []);

  const shouldFetchValues =
    needsValues(filter.operator) &&
    !["between", "like", "not_like"].includes(filter.operator) &&
    !N_OPS.includes(filter.operator) &&
    !(SINGLE_VALUE_OPS.includes(filter.operator) && filter.operator !== "eq") &&
    !isDate;

  useEffect(() => {
    if (!shouldFetchValues || !dim.source_table_alias) return;
    let cancelled = false;
    setLoading(true);

    const col = quoteIdent(dim.name);
    const tbl = quoteIdent(dim.source_table_alias);
    const limit = 51;
    let sql = `SELECT DISTINCT ${col} FROM ${tbl} WHERE ${col} IS NOT NULL`;
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
      })
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
      .catch(() => {
        if (!cancelled) {
          setOptions([]);
          setTruncated(false);
        }
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [modelId, dim.name, dim.source_table_alias, debouncedSearch, shouldFetchValues]);

  const vals = filter.values ?? (filter.value ? [filter.value] : []);
  const multi = filter.operator === "in" || filter.operator === "not_in";
  const inputType = isDate ? "date" : undefined;

  function setOp(op: BusinessFilterOp) {
    const keepValues = needsValues(op);
    const nextValues = keepValues
      ? op === "between"
        ? vals.slice(0, 2)
        : multi
          ? vals
          : vals.slice(0, 1)
      : [];
    onChange({ ...filter, operator: op, values: nextValues, value: undefined, n: undefined });
  }

  function setValues(v: string[]) {
    onChange({ ...filter, values: v });
  }

  return (
    <Paper sx={{ p: 2, width: 360, display: "flex", flexDirection: "column", gap: 1.5 }}>
      <Stack direction="row" alignItems="center" justifyContent="space-between">
        <Typography variant="subtitle2">{dim.display_name || dim.name}</Typography>
        <IconButton size="small" onClick={onClose}>
          <CloseIcon fontSize="small" />
        </IconButton>
      </Stack>

      <FormControl size="small">
        <InputLabel>{t("kpiBusiness.operator")}</InputLabel>
        <Select
          label={t("kpiBusiness.operator")}
          value={filter.operator}
          onChange={(e) => setOp(e.target.value as BusinessFilterOp)}
        >
          {BUILDER_OPS.map((op) => (
            <MenuItem key={op} value={op}>
              {t(OP_LABELS[op])}
            </MenuItem>
          ))}
        </Select>
      </FormControl>

      {needsValues(filter.operator) && filter.operator === "between" && (
        <Stack direction="row" gap={1}>
          <TextField
            size="small"
            label={t("kpiBusiness.fromValue")}
            type={inputType}
            InputLabelProps={isDate ? { shrink: true } : undefined}
            value={vals[0] ?? ""}
            onChange={(e) => setValues([e.target.value, vals[1] ?? ""])}
          />
          <TextField
            size="small"
            label={t("kpiBusiness.toValue")}
            type={inputType}
            InputLabelProps={isDate ? { shrink: true } : undefined}
            value={vals[1] ?? ""}
            onChange={(e) => setValues([vals[0] ?? "", e.target.value])}
          />
        </Stack>
      )}

      {needsValues(filter.operator) &&
        (filter.operator === "like" || filter.operator === "not_like") && (
          <TextField
            size="small"
            label={t("kpiBusiness.pattern")}
            value={vals[0] ?? ""}
            onChange={(e) => setValues([e.target.value])}
          />
        )}

      {needsValues(filter.operator) &&
        SINGLE_VALUE_OPS.includes(filter.operator) &&
        filter.operator !== "eq" && (
          <TextField
            size="small"
            label={t("kpiBusiness.value")}
            type={inputType}
            InputLabelProps={isDate ? { shrink: true } : undefined}
            value={vals[0] ?? ""}
            onChange={(e) => setValues([e.target.value])}
          />
        )}

      {shouldFetchValues && (
        <Autocomplete<string, boolean, false, true>
          size="small"
          multiple={multi}
          freeSolo
          loading={loading}
          options={options}
          value={(multi ? vals : (vals[0] ?? "")) as never}
          onChange={(_, v) => {
            if (multi) {
              setValues((v as string[]) ?? []);
            } else {
              setValues(v ? [v as string] : []);
            }
          }}
          onInputChange={(_, v) => handleSearchInput(v)}
          renderInput={(params) => (
            <TextField
              {...params}
              label={multi ? t("kpiBusiness.values") : t("kpiBusiness.value")}
              helperText={truncated ? t("kpiBusiness.first50") : undefined}
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

      {needsValues(filter.operator) &&
        filter.operator === "eq" &&
        isDate && (
          <TextField
            size="small"
            label={t("kpiBusiness.value")}
            type="date"
            InputLabelProps={{ shrink: true }}
            value={vals[0] ?? ""}
            onChange={(e) => setValues([e.target.value])}
          />
        )}

      {N_OPS.includes(filter.operator) && (
        <TextField
          size="small"
          type="number"
          label={t("kpiBusiness.nValue")}
          value={filter.n ?? ""}
          onChange={(e) =>
            onChange({
              ...filter,
              n: e.target.value ? Number(e.target.value) : undefined,
            })
          }
          inputProps={{ min: 1 }}
        />
      )}

      {/* Filter mode toggle */}
      <FormControl size="small">
        <InputLabel>{t("kpiBusiness.filterMode")}</InputLabel>
        <Select
          label={t("kpiBusiness.filterMode")}
          value={filter.mode ?? "fixed"}
          onChange={(e) => {
            const mode = e.target.value as BusinessFilter["mode"];
            onChange({ ...filter, mode, values: mode === "relative" ? [] : filter.values });
          }}
        >
          <MenuItem value="fixed">{t("kpiBusiness.filterModeFixed")}</MenuItem>
          {isDate && (
            <MenuItem value="relative">{t("kpiBusiness.filterModeRelative")}</MenuItem>
          )}
          <MenuItem value="parameter">{t("kpiBusiness.parameterMode")}</MenuItem>
        </Select>
      </FormControl>

      {filter.mode === "relative" && (
        <FormControl size="small">
          <InputLabel>{t("kpiBusiness.relativePreset")}</InputLabel>
          <Select
            label={t("kpiBusiness.relativePreset")}
            value={(filter.values ?? [])[0] ?? ""}
            onChange={(e) => onChange({ ...filter, values: [e.target.value] })}
          >
            {TIME_WINDOW_PRESETS.filter((p) => p.preset !== "custom_range").map((p) => (
              <MenuItem key={p.preset} value={p.preset}>
                {t(p.labelKey)}
              </MenuItem>
            ))}
          </Select>
        </FormControl>
      )}

      {filter.mode === "parameter" && (
        <Stack spacing={1}>
          <TextField
            size="small"
            label={t("kpiBusiness.parameterName")}
            value={filter.parameter_name ?? ""}
            onChange={(e) => onChange({ ...filter, parameter_name: e.target.value || undefined })}
          />
          <TextField
            size="small"
            label={t("kpiBusiness.parameterDefault")}
            value={
              Array.isArray(filter.default_value)
                ? filter.default_value.join(", ")
                : (filter.default_value ?? "")
            }
            onChange={(e) => onChange({ ...filter, default_value: e.target.value || undefined })}
            helperText={multi ? t("kpiBusiness.parameterDefaultMultiHint") : undefined}
          />
        </Stack>
      )}

      <Stack direction="row" justifyContent="space-between">
        <Button size="small" onClick={onRemove}>
          {t("kpiBusiness.removeFilter")}
        </Button>
        <Button size="small" variant="contained" onClick={onClose}>
          {t("kpiBusiness.done")}
        </Button>
      </Stack>
    </Paper>
  );
}

type Props = {
  filters: BusinessFilter[];
  onChange: (filters: BusinessFilter[]) => void;
  dimensions: Dimension[];
  projectId: string;
  modelId: string;
};

export function KpiFilterBar({ filters, onChange, dimensions, modelId }: Props) {
  const t = useT();
  const [addAnchor, setAddAnchor] = useState<HTMLElement | null>(null);
  const [editingIdx, setEditingIdx] = useState<number | null>(null);
  const [editAnchor, setEditAnchor] = useState<HTMLElement | null>(null);

  const dimsById = useMemo(() => {
    const m = new Map<string, Dimension>();
    for (const d of dimensions) m.set(d.id, d);
    return m;
  }, [dimensions]);

  const usedIds = useMemo(() => new Set(filters.map((f) => f.dimension_id)), [filters]);
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
    return Array.from(groups.values()).sort((a, b) => a.label.localeCompare(b.label));
  }, [addableDims]);
  const showGroupHeaders = addableGroups.length > 1;

  function handleAdd(dim: Dimension) {
    const anchor = addAnchor;
    setAddAnchor(null);
    const defaultOp: BusinessFilterOp = dim.is_time_dim ? "between" : "eq";
    const next: BusinessFilter = {
      dimension_id: dim.id,
      operator: defaultOp,
      values: [],
      label: dim.display_name || dim.name,
    };
    onChange([...filters, next]);
    setEditingIdx(filters.length);
    setEditAnchor(anchor);
  }

  function handleUpdate(idx: number, next: BusinessFilter) {
    const copy = filters.slice();
    copy[idx] = next;
    onChange(copy);
  }

  function handleRemove(idx: number) {
    onChange(filters.filter((_, i) => i !== idx));
    setEditingIdx(null);
    setEditAnchor(null);
  }

  const editingFilter =
    editingIdx !== null && editingIdx < filters.length ? filters[editingIdx] : null;
  const editingDim = editingFilter ? dimsById.get(editingFilter.dimension_id) : null;

  return (
    <Stack spacing={1}>
      <Typography variant="caption" color="text.secondary" display="block">
        {t("kpiBusiness.filtersSection")}
      </Typography>
      <Stack direction="row" spacing={0.5} alignItems="center" sx={{ flexWrap: "wrap" }}>
        <FilterAltIcon fontSize="small" color="action" />
        {filters.map((f, i) => (
          <Chip
            key={`${f.dimension_id}-${i}`}
            size="small"
            label={chipSummary(f, dimsById.get(f.dimension_id), t)}
            onClick={(e) => {
              setEditingIdx(i);
              setEditAnchor(e.currentTarget as HTMLElement);
            }}
            onDelete={() => handleRemove(i)}
          />
        ))}
        <Box>
          <Button
            size="small"
            variant="outlined"
            startIcon={<AddIcon fontSize="small" />}
            disabled={addableDims.length === 0}
            onClick={(e) => setAddAnchor(e.currentTarget)}
          >
            {t("kpiBusiness.addFilter")}
          </Button>
          <Menu
            open={Boolean(addAnchor)}
            anchorEl={addAnchor}
            onClose={() => setAddAnchor(null)}
            PaperProps={{ sx: { maxHeight: 320 } }}
          >
            {addableDims.length === 0 && (
              <MenuItem disabled>{t("kpiBusiness.noDimensionsLeft")}</MenuItem>
            )}
            {addableGroups.flatMap((g) => {
              const items = g.dims.map((d) => (
                <MenuItem
                  key={d.id}
                  dense
                  onClick={() => handleAdd(d)}
                  sx={{ pl: showGroupHeaders ? 3 : 2 }}
                >
                  {d.display_name || d.name}
                </MenuItem>
              ));
              if (!showGroupHeaders) return items;
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
          open={editingFilter !== null && editAnchor !== null}
          anchorEl={editAnchor}
          onClose={() => {
            setEditingIdx(null);
            setEditAnchor(null);
          }}
          anchorOrigin={{ vertical: "bottom", horizontal: "left" }}
        >
          {editingFilter && editingDim && editingIdx !== null && (
            <FilterEditor
              modelId={modelId}
              dim={editingDim}
              filter={editingFilter}
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
    </Stack>
  );
}
