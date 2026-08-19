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

  it("Bug-7820: renders without crashing for z_score bands with negative scaleMin", () => {
    const zScoreBands: KpiThresholdBand[] = [
      { label: "Far", color: "#D32F2F", min: null, max: -0.5 },
      { label: "Near", color: "#F57C00", min: -0.5, max: 0.5 },
      { label: "On Target", color: "#388E3C", min: 0.5, max: null },
    ];
    const { container } = render(
      <BulletChart value={0.2} target={0} bands={zScoreBands} />,
    );
    expect(container.querySelector("svg")).toBeTruthy();
  });

  it("Bug-7820: renders z_score_closer bands with entirely negative scaleMin/Max", () => {
    const closerBands: KpiThresholdBand[] = [
      { label: "Far", color: "#D32F2F", min: -1.25, max: -0.75 },
      { label: "Near", color: "#F57C00", min: -0.75, max: -0.3125 },
    ];
    const { container } = render(
      <BulletChart value={-0.5} bands={closerBands} />,
    );
    expect(container.querySelector("svg")).toBeTruthy();
  });
});
