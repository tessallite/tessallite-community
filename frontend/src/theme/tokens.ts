/**
 * Tessallite brand design tokens — single source of truth for all colours,
 * typography, and canvas-specific values.
 *
 * Source: https://tessallite.io/css/style.css
 */

// ---------------------------------------------------------------------------
// Brand palette
// ---------------------------------------------------------------------------
export const palette = {
  // Core brand
  primaryGreen:      "#006C35",
  primaryGreenDark:  "#004E25",   // solid FACT header
  charcoal:          "#333333",
  mint:              "#F2F7F4",   // app / canvas background
  white:             "#FFFFFF",
  slateBorder:       "#CBD5E1",
  textSecondary:     "#5A6577",

  // Canvas
  canvasDot:         "#C8D8CC",   // subtle but visible on mint background

  // Joins
  joinFactDim:       "#006C35",   // primary green  — fact↔dim relationship
  joinSameType:      "#A8BFB3",   // muted green-grey — dim↔dim / fact↔fact
} as const;

// ---------------------------------------------------------------------------
// Typography
// ---------------------------------------------------------------------------
export const font = {
  sans: "'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif",
  mono: "'JetBrains Mono', 'Fira Code', 'Cascadia Code', monospace",
} as const;

// ---------------------------------------------------------------------------
// ERD node header definitions — one entry per table_type value
// ---------------------------------------------------------------------------
export interface NodeHeaderStyle {
  bg:              string;   // header band background
  border:          string;   // card border colour
  headerTextColor: string;   // table-name text colour (must contrast with bg)
  badgeBg:         string;   // type badge background
  badgeColor:      string;   // type badge text colour (WCAG AA contrast)
  badgeText:       string;   // label shown in badge
}

export const nodeHeader: Record<string, NodeHeaderStyle> = {
  // Fact — solid brand-green header; white text; inverted white badge
  fact: {
    bg:              palette.primaryGreenDark,  // #004E25 — solid green
    border:          palette.primaryGreenDark,
    headerTextColor: palette.white,
    badgeBg:         palette.white,
    badgeColor:      palette.primaryGreenDark,  // green text on white badge
    badgeText:       "FACT",
  },

  // Dimension detail — warm amber / gold theme
  dim_detail: {
    bg:              "#FFFBEE",   // very light warm cream
    border:          "#B8960C",   // darker gold (contrast-safe border)
    headerTextColor: "#5C4300",   // dark amber text
    badgeBg:         "#B8960C",   // darker gold badge
    badgeColor:      palette.white,
    badgeText:       "DIM",
  },

  // Dimension aggregate — cool steel-blue (clearly distinct from both above)
  dim_aggregate: {
    bg:              "#EEF3FB",   // very light cool blue
    border:          "#3A5EA8",   // steel blue
    headerTextColor: "#1A2D5A",   // dark navy text
    badgeBg:         "#3A5EA8",   // steel blue badge
    badgeColor:      palette.white,
    badgeText:       "DIM AGG",
  },

  // Unclassified — neutral gray; indicates the table needs classification
  unclassified: {
    bg:              "#F5F5F5",   // light gray
    border:          "#9E9E9E",   // medium gray
    headerTextColor: "#424242",   // dark gray text
    badgeBg:         "#9E9E9E",   // gray badge
    badgeColor:      palette.white,
    badgeText:       "?",
  },

  // Calendar — purple/violet; clearly distinct from fact (green), dim (amber/blue), unclassified (gray)
  calendar: {
    bg:              "#F5F0FB",   // very light lavender
    border:          "#7B3FA0",   // deep purple
    headerTextColor: "#4A1870",   // dark purple text
    badgeBg:         "#7B3FA0",   // deep purple badge
    badgeColor:      palette.white,
    badgeText:       "CAL",
  },
} as const;

export const nodeHeaderFallback: NodeHeaderStyle = nodeHeader.unclassified;

// ---------------------------------------------------------------------------
// Panel UI tokens — chips, status indicators, table headers
// ---------------------------------------------------------------------------
export const ui = {
  tableHeaderBg:     palette.mint,
  cardBg:            palette.white,
  surfaceMuted:      palette.mint,

  green:             palette.primaryGreen,
  greenBg:           "rgba(0,108,53,0.08)",
  greenLight:        "#E8F5E9",

  gold:              "#D4AF37",
  goldDark:          "#A67C00",
  goldBg:            "rgba(164,124,0,0.08)",
  goldLight:         "#FFF8E1",

  purple:            "#6B4C8A",
  purpleBg:          "rgba(107,76,138,0.08)",

  red:               "#B33A3A",
  redBg:             "#FFEBEE",

  muted:             palette.textSecondary,
  mutedBg:           palette.mint,

  // Pivot total shading — brand-green tints, distinct from each other and the header.
  subtotalBg:        "rgba(0,108,53,0.05)",   // light tint for subtotal rows/cols
  grandTotalBg:      "rgba(0,108,53,0.12)",   // stronger tint for grand-total rows/cols
} as const;

export type StatusSeverity = "active" | "success" | "completed" | "warning" | "stale" | "creating" | "running" | "error" | "failed" | "invalid" | "retired" | "default";

export function statusColor(severity: StatusSeverity | string): { bg: string; fg: string } {
  switch (severity) {
    case "active": case "success": case "completed": case "approved": case "validated":
      return { bg: ui.greenLight, fg: ui.green };
    case "warning": case "stale": case "creating": case "running":
      return { bg: ui.goldLight, fg: ui.goldDark };
    case "error": case "failed": case "invalid": case "refresh_failed":
      return { bg: ui.redBg, fg: ui.red };
    case "retired": case "retired_unused": case "purged":
      return { bg: ui.mutedBg, fg: ui.muted };
    default:
      return { bg: ui.mutedBg, fg: ui.muted };
  }
}
