import { expect, it } from 'vitest';

import functionsSource from '@/functions.ts?raw';
import manifestSource from '../../../manifest.xml?raw';

it('keeps nested raw-import fixtures resolvable', () => {
  expect(functionsSource).toContain('TESSALLITE');
  expect(manifestSource).toContain('VersionOverridesV1_0');
});
