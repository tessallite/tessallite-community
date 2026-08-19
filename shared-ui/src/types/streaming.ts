export interface StreamCallbacks {
  onEvent: (eventName: string, data: Record<string, unknown>) => void;
  onError: (error: Error) => void;
  onComplete: () => void;
}

export interface CompoundStep {
  step_number: number;
  title?: string;
  status: string;
  row_count?: number;
  preview_row?: Record<string, unknown>;
}

// Bug-8370 — a typed taxonomy for stream failures. Every failure path in
// messagesStream.ts throws/reports a StreamError carrying one of these
// codes instead of a raw Error with a hardcoded English message, so the UI
// boundary (ChatCanvas) can resolve a friendly, i18n-backed message instead
// of surfacing engine text verbatim to the user (the Bug-8370 defect: raw
// messagesStream.ts strings reached the toast via `err.message`).
export type StreamErrorCode =
  | "http_error" // non-OK / bodyless response from the stream endpoint
  | "timeout" // no data received within the read timeout
  | "unexpected_end" // stream closed with no terminal event (Bug-7380)
  | "connection_lost" // dropped mid-stream after meaningful content arrived
  | "network_error"; // transport failure, retries exhausted, nothing received

export class StreamError extends Error {
  readonly code: StreamErrorCode;

  constructor(code: StreamErrorCode, message: string) {
    super(message);
    this.name = "StreamError";
    this.code = code;
  }
}

// One i18n key per code — a lookup TABLE, not a v5-style `err.code in t`.
// `in` tests object-key membership; `t` is the translator FUNCTION, so
// `err.code in t` only ever tested the code against Function.prototype's own
// property names ("call", "length", "name", ...) — never a real result, and
// never the intended lookup. This keys a `Map` by the STRING code and
// calls t(key) via `.get()`; a code this map does not recognise (or a non-StreamError)
// degrades to the existing generic connection-error copy rather than
// throwing or leaking raw engine text.
//
// Round-2 fix (F-L3-R1-01) — a PLAIN OBJECT lookup (`{...}[key]`) is not
// actually safe against every unrecognised string key: it also resolves
// inherited Object.prototype members ("constructor", "toString",
// "hasOwnProperty", "valueOf", ...). An unrecognised/future code that
// happens to be literally "constructor" silently returned the inherited
// FUNCTION rather than falling through to the generic message — one
// prototype chain deeper than the `err.code in t` bug this table replaced,
// but the same class of defect. A `Map` has no prototype-inherited keys for
// `.get()` to accidentally return, so it is correct by construction rather
// than correct-if-nobody-forgets-an-own-property-check; `Object.hasOwn`
// would also work but needs an ES2022 lib target this source-only package
// (and every one of its three consumers' own tsconfigs) does not carry.
const STREAM_ERROR_MESSAGE_KEYS: ReadonlyMap<StreamErrorCode, string> = new Map([
  ["http_error", "stream.error.httpError"],
  ["timeout", "stream.error.timeout"],
  ["unexpected_end", "stream.error.unexpectedEnd"],
  ["connection_lost", "stream.error.connectionLost"],
  ["network_error", "stream.error.networkError"],
]);

export function resolveStreamErrorMessage(
  err: Error,
  t: (key: string, params?: Record<string, string | number>) => string,
): string {
  if (err instanceof StreamError) {
    const key = STREAM_ERROR_MESSAGE_KEYS.get(err.code);
    if (key) return t(key);
  }
  return t("chat.connectionError");
}
