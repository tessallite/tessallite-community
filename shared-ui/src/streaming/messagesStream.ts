import type {
  StreamCallbacks,
  StreamLifecycleOptions,
} from "../types/streaming";
import { StreamError } from "../types/streaming";

const READ_TIMEOUT_MS = 45_000;
const MAX_RETRIES = 2;
const RETRY_DELAY_MS = 1_500;

/**
 * Bug-6521 — mint a per-send idempotency key. Callers MUST generate this ONCE
 * per logical send, ABOVE the retry loop (i.e. captured in the `fetchStream`
 * closure passed to `sendMessageStream`), so every automatic retry re-sends the
 * SAME key. The backend dedupes the turn reservation on it, so a retried stream
 * cannot duplicate/re-run the turn. A fresh user send (e.g. the "retry sending"
 * button) is a new logical send and correctly gets a new key.
 */
export function newIdempotencyKey(): string {
  const cryptoObj =
    typeof globalThis !== "undefined"
      ? (globalThis.crypto as Crypto | undefined)
      : undefined;
  if (cryptoObj?.randomUUID) {
    return cryptoObj.randomUUID();
  }
  // Fallback for environments without crypto.randomUUID (older jsdom/test).
  return (
    "idmp-" +
    Date.now().toString(36) +
    "-" +
    Math.random().toString(36).slice(2, 12)
  );
}

function readWithTimeout(
  reader: ReadableStreamDefaultReader<Uint8Array>,
  timeoutMs: number,
): Promise<ReadableStreamReadResult<Uint8Array>> {
  return new Promise((resolve, reject) => {
    const timer = setTimeout(
      () =>
        reject(
          new StreamError(
            "timeout",
            "Stream read timed out — no data received.",
          ),
        ),
      timeoutMs,
    );
    reader.read().then(
      (result) => {
        clearTimeout(timer);
        resolve(result);
      },
      (err) => {
        clearTimeout(timer);
        reject(err);
      },
    );
  });
}

interface DeliveryState {
  receivedMeaningful: boolean;
}

function isStreamActive(lifecycle: StreamLifecycleOptions): boolean {
  return !lifecycle.signal?.aborted && (lifecycle.isCurrent?.() ?? true);
}

function isAbortError(err: unknown): boolean {
  return (
    (err instanceof Error && err.name === "AbortError") ||
    (typeof DOMException !== "undefined" &&
      err instanceof DOMException &&
      err.name === "AbortError")
  );
}

function waitForRetry(
  delayMs: number,
  signal?: AbortSignal,
): Promise<boolean> {
  if (signal?.aborted) return Promise.resolve(false);
  return new Promise((resolve) => {
    let settled = false;
    const timer = setTimeout(() => finish(true), delayMs);
    const onAbort = () => finish(false);
    const finish = (shouldRetry: boolean) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      signal?.removeEventListener("abort", onAbort);
      resolve(shouldRetry);
    };
    signal?.addEventListener("abort", onAbort, { once: true });
    if (signal?.aborted) finish(false);
  });
}

// Bug-7380: terminal events that signal a business-valid stream end.
const TERMINAL_EVENTS = new Set([
  "turn.completed",
  "turn.error",
  "turn.blocked",
]);

