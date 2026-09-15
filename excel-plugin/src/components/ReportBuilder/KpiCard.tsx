import { useState, useCallback, useEffect, useRef } from 'react';
import type { SvgIconComponent } from '@mui/icons-material';
import { Box, Typography, Chip, CircularProgress, Collapse, IconButton, Menu, MenuItem, ListItemText } from '@mui/material';
import {
  Add as AddIcon,
  Remove as RemoveIcon,
  Functions as FunctionsIcon,
  MoreHorizOutlined,
  InfoOutlined,
  KeyboardArrowUpOutlined,
  TrafficOutlined,
  TrendingFlatOutlined,
  SpeedOutlined,
  DeviceThermostatOutlined,
  BarChartOutlined,
  SentimentSatisfiedOutlined,
  AssessmentOutlined,
} from '@mui/icons-material';
import { tokens } from '../../theme';
import type { Kpi, KpiEvaluateResponse, Measure } from '../../types/tessallite';
import { evaluateKpi } from '../../api/modelService';
import { strings, templates } from '../../i18n/strings';

export type KpiInsertMode =
  | 'full_row'
  | 'value_only'
  | 'value_goal'
  | 'status_only'
  | 'kpi_card'
  | 'formula_ref';

interface KpiCardProps {
  kpi: Kpi;
  measures: Measure[];
  projectId: string;
  modelId: string;
  personaId?: string | null;
  checked: boolean;
  onToggle: () => void;
  onAddToValues: () => void;
  onInsertAsFormulas?: () => void;
  onInsertKpi?: (mode: KpiInsertMode) => void;
}

// Bug-6368: the status graphic is chosen for the KPI in the model; render it
// with an app-consistent outlined icon rather than an emoji glyph (which breaks
// the corporate UI standard and renders inconsistently across Excel hosts).
const graphicIcons: Record<string, SvgIconComponent> = {
  'Traffic Light': TrafficOutlined,
  'Standard Arrow': TrendingFlatOutlined,
  Gauge: SpeedOutlined,
  Thermometer: DeviceThermostatOutlined,
  Cylinder: BarChartOutlined,
  'Smiley Face': SentimentSatisfiedOutlined,
};

const INSERT_OPTIONS: { mode: KpiInsertMode; label: string; description: string }[] = [
  { mode: 'full_row', label: strings.kpiCard.insertFullRow, description: strings.kpiCard.insertFullRowDesc },
  { mode: 'value_only', label: strings.kpiCard.insertValueOnly, description: strings.kpiCard.insertValueOnlyDesc },
  { mode: 'value_goal', label: strings.kpiCard.insertValueGoal, description: strings.kpiCard.insertValueGoalDesc },
  { mode: 'status_only', label: strings.kpiCard.insertStatusIcon, description: strings.kpiCard.insertStatusIconDesc },
  // Bug-6729: Trend Icon removed -- the gateway does not serve KPI Trend
  // members, so the formula would be permanently #N/A.
  { mode: 'kpi_card', label: strings.kpiCard.insertKpiCard, description: strings.kpiCard.insertKpiCardDesc },
  { mode: 'formula_ref', label: strings.kpiCard.insertFormulaRef, description: strings.kpiCard.insertFormulaRefDesc },
];

