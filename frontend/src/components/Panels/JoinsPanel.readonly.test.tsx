import { describe, it, expect, afterEach, beforeEach, vi } from "vitest";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { I18nContext } from "../../i18n";
import en from "../../i18n";
import { useBuilderStore } from "../../store/builderStore";
import JoinsPanel from "./JoinsPanel";

vi.mock("../../api/hooks", () => ({
  useSources: () => ({ data: [{ id: "source-1" }], isLoading: false }),
  useAllModelTables: () => ({
    data: [
      { id: "table-left", alias: "Orders", table_type: "fact" },
      { id: "table-right", alias: "Customers", table_type: "dim_detail" },
    ],
    isLoading: false,
  }),
  useJoins: () => ({
    data: [
      {
        id: "join-1",
        left_table_id: "table-left",
        right_table_id: "table-right",
        join_type: "left",
        left_column_name: "customer_id",
        right_column_name: "id",
        warnings: [
          "Column type mismatch: customer_id (INTEGER) vs id (VARCHAR). Create a CAST before joining.",
        ],
      },
    ],
    isLoading: false,
  }),
}));

vi.mock("../../api/client", () => ({
  joinsApi: {
    create: vi.fn().mockResolvedValue({ id: "join-new" }),
    update: vi.fn().mockResolvedValue({}),
    delete: vi.fn().mockResolvedValue({}),
  },
  tableAttributesApi: {
    list: vi.fn().mockResolvedValue([]),
  },
  joinPopulationHealthApi: {
    get: vi.fn().mockResolvedValue({
      model_id: "model-1",
      status: "OK",
      evaluated: true,
      join_count: 0,
      evaluated_count: 0,
      warning_count: 0,
      blocked_count: 0,
      warn_only: false,
      items: [],
    }),
  },
}));

// The confirm dialog is normally auto-accepted. One test needs to hold it open
// so the session can turn read-only WHILE the delete is awaiting confirmation —
// the check-then-act window that the persistence-point guard exists to close.
const { confirmControl } = vi.hoisted(() => ({
  confirmControl: { deferred: false, resolve: null as ((v: boolean) => void) | null },
}));

vi.mock("../Confirm", () => ({
  useConfirm: () => () =>
    confirmControl.deferred
      ? new Promise<boolean>((resolve) => {
          confirmControl.resolve = resolve;
        })
      : Promise.resolve(true),
}));

function renderJoinsPanel(readOnly: boolean, role: string | null = "modeler") {
  if (role === null) window.localStorage.removeItem("user_role");
  else window.localStorage.setItem("user_role", role);
  useBuilderStore.getState().reset();
  useBuilderStore.getState().setReadOnly(readOnly);
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <MemoryRouter initialEntries={["/projects/project-1/models/model-1"]}>
      <QueryClientProvider client={qc}>
        <I18nContext.Provider value={en}>
          <Routes>
            <Route
              path="/projects/:projectId/models/:modelId"
              element={<JoinsPanel />}
            />
          </Routes>
        </I18nContext.Provider>
      </QueryClientProvider>
    </MemoryRouter>,
  );
}

