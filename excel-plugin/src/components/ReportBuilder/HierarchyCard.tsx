import { useState } from 'react';
import { Box, Typography, Chip, Collapse, Tooltip } from '@mui/material';
import { ExpandMore, ExpandLess } from '@mui/icons-material';
import { tokens } from '../../theme';
import type { Hierarchy, HierarchyLevel } from '../../types/tessallite';

interface HierarchyCardProps {
  hierarchy: Hierarchy;
  onAssignToRows: (level?: HierarchyLevel) => void;
}

const typeStyle: Record<string, { bg: string; text: string }> = {
  date: { bg: tokens.colorGoldBg, text: tokens.colorGoldDark },
  explicit: { bg: tokens.colorPurpleBg, text: tokens.colorPurple },
  segment: { bg: tokens.colorPrimaryBg, text: tokens.colorPrimary },
};

export default function HierarchyCard({ hierarchy, onAssignToRows }: HierarchyCardProps) {
  const [expanded, setExpanded] = useState(false);
  const tStyle = typeStyle[hierarchy.type] || typeStyle.explicit;
  const levels = hierarchy.levels ?? [];
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
          label={hierarchy.type}
          size="small"
          sx={{ fontSize: 9, height: 16, bgcolor: tStyle.bg, color: tStyle.text, flexShrink: 0 }}
        />
        <Box
          className="h-action"
          component="span"
          onClick={(e) => { e.stopPropagation(); onAssignToRows(); }}
          sx={{
            opacity: 0, fontSize: 10, px: '5px', py: '2px', borderRadius: 0.5,
            cursor: 'pointer', color: tokens.colorPrimary, flexShrink: 0,
            lineHeight: 1.4, transition: 'opacity 0.15s',
            '&:hover': { bgcolor: tokens.colorPrimaryBg },
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
                component="span"
                onClick={() => onAssignToRows(level)}
                sx={{
                  opacity: 0, fontSize: 10, px: '5px', py: '2px', borderRadius: 0.5,
                  cursor: 'pointer', color: tokens.colorPrimary, lineHeight: 1.4,
                  transition: 'opacity 0.15s',
                  '&:hover': { bgcolor: tokens.colorPrimaryBg },
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
