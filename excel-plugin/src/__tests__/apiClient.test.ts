import { describe, it, expect, beforeEach, vi } from 'vitest';

const mockStorage: Record<string, string> = {};

beforeEach(() => {
  Object.keys(mockStorage).forEach(k => delete mockStorage[k]);
  mockStorage['tessallite_jwt'] = 'test-jwt';
  vi.restoreAllMocks();
});

function stubOfficeRuntime() {
  (globalThis as Record<string, unknown>).OfficeRuntime = {
    storage: {
      getItem: async (key: string) => mockStorage[key] ?? null,
      setItem: async (key: string, value: string) => { mockStorage[key] = value; },
      removeItem: async (key: string) => { delete mockStorage[key]; },
    },
  };
}

beforeEach(() => {
  stubOfficeRuntime();
});

import { apiClient, configureApiClient, ApiError, formatApiError } from '../api/client';

describe('apiClient', () => {
  beforeEach(() => {
    configureApiClient('https://test.example.com');
  });

  it('sends GET with Authorization header', async () => {
    const fetchMock = vi.spyOn(globalThis, 'fetch').mockResolvedValueOnce(
      new Response(JSON.stringify({ ok: true }), { status: 200 }),
    );
    await apiClient.get('/test');
    expect(fetchMock).toHaveBeenCalledWith(
      'https://test.example.com/test',
      expect.objectContaining({
        method: 'GET',
        headers: expect.objectContaining({ Authorization: 'Bearer test-jwt' }),
      }),
    );
  });

  it('sends POST with JSON body', async () => {
    const fetchMock = vi.spyOn(globalThis, 'fetch').mockResolvedValueOnce(
      new Response(JSON.stringify({ id: 1 }), { status: 200 }),
    );
    await apiClient.post('/test', { name: 'hello' });
    expect(fetchMock).toHaveBeenCalledWith(
      'https://test.example.com/test',
      expect.objectContaining({
        method: 'POST',
        body: JSON.stringify({ name: 'hello' }),
      }),
    );
  });

  it('throws ApiError on 401 and removes JWT', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValueOnce(
      new Response(JSON.stringify({ detail: 'Session expired' }), { status: 401 }),
    );
    await expect(apiClient.get('/test')).rejects.toThrow('Session expired');
    expect(mockStorage['tessallite_jwt']).toBeUndefined();
  });

  it('throws ApiError on 403 with detail', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValueOnce(
      new Response(JSON.stringify({ detail: 'Permission denied' }), { status: 403 }),
    );
    await expect(apiClient.get('/test')).rejects.toThrow('Permission denied');
  });

  it('throws ApiError on 422 with validation detail', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValueOnce(
      new Response(JSON.stringify({ detail: 'Invalid input' }), { status: 422 }),
    );
    await expect(apiClient.get('/test')).rejects.toThrow('Invalid input');
  });

  it('throws ApiError on 500 without retry for POST', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValueOnce(
      new Response(JSON.stringify({ detail: 'Internal server error' }), { status: 500 }),
    );
    await expect(apiClient.post('/test')).rejects.toThrow('Internal server error');
  });

  it('retries GET on 500 up to 3 times', async () => {
    const fetchMock = vi.spyOn(globalThis, 'fetch')
      .mockResolvedValueOnce(new Response('Error', { status: 500 }))
      .mockResolvedValueOnce(new Response('Error', { status: 500 }))
      .mockResolvedValueOnce(new Response(JSON.stringify({ ok: true }), { status: 200 }));
    const result = await apiClient.get('/test');
    expect(result).toEqual({ ok: true });
    expect(fetchMock).toHaveBeenCalledTimes(3);
  });

  it('does not retry mutations on 500', async () => {
    const fetchMock = vi.spyOn(globalThis, 'fetch')
      .mockResolvedValueOnce(new Response('Error', { status: 500 }));
    await expect(apiClient.put('/test', {})).rejects.toThrow();
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });
});

describe('formatApiError', () => {
  it('formats 401 as session expired', () => {
    expect(formatApiError(new ApiError(401, {}))).toContain('Session expired');
  });

  it('formats 403 as permission denied', () => {
    expect(formatApiError(new ApiError(403, {}))).toContain('Permission denied');
  });

  it('formats 404 as not found', () => {
    expect(formatApiError(new ApiError(404, {}))).toContain('not found');
  });

  it('formats 422 as validation error', () => {
    expect(formatApiError(new ApiError(422, { detail: 'Bad input' }))).toContain('Bad input');
  });

  it('formats 429 as rate limit', () => {
    expect(formatApiError(new ApiError(429, {}))).toContain('Too many requests');
  });

  it('formats 500 as server error', () => {
    expect(formatApiError(new ApiError(500, {}))).toContain('server error');
  });

  // L-4: pydantic body-validation 422s carry `detail` as a list of objects;
  // it must render readably, never "[object Object]".
  it('renders a list-shaped 422 detail readably (no [object Object])', () => {
    const err = new ApiError(422, {
      detail: [
        { loc: ['body', 'kpi_ids'], msg: 'field required', type: 'value_error.missing' },
        { loc: ['body', 'limit'], msg: 'value is not a valid integer', type: 'type_error.integer' },
      ],
    });
    const out = formatApiError(err);
    expect(out).not.toContain('[object Object]');
    expect(out).toContain('field required');
    expect(out).toContain('value is not a valid integer');
  });

  // L-1: a 502 carrying an actionable source detail surfaces that reason rather
  // than the generic "try again" message.
  it('surfaces an actionable 502 detail instead of the generic retry message', () => {
    const err = new ApiError(502, { detail: 'invalid input syntax for type bigint: "abc"' });
    const out = formatApiError(err);
    expect(out).toContain('invalid input syntax for type bigint');
    expect(out).not.toContain('Please try again');
  });

  it('falls back to the generic message for a 502 with no detail', () => {
    expect(formatApiError(new ApiError(502, {}))).toContain('server error');
  });
});
