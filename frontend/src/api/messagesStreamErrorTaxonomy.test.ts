/**
 * Bug-8370 — typed StreamError/StreamErrorCode taxonomy for stream failures.
 *
 * messagesStream.ts previously threw raw `Error` objects with hardcoded
 * English messages, and ChatCanvas.tsx surfaced them verbatim via
 * `err.message`, bypassing the host i18n layer. These tests prove every
 * failure path now raises a typed `StreamError` with the right
 * `StreamErrorCode`, and that `resolveStreamErrorMessage` (the ChatCanvas
 * boundary) maps a known code to its friendly i18n message while degrading
 * gracefully — never crashing, never leaking raw engine text — for a code it
 * does not recognise. This is also the regression guard for the v5 bug the
 * brief calls out: a correct implementation must be a lookup TABLE keyed by
 * the code, never `err.code in t` (a membership test against the translator
 * FUNCTION, not a container).
 */
import { describe, it, expect, vi } from "vitest";
import { sendMessageStream } from "../../../shared-ui/src/streaming/messagesStream";
import {
  StreamError,
  resolveStreamErrorMessage,
  type StreamErrorCode,
} from "../../../shared-ui/src/types/streaming";
import sharedChatMessages from "../i18n/en/shared-chat.json";

const source = sharedChatMessages as Record<string, string>;
const t = (key: string) => source[key] ?? key;

function neverEndingResponse(): Response {
  const stream = new ReadableStream<Uint8Array>({ start() {} });
  return new Response(stream, {
    status: 200,
    headers: { "Content-Type": "text/event-stream" },
  });
}

describe("messagesStream — StreamErrorCode classification (Bug-8370)", () => {
  it("classifies a non-OK response as 'http_error'", async () => {
    const fetchStream = vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ error: "bad request" }), { status: 400 }),
    );
    const onError = vi.fn();
    const { promise } = sendMessageStream(fetchStream, {
      onEvent: vi.fn(),
      onError,
      onComplete: vi.fn(),
    });
    await promise;

    expect(fetchStream).toHaveBeenCalledTimes(1); // no retry on a business 4xx
    expect(onError).toHaveBeenCalledTimes(1);
    const err = onError.mock.calls[0][0];
    expect(err).toBeInstanceOf(StreamError);
    expect((err as StreamError).code).toBe("http_error");
    expect(err.message).toBe("bad request");
  });

  it("classifies a read timeout with no data ever received as 'timeout'", async () => {
    vi.useFakeTimers();
    try {
      const fetchStream = vi
        .fn()
        .mockResolvedValueOnce(neverEndingResponse())
        .mockResolvedValueOnce(neverEndingResponse())
        .mockResolvedValueOnce(neverEndingResponse());
      const onError = vi.fn();
      const { promise } = sendMessageStream(fetchStream, {
        onEvent: vi.fn(),
        onError,
        onComplete: vi.fn(),
      });

      // 3 attempts x 45s read timeout, plus the 1.5s/3s retry backoffs.
      await vi.advanceTimersByTimeAsync(200_000);
      await promise;

      expect(fetchStream).toHaveBeenCalledTimes(3);
      expect(onError).toHaveBeenCalledTimes(1);
      const err = onError.mock.calls[0][0];
      expect(err).toBeInstanceOf(StreamError);
      expect((err as StreamError).code).toBe("timeout");
    } finally {
      vi.useRealTimers();
    }
  });

  it("classifies a mid-stream drop AFTER meaningful content as 'connection_lost', without retrying", async () => {
    const encoder = new TextEncoder();
    // `pull` (not `start`) so the first chunk is genuinely delivered to a
    // real `read()` call before the stream errors on the NEXT read — errors
    // reject every past AND future read and discard the queue, so enqueueing
    // and erroring together in `start` would never actually deliver the
    // first frame.
    let pulls = 0;
    const stream = new ReadableStream<Uint8Array>({
      pull(controller) {
        pulls += 1;
        if (pulls === 1) {
          controller.enqueue(
            encoder.encode('event: narration.delta\ndata: {"text":"partial"}\n\n'),
          );
        } else {
          controller.error(new Error("socket reset"));
        }
      },
    });
    const fetchStream = vi.fn().mockResolvedValue(
      new Response(stream, { status: 200, headers: { "Content-Type": "text/event-stream" } }),
    );
    const onError = vi.fn();
    const onEvent = vi.fn();
    const { promise } = sendMessageStream(fetchStream, {
      onEvent,
      onError,
      onComplete: vi.fn(),
    });
    await promise;

    expect(onEvent).toHaveBeenCalledWith("narration.delta", { text: "partial" });
    expect(fetchStream).toHaveBeenCalledTimes(1); // no retry once content streamed
    expect(onError).toHaveBeenCalledTimes(1);
    const err = onError.mock.calls[0][0];
    expect(err).toBeInstanceOf(StreamError);
    expect((err as StreamError).code).toBe("connection_lost");
  });

  it("normalises a raw transport failure with no data ever received into 'network_error'", async () => {
    const fetchStream = vi.fn().mockRejectedValue(new TypeError("Failed to fetch"));
    const onError = vi.fn();
    const { promise } = sendMessageStream(fetchStream, {
      onEvent: vi.fn(),
      onError,
      onComplete: vi.fn(),
    });
    await promise;

    expect(fetchStream).toHaveBeenCalledTimes(3); // retries exhausted (MAX_RETRIES=2)
    expect(onError).toHaveBeenCalledTimes(1);
    const err = onError.mock.calls[0][0];
    expect(err).toBeInstanceOf(StreamError);
    expect((err as StreamError).code).toBe("network_error");
    expect(err.message).toBe("Failed to fetch");
  });
});

