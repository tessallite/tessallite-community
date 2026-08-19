import { describe, it, expect, vi, beforeEach } from 'vitest';
import {
  FunctionBatcher,
  computeShapeKey,
  computeResultKey,
  normalizeMemberValue,
  CollisionSafeResultMap,
  type BatchExecutor,
} from '../utils/functionBatcher';

describe('normalizeMemberValue (Bug-7394)', () => {
  it('trims whitespace', () => {
    expect(normalizeMemberValue('  hello  ')).toBe('hello');
  });

  it('case-folds to lowercase', () => {
    expect(normalizeMemberValue('EMEA')).toBe('emea');
    expect(normalizeMemberValue('Mixed')).toBe('mixed');
  });

  it('strips T00:00:00 ISO date suffix', () => {
    expect(normalizeMemberValue('2024-01-15T00:00:00')).toBe('2024-01-15');
  });

  it('strips T00:00:00Z ISO date suffix', () => {
    expect(normalizeMemberValue('2024-01-15T00:00:00Z')).toBe('2024-01-15');
  });

  it('strips T00:00:00.000 fractional ISO suffix', () => {
    expect(normalizeMemberValue('2024-01-15T00:00:00.000')).toBe('2024-01-15');
  });

  it('preserves non-midnight timestamps', () => {
    expect(normalizeMemberValue('2024-01-15T14:30:00')).toBe('2024-01-15t14:30:00');
  });

  it('strips trailing .0 from integer-like floats', () => {
    expect(normalizeMemberValue('5.0')).toBe('5');
    expect(normalizeMemberValue('12.00')).toBe('12');
    expect(normalizeMemberValue('-3.0')).toBe('-3');
  });

  it('preserves non-zero fractional parts', () => {
    expect(normalizeMemberValue('5.5')).toBe('5.5');
    expect(normalizeMemberValue('12.01')).toBe('12.01');
  });

  it('preserves plain strings', () => {
    expect(normalizeMemberValue('north america')).toBe('north america');
  });
});

describe('CollisionSafeResultMap (Bug-7394 adversarial R1: normalization must not merge distinct members)', () => {
  const K = 'cost::region=eu';

  it('stores and resolves a single member value', () => {
    const m = new CollisionSafeResultMap();
    m.set(K, CollisionSafeResultMap.rawSignature([['region', 'EU']]), 100);
    const out = m.finalize();
    expect(out.get(K)).toBe(100);
    expect(out.has(K)).toBe(true);
  });

  it('is idempotent for the SAME raw member seen twice (user-typed vs server form)', () => {
    // Same concrete member; the second write (e.g. server form of the same
    // member) must keep the value, not poison the key.
    const m = new CollisionSafeResultMap();
    const sig = CollisionSafeResultMap.rawSignature([['date', '2024-01-15T00:00:00']]);
    m.set('rev::date=2024-01-15', sig, 500);
    m.set('rev::date=2024-01-15', sig, 500);
    expect(m.finalize().get('rev::date=2024-01-15')).toBe(500);
  });

  it('POISONS a key when two DISTINCT raw members collapse to it (EU vs eu)', () => {
    // The exact adversarial exploit: "EU" (100) and "eu" (999) both returned by
    // the server normalize to region=eu. Neither value may be delivered as the
    // other's — the key is dropped so the fan-out yields #N/A (safe), never 999
    // for the EU cell.
    const m = new CollisionSafeResultMap();
    m.set(K, CollisionSafeResultMap.rawSignature([['region', 'EU']]), 100);
    m.set(K, CollisionSafeResultMap.rawSignature([['region', 'eu']]), 999);
    const out = m.finalize();
    expect(out.has(K)).toBe(false); // dropped -> consumer resolves null -> #N/A
  });

  it('POISONS on the numeric 5 vs 5.0 collision', () => {
    const m = new CollisionSafeResultMap();
    m.set('rev::code=5', CollisionSafeResultMap.rawSignature([['code', '5']]), 111);
    m.set('rev::code=5', CollisionSafeResultMap.rawSignature([['code', '5.0']]), 222);
    expect(m.finalize().has('rev::code=5')).toBe(false);
  });

  it('a poisoned key stays poisoned even if the first member is written again', () => {
    const m = new CollisionSafeResultMap();
    m.set(K, CollisionSafeResultMap.rawSignature([['region', 'EU']]), 100);
    m.set(K, CollisionSafeResultMap.rawSignature([['region', 'eu']]), 999); // poison
    m.set(K, CollisionSafeResultMap.rawSignature([['region', 'EU']]), 100); // ignored
    expect(m.finalize().has(K)).toBe(false);
  });

  it('does not poison independent, non-colliding keys', () => {
    const m = new CollisionSafeResultMap();
    m.set('cost::region=eu', CollisionSafeResultMap.rawSignature([['region', 'EU']]), 100);
    m.set('cost::region=us', CollisionSafeResultMap.rawSignature([['region', 'US']]), 200);
    const out = m.finalize();
    expect(out.get('cost::region=eu')).toBe(100);
    expect(out.get('cost::region=us')).toBe(200);
  });
});

