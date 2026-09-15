import { lazy, Suspense, useId, useMemo, useState } from "react";
import type { ReactNode } from "react";
import {
  Accordion,
  AccordionDetails,
  AccordionSummary,
  Box,
  Button,
  Card,
  CardContent,
  Chip,
  Collapse,
  Dialog,
  DialogContent,
  DialogTitle,
  IconButton,
  Stack,
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableRow,
  Tooltip,
  Typography,
} from "@mui/material";
import {
  Close,
  ExpandMore,
  ExpandLess,
  Fullscreen,
  ManageSearchOutlined,
  Refresh,
  Visibility,
} from "@mui/icons-material";
import type { TurnResponse, Citation } from "../types/turn";
import type { TraceVisibility } from "./TraceStrip";
import { MarkdownAnswer } from "./MarkdownAnswer";
import { MetadataBadges } from "./MetadataBadges";
import { CitationChips } from "./CitationChips";
import { CitationProvenanceDialog } from "./CitationProvenanceDialog";
import { RenderedOutput } from "./RenderedOutput";
import { DataTableBlock } from "./DataTableBlock";
import { VisualArtifactBlock, parseVisualArtifact } from "./VisualArtifactBlock";
import { FeedbackButtons } from "./FeedbackButtons";
import { JudgeBlockCard } from "./JudgeBlockCard";
import { JudgeVerdictStrip } from "./JudgeVerdictStrip";
import { buildAutoChartSpec } from "../utils/chartSpec";
import { useChatContext } from "../providers/ChatProvider";
import { formatThoughtText } from "../utils/thoughtText";

const ChartBlock = lazy(() =>
  import("./ChartBlock").then((m) => ({ default: m.ChartBlock })),
);

function SectionPanel({
  title,
  children,
  defaultOpen = false,
  action,
  compact = false,
  visual = false,
}: {
  title: string;
  children: ReactNode;
  defaultOpen?: boolean;
  /** Control rendered at the right of the summary row, e.g. maximise. */
  action?: ReactNode;
  compact?: boolean;
  visual?: boolean;
}) {
  if (compact && visual) {
    return (
      <Box
        sx={{
          position: "relative",
          borderTop: 1,
          borderColor: "divider",
          px: 1,
          py: 0.5,
        }}
      >
        {action && (
          <Box
            onClick={(event) => event.stopPropagation()}
            onKeyDown={(event) => event.stopPropagation()}
            sx={{ position: "absolute", top: 1, right: 1, zIndex: 1 }}
          >
            {action}
          </Box>
        )}
        {children}
      </Box>
    );
  }

  return (
    <Accordion
      defaultExpanded={defaultOpen}
      disableGutters
      elevation={0}
      sx={{
        border: compact ? 0 : "1px solid",
        borderTop: compact ? 1 : undefined,
        borderColor: "divider",
        borderRadius: compact ? 0 : 1,
        "&:before": { display: "none" },
        "& .MuiAccordionSummary-root": {
          minHeight: compact ? 24 : 32,
          px: 1,
        },
        "& .MuiAccordionSummary-content": {
          my: compact ? 0.25 : 0.5,
        },
      }}
    >
      <AccordionSummary expandIcon={<ExpandMore sx={{ fontSize: compact ? 14 : 18 }} />}>
        <Box
          sx={{
            display: "flex",
            alignItems: "center",
            justifyContent: "space-between",
            gap: 1,
            width: "100%",
            pr: 1,
          }}
        >
          <Typography
            variant="caption"
            sx={{
              fontWeight: 650,
              color: "text.secondary",
              ...(compact ? { fontSize: 10 } : {}),
            }}
          >
            {title}
          </Typography>
          {action && (
            // Keep the action out of the accordion's toggle: a click here must
            // act, not collapse the panel it lives in.
            <Box
              onClick={(event) => event.stopPropagation()}
              onKeyDown={(event) => event.stopPropagation()}
              sx={{ display: "flex", alignItems: "center" }}
            >
              {action}
            </Box>
          )}
        </Box>
      </AccordionSummary>
      <AccordionDetails sx={{ pt: 0, px: 1, pb: compact ? 0.75 : 1 }}>
        {children}
      </AccordionDetails>
    </Accordion>
  );
}

