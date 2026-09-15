import { useState } from 'react';
import { Box, Typography, Chip, Collapse, IconButton } from '@mui/material';
import {
  Add as AddIcon,
  Remove as RemoveIcon,
  Functions as FunctionsIcon,
  FilterAltOutlined,
  InfoOutlined,
  KeyboardArrowUpOutlined,
} from '@mui/icons-material';
import { tokens } from '../../theme';
import type { Measure, GlossaryEntry } from '../../types/tessallite';
import { strings, templates } from '../../i18n/strings';

interface MeasureCardProps {
  measure: Measure;
  checked: boolean;
  onToggle: () => void;
  onAddToValues: () => void;
  /** Bug-9747: add a HAVING-style filter on this measure's aggregated value. */
  onAddToFilter?: () => void;
  /** Phase A default: insert as TESSALLITE.VALUE() formula (connectionless). */
  onInsertAsFunction?: () => void;
  /** Advanced: insert as CUBEVALUE formula (requires workbook connection). */
  onInsertAsFormula?: () => void;
  glossaryEntries?: GlossaryEntry[];
}

/**
 * One colour per `measure_type` the producer actually sends.
 *
 * Bug-9882: this used to key on a 'variant' type the server has never emitted,
 * while omitting 'physical', which it emits on roughly a quarter of `modely`'s
 * measures — so the dead entry was never reached and every physical measure
 * silently borrowed the 'standard' colour. Both halves are the same drift: a
 * client reading what it believes a field means rather than what the wire
 * carries. `physical` and `standard` are both source-column measures, so they
 * share the primary tone; `calculated` is the one that is derived.
 */
export const MEASURE_TYPE_COLORS: Record<string, { bg: string; text: string }> = {
  physical: { bg: tokens.colorPrimaryBg, text: tokens.colorPrimary },
  standard: { bg: tokens.colorPrimaryBg, text: tokens.colorPrimary },
  calculated: { bg: tokens.colorGoldBg, text: tokens.colorGoldDark },
};

