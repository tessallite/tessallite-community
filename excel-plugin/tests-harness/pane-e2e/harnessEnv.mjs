/**
 * Shared, fixed endpoints for the Playwright pane harness.
 *
 * Fixed rather than ephemeral because the pane bundle is BUILT with the origin
 * baked into it (the test profile), so the build step and the server the tests
 * start have to agree on a URL without passing anything between processes.
 * Every value is overridable from the environment for a machine where the
 * defaults collide.
 */
export const ORIGIN_PORT = Number(process.env.TESS_HARNESS_ORIGIN_PORT || 39017);
export const PANE_PORT = Number(process.env.TESS_HARNESS_PANE_PORT || 39018);

/** Where the pane will reach Tessallite: a real origin, or the local shim. */
export function serverUrl() {
  return process.env.TESS_HARNESS_SERVER_URL || `http://127.0.0.1:${ORIGIN_PORT}`;
}

/** Where the built test-profile pane is served from. */
export function paneUrl() {
  return `http://127.0.0.1:${PANE_PORT}/index.html`;
}

/** The directory the test-profile build writes to (never `dist/`). */
export const TEST_PROFILE_OUT_DIR = 'dist-test-profile';

/**
 * Credentials and stack endpoints, from the environment only. Throws with the
 * exact variable name so a missing value is a one-line fix, not a mystery.
 */
export function requireEnv(name, fallback) {
  const v = process.env[name];
  if (v === undefined || v === '') {
    if (fallback === undefined) {
      throw new Error(`Environment variable ${name} is required. See tests-harness/README.md.`);
    }
    return fallback;
  }
  return v;
}

/** True when the local stack details needed to run at all are present. */
export function stackConfigured() {
  const haveCreds = ['TESS_HARNESS_TENANT', 'TESS_HARNESS_EMAIL', 'TESS_HARNESS_PASSWORD']
    .every(n => (process.env[n] ?? '') !== '');
  const haveOrigin = (process.env.TESS_HARNESS_SERVER_URL ?? '') !== ''
    || (process.env.TESS_HARNESS_QUERY_ROUTER_URL ?? '') !== '';
  return haveCreds && haveOrigin;
}
