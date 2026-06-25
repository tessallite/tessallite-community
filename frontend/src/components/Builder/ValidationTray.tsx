import { Alert, Box, Collapse, Stack } from "@mui/material";
import { useT } from "../../i18n";
import {
  useBuilderStore,
  type PanelId,
  type ValidationIssue,
} from "../../store/builderStore";

/**
 * Validation tray — renders the list of validation issues produced by
 * `useModelValidation` (server model alerts + client structural rules).
 *
 * The expansion state is owned by the builder store so the StatusBar (or any
 * other surface) can toggle it with a single click. The tray stays mounted
 * but collapses to zero height when closed, which keeps the Collapse
 * animation smooth and avoids layout jumps.
 *
 * Issues carrying an affected object are clickable and navigate to it:
 * dimensions/measures/aggregates open their panel with the object selected,
 * table-scoped issues centre the canvas on the table.
 */

const PANEL_BY_TYPE: Partial<Record<string, PanelId>> = {
  join: "joins",
  dimension: "dimensions",
  measure: "measures",
  aggregate: "aggregates",
};

export default function ValidationTray() {
  const t = useT();
  const issues = useBuilderStore((s) => s.validationIssues);
  const expanded = useBuilderStore((s) => s.validationExpanded);
  const selectObject = useBuilderStore((s) => s.selectObject);
  const openPanel = useBuilderStore((s) => s.openPanel);
  const focusTable = useBuilderStore((s) => s.focusTable);

  if (issues.length === 0) return null;

  const navigate = (issue: ValidationIssue) => {
    if (issue.tableId) {
      selectObject(issue.tableId, "source");
      // Centre the canvas on the table.
      window.dispatchEvent(
        new CustomEvent("canvas-center-node", { detail: issue.tableId }),
      );
      // When the issue carries the owning source, also open the Sources panel
      // and highlight the table's row (the Sources-panel focus affordance the
      // `sourceId` field exists for).
      if (issue.sourceId) {
        focusTable(issue.tableId, issue.sourceId);
      }
      return;
    }
    if (issue.affectedObject && issue.affectedType) {
      const panel = PANEL_BY_TYPE[issue.affectedType];
      if (panel) {
        selectObject(issue.affectedObject, issue.affectedType);
        openPanel(panel);
      }
    }
  };

  const isNavigable = (issue: ValidationIssue) =>
    Boolean(
      issue.tableId ||
        (issue.affectedObject &&
          issue.affectedType &&
          PANEL_BY_TYPE[issue.affectedType]),
    );

  return (
    <Box
      sx={{
        borderTop: 1,
        borderColor: "divider",
        bgcolor: "grey.50",
      }}
    >
      <Collapse in={expanded}>
        <Stack
          spacing={0.5}
          sx={{ px: 1.5, py: 1, maxHeight: 180, overflowY: "auto" }}
        >
          {issues.map((issue) => {
            const clickable = isNavigable(issue);
            return (
              <Alert
                key={issue.id}
                severity={issue.severity}
                sx={{
                  py: 0,
                  ...(clickable ? { cursor: "pointer" } : {}),
                }}
                onClick={clickable ? () => navigate(issue) : undefined}
                role={clickable ? "button" : undefined}
                aria-label={
                  clickable
                    ? t("validation.goToIssue", { message: issue.message })
                    : undefined
                }
              >
                {issue.message}
              </Alert>
            );
          })}
        </Stack>
      </Collapse>
    </Box>
  );
}
