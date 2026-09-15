/**
 * Core API client for Tessallite services.
 * All calls use JWT bearer authentication via OfficeRuntime.storage.
 */

import { getJwt, removeJwt } from '../utils/storage';
import { logApiEvent, logError } from '../utils/diagnostics';
import { StreamError } from '@tessallite/shared-ui';

let baseUrl = '';
let onUnauthorized: (() => void) | null = null;

export function configureApiClient(serverUrl: string, on401?: () => void) {
  baseUrl = serverUrl.replace(/\/$/, '');
  if (on401) onUnauthorized = on401;
}

export class ApiError extends Error {
  status: number;
  body: unknown;

  constructor(status: number, body: unknown) {
    super(extractDetailMessage(body) ?? `API error ${status}`);
    this.status = status;
    this.body = body;
  }
}

/**
 * Render a FastAPI error `detail` into a readable string.
 * The canonical filter contract returns string details, but pydantic
 * body-validation 422s return `detail` as a list of `{loc, msg, type}` objects.
 * Coercing a list with template interpolation yields "[object Object]" (L-4),
 * so format the list's `msg` fields instead.
 */
function extractDetailMessage(body: unknown): string | null {
  if (typeof body !== 'object' || body === null || !('detail' in body)) {
    return typeof body === 'string' ? body : null;
  }
  const detail = (body as Record<string, unknown>).detail;
  if (typeof detail === 'string') return detail;
  if (Array.isArray(detail)) {
    const msgs = detail
      .map(item =>
        item && typeof item === 'object' && 'msg' in item
          ? String((item as Record<string, unknown>).msg)
          : typeof item === 'string'
            ? item
            : JSON.stringify(item),
      )
      .filter(Boolean);
    return msgs.length > 0 ? msgs.join('; ') : JSON.stringify(detail);
  }
  if (detail != null) return JSON.stringify(detail);
  return null;
}

const MAX_SAFE_RETRIES = 3;
const BASE_DELAY_MS = 1000;

/**
 * Bug-9755: wall-clock ceiling for a task-pane request, mirroring the
 * ceiling `functions.ts::apiRequest` got for Bug-9749 — same shared `fetch`
 * primitive, same unbounded-await exposure (no ceiling means an unsettled
 * promise here freezes whatever UI is awaiting it, e.g. a spinner that never
 * clears, rather than a cell stuck at `#GETTING_DATA`). Duplicated locally
 * rather than imported: `client.ts` (WebView2 task pane) and `functions.ts`
 * (WWAHost custom-functions runtime) are separate bundles.
 */
const REQUEST_TIMEOUT_MS = 30_000;

export interface RequestOptions {
  signal?: AbortSignal;
}

/**
 * Keep a caller abort connected for the whole fetch, including response-body
 * consumption. `AbortSignal.any` is available on current WebView2; the linked
 * controller fallback preserves the same contract on older hosts.
 */
function composeAbortSignals(
  callerSignal: AbortSignal | undefined,
  internalSignal: AbortSignal | undefined,
): AbortSignal | undefined {
  if (!callerSignal) return internalSignal;
  if (!internalSignal) return callerSignal;

  const signalConstructor =
    typeof AbortSignal !== 'undefined'
      ? (AbortSignal as typeof AbortSignal & {
          any?: (signals: AbortSignal[]) => AbortSignal;
        })
      : undefined;
  if (typeof signalConstructor?.any === 'function') {
    return signalConstructor.any([callerSignal, internalSignal]);
  }

  const linkedController = new AbortController();
  const relayAbort = () => linkedController.abort();
  if (callerSignal.aborted || internalSignal.aborted) {
    relayAbort();
  } else {
    callerSignal.addEventListener('abort', relayAbort, { once: true });
    internalSignal.addEventListener('abort', relayAbort, { once: true });
  }
  return linkedController.signal;
}

function withTimeout<T>(promise: Promise<T>, ms: number): Promise<T> {
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error(`timeout_${ms}ms`)), ms);
    promise.then(
      (val) => { clearTimeout(timer); resolve(val); },
      (err) => { clearTimeout(timer); reject(err); },
    );
  });
}

