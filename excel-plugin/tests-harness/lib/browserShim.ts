/**
 * Browser entry for the recording Office.js shim (Playwright pane harness).
 *
 * Bundled to an IIFE by `tests-harness/pane-e2e/prepare.mjs` and injected with
 * `page.addInitScript` BEFORE the add-in loads, so the REAL pane — the real
 * React tree, the real MUI controls, the real `hooks/useExcel` — runs against
 * the same workbook mutation model the Vitest pane harness uses, and a test can
 * assert what a click actually wrote into a workbook.
 *
 * It adds only what a browser needs on top of `excelShim.ts`: the async
 * `OfficeRuntime.storage` the pane's `utils/storage` requires, `Office.onReady`,
 * and a JSON snapshot of the workbook, because a Playwright assertion has to
 * cross the page boundary and only structured data survives that trip. The
 * snapshot preserves JS types, which is the whole point (Bug-9876).
 */
import { installExcelShim, type InstalledShim, type SheetModel, type TableModel } from './excelShim';

export interface WorkbookSnapshot {
  sheets: {
    name: string;
    visibility: string;
    tables: { name: string; headers: string[]; bodyRows: unknown[][] }[];
    pivotTables: { name: string; source: string; row: string[]; column: string[]; data: string[]; filter: string[] }[];
    /** Non-empty cells as `A1 -> value`, types preserved. */
    cells: Record<string, unknown>;
    /** Formulas as `A1 -> formula`. A SEPARATE channel, as in Office.js. */
    formulas: Record<string, unknown>;
  }[];
  names: { name: string; formula: string; comment: string }[];
  settings: Record<string, unknown>;
  ops: { op: string; detail?: unknown }[];
}

declare global {
  interface Window {
    __tsl: {
      shim: InstalledShim;
      snapshot(): WorkbookSnapshot;
      reset(): void;
      /** Seed a storage key before the app boots (used for the persona checks). */
      setStorage(key: string, value: string): void;
      /** Move the user's selection, which is what a pinned write targets. */
      select(sheetName: string, row: number, col: number): void;
      storage(): Record<string, string>;
      /** Every request the page has issued, for query-string assertions. */
      requests: { method: string; url: string; status?: number }[];
    };
  }
}

function colLetters(col: number): string {
  let n = col + 1;
  let s = '';
  while (n > 0) {
    const rem = (n - 1) % 26;
    s = String.fromCharCode(65 + rem) + s;
    n = Math.floor((n - 1) / 26);
  }
  return s;
}

function snapshotSheet(sheet: SheetModel): WorkbookSnapshot['sheets'][number] {
  const cells: Record<string, unknown> = {};
  // The shim's cell map is private; read it back through the public accessor
  // over the rectangle any pane insert could plausibly have touched.
  for (let r = 0; r < 200; r++) {
    for (let c = 0; c < 40; c++) {
      const v = sheet.getCell(r, c);
      if (v !== '' && v !== null && v !== undefined) cells[`${colLetters(c)}${r + 1}`] = v;
    }
  }
  return {
    name: sheet.name,
    visibility: sheet.visibility,
    tables: sheet.tables.itemsUnchecked.map((t: TableModel) => ({
      name: t.nameUnchecked,
      headers: t.headers,
      bodyRows: t.bodyRows,
    })),
    pivotTables: sheet.pivotTables.itemsUnchecked.map(p => ({
      name: p.name,
      source: typeof p.source === 'string' ? p.source : p.source.name,
      row: p.rowHierarchies.added,
      column: p.columnHierarchies.added,
      data: p.dataHierarchies.added,
      filter: p.filterHierarchies.added,
    })),
    cells,
    formulas: sheet.formulaMap(),
  };
}

/** Async, string-only key/value store, like the real `OfficeRuntime.storage`. */
class BrowserStorage {
  private map = new Map<string, string>();
  async getItem(key: string): Promise<string | null> {
    return this.map.has(key) ? (this.map.get(key) as string) : null;
  }
  async setItem(key: string, value: string): Promise<void> {
    if (typeof value !== 'string') throw new TypeError(`OfficeRuntime.storage stores strings only; got ${typeof value}`);
    this.map.set(key, value);
  }
  async removeItem(key: string): Promise<void> { this.map.delete(key); }
  setSync(key: string, value: string): void { this.map.set(key, value); }
  snapshot(): Record<string, string> { return Object.fromEntries(this.map); }
}

function install(): void {
  const g = window as unknown as Record<string, unknown>;
  const storage = new BrowserStorage();
  const requests: { method: string; url: string; status?: number }[] = [];

  const originalFetch = window.fetch.bind(window);
  window.fetch = async function recordedFetch(input: RequestInfo | URL, init?: RequestInit) {
    const url = typeof input === 'string' ? input : (input instanceof URL ? input.href : input.url);
    const entry = { method: (init?.method ?? 'GET').toUpperCase(), url, status: undefined as number | undefined };
    requests.push(entry);
    const res = await originalFetch(input as RequestInfo, init);
    entry.status = res.status;
    return res;
  };

  let shim = installExcelShim(['Sheet1']);
  const decorate = () => {
    // `Office` is replaced wholesale by the shim; re-attach the pieces only a
    // browser-hosted pane needs.
    const office = (window as unknown as { Office: Record<string, unknown> }).Office;
    office.onReady = (cb?: (info: unknown) => void) => {
      const info = { host: 'Excel', platform: 'PC' };
      if (cb) cb(info);
      return Promise.resolve(info);
    };
    office.HostType = { Excel: 'Excel' };
  };
  decorate();

  g.OfficeRuntime = { storage };

  window.__tsl = {
    get shim() { return shim; },
    snapshot(): WorkbookSnapshot {
      const wb = shim.workbook;
      return {
        sheets: wb.worksheets.itemsUnchecked.map(snapshotSheet),
        names: (wb.names as unknown as { _items: { name: string; formula: string; comment: string }[] })._items
          .map(n => ({ name: n.name, formula: n.formula, comment: n.comment })),
        settings: Object.fromEntries(wb.settings),
        ops: wb.log.map(o => ({ op: o.op, detail: o.detail })),
      };
    },
    reset() {
      shim.restore();
      shim = installExcelShim(['Sheet1']);
      decorate();
      requests.length = 0;
    },
    setStorage(key: string, value: string) { storage.setSync(key, value); },
    select(sheetName: string, row: number, col: number) { shim.workbook.select(sheetName, row, col); },
    storage() { return storage.snapshot(); },
    requests,
  };
}

install();
