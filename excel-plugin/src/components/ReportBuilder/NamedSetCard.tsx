import { useState } from 'react';
import { Box, Typography, Chip, CircularProgress, Collapse, IconButton } from '@mui/material';
import {
  Add as AddIcon,
  Visibility as PreviewIcon,
  InfoOutlined,
  KeyboardArrowUpOutlined,
} from '@mui/icons-material';
import { tokens } from '../../theme';
import type { NamedSet, NamedSetPreviewResponse } from '../../types/tessallite';
import { previewNamedSet } from '../../api/modelService';
import { ApiError } from '../../api/client';
import { strings } from '../../i18n/strings';

interface NamedSetCardProps {
  namedSet: NamedSet;
  projectId: string;
  modelId: string;
  personaId?: string;
  onAddToRows: () => void;
  onAddToColumns: () => void;
  onAddToFilter: () => void;
  onInsertAsFormulas?: () => void;
}

const LIST_TYPE_LABELS: Record<string, string> = {
  fixed: 'Fixed',
  dynamic_top_n: 'Dynamic',
  filtered: 'Filtered',
  advanced_mdx: 'MDX',
};

const BUILDER_TYPE_LABELS: Record<string, string> = {
  fixedMembers: 'Fixed',
  fixed: 'Fixed',
  topN: 'Dynamic',
  dynamic_top_n: 'Dynamic',
  filter: 'Filtered',
  filtered: 'Filtered',
};

function explainDefinition(def: Record<string, unknown> | null): string | null {
  if (!def) return null;
  const type = def.type as string;
  if (type === 'fixedMembers' || type === 'fixed') {
    const members = def.members as unknown[];
    const dim = def.dimension as string;
    return `${members?.length ?? 0} member(s) from ${dim}`;
  }
  if (type === 'topN' || type === 'dynamic_top_n') {
    const dir = def.direction === 'bottom' ? 'Bottom' : 'Top';
    return `${dir} ${def.count} ${def.entity} by ${def.measure}`;
  }
  if (type === 'filter' || type === 'filtered') {
    const conds = def.conditions as unknown[];
    return `${def.entity} matching ${conds?.length ?? 0} condition(s)`;
  }
  return null;
}

