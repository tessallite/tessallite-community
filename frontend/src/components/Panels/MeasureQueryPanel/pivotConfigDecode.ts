/**
 * Defensive decoders for a saved pivot-view `config` blob (Bug-8161, review B2).
 *
 * The backend now types the config on WRITE, but views persisted BEFORE that
 * contract existed may still carry malformed fields. SlicerBar dereferences
 * `slicer.dimensionId`/`op`/`values`, and PivotGrid dereferences the
 * conditional-format union — a `null` slicer entry or a `null`/scalar
 * conditionalFormat crashes the loader, which for a SHARED historical view means
 * one user's bad config crashes another user's pivot. These decoders coerce every
 * loader-consumed field to a safe shape on READ (belt-and-braces with the write
 * guard), and report whether anything had to be dropped so the panel can warn.
 */
import { SLICER_OP_LABELS, type Slicer, type SlicerOp } from "./types";
import type { ConditionalFormat, EmptyCellMode } from "./grid/PivotGrid";
import type { MeasureSel } from "./measureColumns";

const VALID_SLICER_OPS: ReadonlySet<string> = new Set(Object.keys(SLICER_OP_LABELS));

function isRecord(v: unknown): v is Record<string, unknown> {
  return !!v && typeof v === "object" && !Array.isArray(v);
}

export interface Decoded<T> {
  value: T;
  /** false when at least one malformed entry/field was dropped or defaulted. */
  ok: boolean;
}

export function decodeSlicers(raw: unknown): Decoded<Slicer[]> {
  if (raw === undefined) return { value: [], ok: true };
  if (!Array.isArray(raw)) return { value: [], ok: false };
  let ok = true;
  const out: Slicer[] = [];
  for (const item of raw) {
    if (
      !isRecord(item) ||
      typeof item.dimensionId !== "string" ||
      !item.dimensionId ||
      typeof item.op !== "string" ||
      !VALID_SLICER_OPS.has(item.op)
    ) {
      ok = false;
      continue;
    }
    const rawValues = item.values;
    const values = Array.isArray(rawValues)
      ? rawValues.filter((v): v is string => typeof v === "string")
      : [];
    if (!Array.isArray(rawValues)) ok = false;
    out.push({ dimensionId: item.dimensionId, op: item.op as SlicerOp, values });
  }
  return { value: out, ok };
}

export function decodeConditionalFormat(raw: unknown): Decoded<ConditionalFormat> {
  if (raw === undefined) return { value: { kind: "none" }, ok: true };
  if (!isRecord(raw)) return { value: { kind: "none" }, ok: false };
  switch (raw.kind) {
    case "none":
      return { value: { kind: "none" }, ok: true };
    case "color-scale":
      return typeof raw.low === "string" && typeof raw.high === "string"
        ? { value: { kind: "color-scale", low: raw.low, high: raw.high }, ok: true }
        : { value: { kind: "none" }, ok: false };
    case "data-bars":
      return typeof raw.color === "string"
        ? { value: { kind: "data-bars", color: raw.color }, ok: true }
        : { value: { kind: "none" }, ok: false };
    case "threshold":
      return typeof raw.below === "string" &&
        typeof raw.above === "string" &&
        typeof raw.threshold === "number"
        ? {
            value: { kind: "threshold", below: raw.below, above: raw.above, threshold: raw.threshold },
            ok: true,
          }
        : { value: { kind: "none" }, ok: false };
    default:
      return { value: { kind: "none" }, ok: false };
  }
}

export function decodeMeasureSelections(raw: unknown): Decoded<MeasureSel[]> {
  if (!Array.isArray(raw)) return { value: [], ok: false };
  let ok = true;
  const out: MeasureSel[] = [];
  for (const item of raw) {
    if (!isRecord(item) || typeof item.measureId !== "string" || !item.measureId) {
      ok = false;
      continue;
    }
    out.push({ measureId: item.measureId, agg: typeof item.agg === "string" ? item.agg : "" });
  }
  return { value: out, ok };
}

export function decodeEmptyCellMode(raw: unknown): EmptyCellMode {
  return raw === "zero" || raw === "dash" ? raw : "blank";
}

export function decodeBool(raw: unknown, dflt: boolean): boolean {
  return typeof raw === "boolean" ? raw : dflt;
}

export function decodePersonaId(raw: unknown): string | null {
  return typeof raw === "string" && raw ? raw : null;
}
