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
