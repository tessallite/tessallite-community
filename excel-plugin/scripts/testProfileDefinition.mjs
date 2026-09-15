/**
 * Read the TEST PROFILE build inputs out of the environment.
 *
 * Deliberately the SAME variable names the phase-1 harnesses already use
 * (`TESS_HARNESS_*`, see `tests-harness/README.md`), so one exported
 * environment drives the headless harness, the Playwright pane harness and the
 * test build. Project and model default to the harness fixture names in
 * `tests-harness/harness.config.json`.
 *
 * Credentials are read from the environment ONLY. Nothing here is committed,
 * and `scripts/testProfileGuard.mjs` refuses the build on a release target.
 */
import { readFileSync } from 'node:fs';
import { join } from 'node:path';

export const TEST_PROFILE_ENV_VARS = [
  'TESS_HARNESS_SERVER_URL',
  'TESS_HARNESS_TENANT',
  'TESS_HARNESS_EMAIL',
  'TESS_HARNESS_PASSWORD',
];

function required(env, name) {
  const value = String(env?.[name] ?? '').trim();
  if (!value) {
    throw new Error(
      `A test-profile build needs ${name}. Required: ${TEST_PROFILE_ENV_VARS.join(', ')} ` +
      '(see tessallite/excel-plugin/tests-harness/README.md).',
    );
  }
  return value;
}

/**
 * @param {Record<string, string | undefined>} env
 * @param {string} pluginDir absolute path to tessallite/excel-plugin
 */
export function resolveTestProfileDefinition(env, pluginDir) {
  let fixtures = { project: 'project1', model: 'modely' };
  try {
    const raw = readFileSync(join(pluginDir, 'tests-harness', 'harness.config.json'), 'utf8');
    const parsed = JSON.parse(raw);
    fixtures = { project: parsed.project ?? fixtures.project, model: parsed.model ?? fixtures.model };
  } catch {
    // Fall back to the defaults above; the explicit env vars still win.
  }

  return {
    serverUrl: required(env, 'TESS_HARNESS_SERVER_URL').replace(/\/$/, ''),
    tenantId: required(env, 'TESS_HARNESS_TENANT'),
    email: required(env, 'TESS_HARNESS_EMAIL'),
    password: required(env, 'TESS_HARNESS_PASSWORD'),
    project: String(env?.TESS_HARNESS_PROJECT ?? '').trim() || fixtures.project,
    model: String(env?.TESS_HARNESS_MODEL ?? '').trim() || fixtures.model,
  };
}
