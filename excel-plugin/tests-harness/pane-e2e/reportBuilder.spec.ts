/**
 * Report Builder, driven through the REAL pane in headless Chromium.
 *
 * Every check here is "a user clicked these controls; what is now in the
 * workbook, and are the numbers NUMBERS?". The pane, the server and the HTTP
 * in between are all real; only Excel is modelled, by the recording shim.
 */
import { test, expect } from '@playwright/test';

import { paneFixtures } from './fixtures.mjs';
import { openPane, waitForLibraries, addToZone, insert, workbook, workbookUntil, toastText } from './pane';

test.describe('Report Builder', () => {
  test('loads signed in under the test profile, with no login screen', async ({ page }) => {
    const fx = await paneFixtures();
    await openPane(page);
    await waitForLibraries(page);

    const storage = await page.evaluate(() => window.__tsl.storage());
    expect(storage.tessallite_jwt, 'the test profile did not obtain a session').toBeTruthy();
    // The PRESET model, not simply the first the project happens to list —
    // which on the demo stack is a different model entirely.
    expect(storage.tessallite_model_id).toBe(fx.model.id);
  });

  test('inserts a local PivotTable whose measure cells are numbers (Bug-9876)', async ({ page }) => {
    const fx = await paneFixtures();
    await openPane(page);
    await waitForLibraries(page);

    await addToZone(page, fx.measure.name, 'Values');
    await addToZone(page, fx.dimension.name, 'Rows');
    await insert(page, 'Pivot');

    const wb = await workbookUntil(
      page,
      w => w.sheets.some(s => s.pivotTables.length > 0),
      'contain a PivotTable',
    );

    const dataSheet = wb.sheets.find(s => s.name === 'Pivot Data');
    expect(dataSheet, 'the backing data sheet was not created').toBeTruthy();
    expect(dataSheet!.visibility, 'the backing sheet must be hidden').toBe('Hidden');

    const table = dataSheet!.tables[0];
    expect(table, 'no _tsl_data_* table was created').toBeTruthy();
    expect(table.name).toBe('_tsl_data_Pivot_Data');
    expect(table.bodyRows.length).toBeGreaterThan(0);

    // The backing table carries the FRIENDLY headers the user sees in the
    // PivotTable field list, not the technical column names.
    const measureCol = table.headers.indexOf(fx.measure.name);
    const dimCol = table.headers.indexOf(fx.dimension.name);
    expect(
      measureCol,
      `"${fx.measure.name}" is not a column of the backing table (${table.headers.join(', ')})`,
    ).toBeGreaterThanOrEqual(0);
    expect(dimCol, `"${fx.dimension.name}" is not a column of the backing table`).toBeGreaterThanOrEqual(0);

    // Bug-9876: every measure cell must be a JS number. A text column makes
    // the PivotTable COUNT instead of SUM — a wrong number, no error anywhere.
    for (const row of table.bodyRows) {
      expect(
        typeof row[measureCol],
        `measure cell ${JSON.stringify(row[measureCol])} reached the workbook as ${typeof row[measureCol]}`,
      ).toBe('number');
      // The dimension column must stay text: a member caption coerced to a
      // number turns a row label into a value Excel will happily aggregate.
      expect(typeof row[dimCol]).toBe('string');
    }

    const pivotSheet = wb.sheets.find(s => s.pivotTables.length > 0)!;
    const pivot = pivotSheet.pivotTables[0];
    expect(pivot.name).toBe('TessalliteLocalPivot');
    expect(pivot.row).toEqual([fx.dimension.name]);
    expect(pivot.data).toEqual([fx.measure.name]);

    // Provenance: the insert leaves a named item behind.
    expect(wb.ops.filter(o => o.op === 'names.add').length).toBeGreaterThan(0);
  });
});

/**
 * The four measure classes Excel must never be allowed to re-aggregate for
 * itself. Each is a REAL measure of the fixture model, resolved from its live
 * metadata by `fixtures.mjs` rather than named in a constant, so a reseed
 * cannot quietly turn one of these into a measure of a different class and make
 * the check pass for nothing.
 */
const BLOCKED_KINDS = ['calculated', 'variant', 'semiAdditive', 'nonAdditive'] as const;

test.describe('local PivotTable refuses measures Excel would re-aggregate wrongly', () => {
  for (const kind of BLOCKED_KINDS) {
    test(`refuses a ${kind} measure, with a visible reason`, async ({ page }) => {
      const fx = await paneFixtures();
      const measure = fx.blockedMeasures[kind];

      await openPane(page);
      await waitForLibraries(page);

      // Only the measure: the insert actions enable on one measure, and the
      // pane's field-compatibility gating can legitimately refuse to pair a
      // non-additive measure with a given dimension — a different rule from
      // the one under test here.
      await addToZone(page, measure.name, 'Values');
      await insert(page, 'Pivot');

      const message = await toastText(page);
      expect(message).toContain('Local PivotTable cannot be inserted');
      expect(message).toContain(measure.name);

      // A refusal must write NOTHING. A partially-inserted pivot is worse than
      // no pivot: the user sees numbers and has no reason to distrust them.
      const wb = await workbook(page);
      expect(wb.sheets.some(s => s.pivotTables.length > 0), 'a refused insert still created a PivotTable').toBe(false);
      expect(wb.sheets.some(s => s.name === 'Pivot Data'), 'a refused insert still created the backing sheet').toBe(false);
      expect(wb.ops.filter(o => o.op === 'range.values'), 'a refused insert still wrote cells').toEqual([]);
    });
  }
});
