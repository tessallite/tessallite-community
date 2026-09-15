/**
 * Faithful Node stubs for the two host objects the custom-functions runtime
 * sees, plus a fetch recorder.
 *
 * The point of the harness is that NOTHING between the stub boundary and the
 * server is faked: the real built `functions.iife.js` runs, issues real HTTP
 * requests to a real Tessallite server, and the values it hands back are
 * asserted for their JavaScript TYPE, not only their content. That is the
 * check that would have caught Bug-9876 (`/plugin/execute` serialises measure
 * values as numeric strings; the add-in wrote text into number cells).
 *
 * Stub fidelity notes:
 *  - `CustomFunctions.Error` / `ErrorCode` are DEFINED here, matching desktop
 *    Excel. `makeFunctionError` therefore THROWS rather than returning the
 *    `#N/A `-prefixed fallback string, so the harness asserts the same error
 *    codes a real workbook cell renders.
 *  - `OfficeRuntime.storage` is async and string-only, like the real one.
 *  - Storage is per-process, matching one runtime instance.
 */

/** Mirrors the Office.js `CustomFunctions.ErrorCode` enum. */
export const ErrorCode = {
  divisionByZero: 'divisionByZero',
  invalidNumber: 'invalidNumber',
  invalidValue: 'invalidValue',
  notAvailable: 'notAvailable',
  nullReference: 'nullReference',
  invalidName: 'invalidName',
};

class CustomFunctionError extends Error {
  constructor(code, message) {
    super(message ?? code);
    this.name = 'CustomFunctionError';
    this.code = code;
  }
}

/** Async, string-only key/value store, like `OfficeRuntime.storage`. */
class StorageStub {
  #map = new Map();

  async getItem(key) {
    return this.#map.has(key) ? this.#map.get(key) : null;
  }

  async setItem(key, value) {
    if (typeof value !== 'string') {
      throw new TypeError(`OfficeRuntime.storage stores strings only; got ${typeof value}`);
    }
    this.#map.set(key, value);
  }

  async removeItem(key) {
    this.#map.delete(key);
  }

  snapshot() {
    return Object.fromEntries(this.#map);
  }
}

/**
 * Counts every request the bundle issues, so a batching claim can be asserted
 * as a REQUEST COUNT rather than inferred from timing.
 */
export class FetchRecorder {
  /**
   * `origin` scopes the recorder to the requests the ADD-IN issues. Without it
   * the origin shim's own upstream `fetch` calls are counted as well and every
   * request appears twice, which would silently turn a batching assertion into
   * a meaningless one.
   */
  constructor(origin) {
    this.calls = [];
    this.origin = origin;
    this.original = globalThis.fetch;
  }

  install() {
    const self = this;
    globalThis.fetch = async function recordedFetch(input, init) {
      const url = typeof input === 'string' ? input : input.url;
      const method = (init && init.method) || 'GET';
      const entry = { method, url, startedAt: Date.now() };
      if (!self.origin || url.startsWith(self.origin)) self.calls.push(entry);
      const res = await self.original.call(globalThis, input, init);
      entry.status = res.status;
      return res;
    };
    return this;
  }

  restore() {
    globalThis.fetch = this.original;
  }

  /** Marker for `since()`, so each check counts only its own requests. */
  mark() {
    return this.calls.length;
  }

  since(mark) {
    return this.calls.slice(mark);
  }

  countSince(mark, method, pathFragment) {
    return this.since(mark).filter(
      c => c.method === method && c.url.includes(pathFragment),
    ).length;
  }
}

/**
 * Install the host globals. Returns the registry the bundle associates its
 * functions into, plus the storage the pane would normally write.
 */
export function installOfficeStubs() {
  const registry = new Map();
  const storage = new StorageStub();

  globalThis.CustomFunctions = {
    associate(id, fn) {
      registry.set(id, fn);
    },
    Error: CustomFunctionError,
    ErrorCode,
  };

  globalThis.OfficeRuntime = { storage };

  return { registry, storage };
}

/**
 * A `CustomFunctions.StreamingInvocation` stub. `result` resolves with the
 * first `setResult` the function delivers.
 */
export function makeStreamingInvocation() {
  let settle;
  const promise = new Promise((resolve) => { settle = resolve; });
  let delivered = false;
  return {
    invocation: {
      onCanceled: null,
      setResult(value) {
        if (delivered) return;
        delivered = true;
        settle(value);
      },
    },
    result: promise,
  };
}
