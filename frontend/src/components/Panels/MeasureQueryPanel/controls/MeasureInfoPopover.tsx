/**
 * Contextual measure-definition popover for the pivot (Bug-8102 / F-104-02).
 *
 * An analyst building a pivot must be able to see what a measure MEANS at
 * selection time — its business definition, formula meaning, and any synonyms —
 * without leaving the task to open a separate modeller glossary panel. This
 * reads EXISTING model metadata only: the measure's own description/expression
 * and the approved glossary definition/synonyms already curated for the model.
 */
import { useState } from "react";
import { IconButton, Popover, Stack, Tooltip, Typography } from "@mui/material";
import InfoOutlinedIcon from "@mui/icons-material/InfoOutlined";
import type { Measure } from "../../../../api/types";
import { useT } from "../../../../i18n";

export interface MeasureGlossaryInfo {
  definition?: string | null;
  synonyms?: string[];
}

export default function MeasureInfoPopover({
  measure,
  glossary,
}: {
  measure: Measure;
  glossary?: MeasureGlossaryInfo;
}) {
  const t = useT();
  const [anchor, setAnchor] = useState<HTMLElement | null>(null);

  const definition = glossary?.definition || measure.description || null;
  const synonyms = (glossary?.synonyms ?? []).filter((s) => s.trim() !== "");
  const isCalculated = measure.measure_type === "calculated";
  const formula = isCalculated ? measure.expression : null;

  // Nothing worth showing → no icon (keeps the row clean for bare measures).
  const hasContent = Boolean(definition) || synonyms.length > 0 || Boolean(formula);
  if (!hasContent) return null;

  return (
    <>
      <Tooltip title={t("pickerBar.measureInfoTooltip")}>
        <IconButton
          size="small"
          aria-label={t("pickerBar.measureInfoTooltip")}
          onClick={(e) => {
            e.stopPropagation();
            setAnchor(e.currentTarget);
          }}
          sx={{ p: 0.25 }}
        >
          <InfoOutlinedIcon sx={{ fontSize: 15 }} />
        </IconButton>
      </Tooltip>
      <Popover
        open={Boolean(anchor)}
        anchorEl={anchor}
        onClose={() => setAnchor(null)}
        anchorOrigin={{ vertical: "bottom", horizontal: "left" }}
        onClick={(e) => e.stopPropagation()}
      >
        <Stack spacing={0.75} sx={{ p: 1.25, maxWidth: 320 }}>
          <Typography variant="subtitle2" sx={{ fontWeight: 700 }}>
            {measure.display_name || measure.name}
          </Typography>
          {definition ? (
            <Typography variant="body2" color="text.secondary">
              {definition}
            </Typography>
          ) : (
            <Typography variant="caption" color="text.disabled">
              {t("pickerBar.measureNoDefinition")}
            </Typography>
          )}
          {formula ? (
            <Stack spacing={0.25}>
              <Typography variant="caption" sx={{ fontWeight: 700, color: "text.secondary" }}>
                {t("pickerBar.measureFormulaLabel")}
              </Typography>
              <Typography
                variant="caption"
                component="code"
                sx={{ fontFamily: "monospace", wordBreak: "break-word" }}
              >
                {formula}
              </Typography>
            </Stack>
          ) : null}
          {synonyms.length > 0 ? (
            <Stack spacing={0.25}>
              <Typography variant="caption" sx={{ fontWeight: 700, color: "text.secondary" }}>
                {t("pickerBar.measureSynonymsLabel")}
              </Typography>
              <Typography variant="caption" color="text.secondary">
                {synonyms.join(", ")}
              </Typography>
            </Stack>
          ) : null}
        </Stack>
      </Popover>
    </>
  );
}
