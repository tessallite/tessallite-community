import { useEffect, useMemo, useState } from "react";
import { safeLocalGet } from "../../utils/safeLocalStorage";
import { useParams } from "react-router-dom";
import { useMutation, useQueries, useQueryClient } from "@tanstack/react-query";
import { useT } from "../../i18n";
import {
  Alert,
  Box,
  Button,
  Card,
  CardContent,
  Chip,
  CircularProgress,
  Dialog,
  DialogActions,
  DialogContent,
  DialogTitle,
  FormControl,
  FormControlLabel,
  InputLabel,
  MenuItem,
  Radio,
  RadioGroup,
  Select,
  Stack,
  Table,
  TableBody,
  TableCell,
  TableContainer,
  TableHead,
  TableRow,
  TextField,
  Tooltip,
  Typography,
} from "@mui/material";
import RefreshIcon from "@mui/icons-material/Refresh";
import { optimizerApiClient } from "../../api/client";
import { useSources } from "../../api/hooks";
import type {
  ColumnStatistics,
  Source,
  SourceStatistics,
  StatsRefreshCadence,
  TableStatistics,
} from "../../api/types";

function formatBytes(bytes: number | null): string {
  if (bytes === null) return "—";
  if (bytes < 1024) return `${bytes} B`;
  const units = ["KB", "MB", "GB", "TB"];
  let v = bytes / 1024;
  let i = 0;
  while (v >= 1024 && i < units.length - 1) {
    v /= 1024;
    i += 1;
  }
  return `${v.toFixed(1)} ${units[i]}`;
}

function formatNumber(n: number | null): string {
  if (n === null) return "—";
  return n.toLocaleString();
}

function formatPercent(ratio: number | null): string {
  if (ratio === null) return "—";
  return `${(ratio * 100).toFixed(2)}%`;
}

function distinctAsPercent(distinct: number | null, rowCount: number | null): string {
  if (distinct === null || rowCount === null || rowCount === 0) return "—";
  return `${((distinct / rowCount) * 100).toFixed(1)}%`;
}

function freshnessLabel(iso: string | null, t?: ReturnType<typeof useT>): { label: string; tone: "success" | "warning" | "default" } {
  if (!iso) return { label: t ? t("statistics.never") : "never", tone: "warning" };
  const last = new Date(iso).getTime();
  const ageMs = Date.now() - last;
  const days = ageMs / (1000 * 60 * 60 * 24);
  if (days < 1) return { label: `${Math.round(ageMs / 60000)} ${t ? t("statistics.minAgo") : "min ago"}`, tone: "success" };
  if (days < 7) return { label: `${Math.round(days)} ${t ? t("statistics.daysAgo") : "d ago"}`, tone: "success" };
  if (days < 30) return { label: `${Math.round(days)} ${t ? t("statistics.daysAgo") : "d ago"}`, tone: "warning" };
  return { label: new Date(iso).toLocaleDateString(), tone: "warning" };
}

type SampleOption = "default" | "custom" | "full";

interface StatisticsPanelProps {
  initialSourceId?: string;
}

