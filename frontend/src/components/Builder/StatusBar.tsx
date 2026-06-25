import { Box, Chip, IconButton, Tooltip, Typography } from "@mui/material";
import { useT } from "../../i18n";
import ExpandLessIcon from "@mui/icons-material/ExpandLess";
import ExpandMoreIcon from "@mui/icons-material/ExpandMore";
import ErrorOutlineIcon from "@mui/icons-material/ErrorOutline";
import WarningAmberIcon from "@mui/icons-material/WarningAmber";
import CheckCircleOutlineIcon from "@mui/icons-material/CheckCircleOutline";
import InfoOutlinedIcon from "@mui/icons-material/InfoOutlined";
import { useBuilderStore } from "../../store/builderStore";
import { useModelNeedsSaveOrDeploy } from "../../store/useModelEditorStore";

interface Props {
  tableCount: number;
  joinCount: number;
  dimCount: number;
  measCount: number;
  aggCount: number;
  sourceCount?: number;
  hierarchyCount?: number;
  pocketCount?: number;
  hasTarget?: boolean;
  selectedName?: string | null;
}

export default function StatusBar({
  tableCount,
  joinCount,
  dimCount,
  measCount,
  aggCount,
  sourceCount = 0,
  hierarchyCount = 0,
  pocketCount = 0,
  hasTarget = false,
  selectedName,
}: Props) {
  const t = useT();
  // Bug-5515: single shared rule — the bar turns red/white whenever the model
  // has unsaved or undeployed changes, because query results then reflect the
  // last deployed version, not the draft the user sees.
  const needsSaveOrDeploy = useModelNeedsSaveOrDeploy();
  const issues = useBuilderStore((s) => s.validationIssues);
  const expanded = useBuilderStore((s) => s.validationExpanded);
  const toggle = useBuilderStore((s) => s.toggleValidationExpanded);

  const errorCount = issues.filter((i) => i.severity === "error").length;
  const warnCount = issues.filter((i) => i.severity === "warning").length;
  const infoCount = issues.filter((i) => i.severity === "info").length;
  const hasIssues = issues.length > 0;

  const clickable = hasIssues;
  const summaryLabel = hasIssues
    ? [
        errorCount > 0 ? (errorCount > 1 ? t("statusBar.errorsCount", { count: String(errorCount) }) : t("statusBar.errorCount", { count: String(errorCount) })) : null,
        warnCount > 0 ? (warnCount > 1 ? t("statusBar.warningsCount", { count: String(warnCount) }) : t("statusBar.warningCount", { count: String(warnCount) })) : null,
        infoCount > 0 ? (infoCount > 1 ? t("statusBar.infosCount", { count: String(infoCount) }) : t("statusBar.infoCount", { count: String(infoCount) })) : null,
      ]
        .filter(Boolean)
        .join(" · ")
    : t("statusBar.noValidationIssues");

  const tooltipLines = [
    sourceCount !== 1 ? t("statusBar.sourcesPlural", { count: String(sourceCount) }) : t("statusBar.sources", { count: String(sourceCount) }),
    hierarchyCount !== 1 ? t("statusBar.hierarchiesPlural", { count: String(hierarchyCount) }) : t("statusBar.hierarchies", { count: String(hierarchyCount) }),
    pocketCount !== 1 ? t("statusBar.pocketsPlural", { count: String(pocketCount) }) : t("statusBar.pockets", { count: String(pocketCount) }),
    hasTarget ? t("statusBar.targetSet") : t("statusBar.noTarget"),
  ].join(" · ");

  return (
    <Box
      data-testid="statusbar"
      data-needs-save-deploy={needsSaveOrDeploy ? "true" : "false"}
      sx={{
        display: "flex",
        alignItems: "center",
        gap: 1,
        px: 1.25,
        height: 26,
        minHeight: 26,
        borderTop: 1,
        borderColor: needsSaveOrDeploy ? "error.main" : "divider",
        bgcolor: needsSaveOrDeploy ? "error.main" : "grey.50",
        color: needsSaveOrDeploy ? "common.white" : "text.secondary",
      }}
    >
      {needsSaveOrDeploy ? (
        <Typography
          data-testid="statusbar-unsaved-label"
          variant="caption"
          sx={{ lineHeight: 1.3, fontWeight: 700, whiteSpace: "nowrap", color: "common.white" }}
        >
          {t("modelSync.statusBarLabel")}
        </Typography>
      ) : null}
      <Tooltip title={tooltipLines} placement="top-start">
        <Typography
          variant="caption"
          sx={{ lineHeight: 1.3, cursor: "default", whiteSpace: "nowrap" }}
        >
          {tableCount !== 1 ? t("statusBar.tablesPlural", { count: String(tableCount) }) : t("statusBar.tables", { count: String(tableCount) })} {"·"}{" "}
          {joinCount !== 1 ? t("statusBar.joinsPlural", { count: String(joinCount) }) : t("statusBar.joins", { count: String(joinCount) })} {"·"}{" "}
          {dimCount !== 1 ? t("statusBar.dimsPlural", { count: String(dimCount) }) : t("statusBar.dims", { count: String(dimCount) })} {"·"}{" "}
          {measCount !== 1 ? t("statusBar.measuresPlural", { count: String(measCount) }) : t("statusBar.measures", { count: String(measCount) })} {"·"}{" "}
          {aggCount !== 1 ? t("statusBar.aggsPlural", { count: String(aggCount) }) : t("statusBar.aggs", { count: String(aggCount) })}
        </Typography>
      </Tooltip>
      {selectedName ? (
        <Typography variant="caption" sx={{ lineHeight: 1.3, fontStyle: "italic", whiteSpace: "nowrap" }}>
          {t("statusBar.selected", { name: selectedName })}
        </Typography>
      ) : null}
      <Box sx={{ flexGrow: 1 }} />
      <Chip
        data-testid="validation-summary"
        size="small"
        variant="outlined"
        color={errorCount > 0 ? "error" : warnCount > 0 ? "warning" : "default"}
        icon={
          errorCount > 0 ? (
            <ErrorOutlineIcon />
          ) : warnCount > 0 ? (
            <WarningAmberIcon />
          ) : infoCount > 0 ? (
            <InfoOutlinedIcon />
          ) : (
            <CheckCircleOutlineIcon />
          )
        }
        label={summaryLabel}
        onClick={clickable ? toggle : undefined}
        sx={{
          height: 20,
          cursor: clickable ? "pointer" : "default",
          "& .MuiChip-label": { px: 0.75, fontSize: 11 },
          "& .MuiChip-icon": { fontSize: 14 },
        }}
        aria-label={clickable ? t("statusBar.toggleValidationTray") : t("statusBar.noValidationIssuesTray")}
      />
      {clickable ? (
        <IconButton
          size="small"
          onClick={toggle}
          sx={{ p: 0.25 }}
          aria-label={expanded ? t("statusBar.collapseValidationTray") : t("statusBar.expandValidationTray")}
        >
          {expanded ? <ExpandLessIcon sx={{ fontSize: 14 }} /> : <ExpandMoreIcon sx={{ fontSize: 14 }} />}
        </IconButton>
      ) : null}
    </Box>
  );
}
