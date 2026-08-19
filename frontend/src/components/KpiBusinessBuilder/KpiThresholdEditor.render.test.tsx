import { describe, it, expect, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { KpiThresholdEditor, createDefaultPresentationMeta } from "./KpiThresholdEditor";
import type { KpiPresentationMeta } from "../../api/types_domains/kpis";

describe("KpiThresholdEditor (render)", () => {
  it("basis switch to Value scale with target=200 emits target-scaled bands", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    // Start with percentage_of_target (ratio-scale bands) to test switching TO value scale
    const meta: KpiPresentationMeta = {
      evaluation_type: "percentage_of_target",
      bands: [
        { label: "Off Target", color: "#D32F2F", min: null, max: 0.80 },
        { label: "Near Target", color: "#F57C00", min: 0.80, max: 1.00 },
        { label: "On Track", color: "#388E3C", min: 1.00, max: null },
      ],
    };

    render(
      <KpiThresholdEditor
        meta={meta}
        direction="higher_is_better"
        target={200}
        onChange={onChange}
      />,
    );

    const basisSelect = screen.getByRole("combobox", { name: /Gauge basis/i });
    await user.click(basisSelect);
    const valueOption = await screen.findByRole("option", { name: /Value scale/i });
    await user.click(valueOption);

    expect(onChange).toHaveBeenCalled();
    const emitted: KpiPresentationMeta = onChange.mock.calls[0][0];
    expect(emitted.evaluation_type).toBe("absolute_value");
    // Absolute bands scaled by target=200: [null, 160], [160, 200], [200, null]
    expect(emitted.bands![0].max).toBe(160);
    expect(emitted.bands![1].max).toBe(200);
    expect(emitted.bands![2].max).toBeNull();
  });

  it("band edit triggers onBandsCustomized callback", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    const onBandsCustomized = vi.fn();
    const meta = createDefaultPresentationMeta(100, "higher_is_better");

    render(
      <KpiThresholdEditor
        meta={meta}
        direction="higher_is_better"
        target={100}
        onChange={onChange}
        onBandsCustomized={onBandsCustomized}
      />,
    );

    const labelInputs = screen.getAllByRole("textbox");
    const firstLabel = labelInputs[0];
    await user.clear(firstLabel);
    await user.type(firstLabel, "Bad");

    expect(onBandsCustomized).toHaveBeenCalled();
  });

  it("renders without target prop (fallback to 100 scale)", () => {
    const onChange = vi.fn();
    const meta = createDefaultPresentationMeta(null, "higher_is_better");

    render(
      <KpiThresholdEditor
        meta={meta}
        direction="higher_is_better"
        onChange={onChange}
      />,
    );

    expect(screen.getByText("Threshold bands")).toBeInTheDocument();
    expect(screen.getByRole("combobox", { name: /Gauge basis/i })).toBeInTheDocument();
  });

  it("basis switch to Percent of target on higher_is_better emits ratio-scale bands (R3-H001)", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    // Start with absolute_value to test switching TO percentage (F-017-20 made
    // percentage_of_target the default, so absolute_value must be forced here).
    const meta = createDefaultPresentationMeta(200, "higher_is_better", "absolute_value");
    expect(meta.evaluation_type).toBe("absolute_value");

    render(
      <KpiThresholdEditor
        meta={meta}
        direction="higher_is_better"
        target={200}
        onChange={onChange}
      />,
    );

    const basisSelect = screen.getByRole("combobox", { name: /Gauge basis/i });
    await user.click(basisSelect);
    const pctOption = await screen.findByRole("option", { name: /Percent of target/i });
    await user.click(pctOption);

    expect(onChange).toHaveBeenCalled();
    const emitted: KpiPresentationMeta = onChange.mock.calls[0][0];
    expect(emitted.evaluation_type).toBe("percentage_of_target");
    // Must be ratio-scale (0-1), NOT 0-100 or target-scaled
    expect(emitted.bands![0].max).toBe(0.80);
    expect(emitted.bands![1].max).toBe(1.00);
    expect(emitted.bands![2].max).toBeNull();
  });

  it("basis switch to Percent of target on lower_is_better emits On Track at high end (R3-M001)", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    // Start on absolute_value so switching TO percentage_of_target actually
    // fires (percentage_of_target is now the default basis — F-017-20).
    const meta = createDefaultPresentationMeta(100, "lower_is_better", "absolute_value");

    render(
      <KpiThresholdEditor
        meta={meta}
        direction="lower_is_better"
        target={100}
        onChange={onChange}
      />,
    );

    const basisSelect = screen.getByRole("combobox", { name: /Gauge basis/i });
    await user.click(basisSelect);
    const pctOption = await screen.findByRole("option", { name: /Percent of target/i });
    await user.click(pctOption);

    expect(onChange).toHaveBeenCalled();
    const emitted: KpiPresentationMeta = onChange.mock.calls[0][0];
    expect(emitted.evaluation_type).toBe("percentage_of_target");
    // Backend inverts ratio for lower_is_better (target/value), so high = good.
    // Bands must have On Track at high end.
    expect(emitted.bands![0].label).toBe("Off Target");
    expect(emitted.bands![2].label).toBe("On Track");
    expect(emitted.bands![0].max).toBe(0.80);
    expect(emitted.bands![2].min).toBe(1.00);
  });

  it("percentage_of_target bands display as 0-100 percentages in UI regardless of target", () => {
    const onChange = vi.fn();
    // Stored in ratio scale (0-1); UI shows × 100
    const meta: KpiPresentationMeta = {
      evaluation_type: "percentage_of_target",
      bands: [
        { label: "Off Target", color: "#D32F2F", min: null, max: 0.80 },
        { label: "Near Target", color: "#F57C00", min: 0.80, max: 1.00 },
        { label: "On Track", color: "#388E3C", min: 1.00, max: null },
      ],
    };

    render(
      <KpiThresholdEditor
        meta={meta}
        direction="higher_is_better"
        target={500}
        onChange={onChange}
      />,
    );

    const basisSelect = screen.getByRole("combobox", { name: /Gauge basis/i });
    expect(basisSelect).toHaveTextContent("Percent of target");

    // UI displays values × 100 (so 0.80 -> 80, 1.00 -> 100)
    const numberInputs = screen.getAllByRole("spinbutton");
    const bandValues = numberInputs.map((el) => Number((el as HTMLInputElement).value));
    expect(bandValues.every((v) => v <= 100)).toBe(true);
  });

  it("closer_is_better with stored absolute_value coerces to Percent of target on load (R5-M001)", () => {
    const onChange = vi.fn();
    // Simulate an existing closer KPI stored with absolute_value (pre-fix data)
    const staleAbsoluteMeta: KpiPresentationMeta = {
      evaluation_type: "absolute_value",
      bands: [
        { label: "On Track", color: "#388E3C", min: null, max: 80 },
        { label: "Near Target", color: "#F57C00", min: 80, max: 90 },
        { label: "Off Target", color: "#D32F2F", min: 90, max: null },
      ],
    };

    render(
      <KpiThresholdEditor
        meta={staleAbsoluteMeta}
        direction="closer_is_better"
        target={100}
        onChange={onChange}
      />,
    );

    // Coercion should set the basis to Percent of target, not leave a blank/broken control
    const basisSelect = screen.getByRole("combobox", { name: /Gauge basis/i });
    expect(basisSelect).toHaveTextContent("Percent of target");
  });

  it("closer_is_better does not offer Value scale option (R4-M001)", () => {
    const onChange = vi.fn();
    const meta = createDefaultPresentationMeta(100, "closer_is_better");

    render(
      <KpiThresholdEditor
        meta={meta}
        direction="closer_is_better"
        target={100}
        onChange={onChange}
      />,
    );

    const basisSelect = screen.getByRole("combobox", { name: /Gauge basis/i });
    expect(basisSelect).toHaveTextContent("Percent of target");
    // The absolute_value option should not be present for closer_is_better
    expect(screen.queryByText("Value scale")).not.toBeInTheDocument();
  });
});
