/**
 * TEST PROFILE build (deliverable (d) of
 * `docs/architecture/architecture_excel-plugin-test-harness.md`).
 *
 * A copy of the add-in with the login removed. When `VITE_TESSALLITE_TEST_PROFILE=1`
 * is set at build time, `vite.config.ts` bakes a preset profile — server URL,
 * tenant, credentials, project and model — into `__TESSALLITE_TEST_PROFILE__`,
 * and this module signs the pane in from it before React mounts. Both the pane
 * and the custom-functions runtime share that session, because both read the
 * same `OfficeRuntime.storage` keys, so a workbook calculates with no pane
 * interaction at all.
 *
 * In every ordinary build `__TESSALLITE_TEST_PROFILE__` is `null`, so
 * `isTestProfileBuild` is a constant `false` and every branch below is dead
 * code the bundler removes. The build itself is refused on a release target by
 * `scripts/testProfileGuard.mjs`.
 */
import { login as apiLogin } from './api/auth';
import { configureApiClient } from './api/client';
import { getProjects, getModels } from './api/modelService';
import { setJwt, setSessionProfile, setModelContext, bumpCacheGeneration, type ConnectionProfile } from './utils/storage';

export interface TestProfileDefinition {
  serverUrl: string;
  tenantId: string;
  email: string;
  password: string;
  /** Project slug or display name, resolved to an id at start-up. */
  project: string;
  /** Model slug or display name, resolved to an id at start-up. */
  model: string;
}

declare const __TESSALLITE_TEST_PROFILE__: TestProfileDefinition | null | undefined;

/**
 * Stamped into the bundle and shown in the pane header. A build carrying this
 * string is a test build and can never be mistaken for a shipping one.
 */
export const TEST_BUILD_MARKER = 'TESSALLITE TEST BUILD - NOT FOR RELEASE';

const definition: TestProfileDefinition | null =
  typeof __TESSALLITE_TEST_PROFILE__ === 'undefined' ? null : (__TESSALLITE_TEST_PROFILE__ ?? null);

export const isTestProfileBuild: boolean = definition !== null;

interface ResolvedContext {
  projectId: string;
  modelId: string;
}

let resolved: ResolvedContext | null = null;
let failure: string | null = null;

/** The reason the preset sign-in failed, for the header marker. Null when fine. */
export function testProfileFailure(): string | null {
  return failure;
}

/**
 * The project the pane should open on. Returns `fallback` unchanged outside a
 * test build, so the shipping bootstrap is untouched.
 */
export function preferredProject<T extends { id: string }>(projects: T[], fallback: T): T {
  if (!resolved) return fallback;
  return projects.find(p => p.id === resolved!.projectId) ?? fallback;
}

/** The model the pane should open on. `fallback` unchanged outside a test build. */
export function preferredModel<T extends { id: string }>(models: T[], fallback: T): T {
  if (!resolved) return fallback;
  return models.find(m => m.id === resolved!.modelId) ?? fallback;
}

function match<T extends { id: string; name?: string; slug?: string }>(items: T[], want: string): T | undefined {
  return items.find(i => i.slug === want) ?? items.find(i => i.name === want) ?? items.find(i => i.id === want);
}

/**
 * Sign in from the baked profile and seed the storage keys a signed-in pane
 * writes. Resolves either way: a failure is reported through the header marker
 * rather than thrown, so the pane still renders and says what went wrong.
 */
export async function applyTestProfile(): Promise<void> {
  if (!definition) return;

  // eslint-disable-next-line no-console
  console.warn(`[Tessallite] ${TEST_BUILD_MARKER} — preset profile for ${definition.email}@${definition.tenantId}`);

  try {
    configureApiClient(definition.serverUrl);
    const response = await apiLogin({
      tenant_id: definition.tenantId,
      email: definition.email,
      password: definition.password,
    });
    const token = response.access_token || '';
    if (!token) throw new Error('login returned no access_token');
    await setJwt(token);

    const profile: ConnectionProfile = {
      id: `${definition.tenantId}:${definition.email}`,
      name: `${definition.email} (test profile)`,
      serverUrl: definition.serverUrl,
      tenantId: definition.tenantId,
      email: definition.email,
      createdAt: new Date().toISOString(),
    };
    await setSessionProfile(profile);

    const projects = await getProjects();
    const project = match(projects, definition.project);
    if (!project) {
      throw new Error(`project "${definition.project}" not found (have: ${projects.map(p => p.name).join(', ')})`);
    }
    const models = await getModels(project.id);
    const model = match(models, definition.model);
    if (!model) {
      throw new Error(`model "${definition.model}" not found (have: ${models.map(m => m.slug ?? m.name).join(', ')})`);
    }

    await setModelContext(project.id, model.id, model.slug, model.name);
    await bumpCacheGeneration();
    resolved = { projectId: project.id, modelId: model.id };
  } catch (e) {
    failure = e instanceof Error ? e.message : String(e);
    // eslint-disable-next-line no-console
    console.error('[Tessallite] test profile sign-in failed:', failure);
  }
}
