/**
 * Start the two servers every pane spec needs, once per run:
 *
 *  - the origin shim, so the pane reaches ONE origin exactly as a deployment
 *    serves it (metadata to model-service, `/api/v1/plugin/*` to query-router),
 *    with the CORS headers a cross-port browser needs;
 *  - a static server for the built test-profile pane.
 *
 * Both listen on the fixed ports in `harnessEnv.mjs`, which is what the bundle
 * was built against.
 */
import { resolve, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';

import { startOriginShim } from '../lib/originShim.mjs';
import { startStaticServer } from '../lib/staticServer.mjs';
import { ORIGIN_PORT, PANE_PORT, TEST_PROFILE_OUT_DIR, requireEnv } from './harnessEnv.mjs';

const HERE = dirname(fileURLToPath(import.meta.url));
const PLUGIN_DIR = resolve(HERE, '../..');

let shim = null;
let pane = null;

export default async function globalSetup() {
  if (!process.env.TESS_HARNESS_SERVER_URL) {
    shim = await startOriginShim({
      modelServiceUrl: requireEnv('TESS_HARNESS_MODEL_SERVICE_URL', 'http://127.0.0.1:8001'),
      queryRouterUrl: requireEnv('TESS_HARNESS_QUERY_ROUTER_URL'),
      port: ORIGIN_PORT,
      cors: true,
    });
  }
  pane = await startStaticServer({
    root: resolve(PLUGIN_DIR, TEST_PROFILE_OUT_DIR),
    port: PANE_PORT,
  });

  return async () => {
    await pane?.close();
    await shim?.close();
  };
}
