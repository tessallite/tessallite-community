import { describe, expect, it, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

// The card reads license state and installs an uploaded signed document via the
// admin API. Mock both calls so the test exercises the upload flow (parse →
// install → success/error) without a backend.
const licenseStatus = vi.fn();
const installLicense = vi.fn();

vi.mock("../api/client", () => ({
  adminApi: {
    licenseStatus: (...a: unknown[]) => licenseStatus(...a),
    installLicense: (...a: unknown[]) => installLicense(...a),
  },
}));

import LicenseManagerCard from "./LicenseManagerCard";

function renderCard() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <LicenseManagerCard />
    </QueryClientProvider>,
  );
}

function fileInput(container: HTMLElement): HTMLInputElement {
  const el = container.querySelector('input[type="file"]');
  if (!el) throw new Error("file input not found");
  return el as HTMLInputElement;
}

// A minimal File stub with text() — avoids depending on jsdom's Blob.text().
function fileStub(name: string, content: string): File {
  return { name, text: () => Promise.resolve(content) } as unknown as File;
}

const STATUS = { edition: "community", enforcement_enabled: true, has_license: false };

describe("LicenseManagerCard", () => {
  beforeEach(() => {
    licenseStatus.mockReset();
    installLicense.mockReset();
  });

  it("renders the current license/edition status", async () => {
    licenseStatus.mockResolvedValue(STATUS);
    renderCard();
    expect(await screen.findByText("community")).toBeInTheDocument();
  });

  it("installs a valid uploaded license document", async () => {
    licenseStatus.mockResolvedValue(STATUS);
    installLicense.mockResolvedValue({});
    const { container } = renderCard();
    await screen.findByText("community");

    const doc = { license_id: "lic_1", edition: "community" };
    fireEvent.change(fileInput(container), {
      target: { files: [fileStub("license.json", JSON.stringify(doc))] },
    });

    // the parsed document is handed to the install mutation, then success shown
    await waitFor(() => expect(installLicense).toHaveBeenCalledWith(doc));
    expect(await screen.findByRole("alert")).toBeInTheDocument();
  });

  it("maps a structured install-reject error_code to a localized hint (Bug-8164)", async () => {
    licenseStatus.mockResolvedValue(STATUS);
    // The backend now rejects with { detail: { error_code, message } }.
    installLicense.mockRejectedValue({
      response: { data: { detail: { error_code: "license_expired", message: "License rejected: expired" } } },
    });
    const { container } = renderCard();
    await screen.findByText("community");

    const doc = { license_id: "lic_exp", edition: "community" };
    fireEvent.change(fileInput(container), {
      target: { files: [fileStub("license.json", JSON.stringify(doc))] },
    });

    // The localized, code-specific hint is shown (not "[object Object]" and not
    // the raw server message when a mapping exists).
    expect(await screen.findByText(/This license has expired/i)).toBeInTheDocument();
  });

  it("falls back to the server message for an unmapped install-reject code (Bug-8164)", async () => {
    licenseStatus.mockResolvedValue(STATUS);
    installLicense.mockRejectedValue({
      response: { data: { detail: { error_code: "some_future_code", message: "License rejected: nope" } } },
    });
    const { container } = renderCard();
    await screen.findByText("community");

    fireEvent.change(fileInput(container), {
      target: { files: [fileStub("license.json", JSON.stringify({ license_id: "x" }))] },
    });

    expect(await screen.findByText("License rejected: nope")).toBeInTheDocument();
  });

  // L21-R1-F02: a PERSISTED rejected licence arrives via GET /admin/license with a
  // machine-readable code under status.error_code. The card must render the
  // code-specific localized hint, not the generic "invalid" banner text.
  const PERSISTED_CODE_HINTS: Array<[string, RegExp]> = [
    ["malformed_license", /missing required fields/i],
    ["invalid_signature", /signature does not match/i],
    ["unknown_key_id", /key this instance does not recognize/i],
    ["unsupported_algorithm", /signature method this build does not support/i],
    ["license_expired", /this license has expired/i],
  ];
  it.each(PERSISTED_CODE_HINTS)(
    "L21-R1-F02 renders persisted error_code hint for %s",
    async (code, pattern) => {
      licenseStatus.mockResolvedValue({
        edition: "community",
        enforcement_enabled: true,
        has_license: true,
        status: {
          edition: "community",
          activated: false,
          license_state: "invalid",
          error_code: code,
        },
      });
      renderCard();
      const banner = await screen.findByTestId("license-manager-invalid-banner");
      expect(banner).toHaveTextContent(pattern);
    },
  );

  it("surfaces an invalid-licence banner when an installed licence fails verification (Bug-7680)", async () => {
    licenseStatus.mockResolvedValue({
      edition: "community",
      enforcement_enabled: true,
      has_license: true,
      status: { edition: "community", activated: false, license_state: "invalid" },
    });
    renderCard();
    expect(
      await screen.findByTestId("license-manager-invalid-banner"),
    ).toBeInTheDocument();
  });

  it("does not show the invalid banner for a healthy installed licence (Bug-7680)", async () => {
    licenseStatus.mockResolvedValue({
      edition: "community",
      enforcement_enabled: true,
      has_license: true,
      status: { edition: "community", activated: true },
    });
    renderCard();
    await screen.findByText("community");
    expect(
      screen.queryByTestId("license-manager-invalid-banner"),
    ).not.toBeInTheDocument();
  });

  it("shows a retryable load error when the status read fails (Bug-7470)", async () => {
    licenseStatus.mockRejectedValue(new Error("network"));
    renderCard();
    // Must surface an explicit error rather than rendering nothing / no-status.
    expect(await screen.findByTestId("license-manager-load-error")).toBeInTheDocument();
    // Retry re-invokes the status query.
    licenseStatus.mockResolvedValue(STATUS);
    fireEvent.click(screen.getByText("Retry"));
    expect(await screen.findByText("community")).toBeInTheDocument();
  });

  it("rejects an invalid (non-JSON) file without calling the API", async () => {
    licenseStatus.mockResolvedValue(STATUS);
    const { container } = renderCard();
    await screen.findByText("community");

    fireEvent.change(fileInput(container), {
      target: { files: [fileStub("bad.json", "{ not valid json")] },
    });

    // bad JSON surfaces an error and never reaches the API
    expect(await screen.findByRole("alert")).toBeInTheDocument();
    expect(installLicense).not.toHaveBeenCalled();
  });
});