export default function StatisticsPanel({ initialSourceId }: StatisticsPanelProps = {}) {
  const t = useT();
  const { projectId, modelId } = useParams<{ projectId: string; modelId: string }>();
  const sources = useSources(projectId!, modelId!);
  const sourceList: Source[] = sources.data ?? [];
  const [activeSourceId, setActiveSourceId] = useState<string | null>(initialSourceId ?? null);
  const [sampleDialogOpen, setSampleDialogOpen] = useState(false);
  const [sampleOption, setSampleOption] = useState<SampleOption>("default");
  const [customSampleSize, setCustomSampleSize] = useState("500");

  useEffect(() => {
    if (sourceList.length && !activeSourceId) {
      setActiveSourceId(initialSourceId ?? sourceList[0].id);
    }
  }, [sourceList, activeSourceId, initialSourceId]);

  const qc = useQueryClient();
  const statsQueries = useQueries({
    queries: sourceList.map((s) => ({
      queryKey: ["source-statistics", s.id],
      queryFn: () => optimizerApiClient.getSourceStatistics(s.id),
      enabled: Boolean(s.id),
    })),
  });
  const statsBySourceId = useMemo(() => {
    const map: Record<string, SourceStatistics | undefined> = {};
    sourceList.forEach((s, i) => {
      map[s.id] = statsQueries[i]?.data as SourceStatistics | undefined;
    });
    return map;
  }, [sourceList, statsQueries]);

  const refreshMutation = useMutation({
    mutationFn: (vars: { sourceId: string; sampleLimit?: number | null; lowCardinalityThreshold?: number }) =>
      optimizerApiClient.refreshSourceStatistics(vars.sourceId, vars.sampleLimit, vars.lowCardinalityThreshold),
    onSuccess: (_data, vars) => {
      qc.invalidateQueries({ queryKey: ["source-statistics", vars.sourceId] });
    },
  });

  const cadenceMutation = useMutation({
    mutationFn: (vars: { sourceId: string; modelTableId: string; cadence: StatsRefreshCadence }) =>
      optimizerApiClient.updateTableStatsCadence(
        vars.sourceId,
        vars.modelTableId,
        vars.cadence,
      ),
    onSuccess: (_data, vars) => {
      qc.invalidateQueries({ queryKey: ["source-statistics", vars.sourceId] });
    },
  });

  const activeSource = sourceList.find((s) => s.id === activeSourceId) ?? null;
  const activeStats = activeSourceId ? statsBySourceId[activeSourceId] : undefined;
  const activeQuery = sourceList.findIndex((s) => s.id === activeSourceId);
  const queryState = activeQuery >= 0 ? statsQueries[activeQuery] : null;

  function handleRecomputeClick() {
    setSampleDialogOpen(true);
  }

  function handleSampleConfirm() {
    setSampleDialogOpen(false);
    if (!activeSourceId) return;
    let sampleLimit: number | null | undefined;
    if (sampleOption === "full") {
      sampleLimit = 0;
    } else if (sampleOption === "custom") {
      const parsed = parseInt(customSampleSize, 10);
      sampleLimit = isNaN(parsed) || parsed <= 0 ? undefined : parsed;
    }
    const storedThreshold = safeLocalGet("builder.settings.lowCardinalityThreshold", "");
    const threshold = storedThreshold ? parseInt(storedThreshold, 10) : undefined;
    refreshMutation.mutate({
      sourceId: activeSourceId,
      sampleLimit,
      lowCardinalityThreshold: threshold && threshold > 0 ? threshold : undefined,
    });
  }

  if (sources.isLoading) {
    return (
      <Box sx={{ p: 4, display: "flex", justifyContent: "center" }}>
        <CircularProgress size={22} />
      </Box>
    );
  }

  if (!sourceList.length) {
    return (
      <Box sx={{ p: 2 }}>
        <Alert severity="info">
          {t("statistics.noSourcesRegistered")}
        </Alert>
      </Box>
    );
  }

  return (
    <Box sx={{ p: 2, display: "flex", flexDirection: "column", gap: 2 }}>
      <Stack direction="row" spacing={2} alignItems="center" flexWrap="wrap">
        <FormControl size="small" sx={{ minWidth: 240 }}>
          <InputLabel id="stats-source-label">{t("statistics.source")}</InputLabel>
          <Select
            labelId="stats-source-label"
            label={t("statistics.source")}
            value={activeSourceId ?? ""}
            onChange={(e) => setActiveSourceId(String(e.target.value))}
          >
            {sourceList.map((s) => (
              <MenuItem key={s.id} value={s.id}>
                {s.display_name}
              </MenuItem>
            ))}
          </Select>
        </FormControl>
        <Button
          size="small"
          variant="contained"
          startIcon={<RefreshIcon />}
          disabled={!activeSourceId || refreshMutation.isPending}
          onClick={handleRecomputeClick}
        >
          {refreshMutation.isPending ? t("statistics.refreshing") : t("statistics.recompute")}
        </Button>
        {refreshMutation.isError && (
          <Alert severity="error" sx={{ flexGrow: 1 }}>
            {String((refreshMutation.error as Error).message ?? t("statistics.refreshFailed"))}
          </Alert>
        )}
      </Stack>

      {queryState?.isLoading && (
        <Box sx={{ p: 2, display: "flex", justifyContent: "center" }}>
          <CircularProgress size={18} />
        </Box>
      )}

      {activeStats && activeStats.tables.length === 0 && (
        <Alert severity="info">
          {t("statistics.noStatisticsCollected", { source: activeSource?.display_name ?? t("statistics.thisSource") })}
        </Alert>
      )}

      {activeStats?.tables.map((t) => (
        <TableCard
          key={t.model_table_id}
          table={t}
          onCadenceChange={(next) =>
            activeSourceId &&
            cadenceMutation.mutate({
              sourceId: activeSourceId,
              modelTableId: t.model_table_id,
              cadence: next,
            })
          }
          cadenceSaving={cadenceMutation.isPending}
        />
      ))}

      {activeStats && activeStats.joins.length > 0 && (
        <JoinSelectivityCard joins={activeStats.joins} />
      )}

      {/* Sample size dialog */}
      <Dialog
        open={sampleDialogOpen}
        onClose={() => setSampleDialogOpen(false)}
        maxWidth="xs"
        fullWidth
      >
        <DialogTitle>{t("statistics.sampleSize")}</DialogTitle>
        <DialogContent>
          <Typography variant="body2" color="text.secondary" sx={{ mb: 2 }}>
            {t("statistics.sampleSizeDescription")}
          </Typography>
          <RadioGroup
            value={sampleOption}
            onChange={(e) => setSampleOption(e.target.value as SampleOption)}
          >
            <FormControlLabel
              value="default"
              control={<Radio size="small" />}
              label={t("statistics.sampleOptionDefault")}
            />
            <FormControlLabel
              value="custom"
              control={<Radio size="small" />}
              label={t("statistics.sampleOptionCustom")}
            />
            {sampleOption === "custom" && (
              <TextField
                size="small"
                type="number"
                value={customSampleSize}
                onChange={(e) => setCustomSampleSize(e.target.value)}
                sx={{ ml: 4, mb: 1, width: 160 }}
                inputProps={{ min: 1 }}
                placeholder="500"
              />
            )}
            <FormControlLabel
              value="full"
              control={<Radio size="small" />}
              label={t("statistics.sampleOptionFull")}
            />
          </RadioGroup>
        </DialogContent>
        <DialogActions>
          <Button onClick={() => setSampleDialogOpen(false)}>{t("common.cancel")}</Button>
          <Button variant="contained" onClick={handleSampleConfirm}>
            {t("common.run")}
          </Button>
        </DialogActions>
      </Dialog>
    </Box>
  );
}

