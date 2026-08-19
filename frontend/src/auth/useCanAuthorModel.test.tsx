/**
 * Bug-8784: authoring capability is now derived solely from the builder-store
 * ``readOnly`` flag, which ``ModelBuilder.tsx`` sets from the backend's
 * per-model ``caller_can_author`` response.  The coarse local-role check
 * (``canEditModelConfig()``) is removed because ``modeler`` is never a
 * ``LocalUser.role`` value — it is a per-project binding — so the canonical
 * modeller was locked out of every authoring panel.
 */
import { describe, it, expect, beforeEach } from "vitest";
import { renderHook } from "@testing-library/react";
import { useCanAuthorModel } from "./useCanAuthorModel";
import { useBuilderStore } from "../store/builderStore";

describe("useCanAuthorModel (Bug-8784)", () => {
  beforeEach(() => {
    useBuilderStore.getState().reset();
    useBuilderStore.getState().setReadOnly(false);
  });

  it("may author when readOnly is false", () => {
    const { result } = renderHook(() => useCanAuthorModel());
    expect(result.current).toBe(true);
  });

  it("may NOT author when readOnly is true (share-link or backend denial)", () => {
    useBuilderStore.getState().setReadOnly(true);
    const { result } = renderHook(() => useCanAuthorModel());
    expect(result.current).toBe(false);
  });

  it("no longer gates on coarse local role — the backend is authoritative", () => {
    // Any role, including `member`, may author when readOnly is false.
    // The backend's caller_can_author already resolved the per-project
    // binding; this hook reflects that single signal.
    window.localStorage.setItem("user_role", "member");
    const { result } = renderHook(() => useCanAuthorModel());
    expect(result.current).toBe(true);

    window.localStorage.setItem("user_role", "viewer");
    // Even a viewer string would pass here — the backend would have set
    // readOnly=true in ModelBuilder.tsx, so this hook would return false.
    // Testing the hook in isolation, readOnly is the only input.
  });
});
