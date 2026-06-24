import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen } from "@testing-library/react";
import LicenseEdition from "./LicenseEdition";
import { safeLocalGet } from "../utils/safeLocalStorage";

vi.mock("../utils/safeLocalStorage", () => ({
  safeLocalGet: vi.fn(),
}));

// The child panels make their own data calls; stub them so this test focuses
// on the page-level access guard.
vi.mock("../components/LicenseAndEdition", () => ({
  default: () => <div data-testid="license-edition" />,
}));
vi.mock("../components/LicenseManagerCard", () => ({
  default: () => <div data-testid="license-manager" />,
}));
vi.mock("../components/AdvisoryPanel", () => ({
  default: () => <div data-testid="advisory-panel" />,
}));

const mockGet = safeLocalGet as unknown as ReturnType<typeof vi.fn>;

describe("LicenseEdition page", () => {
  beforeEach(() => mockGet.mockReset());

  it("shows an access-denied notice for non-admins", () => {
    mockGet.mockReturnValue("member");
    render(<LicenseEdition />);
    expect(
      screen.getByText("This screen is only available to system admins."),
    ).toBeInTheDocument();
    expect(screen.queryByTestId("license-edition")).not.toBeInTheDocument();
  });

  it("renders the panels (incl. license manager) for a system admin", () => {
    mockGet.mockReturnValue("system_admin");
    render(<LicenseEdition />);
    expect(screen.getByTestId("license-edition")).toBeInTheDocument();
    expect(screen.getByTestId("license-manager")).toBeInTheDocument();
    expect(screen.getByTestId("advisory-panel")).toBeInTheDocument();
  });
});
