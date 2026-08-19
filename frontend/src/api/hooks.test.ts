import { describe, it, expect, vi, afterEach } from "vitest";
import { renderHook, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import React from "react";

vi.mock("./client", () => ({
  tenantsApi: {
    me: vi.fn(() => Promise.resolve({ id: "t-1", slug: "acme", name: "Acme Corp" })),
  },
  projectsApi: {
    list: vi.fn(() =>
      Promise.resolve([{ id: "p-1", name: "Project 1" }]),
    ),
    get: vi.fn((id: string) =>
      Promise.resolve({ id, name: "Project 1" }),
    ),
  },
  connectionsApi: {
    list: vi.fn(() => Promise.resolve([{ id: "c-1", name: "pg-main" }])),
  },
  modelsApi: {
    list: vi.fn(() =>
      Promise.resolve([{ id: "m-1", name: "Model X" }]),
    ),
    get: vi.fn((_p: string, id: string) =>
      Promise.resolve({ id, name: "Model X" }),
    ),
  },
  sourcesApi: {
    list: vi.fn(() => Promise.resolve([{ id: "s-1", name: "sales" }])),
  },
  fieldCompatibilityApi: {
    get: vi.fn(() =>
      Promise.resolve({
        model_id: "m-1",
        version_id: "v-1",
        generated_at: "2026-06-14T00:00:00Z",
        status: "compatible",
        measures: {},
        multi_measure: null,
      }),
    ),
  },
  aggregatesApi: {},
  aiOptimizerApi: {
    listRuns: vi.fn((_modelId?: string) =>
      Promise.resolve([{ id: "run-1", status: "completed" }]),
    ),
  },
  aiSchedulerApi: {},
  dataTagsApi: {},
  dimensionsApi: {},
  downstreamAssetsApi: {},
  hierarchiesApi: {},
  impactScanApi: {},
  joinsApi: {},
  llmConfigsApi: {},
  logsApi: {},
  measuresApi: {},
  modelTablesApi: {},
  optimizerApiClient: {},
  personasApi: {},
  namedQueriesApi: {
    list: vi.fn(() =>
      Promise.resolve([
        {
          id: "nq-1",
          model_id: "m-1",
          name: "top_cities",
          display_name: null,
          description: null,
          display_folder: null,
          definition_sql: "SELECT city_name FROM modely",
          output_columns: null,
          shape: "projection",
          row_cap: null,
          column_cap: null,
          certification_status: "draft",
          created_by: null,
          artifact: null,
          refresh_policy: null,
          created_at: "2026-01-01",
          updated_at: "2026-01-01",
        },
      ]),
    ),
  },
  projectSettingsApi: {
    list: vi.fn(() =>
      Promise.resolve([
        { key: "named_query.max_rows", effective_value: 100000 },
        { key: "named_query.max_columns", effective_value: 200 },
      ]),
    ),
  },
  pocketsApi: {},
  rowSecurityApi: {},
  tableAttributesApi: {},
  targetsApi: {},
  userDefinedAttributesApi: {},
}));

import {
  useTenantMe,
  useProjects,
  useProject,
  useConnections,
  useModels,
  useModel,
  useSources,
  useFieldCompatibility,
  useAIOptimizerRuns,
  useNamedQueries,
  useNamedQueryCaps,
} from "./hooks";
import { aiOptimizerApi, fieldCompatibilityApi } from "./client";

function createWrapper() {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return ({ children }: { children: React.ReactNode }) =>
    React.createElement(QueryClientProvider, { client: qc }, children);
}

describe("React Query hooks", () => {
  describe("useTenantMe", () => {
    it("fetches current tenant", async () => {
      const { result } = renderHook(() => useTenantMe(), {
        wrapper: createWrapper(),
      });
      await waitFor(() => expect(result.current.isSuccess).toBe(true));
      expect(result.current.data).toEqual({
        id: "t-1",
        slug: "acme",
        name: "Acme Corp",
      });
    });
  });

  describe("useProjects", () => {
    it("fetches project list", async () => {
      const { result } = renderHook(() => useProjects(), {
        wrapper: createWrapper(),
      });
      await waitFor(() => expect(result.current.isSuccess).toBe(true));
      expect(result.current.data).toHaveLength(1);
      expect(result.current.data![0].name).toBe("Project 1");
    });
  });

  describe("useProject", () => {
    it("fetches a single project by ID", async () => {
      const { result } = renderHook(() => useProject("p-1"), {
        wrapper: createWrapper(),
      });
      await waitFor(() => expect(result.current.isSuccess).toBe(true));
      expect(result.current.data!.id).toBe("p-1");
    });

    it("is disabled when projectId is empty", () => {
      const { result } = renderHook(() => useProject(""), {
        wrapper: createWrapper(),
      });
      expect(result.current.fetchStatus).toBe("idle");
    });
  });

  describe("useConnections", () => {
    it("fetches connections for a project", async () => {
      const { result } = renderHook(() => useConnections("p-1"), {
        wrapper: createWrapper(),
      });
      await waitFor(() => expect(result.current.isSuccess).toBe(true));
      expect(result.current.data).toHaveLength(1);
    });

    it("is disabled when projectId is empty", () => {
      const { result } = renderHook(() => useConnections(""), {
        wrapper: createWrapper(),
      });
      expect(result.current.fetchStatus).toBe("idle");
    });
  });

  describe("useModels", () => {
    it("fetches models for a project", async () => {
      const { result } = renderHook(() => useModels("p-1"), {
        wrapper: createWrapper(),
      });
      await waitFor(() => expect(result.current.isSuccess).toBe(true));
      expect(result.current.data![0].name).toBe("Model X");
    });
  });

  describe("useModel", () => {
    it("fetches a single model", async () => {
      const { result } = renderHook(() => useModel("p-1", "m-1"), {
        wrapper: createWrapper(),
      });
      await waitFor(() => expect(result.current.isSuccess).toBe(true));
      expect(result.current.data!.id).toBe("m-1");
    });

    it("is disabled when modelId is empty", () => {
      const { result } = renderHook(() => useModel("p-1", ""), {
        wrapper: createWrapper(),
      });
      expect(result.current.fetchStatus).toBe("idle");
    });

    it("is disabled when projectId is empty", () => {
      const { result } = renderHook(() => useModel("", "m-1"), {
        wrapper: createWrapper(),
      });
      expect(result.current.fetchStatus).toBe("idle");
    });
  });

  describe("useSources", () => {
    it("fetches sources for a model", async () => {
      const { result } = renderHook(() => useSources("p-1", "m-1"), {
        wrapper: createWrapper(),
      });
      await waitFor(() => expect(result.current.isSuccess).toBe(true));
      expect(result.current.data).toHaveLength(1);
    });

    it("is disabled when both IDs are empty", () => {
      const { result } = renderHook(() => useSources("", ""), {
        wrapper: createWrapper(),
      });
      expect(result.current.fetchStatus).toBe("idle");
    });
  });

  describe("useFieldCompatibility", () => {
    afterEach(() => {
      vi.mocked(fieldCompatibilityApi.get).mockClear();
    });

    it("requests selected measures and all candidate dimensions for persona-scoped evaluation", async () => {
      const { result } = renderHook(
        () => useFieldCompatibility(
          "p-1",
          "m-1",
          "persona-1",
          ["measure-b", "measure-a", "measure-a"],
          ["dim-z", "dim-a", "dim-a"],
        ),
        { wrapper: createWrapper() },
      );

      await waitFor(() => expect(result.current.isSuccess).toBe(true));
      expect(fieldCompatibilityApi.get).toHaveBeenCalledWith(
        "p-1",
        "m-1",
        expect.objectContaining({
          personaId: "persona-1",
          measureIds: ["measure-a", "measure-b"],
          dimensionIds: ["dim-a", "dim-z"],
        }),
      );
    });

    it("is disabled until at least one real measure is selected", () => {
      const { result } = renderHook(
        () => useFieldCompatibility("p-1", "m-1", null, [], ["dim-a"]),
        { wrapper: createWrapper() },
      );
      expect(result.current.fetchStatus).toBe("idle");
    });
  });

  // F-030-06: a caller that lacks a tenant id in scope (ModelHealthPanel passes
  // "") must still surface AI optimiser runs by falling back to the persisted
  // auth-context tenant id, instead of silently disabling the query.
  // Bug-6558: tenant_id is resolved from the JWT server-side; the API call
  // only sends model_id.
  describe("useAIOptimizerRuns", () => {
    afterEach(() => {
      window.localStorage.clear();
      vi.mocked(aiOptimizerApi.listRuns).mockClear();
    });

    it("fetches with an explicit tenant id", async () => {
      const { result } = renderHook(() => useAIOptimizerRuns("acme", "m-1"), {
        wrapper: createWrapper(),
      });
      await waitFor(() => expect(result.current.isSuccess).toBe(true));
      expect(result.current.data).toHaveLength(1);
      expect(aiOptimizerApi.listRuns).toHaveBeenCalledWith("m-1");
    });

    it("falls back to the persisted tenant id when the caller passes an empty string", async () => {
      window.localStorage.setItem("tenant_id", "acme");
      const { result } = renderHook(() => useAIOptimizerRuns("", "m-1"), {
        wrapper: createWrapper(),
      });
      await waitFor(() => expect(result.current.isSuccess).toBe(true));
      expect(result.current.data).toHaveLength(1);
      expect(aiOptimizerApi.listRuns).toHaveBeenCalledWith("m-1");
    });

    it("stays disabled when neither the caller nor localStorage has a tenant id", () => {
      const { result } = renderHook(() => useAIOptimizerRuns("", "m-1"), {
        wrapper: createWrapper(),
      });
      expect(result.current.fetchStatus).toBe("idle");
    });
  });

  describe("useNamedQueries", () => {
    it("fetches the Named Query list", async () => {
      const { result } = renderHook(() => useNamedQueries("p-1", "m-1"), {
        wrapper: createWrapper(),
      });
      await waitFor(() => expect(result.current.data).toHaveLength(1));
      expect(result.current.data[0].name).toBe("top_cities");
    });

    it("memoises the joined data and the wrapper object (Bug-100 ref stability)", async () => {
      const { result, rerender } = renderHook(() => useNamedQueries("p-1", "m-1"), {
        wrapper: createWrapper(),
      });
      await waitFor(() => expect(result.current.data).toHaveLength(1));

      const dataRef = result.current.data;
      const wrapperRef = result.current;
      rerender();

      // Neither the data array nor the wrapper object is re-created on a
      // no-op render — consumers keying useMemo on them stay stable.
      expect(result.current.data).toBe(dataRef);
      expect(result.current).toBe(wrapperRef);
    });
  });

  describe("useNamedQueryCaps", () => {
    it("derives the effective system caps from project settings", async () => {
      const { result } = renderHook(() => useNamedQueryCaps("p-1"), {
        wrapper: createWrapper(),
      });
      await waitFor(() => expect(result.current.isLoading).toBe(false));
      expect(result.current.maxRows).toBe(100000);
      expect(result.current.maxColumns).toBe(200);
    });
  });
});
