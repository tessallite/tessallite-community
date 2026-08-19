import type {
  Direction,
  KpiEvaluateResponse,
  KpiPresentationMeta,
  KpiThresholdBand,
  PresentationType,
} from "../../api/types";
import GaugeChart from "./GaugeChart";
import BulletChart from "./BulletChart";
import RagBar from "./RagBar";
import TrafficLight from "./TrafficLight";
import ProgressRing from "./ProgressRing";
import Thermometer from "./Thermometer";
import { resolveKpiDisplayStatus } from "./statusUtils";
import { deriveScale } from "./chartUtils";

interface Props {
  presentationType: PresentationType | "" | null;
  presentationMeta: KpiPresentationMeta | null;
  evalData: KpiEvaluateResponse;
  direction?: Direction;
  size?: number;
  showDetail?: boolean;
}

function ratioForChart(
  value: number | null,
  target: number | null,
  direction: Direction | undefined,
): number | null {
  if (value === null || target === null || target === 0) return null;
  if (direction === "lower_is_better") {
    // Bug-7223: value <= 0 with a positive target is the best possible
    // outcome (e.g. negative cost = credit).  target/value would be
    // undefined (div-by-zero) or negative, misplacing the gauge needle.
    // Return a large percentage so the needle lands at the best end.
    if (value <= 0 && target > 0) return 1e4;
    // Bug-7223 R1: both negative -- use value/target so a more negative
    // value (better for lower_is_better) yields a higher ratio.
    // (target===0 already guarded at the top of the function)
    if (value <= 0 && target < 0) return (value / target) * 100;
    return (target / value) * 100;
  }
  if (direction === "closer_is_better") {
    return (1 - Math.abs(value - target) / Math.abs(target)) * 100;
  }
  return (value / target) * 100;
}

function rescaleBandsToPercentage(
  bands: KpiThresholdBand[] | undefined,
): KpiThresholdBand[] | undefined {
  if (!bands || bands.length === 0) return bands;
  const boundaries: number[] = [];
  for (const b of bands) {
    if (b.min !== null && b.min !== undefined) boundaries.push(b.min);
    if (b.max !== null && b.max !== undefined) boundaries.push(b.max);
  }
  if (boundaries.length === 0) return bands;
  const isRatioScale = boundaries.every((v) => v <= 2.0);
  if (!isRatioScale) return bands;
  return bands.map((b) => ({
    ...b,
    min: b.min !== null && b.min !== undefined ? b.min * 100 : b.min,
    max: b.max !== null && b.max !== undefined ? b.max * 100 : b.max,
  }));
}

export function bandsLookAbsolute(
  bands: KpiThresholdBand[] | undefined,
  target: number | null,
): boolean {
  if (!bands || bands.length === 0) return false;
  const boundaries: number[] = [];
  for (const b of bands) {
    if (b.min !== null && b.min !== undefined) boundaries.push(b.min);
    if (b.max !== null && b.max !== undefined) boundaries.push(b.max);
  }
  if (boundaries.length === 0) return false;
  const maxBoundary = Math.max(...boundaries);
  if (target !== null && target !== 0 && maxBoundary !== 100) {
    const ratio = maxBoundary / Math.abs(target);
    if (ratio > 0.5 && ratio < 3) return true;
  }
  return false;
}

/**
 * Parse a #RRGGBB / #RGB colour into [r,g,b], or null if unparseable.
 */
