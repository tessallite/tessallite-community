import { describe, expect, it } from 'vitest';
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';

const helpRoot = resolve(__dirname, '../../../help/integrations/excel-add-in');
const pluginRoot = resolve(__dirname, '../..');
const metadata = JSON.parse(
  readFileSync(resolve(pluginRoot, 'public/functions.json'), 'utf8'),
) as { functions: Array<{ id: string }> };
const shippedFunctions = metadata.functions.map(({ id }) => id);

describe('F3 Excel help namespace parity', () => {
  it('advertises exactly the shipped TESSALLITE namespace in both help formats', () => {
    const markdown = readFileSync(`${helpRoot}.md`, 'utf8');
    const html = readFileSync(`${helpRoot}.html`, 'utf8');
    const runtime = readFileSync(resolve(pluginRoot, 'src/functions.ts'), 'utf8');
    const manifest = readFileSync(resolve(pluginRoot, 'manifest.xml'), 'utf8');
    expect(manifest).toContain('DefaultValue="TESSALLITE"');
    for (const source of [markdown, html]) {
      expect(source).not.toMatch(/\bTESS\./);
      for (const fn of shippedFunctions) {
        expect(source).toContain(`TESSALLITE.${fn}`);
        expect(runtime).toContain(`CustomFunctions.associate('${fn}'`);
      }
    }
  });
});
