/**
 * Recording Office.js shim (harness (b)).
 *
 * A workbook mutation MODEL, not a mock of the functions the pane calls. Every
 * `Excel.run` batch, range write, sheet add, table add, PivotTable add and
 * named-item write lands in a workbook object the harness can then assert
 * against — "after this pane action, does the workbook contain what the user
 * expects, and are the numbers NUMBERS?".
 *
 * Deliberately strict about `load()`/`sync()`: reading a property that was
 * never loaded throws, exactly as Office.js does. A missing `load()` is a real
 * add-in defect class and a lenient shim would hide it.
 *
 * Scope: the surface the local-PivotTable insert path uses. Extend it as more
 * pane flows are brought under the harness; do not make it lenient to grow it.
 */

/* eslint-disable @typescript-eslint/no-explicit-any */

export interface RecordedOp {
  op: string;
  detail?: unknown;
}

class NotLoadedError extends Error {
  constructor(what: string) {
    super(`PropertyNotLoaded: "${what}" was read before load()/sync(). Office.js would throw here.`);
    this.name = 'PropertyNotLoaded';
  }
}

/** Base for every proxy object with Office.js load/sync semantics. */
class Loadable {
  /** Property paths requested since the last sync. */
  protected requested = new Set<string>();
  /** Property paths readable now. */
  protected loaded = new Set<string>();

  /**
   * Office.js accepts either a comma-separated string or an array of property
   * paths, and the pane uses both forms. A shim that took only the string form
   * threw `.split is not a function` deep inside `Excel.run`, where the pane's
   * own catch turned it into a silent no-op insert.
   */
  load(paths?: string | string[]): this {
    const list = Array.isArray(paths) ? paths : (paths ?? '').split(',');
    for (const raw of list) {
      const p = raw.trim();
      if (p) this.requested.add(p);
    }
    this.workbook()._pending.push(this);
    return this;
  }

  /** Called by `context.sync()`. */
  _commitLoad(): void {
    for (const p of this.requested) this.loaded.add(p);
    this.requested.clear();
  }

  protected requireLoaded(path: string, label: string): void {
    if (!this.loaded.has(path)) throw new NotLoadedError(`${label} (${path})`);
  }

  // Overridden by every concrete class; typed loosely to keep the base small.
  workbook(): WorkbookModel {
    return (this as any)._wb;
  }
}

export class RangeModel extends Loadable {
  constructor(
    private _wb: WorkbookModel,
    public sheet: SheetModel,
    public startRow: number,
    public startCol: number,
    public rowCount: number,
    public colCount: number,
  ) { super(); }

  get address(): string {
    this.requireLoaded('address', 'Range.address');
    const first = a1(this.startRow, this.startCol);
    // Office.js returns a SINGLE reference for a one-cell range ("Sheet1!B2"),
    // never "B2:B2". The pane parses this string to work out which column of a
    // tracked table the user selected, and a doubled reference makes that parse
    // fail — which is how a drill-through silently reported "this cell does not
    // contain a resolvable measure id".
    if (this.rowCount === 1 && this.colCount === 1) return `${qualify(this.sheet.name)}!${first}`;
    const last = a1(this.startRow + this.rowCount - 1, this.startCol + this.colCount - 1);
    return `${qualify(this.sheet.name)}!${first}:${last}`;
  }

  set values(grid: unknown[][]) {
    if (grid.length !== this.rowCount) {
      throw new Error(`Range values row count ${grid.length} != range height ${this.rowCount}`);
    }
    for (let r = 0; r < grid.length; r++) {
      if (grid[r].length !== this.colCount) {
        throw new Error(`Range values row ${r} width ${grid[r].length} != range width ${this.colCount}`);
      }
      for (let c = 0; c < grid[r].length; c++) {
        this.sheet.setCell(this.startRow + r, this.startCol + c, grid[r][c]);
      }
    }
    this._wb.record('range.values', { sheet: this.sheet.name, address: `${a1(this.startRow, this.startCol)}`, rows: grid.length, cols: this.colCount });
  }

  get values(): unknown[][] {
    const out: unknown[][] = [];
    for (let r = 0; r < this.rowCount; r++) {
      const row: unknown[] = [];
      for (let c = 0; c < this.colCount; c++) row.push(this.sheet.getCell(this.startRow + r, this.startCol + c));
      out.push(row);
    }
    return out;
  }

