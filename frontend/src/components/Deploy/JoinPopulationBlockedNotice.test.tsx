import { describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { I18nContext } from "../../i18n";
import en from "../../i18n";
import JoinPopulationBlockedNotice from "./JoinPopulationBlockedNotice";
import type { JoinPopulationBlockedDetail } from "../../api/versionsApi";

const detail: JoinPopulationBlockedDetail = {
  code: "JOIN_POPULATION_BLOCKED",
  message: "deployment refused",
  threshold: 0.15,
  joins: [
    {
      join_id: "join-1",
      join_label: "Fact.customer_id ↔ Customer.id",
      population_participation: "undeclared",
      status: "BLOCKED",
      row_effect_ratio: 0.2,
      reason: "measured row effect exceeds threshold",
    },
    {
      join_id: "join-2",
      join_label: "Fact.region_id ↔ Region.id",
      population_participation: "enrichment_only",
      status: "BLOCKED",
      row_effect_ratio: 0.18,
      reason: "filtering enrichment effect exceeds threshold",
    },
  ],
};

describe("JoinPopulationBlockedNotice", () => {
  it("shows both selected-snapshot offenders and a keyboard-accessible action", async () => {
    const onOpenJoins = vi.fn();
    render(
      <I18nContext.Provider value={en}>
        <JoinPopulationBlockedNotice detail={detail} onOpenJoins={onOpenJoins} />
      </I18nContext.Provider>,
    );

    expect(screen.getByRole("alert")).toHaveTextContent(
      "Deployment blocked by join-population policy",
    );
    expect(screen.getByText("Fact.customer_id ↔ Customer.id")).toBeInTheDocument();
    expect(screen.getByText("Fact.region_id ↔ Region.id")).toBeInTheDocument();
    expect(screen.getByText(/Declare the join's population role accurately/)).toBeInTheDocument();

    const action = screen.getByRole("button", { name: "Open Joins" });
    action.focus();
    expect(action).toHaveFocus();
    await userEvent.keyboard("{Enter}");
    expect(onOpenJoins).toHaveBeenCalledTimes(1);
  });
});
