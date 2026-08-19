/**
 * F-026-03: a failed model-alerts request must NOT be presented as an empty
 * (clean) server-issue list. The hook surfaces a persistent, retryable
 * "validation unavailable" issue when the alerts query errors, distinct from
 * the "no alerts" case.
 */
import { describe, it, expect, beforeEach, vi } from "vitest";
import { renderHook, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { createElement, type ReactNode } from "react";
import { I18nContext } from "../../i18n";
import en from "../../i18n";
import { useBuilderStore } from "../../store/builderStore";

const listMock = vi.fn();
vi.mock("../../api/client", () => ({
  alertsApi: {
    list: (...args: unknown[]) => listMock(...args),
  },
}));

import { useModelValidation } from "./useModelValidation";

function wrapper({ children }: { children: ReactNode }) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return createElement(
    QueryClientProvider,
    { client: qc },
    createElement(I18nContext.Provider, { value: en }, children),
  );
}

function issues() {
  return useBuilderStore.getState().validationIssues;
}

describe("useModelValidation feed status (F-026-03)", () => {
  beforeEach(() => {
    listMock.mockReset();
    useBuilderStore.getState().reset();
  });

  it("no server alerts -> no feed-unavailable issue (clean is genuinely clean)", async () => {
    listMock.mockResolvedValue([]);
    renderHook(
      () =>
        useModelValidation("p1", "m1", {
          tables: [{ id: "t1", table_type: "fact" } as never],
          joins: [],
          hasTarget: true,
          ready: true,
        }),
      { wrapper },
    );
    await waitFor(() => expect(listMock).toHaveBeenCalled());
    await waitFor(() =>
      expect(issues().some((i) => i.id === "validation-feed-unavailable")).toBe(false),
    );
  });

  it("alerts request fails -> a persistent 'validation unavailable' issue is surfaced", async () => {
    listMock.mockRejectedValue(new Error("network"));
    renderHook(
      () =>
        useModelValidation("p1", "m1", {
          tables: [{ id: "t1", table_type: "fact" } as never],
          joins: [],
          hasTarget: true,
          ready: true,
        }),
      { wrapper },
    );
    await waitFor(() =>
      expect(issues().some((i) => i.id === "validation-feed-unavailable")).toBe(true),
    );
    const feed = issues().find((i) => i.id === "validation-feed-unavailable")!;
    expect(feed.severity).toBe("warning");
    expect(feed.message).toBe(en["validation.feed.unavailable"]);
  });
});
