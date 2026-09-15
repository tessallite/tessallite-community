/**
 * Serve a built pane bundle to the Playwright harness.
 *
 * Forty lines instead of a dependency: the harness needs one directory served
 * over plain HTTP with no caching, which is exactly what `vite preview` would
 * do except that it also applies this repo's dev-certificate and base-path
 * configuration. Serving it here keeps the harness independent of those.
 */
import http from 'node:http';
import https from 'node:https';
import { createReadStream } from 'node:fs';
import { stat } from 'node:fs/promises';
import { extname, join, normalize, resolve } from 'node:path';

const TYPES = {
  '.html': 'text/html; charset=utf-8',
  '.js': 'text/javascript; charset=utf-8',
  '.mjs': 'text/javascript; charset=utf-8',
  '.css': 'text/css; charset=utf-8',
  '.json': 'application/json; charset=utf-8',
  '.svg': 'image/svg+xml',
  '.png': 'image/png',
  '.ico': 'image/x-icon',
  '.woff2': 'font/woff2',
  '.xml': 'text/xml; charset=utf-8',
};

/**
 * `tls` and `host` let a caller serve over HTTPS on a specific address rather
 * than plain HTTP on 127.0.0.1, which the Playwright harness (the only
 * current caller) does not need.
 */
export async function startStaticServer({
  root, port, host = '127.0.0.1', tls = null, mountPath = '/',
}) {
  const rootDir = resolve(root);
  // The bundle is built with a Vite `base` ('/excel-plugin/' for the add-in),
  // so index.html, functions.json and the manifest all name that prefix.
  // Serving the folder at '/' answers 404 to every one of them and Office
  // reports "couldn't download resource". Mount the folder where the build
  // expects it; anything outside the mount is not ours.
  const mount = '/' + mountPath.replace(/^\/+|\/+$/g, '');
  const mountPrefix = mount === '/' ? '' : mount;

  const handler = async (req, res) => {
    const requested = decodeURIComponent((req.url || '/').split('?')[0]);
    if (mountPrefix && !(requested === mountPrefix || requested.startsWith(mountPrefix + '/'))) {
      res.statusCode = 404;
      res.end('not found');
      return;
    }
    const relative = normalize(requested.slice(mountPrefix.length) || '/').replace(/^(\.\.[/\\])+/, '');
    let filePath = join(rootDir, relative);
    if (!filePath.startsWith(rootDir)) {
      res.statusCode = 403;
      res.end('forbidden');
      return;
    }
    try {
      const info = await stat(filePath);
      if (info.isDirectory()) filePath = join(filePath, 'index.html');
      await stat(filePath);
    } catch {
      res.statusCode = 404;
      res.end('not found');
      return;
    }
    res.setHeader('content-type', TYPES[extname(filePath)] ?? 'application/octet-stream');
    // The add-in's own runtime files must never be cached; the harness follows
    // the same rule so a rebuilt bundle is never served stale.
    res.setHeader('cache-control', 'no-store');
    createReadStream(filePath).pipe(res);
  };

  const server = tls ? https.createServer(tls, handler) : http.createServer(handler);

  await new Promise((resolveListen, reject) => {
    server.once('error', reject);
    server.listen(port, host, resolveListen);
  });

  return {
    url: `${tls ? 'https' : 'http'}://${host}:${server.address().port}`,
    async close() { await new Promise(done => server.close(done)); },
  };
}