  /**
   * Formulas are a SEPARATE channel from values, exactly as in Office.js. The
   * pane relies on the distinction: `insertFormula` writes a formula, while
   * `insertLiteral` writes through `values` precisely so that a source-derived
   * string beginning with `=` can never become a formula (Bug-7393). A shim
   * that collapsed the two would make that guarantee untestable.
   */
  set formulas(grid: unknown[][]) {
    if (grid.length !== this.rowCount) {
      throw new Error(`Range formulas row count ${grid.length} != range height ${this.rowCount}`);
    }
    for (let r = 0; r < grid.length; r++) {
      for (let c = 0; c < grid[r].length; c++) {
        this.sheet.setFormula(this.startRow + r, this.startCol + c, grid[r][c]);
      }
    }
    this._wb.record('range.formulas', {
      sheet: this.sheet.name,
      address: a1(this.startRow, this.startCol),
      formulas: grid.flat(),
    });
  }

  get formulas(): unknown[][] {
    const out: unknown[][] = [];
    for (let r = 0; r < this.rowCount; r++) {
      const row: unknown[] = [];
      for (let c = 0; c < this.colCount; c++) row.push(this.sheet.getFormula(this.startRow + r, this.startCol + c));
      out.push(row);
    }
    return out;
  }

  get rowIndex(): number {
    this.requireLoaded('rowIndex', 'Range.rowIndex');
    return this.startRow;
  }

  get columnIndex(): number {
    this.requireLoaded('columnIndex', 'Range.columnIndex');
    return this.startCol;
  }

  set numberFormat(grid: unknown[][]) {
    this._wb.record('range.numberFormat', { sheet: this.sheet.name, formats: grid.flat() });
  }

  /** One cell of this range, as an independent range. */
  getCell(row: number, col: number): RangeModel {
    return new RangeModel(this._wb, this.sheet, this.startRow + row, this.startCol + col, 1, 1);
  }

  /**
   * Formatting is RECORDED, not modelled. The assertions that matter are "was
   * the provenance footer styled as a footer" and "was the KPI status column
   * given an icon set" — both of which the op log answers — while modelling
   * Excel's format object would be a large surface with nothing to catch.
   */
  private recorder(op: string) {
    return new Proxy({} as Record<string, unknown>, {
      set: (_t, prop, value) => {
        this._wb.record(op, {
          sheet: this.sheet.name,
          address: a1(this.startRow, this.startCol),
          property: String(prop),
          value,
        });
        return true;
      },
      get: () => undefined,
    });
  }

  format = {
    autofitColumns: () => this._wb.record('range.format.autofitColumns'),
    font: this.recorder('range.format.font'),
    fill: this.recorder('range.format.fill'),
  };

  /**
   * Conditional formats. `add` returns an object whose icon-set properties are
   * recorded, so a check can assert that the KPI status column really received
   * a three-icon set rather than merely that the insert did not throw.
   */
  conditionalFormats = {
    add: (type: string) => {
      this._wb.record('range.conditionalFormat.add', {
        sheet: this.sheet.name,
        address: a1(this.startRow, this.startCol),
        type,
      });
      return { iconSetOrNullObject: this.recorder('range.conditionalFormat.iconSet') };
    },
  };
}

export class TableModel extends Loadable {
  private _name = '';
  public style = '';

  constructor(private _wb: WorkbookModel, public sheet: SheetModel, public range: RangeModel, public hasHeaders: boolean) {
    super();
  }

  get name(): string {
    this.requireLoaded('name', 'Table.name');
    return this._name;
  }

  set name(v: string) {
    this._name = v;
    // A name set before sync is readable: Office.js returns the client-side
    // value for a property this batch itself assigned.
    this.loaded.add('name');
    this._wb.record('table.name', v);
  }

  /** The name regardless of load state — for harness assertions only. */
  get nameUnchecked(): string { return this._name; }

  get headers(): string[] {
    return this.range.values[0].map(String);
  }

  get bodyRows(): unknown[][] {
    return this.range.values.slice(this.hasHeaders ? 1 : 0);
  }

  getRange(): RangeModel { return this.range; }

  getHeaderRowRange(): RangeModel {
    return new RangeModel(this._wb, this.sheet, this.range.startRow, this.range.startCol, 1, this.range.colCount);
  }

  delete(): void {
    this.sheet.tables._items = this.sheet.tables._items.filter(t => t !== this);
    this._wb.record('table.delete', this._name);
  }
}

export class PivotTableModel extends Loadable {
  hierarchies = new HierarchyCollection(this);
  rowHierarchies = new HierarchyAxis('row', this);
  columnHierarchies = new HierarchyAxis('column', this);
  dataHierarchies = new HierarchyAxis('data', this);
  filterHierarchies = new HierarchyAxis('filter', this);