export interface AssistantTurnProps {
  turn: TurnResponse;
  resultRows?: Record<string, unknown>[];
  visibility?: TraceVisibility;
  onRephrase?: (originalMessage: string) => void;
  onResend?: (prompt: string) => void;
  onFeedback?: (vote: "up" | "down") => void;
  feedbackEnabled?: boolean;
  onOpenTrace?: () => void;
  suggestedQuestions?: string[];
  onSelectQuestion?: (q: string) => void;
  echartsTheme?: Record<string, unknown>;
  chartsCss?: string;
  actionsDisabled?: boolean;
  compact?: boolean;
  /** Excel-only action rail; default hosts keep their existing external rail. */
  turnActions?: ReactNode;
  /**
   * Take over the Visual panel's maximise. Excel supplies this so the control
   * opens a real Office dialog window: a task pane cannot be widened, so the
   * in-pane overlay below can never show the visual any larger than the pane
   * that already holds it. Hosts that leave it undefined — the web app — keep
   * the overlay, where it does have the whole browser window to grow into.
   */
  onMaximizeVisual?: () => void;
}

function refusedDetailText(
  turn: TurnResponse,
  t: (key: string, params?: Record<string, string | number>) => string,
) {
  const action = (turn.guardrail_actions || []).find(
    (item): item is { message_i18n_key?: unknown } =>
      typeof item === "object" && item !== null && "message_i18n_key" in item,
  );
  const key = action?.message_i18n_key;
  if (typeof key === "string" && key) {
    return t(key);
  }
  return turn.answer_text || t("turn.refusedSafeDetail");
}

