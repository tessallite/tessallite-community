import { describe, expect, it } from "vitest";
import { localInputToUtcIso } from "./AuditLog";

describe("AuditLog date filters", () => {
  it("converts a local midnight-boundary filter into the matching UTC instant", () => {
    const offsetMinutes = new Date(2026, 0, 1, 12, 0).getTimezoneOffset();
    const localInput =
      offsetMinutes <= 0 ? "2026-01-01T00:05" : "2026-01-01T23:55";
    const [date, time] = localInput.split("T");
    const [year, month, day] = date.split("-").map(Number);
    const [hour, minute] = time.split(":").map(Number);

    const actual = localInputToUtcIso(localInput);
    expect(actual).toBe(
      new Date(year, month - 1, day, hour, minute).toISOString(),
    );
    if (offsetMinutes !== 0) {
      expect(actual?.slice(0, 10)).not.toBe(date);
    }
  });

  it("omits empty and invalid date filters instead of sending malformed values", () => {
    expect(localInputToUtcIso("")).toBeUndefined();
    expect(localInputToUtcIso("not-a-date")).toBeUndefined();
  });
});
