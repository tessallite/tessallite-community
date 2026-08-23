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
  onInsertMeasureAsFunction,
  onInsertMeasureAsFormula,
  expanded,
  onToggleExpanded,
  loading,
  glossaryEntries,
}: MeasureLibraryProps) {
  const groupedMeasures = useMemo(() => {
    const grouped = new Map<string, Measure[]>();
    const ungrouped: Measure[] = [];
    const variantIdx = new Map<string, Measure[]>();

    for (const m of measures) {
      if (m.base_measure_id) {
        const variants = variantIdx.get(m.base_measure_id) || [];
        variants.push(m);
        variantIdx.set(m.base_measure_id, variants);
        continue;
      }
      if (m.display_folder) {
        const group = grouped.get(m.display_folder) || [];
        group.push(m);
        grouped.set(m.display_folder, group);
      } else {
        ungrouped.push(m);
      }
    }

    const result: { folder?: string; measures: Measure[]; variants: Measure[] }[] = [];

    for (const [folder, list] of grouped) {
      result.push({ folder, measures: list, variants: [] });
    }
    if (ungrouped.length > 0) {
      result.push({ measures: ungrouped, variants: [] });
    }

    for (const entry of result) {
      for (const m of entry.measures) {
        const vars = variantIdx.get(m.id);
        if (vars) entry.variants.push(...vars);
      }
    }

    return result;
  }, [measures]);

  return (
    <>
      <Box
        sx={{
          display: 'flex', alignItems: 'center', px: 1.5, py: 0.75,
          cursor: measures.length > 0 || loading ? 'pointer' : 'default',
          borderTop: `1px solid ${tokens.colorBorderLight}`,
          opacity: measures.length > 0 || loading ? 1 : 0.68,
        }}
        onClick={() => {
          if (measures.length > 0 || loading || searchQuery) onToggleExpanded();
        }}
      >
        <Typography sx={{ fontSize: 12, fontWeight: 700, color: tokens.colorCharcoal, flex: 1 }}>
          {templates.library.measuresHeader(measures.length)}
        </Typography>
        {/* Bug-6710: keyboard path to expand/collapse (header Box is mouse-only). */}
        {(measures.length > 0 || loading || searchQuery) && (
          <IconButton
            size="small"
            onClick={(e) => { e.stopPropagation(); onToggleExpanded(); }}
            aria-expanded={expanded}
            aria-label={templates.library.toggleSectionAria(expanded, strings.library.measuresSection)}
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
            <Skeleton variant="rectangular" height={48} sx={{ mb: 0.5, borderRadius: 1 }} />
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
                  px: 1.5, py: 0.5, textTransform: 'uppercase', bgcolor: tokens.colorSubtleFill,
                }}>
                  {group.folder}
                </Typography>
              )}
              {group.measures.map(m => (
                <Box key={m.id}>
                  <MeasureCard
                    measure={m}
                    checked={selectedMeasureIds.includes(m.id)}
                    onToggle={() => onToggleMeasure(m.id)}
                    onAddToValues={() => onAddToValues(m.id)}
                    onInsertAsFunction={onInsertMeasureAsFunction ? () => onInsertMeasureAsFunction(m.id) : undefined}
                    onInsertAsFormula={onInsertMeasureAsFormula ? () => onInsertMeasureAsFormula(m.id) : undefined}
                    glossaryEntries={glossaryEntries}
                  />
                  {group.variants.filter(v => v.base_measure_id === m.id).map(v => (
                    <Box key={v.id} sx={{ ml: 2 }}>
                      <MeasureCard
                        measure={v}
                        checked={selectedMeasureIds.includes(v.id)}
                        onToggle={() => onToggleMeasure(v.id)}
                        onAddToValues={() => onAddToValues(v.id)}
                        onInsertAsFormula={onInsertMeasureAsFormula ? () => onInsertMeasureAsFormula(v.id) : undefined}
                        glossaryEntries={glossaryEntries}
                      />
                    </Box>
                  ))}
                </Box>
              ))}
            </Box>
          ))
        )}
      </Collapse>
    </>
  );
}
