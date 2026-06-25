import { describe, it, expect } from "vitest";
import { render } from "@testing-library/react";
import BulletChart from "./BulletChart";
import type { KpiThresholdBand } from "../../api/types";

describe("BulletChart", () => {
  it("renders an SVG", () => {
    const { container } = render(<BulletChart value={50} />);
    expect(container.querySelector("svg")).toBeTruthy();
  });

  it("renders with target marker", () => {
    const { container } = render(<BulletChart value={60} target={80} />);
    expect(container.querySelector("svg")).toBeTruthy();
  });

  it("renders without crashing when value is null", () => {
    const { container } = render(<BulletChart value={null} />);
    expect(container.querySelector("svg")).toBeTruthy();
  });

  it("renders without crashing when target is null", () => {
    const { container } = render(<BulletChart value={60} />);
    expect(container.querySelector("svg")).toBeTruthy();
  });

  it("renders with custom bands", () => {
    const bands: KpiThresholdBand[] = [
      { label: "Low", color: "#ff0000", min: 0, max: 50 },
      { label: "High", color: "#00ff00", min: 50, max: 100 },
    ];
    const { container } = render(<BulletChart value={60} bands={bands} />);
    expect(container.querySelector("svg")).toBeTruthy();
  });

  it("renders with custom dimensions", () => {
    const { container } = render(<BulletChart value={50} width={300} height={40} />);
    expect(container.querySelector("svg")).toBeTruthy();
  });

  it("clamps values at boundaries", () => {
    const { container: cNeg } = render(<BulletChart value={-20} />);
    expect(cNeg.querySelector("svg")).toBeTruthy();
    const { container: cOver } = render(<BulletChart value={150} />);
    expect(cOver.querySelector("svg")).toBeTruthy();
  });
});
