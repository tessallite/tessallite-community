/**
 * Workbook metadata persistence for inserted tables.
 * Stores project, model, query, and plugin metadata on tables
 * using Excel custom properties and hidden named ranges.
 */

import { rangesOverlap, type CellRange } from './insertGuard';

const METADATA_PREFIX = '__tessallite_';

/**
 * Bug-7397: Office.js `NamedItem.comment` is capped at 255 characters. Table
 * provenance is stored as `key=value` comments; `semanticQuery` in particular
 * is `JSON.stringify(query)` and easily exceeds the cap for a query with a few
 * measures/dimensions/filters. Over-length assignment is either rejected (the
 * add is wrapped in a swallow-all try) or silently truncated, so a Refresh or
 * Drill that reads `metadata.semanticQuery` (useExcel.ts, tableRefresh.ts —
 * where it is `JSON.parse`d) silently loses ALL provenance for that table.
 *
 * Fix: values whose `key=value` comment would exceed the cap are CHUNKED across
 * additional sequenced named items and reassembled on read. Short values keep
 * the exact single-comment form, so existing workbooks and the read path are
 * unchanged.
 */
export const MAX_COMMENT_LENGTH = 255;

/**
 * Chunking is marked by the item NAME, never by a value sentinel: an over-long
 * value writes a head item `<key>__chunks=<N>` plus N chunk items
 * `<key>__cI=<piece>`. Keying on the name avoids embedding any marker byte in
 * the stored value -- a control byte (e.g. U+0001) is not a legal XML 1.0
 * character and would not survive an OOXML save/reopen, so a value sentinel
 * could not be relied on. It also makes value/marker confusion impossible.
 */
const CHUNKS_SUFFIX = '__chunks';

/** Suffix (appended to the metadata key) naming the i-th chunk item. */
function chunkKey(key: string, i: number): string {
  return `${key}__c${i}`;
}

/**
 * Split `value` into pieces whose UTF-16 length does not exceed `size`, WITHOUT
 * ever cutting a surrogate pair. Iterating a string yields whole code points, so
 * a non-BMP character (emoji, some CJK) -- length 2 in UTF-16 -- is kept intact.
 * A lone surrogate is unencodable in workbook XML and would fail the comment
 * assignment (losing the whole batch) or be replaced with U+FFFD (corrupting
 * the reassembled JSON).
 */
export function splitByCodePoints(value: string, size: number): string[] {
  const chunks: string[] = [];
  let current = '';
  for (const cp of value) {
    if (current.length + cp.length > size && current.length > 0) {
      chunks.push(current);
      current = '';
    }
    current += cp;
  }
  if (current.length > 0) chunks.push(current);
  return chunks;
}

/**
 * Encode one metadata entry into the named-item comment(s) needed to persist it
 * without exceeding {@link MAX_COMMENT_LENGTH}. Returns `{ suffix, comment }`
 * pairs; each becomes a named item `<prefix><rangeKey>_<suffix>`. A value that
 * fits stays a single `key=value` comment (backward compatible); an over-long
 * value emits a `<key>__chunks=<N>` head plus N `<key>__cI=<piece>` items.
 *
 * The per-chunk envelope `<key>__cI=` reserves up to 6 index digits, so every
 * emitted comment is <= the cap for any reachable value size (1e6 chunks would
 * be > 200 MB of value; Excel caps sheets long before that).
 */
export function encodeMetadataEntry(
  key: string,
  value: string,
): { suffix: string; comment: string }[] {
  const single = `${key}=${value}`;
  if (single.length <= MAX_COMMENT_LENGTH) {
    return [{ suffix: key, comment: single }];
  }
  const envelope = key.length + '__c'.length + 6 + 1; // 6 index digits + '='
  // Bug-7397 fix #5: if the key is so long that even an empty chunk would
  // produce an over-length comment, chunking cannot stay within the 255-char
  // cap. Return the value unchunked so Office.js rejects the over-length
  // comment loudly (honest failure) rather than emitting N chunks each
  // silently exceeding the cap (Math.max(1,...) floored to 1 char of content
  // but the envelope alone already exceeds 255).
  const budget = MAX_COMMENT_LENGTH - envelope;
  if (budget < 1) {
    return [{ suffix: key, comment: single }];
  }
  const size = budget;
  const chunks = splitByCodePoints(value, size);
  const out: { suffix: string; comment: string }[] = [
    { suffix: `${key}${CHUNKS_SUFFIX}`, comment: `${key}${CHUNKS_SUFFIX}=${chunks.length}` },
  ];
  chunks.forEach((chunk, i) => {
    const ck = chunkKey(key, i);
    out.push({ suffix: ck, comment: `${ck}=${chunk}` });
  });
  return out;
}

/**
 * Reassemble any chunked values in a raw `key -> value` metadata map (mutates
 * and returns it). For each `<key>__chunks=<N>` head, the `<key>__cI` pieces are
 * concatenated into `<key>` and the head + chunk entries removed. The head is
 * ALWAYS removed so it never leaks to callers. Reassembly happens only when
 * EVERY expected chunk is present: a missing chunk leaves `<key>` absent (an
 * honest "no provenance" skip) rather than collapsing to `''` (which would break
 * `JSON.parse` on the refresh/drill path). Idempotent -- a second call is a
 * no-op because the heads are already gone. Non-chunked keys are untouched.
 */
export function reassembleChunkedMetadata(
  raw: Record<string, string>,
): Record<string, string> {
  for (const key of Object.keys(raw)) {
    if (!key.endsWith(CHUNKS_SUFFIX)) continue;
    const base = key.slice(0, -CHUNKS_SUFFIX.length);
    const rawCountStr = raw[key];
    delete raw[key];
    if (base && base in raw) {
      // Bug-7397 F-1 DEFENSE IN DEPTH (not the primary fix -- see the
      // per-range write lock in setTableMetadata, which serializes writers
      // for the same table and is what actually closes the concurrency race
      // this comment used to describe). This branch only matters for debris
      // left behind by a PRE-fix build (or any future write path that bypasses
      // the lock): if BOTH a plain `<base>` value and a `<base>__chunks` head
      // coexist, we cannot tell from the stored data alone which one was
      // written more recently (a plain value is not necessarily the newer
      // write -- treating it as such is provably wrong for some writer
      // orderings, so this must never be the only guard). Preferring the
      // plain value here is a deliberate, bounded choice: it avoids handing
      // callers a reassembled value built from a POSSIBLY-INCOMPLETE stale
      // chunk set, and it still lets the orphan sweep below clean up the
      // headless `<base>__cI` debris either way.
      continue;
    }
    // Bug-7397 fix #2: validate the FULL string is a non-negative integer.
    // parseInt('2abc', 10) silently returns 2 (partial parse); Number('1e9')
    // returns 1000000000 (scientific notation). Both are wrong for a chunk
    // count. Only a pure-digit string is accepted; anything else is treated
    // as absent/corrupt (honest skip, never silently-wrong reassembly).
    if (!base || !/^\d+$/.test(rawCountStr)) continue;
    const count = Number(rawCountStr);
    if (!Number.isFinite(count) || count < 1) continue;
    const parts: string[] = [];
    let complete = true;
    for (let i = 0; i < count; i++) {
      const ck = chunkKey(base, i);
      if (!(ck in raw)) { complete = false; break; }
      parts.push(raw[ck]);
    }
    if (!complete) {
      // A chunk is missing (e.g. a partially-deleted named-item set, or a
      // corrupt/huge parsed `count`). Do NOT reassemble (that would corrupt
      // the value). Cleanup of the orphan chunk entries is left ENTIRELY to
      // the symmetric orphan sweep below, which scans the keys actually
      // PRESENT in `raw` (a regex over real entries) rather than looping up to
      // the parsed `count` -- a malformed `__chunks` value (e.g. a huge
      // garbage number) would otherwise spin issuing deletes for keys that
      // were never written, for as long as `count` says. <base> stays absent
      // -> honest "no provenance" skip.
      continue;
    }
    for (let i = 0; i < count; i++) delete raw[chunkKey(base, i)];
    raw[base] = parts.join('');
  }
  // Symmetric orphan sweep: any `<base>__cI` still present after head processing
  // has NO valid `<base>__chunks` head (head named-item lost, or its comment
  // parsed non-numeric). Drop it too, so debris from a partially-deleted set
  // never leaks to callers or gets re-persisted by the refresh path
  // (tableRefresh.updateTableTimestamp spreads the returned metadata). No real
  // TableMetadata field name ends with `__cN`, so only our chunk writer matches.
  for (const key of Object.keys(raw)) {
    if (/__c\d+$/.test(key)) delete raw[key];
  }
  return raw;
}

