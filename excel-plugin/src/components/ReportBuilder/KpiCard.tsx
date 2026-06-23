import { useState, useCallback } from 'react';
import { Box, Typography, Chip, CircularProgress, Collapse, IconButton } from '@mui/material';
import {
  Add as AddIcon,
  Functions as FunctionsIcon,
  ExpandMore as ExpandMoreIcon,
  InfoOutlined,
  KeyboardArrowUpOutlined,
} from '@mui/icons-material';
import { tokens } from '../../theme';
import type { Kpi, KpiEvaluateResponse, Measure } from '../../types/tessallite';
import { evaluateKpi } from '../../api/modelService';

export type KpiInsertMode =
  | 'full_row'
  | 'value_only'
  | 'value_goal'
  | 'status_only'
  | 'trend_only'
  | 'kpi_card'
  | 'formula_ref';

interface KpiCardProps {
  kpi: Kpi;
  measures: Measure[];
  projectId: string;
  modelId: string;
  checked: boolean;
  onToggle: () => void;
  onAddToValues: () => void;
  onInsertAsFormulas?: () => void;
  onInsertKpi?: (mode: KpiInsertMode) => void;
}

const graphicIcons: Record<string, string> = {
  'Traffic Light': '\u{1F6A6}',
  'Standard Arrow': '^',
  Gauge: '\u{1F4CA}',
  Thermometer: '\u{1F321}',
  Cylinder: '\u{1F4CA}',
  'Smiley Face': '\u{1F600}',
};

const statusColors: Record<number, string> = {
  1: '#2e7d32',
  0: '#ed6c02',
  [-1]: '#d32f2f',
};

const INSERT_OPTIONS: { mode: KpiInsertMode; label: string; description: string }[] = [
  { mode: 'full_row', label: 'Insert Full KPI Row', description: 'Name | Value | Goal | Status | Trend' },
  { mode: 'value_only', label: 'Insert Value Only', description: 'Single cell with KPI value' },
  { mode: 'value_goal', label: 'Insert Value + Goal', description: 'Two cells: value and goal' },
  { mode: 'status_only', label: 'Insert Status Icon', description: 'Traffic light status indicator' },
  { mode: 'trend_only', label: 'Insert Trend Icon', description: 'Trend arrow indicator' },
  { mode: 'kpi_card', label: 'Insert KPI Card', description: 'Formatted card with all details' },
  { mode: 'formula_ref', label: 'Insert Formula Reference', description: 'CUBEKPIMEMBER formula' },
];

