import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { describe, expect, it, vi } from "vitest";
import RebuildMethodPicker from "./RebuildMethodPicker";

describe("RebuildMethodPicker append-only contract", () => {
  it("shows the contract and edits the periodic full-rebuild cadence", async () => {
    const onInterval = vi.fn();
    function ControlledPicker() {
      const [interval, setInterval] = useState<number | null>(30);
      return (
        <RebuildMethodPicker
          method="incremental"
          onMethodChange={vi.fn()}
          incrementalColumn="business_date"
          onIncrementalColumnChange={vi.fn()}
          lookbackDays={7}
          onLookbackChange={vi.fn()}
          fullRebuildIntervalDays={interval}
          onFullRebuildIntervalChange={(days) => {
            onInterval(days);
            setInterval(days);
          }}
        />
      );
    }
    render(<ControlledPicker />);

    expect(screen.getByTestId("append-only-contract-notice")).toBeVisible();
    const interval = screen.getByLabelText("Full rebuild every N days");
    await userEvent.clear(interval);
    expect(onInterval).toHaveBeenCalledWith(null);
    await userEvent.type(interval, "14");
    expect(onInterval).toHaveBeenLastCalledWith(14);
  });

  it("does not show incremental controls for a full rebuild", () => {
    render(<RebuildMethodPicker method="full" onMethodChange={vi.fn()} />);
    expect(screen.queryByTestId("append-only-contract-notice")).not.toBeInTheDocument();
  });
});
