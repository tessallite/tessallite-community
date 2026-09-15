import { useState } from 'react';
import { Box, Typography, Chip, Collapse, Tooltip } from '@mui/material';
import { ExpandMore, ExpandLess, TableRowsOutlined } from '@mui/icons-material';
import { tokens } from '../../theme';
import type { Hierarchy, HierarchyLevel } from '../../types/tessallite';
import { strings, templates } from '../../i18n/strings';

interface HierarchyCardProps {
  hierarchy: Hierarchy;
  onAssignToRows: (level?: HierarchyLevel) => void;
}

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
  const levels = deriveDisplayLevels(hierarchy);
  const levelChain = levels.map(l => l.name).join(' › ');

  return (
    <Box sx={{ '&:hover': { bgcolor: tokens.colorSubtleFill } }}>
      <Box
        sx={{
          display: 'flex', alignItems: 'center', gap: 0.75,
          pl: '10px', pr: '4px', py: 0, height: 28, minHeight: 28, cursor: 'pointer',
          '&:hover .h-action': { opacity: 1 },
        }}
        onClick={() => setExpanded(!expanded)}
      >
        {expanded
          ? <ExpandLess sx={{ fontSize: 14, color: tokens.colorTextSecondary, flexShrink: 0 }} />
          : <ExpandMore sx={{ fontSize: 14, color: tokens.colorTextSecondary, flexShrink: 0 }} />
        }
        <Tooltip title={levelChain} placement="right" arrow enterDelay={500}>
          <Typography sx={{ fontSize: 12, flex: 1, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
            {hierarchy.display_name || hierarchy.name}
          </Typography>
        </Tooltip>
        <Chip
          label={typeLabel[hierarchy.type] ?? hierarchy.type}
          size="small"
          sx={{ fontSize: 9, height: 14, bgcolor: tokens.colorSubtleFill, color: tokens.colorTextSecondary, flexShrink: 0, borderRadius: '7px' }}
        />
        {/* Bug-6708 class: a real button so keyboard users can assign; the
            hover-revealed affordance also reveals on keyboard focus. */}
        <Box
          className="h-action"
          component="button"
          onClick={(e) => { e.stopPropagation(); onAssignToRows(); }}
          aria-label={templates.hierarchyCard.addToRowsAria(hierarchy.display_name || hierarchy.name)}
          sx={{
            display: 'flex', alignItems: 'center', justifyContent: 'center',
            width: 22, height: 22, p: 0, borderRadius: 0.5,
            cursor: 'pointer', color: tokens.colorPrimary, flexShrink: 0,
            lineHeight: 1.4,
            border: 'none', bgcolor: 'transparent', fontFamily: 'inherit',
            '&:hover': { bgcolor: tokens.colorPrimaryBg },
          }}
        >
          <TableRowsOutlined sx={{ fontSize: 15 }} />
        </Box>
      </Box>

      <Collapse in={expanded}>
        <Box sx={{ ml: 2.5, mr: 1.5, pb: 0.75, borderLeft: `1px solid ${tokens.colorBorderLight}` }}>
          {levels.map((level, idx) => (
            <Box
              key={level.level_number}
              sx={{
                display: 'flex', alignItems: 'center', gap: 0.5,
                height: 26, pl: '30px', pr: '4px',
                bgcolor: '#fafafa',
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
                sx={{ fontSize: 11, color: tokens.colorTextSecondary, minWidth: 10, textAlign: 'right', mr: 0.25 }}
              >
                └
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
                  display: 'flex', alignItems: 'center', justifyContent: 'center',
                  width: 22, height: 22, p: 0, borderRadius: 0.5,
                  cursor: 'pointer', color: tokens.colorPrimary, lineHeight: 1.4,
                  border: 'none', bgcolor: 'transparent', fontFamily: 'inherit',
                  '&:hover': { bgcolor: tokens.colorPrimaryBg },
                }}
              >
                <TableRowsOutlined sx={{ fontSize: 15 }} />
              </Box>
            </Box>
          ))}
        </Box>
      </Collapse>
    </Box>
  );
}
