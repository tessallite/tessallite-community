/**
 * Bug-9910 — MACHINE-CHECK that every message the custom-functions runtime can
 * put in a cell actually reaches the cell.
 *
 * The defect this guards is not a bug in any one message. It is that
 * `safeErrorMessage()` decided which messages were safe by matching text
 * against a hand-maintained prefix list, so adding a message and forgetting the
 * list produced a cell reading "An error occurred. Check the Tessallite panel
 * for details." and a panel that said nothing more. That happened five times:
 * Bug-8712, Bug-9749, Bug-9759, Bug-9880, Bug-9910. Every one was found by a
 * user or a harness, live, never by a test — because the tests asserted that
 * the runtime THREW, never what a cell ENDS UP SHOWING.
 *
 * So this walks the real TypeScript AST of the runtime's own modules and
 * classifies every `new Error(...)` in them:
 *
 *   - built from `CELL_MESSAGES.<key>`  -> correct, and the text is checked
 *     against the cell gate;
 *   - built from an inline string       -> a message outside the table, which
 *     is the shape every one of the five defects had;
 *   - built from anything else          -> must be a sanctioned pass-through,
 *     named here with the reason.
 *
 * A guard that only scanned for inline strings would report green the moment
 * the code stopped using them, so the resolution is explicit rather than
 * best-effort.
 */
import { existsSync, readFileSync } from 'node:fs';
import { dirname, relative, resolve } from 'node:path';
import ts from 'typescript';
import { describe, expect, it } from 'vitest';

import {
  CELL_MESSAGES,
  GENERIC_CELL_ERROR,
  SERVER_AUTHORED_DETAIL_PREFIXES,
  isSafeCellMessage,
} from '../utils/cellMessages';

/**
 * Every module that ends up in the custom-functions bundle.
 *
 * DERIVED, not listed. A hand-written list is the same failure the guard is
 * about: the day a new module in the bundle throws to a cell, a list would keep
 * reporting green while the message it throws is swallowed. This walks the
 * transitive closure of relative imports from the runtime's own entry point
 * (`functions.ts`, the file `vite.config.functions.ts` builds), which is exactly
 * the set of first-party code that can reach a cell.
 */
function runtimeModules(): string[] {
  const entry = resolve(__dirname, '../functions.ts');
  const seen = new Set<string>();
  const queue = [entry];
  while (queue.length) {
    const file = queue.pop()!;
    if (seen.has(file)) continue;
    seen.add(file);
    const source = ts.createSourceFile(
      file, readFileSync(file, 'utf8'), ts.ScriptTarget.ES2020, true,
    );
    for (const stmt of source.statements) {
      if (!ts.isImportDeclaration(stmt)) continue;
      if (!ts.isStringLiteral(stmt.moduleSpecifier)) continue;
      const spec = stmt.moduleSpecifier.text;
      if (!spec.startsWith('.')) continue;          // a package, not our code
      const base = resolve(dirname(file), spec);
      const candidate = [`${base}.ts`, `${base}/index.ts`].find(existsSync);
      // Fail closed: a first-party import this resolver cannot find would
      // silently shrink the scanned set.
      expect(candidate, `cannot resolve ${spec} from ${file}`).toBeTruthy();
      queue.push(candidate!);
    }
  }
  return [...seen];
}

const RUNTIME_MODULES = runtimeModules();

/**
 * `new Error(<expr>)` sites whose argument is neither a table entry nor an
 * inline string, keyed by file and by the exact source text of the argument.
 * Each needs a reason, because an unexplained entry here is how a guard turns
 * into a rubber stamp.
 */
const SANCTIONED_DYNAMIC_ARGS: Record<string, string> = {
  // The 422 pass-through: the query-router authored this text for a
  // user-caused condition. `safeErrorMessage` still gates it against
  // SERVER_AUTHORED_DETAIL_PREFIXES before it can reach a cell.
  'functions.ts::detail': 'server-authored 422 detail, gated on display',
  // An INTERNAL marker, never a cell message. `withTimeout` raises it and
  // `withRequestTimeout` is the only caller that lets it escape a try/catch —
  // and it converts the marker into CELL_MESSAGES.requestTimedOut. The
  // remaining callers (the 422 body read, and TESSALLITE.DIAG's storage and
  // health probes) each swallow it into their own diagnostic text.
  'functions.ts::`timeout_${ms}ms`': 'internal timeout marker, converted before display',
};

type Kind = 'table' | 'inline' | 'dynamic';

interface FoundMessage {
  file: string;
  line: number;
  kind: Kind;
  /** Resolved message text for 'table'/'inline'; the source text for 'dynamic'. */
  text: string;
}

/** The static string value of `expr`, or null when it is not statically known. */
function staticString(expr: ts.Expression): string | null {
  if (ts.isStringLiteral(expr)) return expr.text;
  if (ts.isNoSubstitutionTemplateLiteral(expr)) return expr.text;
  if (ts.isParenthesizedExpression(expr)) return staticString(expr.expression);
  if (
    ts.isBinaryExpression(expr)
    && expr.operatorToken.kind === ts.SyntaxKind.PlusToken
  ) {
    const left = staticString(expr.left);
    const right = staticString(expr.right);
    return left === null || right === null ? null : left + right;
  }
  return null;
}

