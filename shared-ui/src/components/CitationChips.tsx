import { Box, Chip, Tooltip } from "@mui/material";
import type { Citation } from "../types/turn";
import { useChatContext } from "../providers/ChatProvider";

interface CitationChipsProps {
  citations: Citation[];
  // Bug-8181 — each chip is independently checkable now (opens its OWN
  // provenance dialog), so the handler receives the citation that was
  // clicked rather than firing one shared action for every chip.
  onClick?: (citation: Citation, index: number) => void;
}

export function formatValue(value: unknown): string | null {
  if (value == null) return null;
  if (typeof value === "number" && Number.isFinite(value)) {
    return new Intl.NumberFormat(undefined, {
      maximumFractionDigits: Number.isInteger(value) ? 0 : 2,
    }).format(value);
  }
  const str = String(value).trim();
  if (/^-?(?:\d+(\.\d+)?|\d*\.?\d+e[+-]?\d+)$/i.test(str)) {
    const num = Number(str);
    if (Number.isFinite(num)) {
      return new Intl.NumberFormat(undefined, {
        maximumFractionDigits: Number.isInteger(num) ? 0 : 2,
      }).format(num);
    }
  }
  return str.length > 0 ? str : null;
}

function chipLabel(c: Citation, index: number): string {
  const name = c.display_name || c.name;
  if (!name) return `[${index + 1}]`;
  const formatted = formatValue(c.value);
  return formatted ? `${name}: ${formatted}` : name;
}

export function CitationChips({ citations, onClick }: CitationChipsProps) {
  const { t } = useChatContext();
  if (!citations.length) return null;

  return (
    <Box
      sx={{ display: "flex", gap: 0.5, flexWrap: "wrap", mt: 1 }}
      aria-label={t("citations.label")}
    >
      {citations.map((c, i) => {
        const label = chipLabel(c, i);
        const tooltip = c.kind
          ? `${c.kind === "measure" ? t("citations.measure") : t("citations.dimension")}: ${c.display_name || c.name || ""}`.trim()
          : t("citations.source");
        return (
          <Tooltip key={c.id ?? i} title={tooltip} arrow>
            <Chip
              label={label}
              size="small"
              onClick={onClick ? () => onClick(c, i) : undefined}
              sx={{
                fontSize: 11,
                height: 20,
                maxWidth: 320,
                cursor: onClick ? "pointer" : undefined,
              }}
            />
          </Tooltip>
        );
      })}
    </Box>
  );
}
