import {
  Box,
  Button,
  Chip,
  Dialog,
  DialogContent,
  DialogTitle,
  IconButton,
  Stack,
  Typography,
} from "@mui/material";
import { Close, Visibility } from "@mui/icons-material";
import type { Citation } from "../types/turn";
import { useChatContext } from "../providers/ChatProvider";
import { formatValue } from "./CitationChips";

export interface CitationProvenanceDialogProps {
  citation: Citation | null;
  open: boolean;
  onClose: () => void;
  // Optional secondary diagnostics action (F-104-04's "optional separate
  // diagnostics action"): opens the existing generic trace drawer for the
  // whole turn. Omitted entirely when the host does not wire trace viewing
  // (e.g. conversational-client today), matching the existing opt-in pattern
  // AssistantTurn already uses for onOpenTrace elsewhere in this file tree.
  onOpenTrace?: () => void;
}

function routeChipColor(
  routeType: string | null,
): "success" | "secondary" | "warning" | "default" {
  if (routeType === "aggregate") return "success";
  if (routeType === "pocket") return "secondary";
  if (routeType === "source") return "warning";
  return "default";
}

/**
 * Bug-8181 (checkable citations) — clicking a citation chip previously opened
 * the same generic technical trace drawer for every chip, regardless of
 * which one was clicked (F-104-04: "citation chips are not directly
 * checkable citations" — a user cannot verify a metric's definition, route,
 * or exact supporting slice from a semantic label alone). This dialog is the
 * consumer-facing provenance view for ONE citation: its business definition,
 * the value, the route that served it, and the filter/grain slice that
 * produced it — with technical trace inspection as an optional secondary
 * action, not the only action.
 */
export function CitationProvenanceDialog({
  citation,
  open,
  onClose,
  onOpenTrace,
}: CitationProvenanceDialogProps) {
  const { t } = useChatContext();
  if (!citation) return null;

  const kindLabel =
    citation.kind === "measure" ? t("citations.measure") : t("citations.dimension");
  const formattedValue = formatValue(citation.value);

  return (
    <Dialog open={open} onClose={onClose} maxWidth="sm" fullWidth>
      <DialogTitle
        sx={{ display: "flex", alignItems: "center", justifyContent: "space-between" }}
      >
        <Box>
          <Typography variant="subtitle1" component="div">
            {t("citations.provenance.title")}
          </Typography>
          <Typography variant="caption" color="text.secondary">
            {kindLabel}: {citation.display_name || citation.name}
          </Typography>
        </Box>
        <IconButton onClick={onClose} aria-label={t("renderedOutput.closeAria")} size="small">
          <Close fontSize="small" />
        </IconButton>
      </DialogTitle>
      <DialogContent>
        <Stack spacing={2} sx={{ mt: 0.5 }}>
          {formattedValue != null && (
            <Box>
              <Typography variant="caption" color="text.secondary" fontWeight={650}>
                {t("citations.provenance.valueLabel")}
              </Typography>
              <Typography variant="h6" component="div">
                {formattedValue}
              </Typography>
            </Box>
          )}

          <Box>
            <Typography variant="caption" color="text.secondary" fontWeight={650}>
              {t("citations.provenance.definitionLabel")}
            </Typography>
            <Typography variant="body2" sx={{ mt: 0.25, whiteSpace: "pre-wrap" }}>
              {citation.definition || t("citations.provenance.noDefinition")}
            </Typography>
          </Box>

          <Box>
            <Typography variant="caption" color="text.secondary" fontWeight={650}>
              {t("citations.provenance.filterGrainLabel")}
            </Typography>
            <Typography variant="body2" sx={{ mt: 0.25, whiteSpace: "pre-wrap" }}>
              {citation.filter_grain || t("citations.provenance.noFilterGrain")}
            </Typography>
          </Box>

          {citation.route_type && (
            <Box>
              <Chip
                size="small"
                color={routeChipColor(citation.route_type)}
                label={t("trace.routeWithValue", { route: citation.route_type })}
              />
            </Box>
          )}

          {onOpenTrace && (
            <Box>
              <Button
                size="small"
                variant="text"
                startIcon={<Visibility fontSize="small" />}
                onClick={onOpenTrace}
                sx={{ textTransform: "none", minWidth: 0, px: 1, py: 0.25 }}
              >
                {t("turn.viewTrace")}
              </Button>
            </Box>
          )}
        </Stack>
      </DialogContent>
    </Dialog>
  );
}
