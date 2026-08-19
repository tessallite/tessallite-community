/**
 * Shared Modeller-supersedes-Model-viewer grant flow (Bug-8101 / F-104-01).
 *
 * Both admin surfaces that assign project access bindings — the tenant
 * user-admin panel (UsersAccessPanel) and the system user-management page
 * (TenantAdmin) — must enforce the invariant identically: assigning Modeller
 * and Model-viewer to the same effective scope shows a confirmation
 * ("Modeller supersedes Model viewer. The Model viewer role will be removed.")
 * and, on confirm, removes the Model-viewer grant so only Modeller remains; on
 * cancel neither change applies.
 *
 * The check itself is authoritative on the backend (POST /access rejects an
 * overlapping grant with 409 unless supersede=true). This helper performs the
 * dry-run preflight so the UI can show the confirmation before sending the
 * real grant, keeping both surfaces in lockstep. The backend remains the
 * source of truth even if a surface forgets to preflight.
 */
import { accessApi } from "../../api/client";
import type { UserAccessBindingCreate } from "../../api/types";

export interface SupersedeConfirmMessages {
  title: string;
  message: string;
  confirmLabel: string;
}

/**
 * Grant an access binding, running the supersession confirmation first when
 * the backend would trigger it. Returns "granted" on success, "cancelled" when
 * the admin declined the supersession confirmation (nothing was changed).
 *
 * @param confirm  the app confirm() dialog (returns true when confirmed)
 */
export async function grantAccessWithSupersede(
  projectId: string,
  data: UserAccessBindingCreate,
  confirm: (opts: {
    title: string;
    message: string;
    confirmLabel: string;
  }) => Promise<boolean>,
  messages: SupersedeConfirmMessages,
): Promise<"granted" | "cancelled"> {
  const preflight = await accessApi.preflight(projectId, data);
  if (preflight.supersedes) {
    const ok = await confirm({
      title: messages.title,
      message: messages.message,
      confirmLabel: messages.confirmLabel,
    });
    if (!ok) return "cancelled";
    await accessApi.grant(projectId, data, true);
    return "granted";
  }
  await accessApi.grant(projectId, data);
  return "granted";
}