async function request<T>(
  method: string,
  path: string,
  body?: unknown,
  retries = 0,
  options: RequestOptions = {},
): Promise<T> {
  const jwt = await getJwt();
  if (options.signal?.aborted) throw createAbortError();
  const headers: Record<string, string> = {
    'Content-Type': 'application/json',
  };
  if (jwt) {
    headers['Authorization'] = `Bearer ${jwt}`;
  }

  const controller = typeof AbortController !== 'undefined' ? new AbortController() : null;
  const requestSignal = composeAbortSignals(options.signal, controller?.signal);
  const startMs = Date.now();
  let res: Response;
  try {
    res = await withTimeout(
      fetch(`${baseUrl}${path}`, {
        method,
        headers,
        credentials: 'omit',
        body: body ? JSON.stringify(body) : undefined,
        ...(requestSignal ? { signal: requestSignal } : {}),
      }),
      REQUEST_TIMEOUT_MS,
    );
  } catch (e) {
    if (e instanceof Error && e.message.startsWith('timeout_')) {
      controller?.abort();
      logError(`API timeout on ${path} after ${REQUEST_TIMEOUT_MS}ms`);
      throw new ApiError(0, { detail: 'Request timed out. Check your connection and try again.' });
    }
    throw e;
  }
  const durationMs = Date.now() - startMs;

  if (res.status === 401) {
    logApiEvent(path, 401, durationMs);
    await removeJwt();
    onUnauthorized?.();
    throw new ApiError(401, { detail: 'Session expired. Please sign in again.' });
  }

  if (!res.ok) {
    logApiEvent(path, res.status, durationMs);
    if (method === 'GET' && res.status >= 500 && retries < MAX_SAFE_RETRIES) {
      if (options.signal?.aborted) throw createAbortError();
      const delay = BASE_DELAY_MS * Math.pow(2, retries);
      await new Promise(r => setTimeout(r, delay));
      if (options.signal?.aborted) throw createAbortError();
      return request<T>(method, path, body, retries + 1, options);
    }

    let errorBody: unknown;
    try {
      const text = await withTimeout(res.text(), REQUEST_TIMEOUT_MS);
      try { errorBody = JSON.parse(text); } catch { errorBody = text; }
    } catch { errorBody = `${res.status} ${res.statusText}`; }
    logError(`API ${res.status} on ${path}: ${JSON.stringify(errorBody)}`);
    throw new ApiError(res.status, errorBody);
  }

  logApiEvent(path, res.status, durationMs);
  // Bug-1095: 204 No Content (and other empty-body 2xx, e.g. the turn-feedback
  // route) carry no JSON. Calling res.json() on an empty body rejects, which
  // would surface a spurious failure on an otherwise-successful POST. Return
  // undefined for empty bodies.
  if (res.status === 204) {
    return undefined as T;
  }
  // Bug-9755: headers can arrive while the body never does; the ceiling covers
  // the body read too, matching Bug-9749's coverage of `functions.ts::apiRequest`.
  let text: string;
  try {
    text = await withTimeout(res.text(), REQUEST_TIMEOUT_MS);
  } catch (e) {
    if (e instanceof Error && e.message.startsWith('timeout_')) {
      logError(`API timeout reading response body on ${path} after ${REQUEST_TIMEOUT_MS}ms`);
      throw new ApiError(0, { detail: 'Request timed out. Check your connection and try again.' });
    }
    throw e;
  }
  if (!text) {
    return undefined as T;
  }
  return JSON.parse(text) as T;
}

function safeGet<T>(path: string, options?: RequestOptions): Promise<T> {
  return request<T>('GET', path, undefined, 0, options);
}

export const apiClient = {
  get: safeGet,
  post: <T>(path: string, body?: unknown) => request<T>('POST', path, body),
  put: <T>(path: string, body?: unknown) => request<T>('PUT', path, body),
  patch: <T>(path: string, body?: unknown) => request<T>('PATCH', path, body),
  delete: <T>(path: string) => request<T>('DELETE', path),
};

