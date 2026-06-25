import { describe, it, expect, beforeEach, afterEach } from "vitest";
import { render, screen } from "@testing-library/react";
import { act } from "@testing-library/react";
import { I18nContext } from "../../i18n";
import en from "../../i18n";
import { useBuilderStore, type PanelId } from "../../store/builderStore";
import Drawer from "./Drawer";

// F-026-09: opening Saved Queries / Scratchpad / Alerts titled the drawer with
// the raw lowercase id; these panels now resolve their translated titles.
function renderDrawer() {
  return render(
    <I18nContext.Provider value={en as Record<string, string>}>
      <Drawer>
        <div>panel body</div>
      </Drawer>
    </I18nContext.Provider>,
  );
}

describe("Drawer panel titles (F-026-09)", () => {
  beforeEach(() => act(() => useBuilderStore.getState().reset()));
  afterEach(() => act(() => useBuilderStore.getState().reset()));

  const cases: Array<[PanelId, string]> = [
    ["saved-queries", en["panels.savedQueries"]],
    ["scratchpad", en["panels.scratchpad"]],
    ["alerts", en["panels.alerts"]],
  ];

  for (const [panel, expectedTitle] of cases) {
    it(`renders the translated title for ${panel}`, () => {
      act(() => useBuilderStore.getState().openPanel(panel));
      renderDrawer();
      expect(screen.getByRole("heading", { name: expectedTitle })).toBeInTheDocument();
      // The raw lowercase id must not leak as the heading.
      expect(screen.queryByRole("heading", { name: panel })).toBeNull();
    });
  }
});
