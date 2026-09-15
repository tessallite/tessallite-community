/**
 * Transport tests for the layout worker client.
 *
 * The worker *thread* is stubbed here on purpose: what is under test is the
 * client's ordering, timeout, staleness and disposal behaviour (R11), not the
 * engine, which `layoutEngine.test.ts` already exercises for real.
 */
import { afterEach, describe, expect, it, vi } from "vitest";
import { LayoutClient, LayoutError, type LayoutWorkerLike } from "./layoutClient";
import type { LayoutResult, LayoutSnapshot } from "./types";

class StubWorker implements LayoutWorkerLike {
  onmessage: ((event: { data: unknown }) => void) | null = null;
  onerror: ((event: unknown) => void) | null = null;
  posted: Array<{ type: string; operation?: string; requestId: string; snapshot?: LayoutSnapshot }> = [];
  terminated = false;

  postMessage(message: unknown): void {
    this.posted.push(message as StubWorker["posted"][number]);
  }

  terminate(): void {
    this.terminated = true;
  }

  emit(data: unknown): void {
    this.onmessage?.({ data });
  }

  lastRequestId(): string {
    return this.posted[this.posted.length - 1].requestId;
  }
}

function snapshot(revision = 1): LayoutSnapshot {
  return {
    scope: { projectId: "p1", modelId: "m1" },
    revision,
    nodes: [],
    edges: [],
    options: { preset: "hierarchical", direction: "DOWN", spacing: "normal" },
  };
}

function successResult(input: LayoutSnapshot): LayoutResult {
  return {
    kind: "success",
    scope: input.scope,
    revision: input.revision,
    positions: {},
    routes: {},
    options: input.options,
    metrics: { nodeOverlapCount: 0, throughNodeSegmentCount: 0, edgeCrossingCount: 0, totalBends: 0, totalLength: 0, elapsedMs: 1 },
  };
}

/** Observe a promise immediately so an expected rejection is never unhandled. */
function settled<T>(promise: Promise<T>): Promise<T | LayoutError> {
  return promise.then(
    (value) => value,
    (error: unknown) => error as LayoutError,
  );
}

function harness() {
  const workers: StubWorker[] = [];
  const client = new LayoutClient({
    createWorker: () => {
      const worker = new StubWorker();
      workers.push(worker);
      return worker;
    },
  });
  return { client, workers };
}

afterEach(() => {
  vi.useRealTimers();
});

describe("LayoutClient", () => {
  it("posts the batch and resolves the worker's result", async () => {
    const { client, workers } = harness();
    const input = snapshot();
    const promise = client.request(input, "arrange-all");

    expect(workers).toHaveLength(1);
    expect(workers[0].posted[0]).toMatchObject({ type: "layout", operation: "arrange-all" });
    expect(client.busy).toBe(true);

    const requestId = workers[0].lastRequestId();
    workers[0].emit({ ...successResult(input), requestId });

    await expect(promise).resolves.toMatchObject({ kind: "success" });
    expect(client.busy).toBe(false);
  });

  it("fails the batch on timeout and terminates the worker", async () => {
    vi.useFakeTimers();
    const { client, workers } = harness();
    const outcome = settled(client.request(snapshot(), "arrange-all"));

    await vi.advanceTimersByTimeAsync(20_000);

    const error = await outcome;
    expect(error).toBeInstanceOf(LayoutError);
    expect(error).toMatchObject({ code: "timeout" });
    expect(workers[0].terminated).toBe(true);
    expect(client.busy).toBe(false);
  });

  it("ignores a late reply that arrives after the timeout", async () => {
    vi.useFakeTimers();
    const { client, workers } = harness();
    const input = snapshot();
    const first = settled(client.request(input, "arrange-all"));
    const staleWorker = workers[0];
    const staleRequestId = staleWorker.lastRequestId();

    await vi.advanceTimersByTimeAsync(20_000);
    expect(await first).toMatchObject({ code: "timeout" });

    // The engine finally answers, but its batch is already abandoned.
    staleWorker.emit({ ...successResult(input), requestId: staleRequestId });

    // A fresh request still works, on a new worker.
    vi.useRealTimers();
    const second = client.request(snapshot(2), "arrange-all");
    expect(workers).toHaveLength(2);
    workers[1].emit({ ...successResult(snapshot(2)), requestId: workers[1].lastRequestId() });
    await expect(second).resolves.toMatchObject({ kind: "success" });
  });

  it("supersedes an active batch and never applies the superseded reply", async () => {
    const { client, workers } = harness();
    const first = settled(client.request(snapshot(1), "arrange-all"));
    const supersededWorker = workers[0];
    const supersededRequestId = supersededWorker.lastRequestId();

    const second = client.request(snapshot(2), "arrange-all");

    expect(await first).toMatchObject({ code: "cancelled" });
    expect(supersededWorker.terminated).toBe(true);
    expect(workers).toHaveLength(2);

    // The superseded worker's late answer must not resolve the live request.
    supersededWorker.emit({ ...successResult(snapshot(1)), requestId: supersededRequestId });
    expect(client.busy).toBe(true);

    workers[1].emit({ ...successResult(snapshot(2)), requestId: workers[1].lastRequestId() });
    await expect(second).resolves.toMatchObject({ revision: 2 });
  });

  it("rejects a result whose revision is not the requested one", async () => {
    const { client, workers } = harness();
    const promise = client.request(snapshot(7), "arrange-all");
    workers[0].emit({ ...successResult(snapshot(6)), requestId: workers[0].lastRequestId() });
    // `superseded`, not `unknown`: the canvas keeps the user's edit for an
    // engine fault and discards it for a geometry fault, and an abandoned
    // request is neither. Filing this under `unknown` put it in the branch that
    // keeps and SAVES a candidate nobody validated.
    await expect(promise).rejects.toMatchObject({ code: "superseded" });
    await expect(promise).rejects.toThrow(/older revision/);
  });

  it("rejects a result for another model", async () => {
    const { client, workers } = harness();
    const input = snapshot(3);
    const promise = client.request(input, "reroute-links");
    const foreign = { ...successResult(input), scope: { projectId: "p1", modelId: "other" } };
    workers[0].emit({ ...foreign, requestId: workers[0].lastRequestId() });
    await expect(promise).rejects.toThrow(/different model/);
  });

  it("surfaces a worker failure result as a typed error", async () => {
    const { client, workers } = harness();
    const input = snapshot();
    const promise = client.request(input, "arrange-all");
    workers[0].emit({ kind: "failure", scope: input.scope, revision: input.revision, code: "engine-unavailable", message: "no wasm", requestId: workers[0].lastRequestId() });
    await expect(promise).rejects.toMatchObject({ code: "engine-unavailable", message: "no wasm" });
  });

  it("cancels an active batch and refuses new work after disposal", async () => {
    const { client, workers } = harness();
    const promise = settled(client.request(snapshot(), "arrange-all"));
    client.cancel("user pressed cancel");
    expect(await promise).toMatchObject({ code: "cancelled" });
    expect(workers[0].terminated).toBe(true);

    const second = settled(client.request(snapshot(), "arrange-all"));
    client.dispose();
    expect(await second).toMatchObject({ code: "cancelled" });
    await expect(client.request(snapshot(), "arrange-all")).rejects.toThrow(/disposed/);
  });
});
