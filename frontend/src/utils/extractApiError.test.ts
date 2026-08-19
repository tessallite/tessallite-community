import { describe, it, expect } from "vitest";
import { extractApiError } from "./extractApiError";

/**
 * Contract tests for the ONE shared server-error unwrapper.
 *
 * These pin the three response shapes the backend actually emits. They exist
 * because consumers kept hand-rolling `err.response.data.detail as string`,
 * which renders NOTHING for two of the three shapes — the panel then falls back
 * to a fixed sentence and the server's real reason is thrown away
 * (Bug-8904 sibling: DimensionsPanel discarded the 422 rejection reason).
 *
 * Shape 3 in particular is the `_scope.py` body-FK family
 * (`ensure_ref_in_model` -> `{error_code, field, ids, message}`), which is what
 * a rejected `calendar_table_id` PATCH returns.
 */
describe("extractApiError", () => {
  const FALLBACK = "fallback-sentence";

  it("returns a plain string detail (HTTPException(detail='...'))", () => {
    const err = { response: { data: { detail: "Alias 'orders' is already in use." } } };
    expect(extractApiError(err, FALLBACK)).toBe("Alias 'orders' is already in use.");
  });

  it("joins FastAPI's list-shaped 422 validation detail", () => {
    const err = {
      response: {
        data: {
          detail: [
            { msg: "field required", loc: ["body", "display_name"] },
            { msg: "value is not a valid uuid", loc: ["body", "calendar_table_id"] },
          ],
        },
      },
    };
    expect(extractApiError(err, FALLBACK)).toBe(
      "field required; value is not a valid uuid",
    );
  });

  it("unwraps the body-FK guard's structured detail via detail.message", () => {
    // Exactly the shape ensure_calendar_table_in_model produces on a
    // cross-model calendar_table_id (error_code CALENDAR_TABLE_NOT_IN_MODEL).
    const err = {
      response: {
        data: {
          detail: {
            error_code: "CALENDAR_TABLE_NOT_IN_MODEL",
            field: "calendar_table_id",
            ids: ["3f0c1d2e-0000-4000-8000-000000000001"],
            message:
              "calendar_table_id does not reference a calendar table in this model.",
          },
        },
      },
    };
    expect(extractApiError(err, FALLBACK)).toBe(
      "calendar_table_id does not reference a calendar table in this model.",
    );
  });

  it("falls back to the caller's sentence when the failure carries no message", () => {
    expect(extractApiError({ message: "Network Error" }, FALLBACK)).toBe(FALLBACK);
    expect(extractApiError({}, FALLBACK)).toBe(FALLBACK);
    expect(extractApiError(undefined, FALLBACK)).toBe(FALLBACK);
  });
});
