/**
 * The pane's WORKBOOK-WRITING actions, driven through the real UI.
 *
 * Each check answers the same question the local-PivotTable check does: after
 * this click, what is in the workbook, and is each written value of the right
 * TYPE? The live/static pair is the sharpest of them — the same button, the
 * same measure, and the two modes must write into two DIFFERENT Office.js
 * channels (a formula vs a number). A shim that collapsed those channels could
 * not tell them apart, which is why it does not.
 */
import { test, expect, type Page } from '@playwright/test';

import { paneFixtures } from './fixtures.mjs';
import {
  openPane, waitForLibraries, search, expandSection, addToZone,
  insert, workbookUntil, allCells, allFormulas, selectCell, paneConsole,
} from './pane';

// The pane always writes formulas in the production TESSALLITE namespace,
// test-profile build included -- there is no separate test namespace.
const NS = 'TESSALLITE';

/** Pick the insert mode, which the pane persists in OfficeRuntime.storage. */
async function setInsertMode(page: Page, mode: 'Live' | 'Static'): Promise<void> {
  const live = page.getByRole('checkbox', { name: 'Live', exact: true });
  // Toggle through the real UI even when the desired mode is already selected,
  // so the existing assertion still proves persistence rather than a default.
  if (await live.isChecked() === (mode === 'Live')) await live.click();
  await live.setChecked(mode === 'Live');
  await expect
    .poll(async () => (await page.evaluate(() => window.__tsl.storage())).tessallite_insert_mode)
    .toBe(mode.toLowerCase());
}

test.describe('single-measure insert', () => {
  test('live mode writes a custom-function VALUE formula, not a number', async ({ page }) => {
    const fx = await paneFixtures();
    await openPane(page);
    await waitForLibraries(page, ['measures']);
    await setInsertMode(page, 'Live');

    await search(page, fx.measure.name);
    await page.getByRole('button', { name: `Insert ${fx.measure.name} as formula` }).first().click();

    const wb = await workbookUntil(page, w => Object.keys(allFormulas(w)).length > 0, 'receive a formula');

    const formulas = Object.values(allFormulas(wb));
    expect(formulas).toHaveLength(1);
    // The model is addressed by SLUG and the measure by its TECHNICAL name —
    // a display name in a formula resolves to #VALUE! in a real workbook.
    expect(String(formulas[0])).toBe(`=${NS}.VALUE("${fx.model.slug}","${fx.measure.technical}")`);

    // Live mode must not ALSO write a value: a stale number left under a
    // formula is what the user sees whenever the formula fails to calculate.
    expect(allCells(wb)).toEqual({});
  });

  test('static mode writes a number, and never through the formula channel (Bug-7393)', async ({ page }) => {
    const fx = await paneFixtures();
    await openPane(page);
    await waitForLibraries(page, ['measures']);
    await setInsertMode(page, 'Static');

    await search(page, fx.measure.name);
    await page.getByRole('button', { name: `Insert ${fx.measure.name} as formula` }).first().click();

    const wb = await workbookUntil(page, w => Object.keys(allCells(w)).length > 0, 'receive a value');

    const cells = Object.values(allCells(wb));
    expect(cells).toHaveLength(1);
    // Bug-9876: the router serialises measures as numeric strings. A static
    // insert must still land a NUMBER, or every SUM over that cell silently
    // counts instead of adding.
    expect(
      typeof cells[0],
      `static insert put ${JSON.stringify(cells[0])} in the cell as ${typeof cells[0]}`,
    ).toBe('number');
    expect(Number.isFinite(cells[0] as number)).toBe(true);

    // Bug-7393: nothing reached the FORMULA channel. That separation is what
    // stops a source-derived string beginning with '=' from executing.
    expect(allFormulas(wb)).toEqual({});
  });
});

