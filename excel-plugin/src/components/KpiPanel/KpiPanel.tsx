import { useState, useEffect, useMemo, useCallback, useRef } from 'react';
import {
  Box, Typography, Chip, Skeleton, TextField, InputAdornment, IconButton, Checkbox,
} from '@mui/material';
import {
  SearchOutlined,
  TableChartOutlined,
  BarChartOutlined,
  DashboardOutlined,
  RefreshOutlined,
  ChevronRight,
  ExpandMore,
} from '@mui/icons-material';
import { tokens } from '../../theme';
import { getKpis, evaluateKpiBatch, getMeasures, reportKpiUsage } from '../../api/modelService';
import { ApiError } from '../../api/client';
import { buildScorecardPayload, type EvaluatedScorecardKpi } from '../../utils/kpiScorecard';
import type { Kpi, KpiBatchResult, Measure } from '../../types/tessallite';
import { strings, templates } from '../../i18n/strings';
import { useToast } from '../Toast/ToastProvider';

interface KpiPanelProps {
  projectId: string;
  modelId: string;
  personaId?: string | null;
  /** Bug-6903: model slug for TESSALLITE.KPI formula scorecard (live refresh). */
  modelSlug?: string | null;
  onInsertTable: (headers: string[], rows: (string | number)[][]) => Promise<{ address: string | null; postStepWarning: boolean }>;
  onInsertChart: (headers: string[], rows: (string | number)[][]) => Promise<string | null>;
  onInsertScorecard: (
    kpis: EvaluatedScorecardKpi[],
    connectionName: string,
    modelSlug?: string,
  ) => Promise<string | null>;
  connectionName: string;
}

type FilterMode = 'all' | 'certified';
type StatusFilter = 1 | 0 | -1 | null;

const STATUS_COLORS: Record<number, string> = {
  1: '#2e7d32',
  0: '#ed6c02',
  [-1]: '#d32f2f',
};

const TREND_ARROWS: Record<string, string> = {
  improving: '\u2191',
  stable: '\u2192',
  declining: '\u2193',
};

const TREND_COLORS: Record<string, string> = {
  improving: '#2e7d32',
  // Bug-6368: a stable trend is neutral, not a warning. Rendering it in warning
  // orange wrongly signalled a problem; use the neutral secondary text colour.
  stable: tokens.colorTextSecondary,
  declining: '#d32f2f',
};

