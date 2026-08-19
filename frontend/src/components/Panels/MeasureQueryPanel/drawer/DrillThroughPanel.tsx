import { useEffect, useMemo, useRef, useState } from "react";
import { useT } from "../../../../i18n";
import {
  Box,
  Breadcrumbs,
  Button,
  Checkbox,
  CircularProgress,
  Divider,
  FormControl,
  IconButton,
  InputLabel,
  ListItemText,
  Menu,
  MenuItem,
  Paper,
  Select,
  Stack,
  Table,
  TableBody,
  TableCell,
  TableContainer,
  TableHead,
  TableRow,
  Tooltip,
  Typography,
} from "@mui/material";
import CloseIcon from "@mui/icons-material/Close";
import ViewColumnIcon from "@mui/icons-material/ViewColumn";
import FileDownloadIcon from "@mui/icons-material/FileDownload";
import type {
  Dimension,
  DrillableHierarchy,
  DrillThroughResponse,
  HierarchyPathEntry,
} from "../../../../api/types";
import { renderDimValue } from "../pivot";
import { routeBadgeLabel } from "../routeLabels";
import type { DrillContext } from "../types";
import PivotErrorAlert from "../PivotErrorAlert";
import type { PivotPanelError } from "../pivotErrors";
import { drillCurrentPageCsvFilename, drillRowsToCsv } from "./drillCsv";
import { downloadText } from "../export/download";

export type DrillPageSize = 50 | 100 | 200;
export const DRILL_PAGE_SIZES: DrillPageSize[] = [50, 100, 200];

type Props = {
  open: boolean;
  loading: boolean;
  // Bug-8182 (review B4): structured so the friendly message leads and raw
  // backend/transport text stays behind the collapsed accordion.
  error: PivotPanelError | null;
  result: DrillThroughResponse | null;
  context: DrillContext | null;
  rowDims: Dimension[];
  colDims: Dimension[];
  pageSize: DrillPageSize;
  hasPrev: boolean;
  modelId: string;
  hierarchyId: string | null;
  hierarchyOptions: DrillableHierarchy[];
  onClose: () => void;
  onLoadNextPage: () => void;
  onLoadPrevPage: () => void;
  onPageSizeChange: (size: DrillPageSize) => void;
  onSelectHierarchy: (hierarchyId: string) => void;
  onDrillRow?: (
    row: Record<string, unknown>,
    hierarchyId: string,
    pathEntry: HierarchyPathEntry,
  ) => void;
};

function columnsStorageKey(modelId: string, measureId: string): string {
  return `drill.visibleCols.${modelId}.${measureId}`;
}

function readVisibleColumns(
  modelId: string,
  measureId: string,
  allColumns: string[],
): string[] {
  if (!modelId || !measureId) return allColumns;
  try {
    const raw = sessionStorage.getItem(columnsStorageKey(modelId, measureId));
    if (!raw) return allColumns;
    const parsed = JSON.parse(raw) as unknown;
    if (!Array.isArray(parsed)) return allColumns;
    const filtered = parsed.filter(
      (v): v is string => typeof v === "string" && allColumns.includes(v),
    );
    return filtered.length > 0 ? filtered : allColumns;
  } catch {
    return allColumns;
  }
}

function writeVisibleColumns(
  modelId: string,
  measureId: string,
  columns: string[],
): void {
  if (!modelId || !measureId) return;
  try {
    sessionStorage.setItem(
      columnsStorageKey(modelId, measureId),
      JSON.stringify(columns),
    );
  } catch {
    // sessionStorage quota or disabled
  }
}