async function attemptStream(
  rawResponse: Response,
  callbacks: StreamCallbacks,
  delivery: DeliveryState,
  lifecycle: StreamLifecycleOptions,
): Promise<boolean> {
  let buffer = "";
  let currentEvent = "";
  let dataLines: string[] = [];
  let deliveredTerminal = false;

  const dispatchFrame = () => {
    if (!isStreamActive(lifecycle)) {
      currentEvent = "";
      dataLines = [];
      return;
    }
    // Bug-7548 — per the SSE spec, a frame with `data:` lines but no `event:`
    // field defaults to the event type "message". Do not silently drop such
    // spec-legal data-only frames (the previous `&& currentEvent` guard did).
    if (dataLines.length > 0) {
      const eventName = currentEvent || "message";
      const raw = dataLines.join("\n");
      try {
        const data = JSON.parse(raw) as Record<string, unknown>;
        delivery.receivedMeaningful = true;
        if (TERMINAL_EVENTS.has(eventName)) {
          deliveredTerminal = true;
        }
        if (isStreamActive(lifecycle)) callbacks.onEvent(eventName, data);
      } catch {
        // ignore malformed data payloads
      }
    }
    currentEvent = "";
    dataLines = [];
  };

  const consumeLine = (line: string) => {
    // Bug-7548 — the SSE spec treats a blank line as the frame boundary and
    // accepts LF, CRLF, and lone CR as line terminators. We split on "\n"
    // below, so a CRLF stream leaves a trailing "\r": strip it so the blank
    // separator line is recognised and data payloads are not corrupted.
    if (line.endsWith("\r")) line = line.slice(0, -1);
    if (line === "") {
      dispatchFrame();
      return;
    }
    if (line.startsWith(":")) return;
    if (line.startsWith("event:")) {
      currentEvent = line.slice(6).trim();
      return;
    }
    if (line.startsWith("data:")) {
      let value = line.slice(5);
      if (value.startsWith(" ")) value = value.slice(1);
      dataLines.push(value);
    }
  };

  if (!isStreamActive(lifecycle)) return false;

  if (!rawResponse.ok || !rawResponse.body) {
    if (!isStreamActive(lifecycle)) return false;
    const errBody = await rawResponse
      .json()
      .catch(() => ({ error: "unknown" }));
    if (!isStreamActive(lifecycle)) return false;
    callbacks.onError(
      new StreamError(
        "http_error",
        (errBody as Record<string, string>).error ||
          `HTTP ${rawResponse.status}`,
      ),
    );
    return false;
  }

  const reader = rawResponse.body.getReader();
  const decoder = new TextDecoder();
  let cancelPromise: Promise<void> | null = null;
  const cancelReader = () => {
    if (!cancelPromise) {
      cancelPromise = reader.cancel().catch(() => {});
    }
    return cancelPromise;
  };
  const onAbort = () => {
    void cancelReader();
  };
  lifecycle.signal?.addEventListener("abort", onAbort, { once: true });

  try {
    while (true) {
      if (!isStreamActive(lifecycle)) return false;
      const { done, value } = await readWithTimeout(reader, READ_TIMEOUT_MS);
      if (!isStreamActive(lifecycle)) return false;
      if (done) break;

      buffer += decoder.decode(value, { stream: true });
      // Bug-7548 — normalise CRLF and lone-CR terminators to LF before
      // splitting so all three SSE-spec line endings are handled. A lone
      // trailing "\r" (possible when a CR lands at a chunk boundary) is kept in
      // `buffer` so it is not mistaken for a completed line.
      buffer = buffer.replace(/\r\n/g, "\n").replace(/\r(?!$)/g, "\n");
      const lines = buffer.split("\n");
      buffer = lines.pop() ?? "";

      for (const line of lines) {
        if (!isStreamActive(lifecycle)) return false;
        consumeLine(line);
      }
    }

    if (!isStreamActive(lifecycle)) return false;

    // Bug-7548 — flush any residual buffered line (an unterminated final frame
    // that arrived without a trailing newline), then close the frame.
    if (buffer) {
      consumeLine(buffer);
      buffer = "";
    }
    dispatchFrame();

    if (!isStreamActive(lifecycle)) return false;

    // Bug-7380 + F-037-01/F-024-05: a business-valid stream MUST end with a
    // terminal event (turn.completed / turn.blocked / turn.error). If EOF
    // arrives without one, the transport close was not a business completion —
    // regardless of whether any meaningful content was delivered. A 200 whose
    // body closes with ZERO events (gateway/proxy close) is therefore an
    // error, not a silent success: raise it so the UI offers recovery instead
    // of leaving the user's question on screen with no answer.
    if (!deliveredTerminal) {
      if (!isStreamActive(lifecycle)) return false;
      callbacks.onError(
        new StreamError(
          "unexpected_end",
          "The response stream ended unexpectedly. Please check the conversation or retry.",
        ),
      );
      return false;
    }

    if (!isStreamActive(lifecycle)) return false;
    callbacks.onComplete();
    return true;
  } finally {
    lifecycle.signal?.removeEventListener("abort", onAbort);
    // Bug found in L3 (adjacent, pre-existing): `reader.cancel()` returns a
    // PROMISE. Cancelling a stream that is already in the "errored" state
    // (e.g. after a genuine reader.read() rejection — a real network drop,
    // not just the timeout path) rejects that promise with the stream's
    // stored error. A synchronous try/catch around an un-awaited call does
    // NOT catch a promise rejection, so this became an unhandled rejection
    // on every real transport failure mid-stream, not just a synthetic one.
    try {
      await cancelReader();
    } catch {
      /* already closed/errored — nothing to cancel */
    }
  }
}

export function sendMessageStream(
  fetchStream: () => Promise<Response>,
  callbacks: StreamCallbacks,
  lifecycle: StreamLifecycleOptions = {},
): { promise: Promise<void> } {
  const promise = (async () => {
    const delivery: DeliveryState = { receivedMeaningful: false };
    for (let attempt = 0; attempt <= MAX_RETRIES; attempt++) {
      if (!isStreamActive(lifecycle)) return;
      try {
        const response = await fetchStream();
        if (!isStreamActive(lifecycle)) return;
        await attemptStream(response, callbacks, delivery, lifecycle);
        return;
      } catch (err) {
        if (!isStreamActive(lifecycle) || isAbortError(err)) return;
        if (delivery.receivedMeaningful) {
          // Bug-6521 — once meaningful content has streamed, retrying would
          // duplicate visible text, so this is always reported as
          // "connection lost" (never retried) regardless of the underlying
          // cause (a StreamError from readWithTimeout, or a raw transport
          // rejection from reader.read()/fetch).
          if (!isStreamActive(lifecycle)) return;
          callbacks.onError(
            new StreamError(
              "connection_lost",
              "Connection lost after the response started. Please check the conversation before resending.",
            ),
          );
          return;
        }
        if (attempt < MAX_RETRIES) {
          const shouldRetry = await waitForRetry(
            RETRY_DELAY_MS * (attempt + 1),
            lifecycle.signal,
          );
          if (!shouldRetry || !isStreamActive(lifecycle)) return;
          continue;
        }
        // Retries exhausted with no meaningful content ever received. Keep a
        // StreamError's own code (e.g. "timeout"); normalise anything else
        // (a raw fetch/reader rejection) into "network_error" so the caller
        // always receives a typed, classifiable error.
        if (!isStreamActive(lifecycle)) return;
        callbacks.onError(
          err instanceof StreamError
            ? err
            : new StreamError(
                "network_error",
                err instanceof Error ? err.message : String(err),
              ),
        );
      }
    }
  })();

  return { promise };
}
