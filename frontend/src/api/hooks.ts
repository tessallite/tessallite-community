import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useQueries, useQuery } from "@tanstack/react-query";
import { safeLocalGet } from "../utils/safeLocalStorage";
import type { Hierarchy, HierarchyLevel } from "./types";
import {
  aggregatesApi,
  aiOptimizerApi,
  aiSchedulerApi,
  attributeRelationshipsApi,
  connectionsApi,
  dataTagsApi,
  dimensionsApi,
  downstreamAssetsApi,
  fieldCompatibilityApi,
  hierarchiesApi,
  impactAnalysisApi,
  impactScanApi,
  joinsApi,
  kpisApi,
  llmConfigsApi,
  logsApi,
  measuresApi,
  modelTablesApi,
  namedQueriesApi,
  namedSetsApi,
  modelsApi,
  optimizerApiClient,
  preferencesApi,
  personasApi,
  pocketsApi,
  rowSecurityApi,
  projectsApi,
  projectSettingsApi,
  sourcesApi,
  tableAttributesApi,
  targetsApi,
  tenantsApi,
  savedQueriesApi,
  userDefinedAttributesApi,
  editionApi,
  advisoriesApi,
} from "./client";

// ---------------------------------------------------------------------------
// Edition / licensing (read-only badge + current/max display)
// ---------------------------------------------------------------------------

const EDITION_STALE_MS = 5 * 60 * 1000;

export function useEdition() {
  return useQuery({
    queryKey: ["edition"],
    queryFn: () => editionApi.getEdition(),
    staleTime: EDITION_STALE_MS,
  });
}

export function useLimits() {
  return useQuery({
    queryKey: ["edition", "limits"],
    queryFn: () => editionApi.getLimits(),
    staleTime: EDITION_STALE_MS,
  });
}

const ADVISORIES_STALE_MS = 15 * 60 * 1000;

export function useAdvisories() {
  return useQuery({
    queryKey: ["advisories"],
    queryFn: () => advisoriesApi.list(),
    staleTime: ADVISORIES_STALE_MS,
    // The issuer is external; one quick retry, then fall through to the
    // unreachable state rather than spinning.
    retry: 1,
  });
}

// ---------------------------------------------------------------------------
// Tenant / Project
// ---------------------------------------------------------------------------

export function useTenantMe() {
  return useQuery({
    queryKey: ["tenant", "me"],
    queryFn: () => tenantsApi.me(),
  });
}

export function useProjects() {
  return useQuery({
    queryKey: ["projects"],
    queryFn: () => projectsApi.list(),
  });
}

export function useProject(projectId: string) {
  return useQuery({
    queryKey: ["projects", projectId],
    queryFn: () => projectsApi.get(projectId),
    enabled: !!projectId,
  });
}

// ---------------------------------------------------------------------------
// Connections
// ---------------------------------------------------------------------------

export function useConnections(projectId: string) {
  return useQuery({
    queryKey: ["connections", projectId],
    queryFn: () => connectionsApi.list(projectId),
    enabled: !!projectId,
  });
}

// ---------------------------------------------------------------------------
// Model
// ---------------------------------------------------------------------------

export function useModels(projectId: string) {
  return useQuery({
    queryKey: ["models", projectId],
    queryFn: () => modelsApi.list(projectId),
    enabled: !!projectId,
  });
}

export function useModel(projectId: string, modelId: string) {
  return useQuery({
    queryKey: ["models", projectId, modelId],
    queryFn: () => modelsApi.get(projectId, modelId),
    enabled: !!projectId && !!modelId,
  });
}

// ---------------------------------------------------------------------------
// Sources / Targets
// ---------------------------------------------------------------------------

export function useSources(projectId: string, modelId: string) {
  return useQuery({
    queryKey: ["sources", projectId, modelId],
    queryFn: ({ signal }) => sourcesApi.list(projectId, modelId, signal),
    enabled: !!projectId && !!modelId,
  });
}

export function useModelTables(
  projectId: string,
  modelId: string,
  sourceId: string,
) {
  return useQuery({
    queryKey: ["modelTables", projectId, modelId, sourceId],
    queryFn: ({ signal }) => modelTablesApi.list(projectId, modelId, sourceId, signal),
    enabled: !!projectId && !!modelId && !!sourceId,
  });
}

