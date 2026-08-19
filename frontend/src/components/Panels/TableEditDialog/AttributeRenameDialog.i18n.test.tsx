/**
 * F-026-05: the attribute rename / collision dialog must route every visible
 * string through the translation catalogue. This test renders it under a
 * fake-locale translator that prefixes every key so any raw English literal
 * left in the component is detectable, and it also asserts the real English
 * catalogue supplies the expected copy.
 */
import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";
import { I18nContext } from "../../../i18n";
import en from "../../../i18n";
import AttributeRenameDialog, { type RenamePreviewRow } from "./AttributeRenameDialog";

const rows: RenamePreviewRow[] = [
  { type: "dimension", id: "d1", source_column_name: "cust_id", current_name: "Customer", suggested_name: "Client" },
  { type: "measure", id: "m1", source_column_name: "amt", current_name: "Amount", suggested_name: "Amount" },
];

/** A translator that marks every resolved key so raw literals stand out. */
function fakeLocale(): Record<string, string> {
  const marked: Record<string, string> = {};
  for (const key of Object.keys(en as Record<string, string>)) {
    marked[key] = `‹${key}›`;
  }
  return marked;
}

function renderWith(messages: Record<string, string>) {
  return render(
    <I18nContext.Provider value={messages}>
      <AttributeRenameDialog
        open
        mode="alias-change"
        rows={rows}
        takenNames={new Set()}
        onApply={() => {}}
        onKeep={() => {}}
        onRevert={() => {}}
      />
    </I18nContext.Provider>,
  );
}

describe("AttributeRenameDialog i18n (F-026-05)", () => {
  it("renders the English catalogue copy for every user-facing string", () => {
    renderWith(en as Record<string, string>);
    expect(screen.getByText("Review attribute renames")).toBeInTheDocument();
    expect(
      screen.getByText(/The alias change affects the names of the following attributes/),
    ).toBeInTheDocument();
    // Table headings and actions are translated, not hard-coded literals.
    expect(screen.getByText("Source column")).toBeInTheDocument();
    expect(screen.getByText("Current name")).toBeInTheDocument();
    expect(screen.getByText("Apply renames")).toBeInTheDocument();
    expect(screen.getByText("Keep current names")).toBeInTheDocument();
    expect(screen.getByText("Revert alias change")).toBeInTheDocument();
    // The redeploy warning is translated.
    expect(
      screen.getByText(/take effect in connected BI tools after you redeploy/),
    ).toBeInTheDocument();
  });

  it("routes every visible string through the translator (no raw English leaks)", () => {
    renderWith(fakeLocale());
    // Under the marked locale the translated title appears wrapped; the raw
    // English literal must NOT appear anywhere.
    expect(screen.getByText("‹attributeRename.titleAlias›")).toBeInTheDocument();
    expect(screen.queryByText("Review attribute renames")).not.toBeInTheDocument();
    expect(screen.queryByText("Apply renames")).not.toBeInTheDocument();
    expect(screen.queryByText("Keep current names")).not.toBeInTheDocument();
    expect(screen.queryByText("Source column")).not.toBeInTheDocument();
  });
});
