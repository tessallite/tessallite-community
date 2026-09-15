/**
 * Single-origin shim.
 *
 * A deployed Tessallite serves the add-in ONE origin: metadata routes reach
 * model-service and `/api/v1/plugin/*` reaches query-router. The add-in stores
 * exactly one `serverUrl` and depends on that. On a bare local docker stack the
 * two services are separate hosts, so this shim reproduces the deployment's
 * routing rather than making the harness pretend the add-in can hold two URLs.
 *
 * Set `TESS_HARNESS_SERVER_URL` instead when a real single origin exists; the
 * shim is then not started.
 *
 * Two options exist for the harnesses built on top of it:
 *  - `cors`: the Playwright pane harness loads the add-in from a static server
 *    on a different port, so the browser needs the headers a deployment's own
 *    same-origin serving makes unnecessary.
 *  - `fault`: fault injection, for two server states this stack cannot be made
 *    to produce. `stall` accepts the request and never answers, which is the
 *    only way to prove the add-in's own 30s request ceiling (Bug-9749) rather
 *    than the server's timeout. `deny-all` proxies the REAL query and then
 *    stamps the row-security deny-all sentinel onto the real response, which is
 *    the only way to reach the Bug-8453 branch without a deny-all persona in
 *    the demo seed. Both inject a SERVER STATE and leave the add-in — the code
 *    actually under test — untouched.
 */
import http from 'node:http';

const PLUGIN_PREFIX = '/api/v1/plugin';

/**
 * Must equal `shared.security.execute_contract.ROW_SECURITY_DENY_ALL_RULE_ID`,
 * mirrored in `src/utils/rowSecurity.ts`. Repeated here rather than imported:
 * this file is plain ESM for Node and the add-in module is TypeScript.
 */
export const DENY_ALL_RULE_ID = '__deny_all__';

/**
 * @param {object} options
 * @param {string} options.modelServiceUrl
 * @param {string} options.queryRouterUrl
 * @param {number} [options.port] fixed port; 0/undefined picks an ephemeral one
 * @param {boolean} [options.cors] send permissive CORS headers
 */
export async function startOriginShim({ modelServiceUrl, queryRouterUrl, port, cors }) {
  /**
   * Path fragment -> fault mode, consulted per request. `null` clears it.
   * Set through the returned handle so a check can arm a fault, make one call
   * and disarm it without restarting the shim.
   * @type {{ pathFragment: string, mode: 'stall' | 'deny-all' } | null}
   */
  let fault = null;
  const stalled = new Set();

  const server = http.createServer((req, res) => {
    if (cors) {
      res.setHeader('access-control-allow-origin', '*');
      res.setHeader('access-control-allow-headers', '*');
      res.setHeader('access-control-allow-methods', 'GET,POST,PUT,PATCH,DELETE,OPTIONS');
      res.setHeader('access-control-max-age', '600');
    }
    if (req.method === 'OPTIONS') {
      res.statusCode = 204;
      res.end();
      return;
    }

    const injectDenyAll = fault?.mode === 'deny-all' && req.url.includes(fault.pathFragment);

    if (fault?.mode === 'stall' && req.url.includes(fault.pathFragment)) {
      // Accept the request, consume the body, answer never. The socket is held
      // open until the shim is closed, so the add-in's own ceiling is what
      // settles the cell.
      req.resume();
      stalled.add(res);
      return;
    }

    const base = req.url.startsWith(PLUGIN_PREFIX) ? queryRouterUrl : modelServiceUrl;
    const target = base.replace(/\/$/, '') + req.url;

    const chunks = [];
    req.on('data', c => chunks.push(c));
    req.on('error', () => { res.statusCode = 502; res.end('{"detail":"shim request error"}'); });
    req.on('end', async () => {
      const headers = { ...req.headers };
      delete headers.host;
      delete headers['content-length'];
      delete headers.connection;
      try {
        const upstream = await fetch(target, {
          method: req.method,
          headers,
          body: ['GET', 'HEAD'].includes(req.method) || chunks.length === 0
            ? undefined
            : Buffer.concat(chunks),
        });
        let body = Buffer.from(await upstream.arrayBuffer());
        res.statusCode = upstream.status;
        const ct = upstream.headers.get('content-type');
        if (ct) res.setHeader('content-type', ct);
        if (injectDenyAll && upstream.ok && ct?.includes('json')) {
          const parsed = JSON.parse(body.toString('utf8'));
          parsed.security_rules_applied = [DENY_ALL_RULE_ID];
          body = Buffer.from(JSON.stringify(parsed), 'utf8');
        }
        res.end(body);
      } catch (e) {
        res.statusCode = 502;
        res.end(JSON.stringify({ detail: `shim upstream error: ${String(e)}` }));
      }
    });
  });

  await new Promise((resolve, reject) => {
    server.once('error', reject);
    server.listen(port ?? 0, '127.0.0.1', resolve);
  });

  const address = server.address();
  return {
    url: `http://127.0.0.1:${address.port}`,
    /** Arm a fault for requests whose URL contains `pathFragment`. */
    injectFault(pathFragment, mode = 'stall') {
      fault = { pathFragment, mode };
    },
    /** Clear the fault and release anything held open by it. */
    clearFault() {
      fault = null;
      for (const res of stalled) {
        try { res.destroy(); } catch { /* already gone */ }
      }
      stalled.clear();
    },
    async close() {
      this.clearFault();
      await new Promise(resolve => server.close(resolve));
    },
  };
}
