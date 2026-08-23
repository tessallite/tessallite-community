import { useState, useEffect, useMemo, useCallback, useRef } from 'react';
import {
  Box, Typography, Chip, Skeleton, TextField, InputAdornment, IconButton,
} from '@mui/material';
import {
  SearchOutlined,
  TableChartOutlined,
  BarChartOutlined,
  DashboardOutlined,
  RefreshOutlined,
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

  const filteredKpis = useMemo(() => {
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

  if (loading) {
    return (
      <Box sx={{ p: 2, display: 'flex', flexDirection: 'column', gap: 1.5 }}>
        <Skeleton variant="rectangular" height={32} sx={{ borderRadius: 1 }} />
        <Skeleton variant="rectangular" height={80} sx={{ borderRadius: 1 }} />
        <Skeleton variant="rectangular" height={80} sx={{ borderRadius: 1 }} />
        <Skeleton variant="rectangular" height={80} sx={{ borderRadius: 1 }} />
      </Box>
    );
  }

  if (error) {
    return (
      <Box sx={{ p: 3, textAlign: 'center' }}>
        <Typography sx={{ fontSize: 13, color: tokens.colorRed, mb: 1 }}>{error}</Typography>
        <Box
          component="button"
          onClick={fetchData}
          sx={{ fontSize: 12, px: 2, py: 0.75, borderRadius: 1, cursor: 'pointer', border: `1px solid ${tokens.colorPrimary}`, color: tokens.colorPrimary, bgcolor: 'transparent' }}
        >
          {strings.kpiPanel.tryAgain}
        </Box>
      </Box>
    );
  }

  if (kpis.length === 0) {
    return (
      <Box sx={{ p: 3, textAlign: 'center' }}>
        <Typography sx={{ fontSize: 14, fontWeight: 600, color: tokens.colorCharcoal, mb: 0.5 }}>
          {strings.kpiPanel.noKpisTitle}
        </Typography>
        <Typography sx={{ fontSize: 12, color: tokens.colorTextSecondary }}>
          {strings.kpiPanel.noKpisDescription}
        </Typography>
      </Box>
    );
  }

  return (
    <Box sx={{ flex: 1, display: 'flex', flexDirection: 'column', overflow: 'hidden' }}>
      {/* Header */}
      <Box sx={{ px: 1.5, py: 1, borderBottom: `1px solid ${tokens.colorBorderLight}` }}>
        <Box sx={{ display: 'flex', alignItems: 'center', gap: 1, mb: 0.75 }}>
          <Typography sx={{ fontSize: 13, fontWeight: 700, color: tokens.colorCharcoal, flex: 1 }}>
            {templates.kpiPanel.kpiCount(kpis.length)}
          </Typography>
          <IconButton size="small" onClick={fetchData} title={strings.kpiPanel.refreshKpis} sx={{ width: 26, height: 26 }}>
            <RefreshOutlined sx={{ fontSize: 15 }} />
          </IconButton>
          {kpis.length > 0 && (
            <IconButton
              size="small"
              onClick={handleInsertAllScorecard}
              title={strings.kpiPanel.insertAllScorecard}
              sx={{ width: 26, height: 26, color: tokens.colorGoldDark }}
            >
              <DashboardOutlined sx={{ fontSize: 15 }} />
            </IconButton>
          )}
        </Box>
        <Box sx={{ display: 'flex', alignItems: 'center', gap: 0.75, flexWrap: 'wrap' }}>
          {statusCounts.good > 0 && (
            <Chip label={templates.kpiPanel.statusCount(statusCounts.good, strings.kpiPanel.good)} size="small" sx={{ fontSize: 10, height: 18, bgcolor: 'rgba(46,125,50,0.08)', color: '#2e7d32', fontWeight: 600 }} />
          )}
          {statusCounts.warning > 0 && (
            <Chip label={templates.kpiPanel.statusCount(statusCounts.warning, strings.kpiPanel.warning)} size="small" sx={{ fontSize: 10, height: 18, bgcolor: 'rgba(237,108,2,0.08)', color: '#ed6c02', fontWeight: 600 }} />
          )}
          {statusCounts.poor > 0 && (
            <Chip label={templates.kpiPanel.statusCount(statusCounts.poor, strings.kpiPanel.poor)} size="small" sx={{ fontSize: 10, height: 18, bgcolor: 'rgba(211,47,47,0.08)', color: '#d32f2f', fontWeight: 600 }} />
          )}
        </Box>
      </Box>

      {/* Evaluation error banner — list still renders, values unavailable (F-025-03) */}
      {evalError && (
        <Box sx={{ px: 1.5, py: 0.75, borderBottom: `1px solid ${tokens.colorBorderLight}`, bgcolor: 'rgba(237,108,2,0.06)' }}>
          <Typography sx={{ fontSize: 11, color: '#ed6c02' }}>
            {evalError}
          </Typography>
        </Box>
      )}

      {/* Filter bar */}
      <Box sx={{ px: 1.5, py: 0.75, display: 'flex', alignItems: 'center', gap: 0.75, borderBottom: `1px solid ${tokens.colorBorderLight}` }}>
        {(['all', 'certified'] as const).map(f => (
          <Chip
            key={f}
            label={f === 'all' ? strings.kpiPanel.filterAll : strings.kpiPanel.filterCertified}
            size="small"
            variant={filter === f ? 'filled' : 'outlined'}
            onClick={() => setFilter(f)}
            sx={{
              fontSize: 10, height: 20, fontWeight: 600, cursor: 'pointer',
              ...(filter === f ? { bgcolor: tokens.colorPrimary, color: '#fff' } : { color: tokens.colorTextSecondary }),
            }}
          />
        ))}
        <TextField
          size="small"
          placeholder={strings.kpiPanel.searchPlaceholder}
          value={search}
          onChange={(e) => handleSearchChange(e.target.value)}
          InputProps={{
            startAdornment: (
              <InputAdornment position="start">
                <SearchOutlined sx={{ fontSize: 14, color: tokens.colorTextSecondary }} />
              </InputAdornment>
            ),
            sx: { fontSize: 11, height: 24, px: 0.75 },
          }}
          sx={{ flex: 1, '& .MuiOutlinedInput-root': { borderRadius: 1 } }}
        />
      </Box>

      {/* KPI list */}
      <Box sx={{ flex: 1, overflow: 'auto', px: 1, py: 0.5 }}>
        {filteredKpis.length === 0 ? (
          <Typography sx={{ fontSize: 12, color: tokens.colorTextSecondary, textAlign: 'center', py: 3 }}>
            {strings.kpiPanel.noSearchMatch}
          </Typography>
        ) : (
          grouped.map((group, gi) => (
            <Box key={gi} sx={{ mb: 1 }}>
              {group.folder && (
                <Typography sx={{ fontSize: 10, fontWeight: 700, color: tokens.colorTextSecondary, textTransform: 'uppercase', px: 0.5, py: 0.5 }}>
                  {group.folder}
                </Typography>
              )}
              {group.kpis.map(kpi => (
                <KpiRow
                  key={kpi.id}
                  kpi={kpi}
                  result={results.get(kpi.id)}
                  onInsertTable={() => handleInsertTable(kpi)}
                  onInsertChart={() => handleInsertChart(kpi)}
                />
              ))}
            </Box>
          ))
        )}
      </Box>
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
  const statusColor = result?.status != null ? STATUS_COLORS[result.status] ?? tokens.colorTextSecondary : tokens.colorTextSecondary;
  const trendArrow = result?.trend_label ? TREND_ARROWS[result.trend_label.toLowerCase()] ?? '' : '';
  const trendColor = result?.trend_label ? TREND_COLORS[result.trend_label.toLowerCase()] ?? tokens.colorTextSecondary : tokens.colorTextSecondary;
  const chartAvailable = result != null && result.value != null && result.goal != null;

  return (
    <Box sx={{
      border: `1px solid ${tokens.colorBorderLight}`,
      borderRadius: 1.5, mb: 0.75, p: 1, bgcolor: tokens.colorWhite,
      '&:hover': { borderColor: tokens.colorPrimary, boxShadow: '0 1px 4px rgba(0,0,0,0.06)' },
    }}>
      {/* Top row: name + value + trend */}
      <Box sx={{ display: 'flex', alignItems: 'center', gap: 0.75, mb: 0.5 }}>
        <Box sx={{ width: 8, height: 8, borderRadius: '50%', bgcolor: statusColor, flexShrink: 0 }} />
        <Typography sx={{ fontSize: 12, fontWeight: 600, color: tokens.colorCharcoal, flex: 1, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
          {kpi.display_name || kpi.name}
        </Typography>
        {result?.formatted_value && (
          <Typography sx={{ fontSize: 12, fontWeight: 700, color: statusColor, flexShrink: 0 }}>
            {result.formatted_value}
          </Typography>
        )}
        {trendArrow && (
          <Typography sx={{ fontSize: 14, fontWeight: 700, color: trendColor, flexShrink: 0, lineHeight: 1 }}>
            {trendArrow}
          </Typography>
        )}
      </Box>

      {/* Target line */}
      {result?.formatted_goal && (
        <Typography sx={{ fontSize: 10, color: tokens.colorTextSecondary, pl: 1.75, mb: 0.5 }}>
          {strings.kpiPanel.targetPrefix} {result.formatted_goal}
        </Typography>
      )}

      {/* Action buttons */}
      <Box sx={{ display: 'flex', gap: 0.75, pl: 1.75 }}>
        <Box
          component="button"
          onClick={onInsertTable}
          title={strings.kpiPanel.insertTableTitle}
          sx={{
            display: 'flex', alignItems: 'center', gap: 0.4,
            fontSize: 10, fontWeight: 600, px: 0.75, py: 0.35,
            borderRadius: 0.75, cursor: 'pointer',
            border: `1px solid ${tokens.colorBorderLight}`, bgcolor: 'transparent',
            color: tokens.colorTextSecondary,
            '&:hover': { bgcolor: tokens.colorPrimaryBg, color: tokens.colorPrimary, borderColor: tokens.colorPrimary },
          }}
        >
          <TableChartOutlined sx={{ fontSize: 12 }} />
          {strings.kpiPanel.insertTable}
        </Box>
        <Box
          component="button"
          onClick={chartAvailable ? onInsertChart : undefined}
          disabled={!chartAvailable}
          title={chartAvailable ? strings.kpiPanel.insertChartTitle : strings.kpiPanel.evalError}
          sx={{
            display: 'flex', alignItems: 'center', gap: 0.4,
            fontSize: 10, fontWeight: 600, px: 0.75, py: 0.35,
            borderRadius: 0.75,
            cursor: chartAvailable ? 'pointer' : 'not-allowed',
            border: `1px solid ${tokens.colorBorderLight}`, bgcolor: 'transparent',
            color: tokens.colorTextSecondary,
            opacity: chartAvailable ? 1 : 0.4,
            '&:hover': chartAvailable ? { bgcolor: tokens.colorPrimaryBg, color: tokens.colorPrimary, borderColor: tokens.colorPrimary } : {},
          }}
        >
          <BarChartOutlined sx={{ fontSize: 12 }} />
          {strings.kpiPanel.insertChart}
        </Box>
      </Box>
    </Box>
  );
}
