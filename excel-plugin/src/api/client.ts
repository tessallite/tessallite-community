/**
 * Core API client for Tessallite services.
 * All calls use JWT bearer authentication via OfficeRuntime.storage.
 */

import { getJwt, removeJwt } from '../utils/storage';
import { logApiEvent, logError } from '../utils/diagnostics';

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

async function request<T>(
  method: string,
  path: string,
  body?: unknown,
  retries = 0,
): Promise<T> {
  const jwt = await getJwt();
  const headers: Record<string, string> = {
    'Content-Type': 'application/json',
  };
  if (jwt) {
    headers['Authorization'] = `Bearer ${jwt}`;
  }

  const startMs = Date.now();
  const res = await fetch(`${baseUrl}${path}`, {
    method,
    headers,
    body: body ? JSON.stringify(body) : undefined,
  });
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
      const delay = BASE_DELAY_MS * Math.pow(2, retries);
      await new Promise(r => setTimeout(r, delay));
      return request<T>(method, path, body, retries + 1);
    }

    let errorBody: unknown;
    try {
      const text = await res.text();
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
  const text = await res.text();
  if (!text) {
    return undefined as T;
  }
  return JSON.parse(text) as T;
}

function safeGet<T>(path: string): Promise<T> {
  return request<T>('GET', path);
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
): Promise<Response> {
  const jwt = await getJwt();
  const headers: Record<string, string> = {
    'Content-Type': 'application/json',
  };
  if (jwt) {
    headers['Authorization'] = `Bearer ${jwt}`;
  }

  const res = await fetch(`${baseUrl}${path}`, {
    method: 'POST',
    headers,
    body: JSON.stringify(body),
    signal,
  });

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

export function formatApiError(err: ApiError): string {
  switch (err.status) {
    case 401:
      return 'Session expired. Please sign in again.';
    case 403:
      return 'Permission denied. Your account does not have access to this resource.';
    case 404:
      return 'The requested resource was not found. It may have been removed or is not yet deployed.';
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