export function useTableAttributes(
  projectId: string,
  modelId: string,
  tableId: string,
) {
  return useQuery({
    queryKey: ["tableAttributes", projectId, modelId, tableId],
    queryFn: () => tableAttributesApi.list(projectId, modelId, tableId),
    enabled: !!projectId && !!modelId && !!tableId,
  });
}

export function useAllModelTables(
  projectId: string,
  modelId: string,
  sourceIds: string[],
) {
  const sortedSourceIds = [...sourceIds].sort();
  return useQuery({
    queryKey: ["allModelTables", projectId, modelId, sortedSourceIds],
    queryFn: async () => {
      const results = await Promise.all(
        sortedSourceIds.map((sid) => modelTablesApi.list(projectId, modelId, sid))
      );
      return results.flat();
    },
    enabled: !!projectId && !!modelId && sortedSourceIds.length > 0,
  });
}

export function useTargets(projectId: string, modelId: string) {
  return useQuery({
    queryKey: ["targets", projectId, modelId],
    queryFn: () => targetsApi.list(projectId, modelId),
    enabled: !!projectId && !!modelId,
  });
}

// ---------------------------------------------------------------------------
// Dimensions / Measures
// ---------------------------------------------------------------------------

export function useHierarchies(projectId: string, modelId: string) {
  return useQuery({
    queryKey: ["hierarchies", projectId, modelId],
    queryFn: () => hierarchiesApi.list(projectId, modelId),
    enabled: !!projectId && !!modelId,
  });
}

/**
 * Fetch every hierarchy on the model together with its levels.  The backend
 * list endpoint returns `Hierarchy[]` (metadata only, no levels); level detail
 * lives on a per-hierarchy `listLevels` call.  `useQueries` runs the level
 * fetches in parallel and joins the result without an N+1 re-render storm.
 *
 * Used by the canvas segmentation (A1) and hierarchy grouping overlay (A3).
 */
export interface HierarchyWithLevels extends Hierarchy {
  levels: HierarchyLevel[];
}