export default function NamedSetCard({
  namedSet,
  projectId,
  modelId,
  personaId,
  onAddToRows,
  onAddToColumns,
  onAddToFilter,
  onInsertAsFormulas,
}: NamedSetCardProps) {
  const [previewOpen, setPreviewOpen] = useState(false);
  const [preview, setPreview] = useState<NamedSetPreviewResponse | null>(null);
  const [previewLoading, setPreviewLoading] = useState(false);
  // Bug-8712: the preview used to swallow every failure and render the generic
  // "No preview data available", which is indistinguishable from a genuinely
  // empty set. The fail-closed 409 and the not-published 404 both need to say
  // what happened and what to do about it.
  const [previewError, setPreviewError] = useState<string | null>(null);
  const [detailsOpen, setDetailsOpen] = useState(false);

  const typeLabel =
    LIST_TYPE_LABELS[namedSet.list_type ?? ''] ??
    (namedSet.builder_definition ? BUILDER_TYPE_LABELS[namedSet.builder_definition.type as string] : null) ??
    'Set';

  const explanation = explainDefinition(namedSet.builder_definition);

  const handlePreviewToggle = async () => {
    if (previewOpen) {
      setPreviewOpen(false);
      return;
    }
    setPreviewOpen(true);
    if (!preview) {
      setPreviewLoading(true);
      setPreviewError(null);
      try {
        const result = await previewNamedSet(projectId, modelId, namedSet.id, personaId);
        setPreview(result);
      } catch (e) {
        setPreview(null);
        // The add-in previews the PUBLISHED definition. 404 here means the set
        // exists but is not in the deployed version; 409 means the deployed
        // version itself cannot be read. Neither may fall back to the draft.
        setPreviewError(
          e instanceof ApiError && (e.status === 404 || e.status === 409)
            ? strings.namedSetLibrary.previewNotPublished
            : strings.namedSetLibrary.previewFailed,
        );
      } finally {
        setPreviewLoading(false);
      }
    }
  };

  const detailRows = [
    namedSet.description && { label: 'Description', value: namedSet.description },
    explanation && { label: 'Definition', value: explanation },
    namedSet.display_folder && { label: 'Folder', value: namedSet.display_folder },
    namedSet.certification_status !== 'draft' && { label: 'Status', value: namedSet.certification_status },
  ].filter(Boolean) as { label: string; value: string }[];

  return (
    <>
      <Box
        sx={{
          display: 'flex', alignItems: 'center', gap: 0.75,
          px: 1.25, py: 0.5, minHeight: 36, cursor: 'pointer',
          borderBottom: `1px solid ${tokens.colorBorderLight}`,
          '&:hover': { bgcolor: tokens.colorSubtleFill },
        }}
      >
          <Typography sx={{ fontSize: 14, lineHeight: 1, flexShrink: 0 }}>
            {'\u{1F4CB}'}
          </Typography>
          <Box sx={{ flex: 1, overflow: 'hidden' }}>
            <Typography
              sx={{
                fontSize: 13, fontWeight: 400,
                color: namedSet.certification_status === 'deprecated' ? tokens.colorTextSecondary : tokens.colorCharcoal,
                textDecoration: namedSet.certification_status === 'deprecated' ? 'line-through' : 'none',
                overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap',
              }}
            >
              {namedSet.display_name || namedSet.name}
            </Typography>
            {explanation && (
              <Typography sx={{ fontSize: 10, color: tokens.colorTextSecondary, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                {explanation}
              </Typography>
            )}
          </Box>
          <Chip
            label={typeLabel}
            size="small"
            sx={{ fontSize: 9, height: 16, bgcolor: tokens.colorSubtleFill, color: tokens.colorTextSecondary, fontWeight: 600, flexShrink: 0 }}
          />
          {namedSet.certification_status === 'certified' && (
            <Chip
              label="Certified"
              size="small"
              sx={{ fontSize: 9, height: 16, bgcolor: 'rgba(46,125,50,0.08)', color: '#2e7d32', fontWeight: 600, flexShrink: 0 }}
            />
          )}
          {namedSet.certification_status === 'deprecated' && (
            <Chip
              label="Deprecated"
              size="small"
              sx={{ fontSize: 9, height: 16, bgcolor: 'rgba(237,108,2,0.08)', color: '#ed6c02', fontWeight: 600, flexShrink: 0 }}
            />
          )}
          {detailRows.length > 0 && (
            <IconButton
              size="small"
              onClick={(e) => { e.stopPropagation(); setDetailsOpen(v => !v); }}
              title={detailsOpen ? 'Hide details' : 'Show details'}
              aria-label={`${detailsOpen ? 'Hide' : 'Show'} details for ${namedSet.display_name || namedSet.name}`}
              sx={{
                width: 28,
                height: 28,
                color: detailsOpen ? tokens.colorPrimary : tokens.colorTextSecondary,
                '&:hover': { bgcolor: tokens.colorSubtleFill },
              }}
            >
              {detailsOpen ? <KeyboardArrowUpOutlined sx={{ fontSize: 16 }} /> : <InfoOutlined sx={{ fontSize: 15 }} />}
            </IconButton>
          )}
          <Box
            className="ns-actions"
            sx={{ display: 'flex', alignItems: 'center', gap: 0.25, flexShrink: 0 }}
          >
            <Box
              component="span"
              onClick={(e) => { e.stopPropagation(); handlePreviewToggle(); }}
              title="Preview members"
              sx={{
                display: 'flex', alignItems: 'center',
                p: '2px', borderRadius: 0.5, color: tokens.colorTextSecondary,
                '&:hover': { bgcolor: tokens.colorPrimaryBg, color: tokens.colorPrimary },
              }}
            >
              <PreviewIcon sx={{ fontSize: 14 }} />
            </Box>
            <Box
              component="span"
              onClick={(e) => { e.stopPropagation(); onAddToRows(); }}
              title="Add to Rows"
              sx={{
                display: 'flex', alignItems: 'center',
                p: '2px', borderRadius: 0.5, color: tokens.colorPrimary,
                '&:hover': { bgcolor: tokens.colorPrimaryBg },
              }}
            >
              <AddIcon sx={{ fontSize: 14 }} />
            </Box>
          </Box>
      </Box>

      <Collapse in={detailsOpen}>
        <Box sx={{ px: 2, py: 1, bgcolor: tokens.colorSubtleFill, borderBottom: `1px solid ${tokens.colorBorderLight}` }}>
          {detailRows.map(row => (
            <Box key={row.label} sx={{ mb: 0.6 }}>
              <Typography sx={{ fontSize: 10, fontWeight: 700, color: tokens.colorTextSecondary, textTransform: 'uppercase', lineHeight: 1.2 }}>
                {row.label}
              </Typography>
              <Typography sx={{ fontSize: 11.5, color: tokens.colorCharcoal, lineHeight: 1.35 }}>
                {row.value}
              </Typography>
            </Box>
          ))}
        </Box>
      </Collapse>

      {/* Inline preview panel */}
      {previewOpen && (
        <Box sx={{ px: 2, py: 1, bgcolor: tokens.colorSubtleFill, borderBottom: `1px solid ${tokens.colorBorderLight}` }}>
          {previewLoading ? (
            <Box sx={{ display: 'flex', alignItems: 'center', gap: 1 }}>
              <CircularProgress size={12} />
              <Typography sx={{ fontSize: 11, color: tokens.colorTextSecondary }}>Loading preview...</Typography>
            </Box>
          ) : preview && preview.items.length > 0 ? (
            <>
              {preview.explanation && (
                <Typography sx={{ fontSize: 10, color: tokens.colorTextSecondary, mb: 0.5 }}>
                  {preview.explanation}
                </Typography>
              )}
              {preview.items.slice(0, 20).map((item) => (
                <Typography key={item.ordinal} sx={{ fontSize: 11, color: tokens.colorCharcoal, py: 0.125 }}>
                  {item.ordinal}. {item.caption}
                </Typography>
              ))}
              {preview.total_count > 20 && (
                <Typography sx={{ fontSize: 10, color: tokens.colorTextSecondary, mt: 0.5 }}>
                  ...and {preview.total_count - 20} more
                </Typography>
              )}
              {preview.truncated && (
                <Typography sx={{ fontSize: 10, color: tokens.colorGoldDark, mt: 0.25 }}>
                  Results truncated (100 max)
                </Typography>
              )}
              <Box sx={{ display: 'flex', gap: 1, mt: 0.75, flexWrap: 'wrap' }}>
                <Box
                  component="button"
                  onClick={onAddToRows}
                  sx={{
                    fontSize: 10, px: 0.75, py: 0.25, borderRadius: 0.5, cursor: 'pointer',
                    border: `1px solid ${tokens.colorPrimary}`, color: tokens.colorPrimary,
                    bgcolor: 'transparent', '&:hover': { bgcolor: tokens.colorPrimaryBg },
                  }}
                >
                  Insert as Rows
                </Box>
                <Box
                  component="button"
                  onClick={onAddToColumns}
                  sx={{
                    fontSize: 10, px: 0.75, py: 0.25, borderRadius: 0.5, cursor: 'pointer',
                    border: `1px solid ${tokens.colorPrimary}`, color: tokens.colorPrimary,
                    bgcolor: 'transparent', '&:hover': { bgcolor: tokens.colorPrimaryBg },
                  }}
                >
                  Insert as Columns
                </Box>
                <Box
                  component="button"
                  onClick={onAddToFilter}
                  sx={{
                    fontSize: 10, px: 0.75, py: 0.25, borderRadius: 0.5, cursor: 'pointer',
                    border: `1px solid ${tokens.colorTextSecondary}`, color: tokens.colorTextSecondary,
                    bgcolor: 'transparent', '&:hover': { bgcolor: tokens.colorSubtleFill },
                  }}
                >
                  Insert as Filter
                </Box>
                {onInsertAsFormulas && (
                  <Box
                    component="button"
                    onClick={onInsertAsFormulas}
                    sx={{
                      fontSize: 10, px: 0.75, py: 0.25, borderRadius: 0.5, cursor: 'pointer',
                      border: `1px solid ${tokens.colorGoldDark}`, color: tokens.colorGoldDark,
                      bgcolor: 'transparent', '&:hover': { bgcolor: tokens.colorGoldBg },
                    }}
                  >
                    Insert as CUBESET
                  </Box>
                )}
              </Box>
            </>
          ) : (
            <Typography sx={{ fontSize: 11, color: tokens.colorTextSecondary }}>
              {previewError ?? 'No preview data available.'}
            </Typography>
          )}
        </Box>
      )}
    </>
  );
}
