/**
 * Chart pop-out: the two things that fail silently if they regress — offering
 * the control on a host that cannot deliver the payload, and sending the chart
 * before the dialog is listening (which loses it).
 */
import { describe, it, expect, vi, afterEach } from "vitest";
import {
  isChartPopoutSupported,
  openChartPopout,
  POPOUT_READY,
} from "../utils/chartPopout";

type Handler = (arg: unknown) => void;

function stubOffice({
  dialogApi12 = true,
  hasDisplayDialog = true,
}: { dialogApi12?: boolean; hasDisplayDialog?: boolean } = {}) {
  const messageChild = vi.fn();
  const handlers: Record<string, Handler> = {};
  const displayDialogAsync = vi.fn(
    (_url: string, _opts: unknown, cb: (r: unknown) => void) => {
      cb({
        status: "succeeded",
        value: {
          messageChild,
          close: vi.fn(),
          addEventHandler: (event: string, handler: Handler) => {
            handlers[event] = handler;
          },
        },
      });
    },
  );

  (globalThis as Record<string, unknown>).Office = {
    AsyncResultStatus: { Succeeded: "succeeded" },
    EventType: {
      DialogMessageReceived: "dialogMessageReceived",
      DialogEventReceived: "dialogEventReceived",
    },
    context: {
      ui: hasDisplayDialog ? { displayDialogAsync } : {},
      requirements: { isSetSupported: () => dialogApi12 },
    },
  };
  return { messageChild, handlers, displayDialogAsync };
}

afterEach(() => {
  delete (globalThis as Record<string, unknown>).Office;
  vi.restoreAllMocks();
});

describe("isChartPopoutSupported", () => {
  it("is false with no Office host at all", () => {
    delete (globalThis as Record<string, unknown>).Office;
    expect(isChartPopoutSupported()).toBe(false);
  });

  it("is false when the host cannot message the dialog (DialogApi < 1.2)", () => {
    // Without messageChild the dialog would open and stay empty, so the
    // control must not be offered at all.
    stubOffice({ dialogApi12: false });
    expect(isChartPopoutSupported()).toBe(false);
  });

  it("is false when displayDialogAsync is missing", () => {
    stubOffice({ hasDisplayDialog: false });
    expect(isChartPopoutSupported()).toBe(false);
  });

  it("is true on a host with dialogs and DialogApi 1.2", () => {
    stubOffice();
    expect(isChartPopoutSupported()).toBe(true);
  });
});

describe("openChartPopout", () => {
  it("does not send the chart until the dialog says it is listening", () => {
    const { messageChild, handlers } = stubOffice();
    openChartPopout({ kind: "chart", rows: [{ a: 1 }], title: "Revenue" });

    expect(messageChild).not.toHaveBeenCalled();

    handlers.dialogMessageReceived({ message: POPOUT_READY });
    expect(messageChild).toHaveBeenCalledTimes(1);
    expect(JSON.parse(messageChild.mock.calls[0][0] as string)).toEqual({
      kind: "chart",
      rows: [{ a: 1 }],
      title: "Revenue",
    });
  });

  it("ignores dialog messages that are not the ready signal", () => {
    const { messageChild, handlers } = stubOffice();
    openChartPopout({ kind: "chart", rows: [{ a: 1 }] });

    handlers.dialogMessageReceived({ message: "something-else" });
    expect(messageChild).not.toHaveBeenCalled();
  });

  it("opens a same-origin dialog URL", () => {
    const { displayDialogAsync } = stubOffice();
    openChartPopout({ kind: "chart", rows: [] });
    const url = displayDialogAsync.mock.calls[0][0] as string;
    expect(url).toBe(new URL("chart-dialog.html", document.baseURI).href);
  });
});
