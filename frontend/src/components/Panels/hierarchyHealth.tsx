/**
 * Hierarchy health loading + diagnostic rendering (Bug-8291, Bug-8510).
 *
 * WHY THIS MODULE EXISTS — the probe-state invariant
 * --------------------------------------------------
 * `GET .../hierarchy-health` answers two structurally different questions
 * depending on `probe_members`:
 *
 *   probe_members=true   metadata checks AND a bounded live member-integrity
 *                        probe (orphan children, many-to-many parentage).
 *   probe_members=false  metadata checks ONLY — a hierarchy whose source data
 *                        is full of orphans still returns `status: "ok"`.
 *
 * `status` therefore reports only what the checks that RAN found. Rendering it
 * as "healthy" on its own is the false-healthy defect two earlier rounds kept
 * reintroducing: the metadata-only path and the exact-403 fallback path both
 * discarded the fact that the probe never ran.
 *
 * WHERE THE TRUTH LIVES NOW (Bug-8510)
 * ------------------------------------
 * The API is self-describing: every entry carries `members_probed`, true only
 * when the probe ran for that hierarchy AND scanned every adjacent level pair.
 * That is the single authority for "may I show green", and this module reads
 * it rather than reconstructing it — the previous client-side reconstruction
 * (request outcome + scanning `issues` for `member_integrity_unprobed`) was
 * correct but forced every other consumer to rebuild the same logic, and it
 * could not see a pair the probe attempted and failed on.
 *
 * WHAT IS STILL NECESSARILY CLIENT-SIDE
 * -------------------------------------
 * `MemberProbeOutcome` remains, for one reason the API cannot cover: a 403 has
 * no response body, so "your model binding refused the probe" can only be
 * observed from the HTTP status. It is used to WORD the notice (denied vs never
 * requested) and as a fail-closed cross-check, never as the verdict. The global
 * stored role is not authoritative for a model binding — a tenant-wide modeler
 * may hold only a viewer binding on this model — so a requested probe can be
 * rejected with an exact 403. That is a legitimate, expected outcome
 * (`denied`), not an error, and must read "not checked", never "healthy".
 */
import { Alert, Box, Typography } from "@mui/material";
import { useT } from "../../i18n";
import { hierarchiesApi } from "../../api/client";
import type { HierarchyHealthStatus } from "../../api/types";

/** Whether the bounded member-integrity probe actually ran for this payload. */
export type MemberProbeOutcome =
  /** Requested and executed: member integrity WAS checked. */
  | "probed"
  /** Never requested (viewer / read-only surface): member integrity NOT checked. */
  | "not_requested"
  /** Requested but rejected by this model binding (403): member integrity NOT checked. */
  | "denied";

export interface HierarchyHealthPayload {
  memberProbe: MemberProbeOutcome;
  entries: HierarchyHealthStatus[];
}

/** Displayed status. `partial` = metadata checks passed but members unchecked. */
export type HierarchyDisplayStatus = "pending" | "ok" | "partial" | "warning" | "error";

export function isForbiddenResponse(error: unknown): boolean {
  if (!error || typeof error !== "object") return false;
  const response = (error as { response?: { status?: unknown } }).response;
  return response?.status === 403;
}

export function membersWereChecked(outcome: MemberProbeOutcome | null | undefined): boolean {
  return outcome === "probed";
}

/**
 * Load hierarchy health and report, as first-class state, whether the
 * member-integrity probe actually ran.
 */
export async function loadHierarchyHealth(
  projectId: string,
  modelId: string,
  probeMembers: boolean,
): Promise<HierarchyHealthPayload> {
  if (!probeMembers) {
    return {
      memberProbe: "not_requested",
      entries: await hierarchiesApi.health(projectId, modelId, false),
    };
  }
  try {
    return {
      memberProbe: "probed",
      entries: await hierarchiesApi.health(projectId, modelId, true),
    };
  } catch (error) {
    if (isForbiddenResponse(error)) {
      // The model binding rejected the probe. Fall back to metadata-only, but
      // carry the rejection forward so the result is never shown as healthy.
      return {
        memberProbe: "denied",
        entries: await hierarchiesApi.health(projectId, modelId, false),
      };
    }
    throw error;
  }
}

