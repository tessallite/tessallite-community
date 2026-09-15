/**
 * Page object for the Playwright pane harness.
 *
 * Everything a spec needs to drive the REAL task pane: open it under the test
 * profile (so no login screen), find a field in a library and assign it to a
 * zone, press an insert action, and read the workbook the recording shim
 * captured. One responsibility — the pane's UI vocabulary — so a spec reads as
 * a user's sequence of actions and an assertion about the workbook.
 *
 * Every locator uses the pane's own ACCESSIBLE NAMES (`Add <field> to Values`,
 * `Insert <measure> as formula`, ...). That is deliberate: a harness that
 * hunted for CSS classes would keep passing while the pane became unusable
 * with a screen reader.
 */
import { expect, type Page } from '@playwright/test';

import type { WorkbookSnapshot } from '../lib/browserShim';
import { paneUrl } from './harnessEnv.mjs';

/**
 * Relative to the plugin directory, which is where Playwright runs. Passed as
 * a PATH rather than read here so this module needs no Node typings — the
 * add-in has no `@types/node` and adding one for a test helper is not worth a
 * new dependency.
 */
const SHIM_PATH = 'tests-harness/.playwright/browserShim.js';

export type Zone = 'Values' | 'Rows' | 'Columns' | 'Filter';

/**
 * Load the pane with the recording shim installed first, and wait until the
 * test profile has signed itself in — that wait IS the sign-in-free-load
 * assertion every other spec depends on.
 */
/** Console warnings/errors the pane emitted, per page. */
const consoleLog = new WeakMap<Page, string[]>();

/** What the pane logged. The add-in reports several silent degradations only here. */
export function paneConsole(page: Page): string[] {
  return consoleLog.get(page) ?? [];
}

export async function openPane(page: Page): Promise<void> {
  await page.addInitScript({ path: SHIM_PATH });
  const failures: string[] = [];
  page.on('pageerror', e => failures.push(String(e)));
  const logged: string[] = [];
  consoleLog.set(page, logged);
  page.on('console', msg => {
    if (msg.type() === 'warning' || msg.type() === 'error') logged.push(`${msg.type()}: ${msg.text()}`);
  });

  await page.goto(paneUrl(), { waitUntil: 'domcontentloaded' });

  // The header only renders once `useAuth` has restored the seeded session, so
  // its presence proves the preset profile worked and the login screen was
  // never shown.
  await expect(page.getByRole('banner')).toBeVisible({ timeout: 60_000 });
  await expect(page.getByTestId('test-build-marker')).toHaveText('TEST BUILD');
  await expect(page.getByRole('button', { name: /^Sign In$/i })).toHaveCount(0);
  expect(failures, `the pane raised uncaught errors: ${failures.join('; ')}`).toEqual([]);
}

/**
 * The collapsible field libraries. The pane ships them COLLAPSED, so a spec
 * that went straight for a field card would time out on an empty list.
 */
export type Section = 'measures' | 'dimensions' | 'KPIs' | 'named lists' | 'hierarchies';

/** Expand a library section if it is collapsed. Idempotent. */
export async function expandSection(page: Page, section: Section): Promise<void> {
  const toggle = page.getByRole('button', { name: `Expand the ${section} section` });
  if (await toggle.count() > 0) await toggle.first().click();
}

/** Wait for the field libraries to finish loading, then open the ones asked for. */
export async function waitForLibraries(
  page: Page,
  sections: Section[] = ['measures', 'dimensions'],
): Promise<void> {
  await page.getByPlaceholder(/search/i).first().waitFor({ timeout: 60_000 });
  // The compact header keeps its count separate from its title. Its existing
  // accessible toggle becomes available once the measure list is loaded.
  await expect(page.getByRole('button', { name: /(?:Expand|Collapse) the measures section/ })).toBeVisible({ timeout: 60_000 });
  for (const section of sections) await expandSection(page, section);
}

/** Narrow the field libraries to one search term. */
export async function search(page: Page, term: string): Promise<void> {
  const box = page.getByPlaceholder(/search/i).first();
  await box.fill(term);
  // The pane debounces the search before it re-filters the libraries.
  await page.waitForTimeout(400);
}