test.describe('named set insert', () => {
  test('writes a CUBESET / CUBERANKEDMEMBER block', async ({ page }) => {
    const fx = await paneFixtures();
    await openPane(page);
    await waitForLibraries(page, ['named lists']);

    const label = fx.namedSet.display_name || fx.namedSet.name;
    await search(page, label);
    await expandSection(page, 'named lists');
    // The insert actions live inside the card's MEMBER PREVIEW panel, so the
    // real sequence is preview-then-insert. (The preview control is a bare
    // clickable span with only a `title`, hence getByTitle rather than a role
    // — noted on Bug-9884 with the toast finding.)
    // Wait for the search filter to actually reduce the library to this one
    // card before clicking. The preview control carries no accessible name, so
    // `.first()` on an unsettled list opens a DIFFERENT set's preview — which
    // is exactly what happened before this wait was added.
    await expect(page.getByTitle('Preview members')).toHaveCount(1, { timeout: 30_000 });
    await page.getByTitle('Preview members').click();
    const cubeSet = page.getByRole('button', { name: 'Insert as CUBESET' });
    try {
      await cubeSet.waitFor({ timeout: 60_000 });
    } catch {
      // The insert actions only render once the member preview returns rows,
      // so a failure here is about the PREVIEW, not the insert. Say which.
      const requests = await page.evaluate(() =>
        window.__tsl.requests.filter(r => r.url.includes('named-sets')));
      const panel = await page.locator('body').innerText();
      throw new Error(
        'the named-set member preview never produced rows, so the insert actions never rendered.\n'
        + `named-set requests: ${JSON.stringify(requests)}\n`
        + `panel text: ${panel.replace(/\s+/g, ' ').slice(0, 600)}`,
      );
    }
    await cubeSet.click();

    const wb = await workbookUntil(
      page,
      w => Object.values(allFormulas(w)).some(f => String(f).includes('CUBESET')),
      'receive a CUBESET block',
    );

    const formulas = Object.values(allFormulas(wb)).map(String);
    expect(formulas.some(f => f.startsWith('=CUBESET('))).toBe(true);
    expect(formulas.some(f => f.includes('CUBERANKEDMEMBER'))).toBe(true);
    // Every cell of the block is a FORMULA. A member written as a literal
    // would not follow the set when the model changes.
    expect(allCells(wb)).toEqual({});
  });
});

test.describe('KPI scorecard insert', () => {
  test('writes custom-function KPI formulas and no CUBE formula, plus a status icon set', async ({ page }) => {
    await openPane(page);
    await page.getByRole('tab', { name: 'KPIs' }).click();

    const insertScorecard = page.getByRole('button', { name: 'Insert all KPIs as a scorecard table' });
    await insertScorecard.waitFor({ timeout: 60_000 });
    await insertScorecard.click();

    const wb = await workbookUntil(
      page,
      w => Object.keys(allFormulas(w)).length > 0,
      'receive the scorecard rows',
    );

    // The header row is written through the VALUES channel.
    const headers = Object.values(allCells(wb)).map(String);
    expect(headers).toContain('Value');
    expect(headers).toContain('Goal');
    expect(headers).toContain('Status');

    const formulas = Object.values(allFormulas(wb)).map(String);
    expect(formulas.length).toBeGreaterThan(0);

    // The KPI NAME column is written through the same channel as a plain
    // string, which is how Office.js expresses "this cell is not a formula".
    // Only the cells that ARE formulas are constrained.
    const formulaCells = formulas.filter(f => f.startsWith('='));
    expect(formulaCells.length).toBeGreaterThan(0);
    for (const formula of formulaCells) {
      // Bug-6903: the scorecard uses the add-in's OWN custom functions, so it
      // refreshes with no workbook connection at all. A CUBE formula here
      // would render #NAME? for every user who has not set one up.
      expect(formula, `scorecard cell holds a CUBE formula: ${formula}`).not.toMatch(/CUBE[A-Z]/);
      expect(formula.startsWith(`=${NS}.KPI(`), `scorecard cell is not a ${NS}.KPI call: ${formula}`).toBe(true);
    }
    // The name column must arrive as TEXT, not as something Excel evaluates.
    expect(formulas.some(f => !f.startsWith('='))).toBe(true);
    // Value, Goal and Status are each addressed as their own KPI property, so
    // one broken property cannot silently take another's number.
    expect(formulas.some(f => f.includes('"value"'))).toBe(true);
    expect(formulas.some(f => f.includes('"goal"'))).toBe(true);
    expect(formulas.some(f => f.includes('"status"'))).toBe(true);

    // The status column carries a three-icon conditional format; without it
    // the status cell is a bare -1/0/1 with no meaning on the page.
    const iconSets = wb.ops.filter(o => o.op === 'range.conditionalFormat.add');
    expect(iconSets.length).toBeGreaterThan(0);
    expect(
      wb.ops.some(o => o.op === 'range.conditionalFormat.iconSet' && (o.detail as { property?: string })?.property === 'criteria'),
      'an icon set was added with no criteria, which Office.js no-ops',
    ).toBe(true);
  });
});

