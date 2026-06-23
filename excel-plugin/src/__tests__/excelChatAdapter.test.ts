/**
 * Contract tests for createExcelAdapter — verifies that the Excel adapter
 * sends the correct field names and HTTP methods to the backend, and that
 * the abort signal reaches the transport layer.
 */
import { describe, it, expect, beforeEach, vi } from 'vitest';
import type { MockInstance } from 'vitest';

const mockStorage: Record<string, string> = {};

beforeEach(() => {
  Object.keys(mockStorage).forEach(k => delete mockStorage[k]);
  mockStorage['tessallite_jwt'] = 'test-jwt';
  vi.restoreAllMocks();
  (globalThis as Record<string, unknown>).OfficeRuntime = {
    storage: {
      getItem: async (key: string) => mockStorage[key] ?? null,
      setItem: async (key: string, value: string) => { mockStorage[key] = value; },
      removeItem: async (key: string) => { delete mockStorage[key]; },
    },
  };
});

import { configureApiClient } from '../api/client';
import { createExcelAdapter } from '../api/agentChatAdapter';

beforeEach(() => {
  configureApiClient('https://test.example.com');
});

type FetchMock = MockInstance<Parameters<typeof fetch>, ReturnType<typeof fetch>>;

function mockPost(responseBody: unknown, status = 200): FetchMock {
  return vi.spyOn(globalThis, 'fetch').mockResolvedValueOnce(
    new Response(JSON.stringify(responseBody), { status }),
  );
}

function lastBody(fetchMock: FetchMock): Record<string, unknown> {
  const [, init] = fetchMock.mock.calls[0];
  return JSON.parse(init?.body as string);
}

function lastMethod(fetchMock: FetchMock): string {
  const [, init] = fetchMock.mock.calls[0];
  return init?.method as string;
}

const CONV_FIXTURE = {
  id: 'conv-1',
  project_id: 'p1',
  title: null,
  pinned_at: null,
  created_at: '2026-01-01T00:00:00Z',
  last_active_at: '2026-01-01T00:00:00Z',
  deleted_at: null,
  persona_id: null,
  pinned_model_id: null,
};

describe('createExcelAdapter', () => {
  describe('createConversation', () => {
    it('sends activeModelId as pinned_model_id, not model_id', async () => {
      const adapter = createExcelAdapter(() => 'm-support');
      const fetchMock = mockPost({ ...CONV_FIXTURE, pinned_model_id: 'm-support' });

      await adapter.createConversation('p1');

      const body = lastBody(fetchMock);
      expect(body).toHaveProperty('pinned_model_id', 'm-support');
      expect(body).not.toHaveProperty('model_id');
    });

    it('prefers pinnedModelId from options over activeModelId', async () => {
      const adapter = createExcelAdapter(() => 'm-default');
      const fetchMock = mockPost({ ...CONV_FIXTURE, pinned_model_id: 'm-override' });

      await adapter.createConversation('p1', { pinnedModelId: 'm-override' });

      const body = lastBody(fetchMock);
      expect(body).toHaveProperty('pinned_model_id', 'm-override');
    });

    it('includes persona_id when provided via options', async () => {
      const adapter = createExcelAdapter(() => null);
      const fetchMock = mockPost({ ...CONV_FIXTURE, persona_id: 'persona-emea' });

      await adapter.createConversation('p1', { personaId: 'persona-emea' });

      const body = lastBody(fetchMock);
      expect(body).toHaveProperty('persona_id', 'persona-emea');
    });

    it('maps pinned_model_id from backend response', async () => {
      const adapter = createExcelAdapter(() => 'm-sales');
      mockPost({ ...CONV_FIXTURE, pinned_model_id: 'm-sales' });

      const result = await adapter.createConversation('p1');

      expect(result.pinned_model_id).toBe('m-sales');
    });
  });

  describe('updateConversation', () => {
    it('uses PATCH method, not POST', async () => {
      const adapter = createExcelAdapter(() => null);
      const fetchMock = mockPost({ ...CONV_FIXTURE, title: 'Q4 revenue' });

      await adapter.updateConversation('p1', 'conv-1', { title: 'Q4 revenue' });

      expect(lastMethod(fetchMock)).toBe('PATCH');
    });
  });

  describe('streamMessageRaw', () => {
    it('passes AbortSignal to fetch', async () => {
      const adapter = createExcelAdapter(() => null);
      const controller = new AbortController();
      const fetchMock = vi.spyOn(globalThis, 'fetch').mockResolvedValueOnce(
        new Response('data: {}\n\n', { status: 200 }),
      );

      await adapter.streamMessageRaw('p1', 'conv-1', 'hello', controller.signal);

      const [, init] = fetchMock.mock.calls[0];
      expect(init?.signal).toBe(controller.signal);
    });
  });
});
