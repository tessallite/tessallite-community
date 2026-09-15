import type { StreamCallbacks, StreamLifecycleOptions } from "../types/streaming";
/**
 * Bug-6521 — mint a per-send idempotency key. Callers MUST generate this ONCE
 * per logical send, ABOVE the retry loop (i.e. captured in the `fetchStream`
 * closure passed to `sendMessageStream`), so every automatic retry re-sends the
 * SAME key. The backend dedupes the turn reservation on it, so a retried stream
 * cannot duplicate/re-run the turn. A fresh user send (e.g. the "retry sending"
 * button) is a new logical send and correctly gets a new key.
 */
export declare function newIdempotencyKey(): string;
export declare function sendMessageStream(fetchStream: () => Promise<Response>, callbacks: StreamCallbacks, lifecycle?: StreamLifecycleOptions): {
    promise: Promise<void>;
};
