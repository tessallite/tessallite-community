import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes } from "react-router-dom";

const listMock = vi.fn();
const approveMock = vi.fn();
const approveBulkMock = vi.fn();

vi.mock("../../api/client", () => ({
  glossaryApi: {
    list: (...args: unknown[]) => listMock(...args),
    bootstrap: vi.fn(),
    bootstrapJobStatus: vi.fn(),
    approve: (...args: unknown[]) => approveMock(...args),
    approveBulk: (...args: unknown[]) => approveBulkMock(...args),
    reject: vi.fn(),
    delete: vi.fn(),
    deleteBulk: vi.fn(),
    update: vi.fn(),
    create: vi.fn(),
    importCsv: vi.fn(),
    share: vi.fn(),
    revokeShareTokens: vi.fn(),
    regenerateShareToken: vi.fn(),
  },
}));

import GlossaryPanel from "./GlossaryPanel";

const ENTRY = {
  id: "entry-1",
  model_id: "model-1",
  term: "Country",
  definition: "Customer country",
  context_notes: null,
  source: "llm",
  status: "pending_review",
  version: 1,
  superseded_by: null,
  created_by: null,
  proposed_is_hidden: false,
  visibility: "review",
  confidence: "medium",
  created_at: "2026-01-01T00:00:00Z",
  updated_at: "2026-01-01T00:00:00Z",
  sample_values: ["APAC", "EMEA"],
  synonyms: [],
  attachments: [],
};

function renderPanel() {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={["/projects/proj-1/models/model-1"]}>
        <Routes>
          <Route
            path="/projects/:projectId/models/:modelId"
            element={<GlossaryPanel />}
          />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("GlossaryPanel", () => {
  beforeEach(() => {
    listMock.mockReset();
    approveMock.mockReset();
    approveBulkMock.mockReset();
  });

  it("renders sample values returned by the glossary API", async () => {
    listMock.mockResolvedValue([ENTRY]);

    renderPanel();

    await waitFor(() => {
      expect(screen.getByText("Sample values:")).toBeInTheDocument();
    });
    expect(screen.getByText("APAC")).toBeInTheDocument();
    expect(screen.getByText("EMEA")).toBeInTheDocument();
  });

  it("approves all pending entries with one bulk request", async () => {
    listMock.mockResolvedValue([ENTRY]);
    approveBulkMock.mockResolvedValue({ approved_count: 1 });

    renderPanel();
    await screen.findByText("Country");

    await userEvent.click(screen.getByRole("button", { name: /approve all/i }));

    await waitFor(() => {
      expect(approveBulkMock).toHaveBeenCalledTimes(1);
    });
    expect(approveBulkMock).toHaveBeenCalledWith("proj-1", "model-1");
    expect(approveMock).not.toHaveBeenCalled();
  });
});