export default function DrillThroughPanel({
  open,
  loading,
  error,
  result,
  context,
  rowDims,
  colDims,
  pageSize,
  hasPrev,
  modelId,
  hierarchyId,
  hierarchyOptions,
  onClose,
  onLoadNextPage,
  onLoadPrevPage,
  onPageSizeChange,
  onSelectHierarchy,
  onDrillRow,
}: Props) {
  const t = useT();
  const measureId = context?.measure.id ?? "";
  const allColumns = useMemo(
    () => result?.columns ?? [],
    [result?.columns],
  );

  const [visibleColumns, setVisibleColumns] = useState<string[]>([]);
  const hydratedFor = useRef<string>("");

  useEffect(() => {
    if (!measureId || allColumns.length === 0) return;
    const sentinel = `${measureId}:${allColumns.join("|")}`;
    if (hydratedFor.current === sentinel) return;
    hydratedFor.current = sentinel;
    setVisibleColumns(readVisibleColumns(modelId, measureId, allColumns));
  }, [measureId, allColumns, modelId]);

  const [columnsAnchor, setColumnsAnchor] = useState<HTMLElement | null>(null);

  function toggleColumn(name: string) {
    const next = visibleColumns.includes(name)
      ? visibleColumns.filter((n) => n !== name)
      : [...allColumns].filter((n) => visibleColumns.includes(n) || n === name);
    setVisibleColumns(next);
    writeVisibleColumns(modelId, measureId, next);
  }

  function selectAllColumns() {
    setVisibleColumns(allColumns);
    writeVisibleColumns(modelId, measureId, allColumns);
  }

  function handleExportCsv() {
    if (!result || !context) return;
    const body = drillRowsToCsv(result, visibleColumns);
    downloadText(
      drillCurrentPageCsvFilename(context.measure.display_name),
      "text/csv",
      body,
    );
  }

  const hasNext = Boolean(result?.page.has_more);
  const canExport = Boolean(result && visibleColumns.length > 0);
  const filterCount = (context?.coord.rowKey.length ?? 0) + (context?.coord.colKey.length ?? 0);
  const isHierarchy = result?.drill_mode === "hierarchy";
  const canDrillRow =
    isHierarchy && !loading && onDrillRow && result?.drillable_hierarchies.length;

  const showHierarchyPicker =
    hierarchyOptions.length > 1 && !hierarchyId && !result && !loading;

  function handleRowClick(row: Record<string, unknown>) {
    if (!canDrillRow || !result || !onDrillRow) return;
    const drillDim = result.drill_dimension;
    if (!drillDim) return;
    const hid =
      hierarchyId ||
      result.drillable_hierarchies[0]?.hierarchy_id;
    if (!hid) return;
    const pathEntry: HierarchyPathEntry = {
      level_name: drillDim.display_name,
      dimension_name: drillDim.name,
      value: row[drillDim.name] ?? null,
    };
    onDrillRow(row, hid, pathEntry);
  }

  if (!open) return null;

  return (
    <Paper
      variant="outlined"
      sx={{ display: "flex", flexDirection: "column", borderRadius: 1, overflow: "hidden" }}
    >
      <Stack
        direction="row"
        alignItems="center"
        justifyContent="space-between"
        gap={1}
        sx={{ px: 1.5, py: 1.25, bgcolor: "background.paper", borderBottom: 1, borderColor: "divider" }}
      >
        <Stack spacing={0.25} minWidth={0}>
          <Typography variant="subtitle1" fontWeight={700} noWrap>
            {t("drill.drawerTitle", { name: context?.measure.display_name ?? "" })}
          </Typography>
          <Typography variant="caption" color="text.secondary">
            {filterCount > 0 ? t("drill.filtersLabel", { count: String(filterCount) }) : t("drill.sourceRows")}
          </Typography>
        </Stack>
        <IconButton size="small" onClick={onClose} aria-label={t("drill.closeAriaLabel")}>
          <CloseIcon fontSize="small" />
        </IconButton>
      </Stack>

      <Box sx={{ p: 1.5, display: "flex", flexDirection: "column", gap: 1 }}>

      {/* Breadcrumb path */}
      {result && result.hierarchy_path.length > 0 && (
        <Breadcrumbs separator=">" sx={{ fontSize: "0.8rem" }}>
          {result.hierarchy_path.map((entry, i) => (
            <Typography key={i} variant="caption" color="text.secondary">
              {entry.level_name} = {renderDimValue(entry.value)}
            </Typography>
          ))}
          {result.drill_dimension && (
            <Typography variant="caption" color="text.primary" fontWeight={600}>
              {result.drill_dimension.display_name}
            </Typography>
          )}
        </Breadcrumbs>
      )}

      {/* Cell filters as plain text */}
      {context && filterCount > 0 && (
        <Typography variant="caption" color="text.secondary">
          {/* Bug-6285: show business display names, matching the grid/breadcrumb/export. */}
          {[
            ...rowDims.slice(0, context.coord.rowKey.length).map((d, i) => `${d.display_name || d.name} = ${context.coord.rowKey[i]}`),
            ...colDims.slice(0, context.coord.colKey.length).map((d, i) => `${d.display_name || d.name} = ${context.coord.colKey[i]}`),
          ].join(", ")}
        </Typography>
      )}

      {/* Hierarchy picker — multiple drill paths available */}
      {showHierarchyPicker && (
        <Stack spacing={1}>
          <Typography variant="body2" color="text.secondary">
            {t("drill.multiplePaths")}
          </Typography>
          {hierarchyOptions.map((h) => (
            <Button
              key={h.hierarchy_id}
              variant="outlined"
              size="small"
              onClick={() => onSelectHierarchy(h.hierarchy_id)}
              sx={{ justifyContent: "flex-start", textTransform: "none" }}
            >
              <Stack>
                <Typography variant="body2" fontWeight={600}>
                  {h.hierarchy_name}
                </Typography>
                <Typography variant="caption" color="text.secondary">
                  {h.current_level_name} &rarr; {h.next_level_name}
                </Typography>
              </Stack>
            </Button>
          ))}
        </Stack>
      )}

      {/* Drill mode — plain text */}
      {result && (
        <Stack direction="row" spacing={1} alignItems="center" flexWrap="wrap">
          <Typography
            variant="caption"
            sx={{ fontWeight: 700, color: isHierarchy ? "primary.main" : "text.secondary" }}
          >
            {isHierarchy ? t("drill.hierarchyDrill") : t("drill.leafLevel")}
          </Typography>
          {isHierarchy && (
            <Typography variant="caption" color="text.secondary">
              {t("drill.clickToDrillDeeper")}
            </Typography>
          )}
        </Stack>
      )}

      <Stack direction="row" gap={1} alignItems="center" flexWrap="wrap">
        <Tooltip title={t("drill.chooseColumns")}>
          <span>
            <Button
              size="small"
              variant="outlined"
              startIcon={<ViewColumnIcon fontSize="small" />}
              disabled={allColumns.length === 0}
              onClick={(e) => setColumnsAnchor(e.currentTarget)}
            >
              {t("drill.columnsButton", { visible: String(visibleColumns.length), total: String(allColumns.length) })}
            </Button>
          </span>
        </Tooltip>
        <Menu
          open={Boolean(columnsAnchor)}
          anchorEl={columnsAnchor}
          onClose={() => setColumnsAnchor(null)}
          PaperProps={{ sx: { maxHeight: 320 } }}
        >
          <MenuItem onClick={selectAllColumns} dense>
            <ListItemText primary={t("drill.showAll")} />
          </MenuItem>
          <Divider />
          {allColumns.map((name) => (
            <MenuItem
              key={name}
              dense
              onClick={() => toggleColumn(name)}
              disableRipple
            >
              <Checkbox size="small" checked={visibleColumns.includes(name)} />
              <ListItemText primary={name} />
            </MenuItem>
          ))}
        </Menu>

        <FormControl size="small" sx={{ minWidth: 120 }}>
          <InputLabel>{t("drill.pageSize")}</InputLabel>
          <Select
            label={t("drill.pageSize")}
            value={pageSize}
            onChange={(e) => onPageSizeChange(Number(e.target.value) as DrillPageSize)}
          >
            {DRILL_PAGE_SIZES.map((s) => (
              <MenuItem key={s} value={s}>
                {s}
              </MenuItem>
            ))}
          </Select>
        </FormControl>

        <Button
          size="small"
          variant="outlined"
          disabled={loading || !hasPrev}
          onClick={onLoadPrevPage}
        >
          {t("drill.prev")}
        </Button>
        <Button
          size="small"
          variant="outlined"
          disabled={loading || !hasNext}
          onClick={onLoadNextPage}
        >
          {t("drill.next")}
        </Button>

        <Box sx={{ flexGrow: 1 }} />

        <Tooltip title={t("drill.exportCsvTooltip")}>
          <span>
            <Button
              size="small"
              variant="outlined"
              startIcon={<FileDownloadIcon fontSize="small" />}
              disabled={!canExport}
              onClick={handleExportCsv}
            >
              {t("drill.exportCsv")}
            </Button>
          </span>
        </Tooltip>
      </Stack>

      {error && <PivotErrorAlert error={error} />}
      {loading && <CircularProgress size={20} />}

      {result && (
        <>
          <Stack direction="row" gap={1.5} alignItems="center" flexWrap="wrap">
            {result.route_type !== "" && (
              <Typography variant="caption" color="text.secondary">
                {/* Bug-6282: translate route_type the same way the pivot route
                    badge does, rather than printing the raw English value. */}
                {t("drill.routeInfo", { route: routeBadgeLabel(result.route_type, t) })}
              </Typography>
            )}
            <Typography variant="caption" color="text.secondary">
              {t("drill.rowsOnPage", { count: String(result.rows.length) })}
            </Typography>
            {result.execution_ms > 0 && (
              <Typography variant="caption" color="text.secondary">
                {t("drill.executionMs", { ms: String(result.execution_ms) })}
              </Typography>
            )}
          </Stack>
          <TableContainer
            component={Paper}
            variant="outlined"
            sx={{ maxHeight: 460, overflow: "auto", borderRadius: 1 }}
          >
            <Table size="small" stickyHeader>
              <TableHead>
                <TableRow>
                  {visibleColumns.map((col) => (
                    <TableCell key={col} sx={{ fontWeight: 600 }}>
                      {col}
                    </TableCell>
                  ))}
                </TableRow>
              </TableHead>
              <TableBody>
                {result.rows.map((row, i) => (
                  <TableRow
                    key={i}
                    hover={Boolean(canDrillRow)}
                    sx={canDrillRow ? { cursor: "pointer" } : undefined}
                    onClick={canDrillRow ? () => handleRowClick(row) : undefined}
                  >
                    {visibleColumns.map((col) => (
                      <TableCell key={col} sx={{ whiteSpace: "nowrap", fontVariantNumeric: "tabular-nums" }}>
                        {renderDimValue(row[col])}
                      </TableCell>
                    ))}
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </TableContainer>
        </>
      )}
      </Box>
    </Paper>
  );
}