describe("JoinsPanel read-only CRUD controls (Bug-5966)", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    confirmControl.deferred = false;
    confirmControl.resolve = null;
    useBuilderStore.getState().reset();
    useBuilderStore.getState().setReadOnly(false);
  });

  afterEach(() => {
    window.localStorage.removeItem("user_role");
  });

  it("hides add, edit, and delete controls in read-only mode", () => {
    renderJoinsPanel(true);

    expect(screen.queryByRole("button", { name: /add/i })).toBeNull();
    expect(screen.queryByTestId("join-edit-join-1")).toBeNull();
    expect(screen.queryByTestId("join-delete-join-1")).toBeNull();
  });

  it("shows add, edit, and delete controls when editing is allowed", () => {
    renderJoinsPanel(false);

    expect(screen.getByRole("button", { name: /add/i })).toBeInTheDocument();
    expect(screen.getByTestId("join-edit-join-1")).toBeInTheDocument();
    expect(screen.getByTestId("join-delete-join-1")).toBeInTheDocument();
  });

  it("withdraws layout-writing edge controls in read-only mode but keeps cardinality readable (Bug-8504)", () => {
    const dispatched: string[] = [];
    const listener = (e: Event) => dispatched.push(e.type);
    window.addEventListener("reset-edge-path", listener);
    window.addEventListener("toggle-edge-pathing-auto", listener);
    try {
      renderJoinsPanel(true);

      // Persisting controls are gone...
      expect(screen.queryByTestId("join-cardinality-source-join-1")).toBeNull();
      expect(screen.queryByTestId("join-cardinality-target-join-1")).toBeNull();
      expect(screen.queryByTestId("join-reset-path-join-1")).toBeNull();
      expect(screen.queryByTestId("join-toggle-pathing-join-1")).toBeNull();
      // ...but the cardinality information itself is still announced. It must
      // carry a role that accepts an accessible name (ARIA 1.2 forbids naming
      // a generic element), or AT drops the label and reads only the glyph.
      expect(screen.getByRole("img", { name: "Left-side cardinality: many" })).toBeInTheDocument();
      expect(screen.getByRole("img", { name: "Right-side cardinality: one" })).toBeInTheDocument();
      expect(dispatched).toEqual([]);
    } finally {
      window.removeEventListener("reset-edge-path", listener);
      window.removeEventListener("toggle-edge-pathing-auto", listener);
    }
  });

  it("keeps the cardinality and edge-path controls actionable for editors (Bug-8504)", () => {
    renderJoinsPanel(false);

    expect(screen.getByTestId("join-cardinality-source-join-1")).toBeInTheDocument();
    expect(screen.getByTestId("join-cardinality-target-join-1")).toBeInTheDocument();
    expect(screen.getByTestId("join-reset-path-join-1")).toBeInTheDocument();
    expect(screen.getByTestId("join-toggle-pathing-join-1")).toBeInTheDocument();
  });

  // Bug-8504 (round 2, R1 review). The authoring gate here MUST be the builder
  // session flag that Canvas also gates on — never the coarse local role string
  // from /users/me. `ALLOWED_LOCAL_USER_ROLES` is (member | tenant_admin |
  // model_technical): `modeler` is a per-project BINDING, not a local role, so
  // the canonical locally-provisioned modeller carries `member`. Gating this
  // panel on `canEditModelConfig()` (as eight sibling panels do) hid every join
  // control from a user the backend authorises, while the canvas beside it
  // stayed fully editable and silently discarded the joins they drew. This case
  // pins the parity: with the per-model gate open, the panel offers authoring
  // whatever the coarse role string says.
  it.each(["member", "viewer", "analyst", "model_technical", "modeler", "tenant_admin"])(
    "offers authoring for local role %s while the model session is editable (Bug-8504)",
    (role) => {
      renderJoinsPanel(false, role);

      expect(screen.getByRole("button", { name: /add/i })).toBeInTheDocument();
      expect(screen.getByTestId("join-edit-join-1")).toBeInTheDocument();
      expect(screen.getByTestId("join-delete-join-1")).toBeInTheDocument();
      expect(screen.getByTestId("join-reset-path-join-1")).toBeInTheDocument();
      expect(screen.getByTestId("join-toggle-pathing-join-1")).toBeInTheDocument();
    },
  );

  it.each(["member", "viewer", "modeler", "tenant_admin"])(
    "withdraws every authoring control for local role %s once the session is read-only (Bug-8504)",
    (role) => {
      renderJoinsPanel(true, role);

      expect(screen.queryByRole("button", { name: /add/i })).toBeNull();
      expect(screen.queryByTestId("join-edit-join-1")).toBeNull();
      expect(screen.queryByTestId("join-delete-join-1")).toBeNull();
      expect(screen.queryByTestId("join-cardinality-source-join-1")).toBeNull();
      expect(screen.queryByTestId("join-cardinality-target-join-1")).toBeNull();
      expect(screen.queryByTestId("join-reset-path-join-1")).toBeNull();
      expect(screen.queryByTestId("join-toggle-pathing-join-1")).toBeNull();
      // The join itself stays readable — this is a read gate, not a blackout.
      expect(screen.getByRole("img", { name: "Left-side cardinality: many" })).toBeInTheDocument();
    },
  );

  // Bug-8504: hiding the Add button does not close the create path — anything
  // that writes `pendingJoin` into the builder store opens the same dialog.
  // R1 review: and refusing it must never be silent, or a deliberate canvas
  // gesture just vanishes and the product reads as broken.
  it("refuses, and explains, a pending canvas join in read-only mode (Bug-8504)", async () => {
    const { joinsApi } = await import("../../api/client");
    renderJoinsPanel(true);

    act(() => {
      useBuilderStore.getState().setPendingJoin({
        leftTableId: "table-left",
        rightTableId: "table-right",
      } as never);
    });

    expect(screen.queryByRole("dialog")).toBeNull();
    expect(joinsApi.create).not.toHaveBeenCalled();
    // The pending join is consumed, not left queued to fire the moment the
    // session becomes editable.
    expect(useBuilderStore.getState().pendingJoin).toBeNull();
    expect(useBuilderStore.getState().globalMessage?.text).toBe(
      "This model is open read-only, so joins cannot be changed.",
    );
  });

  // R1 review: the dialog can outlive the editable session (the model detail
  // resolves to caller_can_author: false while the form is open). It must be
  // withdrawn with an explanation, not left showing an enabled Save that turns
  // a permission boundary into a generic "failed to create" alert.
  it("withdraws an open create dialog when the session turns read-only (Bug-8504)", async () => {
    renderJoinsPanel(false);

    act(() => {
      useBuilderStore.getState().setPendingJoin({
        leftTableId: "table-left",
        rightTableId: "table-right",
      } as never);
    });
    expect(await screen.findByRole("dialog")).toBeInTheDocument();

    act(() => {
      useBuilderStore.getState().setReadOnly(true);
    });

    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    expect(useBuilderStore.getState().globalMessage?.text).toBe(
      "This model is open read-only, so joins cannot be changed.",
    );
  });

  it("opens the create dialog from a pending canvas join when editing is allowed", async () => {
    renderJoinsPanel(false);

    act(() => {
      useBuilderStore.getState().setPendingJoin({
        leftTableId: "table-left",
        rightTableId: "table-right",
      } as never);
    });

    expect(await screen.findByRole("dialog")).toBeInTheDocument();
  });

  // Bug-8504: the guard has to sit at the point of persistence, not only on the
  // control. This drives the exact check-then-act window a UI-only hide cannot
  // cover: an editor starts a delete, the session turns read-only while the
  // confirmation is open, and the mutation then runs with the button long gone.
  it("does not issue the join delete when the session turns read-only mid-confirmation (Bug-8504)", async () => {
    const { joinsApi } = await import("../../api/client");
    confirmControl.deferred = true;
    renderJoinsPanel(false);

    fireEvent.click(screen.getByTestId("join-delete-join-1"));
    await waitFor(() => expect(confirmControl.resolve).not.toBeNull());

    act(() => {
      useBuilderStore.getState().setReadOnly(true);
    });
    await act(async () => {
      confirmControl.resolve!(true);
    });

    // R2 review: the refusal must read as a refusal. Surfacing the generic
    // "Failed to delete join." would tell the user the product broke when it
    // in fact declined — the same misleading outcome the pending-join path was
    // fixed for.
    await waitFor(() =>
      expect(useBuilderStore.getState().globalMessage?.text).toBe(
        "This model is open read-only, so joins cannot be changed.",
      ),
    );
    expect(useBuilderStore.getState().globalMessage?.severity).toBe("info");
    expect(joinsApi.delete).not.toHaveBeenCalled();
  });

  it("issues the join delete for an editor whose session stays editable", async () => {
    const { joinsApi } = await import("../../api/client");
    confirmControl.deferred = true;
    renderJoinsPanel(false);

    fireEvent.click(screen.getByTestId("join-delete-join-1"));
    await waitFor(() => expect(confirmControl.resolve).not.toBeNull());
    await act(async () => {
      confirmControl.resolve!(true);
    });

    await waitFor(() => expect(joinsApi.delete).toHaveBeenCalledTimes(1));
  });

  it("visibly renders backend join-validation warnings for editors and read-only viewers", () => {
    const { unmount } = renderJoinsPanel(false);
    expect(screen.getByRole("status", { name: "Join validation warnings" })).toBeInTheDocument();
    expect(screen.getByText(/customer_id \(INTEGER\) vs id \(VARCHAR\)/)).toBeInTheDocument();
    unmount();

    renderJoinsPanel(true);
    expect(screen.getByRole("status", { name: "Join validation warnings" })).toBeInTheDocument();
    expect(screen.queryByTestId("join-edit-join-1")).toBeNull();
  });
});
