/**
 * Build guard for the TEST PROFILE build of the add-in.
 *
 * The test profile ships a bundle that signs ITSELF in from baked-in
 * credentials and skips the login screen. That is exactly what a harness on a
 * test machine needs and exactly what must never leave the building. This
 * module is the single rule that decides whether such a build is allowed to be
 * produced; `vite.config.ts` calls it, and `src/__tests__/testProfileGuard.test.ts`
 * asserts it, so the rule cannot drift between the build and the suite.
 *
 * Plain JS/ESM on purpose: `vite.config.ts` (Node, pre-transpile) and Vitest
 * (browser-ish) both import this exact file. There is no second copy.
 */

/** The flag that turns the test profile on. */
export const TEST_PROFILE_FLAG = 'VITE_TESSALLITE_TEST_PROFILE';

/**
 * Environment variables that mark a build as a RELEASE build. The test profile
 * is refused outright when any of them carries a value.
 */
export const RELEASE_MARKER_VARS = [
  'TESSALLITE_RELEASE',
  'TESSALLITE_RELEASE_CHANNEL',
  'TESSALLITE_RELEASE_VERSION',
  'TESSALLITE_EDITION',
];

/** Hostnames that identify a developer/test machine rather than a deployment. */
const LOCAL_HOSTS = new Set(['localhost', '127.0.0.1', '::1', '0.0.0.0', '[::1]']);

export function isTestProfileRequested(env) {
  const raw = String(env?.[TEST_PROFILE_FLAG] ?? '').trim().toLowerCase();
  return raw === '1' || raw === 'true' || raw === 'yes';
}

/**
 * A base URL is "production" unless it is unambiguously a local or private
 * address. Unparsable values count as production: the guard fails CLOSED,
 * because the failure mode it exists to prevent is a test bundle on a real
 * deployment.
 */
export function isProductionBaseUrl(baseUrl) {
  const value = String(baseUrl ?? '').trim();
  if (!value) return false;
  let host;
  try {
    host = new URL(value).hostname.toLowerCase();
  } catch {
    return true;
  }
  if (LOCAL_HOSTS.has(host)) return false;
  if (host.endsWith('.local') || host.endsWith('.localhost')) return false;
  if (/^10\.\d{1,3}\.\d{1,3}\.\d{1,3}$/.test(host)) return false;
  if (/^192\.168\.\d{1,3}\.\d{1,3}$/.test(host)) return false;
  if (/^172\.(1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3}$/.test(host)) return false;
  return true;
}

/**
 * Throw when a test-profile build is being produced from a release target.
 * A no-op for every ordinary build, so the normal path is untouched.
 *
 * @param {Record<string, string | undefined>} env process environment
 * @returns {boolean} true when the test profile is enabled and permitted
 */
export function assertTestProfileBuildAllowed(env) {
  if (!isTestProfileRequested(env)) return false;

  const markers = RELEASE_MARKER_VARS.filter(name => String(env?.[name] ?? '').trim() !== '');
  if (markers.length > 0) {
    throw new Error(
      `${TEST_PROFILE_FLAG} is set together with release marker(s) ${markers.join(', ')}. ` +
      'A test-profile bundle signs itself in from baked-in credentials and must never be ' +
      'produced from a release target. Unset one or the other.',
    );
  }

  const baseUrl = env?.PLUGIN_BASE_URL;
  if (isProductionBaseUrl(baseUrl)) {
    throw new Error(
      `${TEST_PROFILE_FLAG} is set together with a production PLUGIN_BASE_URL (${baseUrl}). ` +
      'A test-profile bundle signs itself in from baked-in credentials and must never be ' +
      'served from a deployment. Use a localhost or private-network base URL.',
    );
  }

  return true;
}