// F-26: TTL cache for getTableMetadata to avoid repeated Excel.run calls.
//
// Bug-7397 F-1/F-2 (round-2 deep review): the cache is keyed by whatever
// literal `rangeAddress` string the CALLER passed to getTableMetadata, but a
// single table can legitimately be read under many different address strings
// -- its own start-cell address (fast path) AND any cell inside it resolved
// via range containment (slow path, e.g. cellContext.ts reading the currently
// selected cell). setTableMetadata only ever knows the ONE address it itself
// was called with, so a plain `_metadataCache.delete(rangeAddress)` could
// never reach a slow-path entry cached under a different address for the SAME
// table. Each cache entry now also records the TABLE's true `rangeKey`
// (`${hashSheetName(sheetName)}_${startCell}`, identical to the key
// setTableMetadata writes named items under) so invalidation can be done BY
// TABLE, not by address string: `invalidateMetadataCacheForRangeKey` purges
// every address ever cached for that table in one call, however the caller
// addressed it.
const _metadataCache = new Map<string, { data: Partial<TableMetadata>; ts: number; rangeKey: string }>();
const _metadataCacheAddressesByRangeKey = new Map<string, Set<string>>();
const METADATA_CACHE_TTL_MS = 30_000;

// Bug-7397 F-1 (round-3 deep-review finding): the `finally`-block invalidation
// in setTableMetadata only closes the WRITE-then-read ordering (a read that
// starts after a write has already committed must not serve pre-write data).
// It does nothing for the MIRRORED ordering -- a read that STARTS first,
// spans a real host round trip (`await context.sync()`), and only resolves
// AFTER a write to the same table has since committed and invalidated: that
// read's snapshot is stale by construction, but nothing stopped it from being
// written into the cache after the fact, because there is no write for it to
// serialize behind (the write lock only orders WRITES against each other).
// A global monotonic epoch closes this: getTableMetadata captures the epoch
// before starting its read; if ANY invalidation (whole-cache or per-table)
// happens while that read is in flight, its result is stale and must be
// discarded rather than cached. Global (not per-rangeKey) is a deliberate
// simplification -- the only cost of over-invalidating is one extra cache
// miss for an unrelated table, negligible next to serving stale data.
let _metadataCacheEpoch = 0;

function cacheMetadata(
  rangeAddress: string,
  rangeKey: string,
  data: Partial<TableMetadata>,
  epochAtReadStart: number,
): void {
  if (epochAtReadStart !== _metadataCacheEpoch) return; // an invalidation raced this read; never cache a stale snapshot
  _metadataCache.set(rangeAddress, { data, ts: Date.now(), rangeKey });
  let addresses = _metadataCacheAddressesByRangeKey.get(rangeKey);
  if (!addresses) {
    addresses = new Set();
    _metadataCacheAddressesByRangeKey.set(rangeKey, addresses);
  }
  addresses.add(rangeAddress);
}

/** Purge every cache entry ever recorded for the table identified by `rangeKey`. */
function invalidateMetadataCacheForRangeKey(rangeKey: string): void {
  // Bump the epoch UNCONDITIONALLY, before checking whether anything is
  // indexed yet -- an in-flight read that has not reached `cacheMetadata` yet
  // (still awaiting its own Excel.run) has nothing to find in `addresses`,
  // but still needs to be cancelled by this invalidation.
  _metadataCacheEpoch++;
  const addresses = _metadataCacheAddressesByRangeKey.get(rangeKey);
  if (!addresses) return;
  for (const address of addresses) _metadataCache.delete(address);
  _metadataCacheAddressesByRangeKey.delete(rangeKey);
}

export function invalidateMetadataCache(): void {
  _metadataCacheEpoch++;
  _metadataCache.clear();
  _metadataCacheAddressesByRangeKey.clear();
}

export interface TableMetadata {
  projectId?: string;
  modelId?: string;
  personaId?: string;
  conversationId?: string;
  turnId?: string;
  semanticQuery?: string;
  columnHeaders?: string;
  measureColumns?: string;
  dimensionColumns?: string;
  pluginVersion: string;
  timestamp: string;
  /**
   * Bug-8424: set by {@link getTableMetadata} when the underlying `Excel.run`
   * threw, so a caller can tell "this table genuinely carries no Tessallite
   * provenance" from "we could not read its provenance this time". Synthetic
   * and never persisted — `setTableMetadata` skips every `_`-prefixed key.
   */
  _metadataFetchFailed?: true;
}

/**
 * Bug-8424: did this metadata read FAIL, as opposed to come back genuinely
 * empty? Exported so the producer (`getTableMetadata`) and every consumer
 * agree on one predicate instead of each re-testing the raw key.
 */
export function isMetadataFetchFailure(metadata: Partial<TableMetadata>): boolean {
  return metadata._metadataFetchFailed === true;
}

/**
 * Extract the sheet name and start cell from a range address.
 * E.g. "Sheet1!A1:D10" -> { sheetName: "Sheet1", startCell: "A1", endCell: "D10" }
 */
