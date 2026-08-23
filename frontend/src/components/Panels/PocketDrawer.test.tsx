/**
 * Bug-8162 — the pocket drawer must tell "your SQL is wrong" apart from
 * "we could not check your SQL".
 *
 * The backend now answers 503 when the query-router cannot be reached, instead
 * of collapsing an outage into the same 400/422/`ok=false` a real rejection
 * uses. That distinction is only useful if the UI preserves it: rendering the
 * server's diagnostic prose for a 503 would tell a modeller with correct SQL
 * that their SQL failed validation.
 */
import { describe, it, expect } from "vitest";
import en from "../../i18n";
import { extractErrorAndViolations } from "./PocketDrawer";

const t = (key: string): string => (en as Record<string, string>)[key] ?? key;

describe("PocketDrawer error mapping (Bug-8162)", () => {
  it("reports a 503 as 'could not be checked, retry', never as a verdict", () => {
    const { message, violations } = extractErrorAndViolations(
      {
        response: {
          status: 503,
          data: {
            detail:
              "validator_unavailable: the query validator could not be reached ...",
          },
        },
      },
      t,
    );
    expect(message).toBe(t("errors.validatorUnavailable"));
    expect(message).toContain("could not be reached");
    expect(message).toContain("has not been rejected");
    // The raw server diagnostic must not reach the user.
    expect(message).not.toContain("validator_unavailable:");
    expect(violations).toBeNull();
  });

  it("still reports a real rejection as a verdict on the SQL", () => {
    // The other half: an implementation that answered "retry" to every failure
    // would pass the test above and fail this one.
    const { message } = extractErrorAndViolations(
      {
        response: {
          status: 400,
          data: { detail: "column no_such_col does not exist" },
        },
      },
      t,
    );
    expect(message).toBe("column no_such_col does not exist");
    expect(message).not.toContain("could not be reached");
  });

  it("still surfaces structured subset violations unchanged", () => {
    const { message, violations } = extractErrorAndViolations(
      {
        response: {
          status: 400,
          data: {
            detail: {
              message: "Pocket SQL is not a valid model subset.",
              violations: [{ code: "not_select_star", message: "must SELECT *" }],
            },
          },
        },
      },
      t,
    );
    expect(message).toBe("Pocket SQL is not a valid model subset.");
    expect(violations).toHaveLength(1);
  });
});