export default function KpiCard({
  kpi,
  measures,
  projectId,
  modelId,
  personaId,
  checked,
  onToggle,
  onAddToValues,
  onInsertAsFormulas,
  onInsertKpi,
}: KpiCardProps) {
  const [evalData, setEvalData] = useState<KpiEvaluateResponse | null>(null);
  const [evalLoading, setEvalLoading] = useState(false);
  const [evalRequested, setEvalRequested] = useState(false);
  const [insertAnchorEl, setInsertAnchorEl] = useState<HTMLElement | null>(null);
  const [detailsOpen, setDetailsOpen] = useState(false);
  const insertOpen = Boolean(insertAnchorEl);

  // Bug-6361 (R2-N1): a monotonic request id guards against out-of-order async
  // responses. Every evaluation captures the id current when it started; its
  // result is applied only if it is still the latest. A persona switch (or any
  // newer fetch) bumps the id, so a slow response fetched under the PREVIOUS
  // persona can never clobber the current persona's value — the exact
  // wrong-numbers-under-wrong-persona hazard R1 targeted.
  const reqIdRef = useRef(0);

  const runEvaluation = useCallback(() => {
    const reqId = ++reqIdRef.current;
    setEvalRequested(true);
    setEvalLoading(true);
    // Bug-6361: evaluate under the active persona so the card value matches the
    // rest of the pane (measures, batch KPI evaluation) instead of the default.
    evaluateKpi(projectId, modelId, kpi.id, personaId || undefined)
      .then((result) => { if (reqIdRef.current === reqId) setEvalData(result); })
      .catch(() => { if (reqIdRef.current === reqId) setEvalData(null); })
      .finally(() => { if (reqIdRef.current === reqId) setEvalLoading(false); });
  }, [projectId, modelId, kpi.id, personaId]);

  const fetchEvaluation = useCallback(() => {
    if (evalRequested) return;
    runEvaluation();
  }, [evalRequested, runEvaluation]);

  // Bug-6361 (R1): the persona changed. The cached evaluation is now stale (it
  // was fetched under the previous persona) and the card is NOT remounted
  // because KpiLibrary keys it by kpi.id, which is persona-stable. Bump the
  // request id (invalidating any in-flight fetch), discard the cache so the next
  // open re-evaluates under the new persona, and re-fetch immediately if a panel
  // showing the value is currently open.
  useEffect(() => {
    reqIdRef.current++;
    setEvalData(null);
    setEvalRequested(false);
    setEvalLoading(false);
    if (detailsOpen || insertOpen) {
      runEvaluation();
    }
    // Only react to a persona change; the open flags are read as a snapshot.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [personaId]);

  const valueMeasure = measures.find(m => m.id === kpi.value_measure_id);
  const goalMeasure = measures.find(m => m.id === kpi.goal_measure_id);
  const GraphicIcon = graphicIcons[kpi.status_graphic] || AssessmentOutlined;

  const detailRows = [
    kpi.description && { label: strings.kpiCard.detailDescription, value: kpi.description },
    valueMeasure && { label: strings.kpiCard.detailValue, value: valueMeasure.display_name },
    goalMeasure && { label: strings.kpiCard.detailGoal, value: goalMeasure.display_name },
    evalData?.formatted_value && { label: strings.kpiCard.detailCurrent, value: evalData.formatted_value },
    evalData?.formatted_goal && { label: strings.kpiCard.detailTarget, value: evalData.formatted_goal },
    evalData?.status_label && { label: strings.kpiCard.detailStatus, value: evalData.status_label },
    evalData?.trend_label && { label: strings.kpiCard.detailTrend, value: evalData.trend_label },
    { label: strings.kpiCard.detailGraphic, value: kpi.status_graphic },
    kpi.weight !== null && { label: strings.kpiCard.detailWeight, value: String(kpi.weight) },
    kpi.display_folder && { label: strings.kpiCard.detailFolder, value: kpi.display_folder },
    kpi.certification_status !== 'draft' && { label: strings.kpiCard.detailCertification, value: kpi.certification_status },
  ].filter(Boolean) as { label: string; value: string }[];

  const handleInsertToggle = (e: React.MouseEvent) => {
    e.stopPropagation();
    if (insertOpen) {
      setInsertAnchorEl(null);
      return;
    }
    setInsertAnchorEl(e.currentTarget as HTMLElement);
    fetchEvaluation();
  };

  return (
    <>
      <Box
        onClick={() => (checked ? onToggle() : onAddToValues())}
        sx={{
          display: 'flex', alignItems: 'center', gap: 0.75,
          pl: '10px', pr: '4px', py: 0, height: 28, minHeight: 28, cursor: 'pointer',
          borderBottom: `1px solid ${tokens.colorBorderLight}`,
          bgcolor: checked ? tokens.colorPrimaryBg : 'transparent',
          '&:hover': { bgcolor: checked ? tokens.colorPrimaryBg : tokens.colorSubtleFill },
        }}
      >
        <GraphicIcon sx={{ fontSize: 15, flexShrink: 0, color: tokens.colorTextSecondary }} />
        <Box sx={{ flex: 1, minWidth: 0, overflow: 'hidden' }}>
          <Typography
            sx={{
              fontSize: 12,
              fontWeight: checked ? 600 : 400,
              color: kpi.certification_status === 'deprecated' ? tokens.colorTextSecondary : checked ? tokens.colorPrimary : tokens.colorCharcoal,
              textDecoration: kpi.certification_status === 'deprecated' ? 'line-through' : 'none',
              overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap',
            }}
          >
            {kpi.display_name || kpi.name}
          </Typography>
        </Box>
        <Chip
          label={strings.kpiCard.chipKpi}
          size="small"
          sx={{ fontSize: 9, height: 14, bgcolor: tokens.colorGoldBg, color: tokens.colorGoldDark, fontWeight: 600, flexShrink: 0, borderRadius: '7px' }}
        />
        {kpi.certification_status === 'certified' && (
          <Chip
            label={strings.kpiCard.chipCertified}
            size="small"
            sx={{ fontSize: 9, height: 14, bgcolor: 'rgba(46,125,50,0.08)', color: '#2e7d32', fontWeight: 600, flexShrink: 0, borderRadius: '7px' }}
          />
        )}
        {kpi.certification_status === 'deprecated' && (
          <Chip
            label={strings.kpiCard.chipDeprecated}
            size="small"
            sx={{ fontSize: 9, height: 14, bgcolor: 'rgba(237,108,2,0.08)', color: '#ed6c02', fontWeight: 600, flexShrink: 0, borderRadius: '7px' }}
          />
        )}
        <IconButton
          size="small"
          onClick={(e) => {
            e.stopPropagation();
            setDetailsOpen(v => !v);
            if (!detailsOpen) fetchEvaluation();
          }}
          title={detailsOpen ? strings.kpiCard.hideDetails : strings.kpiCard.showDetails}
          aria-label={templates.kpiCard.detailsAria(detailsOpen ? strings.kpiCard.hideDetails : strings.kpiCard.showDetails, kpi.display_name || kpi.name)}
          sx={{
            width: 22,
            height: 22,
            color: detailsOpen ? tokens.colorPrimary : tokens.colorTextSecondary,
            '&:hover': { bgcolor: tokens.colorSubtleFill },
          }}
        >
          {detailsOpen ? <KeyboardArrowUpOutlined sx={{ fontSize: 16 }} /> : <InfoOutlined sx={{ fontSize: 15 }} />}
        </IconButton>
        {/* Bug-6708: these controls were clickable spans -- no role, no
            tabIndex, no key handling -- so keyboard and screen-reader users
            could not trigger "Add KPI" (the Bug-6701 entry point) at all.
            Real IconButtons (the pattern the details toggle above and
            MeasureCard already use) give focus, Enter/Space activation, and
            an accessible name for free. */}
        {!checked && (
          <Box sx={{ display: 'flex', alignItems: 'center', gap: 0.25, flexShrink: 0 }}>
            {onInsertKpi && (
              <IconButton
                size="small"
                onClick={handleInsertToggle}
                title={strings.kpiCard.insertOptions}
                aria-label={templates.kpiCard.insertOptionsAria(kpi.display_name || kpi.name)}
                aria-expanded={insertOpen}
                sx={{
                  width: 22, height: 22, color: tokens.colorTextSecondary,
                  '&:hover': { bgcolor: tokens.colorGoldBg },
                }}
              >
                <MoreHorizOutlined sx={{ fontSize: 16 }} />
              </IconButton>
            )}
            {onInsertAsFormulas && !onInsertKpi && (
              <IconButton
                size="small"
                onClick={(e) => { e.stopPropagation(); onInsertAsFormulas(); }}
                title={strings.kpiCard.insertAsCubeFormulas}
                aria-label={templates.kpiCard.insertAsCubeFormulasAria(kpi.display_name || kpi.name)}
                sx={{
                  width: 22, height: 22, color: tokens.colorTextSecondary,
                  '&:hover': { bgcolor: tokens.colorGoldBg },
                }}
              >
                <FunctionsIcon sx={{ fontSize: 14 }} />
              </IconButton>
            )}
            <IconButton
              size="small"
              onClick={(e) => { e.stopPropagation(); onAddToValues(); }}
              title={strings.kpiCard.addKpiToReport}
              aria-label={templates.kpiCard.addKpiToReportAria(kpi.display_name || kpi.name)}
              sx={{
                width: 22, height: 22, color: tokens.colorPrimary,
                '&:hover': { bgcolor: tokens.colorPrimaryBg },
              }}
            >
              <AddIcon sx={{ fontSize: 14 }} />
            </IconButton>
          </Box>
        )}
        {/* Bug-6710: a staged (checked) KPI previously hid its icon cluster,
            leaving row-click as the only way to un-stage it -- unreachable by
            keyboard. This button is the keyboard counterpart of the row click. */}
        {checked && (
          <IconButton
            size="small"
            onClick={(e) => { e.stopPropagation(); onToggle(); }}
            title={strings.kpiCard.removeKpiFromReport}
            aria-label={templates.kpiCard.removeKpiFromReportAria(kpi.display_name || kpi.name)}
            sx={{
              width: 22, height: 22, color: tokens.colorPrimary, flexShrink: 0,
              '&:hover': { bgcolor: tokens.colorPrimaryBg },
            }}
          >
            <RemoveIcon sx={{ fontSize: 14 }} />
          </IconButton>
        )}
      </Box>

      <Collapse in={detailsOpen}>
        <Box sx={{ px: 1.25, py: 0.75, bgcolor: tokens.colorSubtleFill, borderBottom: `1px solid ${tokens.colorBorderLight}` }}>
          {evalLoading && (
            <Typography sx={{ fontSize: 11, color: tokens.colorTextSecondary, mb: 0.75 }}>
              {strings.kpiCard.loadingStatus}
            </Typography>
          )}
          {detailRows.map(row => (
            <Box key={row.label} sx={{ mb: 0.4 }}>
              <Typography sx={{ fontSize: 10, fontWeight: 700, color: tokens.colorTextSecondary, textTransform: 'uppercase', lineHeight: 1.2 }}>
                {row.label}
              </Typography>
              <Typography sx={{ fontSize: 11, color: tokens.colorCharcoal, lineHeight: 1.3 }}>
                {row.value}
              </Typography>
            </Box>
          ))}
        </Box>
      </Collapse>

      {onInsertKpi && (
        <Menu
          anchorEl={insertAnchorEl}
          open={insertOpen}
          onClose={() => setInsertAnchorEl(null)}
          anchorOrigin={{ vertical: 'top', horizontal: 'right' }}
          transformOrigin={{ vertical: 'top', horizontal: 'right' }}
          MenuListProps={{ dense: true, 'aria-label': strings.kpiCard.insertOptionsLabel }}
          PaperProps={{ sx: { minWidth: 210, border: `1px solid ${tokens.colorBorder}`, boxShadow: '0 4px 12px rgba(0,0,0,0.12)' } }}
        >
          <Typography sx={{ px: 1.5, pt: 0.5, pb: 0.75, fontSize: 10, fontWeight: 700, color: tokens.colorTextSecondary, textTransform: 'uppercase', letterSpacing: '0.04em', borderBottom: `1px solid ${tokens.colorBorderLight}` }}>
            {strings.kpiCard.insertOptionsLabel}
          </Typography>
          {INSERT_OPTIONS.map(opt => (
            <MenuItem
              key={opt.mode}
              onClick={() => {
                onInsertKpi(opt.mode);
                setInsertAnchorEl(null);
              }}
              sx={{ px: 1.5, py: 0.5, alignItems: 'flex-start', whiteSpace: 'nowrap' }}
            >
              <ListItemText
                primary={opt.label}
                secondary={opt.description}
                primaryTypographyProps={{ fontSize: 12, lineHeight: 1.2 }}
                secondaryTypographyProps={{ fontSize: 10, lineHeight: 1.2 }}
              />
            </MenuItem>
          ))}
        </Menu>
      )}
    </>
  );
}