describe('computeShapeKey', () => {
  it('groups invocations with the same model and filter columns', () => {
    const key1 = computeShapeKey('inventory', [['region', 'EU']]);
    const key2 = computeShapeKey('inventory', [['region', 'US']]);
    expect(key1).toBe(key2);
  });

  it('separates invocations with different filter columns', () => {
    const key1 = computeShapeKey('inventory', [['region', 'EU']]);
    const key2 = computeShapeKey('inventory', [['country', 'DE']]);
    expect(key1).not.toBe(key2);
  });

  it('separates invocations with different models', () => {
    const key1 = computeShapeKey('inventory', []);
    const key2 = computeShapeKey('sales', []);
    expect(key1).not.toBe(key2);
  });

  it('sorts filter columns for deterministic keys', () => {
    const key1 = computeShapeKey('m', [['b', 'x'], ['a', 'y']]);
    const key2 = computeShapeKey('m', [['a', 'y'], ['b', 'x']]);
    expect(key1).toBe(key2);
  });

  it('handles no filters', () => {
    const key = computeShapeKey('inventory', []);
    expect(key).toBe('inventory::');
  });
});

describe('computeResultKey', () => {
  it('includes the measure and normalizes the filter value (Bug-7394: case-folded)', () => {
    // Behaviour, not format: the key embeds the measure, and a case-variant of
    // the SAME member produces the SAME key (normalization), while the measure
    // prefix is present so different measures never collide.
    const euUpper = computeResultKey('shipping_cost', [['region', 'EU']]);
    const euLower = computeResultKey('shipping_cost', [['region', 'eu']]);
    expect(euUpper).toBe(euLower);
    expect(euUpper.startsWith('shipping_cost::')).toBe(true);
  });

  it('is unique per filter value', () => {
    const key1 = computeResultKey('cost', [['region', 'EU']]);
    const key2 = computeResultKey('cost', [['region', 'US']]);
    expect(key1).not.toBe(key2);
  });

  it('separates keys by measure for the same filters', () => {
    const k1 = computeResultKey('cost', [['region', 'EU']]);
    const k2 = computeResultKey('revenue', [['region', 'EU']]);
    expect(k1).not.toBe(k2);
  });

  it('produces a stable key for no filters', () => {
    const a = computeResultKey('revenue', []);
    const b = computeResultKey('revenue', []);
    expect(a).toBe(b);
    expect(a.startsWith('revenue::')).toBe(true);
  });

  // Bug-7394 (adversarial R2): delimiter injection must NOT let two distinct
  // member tuples forge the same key. Previously `col=value|...` was forgeable
  // by values containing `=`/`|`; the JSON encoding closes it.
  it('R2: distinct multi-column tuples with delimiter-bearing values get DISTINCT keys', () => {
    const a = computeResultKey('cost', [['a', 'p'], ['b', 'q|b=r']]);
    const b = computeResultKey('cost', [['a', 'p|b=q'], ['b', 'r']]);
    expect(a).not.toBe(b);
  });

  it('sorts multiple filter pairs', () => {
    const key1 = computeResultKey('cost', [['b', '2'], ['a', '1']]);
    const key2 = computeResultKey('cost', [['a', '1'], ['b', '2']]);
    expect(key1).toBe(key2);
  });

  it('Bug-7394: normalizes ISO date timestamps to bare dates', () => {
    const userKey = computeResultKey('revenue', [['date', '2024-01-15']]);
    const serverKey = computeResultKey('revenue', [['date', '2024-01-15T00:00:00']]);
    expect(userKey).toBe(serverKey);
  });

  it('Bug-7394: normalizes numeric float .0 to integer', () => {
    const userKey = computeResultKey('revenue', [['id', '5']]);
    const serverKey = computeResultKey('revenue', [['id', '5.0']]);
    expect(userKey).toBe(serverKey);
  });

  it('Bug-7394: case-folds member values', () => {
    const userKey = computeResultKey('revenue', [['region', 'EMEA']]);
    const serverKey = computeResultKey('revenue', [['region', 'emea']]);
    expect(userKey).toBe(serverKey);
  });
});

