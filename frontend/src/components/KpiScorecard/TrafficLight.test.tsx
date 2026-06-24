import { describe, it, expect } from "vitest";
import { render, within } from "@testing-library/react";
import TrafficLight from "./TrafficLight";

describe("TrafficLight (Bug-5343)", () => {
  it("renders a labelled lamp housing for good status", () => {
    const { container } = render(<TrafficLight status={1} />);
    expect(within(container).getByRole("img")).toBeTruthy();
  });

  it("renders for warning status", () => {
    const { container } = render(<TrafficLight status={0} />);
    expect(within(container).getByRole("img")).toBeTruthy();
  });

  it("renders for poor status", () => {
    const { container } = render(<TrafficLight status={-1} />);
    expect(within(container).getByRole("img")).toBeTruthy();
  });

  it("renders when status is null (no data)", () => {
    const { container } = render(<TrafficLight status={null} />);
    expect(within(container).getByRole("img")).toBeTruthy();
  });

  it("renders three lamps", () => {
    const { container } = render(<TrafficLight status={1} />);
    // housing contains 3 lamp boxes
    expect(within(container).getByRole("img").children.length).toBe(3);
  });
});
