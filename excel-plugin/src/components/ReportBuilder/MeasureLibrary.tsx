import { useMemo } from 'react';
import { Box, Typography, Collapse, Skeleton, IconButton } from '@mui/material';
import { ExpandLess, ExpandMore } from '@mui/icons-material';
import { tokens } from '../../theme';
import { strings, templates } from '../../i18n/strings';
import type { Measure, GlossaryEntry } from '../../types/tessallite';
import MeasureCard from './MeasureCard';

interface MeasureLibraryProps {
  measures: Measure[];
  searchQuery: string;
  selectedMeasureIds: string[];
  onToggleMeasure: (measureId: string) => void;
  onAddToValues: (measureId: string) => void;
  /** Bug-9747: add a HAVING-style filter on the measure's aggregated value. */
  onAddToFilter?: (measureId: string) => void;
  /** Phase A default: insert as TESSALLITE.VALUE() formula (connectionless). */
  onInsertMeasureAsFunction?: (measureId: string) => void;
  /** Advanced: insert as CUBEVALUE formula (requires workbook connection). */
  onInsertMeasureAsFormula?: (measureId: string) => void;
  expanded: boolean;
  onToggleExpanded: () => void;
  loading?: boolean;
  glossaryEntries?: GlossaryEntry[];
}

export default function MeasureLibrary({
  measures,
  searchQuery,
  selectedMeasureIds,
  onToggleMeasure,
  onAddToValues,
  onAddToFilter,
  onInsertMeasureAsFunction,
  onInsertMeasureAsFormula,
  expanded,
  onToggleExpanded,
  loading,
  glossaryEntries,
}: MeasureLibraryProps) {
  const groupedMeasures = useMemo(() => {
    // Bug-9882: this used to also nest time variants under their base measure,
    // keyed on `base_measure_id` — a field the producer never sends, so the
    // branch never ran and every variant has always rendered as an ordinary
    // measure. Removed rather than repointed at the real field
    // (`variant_of_measure_id`): nesting makes a variant unreachable whenever
    // the search filters its BASE out of the list, so a user searching "cagr"
    // would find nothing. Nesting that survives search is a UI design task,
    // tracked on Bug-9882, not a rename.
    const grouped = new Map<string, Measure[]>();
    const ungrouped: Measure[] = [];

    for (const m of measures) {
      if (m.display_folder) {
        const group = grouped.get(m.display_folder) || [];
        group.push(m);
        grouped.set(m.display_folder, group);
      } else {
        ungrouped.push(m);
      }
    }

    const result: { folder?: string; measures: Measure[] }[] = [];
    for (const [folder, list] of grouped) result.push({ folder, measures: list });
    if (ungrouped.length > 0) result.push({ measures: ungrouped });

    return result;
  }, [measures]);

  return (
    <>
      <Box
        sx={{
          display: 'flex', alignItems: 'center', px: 1.25, height: 24,
          cursor: measures.length > 0 || loading ? 'pointer' : 'default',
          borderTop: `1px solid ${tokens.colorBorderLight}`,
          borderBottom: `1px solid ${tokens.colorBorderLight}`,
          bgcolor: tokens.colorSubtleFill,
          opacity: measures.length > 0 || loading ? 1 : 0.68,
        }}
        onClick={() => {
          if (measures.length > 0 || loading || searchQuery) onToggleExpanded();
        }}
      >
        <Typography sx={{ fontSize: 10, fontWeight: 700, color: tokens.colorTextSecondary, flex: 1, textTransform: 'uppercase', letterSpacing: '0.03em' }}>
          {strings.library.measuresSection}
        </Typography>
        <Typography sx={{ fontSize: 10, color: tokens.colorTextSecondary }}>{measures.length}</Typography>
        {/* Bug-6710: keyboard path to expand/collapse (header Box is mouse-only). */}
        {(measures.length > 0 || loading || searchQuery) && (
          <IconButton
            size="small"
            onClick={(e) => { e.stopPropagation(); onToggleExpanded(); }}
            aria-expanded={expanded}
            aria-label={templates.library.toggleSectionAria(expanded, strings.library.measuresSection)}
            sx={{ width: 22, height: 22, color: tokens.colorTextSecondary }}
          >
            {expanded ? <ExpandLess sx={{ fontSize: 16 }} /> : <ExpandMore sx={{ fontSize: 16 }} />}
          </IconButton>
        )}
      </Box>
      <Collapse in={expanded}>
        {loading ? (
          <Box sx={{ p: 0.5 }}>
            <Skeleton variant="rectangular" height={28} sx={{ mb: 0.25, borderRadius: 0.5 }} />
            <Skeleton variant="rectangular" height={28} sx={{ mb: 0.25, borderRadius: 0.5 }} />
            <Skeleton variant="rectangular" height={28} sx={{ mb: 0.25, borderRadius: 0.5 }} />
          </Box>
        ) : measures.length === 0 ? (
          <Typography sx={{ fontSize: 11, color: tokens.colorTextSecondary, px: 1.5, py: 1 }}>
            {searchQuery ? strings.measureLibrary.noSearchMatch : strings.measureLibrary.noItemsAvailable}
          </Typography>
        ) : (
          groupedMeasures.map((group, gi) => (
            <Box key={gi}>
              {group.folder && (
                <Typography sx={{
                  fontSize: 10, fontWeight: 600, color: tokens.colorTextSecondary,
                  px: 1.25, py: 0.25, minHeight: 20, textTransform: 'uppercase', bgcolor: tokens.colorSubtleFill,
                }}>
                  {group.folder}
                </Typography>
              )}
              {group.measures.map(m => (
                <MeasureCard
                  key={m.id}
                  measure={m}
                  checked={selectedMeasureIds.includes(m.id)}
                  onToggle={() => onToggleMeasure(m.id)}
                  onAddToValues={() => onAddToValues(m.id)}
                  onAddToFilter={onAddToFilter ? () => onAddToFilter(m.id) : undefined}
                  onInsertAsFunction={onInsertMeasureAsFunction ? () => onInsertMeasureAsFunction(m.id) : undefined}
                  onInsertAsFormula={onInsertMeasureAsFormula ? () => onInsertMeasureAsFormula(m.id) : undefined}
                  glossaryEntries={glossaryEntries}
                />
              ))}
            </Box>
          ))
        )}
      </Collapse>
    </>
  );
}