describe("resolveStreamErrorMessage — the ChatCanvas i18n boundary (Bug-8370)", () => {
  const CASES: Array<[StreamErrorCode, string]> = [
    ["http_error", "stream.error.httpError"],
    ["timeout", "stream.error.timeout"],
    ["unexpected_end", "stream.error.unexpectedEnd"],
    ["connection_lost", "stream.error.connectionLost"],
    ["network_error", "stream.error.networkError"],
  ];

  it.each(CASES)("maps StreamErrorCode '%s' to its friendly i18n message", (code, key) => {
    const err = new StreamError(code, "some raw engine text");
    const message = resolveStreamErrorMessage(err, t);
    expect(message).toBe(source[key]);
    // The Bug-8370 regression itself: raw engine text must never reach the UI.
    expect(message).not.toBe(err.message);
  });

  it("degrades gracefully for an unrecognised StreamErrorCode instead of crashing", () => {
    // Simulates a future code this build's map does not know about yet, or a
    // consumer running stale shared-ui against a newer producer.
    const err = new StreamError("totally_unknown" as StreamErrorCode, "raw text");
    expect(() => resolveStreamErrorMessage(err, t)).not.toThrow();
    expect(resolveStreamErrorMessage(err, t)).toBe(source["chat.connectionError"]);
  });

  it("degrades gracefully for a non-StreamError Error (never reads .code)", () => {
    const err = new Error("plain error, not a StreamError");
    expect(() => resolveStreamErrorMessage(err, t)).not.toThrow();
    expect(resolveStreamErrorMessage(err, t)).toBe(source["chat.connectionError"]);
  });

  it("is a lookup TABLE, not the v5 `err.code in t` bug — never resolves via Function.prototype membership", () => {
    // The v5 bug tested `err.code in t` against the translator FUNCTION: `in`
    // tests object-key membership, so a code that happens to collide with a
    // Function.prototype member name (e.g. "name", "length", "call") would
    // spuriously read as present. Prove a StreamErrorCode-shaped value that
    // collides with a real Function.prototype property degrades to the
    // generic fallback rather than resolving through that prototype chain.
    const err = new StreamError("name" as StreamErrorCode, "raw text");
    expect(resolveStreamErrorMessage(err, t)).toBe(source["chat.connectionError"]);
  });

  // F-L3-R1-01 (round-2 fix) — a PLAIN OBJECT lookup table (`{...}[key]`) is
  // its own residual version of the prototype-collision bug: indexing with
  // an unrecognised key also resolves inherited Object.prototype members
  // ("constructor", "toString", "hasOwnProperty", "valueOf", ...), which are
  // truthy FUNCTIONS, not undefined. The "name" test above collides with
  // Function.prototype (relevant to the ORIGINAL `err.code in t` bug against
  // a function); these collide with Object.prototype instead, the residual
  // gap a plain-object table left open. The fix (a Map, see
  // types/streaming.ts) has no prototype-inherited keys for `.get()` to
  // accidentally return.
  it.each(["constructor", "toString", "hasOwnProperty", "valueOf"] as const)(
    "degrades gracefully for the Object.prototype-colliding code '%s', never returning the inherited member",
    (collidingCode) => {
      const err = new StreamError(
        collidingCode as unknown as StreamErrorCode,
        "raw text",
      );
      const tSpy = vi.fn(t);

      const message = resolveStreamErrorMessage(err, tSpy);

      // A real STRING message, never the inherited Object.prototype function
      // a naive `table[key]` lookup would have returned instead.
      expect(typeof message).toBe("string");
      expect(message).toBe(source["chat.connectionError"]);
      // The translator itself must receive the STRING generic key — proves
      // the bug can't even get as far as calling t() with a function.
      expect(tSpy).toHaveBeenCalledWith("chat.connectionError");
    },
  );
});
