/**
 * QueryPanel — paste a SQL query, pick a dialect, then validate / dry-run /
 * execute it against the model. Shows the routing pipeline as a diagram and
 * paginated results when executed.
 */
import { useEffect, useMemo, useRef, useState } from "react";
import { useParams } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useT } from "../../i18n";
import {
  Accordion,
  AccordionDetails,
  AccordionSummary,
  Alert,
  AlertTitle,
  Box,
  Button,
  Chip,
  Dialog,
  DialogActions,
  DialogContent,
  DialogTitle,
  FormControl,
  IconButton,
  MenuItem,
  Paper,
  Select,
  Stack,
  Table,
  TableBody,
  TableCell,
  TableContainer,
  TableHead,
  TablePagination,
  TableRow,
  TextField,
  Tooltip,
  Typography,
} from "@mui/material";
import SaveIcon from "@mui/icons-material/Save";
import ContentCopyIcon from "@mui/icons-material/ContentCopy";
import ExpandMoreIcon from "@mui/icons-material/ExpandMore";
import { queryRouterApiClient, savedQueriesApi } from "../../api/client";
import { useModel, useMeasures } from "../../api/hooks";
import { formatMeasureValue } from "../../api/measureFormat";
import type {
  ExecuteResponse,
  ExplainResponse,
  MeasureFormatToken,
  PipelineTrace,
  QueryRouterFieldCompatibilityFeedback,
  ValidateResponse,
} from "../../api/types";
import PersonaPicker from "../Persona/PersonaPicker";
import PipelineDiagram from "../Builder/PipelineDiagram";
import UnsavedDeployWarning from "../Builder/UnsavedDeployWarning";
import SqlQueryEditor, { formatSql } from "../Sql/SqlQueryEditor";
import { TIME_VARIANT_NAMES } from "../../constants/timeVariants";
import CalendarBindingHint from "../CalendarBindingHint";
import { ui } from "../../theme/tokens";
import { rowSecurityDeniedAll } from "../../utils/rowSecurity";
import { useBuilderStore } from "../../store/builderStore";
import { syncPublishedSessionVars } from "./querySessionVars";
import {
  extractQueryFieldCompatibilityFromError,
  fieldCompatibilityValidationContextKey,
  fieldCompatibilityCompatibleDimensionNames,
  fieldCompatibilityMessages,
  hasBlockingFieldCompatibility,
  hasNotAnalyzedFieldCompatibility,
  shouldRenderFieldCompatibility,
} from "./queryFieldCompatibility";

const DIALECT_KEYS: Record<string, string> = {
  postgresql: "conn.type.postgresql",
  bigquery: "conn.type.bigquery",
  hadoop_spark: "conn.type.hadoopSpark",
  redshift: "conn.type.redshift",
  snowflake: "conn.type.snowflake",
  sqlserver: "conn.type.sqlserver",
};

// Only used as a fallback; QueryPanel builds the translated list at render time.
const DIALECT_VALUES = ["postgresql", "bigquery", "hadoop_spark", "redshift", "snowflake", "sqlserver"] as const;

const SQL_KEYWORDS = new Set(
  [
    "SELECT", "FROM", "WHERE", "GROUP", "BY", "ORDER", "HAVING", "LIMIT",
    "OFFSET", "JOIN", "INNER", "LEFT", "RIGHT", "FULL", "OUTER", "ON",
    "AS", "AND", "OR", "NOT", "IN", "IS", "NULL", "LIKE", "BETWEEN",
    "DISTINCT", "UNION", "ALL", "CASE", "WHEN", "THEN", "ELSE", "END",
    "WITH", "AS", "EXISTS", "ASC", "DESC", "INSERT", "INTO", "VALUES",
    "UPDATE", "SET", "DELETE", "CREATE", "TABLE", "VIEW", "INDEX", "CAST",
    "OVER", "PARTITION", "WINDOW", "ROWS", "RANGE", "INTERVAL", "EXTRACT",
  ],
);

