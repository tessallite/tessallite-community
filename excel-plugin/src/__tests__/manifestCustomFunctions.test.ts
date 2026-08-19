import { execFileSync } from 'node:child_process';
import { readdirSync, readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join, resolve } from 'node:path';

import { describe, expect, it } from 'vitest';

import productionManifest from '../../manifest.xml?raw';
import manifestTemplate from '../../manifest.xml.template?raw';

/**
 * Bug-8811: this suite used to also import `../../sideload-catalog/manifest.xml`.
 * That directory is gitignored (`excel-plugin/.gitignore:4`) — it is a
 * per-machine sideload artifact the repo deliberately never scripts or commits.
 * The unconditional import therefore failed to RESOLVE on any clean checkout,
 * so the whole file failed collection and NOT ONE of these manifest assertions
 * has ever run in CI. A contract guard that cannot load pins nothing.
 *
 * The two committed manifest sources are pinned here. The third copy the
 * architecture requires to stay in lockstep is `generateManifest()` in the
 * frontend's EndpointsPanel — it lives in the frontend package and is pinned by
 * `frontend/src/components/Panels/EndpointsPanel.test.tsx`, which is the right
 * home for it. Nothing is unguarded by this change; assertions that previously
 * never executed now do.
 *
 * See `docs/architecture/architecture_excel-custom-functions-runtime.md`.
 */

const pluginRoot = resolve(dirname(fileURLToPath(import.meta.url)), '../..');

/**
 * Bug-6905: perpetual Office (2019/2021) only processes the CustomFunctions
 * extension point under VersionOverridesV1_0, and the Script URL must be the
 * classic-script IIFE bundle (an ES module crashes the JS-only runtime).
 * These assertions pin the verified-working sideload structure for both
 * committed manifest variants (Bug-8811: the third was an uncommitted artifact).
 */
describe('Excel custom functions manifest wiring', () => {
  it.each([
    ['manifest.xml', productionManifest],
    ['manifest.xml.template', manifestTemplate],
  ])('%s exposes TESSALLITE custom functions metadata (V1_0 structure)', (_relativePath, manifest) => {
    // V1_0 is required for perpetual-Office sideloading; V1_1 is ignored there.
    expect(manifest).toContain('VersionOverridesV1_0');
    expect(manifest).not.toContain('VersionOverridesV1_1');
    expect(manifest).toContain('xsi:type="CustomFunctions"');
    // V1_0 uses direct Script/Page/Metadata children (no <CustomFunctions> wrapper).
    expect(manifest).not.toContain('<CustomFunctions>');
    // Script URL must point to the IIFE bundle, not an ES module entry.
    expect(manifest).toContain('Functions.Script.Url');
    expect(manifest).toContain('functions.iife.js');
    // Page URL hosts the CDN custom-functions-runtime.js and performs registration.
    expect(manifest).toContain('Functions.Page.Url');
    expect(manifest).toContain('functions.html');
    expect(manifest).toContain('Functions.Metadata.Url');
    expect(manifest).toContain('Functions.Namespace');
    expect(manifest).toContain('DefaultValue="TESSALLITE"');
    // CustomFunctionsRuntime requirement set must be declared.
    expect(manifest).toContain('Name="CustomFunctionsRuntime"');
  });

  it.each([
    ['manifest.xml', productionManifest],
    ['manifest.xml.template', manifestTemplate],
  ])('%s keeps Script/Page/Metadata resources resolvable', (_relativePath, manifest) => {
    // Every resid referenced by the extension point must be defined in Resources.
    for (const resid of ['Functions.Script.Url', 'Functions.Page.Url', 'Functions.Metadata.Url', 'Functions.Namespace']) {
      const references = manifest.split(resid).length - 1;
      expect(references, `${resid} must be referenced and defined`).toBeGreaterThanOrEqual(2);
    }
  });

  // F-025-02: only the TESSALLITE namespace is published. No shipped manifest
  // may declare a second custom-functions namespace (e.g. a stray TESS), which
  // would make advertised formulas resolve to #NAME? on a real host.
  it.each([
    ['manifest.xml', productionManifest],
    ['manifest.xml.template', manifestTemplate],
  ])('%s publishes only the TESSALLITE custom-functions namespace', (_relativePath, manifest) => {
    const namespaceValues = [...manifest.matchAll(/id="Functions\.Namespace"[^>]*DefaultValue="([^"]+)"/g)]
      .map((m) => m[1]);
    expect(namespaceValues.length).toBeGreaterThanOrEqual(1);
    for (const value of namespaceValues) {
      expect(value).toBe('TESSALLITE');
    }
  });
});

