import { describe, it, expect } from "vitest";
import { render } from "@testing-library/react";
import Sparkline from "./Sparkline";
import type { KpiTrendPoint } from "../../api/types";

function pts(...values: (number | null)[]): KpiTrendPoint[] {
  return values.map((v, i) => ({ period: `2024-Q${i + 1}`, value: v }));
}

describe("Sparkline", () => {
  it("renders nothing when data is empty", () => {
    const { container } = render(<Sparkline data={[]} trend={null} />);
    expect(container.querySelector("svg")).toBeNull();
  });

  it("renders nothing with fewer than 2 points", () => {
    const { container } = render(<Sparkline data={pts(42)} trend={null} />);
    expect(container.querySelector("svg")).toBeNull();
  });

  it("renders an SVG for valid data", () => {
    const { container } = render(
      <Sparkline data={pts(10, 20, 30, 25)} trend={1} />,
    );
    expect(container.querySelector("svg")).toBeTruthy();
  });

  it("renders without crashing for declining trend", () => {
    const { container } = render(
      <Sparkline data={pts(30, 20, 10)} trend={-1} />,
    );
    expect(container.querySelector("svg")).toBeTruthy();
  });

  it("renders without crashing for flat trend", () => {
    const { container } = render(
      <Sparkline data={pts(10, 10, 10)} trend={0} />,
    );
    expect(container.querySelector("svg")).toBeTruthy();
  });

  it("handles null values in series", () => {
    const { container } = render(
      <Sparkline data={pts(10, 20, null, 30, 40)} trend={1} />,
    );
    expect(container.querySelector("svg")).toBeTruthy();
  });

  it("renders with custom dimensions", () => {
    const { container } = render(
      <Sparkline data={pts(5, 15)} trend={0} width={120} height={50} />,
    );
    expect(container.querySelector("svg")).toBeTruthy();
  });
});
