/**
 * Single source of truth for Explorer UI access control.
 *
 * Maps every gated Explorer action to the role tier that may perform it, so the
 * page never scatters raw role checks across the JSX (where one site can drift
 * from another or from the backend). Each predicate composes the existing
 * `isTenantAdmin()` / `canEditModelConfig()` helpers, which read the role from
 * localStorage (set only from /users/me — see currentUser.ts).
 *
 * SECURITY MODEL: these gates only hide UI the user can't act on. The backend
 * re-checks every request via `require_role` / `require_tenant_admin`; the
 * mapping here mirrors that enforcement (defense in depth, not a substitute).
 * The full per-role compliance table lives in
 * docs/architecture/architecture_explorer-rbac-matrix.md.
 *
 * Role hierarchy: Tenant Admin includes everything a Modeller can do, which
 * includes everything a Viewer can do. `canEditModelConfig()` already returns
 * true for tenant_admin/system_admin, so modeller-tier actions are inherited by
 * admins automatically.
 */
import { isTenantAdmin, canEditModelConfig } from "./currentUser";

export type ExplorerAction =
  // Tenant-level structural actions — tenant admin only.
  | "project.create"
  | "project.importExport"
  | "project.toggleActive"
  | "project.delete"
  // Project/model setup actions — modeller and above.
  | "project.rename"
  | "project.configDrawer"
  | "model.add"
  | "model.rename"
  | "model.delete"
  | "model.importExport"
  | "model.deploy";

/** Actions reserved for tenant/system admins (tenant + structural project ops). */
const TENANT_ADMIN_ACTIONS: ReadonlySet<ExplorerAction> = new Set<ExplorerAction>([
  "project.create",
  "project.importExport",
  "project.toggleActive",
  "project.delete",
]);

/**
 * Actions a project modeller may perform (admins inherit them).
 *
 * `project.configDrawer` only opens the project setup drawer — a modeller sees
 * the agent-configuration group (their model-level surface), while the drawer
 * itself hides the admin-only sections (connections, LLM providers, branding,
 * users & access, audit, webhooks, SSO, security audit). The split lives in
 * ProjectConfigDrawer and mirrors each section's backend require_role.
 */
const MODELLER_ACTIONS: ReadonlySet<ExplorerAction> = new Set<ExplorerAction>([
  "project.rename",
  "project.configDrawer",
  "model.add",
  "model.rename",
  "model.delete",
  "model.importExport",
  "model.deploy",
]);

/**
 * Whether the current user may perform an Explorer action. Returns false
 * conservatively for unknown actions and for logged-out/corrupted state.
 */
export function canPerform(action: ExplorerAction): boolean {
  if (TENANT_ADMIN_ACTIONS.has(action)) return isTenantAdmin();
  if (MODELLER_ACTIONS.has(action)) return canEditModelConfig();
  return false;
}