function TableCard({
  table,
  onCadenceChange,
  cadenceSaving,
}: {
  table: TableStatistics;
  onCadenceChange: (next: StatsRefreshCadence) => void;
  cadenceSaving: boolean;
}) {
  const t = useT();
  const fresh = freshnessLabel(table.last_refreshed_at, t);
  const nextDue = table.next_refresh_at
    ? new Date(table.next_refresh_at).toLocaleString()
    : null;
  return (
    <Card variant="outlined">
      <CardContent>
        <Stack direction="row" alignItems="center" spacing={1} flexWrap="wrap">
          <Typography variant="subtitle1" sx={{ fontFamily: "monospace" }}>
            {table.physical_name}
          </Typography>
          <Chip
            size="small"
            label={t("statistics.rows", { count: formatNumber(table.row_count) })}
            variant="outlined"
          />
          <Chip
            size="small"
            label={t("statistics.size", { size: formatBytes(table.table_size_bytes) })}
            variant="outlined"
          />
          <RefreshScheduleSelect
            value={table.refresh_cadence}
            onChange={onCadenceChange}
            disabled={cadenceSaving}
          />
          <Chip
            size="small"
            label={t("statistics.refreshed", { time: fresh.label })}
            color={fresh.tone === "default" ? undefined : fresh.tone}
          />
          {nextDue && (
            <Chip
              size="small"
              variant="outlined"
              label={t("statistics.next", { time: nextDue })}
            />
          )}
        </Stack>
        {table.columns.length === 0 ? (
          <Typography variant="caption" color="text.secondary" sx={{ mt: 1 }}>
            {t("statistics.noColumnStatistics")}
          </Typography>
        ) : (
          <ColumnTable columns={table.columns} tableRowCount={table.row_count} />
        )}
      </CardContent>
    </Card>
  );
}

