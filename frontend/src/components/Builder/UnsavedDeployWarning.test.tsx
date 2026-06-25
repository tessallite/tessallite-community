import { describe, expect, it, beforeEach } from "vitest";
import { render, screen } from "@testing-library/react";
import { I18nContext, getMessages } from "../../i18n";
import { useModelEditorStore } from "../../store/useModelEditorStore";
import UnsavedDeployWarning from "./UnsavedDeployWarning";
import StatusBar from "./StatusBar";

const en = getMessages("en");

function renderWithI18n(node: React.ReactNode) {
  return render(<I18nContext.Provider value={en}>{node}</I18nContext.Provider>);
}

function setEditor(state: {
  isDirty: boolean;
  lastSavedVersion: number | null;
  deployedVersion: number | null;
}) {
  useModelEditorStore.setState({
    modelId: "m1",
    lastDeployedAt: null,
    ...state,
  });
}

describe("UnsavedDeployWarning banner (Bug-5515)", () => {
  beforeEach(() => {
    useModelEditorStore.getState().clearModel();
  });

  it("renders the warning when the model is dirty", () => {
    setEditor({ isDirty: true, lastSavedVersion: 2, deployedVersion: 2 });
    renderWithI18n(<UnsavedDeployWarning />);
    const banner = screen.getByTestId("unsaved-deploy-warning");
    expect(banner).toBeInTheDocument();
    expect(banner.textContent).toBe(en["modelSync.queryPanelWarning"]);
  });

  it("renders the warning when saved but not deployed in sync", () => {
    setEditor({ isDirty: false, lastSavedVersion: 3, deployedVersion: 2 });
    renderWithI18n(<UnsavedDeployWarning />);
    expect(screen.getByTestId("unsaved-deploy-warning")).toBeInTheDocument();
  });

  it("renders nothing when the model is saved AND deployed in sync", () => {
    setEditor({ isDirty: false, lastSavedVersion: 4, deployedVersion: 4 });
    renderWithI18n(<UnsavedDeployWarning />);
    expect(screen.queryByTestId("unsaved-deploy-warning")).toBeNull();
  });
});

describe("StatusBar unsaved/undeployed indicator (Bug-5515)", () => {
  beforeEach(() => {
    useModelEditorStore.getState().clearModel();
  });

  function renderBar() {
    return renderWithI18n(
      <StatusBar tableCount={1} joinCount={0} dimCount={0} measCount={0} aggCount={0} />,
    );
  }

  it("flags red and shows the label when the model needs save/deploy", () => {
    setEditor({ isDirty: true, lastSavedVersion: 2, deployedVersion: 2 });
    renderBar();
    const bar = screen.getByTestId("statusbar");
    expect(bar.getAttribute("data-needs-save-deploy")).toBe("true");
    const label = screen.getByTestId("statusbar-unsaved-label");
    expect(label.textContent).toBe(en["modelSync.statusBarLabel"]);
  });

  it("clears the red state only when saved AND deployed in sync", () => {
    setEditor({ isDirty: false, lastSavedVersion: 4, deployedVersion: 4 });
    renderBar();
    const bar = screen.getByTestId("statusbar");
    expect(bar.getAttribute("data-needs-save-deploy")).toBe("false");
    expect(screen.queryByTestId("statusbar-unsaved-label")).toBeNull();
  });
});
