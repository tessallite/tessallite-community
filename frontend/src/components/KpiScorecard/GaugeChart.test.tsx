import { describe, it, expect } from "vitest";
import { render } from "@testing-library/react";
import GaugeChart, { bandsToAxisLine, statusColor } from "./GaugeChart";
import { deriveScale } from "./chartUtils";
import type { KpiThresholdBand } from "../../api/types";

describe("GaugeChart", () => {
  it("renders an SVG", () => {
    const { container } = render(<GaugeChart value={50} />);
    expect(container.querySelector("svg")).toBeTruthy();
  });

  it("renders without crashing when value is null", () => {
    const { container } = render(<GaugeChart value={null} />);
    expect(container.querySelector("svg")).toBeTruthy();
  });

  it("renders with custom bands", () => {
    const bands: KpiThresholdBand[] = [
      { label: "Low", color: "#ff0000", min: 0, max: 50 },
      { label: "High", color: "#00ff00", min: 50, max: 100 },
    ];
    const { container } = render(<GaugeChart value={60} bands={bands} />);
    expect(container.querySelector("svg")).toBeTruthy();
  });

  it("renders with reverse mode", () => {
    const { container } = render(<GaugeChart value={30} reverse />);
    expect(container.querySelector("svg")).toBeTruthy();
  });

  it("renders with custom size", () => {
    const { container } = render(<GaugeChart value={50} size={200} />);
    expect(container.querySelector("svg")).toBeTruthy();
  });

  it("renders at boundary values", () => {
    const { container: c0 } = render(<GaugeChart value={0} />);
    expect(c0.querySelector("svg")).toBeTruthy();
    const { container: c100 } = render(<GaugeChart value={100} />);
    expect(c100.querySelector("svg")).toBeTruthy();
  });

  it("clamps negative and over-100 values without crashing", () => {
    const { container: cNeg } = render(<GaugeChart value={-10} />);
    expect(cNeg.querySelector("svg")).toBeTruthy();
    const { container: cOver } = render(<GaugeChart value={150} />);
    expect(cOver.querySelector("svg")).toBeTruthy();
  });

  it("renders for a high value in the worst band without crashing", () => {
    // Regression for the KYC gauge (value 0.84 in the red band). The progress
    // fill is disabled so it cannot overpaint the green/orange zones; full
    // visual confirmation is done live, here we just ensure it renders.
    const bands: KpiThresholdBand[] = [
      { label: "On Track", color: "#388E3C", min: 0, max: 0.3 },
      { label: "Near Target", color: "#F57C00", min: 0.3, max: 0.5 },
      { label: "Off Target", color: "#D32F2F", min: 0.5, max: 1 },
    ];
    const { container } = render(<GaugeChart value={0.84} bands={bands} />);
    expect(container.querySelector("svg")).toBeTruthy();
  });

  it("maps every RAG band to its own colour stop on the axis line", () => {
    // The axis line is what draws the visible bands. All three colours must be
    // present (in worst->best order) so no zone is hidden — this is the data
    // the gauge paints instead of a single-colour progress fill.
    const bands: KpiThresholdBand[] = [
      { label: "On Track", color: "#388E3C", min: 0, max: 0.3 },
      { label: "Near Target", color: "#F57C00", min: 0.3, max: 0.5 },
      { label: "Off Target", color: "#D32F2F", min: 0.5, max: 1 },
    ];
    const axisLine = bandsToAxisLine(bands, 0, 1);
    const colours = axisLine.map(([, c]) => c);
    expect(colours).toEqual(["#388E3C", "#F57C00", "#D32F2F"]);
    // Stops are normalised cumulative band maxima in [0,1].
    expect(axisLine.map(([stop]) => stop)).toEqual([0.3, 0.5, 1]);
  });

  it("colours a value meeting target green, agreeing with the badge (F-017-06)", () => {
    // Standard 3-band percent scale: Off <80, Near 80-100, On Track [100, ∞).
    const bands: KpiThresholdBand[] = [
      { label: "Off Target", color: "#D32F2F", min: null, max: 80 },
      { label: "Near Target", color: "#F57C00", min: 80, max: 100 },
      { label: "On Track", color: "#388E3C", min: 100, max: null },
    ];
    const { scaleMin, scaleMax } = deriveScale(bands);
    const axisLine = bandsToAxisLine(bands, scaleMin, scaleMax);
    const norm = (v: number) =>
      (Math.max(scaleMin, Math.min(scaleMax, v)) - scaleMin) /
      (scaleMax - scaleMin);

    // Exactly on target -> green (the flagship "beating goal" boundary), not
    // the amber the old <= boundary returned.
    expect(statusColor(norm(100), axisLine)).toBe("#388E3C");
    // Beating target -> green.
    expect(statusColor(norm(120), axisLine)).toBe("#388E3C");
    // Just under target -> amber.
    expect(statusColor(norm(95), axisLine)).toBe("#F57C00");
    // Far below -> red.
    expect(statusColor(norm(50), axisLine)).toBe("#D32F2F");
  });
});
