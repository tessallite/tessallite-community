import { describe, it, expect, beforeEach } from 'vitest';
import {
  getJwt, setJwt, removeJwt,
  getProfiles, saveProfile, removeProfile,
  getActiveProfile, setActiveProfile,
  clearAllAuthData,
  ConnectionProfile,
} from '../utils/storage';

const mockStorage: Record<string, string> = {};

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
  Object.keys(mockStorage).forEach(k => delete mockStorage[k]);
  stubOfficeRuntime();
});

const testProfile: ConnectionProfile = {
  id: 'tenant:user@test.com',
  name: 'Test User',
  serverUrl: 'https://tessallite.test.com',
  tenantId: 'tenant',
  email: 'user@test.com',
  createdAt: '2026-01-01T00:00:00.000Z',
};

describe('JWT storage', () => {
  it('setJwt stores and getJwt retrieves', async () => {
    await setJwt('my-jwt-token');
    const token = await getJwt();
    expect(token).toBe('my-jwt-token');
  });

  it('removeJwt clears the stored token', async () => {
    await setJwt('token-to-remove');
    await removeJwt();
    const token = await getJwt();
    expect(token).toBeNull();
  });

  it('getJwt returns null when no token stored', async () => {
    const token = await getJwt();
    expect(token).toBeNull();
  });
});

describe('profile storage', () => {
  it('saveProfile stores profile metadata without password', async () => {
    await saveProfile(testProfile);
    const stored = mockStorage['tessallite_profiles'];
    expect(stored).toBeDefined();
    const parsed = JSON.parse(stored!);
    expect(parsed).toHaveLength(1);
    expect(parsed[0]).toEqual({
      id: testProfile.id,
      name: testProfile.name,
      serverUrl: testProfile.serverUrl,
      tenantId: testProfile.tenantId,
      email: testProfile.email,
      createdAt: testProfile.createdAt,
    });
    expect(parsed[0]).not.toHaveProperty('password');
  });

  it('getProfiles returns saved profiles', async () => {
    await saveProfile(testProfile);
    const profiles = await getProfiles();
    expect(profiles).toHaveLength(1);
    expect(profiles[0].email).toBe('user@test.com');
  });

  it('saveProfile updates existing profile by id', async () => {
    await saveProfile(testProfile);
    await saveProfile({ ...testProfile, name: 'Updated Name' });
    const profiles = await getProfiles();
    expect(profiles).toHaveLength(1);
    expect(profiles[0].name).toBe('Updated Name');
  });

  it('removeProfile deletes by id', async () => {
    await saveProfile(testProfile);
    await saveProfile({ ...testProfile, id: 'other', email: 'other@test.com' });
    await removeProfile(testProfile.id);
    const profiles = await getProfiles();
    expect(profiles).toHaveLength(1);
    expect(profiles[0].id).toBe('other');
  });

  it('getProfiles returns empty array when nothing stored', async () => {
    const profiles = await getProfiles();
    expect(profiles).toEqual([]);
  });
});

describe('active profile', () => {
  it('setActiveProfile and getActiveProfile round-trip', async () => {
    await saveProfile(testProfile);
    await setActiveProfile(testProfile.id);
    const active = await getActiveProfile();
    expect(active).not.toBeNull();
    expect(active!.id).toBe(testProfile.id);
  });

  it('getActiveProfile returns null when not set', async () => {
    const active = await getActiveProfile();
    expect(active).toBeNull();
  });

  it('clearAllAuthData removes JWT and profiles', async () => {
    await setJwt('token');
    await saveProfile(testProfile);
    await clearAllAuthData();
    const jwt = await getJwt();
    const profiles = await getProfiles();
    expect(jwt).toBeNull();
    expect(profiles).toEqual([]);
  });
});
