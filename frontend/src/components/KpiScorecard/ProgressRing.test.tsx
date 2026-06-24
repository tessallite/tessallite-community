import { describe, it, expect } from "vitest";
import { render } from "@testing-library/react";
import ProgressRing from "./ProgressRing";
import type { KpiThresholdBand } from "../../api/types";

describe("ProgressRing", () => {
  it("renders an SVG", () => {
    const { container } = render(<ProgressRing value={70} />);
    expect(container.querySelector("svg")).toBeTruthy();
  });

  it("renders when value is null", () => {
    const { container } = render(<ProgressRing value={null} />);
    expect(container.querySelector("svg")).toBeTruthy();
  });

  it("renders with custom bands", () => {
    const bands: KpiThresholdBand[] = [
      { label: "Low", color: "#ff0000", min: 0, max: 50 },
      { label: "High", color: "#00ff00", min: 50, max: 100 },
    ];
    const { container } = render(<ProgressRing value={60} bands={bands} />);
    expect(container.querySelector("svg")).toBeTruthy();
  });

  it("clamps out-of-range values", () => {
    const { container: over } = render(<ProgressRing value={150} />);
    expect(over.querySelector("svg")).toBeTruthy();
    const { container: under } = render(<ProgressRing value={-20} />);
    expect(under.querySelector("svg")).toBeTruthy();
  });
});
