#!/usr/bin/env node
// ===========================================================================
// install-certs.mjs — Generate & install the Office-trusted TLS cert/key the
// Excel add-in needs, then publish them to the nginx mount at
// tessallite/infra/certs/.
//
// Excel only trusts add-in resources whose cert chains to the
// "Developer CA for Microsoft Office Add-ins" CA. A plain self-signed cert
// causes black ribbon icons and a taskpane that refuses to load. This script
// uses `office-addin-dev-certs` (a devDependency) to mint that cert, copies
// the pair into infra/certs/, and verifies the on-disk cert and key are a
// matching pair (modulus / key-pair check) before exiting.
//
// Usage:
//   npm run certs:install                       (from tessallite/excel-plugin)
//   CERTS_DIR=/abs/path node scripts/install-certs.mjs
//   CERT_DAYS=365 node scripts/install-certs.mjs
//
// Exit codes: 0 = certs present & verified, non-zero = failure.
// ===========================================================================
import { spawnSync } from 'node:child_process';
import { X509Certificate, createPrivateKey } from 'node:crypto';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const PLUGIN_DIR = path.resolve(__dirname, '..');

// Where nginx (docker-compose) bind-mounts the cert/key from.
const CERTS_DIR =
  process.env.CERTS_DIR || path.resolve(PLUGIN_DIR, '..', 'infra', 'certs');
const DEST_CRT = path.join(CERTS_DIR, 'localhost.crt');
const DEST_KEY = path.join(CERTS_DIR, 'localhost.key');

// office-addin-dev-certs always writes here; no output-path flags exist.
const SRC_DIR = path.join(os.homedir(), '.office-addin-dev-certs');
const SRC_CRT = path.join(SRC_DIR, 'localhost.crt');
const SRC_KEY = path.join(SRC_DIR, 'localhost.key');

const OFFICE_CA = 'Microsoft Office Add-ins';
const CERT_DAYS = process.env.CERT_DAYS || '365';
const CERT_DOMAINS = process.env.CERT_DOMAINS || '127.0.0.1,localhost';
// Regenerate if the leaf expires within this many days.
const RENEW_WINDOW_DAYS = Number(process.env.CERT_RENEW_WINDOW_DAYS || '14');

const log = (m) => console.log(`     ${m}`);
const fail = (m) => {
  console.error(`\n  [ERROR] ${m}\n`);
  process.exit(1);
};

// ---------------------------------------------------------------------------
// modulus / key-pair check: confirm the cert's public key matches the private
// key. Equivalent to comparing `openssl x509 -modulus` with
// `openssl rsa -modulus`, but without depending on an openssl binary.
// ---------------------------------------------------------------------------
function verifyPair(crtPath, keyPath, label) {
  if (!fs.existsSync(crtPath) || !fs.existsSync(keyPath)) {
    return { ok: false, reason: 'cert or key file missing' };
  }
  let cert;
  let key;
  try {
    cert = new X509Certificate(fs.readFileSync(crtPath));
  } catch (e) {
    return { ok: false, reason: `cannot parse certificate: ${e.message}` };
  }
  try {
    key = createPrivateKey(fs.readFileSync(keyPath));
  } catch (e) {
    return { ok: false, reason: `cannot parse private key: ${e.message}` };
  }
  if (!cert.checkPrivateKey(key)) {
    return { ok: false, reason: 'cert and key are NOT a matching pair (modulus mismatch)' };
  }
  const issuer = (cert.issuer || '').replace(/\s+/g, ' ');
  const officeTrusted = issuer.includes(OFFICE_CA);
  const notAfter = new Date(cert.validTo);
  log(`${label} subject : ${(cert.subject || '').replace(/\s+/g, ' ')}`);
  log(`${label} issuer  : ${issuer}`);
  log(`${label} expires : ${cert.validTo}`);
  log(`${label} pair    : MATCH ✓`);
  return { ok: true, officeTrusted, notAfter };
}

function expiresSoon(notAfter) {
  const ms = notAfter.getTime() - Date.now();
  return ms < RENEW_WINDOW_DAYS * 24 * 60 * 60 * 1000;
}

// ---------------------------------------------------------------------------
// Step 1 — short-circuit if infra/certs already holds a valid, Office-trusted,
// matching pair (avoids forcing a UAC prompt on every deploy).
// ---------------------------------------------------------------------------
fs.mkdirSync(CERTS_DIR, { recursive: true });

const existing = verifyPair(DEST_CRT, DEST_KEY, 'on-disk');
if (existing.ok && existing.officeTrusted && !expiresSoon(existing.notAfter)) {
  log('Existing certs are Office-trusted, a matching pair, and not expiring soon — skipping regeneration.');
  process.exit(0);
}
if (existing.ok && !existing.officeTrusted) {
  log('Existing cert is NOT Office-trusted (self-signed?) — regenerating.');
} else if (existing.ok && expiresSoon(existing.notAfter)) {
  log(`Existing cert expires within ${RENEW_WINDOW_DAYS} days — regenerating.`);
} else {
  log('No usable certs found — generating.');
}

// ---------------------------------------------------------------------------
// Step 2 — generate + trust the Office CA and a localhost leaf cert.
// On first run this may pop a UAC / keychain trust prompt; that is expected.
// ---------------------------------------------------------------------------
log(`Running: office-addin-dev-certs install --days ${CERT_DAYS} --domains ${CERT_DOMAINS}`);
const npxCmd = process.platform === 'win32' ? 'npx.cmd' : 'npx';
const res = spawnSync(
  npxCmd,
  ['office-addin-dev-certs', 'install', '--days', CERT_DAYS, '--domains', CERT_DOMAINS],
  { cwd: PLUGIN_DIR, stdio: 'inherit', shell: process.platform === 'win32' },
);
if (res.status !== 0) {
  fail('office-addin-dev-certs install failed. Ensure plugin deps are installed (npm install) and approve the trust prompt.');
}

// ---------------------------------------------------------------------------
// Step 3 — publish the generated pair to infra/certs (write in place to keep
// the docker bind-mount inode stable for already-running containers).
// ---------------------------------------------------------------------------
if (!fs.existsSync(SRC_CRT) || !fs.existsSync(SRC_KEY)) {
  fail(`Expected generated certs at ${SRC_DIR} but they are missing.`);
}
fs.writeFileSync(DEST_CRT, fs.readFileSync(SRC_CRT));
fs.writeFileSync(DEST_KEY, fs.readFileSync(SRC_KEY));
log(`Copied cert -> ${DEST_CRT}`);
log(`Copied key  -> ${DEST_KEY}`);

// ---------------------------------------------------------------------------
// Step 4 — re-verify the published pair (modulus check) before handing off.
// ---------------------------------------------------------------------------
const published = verifyPair(DEST_CRT, DEST_KEY, 'published');
if (!published.ok) {
  fail(`Published certs failed verification: ${published.reason}`);
}
if (!published.officeTrusted) {
  fail('Published cert is not issued by the Office Add-ins CA — Excel will reject it.');
}
log('Certificates installed and verified ✓');
process.exit(0);