function highlightSql(sql: string): JSX.Element[] {
  const tokens: JSX.Element[] = [];
  const re = /('([^'\\]|\\.)*')|("([^"\\]|\\.)*")|(--[^\n]*)|(\b\d+(\.\d+)?\b)|([A-Za-z_][A-Za-z0-9_]*)|(\s+)|([^A-Za-z0-9_\s])/g;
  let match: RegExpExecArray | null;
  let key = 0;
  while ((match = re.exec(sql)) !== null) {
    const value = match[0];
    if (match[1] || match[3]) {
      tokens.push(<span key={key++} style={{ color: "#c62828" }}>{value}</span>);
    } else if (match[5]) {
      tokens.push(<span key={key++} style={{ color: "#9e9e9e", fontStyle: "italic" }}>{value}</span>);
    } else if (match[6]) {
      tokens.push(<span key={key++} style={{ color: "#1565c0" }}>{value}</span>);
    } else if (match[8]) {
      const upper = value.toUpperCase();
      if (SQL_KEYWORDS.has(upper)) {
        tokens.push(
          <span key={key++} style={{ color: "#6a1b9a", fontWeight: 700 }}>
            {upper}
          </span>,
        );
      } else {
        tokens.push(<span key={key++} style={{ color: "#212121" }}>{value}</span>);
      }
    } else {
      tokens.push(<span key={key++}>{value}</span>);
    }
  }
  return tokens;
}

function HighlightedSql({ sql }: { sql: string }) {
  return (
    <Paper
      variant="outlined"
      sx={{
        p: 1,
        bgcolor: ui.mutedBg,
        fontFamily: "JetBrains Mono, monospace",
        fontSize: 12,
        whiteSpace: "pre-wrap",
        wordBreak: "break-word",
        maxHeight: 240,
        overflow: "auto",
      }}
    >
      {highlightSql(sql)}
    </Paper>
  );
}

