/**
 * Shared route-type label helper for the pivot panel and its drawers.
 *
 * The router returns a raw English ``route_type`` (``source`` / ``aggregate``
 * / ``pocket``). Every surface that shows it to the user must translate it the
 * same way — the pivot route badge and the drill-through panel both call this
 * so they never diverge (Bug-6282: the drill panel used to print the raw value
 * while the badge translated it).
 */
export function routeBadgeLabel(routeType: string, t: (key: string) => string): string {
  // F-004-15: translate the aggregate/pocket route-type values too, not just
  // "source"; previously the raw English route_type string leaked into the UI.
  if (routeType === "source") return t("pivot.liveSource");
  if (routeType === "aggregate") return t("pivot.routeAggregate");
  if (routeType === "pocket") return t("pivot.routePocket");
  return routeType;
}
