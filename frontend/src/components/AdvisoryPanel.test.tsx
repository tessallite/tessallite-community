import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen } from "@testing-library/react";
import AdvisoryPanel from "./AdvisoryPanel";
import { useAdvisories } from "../api/hooks";

vi.mock("../api/hooks", () => ({
  useAdvisories: vi.fn(),
}));

const mockAdvisories = useAdvisories as unknown as ReturnType<typeof vi.fn>;

describe("AdvisoryPanel", () => {
  beforeEach(() => {
    mockAdvisories.mockReset();
  });

  it("shows a loading message while fetching", () => {
    mockAdvisories.mockReturnValue({ data: undefined, isLoading: true, isError: false });
    render(<AdvisoryPanel />);
    expect(screen.getByText("Loading advisories…")).toBeInTheDocument();
  });

  it("shows the unreachable message on error without crashing", () => {
    mockAdvisories.mockReturnValue({ data: undefined, isLoading: false, isError: true });
    render(<AdvisoryPanel />);
    expect(
      screen.getByText(/advisory feed is currently unreachable/i),
    ).toBeInTheDocument();
  });

  it("shows the empty message when the feed has no advisories", () => {
    mockAdvisories.mockReturnValue({ data: [], isLoading: false, isError: false });
    render(<AdvisoryPanel />);
    expect(screen.getByText("No advisories at this time.")).toBeInTheDocument();
  });

  it("renders advisory rows with title, severity, and link", () => {
    mockAdvisories.mockReturnValue({
      data: [
        {
          id: "TSL-2026-0001",
          published: "2026-06-22",
          severity: "high",
          title: "Example advisory",
          summary: "A test advisory summary.",
          fixed_in: "0.1.0",
          link: "https://tessallite.io/advisories/TSL-2026-0001",
        },
      ],
      isLoading: false,
      isError: false,
    });
    render(<AdvisoryPanel />);
    expect(screen.getByText("Example advisory")).toBeInTheDocument();
    expect(screen.getByText("High")).toBeInTheDocument();
    expect(screen.getByText("TSL-2026-0001")).toBeInTheDocument();
    expect(screen.getByText("Fixed in 0.1.0")).toBeInTheDocument();
    const link = screen.getByText("Read more").closest("a");
    expect(link).toHaveAttribute(
      "href",
      "https://tessallite.io/advisories/TSL-2026-0001",
    );
  });

  it("falls back to the info severity label for unknown severities", () => {
    mockAdvisories.mockReturnValue({
      data: [{ id: "X", title: "No severity advisory" }],
      isLoading: false,
      isError: false,
    });
    render(<AdvisoryPanel />);
    expect(screen.getByText("Info")).toBeInTheDocument();
  });
});
