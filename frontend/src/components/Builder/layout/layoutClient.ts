/**
 * Typed client for the canvas layout worker.
 *
 * Owns the transport concerns the renderer must not: lazy worker creation, one
 * active batch with at most one coalesced successor, a hard timeout, and
 * rejection of late replies. A timed-out or cancelled batch terminates the
 * worker, because the expensive work is inside the engine and cannot be
 * interrupted cooperatively.
 */
import config from "./config.json";
import type {
  LayoutFailure,
  LayoutFailureCode,
  LayoutOperation,
  LayoutResult,
  LayoutSnapshot,
} from "./types";
// One error class for the whole layout path. Re-exported because the canvas and
// its tests have always reached for it here.
import { LayoutError } from "./layoutErrors";

export { LayoutError };

/** Minimal surface the client needs, so tests can supply a deterministic transport. */
export interface LayoutWorkerLike {
  postMessage(message: unknown): void;
  terminate(): void;
  onmessage: ((event: { data: unknown }) => void) | null;
  onerror?: ((event: unknown) => void) | null;
}

export interface LayoutClientOptions {
  createWorker?: () => LayoutWorkerLike;
  timeoutMs?: number;
}


/** Behaviour the canvas orchestration depends on, so tests can inject a stub. */
export interface LayoutClientLike {
  request(snapshot: LayoutSnapshot, operation: LayoutOperation): Promise<LayoutResult>;
  cancel(reason?: string): void;
  dispose(): void;
  readonly busy: boolean;
}

interface Pending {
  requestId: string;
  snapshot: LayoutSnapshot;
  resolve: (result: LayoutResult) => void;
  reject: (error: LayoutError) => void;
  timer: ReturnType<typeof setTimeout>;
}

function defaultWorkerFactory(): LayoutWorkerLike {
  return new Worker(new URL("./layout.worker.ts", import.meta.url), { type: "module" }) as unknown as LayoutWorkerLike;
}

function failureOf(snapshot: LayoutSnapshot, code: LayoutFailureCode, message: string): LayoutFailure {
  return { kind: "failure", scope: snapshot.scope, revision: snapshot.revision, code, message };
}

export class LayoutClient implements LayoutClientLike {
  private readonly createWorker: () => LayoutWorkerLike;
  private readonly timeoutMs: number;
  private worker: LayoutWorkerLike | null = null;
  private pending: Pending | null = null;
  private sequence = 0;
  /** Bumped whenever the worker is replaced, so replies from the old one are dropped. */
  private generation = 0;
  private disposed = false;

  constructor(options: LayoutClientOptions = {}) {
    this.createWorker = options.createWorker ?? defaultWorkerFactory;
    this.timeoutMs = options.timeoutMs ?? Number(config.worker.timeoutMs);
  }

  get busy(): boolean {
    return this.pending !== null;
  }

  /** Run one layout batch. A second call supersedes the first. */
  request(snapshot: LayoutSnapshot, operation: LayoutOperation): Promise<LayoutResult> {
    if (this.disposed) {
      return Promise.reject(new LayoutError("cancelled", "the canvas layout session has been disposed"));
    }
    // At most one active batch plus one latest request: the superseded batch is
    // rejected and the worker is replaced, so its late reply cannot be applied.
    this.cancelPending("superseded by a newer layout request");
    this.terminateWorker();

    const requestId = `layout-${++this.sequence}`;
    const worker = this.ensureWorker();
    return new Promise<LayoutResult>((resolve, reject) => {
      const timer = setTimeout(() => {
        this.fail(requestId, failureOf(snapshot, "timeout", `layout did not finish within ${this.timeoutMs}ms`));
        // The engines cannot be interrupted cooperatively, so a timed-out batch
        // is abandoned by replacing the worker rather than by waiting for it.
        this.terminateWorker();
      }, this.timeoutMs);
      this.pending = { requestId, snapshot, resolve, reject, timer };
      worker.postMessage({ type: "layout", operation, requestId, snapshot });
    });
  }

