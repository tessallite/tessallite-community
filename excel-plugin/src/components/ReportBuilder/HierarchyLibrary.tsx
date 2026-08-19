import { Box, Typography, Collapse, IconButton } from '@mui/material';
import { ExpandMore, ExpandLess } from '@mui/icons-material';
import { tokens } from '../../theme';
import { strings, templates } from '../../i18n/strings';
import HierarchyCard from './HierarchyCard';
import type { Hierarchy, HierarchyLevel } from '../../types/tessallite';

interface HierarchyLibraryProps {
  hierarchies: Hierarchy[];
  expanded: boolean;
  onToggle: () => void;
  onAssignToRows: (hierarchy: Hierarchy, level?: HierarchyLevel) => void;
}

export default function HierarchyLibrary({
  hierarchies,
  expanded,
  onToggle,
  onAssignToRows,
}: HierarchyLibraryProps) {
  const hasItems = hierarchies && hierarchies.length > 0;

  return (
    <>
      <Box
        sx={{
          display: 'flex', alignItems: 'center', px: 1.5, py: 0.75,
          cursor: hasItems ? 'pointer' : 'default',
          borderTop: `1px solid ${tokens.colorBorderLight}`,
          opacity: hasItems ? 1 : 0.68,
        }}
        onClick={() => { if (hasItems) onToggle(); }}
      >
        <Typography sx={{ fontSize: 12, fontWeight: 700, color: tokens.colorCharcoal, flex: 1 }}>
          {templates.library.hierarchiesHeader(hierarchies?.length ?? 0)}
        </Typography>
        {/* Bug-6710: keyboard path to expand/collapse (header Box is mouse-only). */}
        {hasItems && (
          <IconButton
            size="small"
            onClick={(e) => { e.stopPropagation(); onToggle(); }}
            aria-expanded={expanded}
            aria-label={templates.library.toggleSectionAria(expanded, strings.library.hierarchiesSection)}
            sx={{ width: 24, height: 24, color: tokens.colorTextSecondary }}
          >
            {expanded ? <ExpandLess sx={{ fontSize: 16 }} /> : <ExpandMore sx={{ fontSize: 16 }} />}
          </IconButton>
        )}
      </Box>
      <Collapse in={expanded}>
        <Box sx={{ px: 0.5 }}>
          {hierarchies.map(h => (
            <Box key={h.id} sx={{ mb: 0.5 }}>
              <HierarchyCard
                hierarchy={h}
                onAssignToRows={(level) => onAssignToRows(h, level)}
              />
            </Box>
          ))}
        </Box>
      </Collapse>
    </>
  );
}
