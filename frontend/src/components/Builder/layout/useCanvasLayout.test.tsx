/**
 * Apply-guard tests for the canvas layout orchestration (R11).
 *
 * The transport is stubbed so each ordering can be produced deterministically;
 * the behaviour under test is which results the canvas is allowed to apply.
 *
 * Every test commits an edit (via its own `act`) *before* delivering the worker
 * reply, which is the ordering the guard exists to defend: an edit the user has
 * already made must not be overwritten by a result computed before it.
 */
import { act, renderHook } from "@testing-library/react";
import React from "react";
import { describe, expect, it } from "vitest";
import { LayoutError, type LayoutClientLike } from "./layoutClient";
import { useCanvasLayout, type SignatureNode, type UseCanvasLayoutOptions, layoutSignature, layoutStabilitySignature } from "./useCanvasLayout";
import type { LayoutOperation, LayoutResult, LayoutSnapshot } from "./types";

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (error: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

class StubClient implements LayoutClientLike {
  requests: Array<{ snapshot: LayoutSnapshot; operation: LayoutOperation }> = [];
  deferreds: Array<ReturnType<typeof deferred<LayoutResult>>> = [];
  cancelCount = 0;
  disposeCount = 0;
  private pending = 0;

  get busy(): boolean {
    return this.pending > 0;
  }

  request(snapshot: LayoutSnapshot, operation: LayoutOperation): Promise<LayoutResult> {
    const control = deferred<LayoutResult>();
    this.requests.push({ snapshot, operation });
    this.deferreds.push(control);
    this.pending += 1;
    return control.promise.finally(() => {
      this.pending -= 1;
    });
  }

  cancel(): void {
    this.cancelCount += 1;
  }

  dispose(): void {
    this.disposeCount += 1;
  }
}

function successFor(snapshot: LayoutSnapshot, positions: Record<string, { x: number; y: number }> = {}): LayoutResult {
  return {
    kind: "success",
    scope: snapshot.scope,
    revision: snapshot.revision,
    positions,
    routes: {},
    options: snapshot.options,
    metrics: { nodeOverlapCount: 0, throughNodeSegmentCount: 0, edgeCrossingCount: 0, totalBends: 0, totalLength: 0, elapsedMs: 1 },
  };
}

const baseNodes: SignatureNode[] = [
  { id: "a", position: { x: 0, y: 0 }, width: 100, height: 100 },
  { id: "b", position: { x: 300, y: 0 }, width: 100, height: 100 },
];

type EdgeProp = UseCanvasLayoutOptions["edges"];
type HookView = {
  result: {
    current: {
      run: (
        operation: LayoutOperation,
        options?: Record<string, unknown>,
        context?: { movableIds?: Set<string> },
      ) => Promise<{ applied: boolean; failure?: string }>;
      cancel: () => void;
      busy: boolean;
      canRetry: boolean;
    };
  };
};

function setup(overrides: Partial<UseCanvasLayoutOptions> = {}) {
  const client = new StubClient();
  const applied: LayoutResult[] = [];
  const errors: string[] = [];
  const baseProps: UseCanvasLayoutOptions = {
    projectId: "p1",
    modelId: "m1",
    nodes: baseNodes,
    edges: [],
    readOnly: false,
    buildSnapshot: (revision, options): LayoutSnapshot => ({
      scope: { projectId: "p1", modelId: "m1" },
      revision,
      nodes: [],
      edges: [],
      options: {
        preset: options.preset ?? "hierarchical",
        direction: options.direction ?? "DOWN",
        spacing: options.spacing ?? "normal",
      },
    }),
    applyResult: (result: LayoutResult) => applied.push(result),
    onError: (message: string) => errors.push(message),
    createClient: () => client,
    ...overrides,
  };
  const view = renderHook((props: UseCanvasLayoutOptions) => useCanvasLayout(props), { initialProps: baseProps });
  return { view, client, applied, errors, baseProps };
}

/** Start a batch and return its outcome promise, without awaiting it yet. */
function startBatch(view: HookView, operation: LayoutOperation = "arrange-all"): Promise<{ applied: boolean; failure?: string }> {
  let outcome!: Promise<{ applied: boolean; failure?: string }>;
  act(() => {
    outcome = view.result.current.run(operation, {});
  });
  return outcome;
}

describe("useCanvasLayout apply guards", () => {
  it("applies a current, permitted result", async () => {
    const { view, client, applied } = setup();
    const outcome = startBatch(view);
    await act(async () => {
      client.deferreds[0].resolve(successFor(client.requests[0].snapshot, { a: { x: 5, y: 5 } }));
      expect((await outcome).applied).toBe(true);
    });
    expect(applied).toHaveLength(1);
    expect(applied[0].positions.a).toEqual({ x: 5, y: 5 });
  });

  it("refuses to start in a read-only session", async () => {
    const { view, client, applied, errors } = setup({ readOnly: true });
    let outcome!: { applied: boolean; failure?: string };
    await act(async () => {
      outcome = await view.result.current.run("arrange-all", {});
    });
    expect(outcome.applied).toBe(false);
    expect(client.requests).toHaveLength(0);
    expect(applied).toHaveLength(0);
    expect(errors[0]).toMatch(/read-only/);
  });

  it("refuses a result computed before a table moved", async () => {
    const { view, client, applied, baseProps } = setup();
    const outcome = startBatch(view);
    // The user drags a card, and that edit is committed before the reply lands.
    act(() => {
      view.rerender({ ...baseProps, nodes: [{ ...baseNodes[0], position: { x: 999, y: 0 } }, baseNodes[1]] });
    });
    await act(async () => {
      client.deferreds[0].resolve(successFor(client.requests[0].snapshot));
      expect((await outcome).applied).toBe(false);
    });
    expect(applied).toHaveLength(0);
  });

  it("refuses a result computed before a manual route changed", async () => {
    const withWaypoints = (waypoints: Array<{ x: number; y: number }>): EdgeProp => [
      { id: "j1", source: "a", target: "b", data: { waypoints, pathing: "orthogonal" } },
    ];
    const { view, client, applied, baseProps } = setup({ edges: withWaypoints([]) });
    const outcome = startBatch(view, "reroute-links");
    act(() => {
      view.rerender({ ...baseProps, edges: withWaypoints([{ x: 10, y: 10 }]) });
    });
    await act(async () => {
      client.deferreds[0].resolve(successFor(client.requests[0].snapshot));
      expect((await outcome).applied).toBe(false);
    });
    expect(applied).toHaveLength(0);
  });

  it("refuses a result that arrives after a model switch", async () => {
    const { view, client, applied, baseProps } = setup();
    const outcome = startBatch(view);
    act(() => {
      view.rerender({ ...baseProps, modelId: "m2" });
    });
    await act(async () => {
      client.deferreds[0].resolve(successFor(client.requests[0].snapshot));
      expect((await outcome).applied).toBe(false);
    });
    expect(applied).toHaveLength(0);
  });

  it("stays silent on cancellation but reports a real failure and allows retry", async () => {
    const { view, client, errors } = setup();
    const cancelled = startBatch(view);
    await act(async () => {
      client.deferreds[0].reject(new LayoutError("cancelled", "user cancelled the layout"));
      expect((await cancelled).applied).toBe(false);
    });
    expect(errors).toHaveLength(0);
    expect(view.result.current.canRetry).toBe(false);

    const failed = startBatch(view);
    await act(async () => {
      client.deferreds[1].reject(new LayoutError("geometry-invalid", "layout contains overlapping table cards"));
      expect((await failed).applied).toBe(false);
    });
    expect(errors).toEqual(["layout contains overlapping table cards"]);
    expect(view.result.current.canRetry).toBe(true);

    let retried: boolean | undefined;
    act(() => {
      void view.result.current.retry().then((value) => {
        retried = value;
      });
    });
    await act(async () => {
      client.deferreds[2].resolve(successFor(client.requests[2].snapshot));
    });
    expect(retried.applied).toBe(true);
    expect(client.requests[2].operation).toBe("arrange-all");
  });

  it("passes the operation and options through to the worker request", async () => {
    const { view, client } = setup();
    let outcome!: Promise<boolean>;
    act(() => {
      outcome = view.result.current.run("arrange-selected", { preset: "compact", spacing: "dense" });
    });
    await act(async () => {
      client.deferreds[0].resolve(successFor(client.requests[0].snapshot));
      await outcome;
    });
    expect(client.requests[0].operation).toBe("arrange-selected");
    expect(client.requests[0].snapshot.options).toMatchObject({ preset: "compact", spacing: "dense" });
  });

  it("passes the transient movable set through to the snapshot builder", async () => {
    const seen: Array<Set<string> | undefined> = [];
    const { view, client } = setup({
      buildSnapshot: (revision, options, context): LayoutSnapshot => {
        seen.push(context.movableIds);
        return {
          scope: { projectId: "p1", modelId: "m1" },
          revision,
          nodes: [],
          edges: [],
          options: {
            preset: options.preset ?? "hierarchical",
            direction: options.direction ?? "DOWN",
            spacing: options.spacing ?? "normal",
          },
        };
      },
    });

    let outcome!: Promise<boolean>;
    act(() => {
      outcome = view.result.current.run("arrange-all", {}, { movableIds: new Set(["b"]) });
    });
    await act(async () => {
      client.deferreds[0].resolve(successFor(client.requests[0].snapshot));
      await outcome;
    });

    expect(seen).toHaveLength(1);
    expect([...(seen[0] ?? [])]).toEqual(["b"]);
  });

  it("exposes a cancel action to the transport", async () => {
    const { view, client } = setup();
    const outcome = startBatch(view);
    act(() => {
      view.result.current.cancel();
    });
    expect(client.cancelCount).toBe(1);
    expect(client.disposeCount).toBe(0);
    // The stub transport still owes a rejection for the abandoned batch.
    await act(async () => {
      client.deferreds[0].reject(new LayoutError("cancelled", "user cancelled the layout"));
      expect((await outcome).applied).toBe(false);
    });
  });

  it("allows exactly one automatic recomputation when only card geometry settles", async () => {
    const { view, client, applied, baseProps } = setup();
    const outcome = startBatch(view);
    // Passive measurement settling: same positions, larger resolved width.
    act(() => {
      view.rerender({ ...baseProps, nodes: baseNodes.map((node) => ({ ...node, width: 120 })) });
    });
    await act(async () => {
      client.deferreds[0].resolve(successFor(client.requests[0].snapshot));
    });
    // The loop re-submitted against the updated snapshot; deliver the fresh reply.
    expect(client.requests).toHaveLength(2);
    await act(async () => {
      client.deferreds[1].resolve(successFor(client.requests[1].snapshot));
      expect((await outcome).applied).toBe(true);
    });
    expect(applied).toHaveLength(1);
  });

  it("fails visibly when card geometry changes again during the recomputation", async () => {
    const { view, client, applied, errors, baseProps } = setup();
    const outcome = startBatch(view);
    act(() => {
      view.rerender({ ...baseProps, nodes: baseNodes.map((node) => ({ ...node, width: 120 })) });
    });
    await act(async () => {
      client.deferreds[0].resolve(successFor(client.requests[0].snapshot));
    });
    // Geometry churns again while the single recomputation runs.
    act(() => {
      view.rerender({ ...baseProps, nodes: baseNodes.map((node) => ({ ...node, width: 140 })) });
    });
    await act(async () => {
      client.deferreds[1].resolve(successFor(client.requests[1].snapshot));
      expect((await outcome).applied).toBe(false);
    });
    expect(applied).toHaveLength(0);
    expect(errors).toEqual(["layout cancelled: card geometry kept changing. Please try again."]);
    expect(view.result.current.canRetry).toBe(true);
  });

  it("supersedes without restarting when the user moves a card", async () => {
    const { view, client, applied, baseProps } = setup();
    const outcome = startBatch(view);
    act(() => {
      view.rerender({
        ...baseProps,
        nodes: [{ ...baseNodes[0], position: { x: 10, y: 0 } }, baseNodes[1]],
      });
    });
    await act(async () => {
      client.deferreds[0].resolve(successFor(client.requests[0].snapshot));
      expect((await outcome).applied).toBe(false);
    });
    expect(applied).toHaveLength(0);
    expect(client.requests).toHaveLength(1);
  });

  // R2-05 (external review): every operation shared one set of completion-side
  // effects, so an OLDER request finishing after a newer one started would
  // apply its stale result, clear the newer one's busy state, surface its own
  // error over the newer work, and leave Retry pointing at the superseded
  // operation. Cancelling the request is not enough — the completion-side
  // effects have to be ignored too.
  describe("a superseded run owns nothing", () => {
    it("does not apply its result after a newer run has started", async () => {
      const { view, client, applied } = setup();
      const first = startBatch(view);
      const second = startBatch(view);
      expect(client.deferreds).toHaveLength(2);

      // The OLDER request answers first.
      await act(async () => {
        client.deferreds[0].resolve(successFor(client.requests[0].snapshot, { a: { x: 11, y: 11 } }));
        await first;
      });
      expect(applied, "a superseded run must not touch the canvas").toHaveLength(0);

      // The live run still applies normally.
      await act(async () => {
        client.deferreds[1].resolve(successFor(client.requests[1].snapshot, { a: { x: 22, y: 22 } }));
        await second;
      });
      expect(applied).toHaveLength(1);
      expect(applied[0].positions).toEqual({ a: { x: 22, y: 22 } });
    });

    it("does not clear the live run's busy state when it finishes", async () => {
      const { view, client } = setup();
      const first = startBatch(view);
      const second = startBatch(view);

      await act(async () => {
        client.deferreds[0].resolve(successFor(client.requests[0].snapshot));
        await first;
      });
      expect(view.result.current.busy, "the newer run is still working").toBe(true);

      await act(async () => {
        client.deferreds[1].resolve(successFor(client.requests[1].snapshot));
        await second;
      });
      expect(view.result.current.busy).toBe(false);
    });

    it("does not surface its own failure over the newer work", async () => {
      const { view, client, errors } = setup();
      const first = startBatch(view);
      const second = startBatch(view);

      await act(async () => {
        client.deferreds[0].reject(new Error("the superseded batch failed"));
        await first;
      });
      expect(errors, "a superseded failure is not the user's current problem").toEqual([]);
      expect(view.result.current.canRetry).toBe(false);
      expect(view.result.current.busy).toBe(true);

      await act(async () => {
        client.deferreds[1].resolve(successFor(client.requests[1].snapshot));
        await second;
      });
      expect(errors).toEqual([]);
    });

    it("is retired by Cancel, so a reply already in flight cannot apply", async () => {
      const { view, client, applied } = setup();
      const outcome = startBatch(view);

      act(() => {
        view.result.current.cancel();
      });
      expect(client.cancelCount).toBe(1);

      await act(async () => {
        client.deferreds[0].resolve(successFor(client.requests[0].snapshot, { a: { x: 9, y: 9 } }));
        await outcome;
      });
      expect(applied, "a cancelled run must not apply a late reply").toHaveLength(0);
      expect(view.result.current.busy).toBe(false);
    });
  });

  it("keeps the layout client usable under React StrictMode (F01)", async () => {
    const clients: StubClient[] = [];
    const applied: LayoutResult[] = [];
    const errors: string[] = [];
    const baseProps: UseCanvasLayoutOptions = {
      projectId: "p1",
      modelId: "m1",
      nodes: baseNodes,
      edges: [],
      readOnly: false,
      buildSnapshot: (revision, options): LayoutSnapshot => ({
        scope: { projectId: "p1", modelId: "m1" },
        revision,
        nodes: [],
        edges: [],
        options: {
          preset: options.preset ?? "hierarchical",
          direction: options.direction ?? "DOWN",
          spacing: options.spacing ?? "normal",
        },
      }),
      applyResult: (result: LayoutResult) => applied.push(result),
      onError: (message: string) => errors.push(message),
      createClient: () => {
        const client = new StubClient();
        clients.push(client);
        return client;
      },
    };
    // StrictMode's development-only effect replay runs setup → cleanup → setup.
    const view = renderHook((props: UseCanvasLayoutOptions) => useCanvasLayout(props), {
      initialProps: baseProps,
      wrapper: ({ children }) => React.createElement(React.StrictMode, null, children),
    });

    // The first client was disposed by the replay cleanup; the second is active.
    expect(clients.length).toBeGreaterThanOrEqual(2);
    expect(clients[0].disposeCount).toBe(1);
    const active = clients[clients.length - 1];
    expect(active.disposeCount).toBe(0);

    // Arrange must start on the active client and apply exactly once.
    let outcome!: Promise<boolean>;
    act(() => {
      outcome = view.result.current.run("arrange-all", {});
    });
    expect(active.requests).toHaveLength(1);
    await act(async () => {
      active.deferreds[0].resolve(successFor(active.requests[0].snapshot, { a: { x: 5, y: 5 } }));
      expect((await outcome).applied).toBe(true);
    });
    expect(applied).toHaveLength(1);
    expect(errors).toHaveLength(0);
  });
});

describe("staleness signature covers protection, not only coordinates", () => {
  // Reported by the external review. A signature that tracks only geometry lets
  // this sequence through: unlock a table's position, start Arrange, then Undo
  // the unlock before the result lands. The card is protected again, but the
  // signature never changed, so the result computed while it was movable still
  // applies — and moves the card the user just re-protected.
  const node = (id: string, extra: Record<string, unknown> = {}) => ({
    id, position: { x: 0, y: 0 }, width: 200, height: 100, ...extra,
  });
  const edge = (id: string, data: Record<string, unknown> = {}) => ({
    id, source: "a", target: "b", data,
  });

  it("changes when a table's position lock changes", () => {
    const before = layoutSignature([node("a", { data: { pinned: false } })], []);
    const after = layoutSignature([node("a", { data: { pinned: true } })], []);
    expect(after).not.toBe(before);
  });

  it("changes when a route lock changes", () => {
    const before = layoutSignature([], [edge("j", { locked: false })]);
    const after = layoutSignature([], [edge("j", { locked: true })]);
    expect(after).not.toBe(before);
  });

  it("changes when a marker extent moves the docking heels", () => {
    const before = layoutSignature([], [edge("j", { sourceMarkerExtent: 0 })]);
    const after = layoutSignature([], [edge("j", { sourceMarkerExtent: 12 })]);
    expect(after).not.toBe(before);
  });

  it("supersedes an in-flight batch when protection changes", () => {
    // The stability signature is the one that decides whether a running batch
    // is still about the current problem, so protection has to reach it too.
    const before = layoutStabilitySignature([node("a", { data: { pinned: false } })], []);
    const after = layoutStabilitySignature([node("a", { data: { pinned: true } })], []);
    expect(after).not.toBe(before);
  });

  it("is unchanged by something that does not affect the layout", () => {
    const before = layoutSignature([node("a", { data: { pinned: true, selected: false } })], []);
    const after = layoutSignature([node("a", { data: { pinned: true, selected: true } })], []);
    expect(after).toBe(before);
  });
});