export default function QueryPanel() {
  const { projectId, modelId } = useParams<{ projectId: string; modelId: string }>();
  const t = useT();
  const model = useModel(projectId ?? "", modelId ?? "");
  const measures = useMeasures(projectId ?? "", modelId ?? "");
  const formatByColumn = useMemo(() => {
    const map = new Map<string, MeasureFormatToken>();
    for (const m of measures.data ?? []) {
      if (m.format) {
        map.set(m.name, m.format as MeasureFormatToken);
        if (m.display_name && m.display_name !== m.name) {
          map.set(m.display_name, m.format as MeasureFormatToken);
        }
      }
    }
    return map;
  }, [measures.data]);

  const pendingSql = useBuilderStore((s) => s.pendingSql);
  const setPendingSql = useBuilderStore((s) => s.setPendingSql);
  const consumedPendingSql = useRef<string | null>(null);
  const [sql, setSql] = useState<string>(pendingSql ?? "SELECT 1");
  const [dialect, setDialect] = useState<string>("postgresql");
  const tDialects = useMemo(
    () => DIALECT_VALUES.map((v) => ({ value: v, label: t(DIALECT_KEYS[v]) })),
    [t],
  );
  const [forceRoute, setForceRoute] = useState<"" | "source" | "aggregate" | "pocket">("");
  const [personaId, setPersonaId] = useState<string | null>(null);
  // Bug-9224: keys are copied verbatim from the deployed catalogue.  The SPA
  // must not rebuild `app.<parameter>` from the authored name because the
  // gateway publishes a lower-cased session_var_key.
  const [sessionVars, setSessionVars] = useState<Record<string, string>>({});
  const namedObjects = useQuery({
    queryKey: ["deployedNamedObjects", modelId],
    queryFn: () => queryRouterApiClient.namedObjects(modelId!),
    enabled: Boolean(modelId),
  });
  const [page, setPage] = useState(0);
  const [pageSize, setPageSize] = useState(25);

  useEffect(() => {
    const parameters = namedObjects.data?.parameters ?? [];
    setSessionVars((previous) => syncPublishedSessionVars(previous, parameters));
  }, [namedObjects.data]);

  useEffect(() => {
    if (!pendingSql) {
      consumedPendingSql.current = null;
      return;
    }
    if (consumedPendingSql.current === pendingSql) return;
    consumedPendingSql.current = pendingSql;
    setSql(pendingSql);
    setPendingSql(null);
    setFieldCompatibilityBlockedContextKey(null);
    void executeQuery(pendingSql);
  }, [pendingSql, setPendingSql]);

  const [validating, setValidating] = useState(false);
  const [exploring, setExploring] = useState(false);
  const [executing, setExecuting] = useState(false);

  const [validateResult, setValidateResult] = useState<ValidateResponse | null>(null);
  const [explainResult, setExplainResult] = useState<ExplainResponse | null>(null);
  const [executeResult, setExecuteResult] = useState<ExecuteResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [errorFieldCompatibility, setErrorFieldCompatibility] =
    useState<QueryRouterFieldCompatibilityFeedback | null>(null);
  const [fieldCompatibilityBlockedContextKey, setFieldCompatibilityBlockedContextKey] =
    useState<string | null>(null);
  const [stickyTrace, setStickyTrace] = useState<PipelineTrace | null>(null);

  const trace: PipelineTrace | null =
    executeResult?.trace ?? explainResult?.trace ?? stickyTrace;
  const showingStickyTrace = Boolean(stickyTrace && !executeResult && !explainResult);
  const traceRequestInFlight = showingStickyTrace && (validating || exploring || executing);

  const qc = useQueryClient();
  const [saveOpen, setSaveOpen] = useState(false);
  const [saveName, setSaveName] = useState("");
  const [saveDesc, setSaveDesc] = useState("");

  const saveMutation = useMutation({
    mutationFn: (data: { name: string; description?: string; query_text: string }) =>
      savedQueriesApi.create(projectId!, modelId!, data),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["savedQueries", projectId, modelId] });
      setSaveOpen(false);
      setSaveName("");
      setSaveDesc("");
    },
  });

  const queryCompatibilityContextKey = useMemo(
    () =>
      fieldCompatibilityValidationContextKey({
        sql,
        dialect,
        personaId: personaId ?? null,
        forceRoute: forceRoute || null,
      }),
    [sql, dialect, personaId, forceRoute],
  );
  const executeBlockedByValidatedCompatibility =
    fieldCompatibilityBlockedContextKey === queryCompatibilityContextKey;

  function clearOutputs() {
    // Preserve the last trace so the pipeline diagram doesn't flicker or
    // disappear while a new request is in flight, including validate requests
    // which do not return a pipeline trace.
    const prevTrace = executeResult?.trace ?? explainResult?.trace ?? stickyTrace;
    if (prevTrace) setStickyTrace(prevTrace);
    setError(null);
    setErrorFieldCompatibility(null);
    setValidateResult(null);
    setExplainResult(null);
    setExecuteResult(null);
    setPage(0);
  }

  // F-003-12: map the backend's typed error_type values to translated,
  // user-facing strings. The raw backend message (English, SQL-client parity)
  // is kept as secondary detail after the translated lead line.
  const QUERY_ERROR_TYPE_KEYS: Record<string, string> = {
    no_aggregate_match: "errors.queryType.noAggregateMatch",
    feature_not_supported: "errors.queryType.featureNotSupported",
    cross_model_not_resolved: "errors.queryType.crossModelNotResolved",
    security_audit_failure: "errors.queryType.securityAuditFailure",
  };

  function extractError(err: unknown): string {
    const detail =
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      (err as { response?: { data?: { detail?: any } } })?.response?.data?.detail;
    if (detail) {
      if (typeof detail === "string") return detail;
      if (typeof detail === "object" && detail !== null) {
        const rawMessage =
          "message" in detail && typeof detail.message === "string"
            ? detail.message
            : null;
        const errorType =
          "error_type" in detail && typeof detail.error_type === "string"
            ? detail.error_type
            : null;
        const i18nKey = errorType ? QUERY_ERROR_TYPE_KEYS[errorType] : undefined;
        if (i18nKey) {
          const lead = t(i18nKey);
          return rawMessage ? `${lead}\n${rawMessage}` : lead;
        }
        if (rawMessage) return rawMessage;
        return JSON.stringify(detail);
      }
    }
    if (err instanceof Error) return err.message;
    return t("errors.requestFailed");
  }

  function captureError(err: unknown) {
    setErrorFieldCompatibility(extractQueryFieldCompatibilityFromError(err));
    setError(extractError(err));
  }

  async function handleValidate() {
    if (!modelId) return;
    clearOutputs();
    setValidating(true);
    const validatedContextKey = queryCompatibilityContextKey;
    try {
      const result = await queryRouterApiClient.validate(
        {
          model_id: modelId,
          raw_query: sql,
          dialect,
          ...(Object.keys(sessionVars).length > 0 ? { session_vars: sessionVars } : {}),
        },
        personaId,
      );
      setValidateResult(result);
      setFieldCompatibilityBlockedContextKey(
        hasBlockingFieldCompatibility(result.field_compatibility)
          ? validatedContextKey
          : null,
      );
    } catch (err) {
      captureError(err);
    } finally {
      setValidating(false);
    }
  }

  async function handleExplain() {
    if (!modelId) return;
    clearOutputs();
    setExploring(true);
    try {
      const result = await queryRouterApiClient.explain(
        {
          model_id: modelId,
          raw_query: sql,
          dialect,
          ...(forceRoute ? { force_route: forceRoute } : {}),
          ...(Object.keys(sessionVars).length > 0 ? { session_vars: sessionVars } : {}),
        },
        personaId,
      );
      setStickyTrace(null);
      setExplainResult(result);
    } catch (err) {
      setStickyTrace(null);
      captureError(err);
    } finally {
      setExploring(false);
    }
  }

  async function executeQuery(queryText: string) {
    if (!modelId) return;
    if (queryText === sql && executeBlockedByValidatedCompatibility) return;
    clearOutputs();
    setExecuting(true);
    try {
      const result = await queryRouterApiClient.execute(
        {
          model_id: modelId,
          raw_query: queryText,
          dialect,
          ...(forceRoute ? { force_route: forceRoute } : {}),
          ...(Object.keys(sessionVars).length > 0 ? { session_vars: sessionVars } : {}),
        },
        personaId,
      );
      setStickyTrace(null);
      setExecuteResult(result);
    } catch (err) {
      setStickyTrace(null);
      captureError(err);
    } finally {
      setExecuting(false);
    }
  }

  async function handleExecute() {
    await executeQuery(sql);
  }

  function handleCopy(text: string) {
    void navigator.clipboard?.writeText(text);
  }

  const rewrittenSql = executeResult?.trace
    ? (executeResult.trace.steps.find((s) => s.stage === "rewriter")?.data as
        | { rewritten_sql?: string }
        | undefined
      )?.rewritten_sql ?? ""
    : explainResult?.rewritten_query ?? "";

  const allRows = executeResult?.rows ?? [];
  // Bug-8453: classify via the shared contract, never via allRows.length.
  const executeRowSecurityDenied = rowSecurityDeniedAll(executeResult);
  const pagedRows = useMemo(
    () => allRows.slice(page * pageSize, page * pageSize + pageSize),
    [allRows, page, pageSize],
  );

  const variantsByBase = useMemo(() => {
    const out = new Map<string, Set<string>>();
    for (const m of measures.data ?? []) {
      if (m.variant_kind && m.variant_of_measure_id) {
        const set = out.get(m.variant_of_measure_id) ?? new Set<string>();
        set.add(m.variant_kind);
        out.set(m.variant_of_measure_id, set);
      }
    }
    return out;
  }, [measures.data]);

  const variantBaseMeasures = useMemo(
    () =>
      (measures.data ?? []).filter(
        (m) => !m.variant_kind && variantsByBase.has(m.id),
      ),
    [measures.data, variantsByBase],
  );

  return (
    <Stack spacing={1.5}>
      <UnsavedDeployWarning />
      <CalendarBindingHint context="query" />
      {variantBaseMeasures.length > 0 && (
        <Box>
          {variantBaseMeasures.map((m) => {
            const kinds = TIME_VARIANT_NAMES.filter((v) =>
              variantsByBase.get(m.id)?.has(v),
            );
            return (
              <Accordion
                key={m.id}
                disableGutters
                square
                variant="outlined"
                sx={{ "&:before": { display: "none" } }}
              >
                <AccordionSummary
                  expandIcon={<ExpandMoreIcon fontSize="small" />}
                  sx={{ minHeight: 36, "& .MuiAccordionSummary-content": { my: 0.5 } }}
                >
                  <Typography variant="caption" sx={{ fontWeight: 600 }}>
                    {t("query.timeVariantsOf")} {m.display_name || m.name}
                  </Typography>
                  <Typography variant="caption" color="text.secondary" sx={{ ml: 1 }}>
                    ({kinds.length})
                  </Typography>
                </AccordionSummary>
                <AccordionDetails sx={{ pt: 0, pb: 1 }}>
                  <Typography
                    variant="caption"
                    color="text.secondary"
                    sx={{ display: "block", mb: 0.5 }}
                  >
                    {t("query.clickVariantToCopy")}
                  </Typography>
                  <Box sx={{ display: "flex", flexWrap: "wrap", gap: 0.5 }}>
                    {kinds.map((v) => {
                      const fullName = `${m.name}_${v}`;
                      return (
                        <Chip
                          key={v}
                          size="small"
                          label={v}
                          variant="outlined"
                          onClick={() => navigator.clipboard?.writeText(fullName)}
                          sx={{
                            fontFamily: "monospace",
                            fontSize: "0.7rem",
                            cursor: "pointer",
                          }}
                          title={t("query.copyMeasureName", { fullName })}
                        />
                      );
                    })}
                  </Box>
                </AccordionDetails>
              </Accordion>
            );
          })}
        </Box>
      )}
      {model.data && (
        <Typography variant="caption" color="text.secondary" sx={{ fontFamily: "monospace" }}>
          {t("query.fromClause")}{" "}
          <Typography
            component="span"
            variant="caption"
            sx={{ fontWeight: 700, fontFamily: "monospace", cursor: "pointer" }}
            onClick={() => navigator.clipboard?.writeText(`"${model.data!.slug}"`)}
            title={t("query.clickToCopy")}
          >
            &quot;{model.data.slug}&quot;
          </Typography>
        </Typography>
      )}
      <SqlQueryEditor
        sql={sql}
        onSqlChange={setSql}
        dialect={dialect}
        onDialectChange={setDialect}
        dialects={tDialects}
        onValidate={handleValidate}
        onDryRun={handleExplain}
        onExecute={handleExecute}
        validating={validating}
        dryRunning={exploring}
        executing={executing}
        executeDisabled={executeBlockedByValidatedCompatibility}
        executeDisabledReason={t("query.fieldCompatibility.executeBlocked")}
        placeholder={t("query.placeholder", {
          slug: model.data?.slug ?? "model_slug",
        })}
      />
      <Box display="flex" alignItems="center" gap={1} flexWrap="wrap">
        <PersonaPicker
          projectId={projectId ?? ""}
          modelId={modelId ?? ""}
          value={personaId}
          onChange={setPersonaId}
        />
        <Tooltip title={t("query.saveQuery")}>
          <Button
            size="small"
            variant="outlined"
            startIcon={<SaveIcon fontSize="small" />}
            onClick={() => setSaveOpen(true)}
            disabled={!sql.trim() || sql.trim() === "SELECT 1"}
          >
            {t("common.save")}
          </Button>
        </Tooltip>
        <FormControl size="small" sx={{ minWidth: 140 }}>
          <Select
            value={forceRoute}
            onChange={(e) => setForceRoute(e.target.value as "" | "source" | "aggregate" | "pocket")}
            displayEmpty
            sx={{ fontSize: 12 }}
          >
            <MenuItem value="">
              <Typography variant="caption">{t("query.autoRoute")}</Typography>
            </MenuItem>
            <MenuItem value="source">
              <Typography variant="caption">{t("query.forceSource")}</Typography>
            </MenuItem>
            <MenuItem value="aggregate">
              <Typography variant="caption">{t("query.forceAggregate")}</Typography>
            </MenuItem>
            <MenuItem value="pocket">
              <Typography variant="caption">{t("query.forcePocket")}</Typography>
            </MenuItem>
          </Select>
        </FormControl>
      </Box>

      {(namedObjects.data?.parameters.length ?? 0) > 0 && (
        <Paper variant="outlined" sx={{ p: 1 }}>
          <Typography variant="caption" fontWeight={700} display="block" sx={{ mb: 0.75 }}>
            {t("query.deployedParameters")}
          </Typography>
          <Stack direction="row" spacing={1} useFlexGap flexWrap="wrap">
            {namedObjects.data?.parameters.map((parameter) =>
              parameter.sql_usable !== false ? (
                <TextField
                  key={parameter.session_var_key}
                  size="small"
                  label={parameter.display_name || parameter.name}
                  value={sessionVars[parameter.session_var_key] ?? ""}
                  onChange={(event) =>
                    setSessionVars((previous) => ({
                      ...previous,
                      [parameter.session_var_key]: event.target.value,
                    }))
                  }
                  helperText={parameter.description || parameter.name}
                  inputProps={{ "data-session-var-key": parameter.session_var_key }}
                  sx={{ minWidth: 180 }}
                />
              ) : (
                <Tooltip
                  key={parameter.session_var_key}
                  title={t("query.namedObjectUnavailable", {
                    name: parameter.canonical_name || parameter.name,
                    reason:
                      parameter.unusable_reason || t("query.namedObjectUnavailableUnknown"),
                  })}
                >
                  <span>
                    <Chip
                      size="small"
                      label={parameter.display_name || parameter.name}
                      disabled
                      data-session-var-key={parameter.session_var_key}
                      data-sql-usable="false"
                    />
                  </span>
                </Tooltip>
              ),
            )}
          </Stack>
        </Paper>
      )}

      {((namedObjects.data?.named_sets.length ?? 0) > 0 ||
        (namedObjects.data?.named_queries.length ?? 0) > 0) && (
        <Paper variant="outlined" sx={{ p: 1 }}>
          <Typography variant="caption" fontWeight={700} display="block" sx={{ mb: 0.75 }}>
            {t("query.deployedNamedObjects")}
          </Typography>
          <Stack direction="row" spacing={0.75} useFlexGap flexWrap="wrap">
            {namedObjects.data?.named_sets.map((namedSet) =>
              namedSet.sql_usable ? (
                <Chip
                  key={`set-${namedSet.name}`}
                  size="small"
                  label={namedSet.name}
                  variant="outlined"
                  onClick={() => void navigator.clipboard?.writeText(`@${namedSet.name}`)}
                  title={t("query.copyNamedObject", { name: namedSet.name })}
                />
              ) : (
                <Tooltip
                  key={`set-${namedSet.name}`}
                  title={t("query.namedObjectUnavailable", {
                    name: namedSet.name,
                    reason: namedSet.unusable_reason || t("query.namedObjectUnavailableUnknown"),
                  })}
                >
                  <span>
                    <Chip size="small" label={namedSet.name} disabled />
                  </span>
                </Tooltip>
              ),
            )}
            {namedObjects.data?.named_queries.map((namedQuery) =>
              namedQuery.sql_usable ? (
                <Chip
                  key={`query-${namedQuery.name}`}
                  size="small"
                  label={namedQuery.name}
                  variant="outlined"
                  onClick={() => void navigator.clipboard?.writeText(`@${namedQuery.name}`)}
                  title={t("query.copyNamedObject", { name: namedQuery.name })}
                />
              ) : (
                <Tooltip
                  key={`query-${namedQuery.name}`}
                  title={t("query.namedObjectUnavailable", {
                    name: namedQuery.name,
                    reason: namedQuery.unusable_reason || t("query.namedObjectUnavailableUnknown"),
                  })}
                >
                  <span>
                    <Chip size="small" label={namedQuery.name} disabled />
                  </span>
                </Tooltip>
              ),
            )}
          </Stack>
        </Paper>
      )}

      {error && (
        <Alert
          severity="error"
          onClose={() => {
            setError(null);
            setErrorFieldCompatibility(null);
          }}
          sx={{ whiteSpace: "pre-line" }}
        >
          {error}
        </Alert>
      )}
      <QueryFieldCompatibilityAlert feedback={errorFieldCompatibility} t={t} />
      {validateResult && (
        <Alert
          severity={validateResult.ok ? "success" : "error"}
          onClose={() => setValidateResult(null)}
        >
          {validateResult.ok ? (
            <>
              {t("query.queryIsValid")}{" "}
              {validateResult.requested_measures.length > 0 && (
                <>
                  {t("query.measures")}: <strong>{validateResult.requested_measures.join(", ")}</strong>.{" "}
                </>
              )}
              {validateResult.requested_dimensions.length > 0 && (
                <>
                  {t("query.dimensions")}: <strong>{validateResult.requested_dimensions.join(", ")}</strong>.
                </>
              )}
            </>
          ) : (
            <>
              {validateResult.errors.map((e, i) => (
                <div key={i}>{e}</div>
              ))}
            </>
          )}
          {validateResult.warnings.length > 0 && (
            <Box mt={0.5}>
              {validateResult.warnings.map((w, i) => (
                <Typography key={i} variant="caption" display="block">
                  {t("query.warning")}: {w}
                </Typography>
              ))}
            </Box>
          )}
        </Alert>
      )}
      <QueryFieldCompatibilityAlert
        feedback={validateResult?.field_compatibility}
        t={t}
      />

      {trace && trace.steps.length > 0 && (
        <Box sx={{ opacity: traceRequestInFlight ? 0.45 : 1, transition: "opacity 200ms ease" }}>
          <Typography variant="subtitle2" fontWeight={700} mb={0.5}>
            {t("query.routingPipeline")}
            {traceRequestInFlight && (
              <Typography component="span" variant="caption" color="text.secondary" sx={{ ml: 1 }}>
                ({t("common.loading")}...)
              </Typography>
            )}
          </Typography>
          <PipelineDiagram trace={trace} />
        </Box>
      )}

      {rewrittenSql && (
        <Box>
          <Box display="flex" alignItems="center" mb={0.5}>
            <Typography variant="subtitle2" fontWeight={700} flexGrow={1}>
              {t("query.rewrittenSql")}
            </Typography>
            <Tooltip title={t("query.copyRewrittenSql")}>
              <IconButton size="small" onClick={() => handleCopy(rewrittenSql)}>
                <ContentCopyIcon fontSize="small" />
              </IconButton>
            </Tooltip>
          </Box>
          <HighlightedSql sql={formatSql(rewrittenSql)} />
        </Box>
      )}
      <QueryFieldCompatibilityAlert
        feedback={explainResult?.field_compatibility}
        t={t}
      />

      {executeResult && (
        <Box>
          <Box display="flex" gap={1} flexWrap="wrap" mb={0.5}>
            <Tooltip
              title={routeReasonLabel(
                executeResult.route_type,
                executeResult.reason,
                t,
              )}
              placement="top"
              arrow
            >
              <Chip
                label={`${t("query.route")}: ${routeBadgeLabel(executeResult.route_type, t)}`}
                size="small"
                color={routeBadgeColor(executeResult.route_type)}
              />
            </Tooltip>
            <Chip label={t("query.rowsCount", { count: String(executeResult.rows_returned) })} size="small" variant="outlined" />
            <Chip label={t("query.executionTime", { ms: String(executeResult.execution_ms) })} size="small" variant="outlined" />
            {executeResult.bytes_processed > 0 && (
              <Chip
                label={t("query.bytesProcessed", { bytes: executeResult.bytes_processed.toLocaleString() })}
                size="small"
                variant="outlined"
              />
            )}
          </Box>
          <QueryFieldCompatibilityAlert
            feedback={executeResult.field_compatibility}
            t={t}
          />
          {/* Bug-8453: a row-security deny-all previously rendered as a bare
              empty grid, indistinguishable from "there is genuinely no data".
              Shown regardless of row count, because a denied query still
              returns a row for COUNT-shaped SQL (WHERE 0 = 1 -> 0). */}
          {executeRowSecurityDenied && (
            <Alert severity="warning" sx={{ mb: 1 }}>
              <AlertTitle>{t("query.rowSecurityDeniedTitle")}</AlertTitle>
              {t("query.rowSecurityDeniedBody")}
            </Alert>
          )}
          {/* R5 finding F6: suppress the RESULT too, not just the empty-state
              text. A denial can return a COUNT row containing 0, and a
              screenshot of that cell does not travel with the warning above --
              the same policy the pivot panel applies. */}
          {executeRowSecurityDenied ? null : allRows.length === 0 ? (
            <Typography variant="caption" color="text.secondary">
              {t("query.noRowsReturned")}
            </Typography>
          ) : (
            <Paper variant="outlined">
              <TableContainer sx={{ maxHeight: 360 }}>
                <Table size="small" stickyHeader>
                  <TableHead>
                    <TableRow>
                      {executeResult.columns.map((col) => (
                        <TableCell key={col} sx={{ fontWeight: 700 }}>
                          {col}
                        </TableCell>
                      ))}
                    </TableRow>
                  </TableHead>
                  <TableBody>
                    {pagedRows.map((row, i) => (
                      <TableRow key={page * pageSize + i} hover>
                        {executeResult.columns.map((col) => (
                          <TableCell key={col} sx={{ fontFamily: "JetBrains Mono, monospace", fontSize: 11 }}>
                            {formatCell(row[col], formatByColumn.get(col))}
                          </TableCell>
                        ))}
                      </TableRow>
                    ))}
                  </TableBody>
                </Table>
              </TableContainer>
              <TablePagination
                component="div"
                count={allRows.length}
                page={page}
                onPageChange={(_, p) => setPage(p)}
                rowsPerPage={pageSize}
                onRowsPerPageChange={(e) => {
                  setPageSize(Number(e.target.value));
                  setPage(0);
                }}
                rowsPerPageOptions={[10, 25, 50, 100]}
                size="small"
              />
            </Paper>
          )}
        </Box>
      )}
      <Dialog open={saveOpen} onClose={() => setSaveOpen(false)} maxWidth="sm" fullWidth>
        <DialogTitle>{t("query.saveQueryTitle")}</DialogTitle>
        <DialogContent>
          <Stack spacing={2} sx={{ mt: 1 }}>
            <TextField
              label={t("query.name")}
              size="small"
              fullWidth
              value={saveName}
              onChange={(e) => setSaveName(e.target.value)}
            />
            <TextField
              label={t("query.descriptionOptional")}
              size="small"
              fullWidth
              value={saveDesc}
              onChange={(e) => setSaveDesc(e.target.value)}
            />
            <TextField
              label={t("query.sqlLabel")}
              size="small"
              fullWidth
              multiline
              minRows={3}
              maxRows={8}
              value={sql}
              InputProps={{ readOnly: true, sx: { fontFamily: "monospace", fontSize: "0.85rem" } }}
            />
          </Stack>
        </DialogContent>
        <DialogActions>
          <Button onClick={() => setSaveOpen(false)}>{t("common.cancel")}</Button>
          <Button
            variant="contained"
            disabled={!saveName.trim() || saveMutation.isPending}
            onClick={() =>
              saveMutation.mutate({
                name: saveName.trim(),
                description: saveDesc.trim() || undefined,
                query_text: sql.trim(),
              })
            }
          >
            {saveMutation.isPending ? t("query.saving") : t("common.save")}
          </Button>
        </DialogActions>
      </Dialog>
    </Stack>
  );
}