  // Bug-9909: Excel accepts a Range, an address string OR a Table as a pivot
  // source, and the pane passes the TABLE so the cache follows it as it grows.
  constructor(private _wb: WorkbookModel, public name: string, public source: string | TableModel, public destination: RangeModel) {
    super();
    this.loaded.add('hierarchies');
  }

  wb(): WorkbookModel { return this._wb; }
}

export class HierarchyCollection extends Loadable {
  items: { name: string }[] = [];

  constructor(private pivot: PivotTableModel) {
    super();
  }

  _commitLoad(): void {
    // Hierarchies are the source table's header row.
    const wb = this.pivot.wb();
    const source = this.pivot.source;
    const table = typeof source === 'string' ? wb.findTableByAddress(source) : source;
    this.items = (table ? table.headers : []).map(name => ({ name }));
    super._commitLoad();
  }

  workbook(): WorkbookModel { return this.pivot.wb(); }
}

export class HierarchyAxis {
  added: string[] = [];
  constructor(public axis: string, private pivot: PivotTableModel) {}
  add(hierarchy: { name: string }): void {
    this.added.push(hierarchy.name);
    this.pivot.wb().record('pivot.hierarchy.add', { axis: this.axis, field: hierarchy.name });
  }
}

class Collection<T> extends Loadable {
  _items: T[] = [];
  constructor(protected _wb: WorkbookModel) { super(); }
  get items(): T[] {
    this.requireLoaded('items/name', 'Collection.items');
    return this._items;
  }
  /** Bypasses load() — for harness assertions only. */
  get itemsUnchecked(): T[] { return this._items; }
  workbook(): WorkbookModel { return this._wb; }
}

export class SheetModel extends Loadable {
  private cells = new Map<string, unknown>();
  visibility: string = 'Visible';
  tables: Collection<TableModel>;
  pivotTables: Collection<PivotTableModel>;

  constructor(private _wb: WorkbookModel, public name: string) {
    super();
    this.tables = new Collection<TableModel>(_wb);
    this.pivotTables = new Collection<PivotTableModel>(_wb);
  }

  private formulas = new Map<string, unknown>();

  setCell(r: number, c: number, v: unknown): void { this.cells.set(`${r},${c}`, v); }
  getCell(r: number, c: number): unknown { return this.cells.has(`${r},${c}`) ? this.cells.get(`${r},${c}`) : ''; }

  setFormula(r: number, c: number, v: unknown): void { this.formulas.set(`${r},${c}`, v); }
  getFormula(r: number, c: number): unknown { return this.formulas.has(`${r},${c}`) ? this.formulas.get(`${r},${c}`) : ''; }

  /** Every formula on this sheet, as `A1 -> formula`, for harness assertions. */
  formulaMap(): Record<string, unknown> {
    const out: Record<string, unknown> = {};
    for (const [key, value] of this.formulas) {
      const [r, c] = key.split(',').map(Number);
      if (value !== '' && value !== null && value !== undefined) out[a1(r, c)] = value;
    }
    return out;
  }

  getRangeByIndexes(row: number, col: number, rowCount: number, colCount: number): RangeModel {
    return new RangeModel(this._wb, this, row, col, rowCount, colCount);
  }

  getRange(address: string): RangeModel {
    const { row, col } = parseA1(address);
    return new RangeModel(this._wb, this, row, col, 1, 1);
  }

  activate(): void { this._wb.record('sheet.activate', this.name); }

  delete(): void {
    this._wb.worksheets._items = this._wb.worksheets._items.filter(s => s !== this);
    this._wb.record('sheet.delete', this.name);
  }
}

export class NamedItemModel {
  comment = '';
  isNullObject = false;
  constructor(public name: string, public formula: string) {}
}

class NamesCollection extends Loadable {
  _items: NamedItemModel[] = [];
  constructor(private _wb: WorkbookModel) { super(); }

  get items(): NamedItemModel[] {
    if (!this.loaded.has('items/name')) throw new NotLoadedError('NamedItemCollection.items');
    return this._items;
  }

  add(name: string, reference: string): NamedItemModel {
    const existing = this._items.find(i => i.name === name);
    if (existing) {
      existing.formula = reference;
      return existing;
    }
    const item = new NamedItemModel(name, reference);
    this._items.push(item);
    this._wb.record('names.add', name);
    return item;
  }

