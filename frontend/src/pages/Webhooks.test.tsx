import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

// ---------------------------------------------------------------------------
// Mocks
// ---------------------------------------------------------------------------

const listMock = vi.fn();
const createMock = vi.fn();
const updateMock = vi.fn();
const deleteMock = vi.fn();
const testMock = vi.fn();
const rotateSecretMock = vi.fn();
const deliveriesMock = vi.fn();
const dlqMock = vi.fn();
const dlqCountMock = vi.fn();
const retryDlqMock = vi.fn();
const deleteDlqMock = vi.fn();
const eventTypesMock = vi.fn();

vi.mock("../api/client", () => ({
  webhooksApi: {
    list: (...args: unknown[]) => listMock(...args),
    create: (...args: unknown[]) => createMock(...args),
    update: (...args: unknown[]) => updateMock(...args),
    delete: (...args: unknown[]) => deleteMock(...args),
    test: (...args: unknown[]) => testMock(...args),
    rotateSecret: (...args: unknown[]) => rotateSecretMock(...args),
    deliveries: (...args: unknown[]) => deliveriesMock(...args),
    dlq: (...args: unknown[]) => dlqMock(...args),
    dlqCount: (...args: unknown[]) => dlqCountMock(...args),
    retryDlq: (...args: unknown[]) => retryDlqMock(...args),
    deleteDlq: (...args: unknown[]) => deleteDlqMock(...args),
    eventTypes: (...args: unknown[]) => eventTypesMock(...args),
  },
}));

vi.mock("../i18n", () => ({
  useT: () => (key: string) => key,
}));

vi.mock("../components/HelpIconButton", () => ({
  default: () => null,
}));

import Webhooks from "./Webhooks";

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function renderPage() {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={qc}>
      <Webhooks />
    </QueryClientProvider>,
  );
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

describe("Webhooks — create flow surfaces signing secret", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    listMock.mockResolvedValue([]);
    eventTypesMock.mockResolvedValue([]);
    dlqMock.mockResolvedValue([]);
  });

  it("shows SecretRevealDialog with signing_secret from 201 response after create", async () => {
    const signingSecret = "whsec_test_abc123xyz";
    createMock.mockResolvedValue({
      id: "ep-1",
      name: "Test Endpoint",
      url: "https://example.com/hook",
      event_filters: ["*"],
      is_active: true,
      created_at: null,
      updated_at: null,
      signing_secret: signingSecret,
    });

    renderPage();
    const user = userEvent.setup();

    // Wait for endpoint list to load
    await waitFor(() => {
      expect(screen.getByText("webhooks.noEndpoints")).toBeInTheDocument();
    });

    // Click "New Endpoint"
    await user.click(screen.getByText("webhooks.addEndpoint"));

    // Fill in name and URL
    const nameInput = screen.getByLabelText("webhooks.nameLabel");
    const urlInput = screen.getByLabelText("webhooks.urlLabel");
    await user.type(nameInput, "Test Endpoint");
    await user.type(urlInput, "https://example.com/hook");

    // Click Create
    await user.click(screen.getByText("webhooks.createButton"));

    // The SecretRevealDialog should appear with the secret dialog title
    await waitFor(() => {
      expect(screen.getByText("webhooks.secretDialogTitle")).toBeInTheDocument();
    });

    // The secret value should be present in the password field
    const secretInput = screen.getByDisplayValue(signingSecret);
    expect(secretInput).toBeInTheDocument();

    // Verify the warning is shown
    expect(screen.getByText("webhooks.secretWarning")).toBeInTheDocument();

    // Verify createMock was called with the right payload
    expect(createMock).toHaveBeenCalledWith({
      name: "Test Endpoint",
      url: "https://example.com/hook",
      event_filters: ["*"],
    });
  });

  it("does NOT show SecretRevealDialog after an update (edit)", async () => {
    const existingEndpoint = {
      id: "ep-1",
      name: "Existing",
      url: "https://example.com/hook",
      event_filters: ["*"],
      is_active: true,
      created_at: null,
      updated_at: null,
    };
    listMock.mockResolvedValue([existingEndpoint]);
    updateMock.mockResolvedValue({
      ...existingEndpoint,
      name: "Updated Name",
    });

    renderPage();
    const user = userEvent.setup();

    // Wait for the endpoint row to load
    await waitFor(() => {
      expect(screen.getByText("Existing")).toBeInTheDocument();
    });

    // Click edit
    await user.click(screen.getByLabelText("webhooks.editTooltip"));

    // Change the name
    const nameInput = screen.getByLabelText("webhooks.nameLabel");
    await user.clear(nameInput);
    await user.type(nameInput, "Updated Name");

    // Click Update
    await user.click(screen.getByText("webhooks.updateButton"));

    // Wait for the dialog to close
    await waitFor(() => {
      expect(screen.queryByText("webhooks.dialogEditTitle")).not.toBeInTheDocument();
    });

    // SecretRevealDialog should NOT appear
    expect(screen.queryByText("webhooks.secretDialogTitle")).not.toBeInTheDocument();
  });

  it("shows SecretRevealDialog after secret rotation (existing behavior)", async () => {
    const existingEndpoint = {
      id: "ep-1",
      name: "Existing",
      url: "https://example.com/hook",
      event_filters: ["*"],
      is_active: true,
      created_at: null,
      updated_at: null,
    };
    listMock.mockResolvedValue([existingEndpoint]);

    const rotatedSecret = "whsec_rotated_secret";
    rotateSecretMock.mockResolvedValue({ signing_secret: rotatedSecret });

    renderPage();
    const user = userEvent.setup();

    // Wait for the endpoint row to load
    await waitFor(() => {
      expect(screen.getByText("Existing")).toBeInTheDocument();
    });

    // Click rotate secret
    await user.click(screen.getByLabelText("webhooks.rotateSecretTooltip"));

    // The SecretRevealDialog should appear
    await waitFor(() => {
      expect(screen.getByText("webhooks.secretDialogTitle")).toBeInTheDocument();
    });

    const secretInput = screen.getByDisplayValue(rotatedSecret);
    expect(secretInput).toBeInTheDocument();
  });
});
