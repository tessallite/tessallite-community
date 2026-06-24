import { Box } from "@mui/material";
import { ui, palette } from "../../theme/tokens";
import { useT } from "../../i18n";
import type { KpiDisplayStatus } from "./statusUtils";

interface Props {
  /** -1 poor / 0 warning / 1 good; null renders all lamps dim (no data). */
  status: KpiDisplayStatus | null;
  /** Overall housing height in px; lamps scale from this. */
  size?: number;
}

/** Lamp order top→bottom and the status each represents. */
const LAMPS: { key: KpiDisplayStatus; color: string }[] = [
  { key: -1, color: ui.red },
  { key: 0, color: ui.goldDark },
  { key: 1, color: ui.green },
];

/**
 * Three-lamp traffic light (Bug-5343). Light, corporate styling to match the
 * rest of the scorecard: a clean white housing, "off" lamps shown as a soft
 * tint of their own colour, and the active lamp filled with a gentle halo ring
 * (no heavy dark box or neon glow).
 */
export default function TrafficLight({ status, size = 120 }: Props) {
  const t = useT();
  const lampD = Math.round(size * 0.26);
  const gap = Math.round(size * 0.075);
  const pad = Math.round(lampD * 0.52);

  const ariaKey =
    status === 1
      ? "kpiScorecard.statusGood"
      : status === 0
        ? "kpiScorecard.statusWarning"
        : status === -1
          ? "kpiScorecard.statusPoor"
          : "kpiScorecard.statusUnknown";

  return (
    <Box
      role="img"
      aria-label={t(ariaKey)}
      sx={{
        display: "flex",
        flexDirection: "column",
        alignItems: "center",
        gap: `${gap}px`,
        px: `${pad}px`,
        py: `${pad}px`,
        borderRadius: `${lampD}px`,
        bgcolor: palette.white,
        border: `1px solid ${palette.slateBorder}`,
        boxShadow: "0 1px 2px rgba(15,23,42,0.06)",
      }}
    >
      {LAMPS.map(({ key, color }) => {
        const lit = status === key;
        return (
          <Box
            key={key}
            sx={{
              width: lampD,
              height: lampD,
              borderRadius: "50%",
              // Lit lamp: a glossy sphere (soft top highlight over the status
              // colour) with a gentle halo ring. Off lamps: a faint tint of their
              // own colour so the red/amber/green positions stay recognisable
              // without shouting. No dark housing, no neon glow.
              background: lit
                ? `radial-gradient(circle at 38% 30%, rgba(255,255,255,0.6), rgba(255,255,255,0) 46%), ${color}`
                : `${color}1f`,
              border: `1px solid ${lit ? color : `${color}40`}`,
              boxShadow: lit
                ? `0 0 0 4px ${color}1f, 0 1px 2px ${color}40`
                : "inset 0 1px 1px rgba(255,255,255,0.6)",
              transition: "background 0.25s ease, box-shadow 0.25s ease",
            }}
          />
        );
      })}
    </Box>
  );
}
