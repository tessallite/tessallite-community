/**
 * Bug-9752 round 3 — the Report Builder panel's scroll container must stay a
 * plain BLOCK box, and no field-library section may override the Collapse
 * overflow.
 *
 * Round 2 made the panel root both the scroll container AND a
 * `display: flex; flexDirection: column` container. That turned every section
 * into a flex item with the default `flex-shrink: 1`, and MUI's <Collapse>
 * always renders an inline `min-height: 0` (its collapsedSize) which cancels
 * the flex automatic minimum size that normally stops an item shrinking below
 * its own content. As soon as the panel's content exceeded the pane height the
 * flex algorithm shrank the expanded <Collapse> back to zero, while its
 * entered state (`height: auto; overflow: visible`) kept painting the cards —
 * so an expanded section rendered ON TOP of the sections below instead of
 * pushing them down.
 *
 * This is a layout regression jsdom cannot observe (no layout engine), so the
 * guard is on the two source invariants that caused it. Both assertions fail
 * against the pre-fix code.
 */
import { describe, it, expect } from 'vitest';
import { readFileSync } from 'fs';
import { resolve, dirname } from 'path';
import { fileURLToPath } from 'url';

const __dirname = dirname(fileURLToPath(import.meta.url));
const builderDir = resolve(__dirname, '../components/ReportBuilder');

const LIBRARY_COMPONENTS = [
  'MeasureLibrary.tsx',
  'KpiLibrary.tsx',
  'NamedSetLibrary.tsx',
  'DimensionLibrary.tsx',
  'HierarchyLibrary.tsx',
];

describe('Bug-9752 — expanded field-library sections push the next section down', () => {
  const reportBuilder = readFileSync(resolve(builderDir, 'ReportBuilder.tsx'), 'utf-8');

  /** The sx object literal of the panel's root container. */
  const rootSx = (() => {
    const match = reportBuilder.match(
      /return \(\s*(?:\/\*[\s\S]*?\*\/\s*)?<Box sx=\{\{([^}]*)\}\}/,
    );
    return match ? match[1] : null;
  })();

  it('the panel root is the scroll container', () => {
    expect(rootSx).not.toBeNull();
    expect(rootSx).toMatch(/overflowY:\s*'auto'/);
  });

  it('the panel root is not a flex container (sections must not be shrinkable flex items)', () => {
    expect(rootSx).not.toBeNull();
    expect(rootSx).not.toMatch(/display:\s*'flex'/);
    expect(rootSx).not.toMatch(/flexDirection/);
  });

  it.each(LIBRARY_COMPONENTS)('%s does not override the section Collapse overflow', (file) => {
    const source = readFileSync(resolve(builderDir, file), 'utf-8');
    const sectionCollapses = source.match(/<Collapse\s+in=\{expanded\}[^>]*>/g) ?? [];
    expect(sectionCollapses.length).toBeGreaterThan(0);
    for (const tag of sectionCollapses) {
      expect(tag).not.toMatch(/overflow/);
    }
  });
});