/**
 * Whether the API states that member integrity was fully checked for this
 * hierarchy (Bug-8510).
 *
 * Reads the authoritative `members_probed` field. The strict `=== true` is a
 * fail-closed guard, not redundancy against the type: a backend that predates
 * the field (an older image in a mixed deployment) omits it, and the only safe
 * reading of "the server did not say" is "not checked". A hierarchy is never
 * shown as healthy on the strength of a missing field.
 */
function memberIntegrityCovered(health: HierarchyHealthStatus): boolean {
  return health.members_probed === true;
}

/**
 * Resolve what the status indicator should say. A backend `ok` is only
 * "healthy" when the API reports full member-integrity coverage for this
 * hierarchy; otherwise it is `partial`.
 *
 * The request-level outcome is AND-ed in so the two sources of truth can never
 * disagree in the unsafe direction: if the probe was never requested (or was
 * refused with a 403) the row cannot read healthy even if a future backend bug
 * set `members_probed` on a metadata-only response.
 */
export function hierarchyDisplayStatus(
  health: HierarchyHealthStatus | undefined,
  outcome: MemberProbeOutcome | null | undefined,
): HierarchyDisplayStatus {
  if (!health) return "pending";
  if (health.status === "error") return "error";
  if (health.status === "warning") return "warning";
  if (!membersWereChecked(outcome)) return "partial";
  return memberIntegrityCovered(health) ? "ok" : "partial";
}

const STATUS_COLOR: Record<HierarchyDisplayStatus, string> = {
  pending: "text.disabled",
  ok: "success.main",
  partial: "info.main",
  warning: "warning.main",
  error: "error.main",
};

export function hierarchyStatusColor(status: HierarchyDisplayStatus): string {
  return STATUS_COLOR[status];
}

/**
 * Translated title per issue type the health endpoint can emit.
 *
 * The member-integrity probe types are only four of the fourteen; the ten
 * metadata types (`hierarchy_health.py::_check_hierarchy_health`) fell through
 * to the raw-token fallback, so a modeller with a broken level chain read
 * "Hierarchy issue: dangling_key_attribute" and nothing else. The fallback is
 * kept, deliberately, so a NEW backend issue type still renders visibly rather
 * than silently disappearing — it must never be the normal path.
 */
const ISSUE_TITLE_KEYS: Record<string, string> = {
  // Member-integrity probe (hierarchy_member_integrity.py).
  member_orphan_children: "hierarchies.healthIssue.orphans",
  member_multiple_parents: "hierarchies.healthIssue.multipleParents",
  member_integrity_unprobed: "hierarchies.healthIssue.unprobed",
  member_integrity_probe_failed: "hierarchies.healthIssue.probeFailed",
  // Metadata checks (hierarchy_health.py).
  empty_levels: "hierarchies.healthIssue.emptyLevels",
  duplicate_level_ordinals: "hierarchies.healthIssue.duplicateOrdinals",
  missing_date_config: "hierarchies.healthIssue.missingDateConfig",
  missing_key_attribute: "hierarchies.healthIssue.missingKeyAttribute",
  dangling_key_attribute: "hierarchies.healthIssue.danglingKeyAttribute",
  unknown_key_attribute_source: "hierarchies.healthIssue.unknownKeyAttributeSource",
  unreachable_level_table: "hierarchies.healthIssue.unreachableLevelTable",
  calendar_table_not_bound: "hierarchies.healthIssue.calendarNotBound",
  calendar_source_columns_missing: "hierarchies.healthIssue.calendarColumnsMissing",
  calendar_type_mismatch: "hierarchies.healthIssue.calendarTypeMismatch",
};

function issueTitleKey(issueType: string): string {
  return ISSUE_TITLE_KEYS[issueType] ?? "hierarchies.healthIssue.other";
}

function stringList(value: unknown): string[] {
  return Array.isArray(value) ? value.map(String) : [];
}

