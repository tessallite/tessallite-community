/**
 * Wires the validation tray to its producers (F-026-01).
 *
 * Subscribes to the model-alerts endpoint (the structural validation engine's
 * output, shared with the Model Health tab via the same `["alerts", ...]`
 * query-key family so revalidation invalidates both) and merges in the
 * client-side structural rules, then publishes the combined issue list into
 * the builder store for ValidationTray / StatusBar.
 */
import { useEffect } from "react";
import { useQuery } from "@tanstack/react-query";
import type { Join, ModelTable } from "../../api/types";
import { alertsApi } from "../../api/client";
import { useT } from "../../i18n";
import { useBuilderStore, type ValidationIssue } from "../../store/builderStore";
import { computeStructuralIssues, mapAlertsToIssues } from "./modelValidation";

function issuesSignature(issues: ValidationIssue[]): string {
  return issues.map((i) => `${i.id}|${i.severity}|${i.message}`).join("\n");
}

export function useModelValidation(
  projectId: string,
  modelId: string,
  args: {
    tables: ModelTable[];
    joins: Join[];
    hasTarget: boolean;
    /** Gate evaluation until the builder's data fetches have settled, so the
     *  tray never flashes issues computed from half-loaded data. */
    ready: boolean;
  },
) {
  const { tables, joins, hasTarget, ready } = args;
  const t = useT();
  const displayLocale = useBuilderStore((s) => s.displayLocale);
  const setValidationIssues = useBuilderStore((s) => s.setValidationIssues);

  const alerts = useQuery({
    queryKey: ["alerts", projectId, modelId, "tray"],
    queryFn: () => alertsApi.list(projectId, modelId, { limit: 500 }),
    refetchInterval: 30000,
    enabled: ready && Boolean(projectId) && Boolean(modelId),
  });

  // F-026-03: distinguish "no server alerts" from "the alerts feed failed to
  // load". Collapsing both into an empty list makes an unreachable validation
  // engine look like a clean model. On query error we keep the last-known
  // server alerts (React Query retains `data` as stale on a failed refetch) and
  // add a persistent, retryable "validation unavailable" issue so the tray
  // never presents a broken feed as no problems.
  const alertsError = alerts.isError;

  // `t` is intentionally not a dependency: useT returns a fresh closure every
  // render (see Bug-1010), which would re-run the effect each render. The
  // locale value drives re-translation instead.
  /* eslint-disable react-hooks/exhaustive-deps */
  useEffect(() => {
    if (!ready) return;
    const feedIssue: ValidationIssue[] = alertsError
      ? [
          {
            id: "validation-feed-unavailable",
            severity: "warning",
            message: t("validation.feed.unavailable"),
          },
        ]
      : [];
    const issues = [
      ...feedIssue,
      ...mapAlertsToIssues(t, alerts.data ?? []),
      ...computeStructuralIssues(t, tables, joins, hasTarget),
    ];
    const current = useBuilderStore.getState().validationIssues;
    if (issuesSignature(current) === issuesSignature(issues)) return;
    setValidationIssues(issues);
  }, [
    ready,
    alerts.data,
    alertsError,
    tables,
    joins,
    hasTarget,
    displayLocale,
    setValidationIssues,
  ]);
  /* eslint-enable react-hooks/exhaustive-deps */
}