function QueryFieldCompatibilityAlert({
  feedback,
  t,
}: {
  feedback?: QueryRouterFieldCompatibilityFeedback | null;
  t: (key: string, vars?: Record<string, string>) => string;
}) {
  if (!feedback || !shouldRenderFieldCompatibility(feedback)) return null;

  const notAnalyzed = hasNotAnalyzedFieldCompatibility(feedback);
  const messages = notAnalyzed ? [] : fieldCompatibilityMessages(feedback);
  const compatibleDimensionNames =
    fieldCompatibilityCompatibleDimensionNames(feedback);
  const severity =
    feedback.status === "incompatible" && !notAnalyzed ? "error" : "warning";

  return (
    <Alert severity={severity} sx={{ whiteSpace: "pre-line" }}>
      <Typography variant="subtitle2" fontWeight={700} gutterBottom>
        {t("query.fieldCompatibility.title")}
      </Typography>
      {notAnalyzed ? (
        <Typography variant="body2">
          {t("query.fieldCompatibility.notAnalyzedWarning")}
        </Typography>
      ) : (
        messages.map((message, index) => (
          <Typography key={index} variant="body2">
            {message}
          </Typography>
        ))
      )}
      {!notAnalyzed && compatibleDimensionNames.length > 0 && (
        <Typography variant="caption" display="block" sx={{ mt: 0.5 }}>
          {t("query.fieldCompatibility.compatibleDimensions")}:{" "}
          <strong>{compatibleDimensionNames.join(", ")}</strong>
        </Typography>
      )}
    </Alert>
  );
}