export async function streamRequest(
  path: string,
  body: unknown,
  signal?: AbortSignal,
  extraHeaders?: Record<string, string>,
): Promise<Response> {
  const jwt = await getJwt();
  const headers: Record<string, string> = {
    'Content-Type': 'application/json',
    // Bug-6596: callers may forward per-request headers (e.g. the Bug-6521
    // Idempotency-Key) without losing the JWT auth this helper injects.
    ...(extraHeaders ?? {}),
  };
  if (jwt) {
    headers['Authorization'] = `Bearer ${jwt}`;
  }

  // Bug-9805: keep the caller's cancellation separate from the transport
  // deadline. The shared stream driver retries typed timeout failures, while
  // an AbortError remains a user cancellation and must stop that retry loop.
  const internalController =
    typeof AbortController !== 'undefined' ? new AbortController() : null;
  if (signal?.aborted) {
    internalController?.abort();
    throw createAbortError();
  }

  let removeCallerAbortListener = () => {};
  const callerAbort = signal
    ? new Promise<Response>((_, reject) => {
        const onAbort = () => {
          internalController?.abort();
          reject(createAbortError());
        };
        signal.addEventListener('abort', onAbort, { once: true });
        removeCallerAbortListener = () => signal.removeEventListener('abort', onAbort);
      })
    : null;

  // Keep the caller relay alive after headers resolve. The stream driver owns
  // the body reader, but this client owns the fetch transport and must continue
  // propagating caller cancellation until that body is fully consumed.
  const requestSignal = composeAbortSignals(signal, internalController?.signal);
  const fetchPromise = Promise.resolve().then(() => fetch(`${baseUrl}${path}`, {
    method: 'POST',
    headers,
    credentials: 'omit',
    body: JSON.stringify(body),
    ...(requestSignal ? { signal: requestSignal } : {}),
  }));
  let timeoutHandle: ReturnType<typeof setTimeout> | undefined;
  const headerTimeout = new Promise<Response>((_, reject) => {
    timeoutHandle = setTimeout(() => {
      internalController?.abort();
      reject(new StreamError(
        'timeout',
        'The stream request timed out before response headers arrived.',
      ));
    }, REQUEST_TIMEOUT_MS);
  });

  let res: Response;
  try {
    const pending: Promise<Response>[] = [fetchPromise, headerTimeout];
    if (callerAbort) pending.push(callerAbort);
    res = await Promise.race(pending);
  } finally {
    if (timeoutHandle !== undefined) clearTimeout(timeoutHandle);
    removeCallerAbortListener();
  }

  if (res.status === 401) {
    await removeJwt();
    onUnauthorized?.();
    throw new ApiError(401, { detail: 'Session expired. Please sign in again.' });
  }

  if (!res.ok) {
    let errorBody: unknown;
    try {
      const text = await res.text();
      try { errorBody = JSON.parse(text); } catch { errorBody = text; }
    } catch { errorBody = `${res.status} ${res.statusText}`; }
    throw new ApiError(res.status, errorBody);
  }

  return res;
}

function createAbortError(): Error {
  if (typeof DOMException !== 'undefined') {
    return new DOMException('The operation was aborted.', 'AbortError');
  }
  const error = new Error('The operation was aborted.');
  error.name = 'AbortError';
  return error;
}

export function formatApiError(err: ApiError): string {
  switch (err.status) {
    case 401:
      return 'Session expired. Please sign in again.';
    case 403:
      return 'Permission denied. Your account does not have access to this resource.';
    case 404:
      return 'The requested resource was not found. It may have been removed or is not yet deployed.';
    case 409:
      // Bug-8712 — DEPLOYED_SNAPSHOT_INVALID. The add-in reads the model's
      // PUBLISHED definitions; when the published version cannot be read there
      // is nothing it is allowed to serve. Fail closed with a plain instruction
      // and never fall back to the live draft (Bug-8384 made this deliberate).
      // The raw server detail is deliberately not shown — it names internal
      // snapshot state and gives the user nothing to act on.
      return 'The published version of this model is unavailable. Ask a modeller to deploy the model again, then refresh.';
    case 422:
      return `Validation error: ${err.message}`;
    case 429:
      return 'Too many requests. Please wait a moment and try again.';
    case 500:
    case 502:
    case 503: {
      // L-1: 502s from the source carry an actionable sanitized detail (e.g.
      // "invalid input syntax for type bigint") that "try again" hides — retry
      // cannot help. Surface the detail when the server gave one; otherwise fall
      // back to the generic message. The ApiError message defaults to "API error
      // <status>" when no detail was present, so only show a real reason.
      const hasDetail = err.message && err.message !== `API error ${err.status}`;
      return hasDetail
        ? `Query could not be processed: ${err.message}`
        : 'A server error occurred. Please try again or contact support if the issue persists.';
    }
    default:
      return err.message || 'An unexpected error occurred.';
  }
}
