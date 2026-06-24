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
};

export function localizeKpiLabel(
  label: string | null | undefined,
  t: (key: string) => string,
): string | null {
  if (!label) return null;
  const key = DEFAULT_LABEL_KEYS[label.trim().toLowerCase()];
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