function IssueBody({ detail }: { detail: Record<string, unknown> }) {
  const t = useT();
  const parentLevel = typeof detail.parent_level === "string" ? detail.parent_level : null;
  const childLevel = typeof detail.child_level === "string" ? detail.child_level : null;
  const reason =
    typeof detail.reason === "string"
      ? detail.reason
      : typeof detail.message === "string"
        ? detail.message
        : null;
  const sampleKeys = Array.isArray(detail.sample_keys) ? detail.sample_keys.map(String) : [];
  const sampled = typeof detail.sampled === "number" ? detail.sampled : sampleKeys.length;
  const truncated = detail.truncated === true;
  const check = typeof detail.check === "string" ? detail.check : null;
  const error = typeof detail.error === "string" ? detail.error : null;
  // Metadata-check detail fields.
  const levelName = typeof detail.level_name === "string" ? detail.level_name : null;
  // R1 review: `missing_key_attribute`, `dangling_key_attribute` and
  // `unreachable_level_table` all carry the level's ORDINAL alongside its name.
  // Two levels in one hierarchy may share a name, so the position is what makes
  // the diagnostic point at exactly one level.
  const levelOrdinal = typeof detail.ordinal === "number" ? detail.ordinal : null;
  const attributeSource =
    typeof detail.key_attribute_source === "string" ? detail.key_attribute_source : null;
  const ordinals = stringList(detail.ordinals);
  const missingColumns = stringList(detail.missing_columns);
  const unmappedKeys = stringList(detail.unmapped_period_keys);
  const calendarType = typeof detail.calendar_type === "string" ? detail.calendar_type : null;
  const expectedType = typeof detail.expected_type === "string" ? detail.expected_type : null;
  const actualType = typeof detail.actual_type === "string" ? detail.actual_type : null;

  return (
    <>
      {parentLevel && childLevel ? (
        <Typography variant="caption" display="block">
          {t("hierarchies.healthLevelPair", { parent: parentLevel, child: childLevel })}
        </Typography>
      ) : null}
      {levelName ? (
        <Typography variant="caption" display="block">
          {levelOrdinal === null
            ? t("hierarchies.healthLevel", { name: levelName })
            : t("hierarchies.healthLevelAt", {
                name: levelName,
                ordinal: String(levelOrdinal),
              })}
        </Typography>
      ) : null}
      {ordinals.length > 0 ? (
        <Typography variant="caption" display="block">
          {t("hierarchies.healthOrdinals", { ordinals: ordinals.join(", ") })}
        </Typography>
      ) : null}
      {attributeSource ? (
        <Typography variant="caption" display="block">
          {t("hierarchies.healthAttributeSource", { source: attributeSource })}
        </Typography>
      ) : null}
      {expectedType && actualType ? (
        <Typography variant="caption" display="block">
          {t("hierarchies.healthCalendarMismatch", { expected: expectedType, actual: actualType })}
        </Typography>
      ) : null}
      {calendarType ? (
        <Typography variant="caption" display="block">
          {t("hierarchies.healthCalendarType", { type: calendarType })}
        </Typography>
      ) : null}
      {missingColumns.length > 0 ? (
        <Typography variant="caption" display="block">
          {t("hierarchies.healthMissingColumns", { columns: missingColumns.join(", ") })}
        </Typography>
      ) : null}
      {unmappedKeys.length > 0 ? (
        <Typography variant="caption" display="block">
          {t("hierarchies.healthUnmappedPeriodKeys", { keys: unmappedKeys.join(", ") })}
        </Typography>
      ) : null}
      {reason ? (
        <Typography variant="caption" display="block">
          {reason}
        </Typography>
      ) : null}
      {sampleKeys.length > 0 ? (
        <Typography variant="caption" display="block">
          {t("hierarchies.healthSampleKeys", { keys: sampleKeys.join(", ") })}
        </Typography>
      ) : null}
      {sampled > 0 ? (
        <Typography variant="caption" display="block">
          {truncated
            ? t("hierarchies.healthSampledTruncated", { count: String(sampled) })
            : t("hierarchies.healthSampled", { count: String(sampled) })}
        </Typography>
      ) : null}
      {check || error ? (
        <Typography variant="caption" display="block">
          {t("hierarchies.healthProbeFailure", { check: check ?? "-", error: error ?? "-" })}
        </Typography>
      ) : null}
    </>
  );
}