describe('FunctionBatcher', () => {
  let executor: ReturnType<typeof vi.fn<Parameters<BatchExecutor>, ReturnType<BatchExecutor>>>;
  let batcher: FunctionBatcher;

  beforeEach(() => {
    vi.useFakeTimers();
    // Default executor: for no-filter invocations, return 42 for every measure.
    executor = vi.fn<Parameters<BatchExecutor>, ReturnType<BatchExecutor>>().mockImplementation(
      async (_model, measures, singleValueFilters, _dimensionColumns, _filterValueSets) => {
        const results = new Map<string, number | string | null>();
        for (const measure of measures) {
          const key = computeResultKey(measure, singleValueFilters);
          results.set(key, 42);
        }
        return results;
      },
    );
    batcher = new FunctionBatcher(executor, 50);
  });

  it('coalesces invocations within the window', async () => {
    const p1 = batcher.enqueue('inv', 'cost', []);
    const p2 = batcher.enqueue('inv', 'revenue', []);
    expect(batcher.pendingCount).toBe(2);

    vi.advanceTimersByTime(60);
    await vi.runAllTimersAsync();

    const [r1, r2] = await Promise.all([p1, p2]);
    expect(r1).toBe(42);
    expect(r2).toBe(42);
    // Should have been called once (both measures in the same group).
    expect(executor).toHaveBeenCalledTimes(1);
  });

  it('separates groups by shape key', async () => {
    const p1 = batcher.enqueue('inv', 'cost', []);
    const p2 = batcher.enqueue('inv', 'cost', [['region', 'EU']]);

    vi.advanceTimersByTime(60);
    await vi.runAllTimersAsync();

    await Promise.all([p1, p2]);
    // Two different shape keys = two separate executor calls.
    expect(executor).toHaveBeenCalledTimes(2);
  });

  it('fans results out to individual promises', async () => {
    executor.mockImplementation(async (_model, measures, singleValueFilters) => {
      const results = new Map<string, number | string | null>();
      for (const measure of measures) {
        const key = computeResultKey(measure, singleValueFilters);
        results.set(key, measure === 'cost' ? 100 : 200);
      }
      return results;
    });

    const p1 = batcher.enqueue('inv', 'cost', []);
    const p2 = batcher.enqueue('inv', 'revenue', []);

    vi.advanceTimersByTime(60);
    await vi.runAllTimersAsync();

    expect(await p1).toBe(100);
    expect(await p2).toBe(200);
  });

  it('returns null for measures not in the result', async () => {
    executor.mockImplementation(async () => new Map());

    const p = batcher.enqueue('inv', 'missing', []);

    vi.advanceTimersByTime(60);
    await vi.runAllTimersAsync();

    expect(await p).toBeNull();
  });

  it('rejects all promises on executor error', async () => {
    executor.mockRejectedValue(new Error('Network failure'));

    const p1 = batcher.enqueue('inv', 'cost', []);
    const p2 = batcher.enqueue('inv', 'revenue', []);

    const r1 = p1.catch((e: Error) => e);
    const r2 = p2.catch((e: Error) => e);

    vi.advanceTimersByTime(60);
    await vi.runAllTimersAsync();

    const e1 = await r1;
    const e2 = await r2;
    expect(e1).toBeInstanceOf(Error);
    expect((e1 as Error).message).toBe('Network failure');
    expect(e2).toBeInstanceOf(Error);
    expect((e2 as Error).message).toBe('Network failure');
  });

  it('invalidate() rejects pending invocations', async () => {
    const p = batcher.enqueue('inv', 'cost', []);
    const caught = p.catch((e: Error) => e);

    batcher.invalidate();

    const result = await caught;
    expect(result).toBeInstanceOf(Error);
    expect((result as Error).message).toContain('Batcher invalidated');
    expect(executor).not.toHaveBeenCalled();
  });

  // -----------------------------------------------------------------------
  // Bug-6914: promises must SETTLE on every path. An unsettled promise is a
  // cell stuck at #GETTING_DATA forever — invalidate() cannot reach items
  // already spliced out of `pending`, so executeGroup must reject them itself.
  // -----------------------------------------------------------------------

  it('Bug-6914: invalidation DURING an in-flight batch rejects (never orphans) the promises', async () => {
    // Executor that invalidates the batcher mid-flight — mirrors the live
    // failure where the generation-token check ran inside the execute path.
    executor.mockImplementation(async (_model, measures, singleValueFilters) => {
      batcher.invalidate();
      const results = new Map<string, number | string | null>();
      for (const measure of measures) {
        results.set(computeResultKey(measure, singleValueFilters), 42);
      }
      return results;
    });

    const p1 = batcher.enqueue('inv', 'cost', []).catch((e: Error) => e);
    const p2 = batcher.enqueue('inv', 'revenue', []).catch((e: Error) => e);

    vi.advanceTimersByTime(60);
    await vi.runAllTimersAsync();

    // Both promises SETTLE (no #GETTING_DATA hang) — as rejections, since the
    // results were computed under the pre-invalidation context.
    const [r1, r2] = await Promise.all([p1, p2]);
    expect(r1).toBeInstanceOf(Error);
    expect((r1 as Error).message).toContain('Batcher invalidated');
    expect(r2).toBeInstanceOf(Error);
    expect((r2 as Error).message).toContain('Batcher invalidated');
  });

  it('Bug-6914: executor failure after mid-flight invalidation still rejects the promises', async () => {
    executor.mockImplementation(async () => {
      batcher.invalidate();
      throw new Error('backend exploded');
    });

    const p = batcher.enqueue('inv', 'cost', []).catch((e: Error) => e);

    vi.advanceTimersByTime(60);
    await vi.runAllTimersAsync();

    const result = await p;
    expect(result).toBeInstanceOf(Error);
    expect((result as Error).message).toContain('Batcher invalidated');
  });

  it('deduplicates measures within the same group', async () => {
    const p1 = batcher.enqueue('inv', 'cost', []);
    const p2 = batcher.enqueue('inv', 'cost', []);

    vi.advanceTimersByTime(60);
    await vi.runAllTimersAsync();

    const [r1, r2] = await Promise.all([p1, p2]);
    expect(r1).toBe(42);
    expect(r2).toBe(42);
    expect(executor).toHaveBeenCalledTimes(1);
  });

  // -----------------------------------------------------------------------
  // WRONG-NUMBERS GUARD: same measure, same column, different member values
  // -----------------------------------------------------------------------

  describe('multi-member fan-out (wrong-numbers guard)', () => {
    it('N cells x same measure x different members each get their own value', async () => {
      // Simulate TESSALLITE.MEMBERVALUE("inv","cost","region","EU"),
      //          TESSALLITE.MEMBERVALUE("inv","cost","region","US"),
      //          TESSALLITE.MEMBERVALUE("inv","cost","region","APAC")
      //
      // These share the same shape key (model=inv, filterColumns=[region])
      // but have DIFFERENT filter values. The batcher must issue a GROUP BY
      // query with region as a dimension and fan each cell its own row.
      executor.mockImplementation(
        async (_model, _measures, _singleFilters, dimensionColumns, filterValueSets) => {
          // Verify the executor receives dimensionColumns = ['region']
          // and filterValueSets with region -> {EU, US, APAC}.
          expect(dimensionColumns).toEqual(['region']);
          expect(filterValueSets.get('region')).toEqual(new Set(['EU', 'US', 'APAC']));

          // Simulate a multi-row response (one row per member).
          const results = new Map<string, number | string | null>();
          results.set(computeResultKey('cost', [['region', 'EU']]), 100);
          results.set(computeResultKey('cost', [['region', 'US']]), 200);
          results.set(computeResultKey('cost', [['region', 'APAC']]), 300);
          return results;
        },
      );

      const pEU   = batcher.enqueue('inv', 'cost', [['region', 'EU']]);
      const pUS   = batcher.enqueue('inv', 'cost', [['region', 'US']]);
      const pAPAC = batcher.enqueue('inv', 'cost', [['region', 'APAC']]);

      vi.advanceTimersByTime(60);
      await vi.runAllTimersAsync();

      // Each cell MUST get its own member's value — never another member's.
      expect(await pEU).toBe(100);
      expect(await pUS).toBe(200);
      expect(await pAPAC).toBe(300);

      // Only one executor call for all three (batched).
      expect(executor).toHaveBeenCalledTimes(1);
    });

    it('returns null for a member absent from the result', async () => {
      executor.mockImplementation(async () => {
        const results = new Map<string, number | string | null>();
        // Only EU and US are in the result; APAC is absent.
        results.set(computeResultKey('cost', [['region', 'EU']]), 100);
        results.set(computeResultKey('cost', [['region', 'US']]), 200);
        return results;
      });

      const pEU   = batcher.enqueue('inv', 'cost', [['region', 'EU']]);
      const pUS   = batcher.enqueue('inv', 'cost', [['region', 'US']]);
      const pAPAC = batcher.enqueue('inv', 'cost', [['region', 'APAC']]);

      vi.advanceTimersByTime(60);
      await vi.runAllTimersAsync();

      expect(await pEU).toBe(100);
      expect(await pUS).toBe(200);
      expect(await pAPAC).toBeNull(); // absent = null -> #N/A with reason
    });

    it('Bug-7394 adversarial R1: distinct members colliding on the normalized key fan out to #N/A, not a wrong number', async () => {
      // Reproduce the exact exploit end-to-end through the real batcher, using
      // an executor that builds its map exactly like the live producer
      // (CollisionSafeResultMap). Dirty source holds two distinct members "EU"
      // (100) and "eu" (999) that normalize to the same key. The EU cell must
      // NOT receive 999.
      executor.mockImplementation(async (_model, _measures, singleFilters, dimensionColumns) => {
        expect(dimensionColumns).toEqual(['region']);
        const rows = [
          { region: 'EU', cost: 100 },
          { region: 'eu', cost: 999 },
        ];
        const acc = new CollisionSafeResultMap();
        for (const row of rows) {
          const rowFilters: [string, string][] = [...singleFilters, ['region', String(row.region)]];
          const sig = CollisionSafeResultMap.rawSignature(rowFilters);
          acc.set(computeResultKey('cost', rowFilters), sig, row.cost);
        }
        return acc.finalize();
      });

      const pEU = batcher.enqueue('inv', 'cost', [['region', 'EU']]);
      const pLowerEU = batcher.enqueue('inv', 'cost', [['region', 'eu']]);

      vi.advanceTimersByTime(60);
      await vi.runAllTimersAsync();

      // Neither cell receives the other member's value; the ambiguous key is
      // dropped, so both resolve to null (#N/A) — the safe degradation. The
      // previous (buggy) behaviour delivered 999 to the EU cell.
      expect(await pEU).toBeNull();
      expect(await pLowerEU).toBeNull();
    });

    it('Bug-7394 adversarial R2: delimiter-bearing values cannot forge a shared key -> distinct members stay distinct', async () => {
      // The R2 exploit: two DISTINCT server rows whose member values embed the
      // old `=`/`|` delimiters previously forged an identical key/signature, so
      // one overwrote the other and a cell got the wrong number. With JSON
      // encoding the two tuples produce DISTINCT keys, so each cell gets its
      // own value (no overwrite, no wrong number).
      executor.mockImplementation(async (_model, _measures, singleFilters, dimensionColumns) => {
        expect(dimensionColumns).toEqual(['a', 'b']);
        const rows = [
          { a: 'p', b: 'q|b=r', cost: 100 },
          { a: 'p|b=q', b: 'r', cost: 999 },
        ];
        const acc = new CollisionSafeResultMap();
        for (const row of rows) {
          const rowFilters: [string, string][] = [
            ...singleFilters, ['a', String(row.a)], ['b', String(row.b)],
          ];
          acc.set(computeResultKey('cost', rowFilters), CollisionSafeResultMap.rawSignature(rowFilters), row.cost);
        }
        return acc.finalize();
      });

      const pA = batcher.enqueue('inv', 'cost', [['a', 'p'], ['b', 'q|b=r']]);
      const pB = batcher.enqueue('inv', 'cost', [['a', 'p|b=q'], ['b', 'r']]);

      vi.advanceTimersByTime(60);
      await vi.runAllTimersAsync();

      // Each member gets its OWN value — member A must NOT receive 999.
      expect(await pA).toBe(100);
      expect(await pB).toBe(999);
    });

    it('Bug-7394: user-typed vs server-serialized SAME member still resolves (normalization still bridges)', async () => {
      // The legitimate case normalization was added for: the user typed
      // "2024-01-15" but the server returns "2024-01-15T00:00:00" for the SAME
      // member. Only one server row exists, so there is no collision and the
      // value must still resolve (not #N/A).
      executor.mockImplementation(async (_model, _measures, singleFilters, dimensionColumns) => {
        expect(dimensionColumns).toEqual(['d']);
        const acc = new CollisionSafeResultMap();
        const rowFilters: [string, string][] = [...singleFilters, ['d', '2024-01-15T00:00:00']];
        acc.set(computeResultKey('rev', rowFilters), CollisionSafeResultMap.rawSignature(rowFilters), 777);
        // A second distinct member so 'd' is a GROUP BY dimension, not a single filter.
        const other: [string, string][] = [...singleFilters, ['d', '2024-02-20T00:00:00']];
        acc.set(computeResultKey('rev', other), CollisionSafeResultMap.rawSignature(other), 888);
        return acc.finalize();
      });

      const pJan = batcher.enqueue('inv', 'rev', [['d', '2024-01-15']]);
      const pFeb = batcher.enqueue('inv', 'rev', [['d', '2024-02-20']]);

      vi.advanceTimersByTime(60);
      await vi.runAllTimersAsync();

      expect(await pJan).toBe(777);
      expect(await pFeb).toBe(888);
    });

    it('handles mixed single-value and multi-value filter columns', async () => {
      // region has one value (single-value filter), year has multiple (dimension).
      executor.mockImplementation(async () => {
        const results = new Map<string, number | string | null>();
        results.set(computeResultKey('cost', [['region', 'EU'], ['year', '2024']]), 10);
        results.set(computeResultKey('cost', [['region', 'EU'], ['year', '2025']]), 20);
        return results;
      });

      const p2024 = batcher.enqueue('inv', 'cost', [['region', 'EU'], ['year', '2024']]);
      const p2025 = batcher.enqueue('inv', 'cost', [['region', 'EU'], ['year', '2025']]);

      vi.advanceTimersByTime(60);
      await vi.runAllTimersAsync();

      expect(await p2024).toBe(10);
      expect(await p2025).toBe(20);
      expect(executor).toHaveBeenCalledTimes(1);
    });

    it('same filter value = single-value filter, not dimension', async () => {
      // All invocations have region=EU — this should NOT become a dimension.
      executor.mockImplementation(
        async (_model, _measures, singleFilters, dimensionColumns) => {
          expect(dimensionColumns).toEqual([]);
          expect(singleFilters).toEqual([['region', 'EU']]);
          const results = new Map<string, number | string | null>();
          results.set(computeResultKey('cost', [['region', 'EU']]), 42);
          results.set(computeResultKey('revenue', [['region', 'EU']]), 99);
          return results;
        },
      );

      const p1 = batcher.enqueue('inv', 'cost', [['region', 'EU']]);
      const p2 = batcher.enqueue('inv', 'revenue', [['region', 'EU']]);

      vi.advanceTimersByTime(60);
      await vi.runAllTimersAsync();

      expect(await p1).toBe(42);
      expect(await p2).toBe(99);
      expect(executor).toHaveBeenCalledTimes(1);
    });
  });
});
