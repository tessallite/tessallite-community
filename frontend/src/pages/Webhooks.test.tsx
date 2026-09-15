import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { ConfirmProvider } from "../components/Confirm";

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
      <ConfirmProvider>
        <Webhooks />
      </ConfirmProvider>
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
      signing_secret: null,
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

  it("shows the one-time secret after a URL edit rotates it (Bug-8556)", async () => {
    const existingEndpoint = {
      id: "ep-1",
      name: "Existing",
      url: "https://old.example.com/hook",
      event_filters: ["*"],
      is_active: true,
      created_at: null,
      updated_at: null,
    };
    const rotatedSecret = "whsec_url_edit_rotation";
    listMock.mockResolvedValue([existingEndpoint]);
    updateMock.mockResolvedValue({
      ...existingEndpoint,
      url: "https://new.example.com/hook",
      signing_secret: rotatedSecret,
    });

    renderPage();
    const user = userEvent.setup();

    await waitFor(() => expect(screen.getByText("Existing")).toBeInTheDocument());
    await user.click(screen.getByLabelText("webhooks.editTooltip"));

    const urlInput = screen.getByLabelText("webhooks.urlLabel");
    await user.clear(urlInput);
    await user.type(urlInput, "https://new.example.com/hook");
    await user.click(screen.getByText("webhooks.updateButton"));

    await waitFor(() => {
      expect(screen.getByText("webhooks.secretDialogTitle")).toBeInTheDocument();
    });
    expect(screen.getByDisplayValue(rotatedSecret)).toBeInTheDocument();
    expect(screen.getByText("webhooks.secretRotatedByUrlChange")).toBeInTheDocument();
    expect(updateMock).toHaveBeenCalledWith("ep-1", {
      name: "Existing",
      url: "https://new.example.com/hook",
      event_filters: ["*"],
    });
  });

  it("drops retired persisted filters on an ordinary edit (D21-SOL-R1-F01 / Bug-9614)", async () => {
    const existingEndpoint = {
      id: "ep-legacy",
      name: "Pre-upgrade endpoint",
      url: "https://example.com/hook",
      event_filters: ["tenant.deleted", "model.deleted"],
      is_active: true,
      created_at: null,
      updated_at: null,
    };
    listMock.mockResolvedValue([existingEndpoint]);
    eventTypesMock.mockResolvedValue([
      { value: "model.deleted", label: "Model Deleted" },
    ]);
    updateMock.mockResolvedValue({
      ...existingEndpoint,
      name: "Updated endpoint",
      event_filters: ["model.deleted"],
      signing_secret: null,
    });

    renderPage();
    const user = userEvent.setup();

    await waitFor(() =>
      expect(screen.getByText("Pre-upgrade endpoint")).toBeInTheDocument(),
    );
    await user.click(screen.getByLabelText("webhooks.editTooltip"));

    const currentFilter = await screen.findByLabelText("Model Deleted");
    expect(currentFilter).toBeChecked();

    const nameInput = screen.getByLabelText("webhooks.nameLabel");
    await user.clear(nameInput);
    await user.type(nameInput, "Updated endpoint");
    await user.click(screen.getByText("webhooks.updateButton"));

    await waitFor(() => {
      expect(updateMock).toHaveBeenCalledWith("ep-legacy", {
        name: "Updated endpoint",
        url: "https://example.com/hook",
        event_filters: ["model.deleted"],
      });
    });
    const submittedFilters = updateMock.mock.calls[0][1].event_filters;
    expect(submittedFilters).not.toContain("tenant.deleted");
    expect(submittedFilters).not.toContain("*");
    await waitFor(() =>
      expect(screen.queryByText("webhooks.dialogEditTitle")).not.toBeInTheDocument(),
    );
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

    // Click rotate secret — Bug-6573: this now opens a confirmation dialog first.
    await user.click(screen.getByLabelText("webhooks.rotateSecretTooltip"));
    await screen.findByText("webhooks.rotateSecretConfirmTitle");
    await user.click(screen.getByTestId("confirm-action-button"));

    // The SecretRevealDialog should appear
    await waitFor(() => {
      expect(screen.getByText("webhooks.secretDialogTitle")).toBeInTheDocument();
    });

    const secretInput = screen.getByDisplayValue(rotatedSecret);
    expect(secretInput).toBeInTheDocument();
  });

  // Bug-6573: rotation used to be immediate with no confirmation — the old
  // secret dies instantly and cannot be recovered.
  it("does not rotate the secret until the confirmation dialog is accepted", async () => {
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
    rotateSecretMock.mockResolvedValue({ signing_secret: "whsec_rotated" });

    renderPage();
    const user = userEvent.setup();
    await waitFor(() => expect(screen.getByText("Existing")).toBeInTheDocument());

    await user.click(screen.getByLabelText("webhooks.rotateSecretTooltip"));
    await screen.findByText("webhooks.rotateSecretConfirmTitle");
    expect(rotateSecretMock).not.toHaveBeenCalled();
  });

  it("does not rotate the secret when the confirmation dialog is cancelled", async () => {
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

    renderPage();
    const user = userEvent.setup();
    await waitFor(() => expect(screen.getByText("Existing")).toBeInTheDocument());

    await user.click(screen.getByLabelText("webhooks.rotateSecretTooltip"));
    await screen.findByText("webhooks.rotateSecretConfirmTitle");
    await user.click(screen.getByText("confirm.defaultCancel"));

    await waitFor(() =>
      expect(screen.queryByText("webhooks.rotateSecretConfirmTitle")).not.toBeInTheDocument(),
    );
    expect(rotateSecretMock).not.toHaveBeenCalled();
  });
});

