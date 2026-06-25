/**
 * UnsavedDeployWarning — shared banner shown at the top of every
 * query-generating panel (Query Panel, Pivot) when the open model has unsaved
 * or undeployed changes (Bug-5515).
 *
 * Queries always execute against the DEPLOYED snapshot, while the panels render
 * the current/draft model. When they disagree the user is shown draft fields
 * but gets deployed-version results — a silent mismatch. This banner makes that
 * explicit. The dirty/deployed rule lives in a single selector
 * (useModelNeedsSaveOrDeploy) so all panels share one source of truth.
 */
import { Alert } from "@mui/material";
import { useT } from "../../i18n";
import { useModelNeedsSaveOrDeploy } from "../../store/useModelEditorStore";

export default function UnsavedDeployWarning() {
  const t = useT();
  const needsSaveOrDeploy = useModelNeedsSaveOrDeploy();
  if (!needsSaveOrDeploy) return null;
  return (
    <Alert severity="warning" data-testid="unsaved-deploy-warning" sx={{ py: 0.25 }}>
      {t("modelSync.queryPanelWarning")}
    </Alert>
  );
}