export default function KpiPanel({
  projectId, modelId, personaId, modelSlug, onInsertTable, onInsertChart, onInsertScorecard, connectionName,
}: KpiPanelProps) {
  const { showToast } = useToast();
  const [kpis, setKpis] = useState<Kpi[]>([]);
  // F-025-10 / Bug-6730: scorecard insertions use evaluated literal values by
  // default, but the payload still resolves technical measure names for the
  // explicit advanced CUBE-formula paths and for legacy scorecard metadata.
  const [measuresById, setMeasuresById] = useState<Map<string, Measure>>(new Map());
  const [results, setResults] = useState<Map<string, KpiBatchResult>>(new Map());
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  // F-025-03: evaluation failure must be VISIBLE, not swallowed into an empty
  // map (which made the tab render every KPI as a grey dot with no explanation).
  // The KPI list still loads; this banner surfaces why values are missing.
  const [evalError, setEvalError] = useState<string | null>(null);
  const [filter, setFilter] = useState<FilterMode>('all');
  const [statusFilter, setStatusFilter] = useState<StatusFilter>(null);
  const [collapsedFolders, setCollapsedFolders] = useState<Set<string>>(() => new Set());
  const [search, setSearch] = useState('');
  const [debouncedSearch, setDebouncedSearch] = useState('');
  const debounceTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  // Bug-7387: 300ms debounce on search input to avoid per-keystroke filter jank.
  const handleSearchChange = useCallback((value: string) => {
    setSearch(value);
    if (debounceTimerRef.current) clearTimeout(debounceTimerRef.current);
    debounceTimerRef.current = setTimeout(() => setDebouncedSearch(value), 300);
  }, []);

  useEffect(() => () => {
    if (debounceTimerRef.current) clearTimeout(debounceTimerRef.current);
  }, []);

  const fetchData = useCallback(async () => {
    setLoading(true);
    setError(null);
    setEvalError(null);
    try {
      // Load KPIs first — the batch-evaluate endpoint requires their ids in
      // the request body (F-025-03: a body-less POST 422s). Measures are loaded
      // alongside so the scorecard can resolve value/goal measure names
      // (F-025-10); a measure-load failure must not block KPI display.
      const [kpiList, measureList] = await Promise.all([
        getKpis(projectId, modelId, personaId || undefined),
        getMeasures(projectId, modelId, personaId || undefined).catch(() => [] as Measure[]),
      ]);
      setKpis(kpiList);
      setMeasuresById(new Map(measureList.map(m => [m.id, m])));

      const map = new Map<string, KpiBatchResult>();
      if (kpiList.length > 0) {
        try {
          const batchResults = await evaluateKpiBatch(
            projectId, modelId, kpiList.map(k => k.id), personaId || undefined,
          );
          for (const r of batchResults) {
            map.set(r.kpi_id, r);
          }
        } catch {
          // KPIs still render; values are unavailable and the user is told why.
          setEvalError(strings.kpiPanel.evalError);
        }
      }
      setResults(map);
    } catch (e) {
      // Bug-8712: 409 is DEPLOYED_SNAPSHOT_INVALID — the pane asked for the
      // PUBLISHED definitions and the published version could not be read. Say
      // so and say what to do; "check your connection" sends the user after the
      // wrong problem, and falling back to the live draft is the leak this
      // contract closes.
      setError(
        e instanceof ApiError && e.status === 409
          ? strings.kpiPanel.deployedSnapshotInvalid
          : strings.kpiPanel.loadError,
      );
    } finally {
      setLoading(false);
    }
  }, [projectId, modelId, personaId]);

  useEffect(() => { fetchData(); }, [fetchData]);

  const searchedKpis = useMemo(() => {
    let list = kpis;
    if (filter === 'certified') {
      list = list.filter(k => k.certification_status === 'certified');
    }
    if (debouncedSearch.trim()) {
      const q = debouncedSearch.toLowerCase();
      list = list.filter(k =>
        (k.display_name || k.name).toLowerCase().includes(q) ||
        (k.description || '').toLowerCase().includes(q),
      );
    }
    return list;
  }, [kpis, filter, debouncedSearch]);

  const filteredKpis = useMemo(() => {
    if (statusFilter == null) return searchedKpis;
    return searchedKpis.filter(kpi => results.get(kpi.id)?.status === statusFilter);
  }, [searchedKpis, results, statusFilter]);

  const grouped = useMemo(() => {
    const groups = new Map<string, Kpi[]>();
    const ungrouped: Kpi[] = [];
    for (const kpi of filteredKpis) {
      if (kpi.display_folder) {
        const arr = groups.get(kpi.display_folder) || [];
        arr.push(kpi);
        groups.set(kpi.display_folder, arr);
      } else {
        ungrouped.push(kpi);
      }
    }
    const result: { folder?: string; kpis: Kpi[] }[] = [];
    for (const [folder, list] of groups) result.push({ folder, kpis: list });
    if (ungrouped.length > 0) result.push({ kpis: ungrouped });
    return result;
  }, [filteredKpis]);

  const statusCounts = useMemo(() => {
    let good = 0, warning = 0, poor = 0;
    for (const r of results.values()) {
      if (r.status === 1) good++;
      else if (r.status === 0) warning++;
      else if (r.status === -1) poor++;
    }
    return { good, warning, poor };
  }, [results]);

  const toggleFolder = useCallback((folder: string) => {
    setCollapsedFolders(previous => {
      const next = new Set(previous);
      if (next.has(folder)) next.delete(folder);
      else next.add(folder);
      return next;
    });
  }, []);

  const handleInsertTable = useCallback(async (kpi: Kpi) => {
    const r = results.get(kpi.id);
    const headers = [strings.kpiPanel.headers.kpi, strings.kpiPanel.headers.value, strings.kpiPanel.headers.goal, strings.kpiPanel.headers.status, strings.kpiPanel.headers.trend];
    const row: (string | number)[] = [
      kpi.display_name || kpi.name,
      r?.formatted_value ?? '',
      r?.formatted_goal ?? '',
      r?.status_label ?? '',
      r?.trend_label ?? '',
    ];
    await onInsertTable(headers, [row]);
  }, [results, onInsertTable]);

  const handleInsertChart = useCallback(async (kpi: Kpi) => {
    const r = results.get(kpi.id);
    if (!r || r.value == null || r.goal == null) return;
    const headers = [strings.kpiPanel.metric, kpi.display_name || kpi.name];
    const rows: (string | number)[][] = [
      [strings.kpiPanel.currentLabel, r.value],
      [strings.kpiPanel.targetLabel, r.goal],
    ];
    await onInsertChart(headers, rows);
  }, [results, onInsertChart]);

  const handleInsertAllScorecard = useCallback(async () => {
    // F-025-10: resolve each KPI's value/goal measure id to its technical name
    // and carry the static target literal when a KPI has no goal measure.
    // Bug-6367: route through buildScorecardPayload so this "insert all" path
    // applies the SAME deprecated-KPI filter as the Report Builder scorecard
    // path — deprecated KPIs must not be silently written into the scorecard.
    const payload = buildScorecardPayload(kpis, Array.from(measuresById.values()));

    if (payload.length === 0) {
      showToast(strings.toasts.noKpisAvailable, 'info');
      return;
    }

    // R1 Finding 2: when the panel-load evaluation failed (evalError set),
    // the results map is empty and the scorecard would contain all-null
    // values with a misleading success toast. Mirror ReportBuilder's
    // insert-time evaluation: re-evaluate and abort on failure.
    let effectiveResults = results;
    if (evalError) {
      try {
        const batchResults = await evaluateKpiBatch(
          projectId, modelId, payload.map(k => k.id), personaId || undefined,
        );
        effectiveResults = new Map<string, KpiBatchResult>();
        for (const r of batchResults) {
          effectiveResults.set(r.kpi_id, r);
        }
      } catch {
        showToast(strings.toasts.scorecardInsertionFailed, 'error');
        return;
      }
    }

    // Bug-6730: enrich every KPI with evaluated values from the already-loaded
    // batch results. The scorecard writes literal values, not workbook
    // connection-dependent CUBE formulas.
    const enrichedPayload = payload.map(k => {
      const ev = effectiveResults.get(k.id);
      return {
        ...k,
        evaluatedValue: ev?.value ?? null,
        evaluatedGoal: ev?.goal ?? null,
        evaluatedStatus: ev?.status ?? null,
      };
    });

    // Bug-6747: mirror ReportBuilder's error handling, toasts, composite-KPI
    // notice, undeployed-KPI warning, and usage telemetry (previously absent).
    try {
      // Bug-6903: pass model slug for TESSALLITE.KPI formula scorecard.
      const result = await onInsertScorecard(enrichedPayload, connectionName, modelSlug || undefined);
      if (result) {
        showToast(templates.toasts.scorecardInserted(payload.length), 'success');

        // Telemetry: report usage for each KPI in the scorecard.
        for (const k of payload) {
          reportKpiUsage(projectId, modelId, k.id, {
            cell_reference: result, usage_type: 'excel_insert',
          }).catch(() => {});
        }

        // R1 Finding 1: the KpiPanel scorecard is always literal (no CUBE
        // formulas) since Bug-6730, so composite/undeployed CUBE-formula
        // warnings are not surfaced here — they describe a behavior the
        // literal scorecard does not have. The ReportBuilder's current
        // scorecard path (also literal-only) intentionally omits them.
      }
    } catch {
      showToast(strings.toasts.scorecardInsertionFailed, 'error');
    }
  }, [kpis, measuresById, results, evalError, onInsertScorecard, connectionName, modelSlug, showToast, projectId, modelId, personaId]);

  return (
    <Box sx={{
      flex: 1,
      minHeight: 0,
      overflow: 'auto',
    }}>
      {/* Compact controls: one 34px row replaces the former title/filter rows. */}
      <Box sx={{
        minHeight: 34,
        height: 34,
        px: 1.25,
        display: 'flex',
        alignItems: 'center',
        gap: 0.75,
        borderBottom: `1px solid ${tokens.colorBorderLight}`,
      }}>
        <TextField
          size="small"
          placeholder={strings.kpiPanel.searchPlaceholder}
          value={search}
          onChange={(e) => handleSearchChange(e.target.value)}
          inputProps={{ 'aria-label': strings.kpiPanel.searchPlaceholder }}
          InputProps={{
            startAdornment: (
              <InputAdornment position="start" sx={{ mr: 0.5 }}>
                <SearchOutlined sx={{ fontSize: 14, color: tokens.colorTextSecondary }} />
              </InputAdornment>
            ),
            sx: { fontSize: 11, height: 24, px: 0.75 },
          }}
          sx={{
            flex: 1,
            minWidth: 0,
            '& .MuiOutlinedInput-root': { borderRadius: 0.5 },
          }}
        />
        <Box
          component="label"
          title={strings.kpiPanel.filterCertified}
          sx={{ display: 'flex', alignItems: 'center', gap: 0.5, flexShrink: 0, cursor: 'pointer' }}
        >
          <Checkbox
            checked={filter === 'certified'}
            onChange={(event) => setFilter(event.target.checked ? 'certified' : 'all')}
            inputProps={{ 'aria-label': strings.kpiPanel.filterCertified }}
            sx={{
              p: 0,
              width: 13,
              height: 13,
              '& .MuiSvgIcon-root': { fontSize: 14 },
            }}
          />
          <Typography component="span" sx={{ fontSize: 11, color: tokens.colorTextSecondary, whiteSpace: 'nowrap' }}>
            {strings.kpiPanel.filterCertified}
          </Typography>
        </Box>
        <IconButton
          size="small"
          onClick={fetchData}
          disabled={loading}
          title={strings.kpiPanel.refreshKpis}
          aria-label={strings.kpiPanel.refreshKpis}
          sx={{
            width: 24,
            height: 24,
            flexShrink: 0,
            border: `1px solid ${tokens.colorBorder}`,
            borderRadius: 0.5,
          }}
        >
          <RefreshOutlined sx={{ fontSize: 15 }} />
        </IconButton>
        <IconButton
          size="small"
          onClick={handleInsertAllScorecard}
          disabled={loading || Boolean(error) || kpis.length === 0}
          title={strings.kpiPanel.insertAllScorecard}
          aria-label={strings.kpiPanel.insertAllScorecard}
          sx={{
            width: 24,
            height: 24,
            flexShrink: 0,
            borderRadius: 0.5,
            color: tokens.colorWhite,
            bgcolor: tokens.colorPrimary,
            '&:hover': { bgcolor: tokens.colorPrimaryDark },
            '&.Mui-disabled': { color: tokens.colorWhite, bgcolor: tokens.colorPrimary, opacity: 0.45 },
          }}
        >
          <DashboardOutlined sx={{ fontSize: 15 }} />
        </IconButton>
      </Box>

      {/* Status strip: each pill is a filter, while the count remains the full list count. */}
      <Box sx={{
        minHeight: 26,
        height: 26,
        px: 1.25,
        display: 'flex',
        alignItems: 'center',
        gap: 0.75,
        overflow: 'hidden',
        borderBottom: `1px solid ${tokens.colorBorderLight}`,
      }}>
        <Typography sx={{ fontSize: 11, fontWeight: 600, color: tokens.colorTextSecondary, whiteSpace: 'nowrap' }}>
          {templates.kpiPanel.kpiCount(kpis.length)}
        </Typography>
        <Box sx={{ width: '1px', height: 12, bgcolor: tokens.colorBorderLight, flexShrink: 0 }} />
        {([
          { status: 1 as const, count: statusCounts.good, label: strings.kpiPanel.good, color: '#2e7d32', background: 'rgba(46,125,50,0.08)' },
          { status: 0 as const, count: statusCounts.warning, label: strings.kpiPanel.warning, color: '#ed6c02', background: 'rgba(237,108,2,0.08)' },
          { status: -1 as const, count: statusCounts.poor, label: strings.kpiPanel.poor, color: '#d32f2f', background: 'rgba(211,47,47,0.08)' },
        ]).filter(item => item.count > 0 || item.status === statusFilter).map(item => {
          const selected = statusFilter === item.status;
          return (
            <Chip
              key={item.status}
              clickable
              aria-pressed={selected}
              onClick={() => setStatusFilter(selected ? null : item.status)}
              label={`${templates.kpiPanel.statusCount(item.count, item.label)}${selected ? ' ×' : ''}`}
              title={templates.kpiPanel.statusCount(item.count, item.label)}
              size="small"
              sx={{
                height: 14,
                borderRadius: 7,
                fontSize: 9,
                fontWeight: 600,
                cursor: 'pointer',
                color: selected ? tokens.colorWhite : item.color,
                bgcolor: selected ? item.color : item.background,
                '& .MuiChip-label': { px: 0.625 },
              }}
            />
          );
        })}
        <Typography noWrap title={strings.kpiPanel.statusFilterHint} sx={{ fontSize: 10, color: tokens.colorTextSecondary, ml: 'auto', minWidth: 0 }}>{strings.kpiPanel.statusFilterHint}</Typography>
      </Box>

      {/* Evaluation error banner — list still renders, values unavailable (F-025-03). */}
      {evalError && (
        <Box sx={{ px: 1.25, py: 0.5, borderBottom: `1px solid ${tokens.colorBorderLight}`, bgcolor: 'rgba(237,108,2,0.06)' }}>
          <Typography sx={{ fontSize: 10.5, lineHeight: 1.3, color: '#ed6c02' }}>
            {evalError}
          </Typography>
        </Box>
      )}

      {loading ? (
        <Box sx={{ p: 1.5, display: 'flex', flexDirection: 'column', gap: 1.25 }}>
          {Array.from({ length: 6 }).map((_, index) => (
            <Skeleton key={index} variant="rectangular" height={30} sx={{ borderRadius: 0.5 }} />
          ))}
        </Box>
      ) : error ? (
        <Box sx={{ p: '32px 20px', textAlign: 'center' }}>
          <Typography sx={{ fontSize: 12, lineHeight: 1.4, color: tokens.colorRed, mb: 1.25 }}>
            {error}
          </Typography>
          <Box
            component="button"
            type="button"
            onClick={fetchData}
            sx={{
              height: 26,
              px: 1.5,
              fontSize: 12,
              borderRadius: 0.5,
              cursor: 'pointer',
              border: `1px solid ${tokens.colorPrimary}`,
              color: tokens.colorPrimary,
              bgcolor: 'transparent',
            }}
          >
            {strings.kpiPanel.tryAgain}
          </Box>
        </Box>
      ) : kpis.length === 0 ? (
        <Box sx={{ p: '36px 24px', textAlign: 'center' }}>
          <Typography sx={{ fontSize: 14, fontWeight: 600, color: tokens.colorCharcoal, mb: 0.5 }}>
            {strings.kpiPanel.noKpisTitle}
          </Typography>
          <Typography sx={{ fontSize: 12, lineHeight: 1.4, color: tokens.colorTextSecondary }}>
            {strings.kpiPanel.noKpisDescription}
          </Typography>
        </Box>
      ) : filteredKpis.length === 0 ? (
        <Typography sx={{ p: '28px 20px', fontSize: 12, color: tokens.colorTextSecondary, textAlign: 'center' }}>
          {strings.kpiPanel.noSearchMatch}
        </Typography>
      ) : (
        <Box sx={{ borderTop: `2px solid ${tokens.colorPrimary}` }}>
          {grouped.map((group, index) => {
            if (!group.folder) {
              return group.kpis.map(kpi => (
                <KpiRow
                  key={kpi.id}
                  kpi={kpi}
                  result={results.get(kpi.id)}
                  onInsertTable={() => handleInsertTable(kpi)}
                  onInsertChart={() => handleInsertChart(kpi)}
                />
              ));
            }

            const collapsed = collapsedFolders.has(group.folder);
            return (
              <Box key={group.folder || index}>
                <Box
                  component="button"
                  type="button"
                  onClick={() => toggleFolder(group.folder!)}
                  aria-expanded={!collapsed}
                  aria-label={group.folder}
                  sx={{
                    width: '100%',
                    height: 24,
                    minHeight: 24,
                    p: '0 6px 0 10px',
                    display: 'flex',
                    alignItems: 'center',
                    gap: 0.625,
                    border: 0,
                    borderBottom: `1px solid ${tokens.colorBorderLight}`,
                    bgcolor: tokens.colorSubtleFill,
                    color: tokens.colorTextSecondary,
                    cursor: 'pointer',
                    textAlign: 'left',
                    '&:hover': { bgcolor: tokens.colorPrimaryBg },
                  }}
                >
                  <Typography component="span" sx={{ fontSize: 10, fontWeight: 700, letterSpacing: '0.04em', textTransform: 'uppercase', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                    {group.folder}
                  </Typography>
                  <Typography component="span" sx={{ ml: 'auto', fontSize: 10, fontWeight: 400 }}>
                    {group.kpis.length}
                  </Typography>
                  {collapsed ? <ChevronRight sx={{ fontSize: 14 }} /> : <ExpandMore sx={{ fontSize: 14 }} />}
                </Box>
                {!collapsed && group.kpis.map(kpi => (
                  <KpiRow
                    key={kpi.id}
                    kpi={kpi}
                    result={results.get(kpi.id)}
                    onInsertTable={() => handleInsertTable(kpi)}
                    onInsertChart={() => handleInsertChart(kpi)}
                  />
                ))}
              </Box>
            );
          })}
        </Box>
      )}
    </Box>
  );
}

/* ---------- KPI Row card ---------- */

interface KpiRowProps {
  kpi: Kpi;
  result?: KpiBatchResult;
  onInsertTable: () => void;
  onInsertChart: () => void;
}

function KpiRow({ kpi, result, onInsertTable, onInsertChart }: KpiRowProps) {
  const statusColor = result?.status != null ? STATUS_COLORS[result.status] ?? tokens.colorTextSecondary : '#9a9a9a';
  const trendArrow = result?.trend_label ? TREND_ARROWS[result.trend_label.toLowerCase()] ?? '' : '';
  const trendColor = result?.trend_label ? TREND_COLORS[result.trend_label.toLowerCase()] ?? tokens.colorTextSecondary : tokens.colorTextSecondary;
  const chartAvailable = result != null && result.value != null && result.goal != null;
  const chartTitle = chartAvailable ? strings.kpiPanel.insertChartTitle : strings.kpiPanel.evalError;
  const displayName = kpi.display_name || kpi.name;
  const isCertified = kpi.certification_status === 'certified';
  const isDeprecated = kpi.certification_status === 'deprecated';
  const hasValue = result?.value != null && Boolean(result.formatted_value);

  return (
    <Box sx={{
      minHeight: 30,
      height: 30,
      p: '0 4px 0 10px',
      display: 'flex',
      alignItems: 'center',
      gap: 0.75,
      borderBottom: `1px solid ${tokens.colorBorderLight}`,
      bgcolor: tokens.colorWhite,
      fontSize: 12,
      '&:hover': { bgcolor: tokens.colorPrimaryBg },
    }}>
      <Box sx={{ width: 8, height: 8, borderRadius: '50%', bgcolor: statusColor, flexShrink: 0 }} />
      <Typography
        title={displayName}
        sx={{
          minWidth: 0,
          flex: 1,
          overflow: 'hidden',
          textOverflow: 'ellipsis',
          whiteSpace: 'nowrap',
          fontSize: 12,
          fontWeight: 600,
          color: isDeprecated ? tokens.colorTextSecondary : tokens.colorCharcoal,
          textDecoration: isDeprecated ? 'line-through' : 'none',
        }}
      >
        {displayName}
      </Typography>
      {isCertified && (
        <Box component="span" sx={{
          flexShrink: 0,
          height: 14,
          px: 0.625,
          display: 'inline-flex',
          alignItems: 'center',
          borderRadius: 7,
          bgcolor: 'rgba(46,125,50,0.08)',
          color: '#2e7d32',
          fontSize: 9,
          fontWeight: 600,
          whiteSpace: 'nowrap',
        }}>
          {strings.kpiCard.chipCertified}
        </Box>
      )}
      {isDeprecated && (
        <Box component="span" sx={{
          flexShrink: 0,
          height: 14,
          px: 0.625,
          display: 'inline-flex',
          alignItems: 'center',
          borderRadius: 7,
          bgcolor: 'rgba(237,108,2,0.08)',
          color: '#ed6c02',
          fontSize: 9,
          fontWeight: 600,
          whiteSpace: 'nowrap',
        }}>
          {strings.kpiCard.chipDeprecated}
        </Box>
      )}
      {hasValue ? (
        <Typography sx={{ fontSize: 12, fontWeight: 700, color: statusColor, flexShrink: 0, whiteSpace: 'nowrap' }}>
          {result?.formatted_value}
        </Typography>
      ) : <Typography sx={{ fontSize: 11, color: '#9a9a9a' }}>{strings.kpiPanel.notEvaluated}</Typography>}
      {result?.formatted_goal && (
        <Typography sx={{ fontSize: 10, color: tokens.colorTextSecondary, flexShrink: 0, whiteSpace: 'nowrap' }}>
          / {result.formatted_goal}
        </Typography>
      )}
      {trendArrow && (
        <Typography sx={{ fontSize: 13, fontWeight: 700, color: trendColor, flexShrink: 0, lineHeight: 1 }}>
          {trendArrow}
        </Typography>
      )}
      <Box sx={{ display: 'flex', alignItems: 'center', gap: 0.125, ml: 0.25, flexShrink: 0 }}>
        <IconButton
          size="small"
          onClick={onInsertTable}
          title={strings.kpiPanel.insertTableTitle}
          aria-label={strings.kpiPanel.insertTableTitle}
          sx={{ width: 22, height: 22, color: tokens.colorTextSecondary, borderRadius: 0.5, '&:hover': { color: tokens.colorPrimary, bgcolor: tokens.colorPrimaryBg } }}
        >
          <TableChartOutlined sx={{ fontSize: 15 }} />
        </IconButton>
        <Box component="span" title={chartTitle} sx={{ display: 'inline-flex' }}>
          <IconButton
            size="small"
            onClick={chartAvailable ? onInsertChart : undefined}
            disabled={!chartAvailable}
            title={chartTitle}
            aria-label={chartTitle}
            sx={{
              width: 22,
              height: 22,
              color: tokens.colorTextSecondary,
              borderRadius: 0.5,
              '&:hover': chartAvailable ? { color: tokens.colorPrimary, bgcolor: tokens.colorPrimaryBg } : {},
              '&.Mui-disabled': { color: '#c4c4c4' },
            }}
          >
            <BarChartOutlined sx={{ fontSize: 15 }} />
          </IconButton>
        </Box>
      </Box>
    </Box>
  );
}
