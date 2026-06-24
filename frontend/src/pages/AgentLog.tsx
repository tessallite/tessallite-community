import { useCallback, useMemo, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import {
  Alert,
  Box,
  Button,
  Card,
  CardContent,
  CircularProgress,
  Collapse,
  IconButton,
  Stack,
  Table,
  TableBody,
  TableCell,
  TableContainer,
  TableHead,
  TableRow,
  TextField,
  Tooltip,
  Typography,
} from "@mui/material";
import ArrowBackIcon from "@mui/icons-material/ArrowBack";
import SearchIcon from "@mui/icons-material/SearchOutlined";
import ExpandMoreIcon from "@mui/icons-material/ExpandMore";
import ChevronLeftIcon from "@mui/icons-material/ChevronLeft";
import ChevronRightIcon from "@mui/icons-material/ChevronRight";
import ThumbUpIcon from "@mui/icons-material/ThumbUp";
import ThumbDownIcon from "@mui/icons-material/ThumbDown";
import { useProject } from "../api/hooks";
import { agentApi, type AgentKpis, type LogFilters, type LogTurnRow } from "../api/agentApi";
import { statusColor } from "../theme/tokens";
import HelpIconButton from "../components/HelpIconButton";
import { useT } from "../i18n";

function formatTokens(n: number): string {
  if (n >= 1000) return `${(n / 1000).toFixed(1)}k`;
  return String(n);
}

function formatLatency(ms: number): string {
  if (ms >= 1000) return `${(ms / 1000).toFixed(1)}s`;
  return `${ms}ms`;
}

function verdictSeverity(v: string | null): string {
  if (!v) return "default";
  if (v === "pass") return "success";
  if (v === "fail") return "error";
  return "warning";
}

function statusSeverity(s: string): string {
  if (s === "ok") return "success";
  if (s === "refused") return "warning";
  if (s === "error") return "error";
  return "default";
}

function StatusBadge({ label, severity }: { label: string; severity: string }) {
  const sc = statusColor(severity);
  return (
    <Typography
      component="span"
      variant="caption"
      sx={{
        px: 0.5,
        py: 0.125,
        borderRadius: 0.5,
        fontWeight: 500,
        fontSize: 11,
        bgcolor: sc.bg,
        color: sc.fg,
        whiteSpace: "nowrap",
      }}
    >
      {label}
    </Typography>
  );
}

function DetailBlock({
  label,
  children,
}: {
  label: string;
  children: React.ReactNode;
}) {
  if (!children) return null;
  return (
    <Box sx={{ mb: 1.5 }}>
      <Typography
        variant="caption"
        color="text.secondary"
        sx={{ fontWeight: 600, display: "block", mb: 0.25 }}
      >
        {label}
      </Typography>
      {children}
    </Box>
  );
}

function CodeBlock({ value }: { value: string }) {
  return (
    <Box
      component="pre"
      sx={{
        m: 0,
        p: 1,
        bgcolor: "grey.50",
        border: 1,
        borderColor: "divider",
        borderRadius: 0.5,
        fontSize: 12,
        fontFamily: "'JetBrains Mono', 'Fira Code', monospace",
        overflow: "auto",
        maxHeight: 200,
        whiteSpace: "pre-wrap",
        wordBreak: "break-word",
      }}
    >
      {value}
    </Box>
  );
}

function JsonBlock({ value }: { value: unknown }) {
  return <CodeBlock value={JSON.stringify(value, null, 2)} />;
}

function PromptBlock({
  messages,
}: {
  messages: Record<string, string>;
}) {
  return (
    <Box sx={{ display: "flex", flexDirection: "column", gap: 1 }}>
      {Object.entries(messages).map(([role, text]) => (
        <Box key={role}>
          <Typography
            variant="caption"
            sx={{ fontWeight: 700, textTransform: "uppercase", mb: 0.25, display: "block" }}
          >
            {role}
          </Typography>
          <Box
            component="pre"
            sx={{
              m: 0,
              p: 1,
              bgcolor: "background.paper",
              border: 1,
              borderColor: "divider",
              borderRadius: 0.5,
              fontSize: 12,
              fontFamily: "'JetBrains Mono', 'Fira Code', monospace",
              overflow: "auto",
              maxHeight: 400,
              whiteSpace: "pre-wrap",
              wordBreak: "break-word",
            }}
          >
            {String(text)}
          </Box>
        </Box>
      ))}
    </Box>
  );
}

function ExpandedRow({ row }: { row: LogTurnRow }) {
  const t = useT();
  return (
    <Box sx={{ px: 2, py: 1.5, bgcolor: "grey.50" }}>
      <Stack direction="row" spacing={3} sx={{ flexWrap: "wrap" }}>
        <Box sx={{ flex: 1, minWidth: 320 }}>
          <DetailBlock label={t("agentLog.detailPromptSentToLlm")}>
            {row.prompt_messages ? (
              <PromptBlock messages={row.prompt_messages} />
            ) : (
              <Typography variant="caption" color="text.disabled">
                {t("agentLog.detailNotCaptured")}
              </Typography>
            )}
          </DetailBlock>
          <DetailBlock label={t("agentLog.detailRawLlmResponse")}>
            {row.llm_raw_response ? (
              <CodeBlock value={row.llm_raw_response} />
            ) : (
              <Typography variant="caption" color="text.disabled">
                {t("agentLog.detailNotCaptured")}
              </Typography>
            )}
          </DetailBlock>
          {row.thought_summary && (
            <DetailBlock label={t("agentLog.detailThoughtProcess")}>
              <CodeBlock value={row.thought_summary} />
            </DetailBlock>
          )}
          {row.llm_plan && (
            <DetailBlock label={t("agentLog.detailLlmPlan")}>
              <JsonBlock value={row.llm_plan} />
            </DetailBlock>
          )}
          {row.semantic_query && (
            <DetailBlock label={t("agentLog.detailSemanticQuery")}>
              <JsonBlock value={row.semantic_query} />
            </DetailBlock>
          )}
          {row.routed_sql && (
            <DetailBlock label={t("agentLog.detailPhysicalSql")}>
              <CodeBlock value={row.routed_sql} />
            </DetailBlock>
          )}
          {row.answer_text && (
            <DetailBlock label={t("agentLog.detailAnswer")}>
              <Typography variant="body2">{row.answer_text}</Typography>
            </DetailBlock>
          )}
        </Box>
        <Box sx={{ flex: 1, minWidth: 320 }}>
          {row.citations && row.citations.length > 0 && (
            <DetailBlock label={t("agentLog.detailCitations")}>
              <JsonBlock value={row.citations} />
            </DetailBlock>
          )}
          {row.judge_reasoning && (
            <DetailBlock label={t("agentLog.detailJudgeReasoning")}>
              <CodeBlock value={row.judge_reasoning} />
            </DetailBlock>
          )}
          {row.judge_metrics && (
            <DetailBlock label={t("agentLog.detailJudgeMetrics")}>
              <JsonBlock value={row.judge_metrics} />
            </DetailBlock>
          )}
          {row.guardrail_actions && row.guardrail_actions.length > 0 && (
            <DetailBlock label={t("agentLog.detailGuardrailActions")}>
              <JsonBlock value={row.guardrail_actions} />
            </DetailBlock>
          )}
          {row.user_feedback && (
            <DetailBlock label={t("agentLog.detailUserFeedback")}>
              <JsonBlock value={row.user_feedback} />
            </DetailBlock>
          )}
        </Box>
      </Stack>
    </Box>
  );
}

function KpiCard({ label, value, sub }: { label: string; value: React.ReactNode; sub?: string }) {
  return (
    <Box
      sx={{
        flex: 1,
        minWidth: 100,
        px: 1.5,
        py: 1,
        borderRadius: 1,
        bgcolor: "background.paper",
        border: 1,
        borderColor: "divider",
      }}
    >
      <Typography variant="caption" color="text.secondary" sx={{ fontWeight: 600 }}>
        {label}
      </Typography>
      <Typography variant="h6" fontWeight={700} sx={{ lineHeight: 1.2, mt: 0.25 }}>
        {value}
      </Typography>
      {sub && (
        <Typography variant="caption" color="text.disabled">
          {sub}
        </Typography>
      )}
    </Box>
  );
}

function KpiStrip({ projectId }: { projectId: string }) {
  const t = useT();
  const kpiQuery = useQuery<AgentKpis>({
    queryKey: ["agent-kpis", projectId],
    queryFn: () => agentApi.getKpis(projectId, 30),
    enabled: Boolean(projectId),
  });

  if (kpiQuery.isLoading) {
    return (
      <Card variant="outlined">
        <CardContent sx={{ py: 1.5, px: 2, "&:last-child": { pb: 1.5 } }}>
          <CircularProgress size={16} />
        </CardContent>
      </Card>
    );
  }

  if (!kpiQuery.data) return null;

  const k = kpiQuery.data;
  const feedbackTotal = k.feedback_up + k.feedback_down;
  const satisfaction = feedbackTotal > 0 ? Math.round((k.feedback_up / feedbackTotal) * 100) : null;

  return (
    <Card variant="outlined">
      <CardContent sx={{ py: 1, px: 1.5, "&:last-child": { pb: 1 } }}>
        <Stack direction="row" spacing={1} sx={{ flexWrap: "wrap" }} useFlexGap>
          <KpiCard label={t("agentLog.kpiTotalTurns")} value={k.total_turns} sub={t("agentLog.kpiWindowDays", { days: String(k.window_days) })} />
          <KpiCard label={t("agentLog.kpiOkRefused")} value={`${k.ok_turns} / ${k.refused_turns}`} />
          <KpiCard
            label={t("agentLog.kpiFeedback")}
            value={
              <Stack direction="row" spacing={1} alignItems="center">
                <Stack direction="row" spacing={0.25} alignItems="center">
                  <ThumbUpIcon sx={{ fontSize: 14, color: "success.main" }} />
                  <span>{k.feedback_up}</span>
                </Stack>
                <Stack direction="row" spacing={0.25} alignItems="center">
                  <ThumbDownIcon sx={{ fontSize: 14, color: "error.main" }} />
                  <span>{k.feedback_down}</span>
                </Stack>
              </Stack>
            }
            sub={satisfaction !== null ? t("agentLog.kpiSatisfaction", { pct: String(satisfaction) }) : t("agentLog.kpiNoVotes")}
          />
          <KpiCard label={t("agentLog.kpiCitations")} value={`${(k.citations_rate * 100).toFixed(0)}%`} sub={t("agentLog.kpiOfOkTurns")} />
          <KpiCard label={t("agentLog.kpiAggRouted")} value={`${(k.aggregate_route_rate * 100).toFixed(0)}%`} sub={t("agentLog.kpiOfOkTurns")} />
          <KpiCard label={t("agentLog.kpiJudgeBlocked")} value={`${(k.judge_block_rate * 100).toFixed(0)}%`} />
          {k.dlq_depth > 0 && <KpiCard label={t("agentLog.kpiDlqDepth")} value={k.dlq_depth} />}
        </Stack>
      </CardContent>
    </Card>
  );
}

export default function AgentLog() {
  const t = useT();
  const { tenantId, projectId } = useParams<{
    tenantId: string;
    projectId: string;
  }>();
  const navigate = useNavigate();
  const project = useProject(projectId!);

  const [filters, setFilters] = useState<LogFilters>({
    page: 1,
    page_size: 25,
  });
  const [draftDateFrom, setDraftDateFrom] = useState("");
  const [draftDateTo, setDraftDateTo] = useState("");
  const [draftUser, setDraftUser] = useState("");
  const [draftQ, setDraftQ] = useState("");
  const [expandedId, setExpandedId] = useState<string | null>(null);

  const logQuery = useQuery({
    queryKey: ["agent-log", projectId, filters],
    queryFn: () => agentApi.getLog(projectId!, filters),
    enabled: Boolean(projectId),
    placeholderData: (prev) => prev,
  });

  const handleSearch = useCallback(() => {
    setExpandedId(null);
    setFilters({
      date_from: draftDateFrom || undefined,
      date_to: draftDateTo || undefined,
      caller_ref: draftUser || undefined,
      q: draftQ || undefined,
      page: 1,
      page_size: 25,
    });
  }, [draftDateFrom, draftDateTo, draftUser, draftQ]);

  const handleKeyDown = useCallback(
    (e: React.KeyboardEvent) => {
      if (e.key === "Enter") handleSearch();
    },
    [handleSearch],
  );

  const totalPages = useMemo(() => {
    if (!logQuery.data) return 0;
    return Math.ceil(logQuery.data.total / logQuery.data.page_size);
  }, [logQuery.data]);

  const projectName =
    project.data?.display_name ?? project.data?.slug ?? "";

  return (
    <Box sx={{ display: "flex", flexDirection: "column", height: "calc(100vh - 64px)" }}>
      {/* Header strip */}
      <Box
        sx={{
          display: "flex",
          alignItems: "center",
          px: 1.5,
          py: 0.5,
          borderBottom: 1,
          borderColor: "divider",
          gap: 1,
        }}
      >
        <Tooltip title={t("agentLog.backToProject")}>
          <IconButton
            size="small"
            onClick={() =>
              navigate(`/tenants/${tenantId}/projects/${projectId}`)
            }
          >
            <ArrowBackIcon fontSize="small" />
          </IconButton>
        </Tooltip>
        <Typography variant="h6" fontWeight={700} noWrap>
          {projectName}
          <Typography
            component="span"
            variant="h6"
            fontWeight={400}
            color="text.secondary"
            sx={{ mx: 0.75 }}
          >
            {t("common.pipeSeparator")}
          </Typography>
          {t("agentLog.title")}
        </Typography>
        <HelpIconButton href="/help/agent/agent-log-screen.html" />
      </Box>

      <Box
        sx={{
          p: 2,
          flex: 1,
          display: "block",
          "& > * + *": { mt: 2 },
          overflow: "auto",
        }}
      >

      {/* Filter bar */}
      <Card variant="outlined">
        <CardContent sx={{ py: 1.5, px: 2, "&:last-child": { pb: 1.5 } }}>
          <Stack
            direction="row"
            spacing={1.5}
            alignItems="center"
            flexWrap="wrap"
            useFlexGap
          >
            <TextField
              label={t("agentLog.filterFromLabel")}
              type="date"
              size="small"
              InputLabelProps={{ shrink: true }}
              inputProps={{ "aria-label": t("agentLog.filterFromLabel") }}
              value={draftDateFrom}
              onChange={(e) => setDraftDateFrom(e.target.value)}
              onKeyDown={handleKeyDown}
              sx={{ width: 150 }}
            />
            <TextField
              label={t("agentLog.filterToLabel")}
              type="date"
              size="small"
              InputLabelProps={{ shrink: true }}
              inputProps={{ "aria-label": t("agentLog.filterToLabel") }}
              value={draftDateTo}
              onChange={(e) => setDraftDateTo(e.target.value)}
              onKeyDown={handleKeyDown}
              sx={{ width: 150 }}
            />
            <TextField
              label={t("agentLog.filterUserLabel")}
              size="small"
              value={draftUser}
              onChange={(e) => setDraftUser(e.target.value)}
              onKeyDown={handleKeyDown}
              sx={{ width: 180 }}
            />
            <TextField
              label={t("agentLog.filterQuestionLabel")}
              size="small"
              value={draftQ}
              onChange={(e) => setDraftQ(e.target.value)}
              onKeyDown={handleKeyDown}
              sx={{ flex: 1, minWidth: 180 }}
            />
            <Button
              variant="contained"
              size="small"
              startIcon={<SearchIcon sx={{ fontSize: 16 }} />}
              onClick={handleSearch}
              sx={{ textTransform: "none" }}
            >
              {t("agentLog.searchButton")}
            </Button>
            {logQuery.data != null && (
              <Typography variant="caption" color="text.secondary">
                {logQuery.data.total !== 1
                  ? t("agentLog.matchingRecordsPlural", { count: String(logQuery.data.total) })
                  : t("agentLog.matchingRecords", { count: String(logQuery.data.total) })}
              </Typography>
            )}
          </Stack>
        </CardContent>
      </Card>

      {/* KPI strip */}
      <KpiStrip projectId={projectId!} />

      {/* Loading */}
      {logQuery.isLoading && (
        <Box sx={{ p: 4, display: "flex", justifyContent: "center" }}>
          <CircularProgress size={22} />
        </Box>
      )}

      {/* Error */}
      {logQuery.isError && (
        <Alert severity="error">
          {t("agentLog.loadError", { error: String((logQuery.error as Error)?.message ?? "unknown error") })}
        </Alert>
      )}

      {/* Empty state */}
      {!logQuery.isLoading &&
        !logQuery.isError &&
        logQuery.data &&
        logQuery.data.items.length === 0 && (
          <Alert severity="info">{t("agentLog.noMatchingRecords")}</Alert>
        )}

      {/* Results table */}
      {logQuery.data && logQuery.data.items.length > 0 && (
        <Card variant="outlined">
          <CardContent sx={{ p: 0, "&:last-child": { pb: 0 } }}>
            <TableContainer>
              <Table size="small" stickyHeader>
                <TableHead>
                  <TableRow>
                    <TableCell sx={{ width: 28, px: 0.5 }} />
                    <TableCell sx={{ fontWeight: 600 }}>{t("agentLog.colTimestamp")}</TableCell>
                    <TableCell sx={{ fontWeight: 600 }}>{t("agentLog.colUser")}</TableCell>
                    <TableCell sx={{ fontWeight: 600 }}>{t("agentLog.colQuestion")}</TableCell>
                    <TableCell sx={{ fontWeight: 600 }}>{t("agentLog.colStatus")}</TableCell>
                    <TableCell sx={{ fontWeight: 600 }}>{t("agentLog.colVerdict")}</TableCell>
                    <TableCell sx={{ fontWeight: 600 }}>{t("agentLog.colRoute")}</TableCell>
                    <TableCell sx={{ fontWeight: 600 }} align="center">
                      {t("agentLog.colFeedback")}
                    </TableCell>
                    <TableCell sx={{ fontWeight: 600 }} align="right">
                      {t("agentLog.colRows")}
                    </TableCell>
                    <TableCell sx={{ fontWeight: 600 }} align="right">
                      {t("agentLog.colTokens")}
                    </TableCell>
                    <TableCell sx={{ fontWeight: 600 }} align="right">
                      {t("agentLog.colLatency")}
                    </TableCell>
                  </TableRow>
                </TableHead>
                <TableBody>
                  {logQuery.data.items.map((row) => {
                    const isExpanded = expandedId === row.turn_id;
                    return (
                      <>
                        <TableRow
                          key={row.turn_id}
                          hover
                          sx={{
                            cursor: "pointer",
                            "& > *": {
                              borderBottom: isExpanded ? "none" : undefined,
                            },
                          }}
                          onClick={() =>
                            setExpandedId(isExpanded ? null : row.turn_id)
                          }
                        >
                          <TableCell sx={{ px: 0.5, py: 0.5 }}>
                            <IconButton size="small">
                              <ExpandMoreIcon
                                sx={{
                                  fontSize: 18,
                                  transition: "transform 0.2s",
                                  transform: isExpanded
                                    ? "rotate(180deg)"
                                    : "rotate(0deg)",
                                }}
                              />
                            </IconButton>
                          </TableCell>
                          <TableCell
                            sx={{ fontSize: 13, py: 0.5, whiteSpace: "nowrap" }}
                          >
                            {new Date(row.created_at).toLocaleString()}
                          </TableCell>
                          <TableCell sx={{ fontSize: 13, py: 0.5 }}>
                            {row.caller_ref}
                          </TableCell>
                          <TableCell
                            sx={{
                              fontSize: 13,
                              py: 0.5,
                              maxWidth: 280,
                              overflow: "hidden",
                              textOverflow: "ellipsis",
                              whiteSpace: "nowrap",
                            }}
                          >
                            {row.user_message}
                          </TableCell>
                          <TableCell sx={{ py: 0.5 }}>
                            <StatusBadge
                              label={row.status}
                              severity={statusSeverity(row.status)}
                            />
                          </TableCell>
                          <TableCell sx={{ py: 0.5 }}>
                            {row.judge_verdict ? (
                              <StatusBadge
                                label={row.judge_verdict}
                                severity={verdictSeverity(row.judge_verdict)}
                              />
                            ) : (
                              <Typography
                                variant="caption"
                                color="text.disabled"
                              >
                                {t("common.na")}
                              </Typography>
                            )}
                          </TableCell>
                          <TableCell sx={{ fontSize: 13, py: 0.5 }}>
                            {row.route ?? t("common.na")}
                          </TableCell>
                          <TableCell align="center" sx={{ py: 0.5 }}>
                            {row.user_feedback?.vote === "up" ? (
                              <ThumbUpIcon sx={{ fontSize: 14, color: "success.main" }} />
                            ) : row.user_feedback?.vote === "down" ? (
                              <ThumbDownIcon sx={{ fontSize: 14, color: "error.main" }} />
                            ) : (
                              <Typography variant="caption" color="text.disabled">{t("common.na")}</Typography>
                            )}
                          </TableCell>
                          <TableCell
                            align="right"
                            sx={{ fontSize: 13, py: 0.5 }}
                          >
                            {row.query_result_rows ?? t("common.na")}
                          </TableCell>
                          <TableCell
                            align="right"
                            sx={{
                              fontSize: 13,
                              py: 0.5,
                              fontFamily: "monospace",
                              whiteSpace: "nowrap",
                            }}
                          >
                            {formatTokens(row.usage_input_tokens)}{t("common.tokenSeparator")}
                            {formatTokens(row.usage_output_tokens)}
                          </TableCell>
                          <TableCell
                            align="right"
                            sx={{ fontSize: 13, py: 0.5 }}
                          >
                            {formatLatency(row.latency_ms)}
                          </TableCell>
                        </TableRow>
                        <TableRow key={`${row.turn_id}-detail`}>
                          <TableCell
                            colSpan={11}
                            sx={{
                              p: 0,
                              border: isExpanded ? undefined : "none",
                            }}
                          >
                            <Collapse in={isExpanded} unmountOnExit>
                              <ExpandedRow row={row} />
                            </Collapse>
                          </TableCell>
                        </TableRow>
                      </>
                    );
                  })}
                </TableBody>
              </Table>
            </TableContainer>
          </CardContent>
        </Card>
      )}

      {/* Pagination */}
      {totalPages > 1 && (
        <Stack
          direction="row"
          justifyContent="center"
          alignItems="center"
          spacing={1}
        >
          <IconButton
            size="small"
            disabled={(filters.page ?? 1) <= 1}
            onClick={() =>
              setFilters((f) => ({ ...f, page: (f.page ?? 1) - 1 }))
            }
          >
            <ChevronLeftIcon sx={{ fontSize: 20 }} />
          </IconButton>
          <Typography variant="caption" color="text.secondary">
            {t("agentLog.pageOf", { page: String(logQuery.data?.page ?? 1), total: String(totalPages) })}
          </Typography>
          <IconButton
            size="small"
            disabled={(filters.page ?? 1) >= totalPages}
            onClick={() =>
              setFilters((f) => ({ ...f, page: (f.page ?? 1) + 1 }))
            }
          >
            <ChevronRightIcon sx={{ fontSize: 20 }} />
          </IconButton>
        </Stack>
      )}
      </Box>
    </Box>
  );
}
