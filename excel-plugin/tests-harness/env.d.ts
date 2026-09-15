/**
 * The one Node global the harness specs use. Declared locally rather than
 * pulling in `@types/node`, which is not a dependency of this package and
 * would be a new one.
 */
declare const process: { env: Record<string, string | undefined> };
