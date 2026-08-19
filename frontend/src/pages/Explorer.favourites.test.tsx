/**
 * Bug-8183 — model favourites are a user artifact, not a browser artifact.
 *
 * Before this landed, `pinnedModels` was seeded from and written to
 * `localStorage["pinned_models_<tenant>"]`, so a pin died with the browser
 * profile. These tests assert the pin comes from the server with NO localStorage
 * involved, that toggling writes through the preferences API with the "model"
 * entity type, and that pins made under the old scheme are migrated rather than
 * silently discarded.
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

const getFavouriteModelsMock = vi.fn();
const toggleFavouriteMock = vi.fn();
const listProjectsMock = vi.fn();
const listModelsMock = vi.fn();
const tenantMeMock = vi.fn();

vi.mock("../api/client", () => ({
  projectsApi: {
    list: (...args: unknown[]) => listProjectsMock(...args),
  },
  modelsApi: {
    list: (...args: unknown[]) => listModelsMock(...args),
  },
  tenantsApi: {
    me: (...args: unknown[]) => tenantMeMock(...args),
  },
  preferencesApi: {
    getFavouriteModels: (...args: unknown[]) => getFavouriteModelsMock(...args),
    toggleFavourite: (...args: unknown[]) => toggleFavouriteMock(...args),
  },
}));

vi.mock("../api/agentApi", () => ({
  agentApi: { getConfig: vi.fn().mockResolvedValue({}) },
}));

vi.mock("../api/versionsApi", () => ({
  versionsApi: { deploy: vi.fn(), undeploy: vi.fn() },
}));

vi.mock("../i18n", () => ({
  useT: () => (key: string) => key,
}));

vi.mock("../auth/currentUser", () => ({
  isTenantAdmin: () => true,
  canEditModelConfig: () => true,
}));

vi.mock("../components/Confirm", () => ({
  useConfirm: () => vi.fn().mockResolvedValue(true),
}));

vi.mock("../components/HelpIconButton", () => ({ default: () => null }));
vi.mock("../components/importExport/ProjectImportExportDialog", () => ({
  default: () => null,
}));
vi.mock("../components/importExport/ModelImportExportDialog", () => ({
  default: () => null,
}));
vi.mock("../components/Settings/ProjectConfigDrawer", () => ({
  default: () => null,
}));

vi.mock("react-router-dom", () => ({
  useNavigate: () => vi.fn(),
}));

import Explorer from "./Explorer";

const PROJECT = { id: "p1", slug: "project1", display_name: "Project One", is_active: true };
const MODELS = [
  { id: "m1", slug: "modelx", display_name: "Model X", deployed_version_id: null },
  { id: "m2", slug: "modely", display_name: "Model Y", deployed_version_id: null },
];

function renderExplorer() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <Explorer />
    </QueryClientProvider>,
  );
}

/** The star button carries the pinned state in its aria-label. */
function pinButton(displayName: string) {
  return (
    screen.queryByLabelText(`common.unpin ${displayName}`) ??
    screen.getByLabelText(`common.pin ${displayName}`)
  );
}

/**
 * A stateful fake of the preferences store, so a toggle actually changes what
 * the next read returns. A mock that always answers the same list would let an
 * optimistic-only implementation pass while nothing was ever persisted.
 */
let serverFavourites: string[] = [];

beforeEach(() => {
  vi.clearAllMocks();
  localStorage.clear();
  serverFavourites = [];
  listProjectsMock.mockResolvedValue([PROJECT]);
  listModelsMock.mockResolvedValue(MODELS);
  tenantMeMock.mockResolvedValue({ display_name: "Acme" });
  getFavouriteModelsMock.mockImplementation(async () => ({
    model_ids: [...serverFavourites],
  }));
  toggleFavouriteMock.mockImplementation(async (_p: string, _m: string, body: { entity_id: string }) => {
    const id = body.entity_id;
    if (serverFavourites.includes(id)) {
      serverFavourites = serverFavourites.filter((x) => x !== id);
      return { favourited: false };
    }
    serverFavourites = [id, ...serverFavourites];
    return { favourited: true };
  });
});

