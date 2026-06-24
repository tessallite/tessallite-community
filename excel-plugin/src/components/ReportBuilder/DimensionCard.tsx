import { useState } from 'react';
import { Box, Typography, Chip, Collapse, IconButton, Tooltip } from '@mui/material';
import {
  FilterAltOutlined,
  InfoOutlined,
  KeyboardArrowUpOutlined,
  MoreHorizOutlined,
  TableRowsOutlined,
  ViewColumnOutlined,
} from '@mui/icons-material';
import { tokens } from '../../theme';
import type { DimensionCompatibilityState } from '../../utils/fieldCompatibility';

interface DimensionCardProps {
  id: string;
  displayName: string;
  description?: string;
  dataType: string;
  sourceType: 'dim' | 'calculated';
  isTimeDimension?: boolean;
  calendarType?: string;
  compatibility?: DimensionCompatibilityState;
  onAssign: (zone: 'rows' | 'columns' | 'filter' | 'slicer') => void;
  onPreviewMembers?: (id: string) => void;
}

const dataTypeStyle: Record<string, { bg: string; text: string }> = {
  string: { bg: tokens.colorSubtleFill, text: tokens.colorTextSecondary },
  time: { bg: tokens.colorGoldBg, text: tokens.colorGoldDark },
  numeric: { bg: tokens.colorPrimaryBg, text: tokens.colorPrimary },
};

export default function DimensionCard({
  id,
  displayName,
  description,
  dataType,
  sourceType,
  isTimeDimension,
  calendarType,
  compatibility,
  onAssign,
  onPreviewMembers,
}: DimensionCardProps) {
  const [detailsOpen, setDetailsOpen] = useState(false);
  const dtStyle = dataTypeStyle[dataType] || dataTypeStyle.string;
  const unavailable = Boolean(compatibility?.disabled);
  const unavailableReason = compatibility?.messages[0];
  const tooltip = unavailableReason || (unavailable ? 'Unavailable for the selected measure combination' : undefined);

  const detailRows = [
    description && { label: 'Description', value: description },
    { label: 'Type', value: `${dataType}${sourceType === 'calculated' ? ' - calculated' : ''}` },
    isTimeDimension && calendarType ? { label: 'Calendar', value: calendarType } : null,
  ].filter(Boolean) as { label: string; value: string }[];

  const actions = [
    { zone: 'rows' as const, icon: <TableRowsOutlined sx={{ fontSize: 14 }} />, label: 'Rows' },
    { zone: 'columns' as const, icon: <ViewColumnOutlined sx={{ fontSize: 14 }} />, label: 'Columns' },
    { zone: 'filter' as const, icon: <FilterAltOutlined sx={{ fontSize: 14 }} />, label: 'Filter' },
  ];

  return (
    <>
      <Box
        sx={{
          display: 'flex',
          alignItems: 'center',
          gap: 0.75,
          px: 1.25,
          py: 0.5,
          minHeight: 36,
          borderBottom: `1px solid ${tokens.colorBorderLight}`,
          opacity: unavailable ? 0.62 : 1,
          '&:hover': { bgcolor: tokens.colorSubtleFill },
        }}
      >
        <Typography
          sx={{
            fontSize: 13,
            flex: 1,
            minWidth: 0,
            color: tokens.colorCharcoal,
            overflow: 'hidden',
            textOverflow: 'ellipsis',
            whiteSpace: 'nowrap',
          }}
        >
          {displayName}
        </Typography>
        <Chip
          label={dataType}
          size="small"
          sx={{ fontSize: 9, height: 16, bgcolor: dtStyle.bg, color: dtStyle.text, flexShrink: 0 }}
        />
        <IconButton
          size="small"
          onClick={(e) => { e.stopPropagation(); setDetailsOpen(v => !v); }}
          title={detailsOpen ? 'Hide details' : 'Show details'}
          aria-label={`${detailsOpen ? 'Hide' : 'Show'} details for ${displayName}`}
          sx={{
            width: 26,
            height: 26,
            color: detailsOpen ? tokens.colorPrimary : tokens.colorTextSecondary,
            '&:hover': { bgcolor: tokens.colorSubtleFill },
          }}
        >
          {detailsOpen ? <KeyboardArrowUpOutlined sx={{ fontSize: 16 }} /> : <InfoOutlined sx={{ fontSize: 15 }} />}
        </IconButton>
        <Box sx={{ display: 'flex', gap: 0, flexShrink: 0 }}>
          {actions.map(action => (
            <Tooltip key={action.zone} title={tooltip || `Add to ${action.label}`}>
              <span>
                <IconButton
                  size="small"
                  onClick={(e) => { e.stopPropagation(); if (!unavailable) onAssign(action.zone); }}
                  disabled={unavailable}
                  title={tooltip || `Add to ${action.label}`}
                  aria-label={`Add ${displayName} to ${action.label}`}
                  sx={{
                    width: 26,
                    height: 26,
                    color: tokens.colorPrimary,
                    '&:hover': { bgcolor: tokens.colorPrimaryBg },
                  }}
                >
                  {action.icon}
                </IconButton>
              </span>
            </Tooltip>
          ))}
          {onPreviewMembers && (
            <IconButton
              size="small"
              onClick={(e) => { e.stopPropagation(); onPreviewMembers(id); }}
              title="Preview members"
              aria-label={`Preview members for ${displayName}`}
              sx={{
                width: 26,
                height: 26,
                color: tokens.colorTextSecondary,
                '&:hover': { bgcolor: tokens.colorSubtleFill },
              }}
            >
              <MoreHorizOutlined sx={{ fontSize: 15 }} />
            </IconButton>
          )}
        </Box>
      </Box>
      <Collapse in={detailsOpen}>
        <Box sx={{ px: 2, py: 1, bgcolor: tokens.colorSubtleFill, borderBottom: `1px solid ${tokens.colorBorderLight}` }}>
          {unavailable && (unavailableReason || compatibility?.compatibleDimensionNames.length) && (
            <Box sx={{ mb: 0.6 }}>
              <Typography sx={{ fontSize: 10, fontWeight: 700, color: tokens.colorTextSecondary, textTransform: 'uppercase', lineHeight: 1.2 }}>
                Compatibility
              </Typography>
              {unavailableReason && (
                <Typography sx={{ fontSize: 11.5, color: tokens.colorRed, lineHeight: 1.35 }}>
                  {unavailableReason}
                </Typography>
              )}
              {compatibility?.compatibleDimensionNames.length ? (
                <Typography sx={{ fontSize: 11.5, color: tokens.colorCharcoal, lineHeight: 1.35 }}>
                  Compatible dimensions: {compatibility.compatibleDimensionNames.join(', ')}
                </Typography>
              ) : null}
            </Box>
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
    </>
  );
}
