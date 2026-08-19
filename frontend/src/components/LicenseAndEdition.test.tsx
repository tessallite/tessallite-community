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
        entitlements: {
          own_tenants: 3,
          projects_per_own_tenant: 4,
          models: 9,
          users: 5,
        },
        usage: { models: 7, users: 3, tenants: 1, projects: 2 },
      },
      isLoading: false,
    });
    render(<LicenseAndEdition />);
    expect(screen.getByTestId("license-edition-chip")).toHaveTextContent(
      "Community",
    );
    expect(screen.getByText("Activated")).toBeInTheDocument();
    // tenants usage current/max (platform-level own-tenant count)
    expect(screen.getByText("1 / 3")).toBeInTheDocument();
    // projects usage current/max (per-tenant project count)
    expect(screen.getByText("2 / 4")).toBeInTheDocument();
    // models usage current/max
    expect(screen.getByText("7 / 9")).toBeInTheDocument();
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

  it("shows an explicit, retryable error when the edition read fails (Bug-7470)", () => {
    const refetch = vi.fn();
    mockEdition.mockReturnValue({
      data: undefined,
      isLoading: false,
      isError: true,
      refetch,
    });
    mockLimits.mockReturnValue({
      data: undefined,
      isLoading: false,
      isError: false,
      refetch: vi.fn(),
    });
    render(<LicenseAndEdition />);
    // Must NOT fall back to the plausible "unactivated" chip / unlimited caps.
    expect(screen.queryByTestId("license-edition-chip")).not.toBeInTheDocument();
    expect(screen.getByTestId("license-load-error")).toBeInTheDocument();
    screen.getByText("Retry").click();
    expect(refetch).toHaveBeenCalled();
  });

  it("shows the error state when the limits read fails (Bug-7470)", () => {
    mockEdition.mockReturnValue({
      data: { edition: "community", activated: true },
      isLoading: false,
      isError: false,
      refetch: vi.fn(),
    });
    mockLimits.mockReturnValue({
      data: undefined,
      isLoading: false,
      isError: true,
      refetch: vi.fn(),
    });
    render(<LicenseAndEdition />);
    expect(screen.getByTestId("license-load-error")).toBeInTheDocument();
  });

  // Bug-7680: an installed-but-invalid licence (verifier rejected an expired or
  // untrusted document) must render distinctly from both "activated" and the
  // plain unactivated state — otherwise an invalid licence looks normal.
  it("renders a distinct invalid-licence banner and chip, not 'Activated'", () => {
    mockEdition.mockReturnValue({
      data: {
        edition: "community",
        activated: false,
        enforcement: true,
        license_state: "invalid",
      },
      isLoading: false,
    });
    mockLimits.mockReturnValue({
      data: { entitlements: { models: 2 }, usage: {} },
      isLoading: false,
    });
    render(<LicenseAndEdition />);
    // Distinct invalid banner + chip are shown.
    expect(screen.getByTestId("license-invalid-banner")).toBeInTheDocument();
    expect(screen.getByTestId("license-invalid-chip")).toHaveTextContent("Invalid");
    expect(
      screen.getByText(
        "The installed license is invalid. It may be expired or its signature is not trusted, so it is not active. Install a current, valid license to activate this instance.",
      ),
    ).toBeInTheDocument();
    // Must NOT read as a normal, active licence.
    expect(screen.queryByText("Activated")).not.toBeInTheDocument();
  });

  it("does not show the invalid banner for a normal activated licence", () => {
    mockEdition.mockReturnValue({
      data: { edition: "community", activated: true },
      isLoading: false,
    });
    mockLimits.mockReturnValue({
      data: { entitlements: { models: 2 }, usage: {} },
      isLoading: false,
    });
    render(<LicenseAndEdition />);
    expect(screen.queryByTestId("license-invalid-banner")).not.toBeInTheDocument();
    expect(screen.getByText("Activated")).toBeInTheDocument();
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