export default function KpiCard({
  kpi,
  measures,
  projectId,
  modelId,
  checked,
  onToggle,
  onAddToValues,
  onInsertAsFormulas,
  onInsertKpi,
}: KpiCardProps) {
  const [evalData, setEvalData] = useState<KpiEvaluateResponse | null>(null);
  const [evalLoading, setEvalLoading] = useState(false);
  const [evalRequested, setEvalRequested] = useState(false);
  const [insertOpen, setInsertOpen] = useState(false);
  const [detailsOpen, setDetailsOpen] = useState(false);

  const fetchEvaluation = useCallback(() => {
    if (evalRequested) return;
    setEvalRequested(true);
    setEvalLoading(true);
    evaluateKpi(projectId, modelId, kpi.id)
      .then((result) => setEvalData(result))
      .catch(() => setEvalData(null))
      .finally(() => setEvalLoading(false));
  }, [evalRequested, projectId, modelId, kpi.id]);

  const valueMeasure = measures.find(m => m.id === kpi.value_measure_id);
  const goalMeasure = measures.find(m => m.id === kpi.goal_measure_id);
  const icon = graphicIcons[kpi.status_graphic] || '\u{1F4CA}';

  const statusColor = evalData?.status !== null && evalData?.status !== undefined
    ? statusColors[evalData.status] ?? tokens.colorTextSecondary
    : undefined;

  const detailRows = [
    kpi.description && { label: 'Description', value: kpi.description },
    valueMeasure && { label: 'Value', value: valueMeasure.display_name },
    goalMeasure && { label: 'Goal', value: goalMeasure.display_name },
    evalData?.formatted_value && { label: 'Current', value: evalData.formatted_value },
    evalData?.formatted_goal && { label: 'Target', value: evalData.formatted_goal },
    evalData?.status_label && { label: 'Status', value: evalData.status_label },
    evalData?.trend_label && { label: 'Trend', value: evalData.trend_label },
    { label: 'Graphic', value: kpi.status_graphic },
    kpi.weight !== null && { label: 'Weight', value: String(kpi.weight) },
    kpi.display_folder && { label: 'Folder', value: kpi.display_folder },
    kpi.certification_status !== 'draft' && { label: 'Certification', value: kpi.certification_status },
  ].filter(Boolean) as { label: string; value: string }[];

  const handleInsertToggle = (e: React.MouseEvent) => {
    e.stopPropagation();
    const willOpen = !insertOpen;
    setInsertOpen(willOpen);
    if (willOpen) fetchEvaluation();
  };

  return (
    <>
      <Box
        onClick={() => (checked ? onToggle() : onAddToValues())}
        sx={{
          display: 'flex', alignItems: 'center', gap: 0.75,
          px: 1.25, py: 0.5, minHeight: 36, cursor: 'pointer',
          borderBottom: `1px solid ${tokens.colorBorderLight}`,
          bgcolor: checked ? tokens.colorPrimaryBg : 'transparent',
          '&:hover': { bgcolor: checked ? tokens.colorPrimaryBg : tokens.colorSubtleFill },
        }}
      >
        <Typography sx={{ fontSize: 14, lineHeight: 1, flexShrink: 0 }}>{icon}</Typography>
        <Box sx={{ flex: 1, overflow: 'hidden' }}>
          <Typography
            sx={{
              fontSize: 13,
              fontWeight: checked ? 600 : 400,
              color: kpi.certification_status === 'deprecated' ? tokens.colorTextSecondary : checked ? tokens.colorPrimary : tokens.colorCharcoal,
              textDecoration: kpi.certification_status === 'deprecated' ? 'line-through' : 'none',
              overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap',
            }}
          >
            {kpi.display_name || kpi.name}
          </Typography>
          <Box sx={{ display: 'flex', alignItems: 'center', gap: 0.5 }}>
            {valueMeasure && (
              <Typography sx={{ fontSize: 10, color: tokens.colorTextSecondary, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                {valueMeasure.display_name}{goalMeasure ? ` vs ${goalMeasure.display_name}` : ''}
              </Typography>
            )}
            {evalLoading && <CircularProgress size={8} sx={{ ml: 0.5 }} />}
            {!evalLoading && evalData?.status_label && (
              <Typography sx={{ fontSize: 9, fontWeight: 600, color: statusColor, flexShrink: 0 }}>
                {evalData.status_label}
              </Typography>
            )}
          </Box>
        </Box>
        <Chip
          label="KPI"
          size="small"
          sx={{ fontSize: 9, height: 16, bgcolor: tokens.colorGoldBg, color: tokens.colorGoldDark, fontWeight: 600, flexShrink: 0 }}
        />
        {kpi.certification_status === 'certified' && (
          <Chip
            label="Certified"
            size="small"
            sx={{ fontSize: 9, height: 16, bgcolor: 'rgba(46,125,50,0.08)', color: '#2e7d32', fontWeight: 600, flexShrink: 0 }}
          />
        )}
        {kpi.certification_status === 'deprecated' && (
          <Chip
            label="Deprecated"
            size="small"
            sx={{ fontSize: 9, height: 16, bgcolor: 'rgba(237,108,2,0.08)', color: '#ed6c02', fontWeight: 600, flexShrink: 0 }}
          />
        )}
        <IconButton
          size="small"
          onClick={(e) => {
            e.stopPropagation();
            setDetailsOpen(v => !v);
            if (!detailsOpen) fetchEvaluation();
          }}
          title={detailsOpen ? 'Hide details' : 'Show details'}
          aria-label={`${detailsOpen ? 'Hide' : 'Show'} details for ${kpi.display_name || kpi.name}`}
          sx={{
            width: 28,
            height: 28,
            color: detailsOpen ? tokens.colorPrimary : tokens.colorTextSecondary,
            '&:hover': { bgcolor: tokens.colorSubtleFill },
          }}
        >
          {detailsOpen ? <KeyboardArrowUpOutlined sx={{ fontSize: 16 }} /> : <InfoOutlined sx={{ fontSize: 15 }} />}
        </IconButton>
        {!checked && (
          <Box sx={{ display: 'flex', alignItems: 'center', gap: 0.25, flexShrink: 0 }}>
            {onInsertKpi && (
              <Box
                component="span"
                onClick={handleInsertToggle}
                title="Insert options"
                sx={{
                  display: 'flex', alignItems: 'center',
                  p: '2px', borderRadius: 0.5, color: tokens.colorGoldDark,
                  transform: insertOpen ? 'rotate(180deg)' : 'none',
                  transition: 'transform 0.2s',
                  '&:hover': { bgcolor: tokens.colorGoldBg },
                }}
              >
                <ExpandMoreIcon sx={{ fontSize: 14 }} />
              </Box>
            )}
            {onInsertAsFormulas && !onInsertKpi && (
              <Box
                component="span"
                onClick={(e) => { e.stopPropagation(); onInsertAsFormulas(); }}
                title="Insert as CUBE formulas"
                sx={{
                  display: 'flex', alignItems: 'center',
                  p: '2px', borderRadius: 0.5, color: tokens.colorGoldDark,
                  '&:hover': { bgcolor: tokens.colorGoldBg },
                }}
              >
                <FunctionsIcon sx={{ fontSize: 14 }} />
              </Box>
            )}
            <Box
              component="span"
              onClick={(e) => { e.stopPropagation(); onAddToValues(); }}
              title="Add KPI value to report"
              sx={{
                display: 'flex', alignItems: 'center',
                p: '2px', borderRadius: 0.5, color: tokens.colorPrimary,
                '&:hover': { bgcolor: tokens.colorPrimaryBg },
              }}
            >
              <AddIcon sx={{ fontSize: 14 }} />
            </Box>
          </Box>
        )}
      </Box>

      <Collapse in={detailsOpen}>
        <Box sx={{ px: 2, py: 1, bgcolor: tokens.colorSubtleFill, borderBottom: `1px solid ${tokens.colorBorderLight}` }}>
          {evalLoading && (
            <Typography sx={{ fontSize: 11, color: tokens.colorTextSecondary, mb: 0.75 }}>
              Loading KPI status...
            </Typography>
          )}
          {detailRows.map(row => (
            <Box key={row.label} sx={{ mb: 0.6 }}>
              <Typography sx={{ fontSize: 10, fontWeight: 700, color: tokens.colorTextSecondary, textTransform: 'uppercase', lineHeight: 1.2 }}>
                {row.label}
              </Typography>
              <Typography sx={{ fontSize: 11.5, color: tokens.colorCharcoal, lineHeight: 1.35 }}>
                {row.value}
              </Typography>
            </Box>
          ))}
        </Box>
      </Collapse>

      {onInsertKpi && (
        <Collapse in={insertOpen}>
          <Box sx={{ px: 2, py: 0.5, bgcolor: tokens.colorSubtleFill, borderBottom: `1px solid ${tokens.colorBorderLight}` }}>
            <Typography sx={{ fontSize: 10, fontWeight: 600, color: tokens.colorTextSecondary, mb: 0.25, textTransform: 'uppercase' }}>
              Insert Options
            </Typography>
            {INSERT_OPTIONS.map(opt => (
              <Box
                key={opt.mode}
                component="button"
                onClick={(e: React.MouseEvent) => {
                  e.stopPropagation();
                  onInsertKpi(opt.mode);
                  setInsertOpen(false);
                }}
                sx={{
                  display: 'block', width: '100%', textAlign: 'left',
                  fontSize: 11, px: 0.75, py: 0.375, mb: 0.25,
                  borderRadius: 0.5, cursor: 'pointer',
                  border: 'none', bgcolor: 'transparent',
                  color: tokens.colorCharcoal,
                  '&:hover': { bgcolor: tokens.colorPrimaryBg, color: tokens.colorPrimary },
                }}
              >
                <Box sx={{ fontWeight: 500 }}>{opt.label}</Box>
                <Box sx={{ fontSize: 9, color: tokens.colorTextSecondary }}>{opt.description}</Box>
              </Box>
            ))}
          </Box>
        </Collapse>
      )}
    </>
  );
}
