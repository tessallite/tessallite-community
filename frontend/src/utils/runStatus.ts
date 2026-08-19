/**
 * Translated labels for backend run statuses (review F-559-04).
 *
 * Run-history tables used to render `run.status` verbatim, so every optimiser
 * and refresh run showed the raw backend token. That was invisible while the
 * tokens happened to be English words; Bug-8034 made it visible by adding
 * `queued`, which a user sees for the window between accepting a manual advisor
 * run and the dispatcher claiming it.
 *
 * The root cause is the verbatim render, not the new value, so the fix is one
 * helper shared by every run-history consumer rather than a special case for
 * `queued`.
 *
 * An unrecognised status falls through to the raw value. That keeps the neutral
 * colour token (`statusColor`'s default branch) meaningful and, more
 * importantly, never leaks a raw `runStatus.*` key into the UI when the backend
 * adds a status the frontend has not learned yet.
 */
export type RunStatusLabelFn = (key: string) => string;

export function runStatusLabel(
  status: string | null | undefined,
  t: RunStatusLabelFn,
): string {
  switch (status) {
    case "queued":
      return t("runStatus.queued");
    case "running":
    case "in_progress":
      return t("runStatus.running");
    case "completed":
      return t("runStatus.completed");
    case "failed":
      return t("runStatus.failed");
    default:
      return status ?? "";
  }
}
