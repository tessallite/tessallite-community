import { Box, Typography, Collapse, CircularProgress, Skeleton } from '@mui/material';
import { ExpandLess, ExpandMore } from '@mui/icons-material';
import { tokens } from '../../theme';
import type { Dimension, Zone, DiscoverMembersResponse } from '../../types/tessallite';
import DimensionCard from './DimensionCard';
import type { DimensionCompatibilityState } from '../../utils/fieldCompatibility';

interface DimensionLibraryProps {
  dimensions: Dimension[];
  searchQuery: string;
  onAddToRows: (dimensionId: string) => void;
  onAddToColumns: (dimensionId: string) => void;
  onAddToFilter: (dimensionId: string) => void;
  onPreviewMembers: (dimensionId: string) => void;
  expanded: boolean;
  onToggleExpanded: () => void;
  loading?: boolean;
  memberPreviewDimId: string | null;
  memberPreview: DiscoverMembersResponse | null;
  membersPreviewLoading: boolean;
  onCloseMemberPreview: () => void;
  compatibilityByDimensionId?: Record<string, DimensionCompatibilityState>;
}

export default function DimensionLibrary({
  dimensions,
  searchQuery,
  onAddToRows,
  onAddToColumns,
  onAddToFilter,
  onPreviewMembers,
  expanded,
  onToggleExpanded,
  loading,
  memberPreviewDimId,
  memberPreview,
  membersPreviewLoading,
  onCloseMemberPreview,
  compatibilityByDimensionId,
}: DimensionLibraryProps) {
  return (
    <>
      <Box
        sx={{
          display: 'flex', alignItems: 'center', px: 1.5, py: 0.75,
          cursor: dimensions.length > 0 || loading ? 'pointer' : 'default',
          borderTop: `1px solid ${tokens.colorBorderLight}`,
          opacity: dimensions.length > 0 || loading ? 1 : 0.68,
        }}
        onClick={() => {
          if (dimensions.length > 0 || loading || searchQuery) onToggleExpanded();
        }}
      >
        <Typography sx={{ fontSize: 12, fontWeight: 700, color: tokens.colorCharcoal, flex: 1 }}>
          Dimensions ({dimensions.length})
        </Typography>
        {(dimensions.length > 0 || loading || searchQuery) && (
          expanded ? <ExpandLess sx={{ fontSize: 16, color: tokens.colorTextSecondary }} /> : <ExpandMore sx={{ fontSize: 16, color: tokens.colorTextSecondary }} />
        )}
      </Box>
      <Collapse in={expanded} sx={{ overflow: 'visible' }}>
        {loading ? (
          <Box sx={{ p: 0.5 }}>
            <Skeleton variant="rectangular" height={64} sx={{ mb: 0.5, borderRadius: 1 }} />
            <Skeleton variant="rectangular" height={64} sx={{ mb: 0.5, borderRadius: 1 }} />
            <Skeleton variant="rectangular" height={64} sx={{ mb: 0.5, borderRadius: 1 }} />
          </Box>
        ) : dimensions.length === 0 ? (
          <Typography sx={{ fontSize: 11, color: tokens.colorTextSecondary, px: 1.5, py: 1 }}>
            {searchQuery ? 'No dimensions match your search' : 'No dimensions available'}
          </Typography>
        ) : (
          <Box sx={{ px: 0.5 }}>
            {dimensions.map(d => (
              <DimensionCard
                key={d.id}
                id={d.id}
                displayName={d.display_name}
                description={d.effective_description}
                dataType={d.data_type}
                sourceType={d.source_type}
                isTimeDimension={d.is_time_dimension}
                calendarType={d.calendar_type}
                compatibility={compatibilityByDimensionId?.[d.id]}
                onAssign={(zone) => {
                  const z: Zone = zone === 'filter' || zone === 'slicer' ? 'filters' : zone;
                  if (z === 'rows') onAddToRows(d.id);
                  else if (z === 'columns') onAddToColumns(d.id);
                  else if (z === 'filters') onAddToFilter(d.id);
                }}
                onPreviewMembers={() => onPreviewMembers(d.id)}
              />
            ))}
            {memberPreviewDimId && (
              <Box sx={{ mx: 0.5, mb: 1, p: 1, bgcolor: tokens.colorSubtleFill, borderRadius: 1, maxHeight: 160, overflowY: 'auto' }}>
                <Box sx={{ display: 'flex', alignItems: 'center', gap: 0.5, mb: 0.5 }}>
                  <Typography sx={{ fontSize: 10, fontWeight: 600, color: tokens.colorTextSecondary, flex: 1 }}>
                    Members
                  </Typography>
                  <Box component="span" sx={{ fontSize: 9, color: tokens.colorPrimary, cursor: 'pointer' }} onClick={onCloseMemberPreview}>
                    Close
                  </Box>
                </Box>
                {membersPreviewLoading ? (
                  <CircularProgress size={14} sx={{ color: tokens.colorPrimary }} />
                ) : memberPreview && memberPreview.members.length > 0 ? (
                  memberPreview.members.slice(0, 20).map(m => (
                    <Typography key={m.key} sx={{ fontSize: 10, color: tokens.colorCharcoal }}>{m.name}</Typography>
                  ))
                ) : (
                  <Typography sx={{ fontSize: 10, color: tokens.colorTextSecondary }}>No members found</Typography>
                )}
              </Box>
            )}
          </Box>
        )}
      </Collapse>
    </>
  );
}