export default function MeasureCard({
  measure,
  checked,
  onToggle,
  onAddToValues,
  onAddToFilter,
  onInsertAsFunction,
  onInsertAsFormula,
  glossaryEntries,
}: MeasureCardProps) {
  const [detailsOpen, setDetailsOpen] = useState(false);

  const glossaryMatch = glossaryEntries?.find(
    g => g.term.toLowerCase() === measure.display_name.toLowerCase() ||
         g.term.toLowerCase() === measure.name.toLowerCase(),
  );

  const detailRows = [
    measure.effective_description && { label: strings.measureCard.detailDescription, value: measure.effective_description },
    measure.default_agg && { label: strings.measureCard.detailAggregation, value: measure.default_agg },
    measure.format && { label: strings.measureCard.detailFormat, value: measure.format },
    measure.display_folder && { label: strings.measureCard.detailFolder, value: measure.display_folder },
    measure.semi_additive_behavior && { label: strings.measureCard.detailSemiAdditive, value: measure.semi_additive_behavior },
    glossaryMatch?.definition && { label: strings.measureCard.detailDefinition, value: glossaryMatch.definition },
  ].filter(Boolean) as { label: string; value: string }[];
  const hasDetails = detailRows.length > 0;

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
        <Box
          aria-hidden="true"
          sx={{
            width: 4,
            height: 16,
            borderRadius: 1,
            bgcolor: checked ? tokens.colorPrimary : tokens.colorBorderLight,
            flexShrink: 0,
          }}
        />
        <Typography
          onClick={hasDetails ? (event) => { event.stopPropagation(); setDetailsOpen(v => !v); } : undefined}
          sx={{
            fontSize: 12, flex: 1, minWidth: 0,
            fontWeight: checked ? 600 : 400,
            color: checked ? tokens.colorPrimary : tokens.colorCharcoal,
            cursor: hasDetails ? 'pointer' : 'default',
            overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap',
          }}
        >
          {measure.display_name}
        </Typography>
        {measure.measure_type === 'calculated' && (
          <Chip
            label={strings.measureCard.chipCalculated}
            size="small"
            sx={{ fontSize: 9, height: 14, bgcolor: MEASURE_TYPE_COLORS.calculated.bg, color: MEASURE_TYPE_COLORS.calculated.text, fontWeight: 600, flexShrink: 0, borderRadius: '7px' }}
          />
        )}
        {detailRows.length > 0 && (
          <IconButton
            size="small"
            onClick={(e) => { e.stopPropagation(); setDetailsOpen(v => !v); }}
            title={detailsOpen ? strings.measureCard.hideDetails : strings.measureCard.showDetails}
            aria-label={templates.measureCardDetail.detailsAria(detailsOpen ? strings.measureCard.hideDetails : strings.measureCard.showDetails, measure.display_name)}
            sx={{
              width: 22,
              height: 22,
              color: detailsOpen ? tokens.colorPrimary : tokens.colorTextSecondary,
              '&:hover': { bgcolor: tokens.colorSubtleFill },
            }}
          >
            {detailsOpen ? <KeyboardArrowUpOutlined sx={{ fontSize: 16 }} /> : <InfoOutlined sx={{ fontSize: 15 }} />}
          </IconButton>
        )}
        {/* Bug-9759-followup (sigma-icon regression): "insert as function"/
            "insert as CUBEVALUE" is independent of whether the measure is
            also staged in Values -- same reasoning as Bug-9747's filter icon
            below. Rendered outside the !checked block so it never disappears
            once the measure is added to Values. Only onAddToValues itself is
            genuinely exclusive with an already-checked state. */}
        {onInsertAsFunction && (
          <IconButton
            size="small"
            onClick={(e) => { e.stopPropagation(); onInsertAsFunction(); }}
            title={strings.measureCard.insertAsFunction}
            aria-label={templates.measureCard.insertAsFunction(measure.display_name)}
            sx={{
              width: 22, height: 22, color: tokens.colorTextSecondary, flexShrink: 0,
              '&:hover': { bgcolor: tokens.colorGoldBg },
            }}
          >
            <FunctionsIcon sx={{ fontSize: 14 }} />
          </IconButton>
        )}
        {onInsertAsFormula && !onInsertAsFunction && (
          <IconButton
            size="small"
            onClick={(e) => { e.stopPropagation(); onInsertAsFormula(); }}
            title={strings.measureCard.insertAsCubeFormula}
            aria-label={templates.measureCard.insertAsCubeFormula(measure.display_name)}
            sx={{
              width: 22, height: 22, color: tokens.colorTextSecondary, flexShrink: 0,
              '&:hover': { bgcolor: tokens.colorGoldBg },
            }}
          >
            <FunctionsIcon sx={{ fontSize: 14 }} />
          </IconButton>
        )}
        {!checked && (
          <Box
            className="m-add"
            sx={{ display: 'flex', alignItems: 'center', gap: 0.25, flexShrink: 0 }}
          >
            <IconButton
              size="small"
              onClick={(e) => { e.stopPropagation(); onAddToValues(); }}
              title={strings.measureCard.addToValues}
              aria-label={templates.measureCardDetail.addToValuesAria(measure.display_name)}
              sx={{
                width: 22, height: 22, color: tokens.colorPrimary,
                '&:hover': { bgcolor: tokens.colorPrimaryBg },
              }}
            >
              <AddIcon sx={{ fontSize: 14 }} />
            </IconButton>
          </Box>
        )}
        {/* Bug-9747: a measure must be addable to Filters independently of
            whether it is staged in Values -- the exact SQL pattern this
            exists for is `SELECT SUM(x) ... HAVING SUM(x) > n`, where the
            same measure is both a value AND a filter. Rendered outside the
            !checked block (unlike onAddToValues/onInsertAsFunction, which
            are genuinely mutually exclusive with "already checked") so it
            never disappears once the measure is added to Values. */}
        {onAddToFilter && (
          <IconButton
            size="small"
            onClick={(e) => { e.stopPropagation(); onAddToFilter(); }}
            title={strings.measureCard.addToFilter}
            aria-label={templates.measureCardDetail.addToFilterAria(measure.display_name)}
            sx={{
              width: 22, height: 22, color: tokens.colorTextSecondary, flexShrink: 0,
              '&:hover': { bgcolor: tokens.colorSubtleFill },
            }}
          >
            <FilterAltOutlined sx={{ fontSize: 14 }} />
          </IconButton>
        )}
        {/* Bug-6713: a staged (checked) measure previously hid its icon
            cluster, leaving row-click as the only way to un-stage it --
            unreachable by keyboard. Keyboard counterpart of the row click,
            mirroring KpiCard's Bug-6710 remove button. */}
        {checked && (
          <IconButton
            size="small"
            onClick={(e) => { e.stopPropagation(); onToggle(); }}
            title={strings.measureCard.removeFromValues}
            aria-label={templates.measureCard.removeFromValuesAria(measure.display_name)}
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
    </>
  );
}
