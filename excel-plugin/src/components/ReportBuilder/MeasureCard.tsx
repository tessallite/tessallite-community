import { useState } from 'react';
import { Box, Typography, Chip, Collapse, IconButton } from '@mui/material';
import {
  Add as AddIcon,
  Remove as RemoveIcon,
  Functions as FunctionsIcon,
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
  /** Phase A default: insert as TESSALLITE.VALUE() formula (connectionless). */
  onInsertAsFunction?: () => void;
  /** Advanced: insert as CUBEVALUE formula (requires workbook connection). */
  onInsertAsFormula?: () => void;
  glossaryEntries?: GlossaryEntry[];
}

const typeColors: Record<string, { bg: string; text: string }> = {
  standard: { bg: tokens.colorPrimaryBg, text: tokens.colorPrimary },
  calculated: { bg: tokens.colorGoldBg, text: tokens.colorGoldDark },
  variant: { bg: tokens.colorPurpleBg, text: tokens.colorPurple },
};

export default function MeasureCard({
  measure,
  checked,
  onToggle,
  onAddToValues,
  onInsertAsFunction,
  onInsertAsFormula,
  glossaryEntries,
}: MeasureCardProps) {
  const [detailsOpen, setDetailsOpen] = useState(false);
  const typeStyle = typeColors[measure.measure_type] || typeColors.standard;

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
        <Box
          aria-hidden="true"
          sx={{
            width: 4,
            height: 20,
            borderRadius: 1,
            bgcolor: checked ? tokens.colorPrimary : tokens.colorBorderLight,
            flexShrink: 0,
          }}
        />
        <Typography
          sx={{
            fontSize: 13, flex: 1, minWidth: 0,
            fontWeight: checked ? 600 : 400,
            color: checked ? tokens.colorPrimary : tokens.colorCharcoal,
            overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap',
          }}
        >
          {measure.display_name}
        </Typography>
        <Chip
          label={measure.measure_type}
          size="small"
          sx={{ fontSize: 9, height: 16, bgcolor: typeStyle.bg, color: typeStyle.text, fontWeight: 600, flexShrink: 0 }}
        />
        {detailRows.length > 0 && (
          <IconButton
            size="small"
            onClick={(e) => { e.stopPropagation(); setDetailsOpen(v => !v); }}
            title={detailsOpen ? strings.measureCard.hideDetails : strings.measureCard.showDetails}
            aria-label={templates.measureCardDetail.detailsAria(detailsOpen ? strings.measureCard.hideDetails : strings.measureCard.showDetails, measure.display_name)}
            sx={{
              width: 28,
              height: 28,
              color: detailsOpen ? tokens.colorPrimary : tokens.colorTextSecondary,
              '&:hover': { bgcolor: tokens.colorSubtleFill },
            }}
          >
            {detailsOpen ? <KeyboardArrowUpOutlined sx={{ fontSize: 16 }} /> : <InfoOutlined sx={{ fontSize: 15 }} />}
          </IconButton>
        )}
        {!checked && (
          <Box
            className="m-add"
            sx={{ display: 'flex', alignItems: 'center', gap: 0.25, flexShrink: 0 }}
          >
            {onInsertAsFunction && (
              <IconButton
                size="small"
                onClick={(e) => { e.stopPropagation(); onInsertAsFunction(); }}
                title={strings.measureCard.insertAsFunction}
                aria-label={templates.measureCard.insertAsFunction(measure.display_name)}
                sx={{
                  width: 28, height: 28, color: tokens.colorGoldDark,
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
                  width: 28, height: 28, color: tokens.colorGoldDark,
                  '&:hover': { bgcolor: tokens.colorGoldBg },
                }}
              >
                <FunctionsIcon sx={{ fontSize: 14 }} />
              </IconButton>
            )}
            <IconButton
              size="small"
              onClick={(e) => { e.stopPropagation(); onAddToValues(); }}
              title={strings.measureCard.addToValues}
              aria-label={templates.measureCardDetail.addToValuesAria(measure.display_name)}
              sx={{
                width: 28, height: 28, color: tokens.colorPrimary,
                '&:hover': { bgcolor: tokens.colorPrimaryBg },
              }}
            >
              <AddIcon sx={{ fontSize: 14 }} />
            </IconButton>
          </Box>
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
              width: 28, height: 28, color: tokens.colorPrimary, flexShrink: 0,
              '&:hover': { bgcolor: tokens.colorPrimaryBg },
            }}
          >
            <RemoveIcon sx={{ fontSize: 14 }} />
          </IconButton>
        )}
      </Box>
      <Collapse in={detailsOpen}>
        <Box sx={{ px: 2, py: 1, bgcolor: tokens.colorSubtleFill, borderBottom: `1px solid ${tokens.colorBorderLight}` }}>
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
    </>
  );
}
