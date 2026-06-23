import { Box, Typography, Collapse } from '@mui/material';
import { ExpandMore, ExpandLess } from '@mui/icons-material';
import { tokens } from '../../theme';
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
          Hierarchies ({hierarchies?.length ?? 0})
        </Typography>
        {hasItems && (
          expanded ? <ExpandLess sx={{ fontSize: 16, color: tokens.colorTextSecondary }} /> : <ExpandMore sx={{ fontSize: 16, color: tokens.colorTextSecondary }} />
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