/** The CELL_MESSAGES key `expr` reads, or null when it reads something else. */
function tableKey(expr: ts.Expression): string | null {
  if (
    ts.isPropertyAccessExpression(expr)
    && ts.isIdentifier(expr.expression)
    && expr.expression.text === 'CELL_MESSAGES'
  ) {
    return expr.name.text;
  }
  return null;
}

function errorConstructionsIn(file: string): FoundMessage[] {
  const relPath = relative(resolve(__dirname, '..'), file).replace(/\\/g, '/');
  const source = ts.createSourceFile(
    file, readFileSync(file, 'utf8'), ts.ScriptTarget.ES2020, true,
  );
  const found: FoundMessage[] = [];
  const visit = (node: ts.Node): void => {
    if (
      ts.isNewExpression(node)
      && ts.isIdentifier(node.expression)
      && node.expression.text === 'Error'
      && node.arguments?.length
    ) {
      const arg = node.arguments[0];
      const line = source.getLineAndCharacterOfPosition(node.getStart()).line + 1;
      const key = tableKey(arg);
      if (key !== null) {
        const text = (CELL_MESSAGES as Record<string, string>)[key];
        expect(text, `${relPath}:${line} CELL_MESSAGES.${key} does not exist`)
          .toBeTypeOf('string');
        found.push({ file: relPath, line, kind: 'table', text });
      } else {
        const literal = staticString(arg);
        found.push(literal !== null
          ? { file: relPath, line, kind: 'inline', text: literal }
          : { file: relPath, line, kind: 'dynamic', text: arg.getText(source) });
      }
    }
    ts.forEachChild(node, visit);
  };
  visit(source);
  return found;
}

const ALL = RUNTIME_MODULES.flatMap(errorConstructionsIn);

describe('cell message contract (Bug-9910)', () => {
  it('scans the whole bundle, and finds its error constructions', () => {
    // A guard that silently matched nothing, or scanned one file, would report
    // green forever.
    const scanned = RUNTIME_MODULES.map(
      f => relative(resolve(__dirname, '..'), f).replace(/\\/g, '/'),
    );
    expect(scanned).toContain('functions.ts');
    expect(scanned).toContain('utils/functionBatcher.ts');
    expect(scanned).toContain('utils/cellMessages.ts');
    expect(scanned.length).toBeGreaterThanOrEqual(5);
    expect(ALL.length).toBeGreaterThan(10);
    expect(ALL.some(m => m.kind === 'table')).toBe(true);
  });

  it('constructs every message from the table, never inline', () => {
    const inline = ALL.filter(m => m.kind === 'inline')
      .map(m => `${m.file}:${m.line} ${JSON.stringify(m.text)}`);
    expect(inline).toEqual([]);
  });

  it('every table message the runtime throws survives the cell gate', () => {
    const swallowed = ALL
      .filter(m => m.kind === 'table' && !isSafeCellMessage(m.text))
      .map(m => `${m.file}:${m.line} ${JSON.stringify(m.text)}`);
    expect(swallowed).toEqual([]);
  });

  it('every dynamic message argument is sanctioned with a reason', () => {
    const unsanctioned = ALL
      .filter(m => m.kind === 'dynamic'
        && !(`${m.file}::${m.text}` in SANCTIONED_DYNAMIC_ARGS))
      .map(m => `${m.file}:${m.line} new Error(${m.text})`);
    expect(unsanctioned).toEqual([]);
  });

  it('carries no table message the runtime never constructs', () => {
    // The old allow-list still named 'Unknown measure' and 'Unknown KPI', which
    // nothing throws: those reach the cell through makeFunctionError, which
    // never passes through this gate. A table documenting messages the code does
    // not produce is the drift this guard exists to stop.
    const constructed = new Set(ALL.filter(m => m.kind === 'table').map(m => m.text));
    const unused = Object.entries(CELL_MESSAGES)
      .filter(([, text]) => !constructed.has(text))
      .map(([key]) => key);
    expect(unused).toEqual([]);
  });

  it('the generic fallback is not itself a showable message', () => {
    // If it were, an unrecognised backend string equal to it would be
    // indistinguishable from a message the add-in chose to show.
    expect(isSafeCellMessage(GENERIC_CELL_ERROR)).toBe(false);
  });

  it('withholds an unrecognised backend string', () => {
    expect(isSafeCellMessage(
      "Cannot resolve measure 'x' to a physical column in table public.fact_123",
    )).toBe(false);
    expect(isSafeCellMessage('psql: FATAL: password authentication failed')).toBe(false);
  });

  it('passes the documented server-authored details through', () => {
    for (const prefix of SERVER_AUTHORED_DETAIL_PREFIXES) {
      expect(isSafeCellMessage(prefix)).toBe(true);
      expect(isSafeCellMessage(`${prefix} — with the caller's own identifiers`)).toBe(true);
    }
  });
});