function ColumnTable({ columns, tableRowCount }: { columns: ColumnStatistics[]; tableRowCount: number | null }) {
  const t = useT();
  return (
    <TableContainer sx={{ mt: 1, maxHeight: 360, overflow: "auto" }}>
      <Table size="small" stickyHeader>
        <TableHead>
          <TableRow>
            <TableCell sx={{ fontWeight: 600 }}>{t("statistics.columnHeader")}</TableCell>
            <TableCell sx={{ fontWeight: 600 }}>{t("statistics.typeHeader")}</TableCell>
            <TableCell sx={{ fontWeight: 600 }} align="right">
              {t("statistics.distinctHeader")}
            </TableCell>
            <TableCell sx={{ fontWeight: 600 }} align="right">
              {t("statistics.cardinalityHeader")}
            </TableCell>
            <TableCell sx={{ fontWeight: 600 }} align="right">
              {t("statistics.nullPercentHeader")}
            </TableCell>
            <TableCell sx={{ fontWeight: 600 }}>{t("statistics.minHeader")}</TableCell>
            <TableCell sx={{ fontWeight: 600 }}>{t("statistics.maxHeader")}</TableCell>
            <TableCell sx={{ fontWeight: 600 }}>{t("statistics.topValuesHeader")}</TableCell>
          </TableRow>
        </TableHead>
        <TableBody>
          {columns.map((c) => {
            const rowCount = c.row_count ?? tableRowCount;
            return (
              <TableRow key={c.model_column_id}>
                <TableCell sx={{ fontFamily: "monospace" }}>{c.column_name}</TableCell>
                <TableCell>
                  <Typography variant="caption" color="text.secondary">
                    {c.data_type ?? t("common.separator")}
                  </Typography>
                </TableCell>
                <TableCell align="right">
                  <Tooltip title={t("statistics.distinctValuesCount", { count: formatNumber(c.distinct_count) })}>
                    <span>{formatNumber(c.distinct_count)}</span>
                  </Tooltip>
                </TableCell>
                <TableCell align="right">
                  <Tooltip title={t("statistics.cardinalityRatio", { distinct: formatNumber(c.distinct_count), total: formatNumber(rowCount) })}>
                    <span>{distinctAsPercent(c.distinct_count, rowCount)}</span>
                  </Tooltip>
                </TableCell>
                <TableCell align="right">{formatPercent(c.null_ratio)}</TableCell>
                <TableCell sx={{ maxWidth: 120, overflow: "hidden", textOverflow: "ellipsis" }}>
                  {c.min_value ?? t("common.separator")}
                </TableCell>
                <TableCell sx={{ maxWidth: 120, overflow: "hidden", textOverflow: "ellipsis" }}>
                  {c.max_value ?? t("common.separator")}
                </TableCell>
                <TableCell>
                  {(() => {
                    const isLowCardinality =
                      c.distinct_count !== null && c.distinct_count <= c.top_values.length;
                    const shown = isLowCardinality ? c.top_values : c.top_values.slice(0, 5);
                    return shown.map((tv, i) => (
                      <Chip
                        key={i}
                        size="small"
                        label={t("statistics.topValueWithFrequency", { value: String(tv.value), frequency: formatPercent(tv.frequency) })}
                        sx={{ mr: 0.5, mb: 0.5 }}
                        variant="outlined"
                      />
                    ));
                  })()}
                </TableCell>
              </TableRow>
            );
          })}
        </TableBody>
      </Table>
    </TableContainer>
  );
}