/**
 * F-025-04: a fourth, undocumented V1_1 development manifest (`manifest.dev.xml`)
 * sat outside the lockstep test and could reproduce the perpetual-Office
 * #NAME?/function-runtime failure. It was deleted. This guard fails loudly if
 * ANY `manifest*.xml` in the plugin root regresses to the V1_1 custom-functions
 * schema (or a <CustomFunctions> wrapper), regardless of whether it is listed
 * in the parity table above.
 */
describe('Excel manifest V1_0 runtime-contract guard (all plugin-root manifests)', () => {
  const manifestFiles = readdirSync(pluginRoot)
    .filter((name) => /^manifest.*\.xml$/.test(name));

  it('finds at least the canonical production manifest', () => {
    expect(manifestFiles).toContain('manifest.xml');
  });

  it('no plugin-root manifest.xml regresses to the V1_1 custom-functions schema', () => {
    for (const name of manifestFiles) {
      const contents = readFileSync(join(pluginRoot, name), 'utf8');
      if (!contents.includes('xsi:type="CustomFunctions"')) continue;
      expect(contents, `${name} must use VersionOverridesV1_0`).toContain('VersionOverridesV1_0');
      expect(contents, `${name} must not use VersionOverridesV1_1`).not.toContain('VersionOverridesV1_1');
      expect(contents, `${name} must not use the V1_1 <CustomFunctions> wrapper`).not.toContain('<CustomFunctions>');
      expect(contents, `${name} must reference the IIFE Script bundle`).toContain('functions.iife.js');
    }
  });
});

/**
 * Bug-8811 regression guard.
 *
 * The defect was not in a manifest — it was that this suite depended on a file
 * the repository does not contain, so it silently stopped guarding anything.
 * Any `?raw` import in the plugin's own test suite must resolve to a file that
 * is actually tracked by git; an ignored or generated artifact makes the suite
 * pass on the author's machine and fail collection everywhere else.
 */
describe('Bug-8811 — the suite never depends on an untracked file', () => {
  const testsDir = resolve(pluginRoot, 'src/__tests__');

  it('every `?raw` import in the plugin test suite points at a tracked file', () => {
    const tracked = new Set(
      execFileSync('git', ['ls-files', '-z'], { cwd: pluginRoot, encoding: 'utf8' })
        .split('\0')
        .filter(Boolean)
        .map((rel) => resolve(pluginRoot, rel)),
    );
    // Fail closed: an empty listing means git could not answer, which must not
    // be mistaken for "no violations".
    expect(tracked.size).toBeGreaterThan(0);

    const offenders: string[] = [];
    for (const name of readdirSync(testsDir)) {
      if (!/\.tsx?$/.test(name)) continue;
      const source = readFileSync(join(testsDir, name), 'utf8');
      for (const match of source.matchAll(/from\s+'(\.[^']+)\?raw'/g)) {
        const target = resolve(testsDir, match[1]);
        if (!tracked.has(target)) {
          offenders.push(`${name} -> ${match[1]}`);
        }
      }
    }
    expect(offenders,
      'A test imports `?raw` from a file git does not track. It will resolve on '
      + 'the machine that generated it and fail collection everywhere else, '
      + 'silently disabling every assertion in that file.',
    ).toEqual([]);
  });
});