export function useHierarchiesWithLevels(projectId: string, modelId: string) {
  const hierarchies = useHierarchies(projectId, modelId);
  const hierarchyList = hierarchies.data;

  const levelQueries = useQueries({
    queries: (hierarchyList ?? []).map((h) => ({
      queryKey: ["hierarchyLevels", projectId, modelId, h.id],
      queryFn: () => hierarchiesApi.listLevels(projectId, modelId, h.id),
      enabled: !!projectId && !!modelId && !!h.id,
    })),
  });

  // Memoise the joined result. Without this, `data` is a brand-new array with
  // brand-new objects on every render — which invalidates any downstream
  // useMemo keyed on `data` (notably the Canvas segmentation/hierarchy-group
  // memos) and triggers a setNodes re-run that wipes ReactFlow's internal
  // node dimensions, leaving every node stuck at `visibility: hidden`.
  // Keyed on data-pointer equality via dataUpdatedAt timestamps.
  const levelUpdatedKey = levelQueries.map((q) => q.dataUpdatedAt ?? 0).join("|");
  const isLoading = hierarchies.isLoading || levelQueries.some((q) => q.isLoading);
  const isError = hierarchies.isError || levelQueries.some((q) => q.isError);

  const data = useMemo<HierarchyWithLevels[]>(() => {
    const list = hierarchyList ?? [];
    return list.map((h, idx) => ({
      ...h,
      levels: levelQueries[idx]?.data ?? [],
    }));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [hierarchyList, levelUpdatedKey]);

  return useMemo(
    () => ({ data, isLoading, isError }),
    [data, isLoading, isError],
  );
}

export function useHierarchyLevels(
  projectId: string,
  modelId: string,
  hierarchyId: string,
) {
  return useQuery({
    queryKey: ["hierarchyLevels", projectId, modelId, hierarchyId],
    queryFn: () => hierarchiesApi.listLevels(projectId, modelId, hierarchyId),
    enabled: !!projectId && !!modelId && !!hierarchyId,
  });
}

export function useHierarchy(
  projectId: string,
  modelId: string,
  hierarchyId: string,
) {
  return useQuery({
    queryKey: ["hierarchy", projectId, modelId, hierarchyId],
    queryFn: () => hierarchiesApi.get(projectId, modelId, hierarchyId),
    enabled: !!projectId && !!modelId && !!hierarchyId,
  });
}

export function useDimensions(projectId: string, modelId: string) {
  return useQuery({
    queryKey: ["dimensions", projectId, modelId],
    queryFn: ({ signal }) => dimensionsApi.list(projectId, modelId, signal),
    enabled: !!projectId && !!modelId,
  });
}

export function useAttributeRelationships(
  projectId: string,
  modelId: string,
  dimensionId: string,
) {
  return useQuery({
    queryKey: ["attributeRelationships", projectId, modelId, dimensionId],
    queryFn: () =>
      attributeRelationshipsApi.list(projectId, modelId, dimensionId),
    enabled: !!projectId && !!modelId && !!dimensionId,
  });
}

export function useMeasures(projectId: string, modelId: string) {
  return useQuery({
    queryKey: ["measures", projectId, modelId],
    queryFn: ({ signal }) => measuresApi.list(projectId, modelId, signal),
    enabled: !!projectId && !!modelId,
  });
}

export function useFieldCompatibility(
  projectId: string,
  modelId: string,
  personaId?: string | null,
  measureIds: string[] = [],
  dimensionIds: string[] = [],
) {
  const sortedMeasureIds = [...new Set(measureIds)].sort();
  const sortedDimensionIds = [...new Set(dimensionIds)].sort();
  return useQuery({
    queryKey: [
      "fieldCompatibility",
      projectId,
      modelId,
      personaId ?? null,
      sortedMeasureIds,
      sortedDimensionIds,
    ],
    queryFn: ({ signal }) =>
      fieldCompatibilityApi.get(projectId, modelId, {
        personaId,
        measureIds: sortedMeasureIds,
        dimensionIds: sortedDimensionIds,
        signal,
      }),
    enabled: !!projectId && !!modelId && sortedMeasureIds.length > 0,
  });
}

export function useUserDefinedAttributes(
  projectId: string,
  modelId: string,
  tableId: string,
) {
  return useQuery({
    queryKey: ["userDefinedAttributes", projectId, modelId, tableId],
    queryFn: () => userDefinedAttributesApi.list(projectId, modelId, tableId),
    enabled: !!projectId && !!modelId && !!tableId,
  });
}

// ---------------------------------------------------------------------------
// Joins
// ---------------------------------------------------------------------------

export function useJoins(projectId: string, modelId: string) {
  return useQuery({
    queryKey: ["joins", projectId, modelId],
    queryFn: ({ signal }) => joinsApi.list(projectId, modelId, signal),
    enabled: !!projectId && !!modelId,
  });
}

// ---------------------------------------------------------------------------
// Aggregates
// ---------------------------------------------------------------------------

export function useAggregates(projectId: string, modelId: string) {
  return useQuery({
    queryKey: ["aggregates", projectId, modelId],
    queryFn: () => aggregatesApi.list(projectId, modelId),
    enabled: !!projectId && !!modelId,
  });
}

export function usePockets(projectId: string, modelId: string) {
  return useQuery({
    queryKey: ["pockets", projectId, modelId],
    queryFn: () => pocketsApi.list(projectId, modelId),
    enabled: !!projectId && !!modelId,
  });
}

export function useRowSecurityRules(projectId: string, modelId: string) {
  return useQuery({
    queryKey: ["row-security", projectId, modelId],
    queryFn: () => rowSecurityApi.list(projectId, modelId),
    enabled: !!projectId && !!modelId,
  });
}

// ---------------------------------------------------------------------------
// Lineage
// ---------------------------------------------------------------------------

export function useLineage(projectId: string, modelId: string) {
  return useQuery({
    queryKey: ["lineage", projectId, modelId],
    queryFn: () => modelsApi.lineage(projectId, modelId),
    enabled: !!projectId && !!modelId,
  });
}

// ---------------------------------------------------------------------------
// Metrics
// ---------------------------------------------------------------------------

export function useMetrics(
  projectId: string,
  modelId: string,
  windowHours = 24,
) {
  return useQuery({
    queryKey: ["metrics", projectId, modelId, windowHours],
    queryFn: () => modelsApi.metrics(projectId, modelId, windowHours),
    enabled: !!projectId && !!modelId,
  });
}

// ---------------------------------------------------------------------------
// Logs
// ---------------------------------------------------------------------------

export function useQueryLogs(
  projectId: string,
  filters: {
    modelId?: string;
    page?: number;
    pageSize?: number;
    status?: string;
    errorType?: string;
    routeType?: string;
    clientKind?: string;
    userIdentity?: string;
    dateFrom?: string;
    dateTo?: string;
    includeProbes?: boolean;
  } = {},
) {
  return useQuery({
    queryKey: ["queryLogs", projectId, filters],
    queryFn: () => logsApi.queries(projectId, filters),
    enabled: !!projectId,
  });
}

export function useMissLogs(projectId: string, modelId?: string) {
  return useQuery({
    queryKey: ["missLogs", projectId, modelId],
    queryFn: () => logsApi.misses(projectId, modelId),
    enabled: !!projectId,
  });
}

export function useModelRefreshRuns(projectId: string, modelId: string) {
  return useQuery({
    queryKey: ["modelRefreshRuns", projectId, modelId],
    queryFn: () => aggregatesApi.getModelRuns(projectId, modelId),
    enabled: !!projectId && !!modelId,
  });
}

export function useOptimizerRuns() {
  return useQuery({
    queryKey: ["optimizerRuns"],
    queryFn: () => optimizerApiClient.getRuns(),
    refetchInterval: 30_000,
  });
}

// ---------------------------------------------------------------------------
// AI Scheduler Config
// ---------------------------------------------------------------------------

export function useAISchedulerConfig(projectId: string, modelId: string) {
  return useQuery({
    queryKey: ["aiSchedulerConfig", projectId, modelId],
    queryFn: () => aiSchedulerApi.get(projectId, modelId),
    enabled: !!projectId && !!modelId,
  });
}

// ---------------------------------------------------------------------------
// LLM Configs
// ---------------------------------------------------------------------------

export function useLLMConfigs(projectId: string | undefined) {
  return useQuery({
    queryKey: ["llmConfigs", projectId],
    queryFn: () => llmConfigsApi.list(projectId as string),
    enabled: Boolean(projectId),
  });
}

// ---------------------------------------------------------------------------
// AI Optimizer Runs
// ---------------------------------------------------------------------------

export function useAIOptimizerRuns(tenantId?: string, modelId?: string) {
  // Bug-6558 — the optimizer route resolves the tenant from the JWT;
  // tenant_id was never a declared query parameter.  We keep the hook
  // signature unchanged (callers still pass tenantId for the query key /
  // enabled guard) but no longer forward it to the API call.
  const resolvedTenantId = tenantId || safeLocalGet("tenant_id", "");
  return useQuery({
    queryKey: ["aiOptimizerRuns", resolvedTenantId, modelId],
    queryFn: () => aiOptimizerApi.listRuns(modelId),
    enabled: !!resolvedTenantId,
    refetchInterval: 30_000,
  });
}

// F-011-15: the list endpoint returns lightweight summaries (no recommendations,
// raw response, or decision log). Full detail — including those heavy fields —
// is fetched on demand via this hook only when a run row is expanded.
export function useAIOptimizerRun(runId: string | null) {
  return useQuery({
    queryKey: ["aiOptimizerRun", runId],
    queryFn: () => aiOptimizerApi.getRun(runId as string),
    enabled: !!runId,
  });
}

// ---------------------------------------------------------------------------
// Personas (Phase 8.B)
// ---------------------------------------------------------------------------

export function usePersonas(
  projectId: string,
  modelId: string,
  opts?: { forAudience?: boolean },
) {
  return useQuery({
    queryKey: ["personas", projectId, modelId, opts?.forAudience ?? false],
    queryFn: () => personasApi.list(projectId, modelId, opts),
    enabled: !!projectId && !!modelId,
  });
}

export function usePersona(
  projectId: string,
  modelId: string,
  personaId: string,
) {
  return useQuery({
    queryKey: ["persona", projectId, modelId, personaId],
    queryFn: () => personasApi.get(projectId, modelId, personaId),
    enabled: !!projectId && !!modelId && !!personaId,
  });
}


// ---------------------------------------------------------------------------
// SSE — Agent token streaming
// ---------------------------------------------------------------------------

/**
 * Opens an SSE connection to POST .../messages/stream and accumulates
 * narration.delta token events into a live text string.
 *
 * Call `send(message)` to start a new streaming turn. The returned
 * `text` updates as tokens arrive; `done` is true when the turn is complete.
 */
export function useAgentStream(projectId: string, conversationId: string) {
  const [text, setText] = useState("");
  const [streaming, setStreaming] = useState(false);
  const [done, setDone] = useState(false);
  // Bug-6670: AbortController replaces the dead esRef (which was typed as
  // EventSource but never assigned — fetch+ReadableStream was the actual
  // transport). Aborting the controller cancels both the fetch and the
  // ReadableStream reader in one shot.
  const abortRef = useRef<AbortController | null>(null);
  // Bug-6670: monotonic turn token prevents a stale/aborted turn's chunks
  // from leaking into the next turn's text accumulator.
  const turnTokenRef = useRef(0);

  const send = useCallback(
    (message: string) => {
      // Abort any in-flight streaming turn before starting the new one.
      if (abortRef.current) {
        abortRef.current.abort();
        abortRef.current = null;
      }
      const controller = new AbortController();
      abortRef.current = controller;
      const currentTurn = ++turnTokenRef.current;

      setText("");
      setDone(false);
      setStreaming(true);

      // Use fetch + ReadableStream to POST with a body and receive SSE
      const url = `/api/v1/projects/${projectId}/agent/conversations/${conversationId}/messages/stream`;
      const csrf = document.cookie
        .split("; ")
        .find((row) => row.startsWith("csrf_token="))
        ?.split("=")[1];

      fetch(url, {
        method: "POST",
        credentials: "include",
        signal: controller.signal,
        headers: {
          "Content-Type": "application/json",
          ...(csrf ? { "X-CSRF-Token": csrf } : {}),
        },
        body: JSON.stringify({ text: message }),
      }).then((resp) => {
        if (!resp.ok || !resp.body) {
          setStreaming(false);
          setDone(true);
          return;
        }
        const reader = resp.body.getReader();
        const decoder = new TextDecoder();
        let buffer = "";

        const processChunk = ({ done: streamDone, value }: ReadableStreamReadResult<Uint8Array>): Promise<void> | void => {
          // Guard: if this turn was superseded, stop processing.
          if (turnTokenRef.current !== currentTurn) return;
          if (streamDone) {
            setStreaming(false);
            setDone(true);
            return;
          }
          buffer += decoder.decode(value, { stream: true });
          const lines = buffer.split("\n");
          buffer = lines.pop() ?? "";
          for (const line of lines) {
            if (line.startsWith("event: turn.stream.closed") || line.includes("turn.stream.closed")) {
              setStreaming(false);
              setDone(true);
            }
            if (line.startsWith("data:")) {
              try {
                const payload = JSON.parse(line.slice(5).trim());
                if (payload.text && turnTokenRef.current === currentTurn) {
                  setText((prev) => prev + payload.text);
                }
              } catch {
                // ignore malformed
              }
            }
          }
          return reader.read().then(processChunk);
        };

        reader.read().then(processChunk).catch((err) => {
          // AbortError is expected when the user cancels — not a failure.
          if (err?.name === "AbortError") return;
          setStreaming(false);
          setDone(true);
        });
      }).catch((err) => {
        if (err?.name === "AbortError") return;
        setStreaming(false);
        setDone(true);
      });
    },
    [projectId, conversationId],
  );

  const reset = useCallback(() => {
    if (abortRef.current) {
      abortRef.current.abort();
      abortRef.current = null;
    }
    // R1 Finding 3: bump the turn token so any in-flight chunk that
    // resolved before the abort takes effect is discarded by the
    // turnTokenRef guard instead of leaking into the cleared state.
    ++turnTokenRef.current;
    setText("");
    setStreaming(false);
    setDone(false);
  }, []);

  // Cleanup on unmount: abort any in-flight stream.
  useEffect(() => {
    return () => {
      if (abortRef.current) {
        abortRef.current.abort();
        abortRef.current = null;
      }
    };
  }, []);

  return { text, streaming, done, send, reset };
}

// ---------------------------------------------------------------------------
// Usage & Downstream Assets
// ---------------------------------------------------------------------------

export function useDownstreamAssets(projectId: string, modelId: string) {
  return useQuery({
    queryKey: ["downstream-assets", projectId, modelId],
    queryFn: () => downstreamAssetsApi.list(projectId, modelId),
    enabled: !!projectId && !!modelId,
  });
}

export function useDownstreamAssetSummary(
  projectId: string,
  modelId: string
) {
  return useQuery({
    queryKey: ["downstream-assets-summary", projectId, modelId],
    queryFn: () => downstreamAssetsApi.summary(projectId, modelId),
    enabled: !!projectId && !!modelId,
  });
}

export function useDataTags(projectId: string, modelId: string) {
  return useQuery({
    queryKey: ["data-tags", projectId, modelId],
    queryFn: () => dataTagsApi.list(projectId, modelId),
    enabled: !!projectId && !!modelId,
  });
}

export function useGatewayQueryReferences(
  projectId: string,
  modelId: string
) {
  return useQuery({
    queryKey: ["gateway-query-refs", projectId, modelId],
    queryFn: () => impactScanApi.queryReferences(projectId, modelId),
    enabled: !!projectId && !!modelId,
  });
}

// ---------------------------------------------------------------------------
// Impact Analysis (Bug-7787, Phase 4)
// ---------------------------------------------------------------------------

export function useImpactCatalogue(
  projectId: string,
  modelId: string,
  params?: { object_types?: string; search?: string; cursor?: string; limit?: number },
) {
  return useQuery({
    queryKey: ["impact-catalogue", projectId, modelId, params],
    queryFn: () => impactAnalysisApi.catalogue(projectId, modelId, params),
    enabled: !!projectId && !!modelId,
  });
}

export function useImpactQuery(
  projectId: string,
  modelId: string,
  body: import("./types_domains/model_impact").ImpactQueryRequest | null,
) {
  return useQuery({
    queryKey: ["impact-query", projectId, modelId, body],
    queryFn: () => impactAnalysisApi.query(projectId, modelId, body!),
    enabled: !!projectId && !!modelId && body !== null,
  });
}

export function useColumnUsage(projectId: string, modelId: string) {
  return useQuery({
    queryKey: ["column-usage", projectId, modelId],
    queryFn: () => impactAnalysisApi.columnUsage(projectId, modelId),
    enabled: !!projectId && !!modelId,
  });
}

// ---------------------------------------------------------------------------
// Named Sets
// ---------------------------------------------------------------------------

export function useNamedSets(projectId: string, modelId: string) {
  return useQuery({
    queryKey: ["namedSets", projectId, modelId],
    queryFn: () => namedSetsApi.list(projectId, modelId),
    enabled: !!projectId && !!modelId,
  });
}

// ---------------------------------------------------------------------------
// Named Queries
// ---------------------------------------------------------------------------

/** Module-level empty list so an unloaded query never hands consumers a new
 *  array identity every render (Bug-100 ref-stability class). */
const EMPTY_NAMED_QUERIES: import("./types").NamedQuery[] = [];

/**
 * Bug-100 (ref stability): both the joined data AND the wrapper object are
 * memoised. TanStack Query returns a new result object every render, so
 * consumers keying their own useMemo on `data` (the panel's kind filter, the
 * editor's health lookup) would churn otherwise.
 */
export function useNamedQueries(projectId: string, modelId: string) {
  const query = useQuery({
    queryKey: ["namedQueries", projectId, modelId],
    queryFn: () => namedQueriesApi.list(projectId, modelId),
    enabled: !!projectId && !!modelId,
  });
  const data = useMemo<import("./types").NamedQuery[]>(
    () => query.data ?? EMPTY_NAMED_QUERIES,
    [query.data],
  );
  const isLoading = query.isLoading;
  const isError = query.isError;
  const isFetching = query.isFetching;
  const refetch = query.refetch;
  return useMemo(
    () => ({ data, isLoading, isError, isFetching, refetch }),
    [data, isLoading, isError, isFetching, refetch],
  );
}

export interface NamedQueryCaps {
  /** Effective system row cap (named_query.max_rows), null when unavailable. */
  maxRows: number | null;
  /** Effective system column cap (named_query.max_columns), null when unavailable. */
  maxColumns: number | null;
  isLoading: boolean;
  isError: boolean;
}

/** Effective system caps for Named Query authoring, read through the project
 *  settings API (viewer-accessible) so a per-Named-Query null cap can be shown
 *  against the real default instead of a hardcoded literal. Bug-100: the
 *  joined caps AND the wrapper are memoised. */
export function useNamedQueryCaps(projectId: string): NamedQueryCaps {
  const query = useQuery({
    queryKey: ["project-settings", projectId, "named-query-caps"],
    queryFn: () => projectSettingsApi.list(projectId),
    enabled: !!projectId,
  });
  const joined = useMemo(() => {
    const items = query.data ?? [];
    let maxRows: number | null = null;
    let maxColumns: number | null = null;
    for (const item of items) {
      if (item.key === "named_query.max_rows" && typeof item.effective_value === "number") {
        maxRows = item.effective_value;
      } else if (item.key === "named_query.max_columns" && typeof item.effective_value === "number") {
        maxColumns = item.effective_value;
      }
    }
    return { maxRows, maxColumns, isLoading: query.isLoading, isError: query.isError };
  }, [query.data, query.isLoading, query.isError]);
  return joined;
}

// ---------------------------------------------------------------------------
// KPIs
// ---------------------------------------------------------------------------

export function useKpis(
  projectId: string,
  modelId: string,
  personaId?: string | null,
  deployedOnly = false,
) {
  return useQuery({
    // F-017-05 / F-103-03 (Bug-9091): consumption surfaces (Model Health
    // scorecard) pass deployedOnly so a certified-but-undeployed edit never
    // reaches the executive card before Deploy. deployedOnly is part of the key
    // so the builder (live) and scorecard (deployed) caches never alias.
    queryKey: ["kpis", projectId, modelId, personaId ?? null, deployedOnly],
    queryFn: () => kpisApi.list(projectId, modelId, personaId ?? undefined, deployedOnly),
    enabled: !!projectId && !!modelId,
  });
}

export function useKpiBatchEvaluation(
  projectId: string,
  modelId: string,
  kpiIds: string[],
  enabled = true,
  personaId?: string | null,
) {
  return useQuery({
    // F-017-25: persona_id is part of the key so switching the scorecard's
    // "view as persona" re-evaluates under that persona's measure scope.
    queryKey: ["kpi-batch-eval", projectId, modelId, kpiIds, personaId ?? null],
    queryFn: () =>
      kpisApi.evaluateBatch(
        projectId,
        modelId,
        { kpi_ids: kpiIds },
        personaId ?? undefined,
      ),
    enabled: enabled && !!projectId && !!modelId && kpiIds.length > 0,
  });
}

// ---------------------------------------------------------------------------
// User preferences (favourites & recently used)
// ---------------------------------------------------------------------------

export function useUserPreferences(projectId: string, modelId: string) {
  return useQuery({
    queryKey: ["preferences", projectId, modelId],
    queryFn: () => preferencesApi.get(projectId, modelId),
    enabled: !!projectId && !!modelId,
  });
}

/**
 * Bug-8183: the calling user's favourited models for one project.
 *
 * Separate query key from ``useUserPreferences`` because it is a different
 * scope, not a different view of the same response — invalidating one must not
 * refetch every open model's preferences.
 */
export function useFavouriteModels(projectId: string) {
  return useQuery({
    queryKey: ["favourite-models", projectId],
    queryFn: () => preferencesApi.getFavouriteModels(projectId),
    enabled: !!projectId,
  });
}

// Source statistics — shared hook for cross-panel stats access (2G)
// ---------------------------------------------------------------------------

import type { ColumnStatistics, SourceStatistics } from "./types";

export function useModelSourceStatistics(
  projectId: string,
  modelId: string,
) {
  const sources = useSources(projectId, modelId);
  const sourceIds = useMemo(
    () => (sources.data ?? []).map((s) => s.id),
    [sources.data],
  );

  const statsQueries = useQueries({
    queries: sourceIds.map((sid) => ({
      queryKey: ["source-statistics", sid],
      queryFn: () => optimizerApiClient.getSourceStatistics(sid),
      enabled: !!sid,
    })),
  });

  const columnStatsMap = useMemo(() => {
    const map: Record<string, ColumnStatistics> = {};
    for (let i = 0; i < sourceIds.length; i++) {
      const data = statsQueries[i]?.data as SourceStatistics | undefined;
      if (!data) continue;
      for (const table of data.tables) {
        for (const col of table.columns) {
          map[col.model_column_id] = col;
        }
      }
    }
    return map;
  }, [sourceIds, statsQueries]);

  const isLoading = sources.isLoading || statsQueries.some((q) => q.isLoading);

  return { columnStatsMap, isLoading };
}

// ---------------------------------------------------------------------------
// Saved Queries
// ---------------------------------------------------------------------------

export function useSavedQueries(projectId: string, modelId: string) {
  return useQuery({
    queryKey: ["savedQueries", projectId, modelId],
    queryFn: () => savedQueriesApi.list(projectId, modelId),
    enabled: !!projectId && !!modelId,
  });
}