function parseRangeAddress(rangeAddress: string): { sheetName: string; startCell: string; endCell: string } {
  // F-35: Handle quoted sheet names like "'My Sheet'!A1:D10" or "'It''s a Sheet'!A1:D10"
  const quotedMatch = rangeAddress.match(/^'([^']*(?:''[^']*)*)'!(.+)$/);
  if (quotedMatch) {
    const sheetName = quotedMatch[1].replace(/''/g, "'");
    const rangePart = quotedMatch[2];
    const cells = rangePart.split(':');
    // Bug-7397 fix #1: strip $ (absolute refs) and uppercase cell addresses
    // so $A$1 / a1 / A1 all produce the same canonical key.
    return {
      sheetName,
      startCell: (cells[0] || rangePart).replace(/\$/g, '').toUpperCase(),
      endCell: (cells[1] || cells[0] || rangePart).replace(/\$/g, '').toUpperCase(),
    };
  }
  const parts = rangeAddress.split('!');
  if (parts.length === 2) {
    const rangePart = parts[1] || parts[0];
    const cells = rangePart.split(':');
    return {
      sheetName: parts[0],
      startCell: (cells[0] || rangePart).replace(/\$/g, '').toUpperCase(),
      endCell: (cells[1] || cells[0] || rangePart).replace(/\$/g, '').toUpperCase(),
    };
  }
  return { sheetName: '', startCell: rangeAddress.replace(/\$/g, '').toUpperCase(), endCell: rangeAddress.replace(/\$/g, '').toUpperCase() };
}

/**
 * F-025-19: Excel named-item names forbid spaces and most punctuation, and a
 * sheet name embedded directly in a name breaks for sheets like "Q1 Report" or
 * names containing the "_" delimiter we split on. Hash the sheet name into a
 * short stable token so the named-item key is always a legal identifier and the
 * slow-path key split is unambiguous. Deterministic (same sheet -> same token).
 */
export function hashSheetName(sheetName: string): string {
  // Note: case-insensitive hashing was attempted in round 5 but reverted
  // because it orphans every named item written by previous builds (the hash
  // changes, so existing metadata becomes unfindable). Office always returns
  // consistent casing for sheet names (range.address, selection address), so
  // the pre-fix code was already self-consistent at runtime; the theoretical
  // 'sheet1' vs 'Sheet1' scenario is not reachable via real Office APIs.
  let h = 5381;
  for (let i = 0; i < sheetName.length; i++) {
    h = ((h << 5) + h + sheetName.charCodeAt(i)) >>> 0;
  }
  return `s${h.toString(36)}`;
}

/**
 * F-025-19: quote a sheet name for use in a range reference when it contains a
 * space or punctuation, per Excel's formula rules ('My Sheet'!A1). A literal
 * single quote inside the name is doubled.
 */
export function quoteSheetRef(sheetName: string, rangeRef: string): string {
  if (/^[A-Za-z_][A-Za-z0-9_.]*$/.test(sheetName)) {
    return `${sheetName}!${rangeRef}`;
  }
  return `'${sheetName.replace(/'/g, "''")}'!${rangeRef}`;
}

/**
 * Parse a cell reference like "A1" into { col: 0, row: 0 } (0-indexed).
 */
function parseCellRef(cell: string): { col: number; row: number } {
  // Bug-7397 fix #1: strip $ so absolute refs ($A$1) are handled correctly.
  const cleaned = cell.replace(/\$/g, '');
  const match = cleaned.match(/^([A-Z]+)(\d+)$/i);
  if (!match) return { col: 0, row: 0 };
  const colStr = match[1].toUpperCase();
  let col = 0;
  for (let i = 0; i < colStr.length; i++) {
    col = col * 26 + (colStr.charCodeAt(i) - 64);
  }
  return { col: col - 1, row: parseInt(match[2], 10) - 1 };
}

/**
 * Check if a cell address falls within a range address.
 */
function isCellInRange(cellAddress: string, rangeAddress: string): boolean {
  const cellParsed = parseRangeAddress(cellAddress);
  const rangeParsed = parseRangeAddress(rangeAddress);
  // Bug-7397 fix #1: Excel sheet names are case-insensitive.
  if (cellParsed.sheetName.toLowerCase() !== rangeParsed.sheetName.toLowerCase()) return false;
  const cell = parseCellRef(cellParsed.startCell);
  const start = parseCellRef(rangeParsed.startCell);
  const end = parseCellRef(rangeParsed.endCell);
  return cell.col >= start.col && cell.col <= end.col &&
    cell.row >= start.row && cell.row <= end.row;
}

/**
 * Bug-7397 R6: per-table MUTUAL-EXCLUSION lock (the primary correctness
 * mechanism for the task-pane concurrency race).
 *
 * Rounds 1-5 tried to DETECT staleness after the fact (a plain-key-wins
 * heuristic, then a global invalidation epoch, then a per-table version map
 * re-checked at read time). Three consecutive external cross-family gates
 * rejected every variant for the same structural reason: a detector has a
 * sampling-instant gap. A signal sampled AFTER a host read cannot see a write
 * that landed DURING the read; a re-read on mismatch has no fixpoint (a second
 * write during the re-read is uncaught); and the whole class is blind to the
 * insert path's DATA write, which happens before any version is bumped.
 *
 * The fix is exclusion, not detection: neither operation may begin touching a
 * table that the other is currently touching. This lock serializes the ENTIRE
 * per-table operation on BOTH sides of the race:
 *   - INSERT (useExcel.ts): the cell-data write (insertResultTable) AND the
 *     provenance write (setTableMetadataWithinLock) run as ONE critical
 *     section under the table's lock.
 *   - REFRESH (tableRefresh.ts): reading current metadata, executing the
 *     query, rewriting the table body, and writing back metadata run as ONE
 *     critical section under the SAME table's lock.
 * So an insert's data write can no longer land inside a refresh's delete/add
 * sequence, and a refresh can no longer act on a snapshot a concurrent insert
 * has already superseded.
 *
 * The mechanism is a per-`rangeKey` promise chain: each acquirer queues its
 * `fn` behind whatever call is already pending for that same table. Because
 * the chain is threaded through `.then`, it genuinely serializes across every
 * `await` INSIDE `fn` -- including a live `Excel.run` host round trip -- not
 * merely a synchronous block: the next acquirer's `fn` cannot start until this
 * acquirer's returned promise settles. A concurrent acquirer for the same
 * table therefore WAITS (it does not fail, drop, or overwrite) until the
 * in-flight operation completes, up to a bounded deadline (see
 * DEFAULT_LOCK_ACQUIRE_TIMEOUT_MS).
 *
 * Bug-7397 R12-3 -- WHAT CAN ACTUALLY WEDGE A LOCK. An earlier revision of this
 * comment claimed a hung `/plugin/execute` network call would hold the blocks.
 * That is FALSE: `tableRefresh.refreshTable` executes the query in its UNLOCKED
 * Phase A (`executeQuery` is awaited before `withTableLocksKeys` is ever
 * called), precisely so a slow server never holds cells. The REAL exposure is a
 * hung OFFICE HOST call inside the locked phase -- an `Excel.run` /
 * `context.sync()` in the rewrite, write-back, or pinned cell-write critical
 * section that never resolves. Nothing in this module can force such a call to
 * return, so a wedged holder stays wedged; what we CAN guarantee is that every
 * subsequent acquirer fails loudly and quickly instead of hanging invisibly.
 * `withRangeWriteLock` therefore bounds only the WAIT, never the critical
 * section: on expiry the waiter abandons its queued `fn` (it is never run
 * afterwards, so no surprise write lands minutes later) and rejects with
 * `LockAcquireTimeoutError`, which callers surface as a "busy, try again"
 * message. Timing out the critical section instead would be unsound -- it would
 * admit a second writer into cells the first may still be mutating, which is
 * exactly the wrong-numbers class this lock exists to prevent.
 *
 * Different tables use different keys and run fully concurrently (no global
 * lock).
 *
 * Every caller lives in this one single-threaded task-pane JS realm
 * (`useExcel.ts`, `tableRefresh.ts`); the Excel custom-functions runtime is a
 * separate isolated WWAHost realm that never imports this module (pinned by
 * the import-boundary guard in workbookMetadata.test.ts), so one shared
 * `_rangeWriteLocks` Map is sufficient. The `reassembleChunkedMetadata`
 * plain-value preference remains only a bounded defense for debris from a
 * pre-fix build or any future write path that bypasses this lock.
 */
const _rangeWriteLocks = new Map<string, Promise<unknown>>();

/**
 * Canonical per-table lock key. IDENTICAL to the key `setTableMetadata` writes
 * named items under, so the insert path, the refresh path, and the metadata
 * writer all serialize against each other for the same physical table:
 * sheet-name hashed (space/punctuation-safe, no `_` delimiter) and start cell
 * `$`-stripped + upper-cased ($A$1 / a1 / A1 all collapse to one key).
 */
export function tableRangeKey(rangeAddress: string): string {
  const { sheetName, startCell } = parseRangeAddress(rangeAddress);
  return `${hashSheetName(sheetName)}_${startCell}`;
}

/**
 * Bug-7397 R12-3: raised when an acquirer gave up WAITING for a cell block.
 * Nothing was written. Callers must surface it as a user-visible "this is
 * taking too long, try again" outcome -- never swallow it into a silent no-op
 * (silence is indistinguishable from a broken feature) and never proceed with
 * the write (that would defeat the exclusion the lock exists to provide).
 */
export class LockAcquireTimeoutError extends Error {
  readonly rangeKey: string;
  constructor(rangeKey: string) {
    super(`Timed out waiting for workbook cell-block lock "${rangeKey}"`);
    this.name = 'LockAcquireTimeoutError';
    this.rangeKey = rangeKey;
  }
}

/**
 * Bug-7397 R12-3: how long an acquirer waits for a held cell block before
 * giving up. Sized for the slowest legitimate holder -- a locked critical
 * section is only Office host round trips (a resize + values write + a
 * named-item batch); the network query already ran unlocked. A wait longer than
 * this means the holder is wedged (a `context.sync()` that never resolved), and
 * a user staring at a frozen button learns nothing, so we fail loudly instead.
 */
export const DEFAULT_LOCK_ACQUIRE_TIMEOUT_MS = 20_000;

function withRangeWriteLock<T>(
  rangeKey: string,
  fn: () => Promise<T>,
  // Absolute epoch-ms deadline for ACQUISITION (shared across a multi-key
  // acquisition so the whole set is bounded, not each key independently).
  // Omitted = wait indefinitely (kept only for internal callers that are
  // already inside a bounded outer acquisition).
  deadline?: number,
): Promise<T> {
  // Is anyone (possibly) still holding this key? When the map has no entry the
  // key is genuinely free: `enter` runs on the very next microtask, there is
  // nothing to wait FOR, and arming an acquisition timer would only add a
  // promise hop to the uncontended path (which is the overwhelmingly common
  // one). The timeout exists for CONTENTION, so only contention arms it.
  const contended = _rangeWriteLocks.has(rangeKey);
  const prior = _rangeWriteLocks.get(rangeKey) ?? Promise.resolve();
  let abandoned = false;
  let timer: ReturnType<typeof setTimeout> | null = null;
  const clearTimer = (): void => {
    if (timer !== null) {
      clearTimeout(timer);
      timer = null;
    }
  };

  // The queued critical section. If the waiter already gave up, it must NOT
  // run: the user was told the operation did not happen, so a write landing
  // whenever the wedged holder eventually frees the key would be a surprise
  // mutation. Rejecting instead keeps the chain moving for the NEXT acquirer.
  const enter = (): Promise<T> => {
    if (abandoned) return Promise.reject(new LockAcquireTimeoutError(rangeKey));
    clearTimer(); // the wait is over; the critical section itself is never timed out
    return fn();
  };

  // Chain after the prior holder for this table regardless of whether it
  // succeeded or failed, so one acquirer's error can never wedge the queue for
  // the next one.
  const chained = prior.then(enter, enter);
  const tracked = chained.catch(() => undefined);
  _rangeWriteLocks.set(rangeKey, tracked);
  // Once this operation settles, drop the map entry IF nothing else queued
  // behind it in the meantime -- otherwise the map would retain one resolved
  // promise per table ever touched, for the life of the task pane.
  void tracked.finally(() => {
    clearTimer();
    if (_rangeWriteLocks.get(rangeKey) === tracked) {
      _rangeWriteLocks.delete(rangeKey);
    }
  });

  if (deadline === undefined || !contended) return chained;

  return new Promise<T>((resolve, reject) => {
    timer = setTimeout(() => {
      timer = null;
      abandoned = true;
      reject(new LockAcquireTimeoutError(rangeKey));
    }, Math.max(0, deadline - Date.now()));
    // `enter` runs on a microtask when the key is free, so it always clears the
    // timer before a macrotask timer callback could fire.
    chained.then(
      (value) => { clearTimer(); resolve(value); },
      (err) => { clearTimer(); reject(err); },
    );
  });
}

/**
 * Bug-7397 R6/R9: acquire ONE lock by a PRE-COMPUTED key and run `fn` holding
 * it. The key namespace is the spatial CELL BLOCK (see cellBlockKeys) -- every
 * operation touching a set of cells locks the blocks covering them, so
 * operations on overlapping cells share a key by construction. NOT re-entrant:
 * code inside `fn` must never re-acquire the SAME key (or call the self-locking
 * `setTableMetadata` for overlapping cells) -- it would deadlock behind its own
 * unsettled promise. Use `setTableMetadataWithinLock` for a nested metadata
 * write. Callers almost always use `withTableLocksKeys` (a whole block set).
 */
export function withTableLockKey<T>(
  rangeKey: string,
  fn: () => Promise<T>,
  deadline?: number,
): Promise<T> {
  return withRangeWriteLock(rangeKey, fn, deadline);
}

/** A written rectangle in 0-indexed row/col terms (the insert's full extent). */
export interface TargetRect {
  startRow: number;
  startCol: number;
  rowCount: number;
  colCount: number;
}

/**
 * Bug-7397 R9: SPATIAL BLOCK LOCK size. The sheet grid is partitioned into
 * fixed BLOCK_SIZE x BLOCK_SIZE cell blocks; a lock key names one block. This
 * is the key insight that ends the detector class the first 8 rounds cycled
 * through (version -> epoch -> __table_range -> reserve): lock keys derive from
 * the CELLS an operation touches, not from table identities. Two operations
 * whose rectangles overlap NECESSARILY share at least one block, so they
 * acquire a common key and are mutually excluded BY CONSTRUCTION -- no
 * after-the-fact signal to publish and re-read, no sampling-instant gap.
 * Rectangles more than BLOCK_SIZE cells apart fall in different blocks and run
 * concurrently. This is COARSER than the old per-table guarantee: two DIFFERENT
 * tables closer than BLOCK_SIZE (e.g. Sheet1!A1:D5 and Sheet1!F1:J5 both in
 * block 0,0) now serialize. That is a deliberate correctness-over-concurrency
 * trade -- refreshes/inserts are user-initiated and rare, and false
 * serialization only ever makes one wait, never corrupts. A table costs
 * ceil(rows/BLOCK) x ceil(cols/BLOCK) keys -- small for realistic sizes.
 */
const BLOCK_SIZE = 64;

/**
 * Bug-7397 R9: the sorted, de-duplicated block-lock keys covering the cell
 * rectangle [startRow..endRow] x [startCol..endCol] on `sheetName`. Sorting
 * makes multi-block acquisition deadlock-free (every acquirer takes blocks in
 * the same total order).
 */
export function cellBlockKeys(
  sheetName: string,
  startRow: number,
  startCol: number,
  endRow: number,
  endCol: number,
): string[] {
  const h = hashSheetName(sheetName);
  const r0 = Math.floor(Math.min(startRow, endRow) / BLOCK_SIZE);
  const r1 = Math.floor(Math.max(startRow, endRow) / BLOCK_SIZE);
  const c0 = Math.floor(Math.min(startCol, endCol) / BLOCK_SIZE);
  const c1 = Math.floor(Math.max(startCol, endCol) / BLOCK_SIZE);
  const keys: string[] = [];
  for (let br = r0; br <= r1; br++) {
    for (let bc = c0; bc <= c1; bc++) {
      keys.push(`${h}#b${br}_${bc}`);
    }
  }
  return keys.sort();
}

/** Block-lock keys covering the cells of a range ADDRESS (e.g. 'Sheet1!A1:D10'). */
export function blockKeysForAddress(rangeAddress: string): string[] {
  const { sheetName, startCell, endCell } = parseRangeAddress(rangeAddress);
  const s = parseCellRef(startCell);
  const e = parseCellRef(endCell);
  return cellBlockKeys(sheetName, s.row, s.col, e.row, e.col);
}

/** 0-indexed inclusive row/col bounds of a range ADDRESS (normalised so end >= start). */
export function rangeRowColBounds(rangeAddress: string): { startRow: number; startCol: number; endRow: number; endCol: number } {
  const { startCell, endCell } = parseRangeAddress(rangeAddress);
  const s = parseCellRef(startCell);
  const e = parseCellRef(endCell);
  return {
    startRow: Math.min(s.row, e.row),
    startCol: Math.min(s.col, e.col),
    endRow: Math.max(s.row, e.row),
    endCol: Math.max(s.col, e.col),
  };
}

/** Block-lock keys covering a TargetRect (rowCount x colCount from a start cell). */
export function blockKeysForRect(sheetName: string, rect: TargetRect): string[] {
  return cellBlockKeys(
    sheetName,
    rect.startRow,
    rect.startCol,
    rect.startRow + Math.max(1, rect.rowCount) - 1,
    rect.startCol + Math.max(1, rect.colCount) - 1,
  );
}

/**
 * Bug-7397 R7/R9: acquire MULTIPLE locks and run `fn` holding all of them.
 * Keys are sorted internally so every acquirer takes them in the same total
 * order -- that makes multi-key acquisition deadlock-free even when two
 * operations request overlapping-but-different key sets. Nested
 * `withTableLockKey` calls chain each key behind whatever holds it, so `fn`
 * runs only once every key is free.
 */
export function withTableLocksKeys<T>(
  rangeKeys: string[],
  fn: () => Promise<T>,
  // Bug-7397 R12-3: acquisition is bounded by ONE deadline spanning the whole
  // key set (not per key), so a caller waiting on five blocks cannot wait 5x
  // the budget. On expiry the returned promise rejects with
  // LockAcquireTimeoutError and `fn` never runs -- nothing is written.
  options?: { timeoutMs?: number },
): Promise<T> {
  const unique = Array.from(new Set(rangeKeys)).sort();
  const deadline = Date.now() + (options?.timeoutMs ?? DEFAULT_LOCK_ACQUIRE_TIMEOUT_MS);
  const acquire = (i: number): Promise<T> =>
    i >= unique.length ? fn() : withTableLockKey(unique[i], () => acquire(i + 1), deadline);
  return acquire(0);
}

/**
 * Persist table provenance. Public, self-locking entry point for STANDALONE
 * callers (a metadata write not already inside a held per-table critical
 * section). Callers that already hold the covering block locks (via `withTableLocksKeys`)
 * (the insert path in useExcel.ts, the refresh write-back in tableRefresh.ts)
 * MUST call `setTableMetadataWithinLock` instead to avoid re-entrant deadlock.
 */
export async function setTableMetadata(
  rangeAddress: string,
  metadata: TableMetadata,
): Promise<void> {
  // Bug-7397 R9: serialize on the CELL BLOCKS this range occupies, so a
  // standalone metadata write cannot interleave with an insert/refresh
  // touching the same cells (they share a block key by construction).
  return withTableLocksKeys(blockKeysForAddress(rangeAddress), () => setTableMetadataWithinLock(rangeAddress, metadata));
}

/**
 * Bug-7397 R6: the metadata-write core, WITHOUT acquiring the table lock. The
 * caller MUST already hold the covering block locks (via `withTableLocksKeys`). Splitting
 * the lock acquisition out of the write is what lets the insert and refresh
 * paths put the data write AND the metadata write inside ONE critical section
 * (a single held block-lock set) instead of two separate acquisitions with an
 * exploitable gap between them.
 */
export async function setTableMetadataWithinLock(
  rangeAddress: string,
  metadata: TableMetadata,
): Promise<void> {
  const { sheetName: lockSheetName, startCell: lockStartCell } = parseRangeAddress(rangeAddress);
  const lockKey = `${hashSheetName(lockSheetName)}_${lockStartCell}`;
  try {
    await Excel.run(async (context) => {
      const { sheetName, startCell } = parseRangeAddress(rangeAddress);
      // F-025-19: hash the sheet name so the named-item key is always a legal
      // identifier (sheets like "Q1 Report" or names with "_" no longer break
      // the name rules or the slow-path key split).
      const rangeKey = `${hashSheetName(sheetName)}_${startCell}`;

      // Remove old named items for this table range first. Bug-7397: the two
      // context.sync() calls below are the read-then-write pair the module
      // comment above describes; withRangeWriteLock is what actually prevents
      // another writer for this table from interleaving between them.
      const namedItems = context.workbook.names;
      namedItems.load('items/name, items/comment');
      await context.sync();

      const staleNames = namedItems.items
        .filter(item => item.name.startsWith(`${METADATA_PREFIX}${rangeKey}_`))
        .map(item => item.name);

      for (const staleName of staleNames) {
        context.workbook.names.getItemOrNullObject(staleName).delete();
      }

      // Write new metadata entries with deterministic keys
      const rangeRef = rangeAddress.split('!')[1] || rangeAddress;
      // F-025-19: quote the sheet name in the range reference when it contains
      // spaces/punctuation, otherwise "My Sheet!A1:D10" is an invalid formula
      // and the named-item add throws (silently losing all provenance).
      const sheetRef = quoteSheetRef(sheetName, rangeRef);
      for (const [key, value] of Object.entries(metadata)) {
        // Bug-7397 fix #7: strip synthetic/internal-only keys (e.g.
        // _tableStart) so they are never persisted as named items.
        if (key.startsWith('_')) continue;
        if (value !== undefined && value !== null) {
          // Bug-7397: chunk over-length values so no comment exceeds Excel's
          // 255-char cap (which would silently truncate/drop provenance).
          for (const { suffix, comment } of encodeMetadataEntry(key, String(value))) {
            const name = `${METADATA_PREFIX}${rangeKey}_${suffix}`;
            try {
              const namedItem = context.workbook.names.add(name, sheetRef);
              namedItem.comment = comment;
              // Bug-7397: keep provenance items OUT of the Name Manager (the
              // module contract says "hidden"); chunking multiplies their count.
              // Hidden names still load via names.load('items/...').
              namedItem.visible = false;
            } catch {
              // Named item may already exist; skip. NOTE (Bug-7397 F-4,
              // deep-review finding): Office.js batches `.add()` -- a
              // duplicate-name failure actually surfaces when the SHARED
              // `context.sync()` below executes, not synchronously here, so
              // this catch does not reliably intercept it per entry; a real
              // collision instead rejects the whole batch, caught by
              // setTableMetadata's outer try/catch (the entire write for this
              // table is then dropped, not just the one colliding entry). The
              // write lock above makes a same-table duplicate very unlikely
              // (this writer's own stale-name deletes always precede its
              // adds), but this per-add catch is not a substitute for it.
            }
          }
        }
      }

      // Store the full table range as a named item for containment lookup
      try {
        const rangeItem = context.workbook.names.add(
          `${METADATA_PREFIX}${rangeKey}__table_range`,
          sheetRef,
        );
        rangeItem.comment = `__table_range=${rangeAddress}`;
        rangeItem.visible = false; // Bug-7397: keep out of the Name Manager
      } catch {
        // non-critical
      }

      await context.sync();
    });
  } catch (err) {
    // Bug-7397 fix #4: surface errors instead of swallowing silently. A
    // sync failure after stale-name deletes but before new-name adds means
    // the table lost its old provenance AND got no new one -- callers MUST
    // be able to detect this rather than assuming a silent success.
    if (typeof console !== 'undefined' && console.warn) {
      console.warn('[tessallite] setTableMetadata failed:', err);
    }
    throw err;
  } finally {
    // Bug-7397 F-1: invalidate AFTER this write settles (success or failure),
    // not before it even starts. Invalidating up front used to leave a window
    // where a read landing between the early invalidation and this write's
    // actual completion would re-populate the cache with a transiently-stale
    // value, and nothing would be left to clear it once this write finally
    // commits (Refresh/Drill would then silently use that stale value for up
    // to METADATA_CACHE_TTL_MS). Purging by rangeKey (not just this call's own
    // `rangeAddress` string) also clears any slow-path entries cached under a
    // DIFFERENT address for this same table (F-2; see cacheMetadata above).
    // Also bumps this table's version counter, which the refresh path reads
    // only as a defensive backstop assertion (the lock, not this counter, is
    // what prevents the race).
    invalidateMetadataCacheForRangeKey(lockKey);
  }
}

/**
 * Bug-7397 R6: delete every named item that stores provenance for the table at
 * `rangeAddress`, WITHOUT acquiring locks (the caller must already
 * hold the covering block locks). Used by the refresh path to clean up orphaned
 * provenance under a stale key when a table's start cell moves. Best-effort:
 * a failure is logged, not thrown, because the caller has already written the
 * authoritative provenance under the new key.
 */
export async function removeTableMetadataWithinLock(rangeAddress: string): Promise<void> {
  const { sheetName, startCell } = parseRangeAddress(rangeAddress);
  const rangeKey = `${hashSheetName(sheetName)}_${startCell}`;
  try {
    await Excel.run(async (context) => {
      const namedItems = context.workbook.names;
      namedItems.load('items/name');
      await context.sync();
      const staleNames = namedItems.items
        .filter(item => item.name.startsWith(`${METADATA_PREFIX}${rangeKey}_`))
        .map(item => item.name);
      for (const staleName of staleNames) {
        context.workbook.names.getItemOrNullObject(staleName).delete();
      }
      await context.sync();
    });
  } catch (err) {
    if (typeof console !== 'undefined' && console.warn) {
      console.warn('[tessallite] removeTableMetadataWithinLock failed:', err);
    }
  } finally {
    invalidateMetadataCacheForRangeKey(rangeKey);
  }
}

/** 0-indexed column -> A1 letters (0 -> A, 26 -> AA). */
function columnIndexToLetters(col: number): string {
  let s = '';
  let c = col + 1;
  while (c > 0) {
    const r = (c - 1) % 26;
    s = String.fromCharCode(65 + r) + s;
    c = Math.floor((c - 1) / 26);
  }
  return s || 'A';
}

/** 0-indexed (row, col) -> A1 cell address (0,0 -> A1). */
function cellA1(row: number, col: number): string {
  return `${columnIndexToLetters(col)}${row + 1}`;
}

/**
 * Bug-7397 R8-1: the range address covering the UNION of a table's current
 * extent (`currentAddress`) and the extent it is ABOUT to grow into
 * (`intendedRows` tall x `intendedCols` wide from its start cell). A refresh
 * publishes this claim BEFORE it mutates cells so a concurrent insert into the
 * growth zone sees the (grown) extent and serializes against the refresh.
 */
export function unionTableRangeAddress(
  currentAddress: string,
  intendedRows: number,
  intendedCols: number,
): string {
  const { sheetName, startCell, endCell } = parseRangeAddress(currentAddress);
  const s = parseCellRef(startCell);
  const e = parseCellRef(endCell);
  const intendedEndRow = s.row + Math.max(0, intendedRows - 1);
  const intendedEndCol = s.col + Math.max(0, intendedCols - 1);
  const endRow = Math.max(e.row, intendedEndRow);
  const endCol = Math.max(e.col, intendedEndCol);
  const rangeRef = `${cellA1(s.row, s.col)}:${cellA1(endRow, endCol)}`;
  return quoteSheetRef(sheetName, rangeRef);
}

/**
 * Bug-7397 R12-2: extend a range address downward by `extraRows`.
 *
 * The refresh path uses this to pull the PROVENANCE FOOTER ROW inside the
 * region it locks and rewrites. The footer sits one row below the table body,
 * so a refresh that grows the table writes result data over the footer's old
 * cell, and a refresh that shrinks it leaves the footer stranded below blank
 * rows. Extending the union by one row covers BOTH the footer's current
 * position (current extent + 1) and its intended position (intended extent + 1)
 * -- max(a,b) + 1 == max(a + 1, b + 1) -- so a single extension is exact, not
 * an approximation.
 */
export function expandRangeRows(rangeAddress: string, extraRows: number): string {
  const { sheetName, startCell, endCell } = parseRangeAddress(rangeAddress);
  const s = parseCellRef(startCell);
  const e = parseCellRef(endCell);
  const endRow = Math.max(s.row, e.row) + Math.max(0, extraRows);
  const rangeRef = `${cellA1(Math.min(s.row, e.row), Math.min(s.col, e.col))}:${cellA1(endRow, Math.max(s.col, e.col))}`;
  return quoteSheetRef(sheetName, rangeRef);
}

// ---- Entity usage manifest (Phase 5) ----

declare const OfficeRuntime: {
  storage: {
    getItem(key: string): Promise<string | null>;
    setItem(key: string, value: string): Promise<void>;
    removeItem(key: string): Promise<void>;
  };
};

export interface EntityUsageEntry {
  id: string;
  type: 'named_set' | 'kpi';
  displayName: string;
  certificationStatus?: string;
  updatedAt?: string;
  insertedAt: string;
  cellLocations: string[];
  // F-025-16: the workbook and model the entity was inserted into. The manifest
  // lives in OfficeRuntime.storage, which is per-add-in per-MACHINE — shared
  // across every workbook the user opens. Without these scoping keys, inserting
  // a KPI from model A in workbook 1 raised a spurious "Deleted from server"
  // warning the moment the user switched to model B or opened workbook 2.
  // Entries are now only compared against the model they belong to, in the
  // workbook they were inserted into. Legacy entries (written before this fix)
  // have neither key and are treated as belonging to the current scope so they
  // are not orphaned.
  workbookId?: string;
  modelId?: string;
}

// F-025-16: a stable per-workbook identifier persisted in the workbook's own
// settings (Office.context.document.settings), so it travels with the .xlsx
// file and is distinct per workbook. Read once and memoised per session.
let _workbookIdPromise: Promise<string | null> | null = null;
const WORKBOOK_ID_SETTING = 'tessallite_workbook_id';

function generateId(): string {
  if (typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function') {
    return crypto.randomUUID();
  }
  return `wb_${Date.now()}_${Math.random().toString(36).slice(2, 10)}`;
}

export async function getWorkbookId(): Promise<string | null> {
  if (_workbookIdPromise) return _workbookIdPromise;
  _workbookIdPromise = (async () => {
    try {
      if (typeof Office === 'undefined' || !Office.context?.document?.settings) return null;
      const settings = Office.context.document.settings;
      const existing = settings.get(WORKBOOK_ID_SETTING) as string | null;
      if (existing) return existing;
      const fresh = generateId();
      settings.set(WORKBOOK_ID_SETTING, fresh);
      await new Promise<void>((resolve) => {
        settings.saveAsync(() => resolve());
      });
      return fresh;
    } catch {
      return null;
    }
  })();
  return _workbookIdPromise;
}

/** Test-only: reset the memoised workbook id between cases. */
export function _resetWorkbookIdCache(): void {
  _workbookIdPromise = null;
}

/**
 * F-025-16: an entry belongs to the active scope when its workbook AND model
 * match the current context. Legacy entries with no scope keys (written before
 * this fix) are treated as in-scope so they are not falsely orphaned.
 */
function entryInScope(
  entry: EntityUsageEntry,
  workbookId: string | null,
  modelId: string | null,
): boolean {
  if (entry.workbookId && workbookId && entry.workbookId !== workbookId) return false;
  if (entry.modelId && modelId && entry.modelId !== modelId) return false;
  return true;
}

export interface EntityManifest {
  version: 1;
  entries: EntityUsageEntry[];
}

const MANIFEST_XML_KEY = 'tessallite_entity_manifest';

async function readManifestFromStorage(): Promise<EntityManifest> {
  const empty: EntityManifest = { version: 1, entries: [] };
  try {
    const raw = await OfficeRuntime.storage.getItem(MANIFEST_XML_KEY);
    if (!raw) return empty;
    return JSON.parse(raw) as EntityManifest;
  } catch {
    return empty;
  }
}

async function writeManifestToStorage(manifest: EntityManifest): Promise<void> {
  try {
    await OfficeRuntime.storage.setItem(MANIFEST_XML_KEY, JSON.stringify(manifest));
  } catch {
    // non-critical
  }
}

export async function trackEntityUsage(
  type: 'named_set' | 'kpi',
  entityId: string,
  displayName: string,
  cellAddress: string,
  certificationStatus?: string,
  updatedAt?: string,
  modelId?: string,
): Promise<void> {
  // F-025-16: tag the entry with its workbook + model so later staleness checks
  // only fire when the same model is loaded in the same workbook.
  const workbookId = await getWorkbookId();
  const manifest = await readManifestFromStorage();
  const existing = manifest.entries.find(
    e => e.id === entityId && e.type === type &&
      (e.workbookId ?? workbookId ?? null) === (workbookId ?? null) &&
      (e.modelId ?? modelId ?? null) === (modelId ?? null),
  );
  if (existing) {
    if (!existing.cellLocations.includes(cellAddress)) {
      existing.cellLocations.push(cellAddress);
    }
    if (certificationStatus) existing.certificationStatus = certificationStatus;
    if (updatedAt) existing.updatedAt = updatedAt;
    if (workbookId && !existing.workbookId) existing.workbookId = workbookId;
    if (modelId && !existing.modelId) existing.modelId = modelId;
  } else {
    manifest.entries.push({
      id: entityId,
      type,
      displayName,
      certificationStatus,
      updatedAt,
      insertedAt: new Date().toISOString(),
      cellLocations: [cellAddress],
      workbookId: workbookId ?? undefined,
      modelId,
    });
  }
  await writeManifestToStorage(manifest);
}

export async function getEntityManifest(): Promise<EntityManifest> {
  return readManifestFromStorage();
}

export interface StaleEntity {
  entry: EntityUsageEntry;
  currentStatus: string;
  reason: 'deprecated' | 'deleted' | 'status_changed' | 'version_changed';
}

export async function checkStaleEntities(
  currentEntities: { id: string; type: 'named_set' | 'kpi'; certification_status: string; updated_at?: string }[],
  modelId?: string,
): Promise<StaleEntity[]> {
  // F-025-16: only inspect manifest entries that belong to THIS workbook and
  // THIS model. The supplied `currentEntities` are the loaded model's entities,
  // so an entry for a different model/workbook must never be reported "deleted".
  const workbookId = await getWorkbookId();
  const manifest = await readManifestFromStorage();
  const stale: StaleEntity[] = [];

  const lookup = new Map(currentEntities.map(e => [`${e.type}:${e.id}`, e]));

  for (const entry of manifest.entries) {
    if (!entryInScope(entry, workbookId, modelId ?? null)) continue;
    const key = `${entry.type}:${entry.id}`;
    const current = lookup.get(key);

    if (!current) {
      stale.push({ entry, currentStatus: 'deleted', reason: 'deleted' });
    } else if (current.certification_status === 'deprecated' && entry.certificationStatus !== 'deprecated') {
      stale.push({ entry, currentStatus: current.certification_status, reason: 'deprecated' });
    } else if (entry.certificationStatus && current.certification_status !== entry.certificationStatus) {
      stale.push({ entry, currentStatus: current.certification_status, reason: 'status_changed' });
    } else if (entry.updatedAt && current.updated_at && entry.updatedAt !== current.updated_at) {
      stale.push({ entry, currentStatus: current.certification_status, reason: 'version_changed' });
    }
  }

  return stale;
}

export async function updateManifestStatuses(
  currentEntities: { id: string; type: 'named_set' | 'kpi'; certification_status: string; updated_at?: string }[],
  skipUpdatedAtKeys?: Set<string>,
  modelId?: string,
): Promise<void> {
  // F-025-16: only reconcile entries belonging to the active workbook + model.
  const workbookId = await getWorkbookId();
  const manifest = await readManifestFromStorage();
  const lookup = new Map(currentEntities.map(e => [`${e.type}:${e.id}`, e]));

  for (const entry of manifest.entries) {
    if (!entryInScope(entry, workbookId, modelId ?? null)) continue;
    const key = `${entry.type}:${entry.id}`;
    const current = lookup.get(key);
    if (current) {
      entry.certificationStatus = current.certification_status;
      if (current.updated_at && !skipUpdatedAtKeys?.has(key)) {
        entry.updatedAt = current.updated_at;
      }
    }
  }

  await writeManifestToStorage(manifest);
}

export async function removeEntityFromManifest(
  type: 'named_set' | 'kpi',
  entityId: string,
): Promise<void> {
  const manifest = await readManifestFromStorage();
  manifest.entries = manifest.entries.filter(e => !(e.id === entityId && e.type === type));
  await writeManifestToStorage(manifest);
}

/**
 * Bug-7397 R12-4: options for {@link getTableMetadata}.
 */
export interface GetTableMetadataOptions {
  /**
   * Skip the TTL cache and read the workbook. Required by any caller whose
   * correctness depends on the value reflecting the CURRENT host state rather
   * than a value that was true within the last {@link METADATA_CACHE_TTL_MS}
   * -- specifically the refresh path's under-lock re-validation, which exists
   * to answer "did anything change while I was executing the query?". A cached
   * answer to that question compares the original snapshot against itself and
   * can never say "yes". The stale entry for this address is dropped before the
   * read so a discarded result (see the epoch guard in `cacheMetadata`) cannot
   * leave the pre-read value behind for the next caller.
   */
  bypassCache?: boolean;
}

export async function getTableMetadata(
  rangeAddress: string,
  options?: GetTableMetadataOptions,
): Promise<Partial<TableMetadata>> {
  // F-26: Check TTL cache first
  const cached = _metadataCache.get(rangeAddress);
  if (cached) {
    if (!options?.bypassCache && (Date.now() - cached.ts) < METADATA_CACHE_TTL_MS) {
      return { ...cached.data };
    }
    // Bug-7397 fix #8: evict the expired entry (and its rangeKey index) so
    // the cache does not grow unboundedly over a long task-pane session.
    // Bug-7397 R12-4: a forced host read evicts too, for the reason above.
    _metadataCache.delete(rangeAddress);
    const addresses = _metadataCacheAddressesByRangeKey.get(cached.rangeKey);
    if (addresses) {
      addresses.delete(rangeAddress);
      if (addresses.size === 0) _metadataCacheAddressesByRangeKey.delete(cached.rangeKey);
    }
  }

  const metadata: Partial<TableMetadata> = {};
  // Bug-7397 F-1/F-2: the table this address resolves to, for cache indexing.
  // Defaults to the fast-path form (matches setTableMetadata's own rangeKey
  // whenever `rangeAddress` IS the table's start-cell address); the slow path
  // below overwrites it with the CONTAINING table's true key when the address
  // is some other cell inside the table instead.
  const { sheetName: outerSheetName, startCell: outerStartCell } = parseRangeAddress(rangeAddress);
  let cacheRangeKey = `${hashSheetName(outerSheetName)}_${outerStartCell}`;
  // Bug-7397 F-1 (round-3 follow-up): capture the invalidation epoch BEFORE
  // starting the read. If a write to this (or any) table invalidates the
  // cache while this read's own Excel.run round trip is still in flight, this
  // read's result is stale by construction and cacheMetadata below must
  // refuse to write it in, however long the read otherwise takes.
  const epochAtReadStart = _metadataCacheEpoch;
  try {
    await Excel.run(async (context) => {
      const { sheetName, startCell } = parseRangeAddress(rangeAddress);
      const namedItems = context.workbook.names;
      namedItems.load('items/name, items/comment');
      await context.sync();

      // First try exact match by start-cell key (fast path). F-025-19: keyed
      // by the hashed sheet name to match setTableMetadata.
      const rangeKey = `${hashSheetName(sheetName)}_${startCell}`;
      const directPrefix = `${METADATA_PREFIX}${rangeKey}_`;
      const directMatch = namedItems.items.find(item => item.name.startsWith(directPrefix));
      if (directMatch) {
        // Bug-7397 fix #7: _tableStart is a synthetic read-only key; the
        // key.startsWith('_') skip in setTableMetadata prevents persistence.
        // It is kept enumerable so that {…metadata} spread in cacheMetadata
        // carries it into cached entries (cellContext.ts reads it on every
        // selection-change, which almost always hits the cache).
        (metadata as Record<string, string>)['_tableStart'] = startCell;
        for (const item of namedItems.items) {
          if (item.name.startsWith(directPrefix) && item.comment && !item.name.endsWith('__table_range')) {
            const eqIdx = item.comment.indexOf('=');
            if (eqIdx > 0) {
              const key = item.comment.slice(0, eqIdx);
              const value = item.comment.slice(eqIdx + 1);
              (metadata as Record<string, string>)[key] = value;
            }
          }
        }
        // Fall through to the SINGLE reassemble + cache funnel after Excel.run
        // (this callback `return` only exits the callback; the code after
        // Excel.run always runs). Keeping one funnel means both the fast path
        // and the slow path are covered by the same reassembly call, so no
        // read path can regress independently. Bug-5799 caching is preserved by
        // that single funnel below.
        return;
      }

      // Slow path: search by range containment. F-025-19: keyed by the hashed
      // sheet name; the base-key split below is now unambiguous because the
      // hash token contains no "_" delimiter.
      const sheetPrefix = `${METADATA_PREFIX}${hashSheetName(sheetName)}_`;
      const rangeItems = namedItems.items.filter(
        item => item.name.startsWith(sheetPrefix) && item.name.endsWith('__table_range') && item.comment,
      );

      for (const rangeItem of rangeItems) {
        const commentMatch = rangeItem.comment.match(/^__table_range=(.+)$/);
        if (!commentMatch) continue;
        const tableRange = commentMatch[1];
        if (isCellInRange(rangeAddress, tableRange)) {
          // Found the containing table; extract its base key and start cell.
          // `baseKey` IS the table's true rangeKey (setTableMetadata names
          // this item `${METADATA_PREFIX}${rangeKey}__table_range`) -- use it
          // for cache indexing so this slow-path entry (cached under whatever
          // mid-table cell address the caller queried) is invalidated
          // together with every other address cached for the SAME table.
          const rangeItemSuffix = `__table_range`;
          const baseKey = rangeItem.name.slice(METADATA_PREFIX.length, -rangeItemSuffix.length);
          cacheRangeKey = baseKey;
          const parts = baseKey.split('_');
          const tableStartCell = parts.length >= 2 ? parts[parts.length - 1] : startCell;
          (metadata as Record<string, string>)['_tableStart'] = tableStartCell;
          const metaPrefix = `${METADATA_PREFIX}${baseKey}_`;
          for (const item of namedItems.items) {
            if (item.name.startsWith(metaPrefix) && item.comment && !item.name.endsWith('__table_range')) {
              const eqIdx = item.comment.indexOf('=');
              if (eqIdx > 0) {
                const key = item.comment.slice(0, eqIdx);
                const value = item.comment.slice(eqIdx + 1);
                (metadata as Record<string, string>)[key] = value;
              }
            }
          }
          return;
        }
      }
    });
  } catch (err) {
    // Bug-8424: a transient host error used to fall through to the funnel
    // below, which CACHED the empty `{}` for the full TTL. Every refresh in
    // that window then saw a table with no provenance and dropped it with no
    // skip reason at all — the one silent-degrade path in a refresh routine
    // that reports an honest reason everywhere else. Return a sentinel WITHOUT
    // caching, so the next read retries and the caller can say what happened.
    console.warn('getTableMetadata failed for', rangeAddress, err);
    return { _metadataFetchFailed: true };
  }
  // Bug-7397: the SINGLE reassemble + cache funnel for BOTH read paths (fast
  // and slow). Reassemble chunked values, then cache (Bug-5799) before use.
  // Bug-7397 F-1/F-2: cache by TABLE (cacheRangeKey), not just by the literal
  // address string, so setTableMetadata can invalidate every address ever
  // cached for this table regardless of how each caller addressed it.
  // Bug-7397 F-1 (round-3 follow-up): pass epochAtReadStart so a write that
  // invalidated the cache WHILE this read was in flight discards this now-
  // stale result instead of caching it.
  reassembleChunkedMetadata(metadata as Record<string, string>);
  cacheMetadata(rangeAddress, cacheRangeKey, { ...metadata }, epochAtReadStart);
  return metadata;
}
