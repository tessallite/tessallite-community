// R3 (round-3 external review): shared display constants for named sets —
// kind label i18n keys and certification-status colors — used by both the
// authoring surface (NamedSetsPanel) and the read-only deployed viewer
// (NamedSetsScorecardTab). Previously the viewer imported these from
// NamedSetsPanel.tsx directly, coupling a lightweight read-only component to
// the full ~2400-line authoring module. Living here, neither depends on the
// other for shared presentation.

/** i18n keys for each named-set list_type, keyed by the raw backend value. */
export const LIST_TYPE_LABELS: Record<string, string> = {
  fixed: "namedSets.labelFixed",
  dynamic_top_n: "namedSets.labelDynamic",
  filtered: "namedSets.labelFiltered",
  advanced_mdx: "namedSets.labelMdx",
  sql_fixed: "namedSets.labelSqlFixed",
};

/** MUI Chip color for each certification_status value. */
export const CERT_COLORS: Record<string, "success" | "warning" | "default" | "info"> = {
  certified: "success",
  shared: "info",
  draft: "default",
  deprecated: "warning",
};
