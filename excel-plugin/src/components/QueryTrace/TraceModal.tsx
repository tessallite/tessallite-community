import { Box, Typography, Dialog, DialogTitle, DialogContent, DialogActions, Button, ThemeProvider, Chip } from '@mui/material';
import { tokens, theme } from '../../theme';
import type { SemanticQuery, PluginRouteTrace } from '../../types/tessallite';
import { strings, templates } from '../../i18n/strings';

interface TraceModalProps {
  open: boolean;
  onClose: () => void;
  query: SemanticQuery | null;
  // F-025-20: the server's route decision for the last run, when available.
  route?: PluginRouteTrace | null;
  modelId: string;
  personaId?: string | null;
}

const ROUTE_LABELS: Record<string, string> = {
  aggregate: strings.trace.routeAggregate,
  pocket: strings.trace.routePocket,
  source: strings.trace.routeSource,
};

export default function TraceModal({ open, onClose, query, route, modelId, personaId }: TraceModalProps) {
  return (
    <ThemeProvider theme={theme}>
    <Dialog open={open} onClose={onClose} maxWidth={false} sx={{ '& .MuiDialog-paper': { width: 380, borderRadius: 2 } }}>
      <DialogTitle sx={{ fontSize: 14, fontWeight: 700, pb: 0 }}>{strings.trace.title}</DialogTitle>
      <DialogContent sx={{ p: 2 }}>
        {query ? (
          <Box>
            <Typography sx={{ fontSize: 11, fontWeight: 600, color: tokens.colorTextSecondary, mb: 0.5 }}>
              {templates.trace.modelPersona(modelId, personaId)}
            </Typography>

            {/* F-025-20: show the route the report actually took. */}
            {route && (
              <Box sx={{ mb: 1.5 }}>
                <Box sx={{ display: 'flex', alignItems: 'center', gap: 1, mb: 0.5 }}>
                  <Chip
                    size="small"
                    label={ROUTE_LABELS[route.route_type] || route.route_type}
                    color={route.route_type === 'source' ? 'default' : 'success'}
                    sx={{ fontSize: 10, height: 20 }}
                  />
                </Box>
                {route.reason && (
                  <Typography sx={{ fontSize: 10, color: tokens.colorTextSecondary, mb: 0.5 }}>
                    {route.reason}
                  </Typography>
                )}
                {route.rewritten_query && (
                  <Box sx={{ bgcolor: tokens.colorSubtleFill, p: 1, borderRadius: 1, fontFamily: tokens.fontMono, fontSize: 10, whiteSpace: 'pre-wrap', maxHeight: 200, overflow: 'auto', mb: 1 }}>
                    {route.rewritten_query}
                  </Box>
                )}
                {/* Bug-6389: explain a policy withhold instead of silently
                    rendering nothing, so the trace does not look broken. */}
                {!route.rewritten_query && route.rewritten_query_redacted && (
                  <Typography sx={{ fontSize: 10, color: tokens.colorTextSecondary, mb: 1 }}>
                    {strings.trace.sqlRedacted}
                  </Typography>
                )}
              </Box>
            )}

            <Typography sx={{ fontSize: 11, fontWeight: 600, color: tokens.colorTextSecondary, mb: 0.5 }}>
              {strings.trace.semanticQuery}
            </Typography>
            <Box sx={{ bgcolor: tokens.colorSubtleFill, p: 1, borderRadius: 1, fontFamily: tokens.fontMono, fontSize: 11, whiteSpace: 'pre-wrap', maxHeight: 280, overflow: 'auto' }}>
              {JSON.stringify(query, null, 2)}
            </Box>
            <Typography sx={{ fontSize: 10, color: tokens.colorTextSecondary, mt: 1 }}>
              {strings.trace.description}
            </Typography>
          </Box>
        ) : (
          <Typography sx={{ fontSize: 13, color: tokens.colorTextSecondary }}>
            {strings.trace.emptyState}
          </Typography>
        )}
      </DialogContent>
      <DialogActions sx={{ px: 2, pb: 1.5 }}>
        <Button size="small" onClick={onClose} sx={{ textTransform: 'none' }}>{strings.trace.close}</Button>
      </DialogActions>
    </Dialog>
    </ThemeProvider>
  );
}