// Bug-7336: "Test delivery" fired the request, invalidated the query, and
// showed NOTHING — a network-down endpoint and a working one looked
// identical to the operator clicking the button.
describe("Webhooks — test delivery shows a visible outcome (Bug-7336)", () => {
  const existingEndpoint = {
    id: "ep-1",
    name: "Existing",
    url: "https://example.com/hook",
    event_filters: ["*"],
    is_active: true,
    created_at: null,
    updated_at: null,
  };

  beforeEach(() => {
    vi.clearAllMocks();
    listMock.mockResolvedValue([existingEndpoint]);
    eventTypesMock.mockResolvedValue([]);
    dlqMock.mockResolvedValue([]);
  });

  it("shows a success outcome when the delivery is delivered", async () => {
    testMock.mockResolvedValue({
      id: "d-1",
      endpoint_id: "ep-1",
      event_type: "test",
      payload: {},
      status: "delivered",
      attempts: 1,
      response_code: 200,
      error_message: null,
      created_at: null,
    });
    renderPage();
    const user = userEvent.setup();

    await waitFor(() => expect(screen.getByText("Existing")).toBeInTheDocument());
    await user.click(screen.getByLabelText("webhooks.testDeliveryTooltip"));

    expect(await screen.findByText("webhooks.testDeliverySuccessShort")).toBeInTheDocument();
  });

  it("shows the server's failure reason when the delivery is dead-lettered", async () => {
    testMock.mockResolvedValue({
      id: "d-2",
      endpoint_id: "ep-1",
      event_type: "test",
      payload: {},
      status: "dlq",
      attempts: 1,
      response_code: 503,
      error_message: "Connection refused",
      created_at: null,
    });
    renderPage();
    const user = userEvent.setup();

    await waitFor(() => expect(screen.getByText("Existing")).toBeInTheDocument());
    await user.click(screen.getByLabelText("webhooks.testDeliveryTooltip"));

    const outcome = await screen.findByText("webhooks.testDeliveryFailedShort");
    expect(outcome).toBeInTheDocument();
  });

  it("shows a failure outcome when the request itself errors", async () => {
    testMock.mockRejectedValue({ response: { status: 500, data: { detail: "boom" } } });
    renderPage();
    const user = userEvent.setup();

    await waitFor(() => expect(screen.getByText("Existing")).toBeInTheDocument());
    await user.click(screen.getByLabelText("webhooks.testDeliveryTooltip"));

    expect(await screen.findByText("webhooks.testDeliveryFailedShort")).toBeInTheDocument();
  });

  it.each(["delivered", "failed"])("invalidates a %s outcome when the endpoint URL is saved", async (status) => {
    testMock.mockResolvedValue({ status, response_code: 200, error_message: "failed" });
    const updated = { ...existingEndpoint, url: "https://new.example.com/hook" };
    updateMock.mockImplementation(async () => {
      listMock.mockResolvedValue([updated]);
      return updated;
    });
    renderPage();
    const user = userEvent.setup();
    await screen.findByText("Existing");
    await user.click(screen.getByLabelText("webhooks.testDeliveryTooltip"));
    const label = status === "delivered"
      ? "webhooks.testDeliverySuccessShort" : "webhooks.testDeliveryFailedShort";
    expect(await screen.findByText(label)).toBeInTheDocument();

    await user.click(screen.getByLabelText("webhooks.editTooltip"));
    await user.clear(screen.getByLabelText("webhooks.urlLabel"));
    await user.type(screen.getByLabelText("webhooks.urlLabel"), updated.url);
    await user.click(screen.getByText("webhooks.updateButton"));
    await screen.findByText("https://new.example.com/***");
    expect(screen.queryByText(label)).not.toBeInTheDocument();
    expect(testMock).toHaveBeenCalledTimes(1);

    await user.click(screen.getByLabelText("webhooks.testDeliveryTooltip"));
    expect(await screen.findByText(label)).toBeInTheDocument();
  });

  it("does not attach an in-flight test outcome to a saved replacement URL", async () => {
    let finishTest!: (value: unknown) => void;
    testMock.mockReturnValue(new Promise((resolve) => { finishTest = resolve; }));
    const updated = { ...existingEndpoint, url: "https://new.example.com/hook" };
    updateMock.mockImplementation(async () => {
      listMock.mockResolvedValue([updated]);
      return updated;
    });
    renderPage();
    const user = userEvent.setup();
    await screen.findByText("Existing");
    await user.click(screen.getByLabelText("webhooks.testDeliveryTooltip"));
    await user.click(screen.getByLabelText("webhooks.editTooltip"));
    await user.clear(screen.getByLabelText("webhooks.urlLabel"));
    await user.type(screen.getByLabelText("webhooks.urlLabel"), updated.url);
    await user.click(screen.getByText("webhooks.updateButton"));
    await screen.findByText("https://new.example.com/***");

    finishTest({ status: "delivered", response_code: 200 });
    await waitFor(() => expect(screen.getByLabelText("webhooks.testDeliveryTooltip")).toBeEnabled());
    expect(screen.queryByText("webhooks.testDeliverySuccessShort")).not.toBeInTheDocument();
  });

  // Bug-9566: the outcome chip used to stay visible (from the prior test)
  // across a new test click, so a stale failure could still be showing while
  // a fresh, possibly-successful test was in flight.
  it("clears the previous outcome chip when a re-test starts", async () => {
    testMock.mockRejectedValueOnce({ response: { status: 500, data: { detail: "boom" } } });
    let resolveSecond: (value: unknown) => void = () => {};
    const secondCall = new Promise((resolve) => {
      resolveSecond = resolve;
    });
    testMock.mockImplementationOnce(() => secondCall);

    renderPage();
    const user = userEvent.setup();

    await waitFor(() => expect(screen.getByText("Existing")).toBeInTheDocument());
    await user.click(screen.getByLabelText("webhooks.testDeliveryTooltip"));
    expect(await screen.findByText("webhooks.testDeliveryFailedShort")).toBeInTheDocument();

    await user.click(screen.getByLabelText("webhooks.testDeliveryTooltip"));

    // The stale failure chip must be gone the instant the new test starts,
    // even though the new request has not resolved yet.
    expect(screen.queryByText("webhooks.testDeliveryFailedShort")).not.toBeInTheDocument();
    expect(screen.queryByText("webhooks.testDeliverySuccessShort")).not.toBeInTheDocument();

    resolveSecond({
      id: "d-3",
      endpoint_id: "ep-1",
      event_type: "test",
      payload: {},
      status: "delivered",
      attempts: 1,
      response_code: 200,
      error_message: null,
      created_at: null,
    });

    expect(await screen.findByText("webhooks.testDeliverySuccessShort")).toBeInTheDocument();
  });
});
