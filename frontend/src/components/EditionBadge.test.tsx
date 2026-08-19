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
});
