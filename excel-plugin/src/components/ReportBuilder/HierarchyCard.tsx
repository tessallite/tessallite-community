import { useState } from 'react';
import { Box, Typography, Chip, Collapse, Tooltip } from '@mui/material';
import { ExpandMore, ExpandLess } from '@mui/icons-material';
import { tokens } from '../../theme';
import type { Hierarchy, HierarchyLevel } from '../../types/tessallite';
import { strings, templates } from '../../i18n/strings';

interface HierarchyCardProps {
  hierarchy: Hierarchy;
  onAssignToRows: (level?: HierarchyLevel) => void;
}

const typeStyle: Record<string, { bg: string; text: string }> = {
  date_embedded: { bg: tokens.colorGoldBg, text: tokens.colorGoldDark },
  explicit: { bg: tokens.colorPurpleBg, text: tokens.colorPurple },
  segment: { bg: tokens.colorPrimaryBg, text: tokens.colorPrimary },
};

// Human label for the backend type enum; the chip previously showed the raw
// token ("date_embedded") -- the Bug-6716 defect class.
const typeLabel: Record<string, string> = {
  date_embedded: strings.hierarchyCard.typeCalendar,
  explicit: strings.hierarchyCard.typeExplicit,
  segment: strings.hierarchyCard.typeSegment,
};

/**
 * Display levels for a hierarchy from the LIST endpoint. The summary
 * response carries `level_names` (ordinal-ordered) but NOT full `levels`
 * objects -- the previous `hierarchy.levels ?? []` therefore rendered every
 * hierarchy as a bare header with no levels. Synthesized levels carry no
 * `dimensionName`; the add-to-zone handler resolves that from the detail
 * endpoint BY NAME (persona exclusions can skip ordinals, so a positional
 * index is not a safe join key).
 */
export function deriveDisplayLevels(hierarchy: Pick<Hierarchy, 'levels' | 'level_names'>): HierarchyLevel[] {
  if (hierarchy.levels && hierarchy.levels.length > 0) return hierarchy.levels;
  return (hierarchy.level_names ?? []).map((name, idx) => ({ name, level_number: idx }));
}

export default function HierarchyCard({ hierarchy, onAssignToRows }: HierarchyCardProps) {
  const [expanded, setExpanded] = useState(false);
  const tStyle = typeStyle[hierarchy.type] || typeStyle.explicit;
  const levels = deriveDisplayLevels(hierarchy);
  const levelChain = levels.map(l => l.name).join(' › ');

  return (
    <Box sx={{ '&:hover': { bgcolor: tokens.colorSubtleFill } }}>
      <Box
        sx={{
          display: 'flex', alignItems: 'center', gap: 0.75,
          px: 1.5, py: '5px', minHeight: 30, cursor: 'pointer',
          '&:hover .h-action': { opacity: 1 },
        }}
        onClick={() => setExpanded(!expanded)}
      >
        {expanded
          ? <ExpandLess sx={{ fontSize: 14, color: tokens.colorTextSecondary, flexShrink: 0 }} />
          : <ExpandMore sx={{ fontSize: 14, color: tokens.colorTextSecondary, flexShrink: 0 }} />
        }
        <Tooltip title={levelChain} placement="right" arrow enterDelay={500}>
          <Typography sx={{ fontSize: 13, flex: 1, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
            {hierarchy.display_name || hierarchy.name}
          </Typography>
        </Tooltip>
        <Chip
          label={typeLabel[hierarchy.type] ?? hierarchy.type}
          size="small"
          sx={{ fontSize: 9, height: 16, bgcolor: tStyle.bg, color: tStyle.text, flexShrink: 0 }}
        />
        {/* Bug-6708 class: a real button so keyboard users can assign; the
            hover-revealed affordance also reveals on keyboard focus. */}
        <Box
          className="h-action"
          component="button"
          onClick={(e) => { e.stopPropagation(); onAssignToRows(); }}
          aria-label={templates.hierarchyCard.addToRowsAria(hierarchy.display_name || hierarchy.name)}
          sx={{
            opacity: 0, fontSize: 10, px: '5px', py: '2px', borderRadius: 0.5,
            cursor: 'pointer', color: tokens.colorPrimary, flexShrink: 0,
            lineHeight: 1.4, transition: 'opacity 0.15s',
            border: 'none', bgcolor: 'transparent', fontFamily: 'inherit',
            '&:hover': { bgcolor: tokens.colorPrimaryBg },
            '&:focus-visible': { opacity: 1 },
          }}
        >
          Rows
        </Box>
      </Box>

      <Collapse in={expanded}>
        <Box sx={{ ml: 2.5, mr: 1.5, pb: 0.75, borderLeft: `1px solid ${tokens.colorBorderLight}` }}>
          {levels.map((level, idx) => (
            <Box
              key={level.level_number}
              sx={{
                display: 'flex', alignItems: 'center', gap: 0.5,
                py: '3px', pl: 1.5,
                position: 'relative',
                '&:hover .level-action': { opacity: 1 },
                '&::before': {
                  content: '""', position: 'absolute', left: 0,
                  top: '50%', width: 8, height: '1px',
                  bgcolor: tokens.colorBorderLight,
                },
              }}
            >
              <Typography
                component="span"
                sx={{ fontSize: 9, color: tokens.colorTextSecondary, minWidth: 14, textAlign: 'right', mr: 0.25 }}
              >
                L{level.level_number}
              </Typography>
              <Typography sx={{ fontSize: 11, color: tokens.colorCharcoal, flex: 1, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                {level.name}
              </Typography>
              {level.time_unit && (
                <Chip
                  label={level.time_unit}
                  size="small"
                  sx={{ fontSize: 9, height: 14, bgcolor: tokens.colorGoldBg, color: tokens.colorGoldDark }}
                />
              )}
              <Box
                className="level-action"
                component="button"
                onClick={() => onAssignToRows(level)}
                aria-label={templates.hierarchyCard.addLevelToRowsAria(hierarchy.display_name || hierarchy.name, level.name)}
                sx={{
                  opacity: 0, fontSize: 10, px: '5px', py: '2px', borderRadius: 0.5,
                  cursor: 'pointer', color: tokens.colorPrimary, lineHeight: 1.4,
                  transition: 'opacity 0.15s',
                  border: 'none', bgcolor: 'transparent', fontFamily: 'inherit',
                  '&:hover': { bgcolor: tokens.colorPrimaryBg },
                  '&:focus-visible': { opacity: 1 },
                }}
              >
                Rows
              </Box>
            </Box>
          ))}
        </Box>
      </Collapse>
    </Box>
  );
}
