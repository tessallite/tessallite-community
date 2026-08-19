import { describe, it, expect, vi } from "vitest";
import {
  sendMessageStream,
  newIdempotencyKey,
} from "../../../shared-ui/src/streaming/messagesStream";
import { StreamError } from "../../../shared-ui/src/types/streaming";

// Bug-6521 — the streaming retry re-POSTs a state-changing request. The
// idempotency key is minted ONCE per logical send (captured in the fetchStream
// closure), so an automatic retry re-invokes the SAME closure and re-sends the
// SAME key. These tests prove the retry loop re-invokes the identical closure
// (stable key) and that fresh sends get fresh keys.

describe("newIdempotencyKey", () => {
  it("returns a non-empty, unique key per call", () => {
    const a = newIdempotencyKey();
    const b = newIdempotencyKey();
    expect(typeof a).toBe("string");
    expect(a.length).toBeGreaterThan(0);
    expect(a).not.toBe(b);
  });
});

describe("sendMessageStream retry key stability", () => {
  it("re-invokes the same fetchStream closure (stable key) on automatic retry", async () => {
    vi.useFakeTimers();
    try {
      // One key per logical send, captured in the closure — exactly how
      // ChatCanvas.handleSend threads it above the retry loop.
      const key = newIdempotencyKey();
      const seenKeys: string[] = [];
      let attempt = 0;
      const fetchStream = vi.fn(async () => {
        seenKeys.push(key);
        attempt += 1;
        if (attempt === 1) {
          // First attempt fails before any meaningful event -> triggers retry.
          throw new Error("network drop");
        }
        // Second attempt succeeds with a proper terminal event. A bodyless 200
        // is NOT a valid terminal (see F-037-01 regression below) — it would
        // now surface as an error, so a real success must carry turn.completed.
        return new Response(
          "event: turn.completed\ndata: {}\n\n",
          { status: 200, headers: { "Content-Type": "text/event-stream" } },
        );
      });

      const onComplete = vi.fn();
      const { promise } = sendMessageStream(fetchStream, {
        onEvent: vi.fn(),
        onError: vi.fn(),
        onComplete,
      });

      // Drive the 1.5s retry backoff.
      await vi.advanceTimersByTimeAsync(2_000);
      await promise;

      expect(fetchStream).toHaveBeenCalledTimes(2);
      expect(seenKeys).toEqual([key, key]);
      expect(onComplete).toHaveBeenCalledTimes(1);
    } finally {
      vi.useRealTimers();
    }
  });
});

describe("sendMessageStream empty-stream terminal contract (F-037-01)", () => {
  it("treats a 200 that closes with zero events as an error, not a completion", async () => {
    // A gateway/proxy can close a 200 body before emitting any event. Without a
    // terminal event the send has NO business-valid answer; the UI must be
    // given a recovery cue (onError), never a silent onComplete that leaves the
    // user's question on screen with no answer.
    const onError = vi.fn();
    const onComplete = vi.fn();
    const fetchStream = vi.fn(
      async () => new Response(null, { status: 200 }),
    );

    const { promise } = sendMessageStream(fetchStream, {
      onEvent: vi.fn(),
      onError,
      onComplete,
    });
    await promise;

    expect(onError).toHaveBeenCalledTimes(1);
    expect(onComplete).not.toHaveBeenCalled();
    // Bug-8370 — typed taxonomy, not a raw Error. A null-bodied Response has
    // no readable stream at all (`.body === null`), so this is classified as
    // 'http_error' (the bodyless-response guard), distinct from
    // 'unexpected_end' below (a real body stream that closes without ever
    // sending a terminal event).
    const err = onError.mock.calls[0][0];
    expect(err).toBeInstanceOf(StreamError);
    expect((err as StreamError).code).toBe("http_error");
  });

  it("treats a 200 with content but no terminal event as an error", async () => {
    const onError = vi.fn();
    const onComplete = vi.fn();
    const fetchStream = vi.fn(
      async () =>
        new Response("event: token\ndata: {\"text\":\"hi\"}\n\n", {
          status: 200,
          headers: { "Content-Type": "text/event-stream" },
        }),
    );

    const { promise } = sendMessageStream(fetchStream, {
      onEvent: vi.fn(),
      onError,
      onComplete,
    });
    await promise;

    expect(onError).toHaveBeenCalledTimes(1);
    expect(onComplete).not.toHaveBeenCalled();
    const err = onError.mock.calls[0][0];
    expect(err).toBeInstanceOf(StreamError);
    expect((err as StreamError).code).toBe("unexpected_end");
  });
});