  /**
   * Abandon the active batch. The worker is terminated as well: the engines
   * cannot be interrupted mid-transaction, and a new worker is created lazily
   * on the next request.
   */
  cancel(reason = "layout was cancelled"): void {
    this.cancelPending(reason);
    this.terminateWorker();
  }

  dispose(): void {
    this.disposed = true;
    this.cancelPending("the canvas layout session was disposed");
    this.terminateWorker();
  }

  private ensureWorker(): LayoutWorkerLike {
    if (this.worker) return this.worker;
    const generation = ++this.generation;
    let worker: LayoutWorkerLike;
    try {
      worker = this.createWorker();
    } catch (error) {
      // A worker that will not start is the engine being unavailable — it says
      // nothing about the geometry the user just produced. Letting this escape
      // untyped made it `unknown`, and the canvas discards on `unknown`, so
      // every gesture was rolled back in an environment without Worker support
      // instead of simply going unrepaired.
      throw new LayoutError(
        "engine-unavailable",
        `layout worker could not be started: ${error instanceof Error ? error.message : String(error)}`,
      );
    }
    worker.onmessage = (event) => this.handleMessage(generation, event.data);
    worker.onerror = (event) => {
      this.failPending(failureOf(this.pending?.snapshot ?? this.emptySnapshot(), "engine-unavailable", `layout worker failed: ${String(event)}`));
      this.terminateWorker();
    };
    this.worker = worker;
    return worker;
  }

  private handleMessage(generation: number, data: unknown): void {
    // A reply from a replaced worker is stale by construction.
    if (generation !== this.generation) return;
    const pending = this.pending;
    if (!pending) return; // late reply after cancel/timeout/dispose
    if (!data || typeof data !== "object") {
      this.fail(pending.requestId, failureOf(pending.snapshot, "engine-unavailable", "layout worker returned an unreadable result"));
      return;
    }
    const message = data as LayoutResult | LayoutFailure;
    if (message.kind === "failure") {
      this.fail(pending.requestId, { ...message, scope: pending.snapshot.scope, revision: pending.snapshot.revision });
      return;
    }
    clearTimeout(pending.timer);
    this.pending = null;
    // A result for a different scope or revision is never applied.
    if (message.scope.projectId !== pending.snapshot.scope.projectId || message.scope.modelId !== pending.snapshot.scope.modelId) {
      pending.reject(new LayoutError("superseded", "layout result belongs to a different model"));
      return;
    }
    if (message.revision !== pending.snapshot.revision) {
      pending.reject(new LayoutError("superseded", "layout result belongs to an older revision"));
      return;
    }
    pending.resolve(message);
  }

  private emptySnapshot(): LayoutSnapshot {
    return {
      scope: { projectId: "", modelId: "" },
      revision: -1,
      nodes: [],
      edges: [],
      options: { preset: "hierarchical", direction: "DOWN", spacing: "normal" },
    };
  }

  private fail(requestId: string, failure: LayoutFailure): void {
    const pending = this.pending;
    if (!pending || pending.requestId !== requestId) return;
    clearTimeout(pending.timer);
    this.pending = null;
    pending.reject(new LayoutError(failure.code, failure.message));
  }

  private failPending(failure: LayoutFailure): void {
    const pending = this.pending;
    if (!pending) return;
    clearTimeout(pending.timer);
    this.pending = null;
    pending.reject(new LayoutError(failure.code, failure.message));
  }

  private cancelPending(reason: string): void {
    const pending = this.pending;
    if (!pending) return;
    clearTimeout(pending.timer);
    this.pending = null;
    pending.reject(new LayoutError("cancelled", reason));
  }

  private terminateWorker(): void {
    const worker = this.worker;
    this.worker = null;
    // Bump the generation so anything already in flight from this worker is ignored.
    this.generation++;
    if (!worker) return;
    try {
      worker.terminate();
    } catch {
      /* a transport that cannot terminate is already unusable */
    }
  }
}
