/**
 * Bug-6248: the wizard threshold step must preserve (or let the user choose)
 * the evaluation_type rather than silently forcing absolute_value on every
 * meta update. These tests verify the saved presentation_meta carries the
 * correct evaluation_type after user interactions.
 */
import { describe, it, expect, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import KpiWizardStep3Thresholds from "./KpiWizardStep3Thresholds";
import { EMPTY_FORM } from "./types";
import type { KpiWizardFormState } from "./types";

function renderStep(
  formOverrides: Partial<KpiWizardFormState> = {},
  onChange?: (patch: Partial<KpiWizardFormState>) => void,
) {
  const form: KpiWizardFormState = { ...EMPTY_FORM, ...formOverrides };
  const onChangeFn = onChange ?? vi.fn();
  const utils = render(
    <KpiWizardStep3Thresholds form={form} onChange={onChangeFn} />,
  );
  return { ...utils, onChange: onChangeFn };
}

describe("KpiWizardStep3Thresholds evaluation_type", () => {
  it("preserves an existing percentage_of_target evaluation_type when toggling colorblind mode", async () => {
    const onChange = vi.fn();
    renderStep(
      {
        direction: "higher_is_better",
        presentation_meta: {
          evaluation_type: "percentage_of_target",
          bands: [
            { label: "Off", color: "#D32F2F", min: null, max: 0.8 },
            { label: "On", color: "#388E3C", min: 0.8, max: null },
          ],
        },
      },
      onChange,
    );

    const toggle = screen.getByRole("checkbox");
    await userEvent.click(toggle);

    expect(onChange).toHaveBeenCalledTimes(1);
    const patch = onChange.mock.calls[0][0] as Partial<KpiWizardFormState>;
    expect(patch.presentation_meta?.evaluation_type).toBe("percentage_of_target");
  });

  it("does not force absolute_value when the form has no presentation_meta yet", async () => {
    const onChange = vi.fn();
    renderStep(
      { direction: "higher_is_better", presentation_meta: null },
      onChange,
    );

    const toggle = screen.getByRole("checkbox");
    await userEvent.click(toggle);

    expect(onChange).toHaveBeenCalledTimes(1);
    const patch = onChange.mock.calls[0][0] as Partial<KpiWizardFormState>;
    // F-017-20: with no existing meta the default basis is percentage_of_target
    // (the spec basis, matching a null presentation_meta on the backend), not a
    // forced absolute_value.
    expect(patch.presentation_meta?.evaluation_type).toBe("percentage_of_target");
  });

  it("defaults closer_is_better to percentage_of_target, not absolute_value", async () => {
    const onChange = vi.fn();
    renderStep(
      { direction: "closer_is_better", presentation_meta: null },
      onChange,
    );

    const toggle = screen.getByRole("checkbox");
    await userEvent.click(toggle);

    expect(onChange).toHaveBeenCalledTimes(1);
    const patch = onChange.mock.calls[0][0] as Partial<KpiWizardFormState>;
    expect(patch.presentation_meta?.evaluation_type).toBe("percentage_of_target");
  });

  it("renders the evaluation basis selector", () => {
    renderStep({ direction: "higher_is_better" });
    // The evaluation basis label should be present (from kpiBusiness.evaluationBasis i18n key).
    expect(screen.getByLabelText(/gauge basis/i)).toBeInTheDocument();
  });
});
