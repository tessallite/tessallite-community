import { useMemo } from 'react';
import { Box, Typography, Collapse, Skeleton } from '@mui/material';
import { ExpandLess, ExpandMore, Dashboard as DashboardIcon } from '@mui/icons-material';
import { tokens } from '../../theme';
import type { Kpi, Measure } from '../../types/tessallite';
import KpiCard, { type KpiInsertMode } from './KpiCard';

interface KpiLibraryProps {
  kpis: Kpi[];
  measures: Measure[];
  projectId: string;
  modelId: string;
  searchQuery: string;
  selectedKpiValueMeasureIds: string[];
  onToggleKpi: (kpi: Kpi) => void;
  onAddKpiToValues: (kpi: Kpi) => void;
  onInsertKpiAsFormulas?: (kpi: Kpi) => void;
  onInsertKpi?: (kpi: Kpi, mode: KpiInsertMode) => void;
  onInsertScorecard?: () => void;
  expanded: boolean;
  onToggleExpanded: () => void;
  loading?: boolean;
}

export default function KpiLibrary({
  kpis,
  measures,
  projectId,
  modelId,
  searchQuery,
  selectedKpiValueMeasureIds,
  onToggleKpi,
  onAddKpiToValues,
  onInsertKpiAsFormulas,
  onInsertKpi,
  onInsertScorecard,
  expanded,
  onToggleExpanded,
  loading,
}: KpiLibraryProps) {
  const groupedKpis = useMemo(() => {
    const grouped = new Map<string, Kpi[]>();
    const ungrouped: Kpi[] = [];

    for (const kpi of kpis) {
      if (kpi.display_folder) {
        const group = grouped.get(kpi.display_folder) || [];
        group.push(kpi);
        grouped.set(kpi.display_folder, group);
      } else {
        ungrouped.push(kpi);
      }
    }

    const result: { folder?: string; kpis: Kpi[] }[] = [];
    for (const [folder, list] of grouped) {
      result.push({ folder, kpis: list });
    }
    if (ungrouped.length > 0) {
      result.push({ kpis: ungrouped });
    }
    return result;
  }, [kpis]);

  return (
    <>
      <Box
        sx={{
          display: 'flex', alignItems: 'center', px: 1.5, py: 0.75,
          cursor: kpis.length > 0 || loading ? 'pointer' : 'default',
          borderTop: `1px solid ${tokens.colorBorderLight}`,
          opacity: kpis.length > 0 || loading ? 1 : 0.68,
        }}
        onClick={() => {
          if (kpis.length > 0 || loading || searchQuery) onToggleExpanded();
        }}
      >
        <Typography sx={{ fontSize: 12, fontWeight: 700, color: tokens.colorCharcoal, flex: 1 }}>
          KPIs ({kpis.length})
        </Typography>
        {onInsertScorecard && kpis.length > 0 && (
          <Box
            component="span"
            onClick={(e: React.MouseEvent) => { e.stopPropagation(); onInsertScorecard(); }}
            title="Insert KPI Scorecard"
            sx={{
              display: 'flex', alignItems: 'center', mr: 0.5,
              p: '2px', borderRadius: 0.5, color: tokens.colorGoldDark,
              '&:hover': { bgcolor: tokens.colorGoldBg },
            }}
          >
            <DashboardIcon sx={{ fontSize: 14 }} />
          </Box>
        )}
        {(kpis.length > 0 || loading || searchQuery) && (
          expanded ? <ExpandLess sx={{ fontSize: 16, color: tokens.colorTextSecondary }} /> : <ExpandMore sx={{ fontSize: 16, color: tokens.colorTextSecondary }} />
        )}
      </Box>
      <Collapse in={expanded}>
        {loading ? (
          <Box sx={{ p: 1 }}>
            <Skeleton variant="rectangular" height={48} sx={{ mb: 0.5, borderRadius: 1 }} />
            <Skeleton variant="rectangular" height={48} sx={{ mb: 0.5, borderRadius: 1 }} />
          </Box>
        ) : kpis.length === 0 ? (
          <Typography sx={{ fontSize: 11, color: tokens.colorTextSecondary, px: 1.5, py: 1 }}>
            {searchQuery ? 'No KPIs match your search' : 'No KPIs available'}
          </Typography>
        ) : (
          groupedKpis.map((group, gi) => (
            <Box key={gi}>
              {group.folder && (
                <Typography sx={{
                  fontSize: 10, fontWeight: 600, color: tokens.colorTextSecondary,
                  px: 1.5, py: 0.5, textTransform: 'uppercase', bgcolor: tokens.colorSubtleFill,
                }}>
                  {group.folder}
                </Typography>
              )}
              {group.kpis.map(kpi => (
                <KpiCard
                  key={kpi.id}
                  kpi={kpi}
                  measures={measures}
                  projectId={projectId}
                  modelId={modelId}
                  checked={kpi.value_measure_id ? selectedKpiValueMeasureIds.includes(kpi.value_measure_id) : false}
                  onToggle={() => onToggleKpi(kpi)}
                  onAddToValues={() => onAddKpiToValues(kpi)}
                  onInsertAsFormulas={onInsertKpiAsFormulas ? () => onInsertKpiAsFormulas(kpi) : undefined}
                  onInsertKpi={onInsertKpi ? (mode) => onInsertKpi(kpi, mode) : undefined}
                />
              ))}
            </Box>
          ))
        )}
      </Collapse>
    </>
  );
}