test.describe('Report Builder table insert', () => {
  test('writes a table whose measure column is numeric, plus a provenance footer', async ({ page }) => {
    const fx = await paneFixtures();
    await openPane(page);
    await waitForLibraries(page);

    await addToZone(page, fx.measure.name, 'Values');
    await addToZone(page, fx.dimension.name, 'Rows');
    await insert(page, 'Table');

    const wb = await workbookUntil(page, w => w.sheets.some(s => s.tables.length > 0), 'contain a table');

    const sheet = wb.sheets.find(s => s.tables.length > 0)!;
    const table = sheet.tables[0];
    const measureCol = table.headers.indexOf(fx.measure.name);
    expect(
      measureCol,
      `"${fx.measure.name}" is not a column of the inserted table (${table.headers.join(', ')})`,
    ).toBeGreaterThanOrEqual(0);
    for (const row of table.bodyRows) {
      expect(
        typeof row[measureCol],
        `measure cell ${JSON.stringify(row[measureCol])} reached the workbook as ${typeof row[measureCol]}`,
      ).toBe('number');
    }

    // The provenance footer: inserted data must say where it came from, and
    // must be styled as a footer rather than left looking like more data.
    const footer = Object.values(sheet.cells)
      .filter(v => typeof v === 'string')
      .map(String)
      .find(v => v.startsWith('Source: Tessallite'));
    expect(footer, 'no provenance footer was written below the table').toBeTruthy();
    expect(wb.ops.filter(o => o.op === 'range.format.font').length).toBeGreaterThan(0);
  });
});

test.describe('Drill-through from an inserted table (Bug-9886)', () => {
  test('resolves a measure id for a cell inside a table the pane just inserted', async ({ page }) => {
    const fx = await paneFixtures();
    await openPane(page);
    await waitForLibraries(page);

    await addToZone(page, fx.measure.name, 'Values');
    await addToZone(page, fx.dimension.name, 'Rows');
    await insert(page, 'Table');

    const wb = await workbookUntil(page, w => w.sheets.some(s => s.tables.length > 0), 'contain a table');
    const sheet = wb.sheets.find(s => s.tables.length > 0)!;
    const table = sheet.tables[0];
    const measureCol = table.headers.indexOf(fx.measure.name);
    expect(measureCol, `"${fx.measure.name}" is not a column of the inserted table`).toBeGreaterThanOrEqual(0);

    // The table's start cell, from the recorded `__table_range` named item —
    // never assumed to be A1, since the insert is pinned to the active cell.
    const rangeItem = wb.names.find(n => n.name.endsWith('__table_range') && n.comment.includes(sheet.name));
    expect(rangeItem, 'no __table_range provenance item was written').toBeTruthy();
    const tableRange = rangeItem!.comment.replace(/^__table_range=/, '');
    const startRef = tableRange.split('!')[1].split(':')[0];
    const startColLetter = startRef.match(/[A-Z]+/)![0];
    const startRow = Number(startRef.match(/\d+/)![0]) - 1;
    const startCol = startColLetter.split('').reduce((n, ch) => n * 26 + (ch.charCodeAt(0) - 64), 0) - 1;

    // Select the first DATA row of the measure column — inside the table,
    // never the header row — exactly as the bug's repro steps describe.
    await selectCell(page, sheet.name, startRow + 1, startCol + measureCol);
    await page.getByRole('button', { name: 'Drill through selected cell' }).click();

    // A resolvable measure id opens the drill panel; an unresolved one shows
    // "Drill-through is unavailable because this cell does not contain a
    // resolvable measure id." instead. Race the two outcomes explicitly so a
    // regression here fails with the refusal text, not a bare timeout.
    const panel = page.getByText('Drill Through', { exact: true });
    const refusal = page.getByText(/does not contain a resolvable measure id/i);
    await expect(panel.or(refusal)).toBeVisible({ timeout: 15_000 });
    await expect(refusal, 'drill-through refused a cell of a table the pane just inserted').toHaveCount(0);
    expect(
      paneConsole(page).join('\n'),
      'getTableMetadata reported a read failure',
    ).not.toContain('getTableMetadata failed');
  });
});
