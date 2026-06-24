import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen } from "@testing-library/react";
import LicenseAndEdition from "./LicenseAndEdition";
import { useEdition, useLimits } from "../api/hooks";

vi.mock("../api/hooks", () => ({
  useEdition: vi.fn(),
  useLimits: vi.fn(),
}));

const mockEdition = useEdition as unknown as ReturnType<typeof vi.fn>;
const mockLimits = useLimits as unknown as ReturnType<typeof vi.fn>;

describe("LicenseAndEdition", () => {
  beforeEach(() => {
    mockEdition.mockReset();
    mockLimits.mockReset();
  });

  it("shows a loading state until edition + limits resolve", () => {
    mockEdition.mockReturnValue({ data: undefined, isLoading: true });
    mockLimits.mockReturnValue({ data: undefined, isLoading: true });
    render(<LicenseAndEdition />);
    expect(screen.getByText("Loading edition…")).toBeInTheDocument();
  });

  it("renders the edition chip and current/max resource rows", () => {
    mockEdition.mockReturnValue({
      data: { edition: "community", activated: true },
      isLoading: false,
    });
    mockLimits.mockReturnValue({
      data: {
        edition: "community",
        entitlements: { own_tenants: 2, models: 2, users: 5 },
        usage: { models: 1, users: 3 },
      },
      isLoading: false,
    });
    render(<LicenseAndEdition />);
    expect(screen.getByTestId("license-edition-chip")).toHaveTextContent(
      "Community",
    );
    expect(screen.getByText("Activated")).toBeInTheDocument();
    // models usage current/max
    expect(screen.getByText("1 / 2")).toBeInTheDocument();
    // users usage current/max
    expect(screen.getByText("3 / 5")).toBeInTheDocument();
    // community (non-enterprise) shows the upgrade body + a "Get a license" CTA
    expect(
      screen.getByText(
        "Need more capacity or enterprise features? Get a license at tessallite.io.",
      ),
    ).toBeInTheDocument();
    const cta = screen.getByText("Get a license").closest("a");
    expect(cta).toHaveAttribute("href", "https://tessallite.io/register.html");
  });

  it("shows unlimited for uncapped projects", () => {
    mockEdition.mockReturnValue({
      data: { edition: "community", activated: true },
      isLoading: false,
    });
    mockLimits.mockReturnValue({
      data: { entitlements: { models: 2 }, usage: {} },
      isLoading: false,
    });
    render(<LicenseAndEdition />);
    // projects_per_own_tenant is absent -> unlimited
    expect(screen.getAllByText("Unlimited").length).toBeGreaterThan(0);
  });

  it("shows the manage CTA on enterprise instead of upgrade", () => {
    mockEdition.mockReturnValue({
      data: { edition: "enterprise", activated: true },
      isLoading: false,
    });
    mockLimits.mockReturnValue({
      data: { entitlements: {}, usage: {} },
      isLoading: false,
    });
    render(<LicenseAndEdition />);
    // enterprise (activated) shows the register/manage body, not the upgrade prompt
    expect(
      screen.getByText(
        "Get a license for Tessallite at tessallite.io, then upload it here.",
      ),
    ).toBeInTheDocument();
    expect(
      screen.queryByText(
        "Need more capacity or enterprise features? Get a license at tessallite.io.",
      ),
    ).not.toBeInTheDocument();
  });
});
