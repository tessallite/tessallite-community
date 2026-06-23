import { useMemo } from "react";
import type { KpiThresholdBand } from "../../api/types";
import { ui, palette } from "../../theme/tokens";
import { useT } from "../../i18n";
import { deriveScale } from "./chartUtils";
import { bandsToAxisLine, statusColor } from "./GaugeChart";

interface Props {
  value: number | null;
  bands?: KpiThresholdBand[];
  width?: number;
  height?: number;
}

function fmtBoundary(v: number): string {
  if (Number.isInteger(v)) return String(v);
  const abs = Math.abs(v);
  return abs < 10 ? v.toFixed(2).replace(/0$/, "") : v.toFixed(1);
}

/**
 * A real thermometer (new presentation type). A rounded glass tube with a bulb
 * reservoir; a single status-coloured mercury column rises from the bulb to the
 * value. A slim colour-zone strip beside the tube carries the RAG bands and a
 * tick scale labels the boundaries. The bulb-and-tube silhouette makes it read
 * as a thermometer — not a vertical RAG bar (which fills the whole track with
 * colour). Bespoke SVG for crisp, controlled geometry.
 */
export default function Thermometer({
  value,
  bands,
  width = 120,
  height = 150,
}: Props) {
  const t = useT();
  const defaultBands = useMemo<KpiThresholdBand[]>(
    () => [
      { label: t("kpiScorecard.poor"), color: ui.red, min: 0, max: 40 },
      { label: t("kpiScorecard.warning"), color: "#ed6c02", min: 40, max: 70 },
      { label: t("kpiScorecard.good"), color: ui.green, min: 70, max: 100 },
    ],
    [t],
  );
  const effectiveBands = bands && bands.length > 0 ? bands : defaultBands;
  const { scaleMin, scaleMax } = useMemo(
    () => deriveScale(effectiveBands),
    [effectiveBands],
  );
  const range = scaleMax - scaleMin || 1;
  const axisLine = useMemo(
    () => bandsToAxisLine(effectiveBands, scaleMin, scaleMax),
    [effectiveBands, scaleMin, scaleMax],
  );
  const clamped =
    value !== null ? Math.max(scaleMin, Math.min(scaleMax, value)) : scaleMin;
  const norm = (clamped - scaleMin) / range;
  const mercury = value !== null ? statusColor(norm, axisLine) : "#9e9e9e";

  // --- Geometry -----------------------------------------------------------
  // Tube sits just right of centre, leaving room for the tick labels on its
  // left; the whole SVG is centred in the card so it reads as balanced.
  const padT = 12;
  const padB = 10;
  const bulbR = Math.max(12, Math.min(width * 0.17, height * 0.11, 18));
  const tubeW = bulbR * 1.0;
  const cx = Math.round(width * 0.58);
  const bulbCy = height - padB - bulbR;
  const tubeTopY = padT;
  const scaleTopY = padT + tubeW * 0.5 + 2;
  const scaleBotY = bulbCy - bulbR * 0.15;
  const span = scaleBotY - scaleTopY || 1;
  const yFor = (v: number) =>
    scaleBotY - ((v - scaleMin) / range) * span;
  const valueY = scaleBotY - norm * span;

  const boundaries = useMemo(() => {
    const vals = new Set<number>();
    for (const b of effectiveBands) {
      vals.add(b.min ?? scaleMin);
      vals.add(b.max ?? scaleMax);
    }
    return [...vals]
      .filter((v) => v >= scaleMin && v <= scaleMax)
      .sort((a, b) => a - b);
  }, [effectiveBands, scaleMin, scaleMax]);

  const stripX = cx + tubeW / 2 + 3;
  const stripW = 3.5;
  const tubeLeft = cx - tubeW / 2;
  const colW = tubeW - 3;

  return (
    <svg
      width={width}
      height={height}
      viewBox={`0 0 ${width} ${height}`}
      role="img"
      aria-label={t(
        value === null ? "kpiScorecard.statusUnknown" : "kpiScorecard.value",
      )}
    >
      {/* slim colour-zone strip — the RAG context, beside the tube */}
      {effectiveBands.map((b, i) => {
        const yTop = yFor(b.max ?? scaleMax);
        const yBot = yFor(b.min ?? scaleMin);
        return (
          <rect
            key={i}
            x={stripX}
            y={yTop}
            width={stripW}
            height={Math.max(0, yBot - yTop)}
            rx={1.5}
            fill={b.color}
            opacity={0.85}
          />
        );
      })}

      {/* glass — empty tube + bulb */}
      <rect
        x={tubeLeft}
        y={tubeTopY}
        width={tubeW}
        height={bulbCy - tubeTopY}
        rx={tubeW / 2}
        fill="#EEF2F7"
        stroke={palette.slateBorder}
        strokeWidth={1}
      />
      <circle
        cx={cx}
        cy={bulbCy}
        r={bulbR}
        fill="#EEF2F7"
        stroke={palette.slateBorder}
        strokeWidth={1}
      />

      {/* mercury — bulb + rising column to the value */}
      <circle cx={cx} cy={bulbCy} r={bulbR - 1.5} fill={mercury} />
      <rect
        x={cx - colW / 2}
        y={valueY}
        width={colW}
        height={bulbCy - valueY + 2}
        rx={colW / 2}
        fill={mercury}
      />
      {/* glossy highlight on the mercury */}
      <rect
        x={cx - tubeW * 0.24}
        y={valueY + 3}
        width={tubeW * 0.16}
        height={Math.max(0, bulbCy - valueY)}
        rx={tubeW * 0.08}
        fill="rgba(255,255,255,0.4)"
      />

      {/* tick scale + boundary labels on the left */}
      {boundaries.map((b, i) => {
        const y = yFor(b);
        return (
          <g key={i}>
            <line
              x1={tubeLeft - 4}
              x2={tubeLeft - 1}
              y1={y}
              y2={y}
              stroke={palette.slateBorder}
              strokeWidth={1}
            />
            <text
              x={tubeLeft - 6}
              y={y + 3}
              textAnchor="end"
              fontSize={9}
              fill={palette.textSecondary}
            >
              {fmtBoundary(b)}
            </text>
          </g>
        );
      })}
    </svg>
  );
}
