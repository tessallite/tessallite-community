/**
 * Pivot-config error contract (L18 — Bug-8161 / Bug-8182 / Bug-7442).
 *
 * Two guards:
 *  1. Producer/consumer alignment — the frontend ERROR_CODE_MAP must cover exactly
 *     the backend PivotConfigErrorCode token set, read here from the Python schema
 *     source so the finite domain can never silently drift.
 *  2. Bug-8182 recovery UX — the panel leads with the mapped friendly message and
 *     keeps raw backend/transport text collapsed (unmounted) behind the accordion.
 */
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { I18nContext } from "../../../i18n";
import en from "../../../i18n";
import {
  ERROR_CODE_MAP,
  PIVOT_ERROR_CODES,
  toPivotError,
} from "./pivotErrors";
import PivotErrorAlert from "./PivotErrorAlert";

const here = dirname(fileURLToPath(import.meta.url));
// .../frontend/src/components/Panels/MeasureQueryPanel → up 5 → tessallite/
const tessalliteRoot = join(here, "..", "..", "..", "..", "..");
const SCHEMA_PATH = join(
  tessalliteRoot,
  "shared",
  "schemas",
  "domains",
  "pivot_config.py",
);

function backendErrorCodes(): string[] {
  const source = readFileSync(SCHEMA_PATH, "utf8");
  // Match ``NAME = "NAME"`` members inside class PivotConfigErrorCode.
  const body = source.slice(source.indexOf("class PivotConfigErrorCode"));
  const end = body.indexOf("\nclass ");
  const scoped = end === -1 ? body : body.slice(0, end);
  const codes: string[] = [];
  const re = /^\s+([A-Z_]+)\s*=\s*"([A-Z_]+)"/gm;
  let m: RegExpExecArray | null;
  while ((m = re.exec(scoped)) !== null) {
    expect(m[1]).toBe(m[2]); // member name equals its string value
    codes.push(m[2]);
  }
  return codes;
}

const t = (key: string, vars?: Record<string, string | number>): string => {
  const flat = en as Record<string, string>;
  let s = flat[key] ?? key;
  if (vars) for (const [k, v] of Object.entries(vars)) s = s.replace(`{{${k}}}`, String(v));
  return s;
};

describe("pivot error-code contract", () => {
  it("ERROR_CODE_MAP covers exactly the backend PivotConfigErrorCode set", () => {
    const backend = backendErrorCodes().sort();
    expect(backend.length).toBeGreaterThan(0);
    expect([...PIVOT_ERROR_CODES].sort()).toEqual(backend);
    expect(Object.keys(ERROR_CODE_MAP).sort()).toEqual(backend);
  });

  it("every mapped i18n key resolves in the English bundle", () => {
    const flat = en as Record<string, string>;
    for (const key of Object.values(ERROR_CODE_MAP)) {
      expect(flat[key], `missing en key: ${key}`).toBeTruthy();
    }
    // The generic fallback used for an unrecognised typed code must also resolve.
    expect(flat["pivot.loadViewInvalidConfig"]).toBeTruthy();
    expect(flat["pivot.errorDetailsToggle"]).toBeTruthy();
  });
});

describe("toPivotError", () => {
  it("maps a typed 422 error_code to the friendly message and keeps the raw detail", () => {
    const err = {
      response: {
        data: {
          detail: {
            error_code: "INVALID_MEASURE_ID",
            message: "measure_id 'oops' is not a valid UUID.",
          },
        },
      },
    };
    const result = toPivotError(err, t, t("errors.requestFailed"));
    expect(result.errorCode).toBe("INVALID_MEASURE_ID");
    expect(result.message).toBe(t("pivot.errorInvalidMeasureId"));
    expect(result.detail).toBe("measure_id 'oops' is not a valid UUID.");
  });

  it("falls back to a generic friendly message with raw prose behind detail for an untyped error", () => {
    const err = { response: { data: { detail: "boom: internal traceback leaked" } } };
    const result = toPivotError(err, t, t("errors.requestFailed"));
    expect(result.errorCode).toBeNull();
    expect(result.message).toBe(t("errors.requestFailed"));
    expect(result.detail).toBe("boom: internal traceback leaked");
  });

  it("B3: a non-pivot typed code (query-router) is NOT labeled as a corrupt saved view", () => {
    // The query-router returns its own error_codes for query/persona/security
    // failures. These must fall through to the caller's generic friendly message
    // — never a pivot-config-invalid one — with the backend message kept behind
    // the accordion. Regression guard for the round-1 mapper that mapped every
    // error_code to pivot.loadViewInvalidConfig.
    for (const code of ["OBJECT_NOT_AVAILABLE", "PERSONA_OBJECT_NOT_INCLUDED", "COLUMN_RESTRICTED"]) {
      const err = {
        response: { data: { detail: { error_code: code, message: "objects not available" } } },
      };
      const result = toPivotError(err, t, t("drill.loadFailed"));
      expect(result.errorCode, `${code} must not be adopted`).toBeNull();
      expect(result.message).toBe(t("drill.loadFailed"));
      expect(result.message).not.toBe(t("pivot.loadViewInvalidConfig"));
      // The backend message is preserved for the collapsed accordion.
      expect(result.detail).toBe("objects not available");
    }
  });

  it("a pivot-config typed code IS mapped to its specific friendly message", () => {
    const err = {
      response: { data: { detail: { error_code: "INVALID_STRUCTURE", message: "bad shape" } } },
    };
    const result = toPivotError(err, t, t("errors.requestFailed"));
    expect(result.errorCode).toBe("INVALID_STRUCTURE");
    expect(result.message).toBe(t("pivot.errorInvalidStructure"));
  });
});

describe("PivotErrorAlert (Bug-8182 recovery UX)", () => {
  function renderAlert(detail: string | null) {
    return render(
      <I18nContext.Provider value={en}>
        <PivotErrorAlert error={{ message: "Friendly problem summary", detail }} />
      </I18nContext.Provider>,
    );
  }

  it("shows the friendly message and hides raw detail until the accordion is opened", () => {
    const raw = "raw backend traceback line 42";
    renderAlert(raw);
    // Friendly message is the primary surface.
    expect(screen.getByText("Friendly problem summary")).toBeInTheDocument();
    // Raw detail is not rendered while collapsed (unmountOnExit).
    expect(screen.queryByText(raw)).toBeNull();
    const toggle = screen.getByRole("button", { name: /technical details/i });
    expect(toggle).toHaveAttribute("aria-expanded", "false");
    // Opening it reveals the raw text.
    fireEvent.click(toggle);
    expect(screen.getByText(raw)).toBeInTheDocument();
  });

  it("renders no accordion when there is no raw detail", () => {
    renderAlert(null);
    expect(screen.getByText("Friendly problem summary")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /technical details/i })).toBeNull();
  });
});