/**
 * Render the structured diagnostics for one hierarchy.
 *
 * When the member probe did not run FOR THE REQUEST, an explicit "member
 * integrity was not checked" notice is rendered FIRST — including when the
 * backend returned an empty issue list, which is precisely the false-healthy
 * case. This notice is deliberately keyed on the request-level outcome rather
 * than on `members_probed`: its two wordings state why the whole request went
 * unprobed (never asked for / refused by this model binding), which is exactly
 * what a 403 or a viewer session means. A probe that RAN but skipped an
 * individual level pair is explained per pair by the backend's own
 * `member_integrity_unprobed` / `member_integrity_probe_failed` issues below,
 * and is reflected in the status indicator through `members_probed`.
 */
export function HierarchyHealthIssues({
  hierarchyName,
  health,
  memberProbe,
}: {
  hierarchyName: string;
  health: HierarchyHealthStatus | undefined;
  memberProbe: MemberProbeOutcome | null | undefined;
}) {
  const t = useT();
  if (!health) return null;

  const checked = membersWereChecked(memberProbe);
  const issues = health.issues ?? [];
  // R1-2: the status indicator can read "partial" purely on the strength of
  // `members_probed`, so the diagnostics must be able to explain that on their
  // own. Normally the backend names each skipped pair, and repeating a generic
  // notice next to those would be noise. But coverage is deliberately reported
  // independently of the issue list, so a server that reports incomplete
  // coverage without naming a pair — or one too old to send the field at all —
  // would otherwise leave a partial row with nothing said about it.
  const explainedPerPair = issues.some(
    (issue) =>
      issue.issue_type === "member_integrity_unprobed" ||
      issue.issue_type === "member_integrity_probe_failed",
  );
  const coverageUnexplained =
    checked && !memberIntegrityCovered(health) && !explainedPerPair;
  const showProbeNotice = !checked || coverageUnexplained;
  if (!showProbeNotice && issues.length === 0) return null;

  return (
    <Box
      role="list"
      aria-label={t("hierarchies.healthIssuesFor", { name: hierarchyName })}
      sx={{ display: "grid", gap: 0.5, mt: 0.75, pl: 1.75 }}
    >
      {showProbeNotice ? (
        <Alert
          role="listitem"
          severity="info"
          icon={false}
          data-testid={coverageUnexplained ? "member-probe-incomplete" : "member-probe-not-run"}
          sx={{ py: 0.25, px: 0.75, "& .MuiAlert-message": { width: "100%" } }}
        >
          <Typography variant="caption" fontWeight={700} display="block">
            {t(
              coverageUnexplained
                ? "hierarchies.healthIssue.incomplete"
                : "hierarchies.healthIssue.unprobed",
            )}
          </Typography>
          <Typography variant="caption" display="block">
            {coverageUnexplained
              ? t("hierarchies.memberProbeIncomplete")
              : memberProbe === "denied"
                ? t("hierarchies.memberProbeDenied")
                : t("hierarchies.memberProbeNotRequested")}
          </Typography>
        </Alert>
      ) : null}
      {issues.map((issue, issueIndex) => (
        <Alert
          key={`${issue.issue_type}-${issueIndex}`}
          role="listitem"
          severity={
            issue.severity === "error" ? "error" : issue.severity === "warning" ? "warning" : "info"
          }
          icon={false}
          sx={{ py: 0.25, px: 0.75, "& .MuiAlert-message": { width: "100%" } }}
        >
          <Typography variant="caption" fontWeight={700} display="block">
            {t(issueTitleKey(issue.issue_type), { type: issue.issue_type })}
          </Typography>
          <IssueBody detail={issue.detail ?? {}} />
        </Alert>
      ))}
    </Box>
  );
}