  getItemOrNullObject(name: string): NamedItemModel & { delete(): void } {
    const found = this._items.find(i => i.name === name);
    const self = this;
    const target = found ?? Object.assign(new NamedItemModel(name, ''), { isNullObject: true });
    return Object.assign(target, {
      delete(): void {
        if (found) {
          self._items = self._items.filter(i => i !== found);
          self._wb.record('names.delete', name);
        }
      },
    });
  }

  workbook(): WorkbookModel { return this._wb; }
}

export class WorkbookModel {
  worksheets: Collection<SheetModel> & { add(name?: string): SheetModel };
  names = new NamesCollection(this);
  application = { calculate: (type: string) => this.record('application.calculate', type) };
  log: RecordedOp[] = [];
  _pending: Loadable[] = [];
  settings = new Map<string, unknown>();

  constructor(sheetNames: string[] = ['Sheet1']) {
    const wb = this;
    const sheets = new Collection<SheetModel>(this) as Collection<SheetModel> & { add(name?: string): SheetModel };
    sheets.add = function add(name?: string): SheetModel {
      const finalName = name ?? `Sheet${this._items.length + 1}`;
      if (this._items.some(s => s.name === finalName)) {
        throw new Error(`Worksheet "${finalName}" already exists — Excel would reject this add.`);
      }
      const sheet = new SheetModel(wb, finalName);
      this._items.push(sheet);
      wb.record('sheet.add', finalName);
      return sheet;
    };
    for (const n of sheetNames) sheets._items.push(new SheetModel(this, n));
    this.worksheets = sheets;
  }

  /**
   * The user's selection. A pane insert PINS its write target from exactly one
   * read of this (Bug-7397), so the harness has to model it rather than let
   * every write default to A1 and hide target-resolution defects.
   */
  selection = { sheetName: 'Sheet1', row: 0, col: 0 };

  /** Move the selection, as a user clicking a cell would. */
  select(sheetName: string, row: number, col: number): void {
    this.selection = { sheetName, row, col };
  }

  getSelectedRange(): RangeModel {
    const sheet = this.sheet(this.selection.sheetName) ?? this.worksheets.itemsUnchecked[0];
    return new RangeModel(this, sheet, this.selection.row, this.selection.col, 1, 1);
  }

  record(op: string, detail?: unknown): void { this.log.push({ op, detail }); }

  ops(op: string): RecordedOp[] { return this.log.filter(o => o.op === op); }

  sheet(name: string): SheetModel | undefined {
    return this.worksheets.itemsUnchecked.find(s => s.name === name);
  }

  allTables(): TableModel[] {
    return this.worksheets.itemsUnchecked.flatMap(s => s.tables.itemsUnchecked);
  }

  allPivotTables(): PivotTableModel[] {
    return this.worksheets.itemsUnchecked.flatMap(s => s.pivotTables.itemsUnchecked);
  }

  findTableByAddress(address: string): TableModel | undefined {
    const sheetName = address.includes('!') ? address.split('!')[0].replace(/^'|'$/g, '') : null;
    return this.allTables().find(t => (sheetName ? t.sheet.name === sheetName : true));
  }

  sync(): void {
    const pending = this._pending.splice(0);
    for (const p of pending) p._commitLoad();
  }
}

// --- A1 helpers ------------------------------------------------------------

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

function a1(row: number, col: number): string {
  return `${colLetters(col)}${row + 1}`;
}

/**
 * Excel quotes a sheet name in an address ONLY when it needs to — "Sheet1!B2",
 * but "'Pivot Data'!A1". The pane HASHES the sheet name out of an address to
 * find a table's metadata, so quoting a name that Excel would leave bare makes
 * every lookup miss: the observed symptom was a drill-through reporting "this
 * cell does not contain a resolvable measure id" on a table it had just
 * written itself.
 */
function qualify(sheetName: string): string {
  return /^[A-Za-z0-9_]+$/.test(sheetName) ? sheetName : `'${sheetName.replace(/'/g, "''")}'`;
}

function parseA1(address: string): { row: number; col: number } {
  const m = /^\$?([A-Z]+)\$?(\d+)/.exec(address.replace(/^.*!/, '').toUpperCase());
  if (!m) throw new Error(`Unparsable A1 address: ${address}`);
  let col = 0;
  for (const ch of m[1]) col = col * 26 + (ch.charCodeAt(0) - 64);
  return { row: Number(m[2]) - 1, col: col - 1 };
}

// --- Installation ----------------------------------------------------------

