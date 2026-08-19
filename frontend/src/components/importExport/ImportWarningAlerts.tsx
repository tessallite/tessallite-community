import { Alert, Stack, Typography } from "@mui/material";

import type { ImportWarning } from "../../api/importExportApi";
import {
  formatImportWarning,
  importWarningSeverity,
  type ImportWarningTranslator,
} from "./warningText";

type Props = {
  warnings: Array<ImportWarning | string>;
  t: ImportWarningTranslator;
  showHeading?: boolean;
};

/** Render every import warning at its backend-declared severity. */
export default function ImportWarningAlerts({
  warnings,
  t,
  showHeading = true,
}: Props) {
  if (warnings.length === 0) return null;

  return (
    <Stack spacing={1} sx={{ mt: 1 }} data-testid="import-warning-alerts">
      {showHeading && (
        <Typography variant="subtitle2">
          {t("importDialog.warningsCount", { count: String(warnings.length) })}
        </Typography>
      )}
      {warnings.map((warning, index) => (
        <Alert
          key={index}
          severity={importWarningSeverity(warning)}
          data-severity={importWarningSeverity(warning)}
        >
          {formatImportWarning(warning, t)}
        </Alert>
      ))}
    </Stack>
  );
}
