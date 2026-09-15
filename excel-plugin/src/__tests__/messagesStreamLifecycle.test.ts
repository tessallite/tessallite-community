import { describe, it, expect, vi } from "vitest";
import { sendMessageStream } from "@tessallite/shared-ui";

function callbacks() {
  return {
    onEvent: vi.fn(),
    onError: vi.fn(),
    onComplete: vi.fn(),
  };
}

function abortError(): DOMException {
  return new DOMException("The operation was aborted.", "AbortError");
}

describe("stream send lifecycle (Bug-9805)", () => {
  it("ignores caller abort before response headers arrive", async () => {
    const caller = new AbortController();
    const streamCallbacks = callbacks();
    const fetchStream = vi.fn(
      () =>
        new Promise<Response>((_, reject) => {
          caller.signal.addEventListener("abort", () => reject(abortError()), {
            once: true,
          });
        }),
    );

    const { promise } = sendMessageStream(
      fetchStream,
      streamCallbacks,
      { signal: caller.signal },
    );
    await vi.waitFor(() => expect(fetchStream).toHaveBeenCalledOnce());
    caller.abort();
    await promise;

    expect(streamCallbacks.onEvent).not.toHaveBeenCalled();
    expect(streamCallbacks.onError).not.toHaveBeenCalled();
    expect(streamCallbacks.onComplete).not.toHaveBeenCalled();
  });

  it("cancels the reader and ignores callbacks after headers but before the first event", async () => {
    const caller = new AbortController();
    let cancelCalls = 0;
    const body = new ReadableStream<Uint8Array>({
      pull() {
        // Keep the first reader.read() pending without closing the body.
      },
      cancel() {
        cancelCalls += 1;
      },
    });
    const streamCallbacks = callbacks();
    const { promise } = sendMessageStream(
      async () => new Response(body, { status: 200 }),
      streamCallbacks,
      { signal: caller.signal },
    );

    await new Promise<void>((resolve) => setTimeout(resolve, 0));
    caller.abort();
    await promise;

    expect(cancelCalls).toBe(1);
    expect(streamCallbacks.onEvent).not.toHaveBeenCalled();
    expect(streamCallbacks.onError).not.toHaveBeenCalled();
    expect(streamCallbacks.onComplete).not.toHaveBeenCalled();
  });

  it("keeps partial narration but suppresses all later callbacks after abort", async () => {
    const caller = new AbortController();
    const encoder = new TextEncoder();
    let pulls = 0;
    let cancelCalls = 0;
    const body = new ReadableStream<Uint8Array>({
      pull(controller) {
        pulls += 1;
        if (pulls === 1) {
          controller.enqueue(
            encoder.encode(
              'event: narration.delta\ndata: {"text":"partial"}\n\n',
            ),
          );
        }
      },
      cancel() {
        cancelCalls += 1;
      },
    });
    const streamCallbacks = callbacks();
    let narrationSeen!: () => void;
    const narrationPromise = new Promise<void>((resolve) => {
      narrationSeen = resolve;
    });
    streamCallbacks.onEvent.mockImplementation((eventName) => {
      if (eventName === "narration.delta") narrationSeen();
    });

    const { promise } = sendMessageStream(
      async () => new Response(body, { status: 200 }),
      streamCallbacks,
      { signal: caller.signal },
    );
    await narrationPromise;
    caller.abort();
    await promise;

    expect(streamCallbacks.onEvent).toHaveBeenCalledOnce();
    expect(streamCallbacks.onEvent).toHaveBeenCalledWith(
      "narration.delta",
      { text: "partial" },
    );
    expect(cancelCalls).toBe(1);
    expect(streamCallbacks.onError).not.toHaveBeenCalled();
    expect(streamCallbacks.onComplete).not.toHaveBeenCalled();
  });

  it("suppresses a terminal event when abort happens immediately before it", async () => {
    const caller = new AbortController();
    let cancelCalls = 0;
    const body = new ReadableStream<Uint8Array>({
      start(controller) {
        controller.enqueue(
          new TextEncoder().encode(
            'event: narration.delta\ndata: {"text":"partial"}\n\nevent: turn.completed\ndata: {}\n\n',
          ),
        );
      },
      cancel() {
        cancelCalls += 1;
      },
    });
    const streamCallbacks = callbacks();
    streamCallbacks.onEvent.mockImplementation((eventName) => {
      if (eventName === "narration.delta") caller.abort();
    });

    const { promise } = sendMessageStream(
      async () => new Response(body, { status: 200 }),
      streamCallbacks,
      { signal: caller.signal },
    );
    await promise;

    expect(streamCallbacks.onEvent).toHaveBeenCalledOnce();
    expect(streamCallbacks.onEvent).toHaveBeenCalledWith(
      "narration.delta",
      { text: "partial" },
    );
    expect(cancelCalls).toBe(1);
    expect(streamCallbacks.onError).not.toHaveBeenCalled();
    expect(streamCallbacks.onComplete).not.toHaveBeenCalled();
  });
});

describe("stream send generation (Bug-9805)", () => {
  it("ignores callbacks from a superseded generation", async () => {
    let generation = 1;
    const streamCallbacks = callbacks();
    streamCallbacks.onEvent.mockImplementation((eventName) => {
      if (eventName === "narration.delta") generation = 2;
    });
    const response = new Response(
      'event: narration.delta\ndata: {"text":"partial"}\n\nevent: turn.completed\ndata: {}\n\n',
      { status: 200 },
    );

    const { promise } = sendMessageStream(
      async () => response,
      streamCallbacks,
      { isCurrent: () => generation === 1 },
    );
    await promise;

    expect(streamCallbacks.onEvent).toHaveBeenCalledOnce();
    expect(streamCallbacks.onError).not.toHaveBeenCalled();
    expect(streamCallbacks.onComplete).not.toHaveBeenCalled();
  });
});