export interface InstalledShim {
  workbook: WorkbookModel;
  restore(): void;
}

/** Install `Excel` and `Office` globals backed by a fresh workbook. */
export function installExcelShim(sheetNames?: string[]): InstalledShim {
  const workbook = new WorkbookModel(sheetNames);
  const g = globalThis as any;
  const prevExcel = g.Excel;
  const prevOffice = g.Office;

  const tablesAdd = function (this: Collection<TableModel>, range: RangeModel, hasHeaders: boolean): TableModel {
    const sheet = range.sheet;
    const table = new TableModel(workbook, sheet, range, hasHeaders);
    sheet.tables._items.push(table);
    workbook.record('table.add', { sheet: sheet.name, rows: range.rowCount, cols: range.colCount });
    return table;
  };
  const pivotAdd = function (this: Collection<PivotTableModel>, name: string, source: string | TableModel, destination: RangeModel): PivotTableModel {
    const pivot = new PivotTableModel(workbook, name, source, destination);
    destination.sheet.pivotTables._items.push(pivot);
    workbook.record('pivotTable.add', {
      name,
      source: typeof source === 'string' ? source : source.name,
      sourceKind: typeof source === 'string' ? 'range' : 'table',
      sheet: destination.sheet.name,
    });
    return pivot;
  };

  // Attach the collection factories the pane calls.
  for (const s of workbook.worksheets.itemsUnchecked) {
    (s.tables as any).add = tablesAdd.bind(s.tables);
    (s.pivotTables as any).add = pivotAdd.bind(s.pivotTables);
  }
  const originalSheetAdd = workbook.worksheets.add.bind(workbook.worksheets);
  (workbook.worksheets as any).add = (name?: string) => {
    const sheet = originalSheetAdd(name);
    (sheet.tables as any).add = tablesAdd.bind(sheet.tables);
    (sheet.pivotTables as any).add = pivotAdd.bind(sheet.pivotTables);
    return sheet;
  };

  // The two sheet lookups every pinned write uses. `getItem` THROWS on an
  // unknown name, exactly as Office.js does: a pane that resolved the wrong
  // sheet name must fail here, not silently write somewhere else.
  (workbook.worksheets as any).getItem = (name: string): SheetModel => {
    const sheet = workbook.sheet(name);
    if (!sheet) throw new Error(`ItemNotFound: no worksheet named "${name}". Office.js would throw here.`);
    return sheet;
  };
  (workbook.worksheets as any).getActiveWorksheet = (): SheetModel =>
    workbook.sheet(workbook.selection.sheetName) ?? workbook.worksheets.itemsUnchecked[0];

  g.Excel = {
    async run<T>(fn: (context: any) => Promise<T>): Promise<T> {
      workbook.record('Excel.run.start');
      const context = {
        workbook,
        sync: async () => { workbook.sync(); },
      };
      try {
        const out = await fn(context);
        workbook.record('Excel.run.end');
        return out;
      } catch (e) {
        workbook.record('Excel.run.error', String(e));
        throw e;
      }
    },
    SheetVisibility: { visible: 'Visible', hidden: 'Hidden', veryHidden: 'VeryHidden' },
    CalculationType: { fullRebuild: 'FullRebuild', full: 'Full', recalculate: 'Recalculate' },
    // Enums the pane reads while building a KPI scorecard's icon-set format.
    // Present with their real Office.js values so a typo in the add-in shows
    // up here as `undefined` rather than being quietly accepted.
    ConditionalFormatType: { iconSet: 'IconSet' },
    IconSet: { threeTrafficLights1: 'ThreeTrafficLights1' },
    ConditionalFormatIconRuleType: { number: 'Number', percent: 'Percent', formula: 'Formula' },
    ConditionalIconCriterionOperator: {
      greaterThan: 'GreaterThan',
      greaterThanOrEqual: 'GreaterThanOrEqual',
    },
    ChartType: { columnClustered: 'ColumnClustered' },
    ChartSeriesBy: { auto: 'Auto' },
  };

  g.Office = {
    context: {
      document: {
        settings: {
          get: (k: string) => (workbook.settings.has(k) ? workbook.settings.get(k) : null),
          set: (k: string, v: unknown) => { workbook.settings.set(k, v); },
          saveAsync: (cb: (r: unknown) => void) => cb({ status: 'succeeded' }),
        },
      },
    },
  };

  return {
    workbook,
    restore() {
      g.Excel = prevExcel;
      g.Office = prevOffice;
    },
  };
}