/** Assign a field to a zone by its display name, exactly as a user would. */
export async function addToZone(page: Page, displayName: string, zone: Zone): Promise<void> {
  await search(page, displayName);
  const button = page.getByRole('button', { name: `Add ${displayName} to ${zone}` }).first();
  if (await button.count() === 0) {
    // A section can re-collapse when the filtered list re-renders; open every
    // one and look again rather than failing on a layout detail.
    for (const section of ['measures', 'dimensions', 'KPIs', 'named lists', 'hierarchies'] as Section[]) {
      await expandSection(page, section);
    }
  }
  await button.waitFor({ timeout: 30_000 });
  await button.click();
}

/** Press one of the Report Builder insert actions. */
export async function insert(page: Page, action: 'Table' | 'Chart' | 'Pivot'): Promise<void> {
  const button = page.getByRole('button', { name: action, exact: true });
  await expect(button).toBeEnabled({ timeout: 30_000 });
  await button.click();
}

/** The workbook the pane's actions produced, types intact. */
export async function workbook(page: Page): Promise<WorkbookSnapshot> {
  return page.evaluate(() => window.__tsl.snapshot());
}

/**
 * Move the user's selection, as a click on a worksheet cell would. `row` and
 * `col` are 0-indexed, matching `Office.js` `rowIndex`/`columnIndex`. Drives
 * the shim directly (there is no real grid to click in headless Chromium);
 * every pane action that reads `context.workbook.getSelectedRange()` picks
 * this up on its next `Excel.run`.
 */
export async function selectCell(page: Page, sheetName: string, row: number, col: number): Promise<void> {
  await page.evaluate(
    ({ sheetName, row, col }) => window.__tsl.select(sheetName, row, col),
    { sheetName, row, col },
  );
}

/** Wait until the workbook satisfies a predicate, then return it. */
export async function workbookUntil(
  page: Page,
  predicate: (wb: WorkbookSnapshot) => boolean,
  what: string,
  timeout = 60_000,
): Promise<WorkbookSnapshot> {
  const deadline = Date.now() + timeout;
  let last: WorkbookSnapshot | null = null;
  while (Date.now() < deadline) {
    last = await workbook(page);
    if (predicate(last)) return last;
    await page.waitForTimeout(500);
  }
  throw new Error(
    `timed out waiting for the workbook to ${what}. Last snapshot: `
    + JSON.stringify({ sheets: last?.sheets.map(s => ({ name: s.name, tables: s.tables.map(t => t.name), cells: Object.keys(s.cells).length })), ops: last?.ops.slice(-12) }),
  );
}

/** Every cell the pane wrote, across every sheet, as `Sheet!A1 -> value`. */
export function allCells(wb: WorkbookSnapshot): Record<string, unknown> {
  const out: Record<string, unknown> = {};
  for (const sheet of wb.sheets) {
    for (const [addr, value] of Object.entries(sheet.cells)) out[`${sheet.name}!${addr}`] = value;
  }
  return out;
}

/**
 * Every formula the pane wrote. A SEPARATE channel from `allCells`, because
 * the pane's live/static distinction is exactly the distinction between the
 * two, and Bug-7393 depends on a static insert never touching this one.
 */
export function allFormulas(wb: WorkbookSnapshot): Record<string, unknown> {
  const out: Record<string, unknown> = {};
  for (const sheet of wb.sheets) {
    for (const [addr, value] of Object.entries(sheet.formulas)) out[`${sheet.name}!${addr}`] = value;
  }
  return out;
}

/**
 * The visible toast text, which is how the pane reports a refusal.
 *
 * Located by ROLE (`status` for info/success/warning, `alert` for error), so
 * the harness fails if the toast stops being announced to assistive tech —
 * which is exactly the state Bug-9884 found it in.
 */
export async function toastText(page: Page): Promise<string> {
  const toast = page.getByRole('status').or(page.getByRole('alert')).first();
  await toast.waitFor({ timeout: 30_000 });
  return (await toast.innerText()).trim();
}
