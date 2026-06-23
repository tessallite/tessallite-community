import { describe, it, expect } from "vitest";
import { render } from "@testing-library/react";
import Thermometer from "./Thermometer";
import type { KpiThresholdBand } from "../../api/types";

describe("Thermometer", () => {
  it("renders an SVG", () => {
    const { container } = render(<Thermometer value={70} />);
    expect(container.querySelector("svg")).toBeTruthy();
  });

  it("renders when value is null", () => {
    const { container } = render(<Thermometer value={null} />);
    expect(container.querySelector("svg")).toBeTruthy();
  });

  it("renders with custom bands", () => {
    const bands: KpiThresholdBand[] = [
      { label: "Low", color: "#ff0000", min: 0, max: 50 },
      { label: "High", color: "#00ff00", min: 50, max: 100 },
    ];
    const { container } = render(<Thermometer value={60} bands={bands} />);
    expect(container.querySelector("svg")).toBeTruthy();
  });

  it("clamps out-of-range values", () => {
    const { container: over } = render(<Thermometer value={150} />);
    expect(over.querySelector("svg")).toBeTruthy();
    const { container: under } = render(<Thermometer value={-20} />);
    expect(under.querySelector("svg")).toBeTruthy();
  });
});
