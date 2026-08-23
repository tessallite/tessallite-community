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
  Stack,
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableRow,
  Typography,
} from "@mui/material";
import {
  ExpandMore,
  ExpandLess,
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
}: {
  title: string;
  children: ReactNode;
  defaultOpen?: boolean;
}) {
  return (
    <Accordion
      defaultExpanded={defaultOpen}
      disableGutters
      elevation={0}
      sx={{
        border: "1px solid",
        borderColor: "divider",
        borderRadius: 1,
        "&:before": { display: "none" },
        "& .MuiAccordionSummary-root": {
          minHeight: 32,
          px: 1,
        },
        "& .MuiAccordionSummary-content": {
          my: 0.5,
        },
      }}
    >
      <AccordionSummary expandIcon={<ExpandMore sx={{ fontSize: 18 }} />}>
        <Typography variant="caption" sx={{ fontWeight: 650, color: "text.secondary" }}>
          {title}
        </Typography>
      </AccordionSummary>
      <AccordionDetails sx={{ pt: 0, px: 1, pb: 1 }}>{children}</AccordionDetails>
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

  const hasChart = useMemo(() => {
    if (visualArtifact?.chart_type) return visualArtifact.chart_type !== "kpi";
    if (!resultRows || resultRows.length === 0) return false;
    const spec = buildAutoChartSpec(resultRows);
    return spec !== null && spec.kind !== "metric";
  }, [resultRows, visualArtifact]);

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
        mb: 2,
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
          borderColor: isBlocked
            ? "warning.main"
            : isError
              ? "error.main"
              : "divider",
          bgcolor: isBlocked
            ? "warning.50"
            : isError
              ? "error.50"
              : "background.paper",
        }}
      >
        <CardContent
          sx={{
            display: "flex",
            flexDirection: "column",
            gap: 1.25,
            minWidth: 0,
            "&:last-child": { pb: 2 },
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
                    p: 1.5,
                    mt: 0.75,
                    bgcolor: "warning.50",
                    border: 1,
                    borderColor: "warning.200",
                    borderRadius: 1,
                  }}
                >
                  <Typography
                    variant="body2"
                    color="text.secondary"
                    sx={{ whiteSpace: "pre-wrap" }}
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
                  <DataTableBlock rows={tableRows} />
                </Box>
              )}
              <Box
                sx={{
                  display: "flex",
                  alignItems: "center",
                  flexWrap: "wrap",
                  gap: 1,
                  mt: 1,
                }}
              >
                <Typography variant="caption" color="text.secondary">
                  {t("turn.rephraseHint")}
                </Typography>
                {onRephrase && (
                  <Button
                    size="small"
                    variant="outlined"
                    color="warning"
                    startIcon={<Refresh fontSize="small" />}
                    disabled={actionsDisabled}
                    onClick={() => onRephrase(turn.user_message)}
                    sx={{
                      textTransform: "none",
                      minWidth: 0,
                      py: 0.25,
                      px: 1,
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
                    startIcon={<Visibility fontSize="small" />}
                    onClick={onOpenTrace}
                    sx={{
                      textTransform: "none",
                      minWidth: 0,
                      py: 0.25,
                      px: 1,
                    }}
                  >
                    {t("turn.viewTrace")}
                  </Button>
                )}
              </Box>
            </Box>
          )}

          {isError && (
            <Typography variant="body2" color="error.main">
              {turn.answer_text || t("turn.errorFallback")}
            </Typography>
          )}

          {!isBlocked && !isError && (
            <>
              <MetadataBadges turn={turn} />

              {turn.answer_text && (
                <MarkdownAnswer content={turn.answer_text} />
              )}

              {turn.calculation_steps &&
                turn.calculation_steps.length > 0 && (
                  <Accordion
                    disableGutters
                    elevation={0}
                    sx={{
                      border: "1px solid",
                      borderColor: "divider",
                      "&:before": { display: "none" },
                    }}
                  >
                    <AccordionSummary
                      expandIcon={<ExpandMore sx={{ fontSize: 18 }} />}
                    >
                      <Typography variant="caption" sx={{ fontWeight: 600 }}>
                        {t("turn.calculationSteps", {
                          count: String(turn.calculation_steps.length),
                        })}
                      </Typography>
                    </AccordionSummary>
                    <AccordionDetails sx={{ pt: 0 }}>
                      <Table size="small">
                        <TableHead>
                          <TableRow>
                            <TableCell sx={{ fontSize: 12, fontWeight: 600 }}>
                              #
                            </TableCell>
                            <TableCell sx={{ fontSize: 12, fontWeight: 600 }}>
                              {t("turn.calcStepDescription")}
                            </TableCell>
                            <TableCell
                              align="right"
                              sx={{ fontSize: 12, fontWeight: 600 }}
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
                              <TableCell sx={{ fontSize: 12 }}>
                                {step.step_number ?? step.step ?? idx + 1}
                              </TableCell>
                              <TableCell sx={{ fontSize: 12 }}>
                                {step.description ?? step.name ?? ""}
                                {step.formula && (
                                  <Typography
                                    variant="caption"
                                    color="text.secondary"
                                    sx={{ display: "block" }}
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
                                    sx={{ display: "block" }}
                                  >
                                    {step.result_preview}
                                  </Typography>
                                )}
                              </TableCell>
                              <TableCell
                                align="right"
                                sx={{ fontSize: 12, fontFamily: "monospace" }}
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

              {visualArtifact ? (
                <>
                  {visualArtifact.chart_type && (
                    <SectionPanel title={t("turn.visual")} defaultOpen>
                      <VisualArtifactBlock
                        artifact={visualArtifact}
                        echartsTheme={echartsTheme}
                      />
                    </SectionPanel>
                  )}
                  {visualArtifact.legacy_html && (
                    <SectionPanel title={t("turn.supportingDetails")}>
                      <RenderedOutput
                        html={visualArtifact.legacy_html}
                        chartsCss={chartsCss}
                      />
                    </SectionPanel>
                  )}
                </>
              ) : turn.rendered_output ? (
                <SectionPanel title={t("turn.visual")} defaultOpen>
                  <RenderedOutput
                    html={turn.rendered_output}
                    chartsCss={chartsCss}
                  />
                </SectionPanel>
              ) : resultRows && resultRows.length > 0 && hasChart ? (
                <SectionPanel title={t("turn.visual")} defaultOpen>
                  <Suspense fallback={null}>
                    <ChartBlock
                      rows={resultRows}
                      echartsTheme={echartsTheme}
                    />
                  </Suspense>
                </SectionPanel>
              ) : null}

              {tableRows && tableRows.length > 0 && (
                <SectionPanel
                  title={t("turn.showData", {
                    count: String(tableRows.length),
                  })}
                >
                  <DataTableBlock rows={tableRows} />
                </SectionPanel>
              )}

              {turn.citations && turn.citations.length > 0 && (
                <CitationChips
                  citations={turn.citations}
                  onClick={(citation) => setSelectedCitation(citation)}
                />
              )}

              {((showThought && Boolean(turn.thought_summary)) ||
                (showSemantic && Boolean(turn.semantic_query)) ||
                (showPhysical && Boolean(turn.routed_sql))) && (
                <SectionPanel title={t("turn.diagnostics")}>
                  <Stack spacing={1}>
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
                            fontSize: 12,
                            lineHeight: 1.45,
                            whiteSpace: "pre-wrap",
                            overflowWrap: "anywhere",
                            color: "text.secondary",
                            bgcolor: "action.hover",
                            borderRadius: 1,
                            p: 1,
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
                            p: 1,
                            m: 0,
                            fontSize: 11,
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
                            p: 1,
                            m: 0,
                            fontSize: 11,
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

              <JudgeVerdictStrip turn={turn} />

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
                        sx={{ fontSize: 12, cursor: "pointer" }}
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
    </Box>
  );
}
