import { useMemo } from 'react';
import { Box, Typography, Collapse, Skeleton, IconButton } from '@mui/material';
import { ExpandLess, ExpandMore } from '@mui/icons-material';
import { tokens } from '../../theme';
import { strings, templates } from '../../i18n/strings';
import type { NamedSet } from '../../types/tessallite';
import NamedSetCard from './NamedSetCard';

interface NamedSetLibraryProps {
  namedSets: NamedSet[];
  searchQuery: string;
  projectId: string;
  modelId: string;
  personaId?: string;
  onAddToRows: (ns: NamedSet) => void;
  onAddToColumns: (ns: NamedSet) => void;
  onAddToFilter: (ns: NamedSet) => void;
  onInsertAsFormulas?: (ns: NamedSet) => void;
  expanded: boolean;
  onToggleExpanded: () => void;
  loading?: boolean;
}

export default function NamedSetLibrary({
  namedSets,
  searchQuery,
  projectId,
  modelId,
  personaId,
  onAddToRows,
  onAddToColumns,
  onAddToFilter,
  onInsertAsFormulas,
  expanded,
  onToggleExpanded,
  loading,
}: NamedSetLibraryProps) {
  const groupedSets = useMemo(() => {
    const grouped = new Map<string, NamedSet[]>();
    const ungrouped: NamedSet[] = [];

    for (const ns of namedSets) {
      if (ns.display_folder) {
        const group = grouped.get(ns.display_folder) || [];
        group.push(ns);
        grouped.set(ns.display_folder, group);
      } else {
        ungrouped.push(ns);
      }
    }

    const result: { folder?: string; sets: NamedSet[] }[] = [];
    for (const [folder, list] of grouped) {
      result.push({ folder, sets: list });
    }
    if (ungrouped.length > 0) {
      result.push({ sets: ungrouped });
    }
    return result;
  }, [namedSets]);

  return (
    <>
      <Box
        sx={{
          display: 'flex', alignItems: 'center', px: 1.5, py: 0.75,
          cursor: namedSets.length > 0 || loading ? 'pointer' : 'default',
          borderTop: `1px solid ${tokens.colorBorderLight}`,
          opacity: namedSets.length > 0 || loading ? 1 : 0.68,
        }}
        onClick={() => {
          if (namedSets.length > 0 || loading || searchQuery) onToggleExpanded();
        }}
      >
        <Typography sx={{ fontSize: 12, fontWeight: 700, color: tokens.colorCharcoal, flex: 1 }}>
          {templates.library.namedSetsHeader(namedSets.length)}
        </Typography>
        {/* Bug-6710: keyboard path to expand/collapse (header Box is mouse-only). */}
        {(namedSets.length > 0 || loading || searchQuery) && (
          <IconButton
            size="small"
            onClick={(e) => { e.stopPropagation(); onToggleExpanded(); }}
            aria-expanded={expanded}
            aria-label={templates.library.toggleSectionAria(expanded, strings.library.namedSetsSection)}
            sx={{ width: 24, height: 24, color: tokens.colorTextSecondary }}
          >
            {expanded ? <ExpandLess sx={{ fontSize: 16 }} /> : <ExpandMore sx={{ fontSize: 16 }} />}
          </IconButton>
        )}
      </Box>
      <Collapse in={expanded}>
        {loading ? (
          <Box sx={{ p: 1 }}>
            <Skeleton variant="rectangular" height={48} sx={{ mb: 0.5, borderRadius: 1 }} />
            <Skeleton variant="rectangular" height={48} sx={{ mb: 0.5, borderRadius: 1 }} />
          </Box>
        ) : namedSets.length === 0 ? (
          <Typography sx={{ fontSize: 11, color: tokens.colorTextSecondary, px: 1.5, py: 1 }}>
            {searchQuery ? strings.namedSetLibrary.noSearchMatch : strings.namedSetLibrary.noItemsAvailable}
          </Typography>
        ) : (
          groupedSets.map((group, gi) => (
            <Box key={gi}>
              {group.folder && (
                <Typography sx={{
                  fontSize: 10, fontWeight: 600, color: tokens.colorTextSecondary,
                  px: 1.5, py: 0.5, textTransform: 'uppercase', bgcolor: tokens.colorSubtleFill,
                }}>
                  {group.folder}
                </Typography>
              )}
              {group.sets.map(ns => (
                <NamedSetCard
                  key={ns.id}
                  namedSet={ns}
                  projectId={projectId}
                  modelId={modelId}
                  personaId={personaId}
                  onAddToRows={() => onAddToRows(ns)}
                  onAddToColumns={() => onAddToColumns(ns)}
                  onAddToFilter={() => onAddToFilter(ns)}
                  onInsertAsFormulas={onInsertAsFormulas ? () => onInsertAsFormulas(ns) : undefined}
                />
              ))}
            </Box>
          ))
        )}
      </Collapse>
    </>
  );
}
