import { describe, it, expect } from "vitest";
import { render } from "@testing-library/react";
import RagBar from "./RagBar";
import type { KpiThresholdBand } from "../../api/types";

describe("RagBar", () => {
  it("renders an SVG", () => {
    const { container } = render(<RagBar value={50} />);
    expect(container.querySelector("svg")).toBeTruthy();
  });

  it("renders without crashing when value is null", () => {
    const { container } = render(<RagBar value={null} />);
    expect(container.querySelector("svg")).toBeTruthy();
  });

  it("renders with custom 2-band layout", () => {
    const bands: KpiThresholdBand[] = [
      { label: "Fail", color: "#d32f2f", min: 0, max: 50 },
      { label: "Pass", color: "#2e7d32", min: 50, max: 100 },
    ];
    const { container } = render(<RagBar value={75} bands={bands} />);
    expect(container.querySelector("svg")).toBeTruthy();
  });

  it("renders with custom 5-band layout", () => {
    const bands: KpiThresholdBand[] = [
      { label: "A", color: "#d32f2f", min: 0, max: 20 },
      { label: "B", color: "#ef6c00", min: 20, max: 40 },
      { label: "C", color: "#ed6c02", min: 40, max: 60 },
      { label: "D", color: "#66bb6a", min: 60, max: 80 },
      { label: "E", color: "#2e7d32", min: 80, max: 100 },
    ];
    const { container } = render(<RagBar value={55} bands={bands} />);
    expect(container.querySelector("svg")).toBeTruthy();
  });

  it("renders with custom dimensions", () => {
    const { container } = render(<RagBar value={50} width={300} height={24} />);
    expect(container.querySelector("svg")).toBeTruthy();
  });

  it("clamps values at boundaries", () => {
    const { container: cNeg } = render(<RagBar value={-10} />);
    expect(cNeg.querySelector("svg")).toBeTruthy();
    const { container: cOver } = render(<RagBar value={120} />);
    expect(cOver.querySelector("svg")).toBeTruthy();
  });
});
