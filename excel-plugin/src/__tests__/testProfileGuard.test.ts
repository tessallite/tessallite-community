/**
 * The TEST PROFILE build must be impossible to produce from a release target.
 *
 * A test-profile bundle signs itself in from baked-in credentials and hides the
 * login screen. Shipping one would hand every workbook a live session for the
 * test account. `vite.config.ts` calls `assertTestProfileBuildAllowed` before
 * anything is built; these are the cases that must refuse.
 */
import { describe, it, expect } from 'vitest';
import {
  assertTestProfileBuildAllowed,
  isProductionBaseUrl,
  isTestProfileRequested,
  RELEASE_MARKER_VARS,
  TEST_PROFILE_FLAG,
} from '../../scripts/testProfileGuard.mjs';
import { isTestProfileBuild, TEST_BUILD_MARKER } from '../testProfile';

const ON = { [TEST_PROFILE_FLAG]: '1' };

describe('test-profile build guard', () => {
  it('is a no-op for an ordinary build', () => {
    expect(assertTestProfileBuildAllowed({})).toBe(false);
    expect(assertTestProfileBuildAllowed({ PLUGIN_BASE_URL: 'https://cloud.tessallite.io' })).toBe(false);
    expect(assertTestProfileBuildAllowed({ TESSALLITE_RELEASE: '1' })).toBe(false);
  });

  it('allows the test profile against a localhost base URL', () => {
    expect(assertTestProfileBuildAllowed({ ...ON, PLUGIN_BASE_URL: 'https://localhost:3443' })).toBe(true);
    expect(assertTestProfileBuildAllowed({ ...ON })).toBe(true);
    expect(assertTestProfileBuildAllowed({ ...ON, PLUGIN_BASE_URL: 'http://192.168.1.40:3443' })).toBe(true);
  });

  it('refuses the test profile against a production base URL', () => {
    expect(() => assertTestProfileBuildAllowed({ ...ON, PLUGIN_BASE_URL: 'https://cloud.tessallite.io' }))
      .toThrow(/production PLUGIN_BASE_URL/);
  });

  it.each(RELEASE_MARKER_VARS)('refuses the test profile when %s marks a release build', (marker) => {
    expect(() => assertTestProfileBuildAllowed({ ...ON, [marker]: 'anything' }))
      .toThrow(new RegExp(`release marker\\(s\\) ${marker}`));
  });

  it('fails closed on a base URL it cannot parse', () => {
    // An unparsable value must not be treated as "local, therefore fine".
    expect(isProductionBaseUrl('cloud.tessallite.io')).toBe(true);
    expect(() => assertTestProfileBuildAllowed({ ...ON, PLUGIN_BASE_URL: 'cloud.tessallite.io' }))
      .toThrow(/production PLUGIN_BASE_URL/);
  });

  it('treats only explicit affirmatives as a request for the test profile', () => {
    expect(isTestProfileRequested({ [TEST_PROFILE_FLAG]: '1' })).toBe(true);
    expect(isTestProfileRequested({ [TEST_PROFILE_FLAG]: 'true' })).toBe(true);
    expect(isTestProfileRequested({ [TEST_PROFILE_FLAG]: '0' })).toBe(false);
    expect(isTestProfileRequested({ [TEST_PROFILE_FLAG]: '' })).toBe(false);
    expect(isTestProfileRequested({})).toBe(false);
  });
});

describe('the shipping bundle carries no test profile', () => {
  it('reports itself as an ordinary build when __TESSALLITE_TEST_PROFILE__ is absent', () => {
    // The unit suite runs with no Vite `define`, which is the same shape an
    // ordinary release build produces (`null`). Either way the pane must report
    // itself as a normal build, so the header marker never renders and the
    // preset sign-in never runs.
    expect(isTestProfileBuild).toBe(false);
  });

  it('keeps the marker a single unmistakable string', () => {
    // Grepped by the build pipeline and by a human checking a bundle; it must
    // not quietly become a friendlier phrase.
    expect(TEST_BUILD_MARKER).toBe('TESSALLITE TEST BUILD - NOT FOR RELEASE');
  });
});