function parseHexColor(color: string): [number, number, number] | null {
  const hex = color.trim().replace(/^#/, "");
  if (hex.length === 3) {
    const r = parseInt(hex[0] + hex[0], 16);
    const g = parseInt(hex[1] + hex[1], 16);
    const b = parseInt(hex[2] + hex[2], 16);
    if ([r, g, b].some(Number.isNaN)) return null;
    return [r, g, b];
  }
  if (hex.length === 6) {
    const r = parseInt(hex.slice(0, 2), 16);
    const g = parseInt(hex.slice(2, 4), 16);
    const b = parseInt(hex.slice(4, 6), 16);
    if ([r, g, b].some(Number.isNaN)) return null;
    return [r, g, b];
  }
  return null;
}

// Exact "good" band colours from the backend preset contract
// (services/model-service/src/kpi_threshold.py _GOOD_BAND_COLORS). Includes the
// colour-blind palette (blue On Track / Exceeding), so the goal marker resolves
// correctly in colour-blind mode too (Bug-7239 R2). Lowercased for comparison.
const GOOD_BAND_COLORS = new Set(["#388e3c", "#1565c0", "#0d47a1"]);

/**
 * True when a band's colour signals "good" (RAG green/blue), mirroring the
 * backend `_status_from_color` classifier (kpi_threshold.py): the exact good
 * preset colours, else a green-dominant OR blue-dominant hue heuristic for
 * custom colours. Blue-dominant counts as good because the colour-blind presets
 * and the "Exceeding" band use blue for the best status.
 */
function isGoodBandColor(color: string): boolean {
  const c = (color || "").trim().toLowerCase();
  if (GOOD_BAND_COLORS.has(c)) return true;
  const rgb = parseHexColor(c);
  if (!rgb) return false;
  const [r, g, b] = rgb;
  const mx = Math.max(r, g, b);
  if (mx - Math.min(r, g, b) <= 24) return false; // near-grayscale: not good
  if (g === mx && g >= 110) return true; // green-dominant
  if (b === mx && b >= 120) return true; // blue-dominant (exceeding / colour-blind)
  return false;
}

/**
 * The value where the "good" region begins, read off the band scale (Bug-7239).
 *
 * The good region spans from the first to the last good-coloured band (a scale
 * can have more than one — e.g. "On Track" + "Exceeding" in the 4-band presets;
 * every authored preset keeps these contiguous). The goal marker is the boundary
 * the good region shares with the neighbouring WORSE region, on whichever side
 * that is:
 *   - good region at the HIGH end (worse bands below): entry = region's lower
 *     edge (a value must climb to reach it).
 *   - good region at the LOW end (worse bands above): entry = region's upper edge
 *     (a value must stay below it to remain good — lower-is-better / variance).
 * Returns null when there is no good band, or the good region has no closed edge
 * facing a worse band (it spans the whole scale).
 */
function goodRegionEntryBoundary(bands: KpiThresholdBand[]): number | null {
  const goodFlags = bands.map((b) => isGoodBandColor(b.color));
  if (!goodFlags.some(Boolean)) return null;

  // Are there any worse (non-good) bands below vs above the good region?
  const firstGood = goodFlags.indexOf(true);
  const lastGood = goodFlags.lastIndexOf(true);
  const worseBelow = goodFlags.slice(0, firstGood).some((f) => !f) || firstGood > 0;
  const worseAbove =
    goodFlags.slice(lastGood + 1).some((f) => !f) || lastGood < bands.length - 1;

  // Collect the good region's own closed boundaries.
  const firstGoodBand = bands[firstGood];
  const lastGoodBand = bands[lastGood];
  const regionMin = firstGoodBand.min; // entry edge if good sits at the HIGH end
  const regionMax = lastGoodBand.max; // entry edge if good sits at the LOW end

  const hasMin = regionMin !== null && regionMin !== undefined;
  const hasMax = regionMax !== null && regionMax !== undefined;

  // Prefer the edge that faces the worse region.
  if (worseBelow && hasMin) return regionMin as number;
  if (worseAbove && hasMax) return regionMax as number;
  // Fall back to whichever closed edge exists.
  if (hasMin) return regionMin as number;
  if (hasMax) return regionMax as number;
  return null;
}

/**
 * Goal-threshold reading for the bullet chart's target marker (Bug-5345, Bug-7239).
 *
 * The bullet chart's signature feature — the one that distinguishes it from the
 * RAG bar — is a target reference line. On the authoritative path the backend
 * does not hand us a raw goal position, but the band scale encodes it: the inner
 * edge of the GOOD band is the level the value must reach.
 *
 * Bug-7239: the good band is NOT always the topmost band. For lower-is-better,
 * deviation and variance scales the good ("On Track") band sits at the LOW end,
 * so the previous top-band-min heuristic returned the bad-band edge (= scale max)
 * and the marker vanished. We locate the good band by colour and return the
 * boundary between it and the neighbouring worse band, on whichever side that is.
 */
export function goalThreshold(
  bands: KpiThresholdBand[] | undefined,
): number | null {
  if (!bands || bands.length === 0) return null;

  // Interiorness must be judged against the SAME axis the BulletChart plots
  // (deriveScale), not the concrete-boundary span. Canonical/default band sets
  // are open-ended (first band min=null, last band max=null), so their concrete
  // span collapses to the interior boundaries and the good-band edge would sit
  // exactly on that span extreme and be wrongly rejected (Bug-7239). deriveScale
  // expands the open ends into real headroom, making the good-band inner edge a
  // genuine interior, drawable marker position.
  const { scaleMin, scaleMax } = deriveScale(bands);

  const entry = goodRegionEntryBoundary(bands);
  if (entry !== null) {
    // Only draw the marker when the good-region entry sits inside the plotted
    // axis (a whole-scale good region has no meaningful goal line).
    if (entry > scaleMin && entry < scaleMax) return entry;
    return null;
  }

  // No colour-identified good band: fall back to the historical top-band-min
  // reading so ascending high-is-good bands with non-standard colours still work.
  const concreteMins = bands
    .map((b) => b.min)
    .filter((v): v is number => v !== null && v !== undefined);
  if (concreteMins.length === 0) return null;
  const topBandMin = Math.max(...concreteMins);
  if (topBandMin > scaleMin && topBandMin < scaleMax) return topBandMin;
  return null;
}

export interface ChartInputs {
  /** The needle/bar position to plot. */
  chartValue: number | null;
  /** The bands defining the chart scale and colours. */
  bands: KpiThresholdBand[] | undefined;
  /** True when the backend supplied an authoritative position + bands. */
  hasAuthoritative: boolean;
  /** Legacy-only: raw-value plotting (no rescaling). */
  isAbsolute: boolean;
}

/**
 * Resolve the value and bands the chart should plot (Bug-1226).
 *
 * When the backend exposes the authoritative position it matched the status
 * against (`status_position`) plus the bands it matched against (`status_bands`),
 * the chart needle is driven from those directly. Because the backend matched
 * the status badge on the SAME position/band pair, the needle, band colour and
 * badge cannot disagree for any evaluation type (percentage_variance / z_score /
 * percentile_rank / percentage_of_target). No per-type rescaling in the
 * frontend — one authoritative scale.
 *
 * The legacy percentage-of-target path is retained only for responses that
 * predate these fields (e.g. cached payloads).
 */
export function resolveChartInputs(
  evalData: KpiEvaluateResponse,
  presentationMeta: KpiPresentationMeta | null,
  direction?: Direction,
): ChartInputs {
  const explicitType = presentationMeta?.evaluation_type;
  const rawBands = presentationMeta?.bands;
  const value = evalData.value;
  const target = evalData.target ?? evalData.goal;

  const authoritativePosition = evalData.status_position;
  const authoritativeBands = evalData.status_bands;
  const hasAuthoritative =
    authoritativePosition !== null &&
    authoritativePosition !== undefined &&
    Array.isArray(authoritativeBands) &&
    authoritativeBands.length > 0;

  if (hasAuthoritative) {
    return {
      chartValue: authoritativePosition as number,
      bands: authoritativeBands as KpiThresholdBand[],
      hasAuthoritative: true,
      isAbsolute: false,
    };
  }

  const ratioValue = ratioForChart(value, target, direction);
  const isAbsolute =
    explicitType === "absolute_value" ||
    (!explicitType && target === null) ||
    (!explicitType && bandsLookAbsolute(rawBands, target)) ||
    (explicitType === "percentage_of_target" && ratioValue === null);
  return {
    chartValue: isAbsolute ? value : ratioValue,
    bands: isAbsolute ? rawBands : rescaleBandsToPercentage(rawBands),
    hasAuthoritative: false,
    isAbsolute,
  };
}

export default function KpiVisual({
  presentationType,
  presentationMeta,
  evalData,
  direction,
  size,
  showDetail,
}: Props) {
  if (!presentationType) return null;

  const { chartValue, bands, hasAuthoritative, isAbsolute } = resolveChartInputs(
    evalData,
    presentationMeta,
    direction,
  );

  switch (presentationType) {
    case "traffic_light":
      return (
        <TrafficLight
          status={resolveKpiDisplayStatus(
            evalData.status ?? null,
            evalData.status_label ?? null,
          )}
          size={size ? Math.round(size * 0.52) : 105}
        />
      );

    case "progress_ring":
      return (
        <ProgressRing
          value={chartValue}
          bands={bands}
          size={size ? Math.round(size * 0.66) : 130}
          showDetail={showDetail}
        />
      );

    case "thermometer":
      return (
        <Thermometer
          value={chartValue}
          bands={bands}
          width={size ? Math.round(size * 0.6) : 120}
          height={size ? Math.round(size * 0.72) : 150}
        />
      );

    case "gauge":
    case "speedometer":
    case "reverse_gauge":
      return (
        <GaugeChart
          value={chartValue}
          bands={bands}
          size={size ?? 124}
          showDetail={showDetail}
        />
      );

    case "bullet_chart":
      return (
        <BulletChart
          value={chartValue}
          // The bullet's target marker is what distinguishes it from the RAG bar
          // (Bug-5345). On the authoritative/absolute path the goal lives in the
          // band scale, so the marker is the GOOD band's inner edge — identified
          // by band colour, on whichever end the good band sits (Bug-7239); the
          // legacy percentage path draws the synthetic 100% reference.
          target={
            hasAuthoritative || isAbsolute ? goalThreshold(bands) : 100
          }
          bands={bands}
          width={size ? Math.round(size * 1.3) : 268}
          height={70}
        />
      );

    case "rag_bar":
      return (
        <RagBar
          value={chartValue}
          bands={bands}
          width={size ? Math.round(size * 1.2) : 248}
          height={52}
        />
      );

    default:
      return null;
  }
}
