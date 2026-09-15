export type KpiDisplayStatus = -1 | 0 | 1;

function normaliseStatus(status: number | null | undefined): KpiDisplayStatus | null {
  if (status === 1 || status === 0 || status === -1) return status;
  return null;
}

export function statusFromLabel(label: string | null | undefined): KpiDisplayStatus | null {
  if (!label) return null;
  const text = label.trim().toLowerCase();
  if (!text) return null;

  if (
    text.includes("off target") ||
    text.includes("poor") ||
    text.includes("critical") ||
    text.includes("breach")
  ) {
    return -1;
  }
  if (
    text.includes("near target") ||
    text.includes("warning") ||
    text.includes("attention")
  ) {
    return 0;
  }
  if (
    text.includes("on track") ||
    text.includes("good") ||
    text.includes("met target")
  ) {
    return 1;
  }

  return null;
}

// F-017-18: backend status/trend labels are English-only ("On Track",
// "Improving", ...). Translate the KNOWN default-preset labels via i18n while
// leaving custom band labels (anything not in the map) verbatim, so a modeler's
// bespoke label still shows as authored. The map keys are the canonical English
// strings the backend emits for the standard/centred/variance presets and the
// trend classifier.
//
// Bug-7232: the fail-loud backend-authored evaluation labels are added to the
// same map — they are fixed sentences in model-service/src/api/kpis.py, not
// custom band labels, so non-English users must not see them verbatim.
const DEFAULT_LABEL_KEYS: Record<string, string> = {
  "off target": "kpiScorecard.offTarget",
  "near target": "kpiScorecard.nearTarget",
  "on track": "kpiScorecard.onTrack",
  critical: "kpiScorecard.critical",
  warning: "kpiScorecard.warning",
  exceeding: "kpiScorecard.exceeding",
  "no data": "kpiScorecard.statusUnknown",
  improving: "kpiScorecard.improving",
  stable: "kpiScorecard.flat",
  declining: "kpiScorecard.declining",
  "insufficient data": "kpiScorecard.insufficientData",
  // Fail-loud evaluation labels (backend-authored fixed sentences).
  "restricted by row security": "kpiScorecard.statusRowSecurityRestricted",
  "model is not deployed — deploy the model before evaluating kpis":
    "kpiScorecard.modelNotDeployed",
  "evaluation failed — this time-intelligence kpi needs a time dimension":
    "kpiScorecard.tiNeedsTimeDimension",
  "composite evaluation failed — circular composite reference":
    "kpiScorecard.compositeCycle",
  "evaluation failed — check kpi expression and model scope":
    "kpiScorecard.evaluationFailedGeneric",
  "target query failed": "kpiScorecard.targetQueryFailed",
  "no expression configured": "kpiScorecard.noExpression",
  "evaluation failed — every child kpi errored": "kpiScorecard.allChildrenErrored",
};

// Bug-7232: the composite-depth label carries a dynamic nesting count (the
// backend's _MAX_COMPOSITE_DEPTH constant), so it cannot sit in the exact-match
// map. Prefix-match it and render the fixed translated sentence.
const COMPOSITE_DEPTH_PREFIX =
  "Composite evaluation failed — composite nesting deeper than";

const FALLBACK_KEY = "kpiScorecard.compositeDepthExceeded";

export function localizeKpiLabel(
  label: string | null | undefined,
  t: (key: string) => string,
): string | null {
  if (!label) return null;
  const trimmed = label.trim();
  const key =
    DEFAULT_LABEL_KEYS[trimmed.toLowerCase()] ??
    (trimmed.startsWith(COMPOSITE_DEPTH_PREFIX) ? FALLBACK_KEY : undefined);
  if (!key) return label; // custom label — show as authored
  const translated = t(key);
  // t() returns the key unchanged when missing; fall back to the raw label.
  return translated && translated !== key ? translated : label;
}

export function resolveKpiDisplayStatus(
  status: number | null | undefined,
  label?: string | null,
): KpiDisplayStatus | null {
  // F-017-17 (regression of Bug-853): trust the backend numeric status first.
  // The backend now derives status correctly for every evaluation type
  // (F-017-02 variance, F-017-03 z_score/percentile), so the English
  // keyword-match on the label must NOT override it — a custom band labelled
  // e.g. "Breach watch OK" must not be forced to -1, and non-English labels
  // would fall through to the wrong bucket. Label matching is kept only as a
  // last-resort fallback when the API sends no numeric status.
  return normaliseStatus(status) ?? statusFromLabel(label);
}