function routeReasonLabel(
  routeType: string,
  reason: string | undefined,
  t: (key: string) => string,
): string {
  // The routing narrative is no longer withheld from any authenticated caller
  // (decision 2026-08-11, option C), so `reason` is always prose.
  const localizedReason = reason;
  const LOCALIZED: Record<string, string> = {
    source: "query.routeReasonSource",
    aggregate: "query.routeReasonAggregate",
    pocket: "query.routeReasonPocket",
  };
  const i18nKey = LOCALIZED[routeType];
  if (i18nKey) {
    const summary = t(i18nKey);
    // Bug-6971: surface the router's detailed reason string instead of
    // discarding it for known route types.
    return localizedReason ? `${summary}\n${localizedReason}` : summary;
  }
  return localizedReason || `${t("query.routeType")}: ${routeType}`;
}

function routeBadgeLabel(routeType: string, t: (key: string) => string): string {
  // F-004-15: translate the aggregate/pocket route-type values too, not just
  // "source"; previously the raw English route_type string leaked into the UI.
  if (routeType === "source") return t("query.liveSource");
  if (routeType === "aggregate") return t("query.routeAggregate");
  if (routeType === "pocket") return t("query.routePocket");
  return routeType;
}

function routeBadgeColor(
  routeType: string,
): "primary" | "secondary" | "warning" | "default" {
  if (routeType === "aggregate") return "primary";
  if (routeType === "pocket") return "secondary";
  if (routeType === "source") return "warning";
  return "default";
}

function formatCell(value: unknown, formatToken?: MeasureFormatToken | null): string {
  if (value === null || value === undefined) return "—";
  if (formatToken) {
    const out = formatMeasureValue(value, formatToken);
    if (out !== "" && out !== String(value)) return out;
  }
  if (typeof value === "object") return JSON.stringify(value);
  return String(value);
}