export function AssistantTurn({
  turn,
  resultRows,
  visibility,
  onRephrase,
  onResend,
  onFeedback,
  feedbackEnabled,
  onOpenTrace,
  suggestedQuestions,
  onSelectQuestion,
  echartsTheme,
  chartsCss,
  actionsDisabled = false,
  compact = false,
  turnActions,
  onMaximizeVisual,
}: AssistantTurnProps) {
  const { t } = useChatContext();
  const isJudgeBlocked = turn.status === "judge_blocked";
  const isRefused = turn.status === "refused";
  const isBlocked = isRefused || isJudgeBlocked;
  const isError = turn.status === "error";
  const [blockedExpanded, setBlockedExpanded] = useState(isRefused);
  // Bug-8181 — which citation's provenance dialog is open, if any. Each chip
  // opens its OWN dialog now (a per-citation click target), not the shared
  // generic trace drawer every chip used to invoke.
  const [selectedCitation, setSelectedCitation] = useState<Citation | null>(
    null,
  );
  // Whether the visual is shown enlarged in its own dialog. The dialog renders
  // the same block, so there is a single definition of what "the visual" is.
  const [visualMaximized, setVisualMaximized] = useState(false);
  const [citationsOpen, setCitationsOpen] = useState(false);
  // Bug-5960: stable id linking the disclosure trigger to its panel via
  // aria-controls (per turn, so multiple blocked turns in one conversation
  // don't collide).
  const blockedDetailId = useId();
  const visualArtifact = useMemo(
    () => parseVisualArtifact(turn.rendered_output),
    [turn.rendered_output],
  );
  const artifactRows = visualArtifact?.rows;
  const tableRows =
    visualArtifact && visualArtifact.include_table === false
      ? undefined
      : artifactRows && artifactRows.length > 0
      ? artifactRows
      : resultRows && resultRows.length > 0
        ? resultRows
        : undefined;
  const tableColumns = Object.keys(tableRows?.[0] ?? {});

  const hasChart = useMemo(() => {
    if (visualArtifact?.chart_type) return visualArtifact.chart_type !== "kpi";
    if (!resultRows || resultRows.length === 0) return false;
    const spec = buildAutoChartSpec(resultRows);
    return spec !== null && spec.kind !== "metric";
  }, [resultRows, visualArtifact]);

  // The visual, defined once and rendered in two places: inline in the turn and
  // enlarged in the maximise dialog. `heightOverride` is what lets the dialog
  // give the chart the height it has available instead of its inline size.
  // Order mirrors the source precedence: a parsed artifact wins over raw
  // rendered output, which wins over a chart derived from the result rows.
  //
  // Bug-9921: no compact fallback is passed here any more — when
  // heightOverride is undefined and compact is true, VisualArtifactBlock/
  // ChartBlock derive their own height from the pane's visible height (see
  // utils/chartLayout.ts) via a live hook, which a static prop value here
  // could not keep in sync with window resizes.
  const renderVisualBody = (heightOverride?: number | string): ReactNode => {
    if (visualArtifact) {
      return visualArtifact.chart_type ? (
        <VisualArtifactBlock
          artifact={visualArtifact}
          echartsTheme={echartsTheme}
          heightOverride={heightOverride}
          compact={compact}
        />
      ) : null;
    }
    if (turn.rendered_output) {
      return (
        <RenderedOutput html={turn.rendered_output} chartsCss={chartsCss} compact={compact} />
      );
    }
    if (resultRows && resultRows.length > 0 && hasChart) {
      return (
        <Suspense fallback={null}>
          <ChartBlock
            rows={resultRows}
            echartsTheme={echartsTheme}
            heightOverride={heightOverride}
            compact={compact}
          />
        </Suspense>
      );
    }
    return null;
  };
  const visualBody = renderVisualBody();

  const showThought = visibility?.showThoughtProcess ?? !!turn.thought_summary;
  const showSemantic = visibility?.showSemanticQuery ?? !!turn.semantic_query;
  const showPhysical = visibility?.showPhysicalQuery ?? !!turn.routed_sql;
  const hasSafeRetainedTrace =
    Boolean(visibility) &&
    ((showThought && Boolean(turn.thought_summary)) ||
      (showSemantic && Boolean(turn.semantic_query)) ||
      (showPhysical && Boolean(turn.routed_sql)));

  const routeLabel = turn.route;
  const routeColor =
    routeLabel === "aggregate"
      ? "#e8f5e9"
      : routeLabel === "pocket"
        ? "#f3e5f5"
        : routeLabel === "source"
          ? "#fff8e1"
          : undefined;
  const routeTextColor =
    routeLabel === "aggregate"
      ? "#2e7d32"
      : routeLabel === "pocket"
        ? "#7b1fa2"
        : routeLabel === "source"
          ? "#f57f17"
          : undefined;

  return (
    <Box
      sx={{
        display: "flex",
        justifyContent: "flex-start",
        mb: compact ? 1 : 2,
        minWidth: 0,
      }}
    >
      <Card
        variant="outlined"
        sx={{
          maxWidth: "100%",
          width: "100%",
          minWidth: 0,
          overflow: "visible",
          borderRadius: compact ? 0.5 : undefined,
          borderColor: isBlocked
            ? (compact ? "#D4AF37" : "warning.main")
            : isError
              ? (compact ? "#B33A3A" : "error.main")
              : "divider",
          bgcolor: isBlocked
            ? (compact ? "#fff8e1" : "warning.50")
            : isError
              ? (compact ? "#ffebee" : "error.50")
              : "background.paper",
        }}
      >
        <CardContent
          sx={{
            display: "flex",
            flexDirection: "column",
            gap: compact ? 0 : 1.25,
            minWidth: 0,
            p: compact ? (isBlocked || isError ? 1 : 0) : undefined,
            "&:last-child": compact ? { pb: isBlocked || isError ? 1 : 0 } : { pb: 2 },
          }}
        >
          {isBlocked && (
            <Box>
              {/* Bug-5960: this was a plain clickable Box with an inert
                  IconButton indicator — mouse-only, no keyboard activation,
                  and no aria-expanded/aria-controls, so keyboard and
                  screen-reader users could not discover or operate the
                  blocked/refused detail disclosure. Rendered as a real
                  <button> (via Box component="button") with disclosure
                  ARIA semantics; the chevron becomes a non-interactive,
                  aria-hidden decoration so there is no nested-button HTML
                  and only one focusable/tab-stop control for this action. */}
              <Box
                component="button"
                type="button"
                onClick={() => setBlockedExpanded((v) => !v)}
                aria-expanded={blockedExpanded}
                aria-controls={blockedDetailId}
                aria-label={
                  isJudgeBlocked
                    ? t("turn.blockedQuality")
                    : t("turn.refused")
                }
                sx={{
                  display: "flex",
                  alignItems: "center",
                  justifyContent: "space-between",
                  width: "100%",
                  cursor: "pointer",
                  border: 0,
                  p: 0,
                  m: 0,
                  bgcolor: "transparent",
                  font: "inherit",
                  textAlign: "left",
                  color: "inherit",
                  "&:focus-visible": {
                    outline: "2px solid",
                    outlineColor: "warning.dark",
                    outlineOffset: 2,
                  },
                }}
              >
                <Typography
                  variant="subtitle2"
                  color="warning.dark"
                  fontWeight={600}
                  sx={compact ? { fontSize: 11 } : undefined}
                >
                  {isJudgeBlocked
                    ? t("turn.blockedQuality")
                    : t("turn.refused")}
                </Typography>
                <Box component="span" aria-hidden="true" sx={{ display: "inline-flex", p: "5px" }}>
                  {blockedExpanded ? (
                    <ExpandLess fontSize="small" />
                  ) : (
                    <ExpandMore fontSize="small" />
                  )}
                </Box>
              </Box>
              <Collapse in={blockedExpanded}>
                <Box
                  id={blockedDetailId}
                  sx={{
                    p: compact ? 0.75 : 1.5,
                    mt: compact ? 0.5 : 0.75,
                    bgcolor: compact ? "#fff8e1" : "warning.50",
                    border: 1,
                    borderColor: compact ? "#D4AF37" : "warning.200",
                    borderRadius: compact ? 0.5 : 1,
                  }}
                >
                  <Typography
                    variant="body2"
                    color="text.secondary"
                    sx={{
                      whiteSpace: "pre-wrap",
                      ...(compact ? { fontSize: 11, lineHeight: 1.35 } : {}),
                    }}
                  >
                    {isJudgeBlocked
                      ? t("turn.blockedSafeDetail")
                      : refusedDetailText(turn, t)}
                  </Typography>
                </Box>
              </Collapse>
              {isJudgeBlocked && (
                <JudgeBlockCard
                  turn={turn}
                  onResend={onResend}
                  disabled={actionsDisabled}
                />
              )}
              {tableRows && tableRows.length > 0 && (
                <Box sx={{ mt: 1 }}>
                  <DataTableBlock rows={tableRows} compact={compact} />
                </Box>
              )}
              <Box
                sx={{
                  display: "flex",
                  alignItems: "center",
                  flexWrap: "wrap",
                  gap: compact ? 0.5 : 1,
                  mt: compact ? 0.5 : 1,
                }}
              >
                <Typography
                  variant="caption"
                  color="text.secondary"
                  sx={compact ? { fontSize: 10, lineHeight: 1.2 } : undefined}
                >
                  {t("turn.rephraseHint")}
                </Typography>
                {onRephrase && (
                  <Button
                    size="small"
                    variant="outlined"
                    color="warning"
                    startIcon={<Refresh sx={{ fontSize: compact ? 14 : undefined }} />}
                    disabled={actionsDisabled}
                    onClick={() => onRephrase(turn.user_message)}
                    sx={{
                      textTransform: "none",
                      minWidth: 0,
                      py: compact ? 0.125 : 0.25,
                      px: compact ? 0.75 : 1,
                      ...(compact
                        ? { height: 20, fontSize: 10, borderRadius: "2px" }
                        : {}),
                    }}
                  >
                    {t("turn.rephrase")}
                  </Button>
                )}
                {onOpenTrace && hasSafeRetainedTrace && (
                  <Button
                    size="small"
                    variant="text"
                    color="inherit"
                    startIcon={<Visibility sx={{ fontSize: compact ? 14 : undefined }} />}
                    onClick={onOpenTrace}
                    sx={{
                      textTransform: "none",
                      minWidth: 0,
                      py: compact ? 0.125 : 0.25,
                      px: compact ? 0.75 : 1,
                      ...(compact
                        ? { height: 20, fontSize: 10, borderRadius: "2px" }
                        : {}),
                    }}
                  >
                    {t("turn.viewTrace")}
                  </Button>
                )}
              </Box>
              {compact && turnActions && (
                <Box sx={{ mt: 0.75, pt: 0.75, borderTop: 1, borderColor: "divider" }}>
                  {turnActions}
                </Box>
              )}
            </Box>
          )}

          {isError && (
            <Box>
              <Typography variant="body2" color="error.main" sx={compact ? { fontSize: 11, lineHeight: 1.4 } : undefined}>
                {turn.answer_text || t("turn.errorFallback")}
              </Typography>
              {compact && turnActions && (
                <Box sx={{ mt: 0.75, pt: 0.75, borderTop: 1, borderColor: "divider" }}>
                  {turnActions}
                </Box>
              )}
            </Box>
          )}

          {!isBlocked && !isError && (
            <>
              <MetadataBadges turn={turn} compact={compact} />

              {/* The visual leads the answer when there is one: the chart is
                  the result, the prose explains it. With no visual this
                  renders nothing and the text stays first. */}
              {visualBody && (
                <SectionPanel
                  title={t("turn.visual")}
                  defaultOpen
                  compact={compact}
                  visual={compact}
                  action={
                    <Tooltip title={t("turn.maximizeVisual")}>
                      <IconButton
                        size="small"
                        aria-label={t("turn.maximizeVisual")}
                        onClick={
                          onMaximizeVisual ?? (() => setVisualMaximized(true))
                        }
                        sx={compact ? { width: 24, height: 24, p: 0 } : undefined}
                      >
                        <Fullscreen sx={{ fontSize: compact ? 14 : 18 }} />
                      </IconButton>
                    </Tooltip>
                  }
                >
                  {visualBody}
                </SectionPanel>
              )}

              {turn.answer_text && <MarkdownAnswer content={turn.answer_text} compact={compact} />}

              {turn.calculation_steps &&
                turn.calculation_steps.length > 0 && (
                  <Accordion
                    disableGutters
                    elevation={0}
                    sx={{
                      border: compact ? 0 : "1px solid",
                      borderTop: compact ? 1 : undefined,
                      borderColor: "divider",
                      borderRadius: compact ? 0 : undefined,
                      "&:before": { display: "none" },
                      "& .MuiAccordionSummary-root": {
                        minHeight: compact ? 24 : undefined,
                        px: compact ? 1 : undefined,
                      },
                      "& .MuiAccordionSummary-content": {
                        my: compact ? 0.25 : undefined,
                      },
                    }}
                  >
                    <AccordionSummary
                      expandIcon={<ExpandMore sx={{ fontSize: compact ? 14 : 18 }} />}
                    >
                      <Typography variant="caption" sx={{ fontWeight: 600, ...(compact ? { fontSize: 10 } : {}) }}>
                        {t("turn.calculationSteps", {
                          count: String(turn.calculation_steps.length),
                        })}
                      </Typography>
                    </AccordionSummary>
                    <AccordionDetails sx={{ pt: 0, px: compact ? 1 : undefined, pb: compact ? 0.75 : undefined }}>
                      <Table size="small">
                        <TableHead>
                          <TableRow>
                            <TableCell sx={{ fontSize: compact ? 10 : 12, fontWeight: 600, py: compact ? 0.375 : undefined }}>
                              #
                            </TableCell>
                            <TableCell sx={{ fontSize: compact ? 10 : 12, fontWeight: 600, py: compact ? 0.375 : undefined }}>
                              {t("turn.calcStepDescription")}
                            </TableCell>
                            <TableCell
                              align="right"
                              sx={{ fontSize: compact ? 10 : 12, fontWeight: 600, py: compact ? 0.375 : undefined }}
                            >
                              {t("turn.calcStepValue")}
                            </TableCell>
                          </TableRow>
                        </TableHead>
                        <TableBody>
                          {turn.calculation_steps.map((step, idx) => (
                            <TableRow
                              key={step.step_number ?? step.step ?? idx}
                            >
                              <TableCell sx={{ fontSize: compact ? 11 : 12, py: compact ? 0.375 : undefined }}>
                                {step.step_number ?? step.step ?? idx + 1}
                              </TableCell>
                              <TableCell sx={{ fontSize: compact ? 11 : 12, py: compact ? 0.375 : undefined }}>
                                {step.description ?? step.name ?? ""}
                                {step.formula && (
                                  <Typography
                                    variant="caption"
                                    color="text.secondary"
                                    sx={{ display: "block", ...(compact ? { fontSize: 10 } : {}) }}
                                  >
                                    {t("turn.calcStepFormula", {
                                      formula: step.formula,
                                    })}
                                  </Typography>
                                )}
                                {step.result_preview && (
                                  <Typography
                                    variant="caption"
                                    color="text.secondary"
                                    sx={{ display: "block", ...(compact ? { fontSize: 10 } : {}) }}
                                  >
                                    {step.result_preview}
                                  </Typography>
                                )}
                              </TableCell>
                              <TableCell
                                align="right"
                                sx={{ fontSize: compact ? 10 : 12, fontFamily: "monospace", py: compact ? 0.375 : undefined }}
                              >
                                {step.formatted_value ??
                                  (step.value != null
                                    ? String(step.value)
                                    : "")}
                              </TableCell>
                            </TableRow>
                          ))}
                        </TableBody>
                      </Table>
                    </AccordionDetails>
                  </Accordion>
                )}

              {visualArtifact?.legacy_html && (
                <SectionPanel title={t("turn.supportingDetails")} compact={compact}>
                  <RenderedOutput
                    html={visualArtifact.legacy_html}
                    chartsCss={chartsCss}
                    compact={compact}
                  />
                </SectionPanel>
              )}

              {tableRows && tableRows.length > 0 && (
                <SectionPanel
                  title={t("turn.showData", {
                    count: String(tableRows.length),
                  })}
                  compact={compact}
                >
                  {compact && tableColumns.length > 0 && (
                    <Typography
                      component="div"
                      variant="caption"
                      color="text.secondary"
                      sx={{
                        mb: 0.25,
                        fontSize: 9,
                        lineHeight: 1.2,
                        textAlign: "right",
                        whiteSpace: "nowrap",
                        overflow: "hidden",
                        textOverflow: "ellipsis",
                      }}
                    >
                      {tableColumns.join(" · ")}
                    </Typography>
                  )}
                  <DataTableBlock rows={tableRows} compact={compact} />
                </SectionPanel>
              )}

              {!compact && turn.citations && turn.citations.length > 0 && (
                <CitationChips
                  citations={turn.citations}
                  onClick={(citation) => setSelectedCitation(citation)}
                />
              )}

              {((showThought && Boolean(turn.thought_summary)) ||
                (showSemantic && Boolean(turn.semantic_query)) ||
                (showPhysical && Boolean(turn.routed_sql))) && (
                <SectionPanel title={t("turn.diagnostics")} compact={compact}>
                  <Stack spacing={compact ? 0.5 : 1}>
                    {showThought && Boolean(turn.thought_summary) && (
                      <Box>
                        <Typography variant="caption" color="text.secondary" fontWeight={650}>
                          {t("turn.thinking")}
                        </Typography>
                        <Box
                          sx={{
                            mt: 0.5,
                            maxHeight: "8rem",
                            overflowY: "auto",
                            fontSize: compact ? 10.5 : 12,
                            lineHeight: compact ? 1.35 : 1.45,
                            whiteSpace: "pre-wrap",
                            overflowWrap: "anywhere",
                            color: "text.secondary",
                            bgcolor: "action.hover",
                            borderRadius: 1,
                            p: compact ? 0.75 : 1,
                          }}
                        >
                          {formatThoughtText(turn.thought_summary ?? "")}
                        </Box>
                      </Box>
                    )}
                    {showSemantic && Boolean(turn.semantic_query) && (
                      <Box>
                        <Typography variant="caption" color="text.secondary" fontWeight={650}>
                          {t("turn.semanticQuery")}
                        </Typography>
                        <Box
                          component="pre"
                          sx={{
                            mt: 0.5,
                            p: compact ? 0.75 : 1,
                            m: 0,
                            fontSize: compact ? 10 : 11,
                            overflowX: "auto",
                            whiteSpace: "pre-wrap",
                            bgcolor: "action.hover",
                            borderRadius: 1,
                          }}
                        >
                          {JSON.stringify(turn.semantic_query, null, 2)}
                        </Box>
                      </Box>
                    )}
                    {showPhysical && Boolean(turn.routed_sql) && (
                      <Box>
                        <Typography variant="caption" color="text.secondary" fontWeight={650}>
                          {t("turn.physicalQuery")}
                        </Typography>
                        <Box
                          component="pre"
                          sx={{
                            mt: 0.5,
                            p: compact ? 0.75 : 1,
                            m: 0,
                            fontSize: compact ? 10 : 11,
                            overflowX: "auto",
                            whiteSpace: "pre-wrap",
                            bgcolor: "action.hover",
                            borderRadius: 1,
                          }}
                        >
                          {turn.routed_sql}
                        </Box>
                      </Box>
                    )}
                  </Stack>
                </SectionPanel>
              )}

              {!compact && (
                <Stack
                  direction="row"
                  spacing={0.25}
                  alignItems="center"
                  sx={{ mt: 0.5 }}
                >
                  {feedbackEnabled && onFeedback && (
                    <FeedbackButtons onFeedback={onFeedback} />
                  )}
                  {routeLabel && routeColor && (
                    <Typography
                      variant="caption"
                      sx={{
                        px: 0.75,
                        py: 0.15,
                        borderRadius: 0.5,
                        fontWeight: 600,
                        fontSize: 10,
                        bgcolor: routeColor,
                        color: routeTextColor,
                      }}
                    >
                      {routeLabel.toUpperCase()}
                    </Typography>
                  )}
                  {turn.provider && (
                    <Typography
                      variant="caption"
                      color="text.secondary"
                      sx={{ ml: "auto", fontSize: 11 }}
                    >
                      {t("turn.answeredBy", { provider: turn.provider })}
                    </Typography>
                  )}
                </Stack>
              )}

              {compact && (
                <>
                  <Box
                    sx={{
                      display: "flex",
                      alignItems: "center",
                      gap: 0.5,
                      minHeight: 30,
                      px: 0.75,
                      minWidth: 0,
                      flexWrap: "wrap",
                      borderTop: 1,
                      borderColor: "divider",
                    }}
                  >
                    {turnActions && (
                      <Box sx={{ minWidth: 0, flex: "1 1 auto" }}>
                        {turnActions}
                      </Box>
                    )}
                    {turn.citations && turn.citations.length > 0 && (
                      <Button
                        size="small"
                        variant="outlined"
                        aria-label={t("citations.label")}
                        title={t("citations.label")}
                        onClick={() => setCitationsOpen((v) => !v)}
                        sx={{ minWidth: 26, width: 26, height: 24, p: 0, fontSize: 11 }}
                      >
                        {turn.citations.length}
                      </Button>
                    )}
                    {onOpenTrace && hasSafeRetainedTrace && (
                      <Tooltip title={t("turn.viewTrace")}>
                        <IconButton
                          size="small"
                          aria-label={t("turn.viewTrace")}
                          onClick={onOpenTrace}
                          sx={{ width: 26, height: 24, p: 0, border: 1, borderColor: "divider", borderRadius: 0.5 }}
                        >
                          <ManageSearchOutlined sx={{ fontSize: 14 }} />
                        </IconButton>
                      </Tooltip>
                    )}
                    <JudgeVerdictStrip turn={turn} compact />
                    {feedbackEnabled && onFeedback && (
                      <Box sx={{ ml: "auto" }}>
                        <FeedbackButtons onFeedback={onFeedback} compact />
                      </Box>
                    )}
                  </Box>
                  {citationsOpen && turn.citations && turn.citations.length > 0 && (
                    <Box sx={{ px: 1, py: 0.5, borderTop: 1, borderColor: "divider" }}>
                      <CitationChips
                        citations={turn.citations}
                        compact
                        onClick={(citation) => setSelectedCitation(citation)}
                      />
                    </Box>
                  )}
                </>
              )}

              {!compact && <JudgeVerdictStrip turn={turn} />}

              {suggestedQuestions &&
                suggestedQuestions.length > 0 &&
                onSelectQuestion && (
                  <Stack
                    direction="row"
                    gap={0.5}
                    flexWrap="wrap"
                    sx={{ mt: 1 }}
                  >
                    {suggestedQuestions.map((q) => (
                      <Chip
                        key={q}
                        label={q}
                        size="small"
                        variant="outlined"
                        onClick={() => onSelectQuestion(q)}
                        sx={{
                          fontSize: compact ? 11 : 12,
                          height: compact ? 20 : undefined,
                          borderRadius: compact ? "2px" : undefined,
                          cursor: "pointer",
                        }}
                      />
                    ))}
                  </Stack>
                )}
            </>
          )}
        </CardContent>
      </Card>
      <CitationProvenanceDialog
        citation={selectedCitation}
        open={selectedCitation !== null}
        onClose={() => setSelectedCitation(null)}
        onOpenTrace={
          onOpenTrace
            ? () => {
                setSelectedCitation(null);
                onOpenTrace();
              }
            : undefined
        }
      />
      {/* Maximised visual. The dialog body is only mounted while open, so the
          enlarged chart is a second instance that exists only on demand. */}
      <Dialog
        open={visualMaximized}
        onClose={() => setVisualMaximized(false)}
        fullWidth
        maxWidth="xl"
        aria-label={t("turn.visual")}
      >
        <DialogTitle
          sx={{
            display: "flex",
            alignItems: "center",
            justifyContent: "space-between",
            gap: 1,
            py: 1.25,
          }}
        >
          <Typography component="span" sx={{ fontWeight: 650, fontSize: 15 }}>
            {t("turn.visual")}
          </Typography>
          <IconButton
            size="small"
            aria-label={t("turn.closeVisual")}
            onClick={() => setVisualMaximized(false)}
          >
            <Close sx={{ fontSize: 20 }} />
          </IconButton>
        </DialogTitle>
        <DialogContent dividers>{renderVisualBody("70vh")}</DialogContent>
      </Dialog>
    </Box>
  );
}