function JoinSelectivityCard({ joins }: { joins: { join_id: string; selectivity: number | null; left_distinct_count: number | null; right_distinct_count: number | null; match_ratio: number | null }[] }) {
  const t = useT();
  return (
    <Card variant="outlined">
      <CardContent>
        <Typography variant="subtitle1" gutterBottom>
          {t("statistics.joinSelectivity")}
        </Typography>
        <TableContainer>
          <Table size="small">
            <TableHead>
              <TableRow>
                <TableCell sx={{ fontWeight: 600 }}>{t("statistics.joinHeader")}</TableCell>
                <TableCell sx={{ fontWeight: 600 }} align="right">
                  {t("statistics.selectivityHeader")}
                </TableCell>
                <TableCell sx={{ fontWeight: 600 }} align="right">
                  {t("statistics.leftDistinctHeader")}
                </TableCell>
                <TableCell sx={{ fontWeight: 600 }} align="right">
                  {t("statistics.rightDistinctHeader")}
                </TableCell>
                <TableCell sx={{ fontWeight: 600 }} align="right">
                  {t("statistics.matchRatioHeader")}
                </TableCell>
              </TableRow>
            </TableHead>
            <TableBody>
              {joins.map((j) => (
                <TableRow key={j.join_id}>
                  <TableCell sx={{ fontFamily: "monospace" }}>{j.join_id.slice(0, 8)}…</TableCell>
                  <TableCell align="right">{j.selectivity?.toFixed(4) ?? t("common.separator")}</TableCell>
                  <TableCell align="right">{formatNumber(j.left_distinct_count)}</TableCell>
                  <TableCell align="right">{formatNumber(j.right_distinct_count)}</TableCell>
                  <TableCell align="right">{formatPercent(j.match_ratio)}</TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </TableContainer>
      </CardContent>
    </Card>
  );
}

export function StatsRefreshCadenceSelect({
  value,
  onChange,
  disabled,
}: {
  value: StatsRefreshCadence;
  onChange: (next: StatsRefreshCadence) => void;
  disabled?: boolean;
}) {
  return (
    <RefreshScheduleSelect value={value} onChange={onChange} disabled={disabled} />
  );
}

function RefreshScheduleSelect({
  value,
  onChange,
  disabled,
}: {
  value: StatsRefreshCadence;
  onChange: (next: StatsRefreshCadence) => void;
  disabled?: boolean;
}) {
  const t = useT();
  return (
    <Tooltip title={t("statistics.refreshScheduleTooltip")}>
      <FormControl size="small" disabled={disabled} sx={{ minWidth: 160 }}>
        <InputLabel id="stats-cadence-label">{t("statistics.refreshSchedule")}</InputLabel>
        <Select
          labelId="stats-cadence-label"
          label={t("statistics.refreshSchedule")}
          value={value}
          onChange={(e) => onChange(e.target.value as StatsRefreshCadence)}
        >
          <MenuItem value="manual">{t("statistics.cadenceManual")}</MenuItem>
          <MenuItem value="daily">{t("statistics.cadenceDaily")}</MenuItem>
          <MenuItem value="weekly">{t("statistics.cadenceWeekly")}</MenuItem>
          <MenuItem value="monthly">{t("statistics.cadenceMonthly")}</MenuItem>
        </Select>
      </FormControl>
    </Tooltip>
  );
}
