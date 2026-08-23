import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import RefreshRunHistory from "./RefreshRunHistory";

describe("RefreshRunHistory applied mode", () => {
  it("shows a full fallback instead of hiding it behind the trigger", () => {
    render(
      <RefreshRunHistory
        runs={[
          {
            id: "bug-8740-run",
            status: "completed",
            started_at: "2026-08-22T12:00:00Z",
            completed_at: "2026-08-22T12:01:00Z",
            rows_written: 42,
            refresh_mode: "full",
            triggered_by: "scheduled",
          },
        ]}
      />,
    );

    expect(screen.getByTestId("refresh-mode-bug-8740-run")).toHaveTextContent(
      "Full",
    );
    expect(screen.getByText("Scheduled")).toBeVisible();
  });
});
