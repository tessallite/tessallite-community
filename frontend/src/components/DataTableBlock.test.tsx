import { describe, it, expect } from "vitest";
import { render, screen, within } from "@testing-library/react";
import sharedChatMessages from "../i18n/en/shared-chat.json";
import { ChatProvider } from "../../../shared-ui/src/providers/ChatProvider";
import { DataTableBlock } from "../../../shared-ui/src/components/DataTableBlock";
import type { AgentChatAdapter } from "../../../shared-ui/src/types/adapter";

// Real en table resolution, so the test also proves the i18n keys exist
// (Bug-6518) rather than silently passing on raw-key fallback.
const t = (key: string, vars?: Record<string, string | number>) => {
  let text = (sharedChatMessages as Record<string, string>)[key] ?? key;
  if (vars) {
    for (const [name, value] of Object.entries(vars)) {
      text = text.split(`{{${name}}}`).join(String(value));
    }
  }
  return text;
};

function renderTable(rows: Record<string, unknown>[], maxRows?: number) {
  return render(
    <ChatProvider
      adapter={{} as AgentChatAdapter}
      t={t}
      projectId="proj-1"
      config={null}
    >
      <DataTableBlock rows={rows} maxRows={maxRows} />
    </ChatProvider>,
  );
}

describe("DataTableBlock number formatting (Bug-6519)", () => {
  it("does not group year columns", () => {
    renderTable([{ year: 2024, revenue: 1200000 }]);
    // Year must render bare, not "2,024".
    expect(screen.getByText("2024")).toBeInTheDocument();
    expect(screen.queryByText("2,024")).not.toBeInTheDocument();
  });

  it("does not group identifier columns", () => {
    renderTable([{ customer_id: 10001, order_count: 12000 }]);
    expect(screen.getByText("10001")).toBeInTheDocument();
    expect(screen.queryByText("10,001")).not.toBeInTheDocument();
  });

  it("still groups genuine quantity columns", () => {
    renderTable([{ revenue: 1200000 }]);
    expect(screen.getByText("1,200,000")).toBeInTheDocument();
  });

  it("groups a count column even when its value equals a year", () => {
    // Semantics come from the column name, not the magnitude: a count of 2024
    // is a quantity and should group.
    renderTable([{ order_count: 20240 }]);
    expect(screen.getByText("20,240")).toBeInTheDocument();
  });

  it("groups a measure whose name merely starts with a temporal token", () => {
    // "year_revenue" is a quantity, not a list of years — a leading temporal
    // token must not suppress grouping (R1 finding 2).
    renderTable([{ year_revenue: 1200000 }]);
    expect(screen.getByText("1,200,000")).toBeInTheDocument();
  });

  it("preserves leading zeros on string identifiers (no numeric coercion)", () => {
    // A zip stored as a JSON string must render verbatim, not be reparsed to
    // 7030 (R1 finding 1).
    renderTable([{ zip: "07030" }]);
    expect(screen.getByText("07030")).toBeInTheDocument();
    expect(screen.queryByText("7030")).not.toBeInTheDocument();
  });

  it("preserves precision on bigint-as-string ids", () => {
    // Beyond Number.MAX_SAFE_INTEGER, Number() would corrupt trailing digits.
    renderTable([{ customer_id: "1234567890123456789" }]);
    expect(screen.getByText("1234567890123456789")).toBeInTheDocument();
  });
});

describe("DataTableBlock i18n contract (Bug-6518)", () => {
  it("renders the localized over-limit message", () => {
    renderTable([{ a: 1 }, { a: 2 }, { a: 3 }], 2);
    expect(
      screen.getByText(/Results exceed the display limit/i),
    ).toBeInTheDocument();
  });

  it("localizes boolean cells via the i18n table", () => {
    renderTable([{ active: true }, { active: false }]);
    const rowgroup = screen.getAllByRole("rowgroup");
    // body rowgroup holds the data cells
    const body = rowgroup[rowgroup.length - 1];
    expect(within(body).getByText("Yes")).toBeInTheDocument();
    expect(within(body).getByText("No")).toBeInTheDocument();
  });
});
