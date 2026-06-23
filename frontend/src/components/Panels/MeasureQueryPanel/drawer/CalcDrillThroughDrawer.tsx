import { useMemo } from "react";
import {
  Alert,
  Box,
  Chip,
  Divider,
  Drawer,
  IconButton,
  Stack,
  Typography,
} from "@mui/material";
import CloseIcon from "@mui/icons-material/Close";
import { useT } from "../../../../i18n";
import { formatMeasureValue } from "../../../../api/measureFormat";
import type { Dimension, DrillThroughFilter, Measure } from "../../../../api/types";
import type { DrillContext } from "../types";
import { resolveReferencedMeasures } from "./calcExpression";
import DrillMiniPanel from "./DrillMiniPanel";

type Props = {
  open: boolean;
  context: DrillContext | null;
  rowDims: Dimension[];
  colDims: Dimension[];
  allMeasures: Measure[];
  personaId?: string | null;
  // F-019-03: active-slicer predicates forwarded to each mini panel.
  filters?: DrillThroughFilter[];
  onClose: () => void;
};

export default function CalcDrillThroughDrawer({
  open,
  context,
  rowDims,
  colDims,
  allMeasures,
  personaId,
  filters,
  onClose,
}: Props) {
  const t = useT();
  const calcMeasure = context?.measure ?? null;

  const { resolved, unresolvedNames } = useMemo(() => {
    if (!calcMeasure) return { resolved: [] as Measure[], unresolvedNames: [] as string[] };
    return resolveReferencedMeasures(calcMeasure.expression, allMeasures);
  }, [calcMeasure, allMeasures]);

  const cellValueText = useMemo(() => {
    if (!calcMeasure || !context) return "";
    // F-019-06: read the CLICKED calc measure's own (alias-keyed) value, not
    // the first column measure's value (coord.measureValue). When the calc
    // column is not first, the old lookup showed a different measure's number
    // directly above this measure's formula — a visibly wrong "Formula value".
    const v =
      context.coord.measureValues?.[calcMeasure.name] ??
      context.coord.measureValue;
    if (v === null || v === undefined) return "—";
    return formatMeasureValue(v, calcMeasure.format);
  }, [calcMeasure, context]);

  return (
    <Drawer
      anchor="right"
      open={open}
      onClose={onClose}
      sx={{ zIndex: (t) => t.zIndex.drawer + 3 }}
      PaperProps={{ sx: { width: { xs: "100%", sm: 780 } } }}
    >
      <Box sx={{ p: 2, display: "flex", flexDirection: "column", height: "100%", gap: 1 }}>
        <Stack direction="row" alignItems="center" justifyContent="space-between">
          <Typography variant="subtitle1">
            {t("calcDrill.title", { name: calcMeasure?.display_name ?? "" })}
          </Typography>
          <IconButton size="small" onClick={onClose} aria-label={t("calcDrill.closeAriaLabel")}>
            <CloseIcon fontSize="small" />
          </IconButton>
        </Stack>

        {context && calcMeasure && (
          <Stack direction="row" spacing={0.5} sx={{ flexWrap: "wrap" }}>
            {rowDims.map((d, i) => (
              <Chip key={`r-${d.id}`} size="small" label={`${d.name} = ${context.coord.rowKey[i] ?? ""}`} />
            ))}
            {colDims.map((d, i) => (
              <Chip key={`c-${d.id}`} size="small" label={`${d.name} = ${context.coord.colKey[i] ?? ""}`} />
            ))}
          </Stack>
        )}

        {calcMeasure && (
          <Box sx={{ p: 1, bgcolor: "action.hover", borderRadius: 1 }}>
            <Stack direction="row" alignItems="baseline" spacing={2}>
              <Typography variant="caption" color="text.secondary">
                {t("calcDrill.formulaValue")}
              </Typography>
              <Typography variant="h6" sx={{ fontFamily: "monospace" }}>
                {cellValueText}
              </Typography>
            </Stack>
            {calcMeasure.expression && (
              <Typography
                variant="caption"
                color="text.secondary"
                sx={{ fontFamily: "monospace", display: "block", mt: 0.5 }}
              >
                {calcMeasure.expression}
              </Typography>
            )}
          </Box>
        )}

        {unresolvedNames.length > 0 && (
          <Alert severity="warning">
            {t("calcDrill.unknownMeasures", { names: unresolvedNames.join(", ") })}
          </Alert>
        )}

        {calcMeasure && resolved.length === 0 && unresolvedNames.length === 0 && (
          <Alert severity="info">
            {t("calcDrill.noReferences")}
          </Alert>
        )}

        <Divider />

        <Box sx={{ flexGrow: 1, overflow: "auto", display: "flex", flexDirection: "column", gap: 1.5 }}>
          {context &&
            resolved.map((m) => (
              <DrillMiniPanel
                key={m.id}
                measure={m}
                coord={context.coord}
                rowDims={rowDims}
                colDims={colDims}
                personaId={personaId}
                filters={filters}
              />
            ))}
        </Box>
      </Box>
    </Drawer>
  );
}
