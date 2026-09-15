import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen } from "@testing-library/react";
import EditionBadge from "./EditionBadge";
import { useEdition, useLimits } from "../api/hooks";

vi.mock("../api/hooks", () => ({
  useEdition: vi.fn(),
  useLimits: vi.fn(),
}));

const mockEdition = useEdition as unknown as ReturnType<typeof vi.fn>;
const mockLimits = useLimits as unknown as ReturnType<typeof vi.fn>;

describe("EditionBadge", () => {
  beforeEach(() => {
    mockLimits.mockReturnValue({ data: { entitlements: { models: 2, users: 2 } } });
  });

  it("renders nothing until the edition resolves", () => {
    mockEdition.mockReturnValue({ data: undefined });
    const { container } = render(<EditionBadge />);
    expect(container.firstChild).toBeNull();
  });

  it("shows the Community label when on the community edition", () => {
    mockEdition.mockReturnValue({ data: { edition: "community", activated: true } });
    render(<EditionBadge />);
    expect(screen.getByTestId("edition-badge")).toHaveTextContent("Community");
  });

  it("shows Internal unlimited when the hatch is on", () => {
    mockEdition.mockReturnValue({
      data: { edition: "internal-unlimited", activated: false, enforcement: false },
    });
    render(<EditionBadge />);
    expect(screen.getByTestId("edition-badge")).toHaveTextContent("Internal unlimited");
  });

  // Bug-9307: an unactivated/invalid license manager reports the real edition
  // name (e.g. "community") with activated=false — the chip used to look
  // identical to a genuinely activated edition either way.
  describe("not-activated marker (Bug-9307)", () => {
    it("shows a Not activated chip for a real edition reporting activated=false", () => {
      mockEdition.mockReturnValue({ data: { edition: "community", activated: false } });
      render(<EditionBadge />);
      expect(screen.getByTestId("edition-not-activated")).toHaveTextContent("Not activated");
    });

    it("does not show the marker for an activated edition", () => {
      mockEdition.mockReturnValue({ data: { edition: "community", activated: true } });
      render(<EditionBadge />);
      expect(screen.queryByTestId("edition-not-activated")).toBeNull();
    });

    it("does not show the marker for the unactivated placeholder edition (already self-describing)", () => {
      mockEdition.mockReturnValue({ data: { edition: "unactivated", activated: false } });
      render(<EditionBadge />);
      expect(screen.queryByTestId("edition-not-activated")).toBeNull();
    });

    it("does not show the marker for the internal-unlimited hatch", () => {
      mockEdition.mockReturnValue({ data: { edition: "internal-unlimited", activated: false } });
      render(<EditionBadge />);
      expect(screen.queryByTestId("edition-not-activated")).toBeNull();
    });
  });
});
