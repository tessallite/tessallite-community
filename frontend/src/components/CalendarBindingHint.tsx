/**
 * Inline hint about calendar alias availability.
 *
 * A calendar alias is a ModelTable whose ``calendar_table_id`` is set.
 * Period-aware time variants (_ytd, _qtd, _prior_year, etc.) no longer
 * require a calendar alias — they can derive period boundaries from the
 * hierarchy's calendar_type using SQL expressions. Calendar aliases
 * remain useful for retail 4-4-5, dense date enumeration, and backward
 * compatibility.
 *
 * This hint is informational, not a blocker.
 */
import { useQueries } from "@tanstack/react-query";
import { Alert, Link } from "@mui/material";
import { useParams } from "react-router-dom";
import { modelTablesApi } from "../api/client";
import { useSources } from "../api/hooks";
import { useT } from "../i18n";

interface Props {
  context?: "measure-edit" | "measures-list" | "query" | "pivot";
}

export default function CalendarBindingHint({ context = "measure-edit" }: Props) {
  const t = useT();
  const { projectId, modelId } = useParams<{ projectId: string; modelId: string }>();
  const sources = useSources(projectId ?? "", modelId ?? "");

  const tableQueries = useQueries({
    queries: (sources.data ?? []).map((s) => ({
      queryKey: ["modelTables", projectId, modelId, s.id],
      queryFn: () =>
        modelTablesApi.list(projectId ?? "", modelId ?? "", s.id),
      enabled: !!projectId && !!modelId,
    })),
  });

  if (!sources.data || sources.data.length === 0) return null;
  if (tableQueries.some((q) => q.isLoading)) return null;

  const allTables = tableQueries.flatMap((q) => q.data ?? []);
  const hasCalendarAlias = allTables.some(
    (t) => !!t.calendar_table_id || (t.alias ?? "").toLowerCase().includes("calendar"),
  );
  if (hasCalendarAlias) return null;
  if (context !== "measure-edit") return null;

  return (
    <Alert severity="info" variant="outlined" sx={{ mb: 1.5 }}>
      {t("calendarHint.noCalendarBound")}{" "}
      <Link
        href="/help/modelling/configure-time-variants.html"
        target="_blank"
        rel="noopener"
      >
        {t("calendarHint.learnMore")}
      </Link>
    </Alert>
  );
}
