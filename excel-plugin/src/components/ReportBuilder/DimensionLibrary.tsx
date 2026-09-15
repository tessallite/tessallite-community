import { Box, Typography, Collapse, CircularProgress, Skeleton, IconButton } from '@mui/material';
import { ExpandLess, ExpandMore } from '@mui/icons-material';
import { tokens } from '../../theme';
import { strings, templates } from '../../i18n/strings';
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
          display: 'flex', alignItems: 'center', px: 1.25, height: 24,
          cursor: dimensions.length > 0 || loading ? 'pointer' : 'default',
          borderTop: `1px solid ${tokens.colorBorderLight}`,
          borderBottom: `1px solid ${tokens.colorBorderLight}`,
          bgcolor: tokens.colorSubtleFill,
          opacity: dimensions.length > 0 || loading ? 1 : 0.68,
        }}
        onClick={() => {
          if (dimensions.length > 0 || loading || searchQuery) onToggleExpanded();
        }}
      >
        <Typography sx={{ fontSize: 10, fontWeight: 700, color: tokens.colorTextSecondary, flex: 1, textTransform: 'uppercase', letterSpacing: '0.03em' }}>
          {strings.library.dimensionsSection}
        </Typography>
        <Typography sx={{ fontSize: 10, color: tokens.colorTextSecondary }}>{dimensions.length}</Typography>
        {/* Bug-6710: keyboard path to expand/collapse (header Box is mouse-only). */}
        {(dimensions.length > 0 || loading || searchQuery) && (
          <IconButton
            size="small"
            onClick={(e) => { e.stopPropagation(); onToggleExpanded(); }}
            aria-expanded={expanded}
            aria-label={templates.library.toggleSectionAria(expanded, strings.library.dimensionsSection)}
            sx={{ width: 22, height: 22, color: tokens.colorTextSecondary }}
          >
            {expanded ? <ExpandLess sx={{ fontSize: 16 }} /> : <ExpandMore sx={{ fontSize: 16 }} />}
          </IconButton>
        )}
      </Box>
      {/* Bug-9752 round 3: no `overflow: visible` override. MUI already sets
          `overflow: visible` on an ENTERED Collapse, so nothing inside an open
          section is clipped; forcing it in every state only let the collapsed
          and mid-animation content paint outside the box, over the sections
          below. The other four library sections never overrode it. */}
      <Collapse in={expanded}>
        {loading ? (
          <Box sx={{ p: 0.5 }}>
            <Skeleton variant="rectangular" height={30} sx={{ mb: 0.25, borderRadius: 0.5 }} />
            <Skeleton variant="rectangular" height={30} sx={{ mb: 0.25, borderRadius: 0.5 }} />
            <Skeleton variant="rectangular" height={30} sx={{ mb: 0.25, borderRadius: 0.5 }} />
          </Box>
        ) : dimensions.length === 0 ? (
          <Typography sx={{ fontSize: 11, color: tokens.colorTextSecondary, px: 1.5, py: 1 }}>
            {searchQuery ? strings.dimensionLibrary.noSearchMatch : strings.dimensionLibrary.noItemsAvailable}
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
              <Box sx={{ mx: 0.5, mb: 0.5, p: 0.75, bgcolor: tokens.colorSubtleFill, borderRadius: 0.5, maxHeight: 160, overflowY: 'auto' }}>
                <Box sx={{ display: 'flex', alignItems: 'center', gap: 0.5, mb: 0.5 }}>
                  <Typography sx={{ fontSize: 10, fontWeight: 600, color: tokens.colorTextSecondary, flex: 1 }}>
                    {strings.dimensionLibrary.membersLabel}
                  </Typography>
                  {/* Bug-6713: was a mouse-only span; a native button gives
                      focus + Enter/Space activation. */}
                  <Box
                    component="button"
                    onClick={onCloseMemberPreview}
                    aria-label={strings.dimensionLibrary.closeMemberPreviewAria}
                    sx={{
                      fontSize: 9, color: tokens.colorPrimary, cursor: 'pointer',
                      border: 'none', bgcolor: 'transparent', p: 0, fontFamily: 'inherit',
                    }}
                  >
                    {strings.dimensionLibrary.closeMemberPreview}
                  </Box>
                </Box>
                {membersPreviewLoading ? (
                  <CircularProgress size={14} sx={{ color: tokens.colorPrimary }} />
                ) : memberPreview && memberPreview.members.length > 0 ? (
                  <Box sx={{ display: 'flex', flexWrap: 'wrap', gap: 0.25 }}>
                  {memberPreview.members.slice(0, 20).map(m => (
                    <Box key={m.key} component="span" sx={{ px: 0.5, py: 0.125, bgcolor: tokens.colorWhite, border: `1px solid ${tokens.colorBorderLight}`, borderRadius: 0.5, fontSize: 10, color: tokens.colorCharcoal }}>
                      {m.name}
                    </Box>
                  ))}
                  </Box>
                ) : (
                  <Typography sx={{ fontSize: 10, color: tokens.colorTextSecondary }}>{strings.dimensionLibrary.noMembersFound}</Typography>
                )}
              </Box>
            )}
          </Box>
        )}
      </Collapse>
    </>
  );
}