describe("Bug-8183 — model favourites come from the server", () => {
  it("pins a model the server reports as favourite with nothing in localStorage", async () => {
    serverFavourites = ["m2"];

    renderExplorer();

    await waitFor(() => expect(screen.getByText("Model Y")).toBeInTheDocument());
    await waitFor(() =>
      expect(screen.getByLabelText("common.unpin Model Y")).toBeInTheDocument(),
    );
    // The other model in the same project is untouched, so the pin is per
    // model rather than a blanket "something is favourited" flag.
    expect(screen.getByLabelText("common.pin Model X")).toBeInTheDocument();
    expect(localStorage.getItem("pinned_models_")).toBeNull();
    expect(getFavouriteModelsMock).toHaveBeenCalledWith("p1");
  });

  it("writes a toggle through the preferences API as an entity_type of model", async () => {
    renderExplorer();
    await waitFor(() => expect(screen.getByText("Model X")).toBeInTheDocument());

    await userEvent.click(pinButton("Model X"));

    await waitFor(() =>
      expect(toggleFavouriteMock).toHaveBeenCalledWith("p1", "m1", {
        entity_type: "model",
        entity_id: "m1",
      }),
    );
    // Still pinned after the refetch, so the state survived the round trip
    // rather than existing only in the optimistic cache write.
    await waitFor(() =>
      expect(screen.getByLabelText("common.unpin Model X")).toBeInTheDocument(),
    );
    expect(serverFavourites).toEqual(["m1"]);
  });

  it("restores the star when the server refuses the toggle", async () => {
    serverFavourites = ["m1"];
    toggleFavouriteMock.mockRejectedValue(new Error("boom"));

    renderExplorer();
    await waitFor(() =>
      expect(screen.getByLabelText("common.unpin Model X")).toBeInTheDocument(),
    );

    await userEvent.click(screen.getByLabelText("common.unpin Model X"));

    await waitFor(() => expect(toggleFavouriteMock).toHaveBeenCalled());
    await waitFor(() =>
      expect(screen.getByLabelText("common.unpin Model X")).toBeInTheDocument(),
    );
  });

  it("migrates pins left in localStorage by the old browser-local scheme", async () => {
    localStorage.setItem("pinned_models_", JSON.stringify(["m2", "other-project-model"]));

    renderExplorer();
    await waitFor(() => expect(screen.getByText("Model Y")).toBeInTheDocument());

    await waitFor(() =>
      expect(toggleFavouriteMock).toHaveBeenCalledWith("p1", "m2", {
        entity_type: "model",
        entity_id: "m2",
      }),
    );
    // Only this project's ids are claimed; a model belonging to a project the
    // user has not opened yet stays behind for when they do.
    expect(toggleFavouriteMock).toHaveBeenCalledTimes(1);
    expect(JSON.parse(localStorage.getItem("pinned_models_") ?? "[]")).toEqual([
      "other-project-model",
    ]);
    // The pin survives as a server-side favourite, which is the whole point of
    // migrating it rather than dropping the key.
    await waitFor(() =>
      expect(screen.getByLabelText("common.unpin Model Y")).toBeInTheDocument(),
    );
    expect(serverFavourites).toEqual(["m2"]);
  });

  it("does not re-toggle a legacy pin the server already holds", async () => {
    localStorage.setItem("pinned_models_", JSON.stringify(["m1"]));
    serverFavourites = ["m1"];

    renderExplorer();
    await waitFor(() =>
      expect(screen.getByLabelText("common.unpin Model X")).toBeInTheDocument(),
    );

    // A blind replay would call /favourite and UN-favourite what is already
    // pinned, because that route is a toggle rather than a set.
    await waitFor(() =>
      expect(localStorage.getItem("pinned_models_")).toBeNull(),
    );
    expect(toggleFavouriteMock).not.toHaveBeenCalled();
  });
});
