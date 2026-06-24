import { describe, it, expect, beforeEach } from 'vitest';
import { getDiagnosticsReport, logApiEvent, logError, logInfo, clearDiagnostics } from '../utils/diagnostics';
import { saveProfile, getProfiles, removeProfile, getJwt, setJwt, removeJwt } from '../utils/storage';

const mockStorage: Record<string, string> = {};

beforeEach(() => {
  Object.keys(mockStorage).forEach(k => delete mockStorage[k]);
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

describe('diagnostics redaction', () => {
  it('redacts passwords from event log', () => {
    clearDiagnostics();
    logError('User login failed: password=secret123');
    const report = getDiagnosticsReport();
    expect(report).not.toContain('secret123');
    expect(report).toContain('[REDACTED]');
  });

  it('redacts JWT tokens from event log', () => {
    clearDiagnostics();
    logError('Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dummy');
    const report = getDiagnosticsReport();
    expect(report).not.toContain('eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9');
    expect(report).toContain('[REDACTED]');
  });

  it('redacts connection strings containing passwords', () => {
    clearDiagnostics();
    logError('Connection failed: Provider=MSOLAP.8;Data Source=https://test;Password=mypassword123;Catalog=test');
    const report = getDiagnosticsReport();
    expect(report).not.toContain('mypassword123');
    expect(report).toContain('[REDACTED]');
  });

  it('redacts JSON secret keys in log messages', () => {
    clearDiagnostics();
    logError(JSON.stringify({ access_token: 'abc123', api_key: 'key-456', error: 'auth' }));
    const report = getDiagnosticsReport();
    expect(report).not.toContain('abc123');
    expect(report).not.toContain('key-456');
    expect(report).toContain('[REDACTED]');
  });

  it('redacts API auth URLs in logError', () => {
    clearDiagnostics();
    logError('POST /api/v1/auth/login failed');
    const report = getDiagnosticsReport();
    expect(report).toContain('[REDACTED]');
    expect(report).not.toContain('/api/v1/auth/login');
  });

  it('shortens API paths in logApiEvent', () => {
    clearDiagnostics();
    logApiEvent('/api/v1/auth/login', 200, 50);
    const report = getDiagnosticsReport();
    expect(report).toContain('/api/v1/[REDACTED]');
    expect(report).not.toContain('/api/v1/auth/login');
  });

  it('clearDiagnostics empties the event log', () => {
    logInfo('test event 1');
    logInfo('test event 2');
    expect(getDiagnosticsReport()).toContain('test event 1');
    clearDiagnostics();
    const report = getDiagnosticsReport();
    expect(report).not.toContain('test event 1');
  });
});

describe('storage security', () => {
  it('never writes password to storage', async () => {
    stubOfficeRuntime();
    await saveProfile({
      id: 'test:id',
      name: 'Test',
      serverUrl: 'https://test',
      tenantId: 'test-tenant',
      email: 'test@test.com',
      createdAt: new Date().toISOString(),
    });
    const raw = mockStorage['tessallite_profiles'];
    expect(raw).toBeDefined();
    expect(raw).not.toContain('password');
    const parsed = JSON.parse(raw!);
    expect(parsed[0]).not.toHaveProperty('password');
  });

  it('JWT is stored and retrieved via dedicated key', async () => {
    stubOfficeRuntime();
    await setJwt('test-jwt-token');
    const jwt = await getJwt();
    expect(jwt).toBe('test-jwt-token');
    await removeJwt();
    const removed = await getJwt();
    expect(removed).toBeNull();
  });

  it('removeProfile does not leave dangling data', async () => {
    stubOfficeRuntime();
    await saveProfile({
      id: 'test:1', name: 'P1', serverUrl: 'https://1', tenantId: 't1', email: 'p1@t.com', createdAt: '',
    });
    await saveProfile({
      id: 'test:2', name: 'P2', serverUrl: 'https://2', tenantId: 't2', email: 'p2@t.com', createdAt: '',
    });
    await removeProfile('test:1');
    const profiles = await getProfiles();
    expect(profiles).toHaveLength(1);
    expect(profiles[0].id).toBe('test:2');
  });
});
