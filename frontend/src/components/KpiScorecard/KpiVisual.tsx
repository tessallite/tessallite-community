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
    if (value === 0) return null;
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
 * Goal-threshold reading for the bullet chart's target marker (Bug-5345).
 *
 * The bullet chart's signature feature — the one that distinguishes it from the
 * RAG bar — is a target reference line. On the authoritative path the backend
 * does not hand us a raw goal position, but the band scale encodes it: the lower
 * edge of the top (best) band is the level the value must reach. Returns that
 * boundary when it sits strictly inside the plotted scale, else null.
 */
export function goalThreshold(
  bands: KpiThresholdBand[] | undefined,
): number | null {
  if (!bands || bands.length === 0) return null;
  const mins = bands
    .map((b) => b.min)
    .filter((v): v is number => v !== null && v !== undefined);
  const maxes = bands
    .map((b) => b.max)
    .filter((v): v is number => v !== null && v !== undefined);
  if (mins.length === 0 || maxes.length === 0) return null;
  const topBandMin = Math.max(...mins);
  const scaleMin = Math.min(...mins);
  const scaleMax = Math.max(...maxes);
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
          // band scale, so the marker is the top-band threshold; the legacy
          // percentage path draws the synthetic 100% reference.
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
