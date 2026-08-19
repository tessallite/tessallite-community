import { describe, it, expect, vi, beforeEach } from "vitest";
import { grantAccessWithSupersede } from "./grantAccessWithSupersede";
import { accessApi } from "../../api/client";

// Bug-8101: the shared Modeller-supersedes-Model-viewer grant flow must show the
// confirmation only when the backend preflight says supersession will occur, and
// must NOT mutate on cancel.
vi.mock("../../api/client", () => ({
  accessApi: {
    preflight: vi.fn(),
    grant: vi.fn(),
  },
}));

const MESSAGES = {
  title: "Modeller supersedes Model viewer",
  message: "Modeller supersedes Model viewer. The Model viewer role will be removed.",
  confirmLabel: "Continue",
};

describe("grantAccessWithSupersede", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("grants directly with no confirmation when no supersession applies", async () => {
    (accessApi.preflight as vi.Mock).mockResolvedValue({
      supersedes: false,
      removed_model_viewer_count: 0,
      grant_is_redundant: false,
    });
    (accessApi.grant as vi.Mock).mockResolvedValue({});
    const confirm = vi.fn();

    const result = await grantAccessWithSupersede(
      "p1",
      { user_identity: "a@b.com", role: "viewer", model_id: null },
      confirm,
      MESSAGES,
    );

    expect(result).toBe("granted");
    expect(confirm).not.toHaveBeenCalled();
    expect(accessApi.grant).toHaveBeenCalledWith("p1", expect.any(Object));
  });

  it("shows the confirmation and grants with supersede=true when confirmed", async () => {
    (accessApi.preflight as vi.Mock).mockResolvedValue({
      supersedes: true,
      removed_model_viewer_count: 1,
      grant_is_redundant: false,
    });
    (accessApi.grant as vi.Mock).mockResolvedValue({});
    const confirm = vi.fn().mockResolvedValue(true);

    const result = await grantAccessWithSupersede(
      "p1",
      { user_identity: "a@b.com", role: "modeler", model_id: null },
      confirm,
      MESSAGES,
    );

    expect(result).toBe("granted");
    expect(confirm).toHaveBeenCalledWith({
      title: MESSAGES.title,
      message: MESSAGES.message,
      confirmLabel: MESSAGES.confirmLabel,
    });
    expect(accessApi.grant).toHaveBeenCalledWith("p1", expect.any(Object), true);
  });

  it("applies NOTHING on cancel (neither change)", async () => {
    (accessApi.preflight as vi.Mock).mockResolvedValue({
      supersedes: true,
      removed_model_viewer_count: 1,
      grant_is_redundant: false,
    });
    const confirm = vi.fn().mockResolvedValue(false);

    const result = await grantAccessWithSupersede(
      "p1",
      { user_identity: "a@b.com", role: "modeler", model_id: null },
      confirm,
      MESSAGES,
    );

    expect(result).toBe("cancelled");
    expect(accessApi.grant).not.toHaveBeenCalled();
  });
});
