import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useQueries, useQuery } from "@tanstack/react-query";
import { safeLocalGet } from "../utils/safeLocalStorage";
import type { Hierarchy, HierarchyLevel } from "./types";
import {
  aggregatesApi,
  aiOptimizerApi,
  aiSchedulerApi,
  connectionsApi,
  dataTagsApi,
  dimensionsApi,
  downstreamAssetsApi,
  fieldCompatibilityApi,
  hierarchiesApi,
  impactScanApi,
  joinsApi,
  kpisApi,
  llmConfigsApi,
  logsApi,
  measuresApi,
  modelTablesApi,
  namedSetsApi,
  modelsApi,
  optimizerApiClient,
  preferencesApi,
  personasApi,
  pocketsApi,
  rowSecurityApi,
  projectsApi,
  sourcesApi,
  tableAttributesApi,
  targetsApi,
  tenantsApi,
  savedQueriesApi,
  userDefinedAttributesApi,
  editionApi,
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
    clientKind?: "looker_studio" | "looker_cloud";
    userIdentity?: string;
    dateFrom?: string;
    dateTo?: string;
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
  // F-030-06: callers that lack a tenant id in scope (e.g. ModelHealthPanel)
  // used to pass "" and silently disable the query. Fall back to the persisted
  // auth-context tenant id so AI runs surface for every caller.
  const resolvedTenantId = tenantId || safeLocalGet("tenant_id", "");
  return useQuery({
    queryKey: ["aiOptimizerRuns", resolvedTenantId, modelId],
    queryFn: () => aiOptimizerApi.listRuns(resolvedTenantId, modelId),
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
// SSE — Refresh stream
// ---------------------------------------------------------------------------

export interface StreamRefreshRun {
  run_type: "aggregate" | "pocket";
  id: string;
  status: string;
  refresh_mode: string;
  started_at: string | null;
  completed_at: string | null;
  rows_written: number | null;
  error_message: string | null;
}

/**
 * Opens a Server-Sent Events connection to the refresh stream endpoint and
 * accumulates run updates. Falls back to a 5-second polling interval when
 * the EventSource connection fails.
 */
export function useRefreshStream(projectId: string, modelId: string) {
  const [runs, setRuns] = useState<StreamRefreshRun[]>([]);
  const [connected, setConnected] = useState(false);
  const [error, setError] = useState(false);
  const esRef = useRef<EventSource | null>(null);
  const fallbackRef = useRef<ReturnType<typeof setInterval> | null>(null);

  const url = `/api/v1/projects/${projectId}/models/${modelId}/refresh/stream`;

  const startFallbackPoll = useCallback(() => {
    if (fallbackRef.current) return;
    fallbackRef.current = setInterval(() => {
      aggregatesApi.getModelRuns(projectId, modelId).then((data) => {
        const mapped: StreamRefreshRun[] = data.map((r: any) => ({
          run_type: "aggregate" as const,
          id: r.id,
          status: r.status,
          refresh_mode: r.refresh_mode,
          started_at: r.started_at,
          completed_at: r.completed_at,
          rows_written: r.rows_written ?? null,
          error_message: r.error_message ?? null,
        }));
        setRuns(mapped);
      });
    }, 5_000);
  }, [projectId, modelId]);

  useEffect(() => {
    if (!projectId || !modelId) return;

    const es = new EventSource(url);
    esRef.current = es;

    es.addEventListener("connected", () => {
      setConnected(true);
      setError(false);
    });

    es.addEventListener("done", () => {
      setConnected(false);
      es.close();
    });

    es.addEventListener("timeout", () => {
      setConnected(false);
      es.close();
    });

    es.onmessage = (evt) => {
      try {
        const run: StreamRefreshRun = JSON.parse(evt.data);
        setRuns((prev) => {
          const idx = prev.findIndex((r) => r.id === run.id);
          if (idx >= 0) {
            const updated = [...prev];
            updated[idx] = run;
            return updated;
          }
          return [run, ...prev];
        });
      } catch {
        // ignore malformed events
      }
    };

    es.onerror = () => {
      setError(true);
      setConnected(false);
      es.close();
      startFallbackPoll();
    };

    return () => {
      es.close();
      esRef.current = null;
      if (fallbackRef.current) {
        clearInterval(fallbackRef.current);
        fallbackRef.current = null;
      }
    };
  }, [projectId, modelId, url, startFallbackPoll]);

  return { runs, connected, error };
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
  const esRef = useRef<EventSource | null>(null);

  const send = useCallback(
    (message: string) => {
      if (esRef.current) {
        esRef.current.close();
        esRef.current = null;
      }
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
                if (payload.text) {
                  setText((prev) => prev + payload.text);
                }
              } catch {
                // ignore malformed
              }
            }
          }
          return reader.read().then(processChunk);
        };

        reader.read().then(processChunk).catch(() => {
          setStreaming(false);
          setDone(true);
        });
      }).catch(() => {
        setStreaming(false);
        setDone(true);
      });
    },
    [projectId, conversationId],
  );

  const reset = useCallback(() => {
    if (esRef.current) {
      esRef.current.close();
      esRef.current = null;
    }
    setText("");
    setStreaming(false);
    setDone(false);
  }, []);

  return { text, streaming, done, send, reset };
}

// ---------------------------------------------------------------------------
// Impact Analysis — Downstream Assets
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
// KPIs
// ---------------------------------------------------------------------------

export function useKpis(projectId: string, modelId: string, personaId?: string | null) {
  return useQuery({
    queryKey: ["kpis", projectId, modelId, personaId ?? null],
    queryFn: () => kpisApi.list(projectId, modelId, personaId ?? undefined),
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
