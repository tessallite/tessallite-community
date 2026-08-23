import { describe, expect, it } from "vitest";
import en from "../i18n";
import { statusColor, ui } from "../theme/tokens";
import { runStatusLabel } from "./runStatus";

const t = (key: string) => (en as Record<string, string>)[key] ?? key;

describe("runStatusLabel (F-559-04)", () => {
  it("translates every status a run history can show, including queued", () => {
    // Bug-8034 made `queued` user-visible: it is what a manual advisor run
    // shows between acceptance and the dispatcher claiming it.
    expect(runStatusLabel("queued", t)).toBe("Queued");
    expect(runStatusLabel("running", t)).toBe("Running");
    expect(runStatusLabel("in_progress", t)).toBe("Running");
    expect(runStatusLabel("completed", t)).toBe("Completed");
    expect(runStatusLabel("failed", t)).toBe("Failed");
  });

  it("never renders a raw i18n key for any status it maps", () => {
    for (const status of ["queued", "running", "in_progress", "completed", "failed"]) {
      expect(runStatusLabel(status, t)).not.toMatch(/^runStatus\./);
    }
  });

  it("falls back to the raw value for a status the frontend does not know", () => {
    // Fail visible, not broken: a backend status added later shows as itself
    // rather than as `runStatus.whatever`.
    expect(runStatusLabel("some_new_backend_status", t)).toBe("some_new_backend_status");
    expect(runStatusLabel(null, t)).toBe("");
    expect(runStatusLabel(undefined, t)).toBe("");
  });

  it("keeps queued on the neutral colour token", () => {
    // The reviewer verified the neutral fall-through; this pins it so a later
    // edit to statusColor cannot quietly give `queued` a success/error colour.
    expect(statusColor("queued")).toEqual({ bg: ui.mutedBg, fg: ui.muted });
  });
});
