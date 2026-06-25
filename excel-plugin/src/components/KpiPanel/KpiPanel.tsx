import { useState, useEffect, useMemo, useCallback } from 'react';
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
import { getKpis, evaluateKpiBatch, getMeasures } from '../../api/modelService';
import { buildScorecardKpi } from '../../utils/kpiScorecard';
import type { Kpi, KpiBatchResult, Measure } from '../../types/tessallite';

interface KpiPanelProps {
  projectId: string;
  modelId: string;
  personaId?: string | null;
  onInsertTable: (headers: string[], rows: (string | number)[][]) => Promise<string | null>;
  onInsertChart: (headers: string[], rows: (string | number)[][]) => Promise<string | null>;
  onInsertScorecard: (
    kpis: { id: string; name: string; display_name: string | null; valueMeasureName: string | null; goalMeasureName: string | null; goalLiteral?: number | null; updated_at?: string }[],
    connectionName: string,
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

export default function KpiPanel({
  projectId, modelId, personaId, onInsertTable, onInsertChart, onInsertScorecard, connectionName,
}: KpiPanelProps) {
  const [kpis, setKpis] = useState<Kpi[]>([]);
  // F-025-10: the scorecard's Value/Goal columns are CUBEVALUE formulas over
  // the KPI's value/goal MEASURES — so we must resolve measure id -> technical
  // name. Without this the scorecard wrote blank Value/Goal cells.
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
        getKpis(projectId, modelId),
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
          setEvalError('KPI values could not be evaluated right now. The list below is current; try refreshing.');
        }
      }
      setResults(map);
    } catch (e) {
      setError('Could not load KPIs. Check your connection.');
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
    if (search.trim()) {
      const q = search.toLowerCase();
      list = list.filter(k =>
        (k.display_name || k.name).toLowerCase().includes(q) ||
        (k.description || '').toLowerCase().includes(q),
      );
    }
    return list;
  }, [kpis, filter, search]);

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
    const headers = ['KPI', 'Value', 'Goal', 'Status', 'Trend'];
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
    const value = r?.value ?? 0;
    const goal = r?.goal ?? 0;
    const headers = ['Metric', kpi.display_name || kpi.name];
    const rows: (string | number)[][] = [
      ['Current', value],
      ['Target', goal],
    ];
    await onInsertChart(headers, rows);
  }, [results, onInsertChart]);

  const handleInsertAllScorecard = useCallback(async () => {
    // F-025-10: resolve each KPI's value/goal measure id to its technical name
    // (the scorecard CUBEVALUE binds by technical measure name) and carry the
    // static target literal when a KPI has no goal measure.
    const payload = kpis.map(k => buildScorecardKpi(k, measuresById));
    await onInsertScorecard(payload, connectionName);
  }, [kpis, measuresById, onInsertScorecard, connectionName]);

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
          Try again
        </Box>
      </Box>
    );
  }

  if (kpis.length === 0) {
    return (
      <Box sx={{ p: 3, textAlign: 'center' }}>
        <Typography sx={{ fontSize: 14, fontWeight: 600, color: tokens.colorCharcoal, mb: 0.5 }}>
          No KPIs yet
        </Typography>
        <Typography sx={{ fontSize: 12, color: tokens.colorTextSecondary }}>
          Create KPIs in the Tessallite web app, then come back here to view and insert them.
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
            KPIs ({kpis.length})
          </Typography>
          <IconButton size="small" onClick={fetchData} title="Refresh KPIs" sx={{ width: 26, height: 26 }}>
            <RefreshOutlined sx={{ fontSize: 15 }} />
          </IconButton>
          {kpis.length > 0 && (
            <IconButton
              size="small"
              onClick={handleInsertAllScorecard}
              title="Insert all KPIs as a scorecard table"
              sx={{ width: 26, height: 26, color: tokens.colorGoldDark }}
            >
              <DashboardOutlined sx={{ fontSize: 15 }} />
            </IconButton>
          )}
        </Box>
        <Box sx={{ display: 'flex', alignItems: 'center', gap: 0.75, flexWrap: 'wrap' }}>
          {statusCounts.good > 0 && (
            <Chip label={`${statusCounts.good} Good`} size="small" sx={{ fontSize: 10, height: 18, bgcolor: 'rgba(46,125,50,0.08)', color: '#2e7d32', fontWeight: 600 }} />
          )}
          {statusCounts.warning > 0 && (
            <Chip label={`${statusCounts.warning} Warning`} size="small" sx={{ fontSize: 10, height: 18, bgcolor: 'rgba(237,108,2,0.08)', color: '#ed6c02', fontWeight: 600 }} />
          )}
          {statusCounts.poor > 0 && (
            <Chip label={`${statusCounts.poor} Poor`} size="small" sx={{ fontSize: 10, height: 18, bgcolor: 'rgba(211,47,47,0.08)', color: '#d32f2f', fontWeight: 600 }} />
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
            label={f === 'all' ? 'All' : 'Certified'}
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
          placeholder="Search..."
          value={search}
          onChange={(e) => setSearch(e.target.value)}
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
            No KPIs match your search
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
          <Typography sx={{ fontSize: 14, fontWeight: 700, color: statusColor, flexShrink: 0, lineHeight: 1 }}>
            {trendArrow}
          </Typography>
        )}
      </Box>

      {/* Target line */}
      {result?.formatted_goal && (
        <Typography sx={{ fontSize: 10, color: tokens.colorTextSecondary, pl: 1.75, mb: 0.5 }}>
          Target: {result.formatted_goal}
        </Typography>
      )}

      {/* Action buttons */}
      <Box sx={{ display: 'flex', gap: 0.75, pl: 1.75 }}>
        <Box
          component="button"
          onClick={onInsertTable}
          title="Insert this KPI as a mini-table in the worksheet"
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
          Insert Table
        </Box>
        <Box
          component="button"
          onClick={onInsertChart}
          title="Insert this KPI as a chart comparing value vs target"
          sx={{
            display: 'flex', alignItems: 'center', gap: 0.4,
            fontSize: 10, fontWeight: 600, px: 0.75, py: 0.35,
            borderRadius: 0.75, cursor: 'pointer',
            border: `1px solid ${tokens.colorBorderLight}`, bgcolor: 'transparent',
            color: tokens.colorTextSecondary,
            '&:hover': { bgcolor: tokens.colorPrimaryBg, color: tokens.colorPrimary, borderColor: tokens.colorPrimary },
          }}
        >
          <BarChartOutlined sx={{ fontSize: 12 }} />
          Insert Chart
        </Box>
      </Box>
    </Box>
  );
}
