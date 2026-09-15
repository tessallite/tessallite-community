/**
 * Bug-9921: in the compact task-pane presentation applied to the Excel
 * add-in's Ask Tessallite chat mode (commit b628a6aaa), a chart answer
 * collapsed to a 118px-tall strip — too short to read axis labels, a
 * legend, or the plot area. A first fix (220px fixed) was still a literal,
 * per owner feedback: "it should be dynamic."
 *
 * The rule now: the chart takes about 40% of the pane's visible height
 * (window.innerHeight, re-measured on window resize — the task pane fills
 * the whole browser viewport vertically), clamped between
 * COMPACT_CHART_HEIGHT (220px floor — a short pane still gets a legible
 * chart) and COMPACT_CHART_HEIGHT_CEILING (420px ceiling — a tall pane
 * doesn't become a wall of chart). The ratio is applied to CSS pixels
 * (window.innerHeight), never device pixels, so the same rule holds at any
 * devicePixelRatio / Windows display-scaling level.
 *
 * echarts cannot actually paint in jsdom (no canvas 2D context without the
 * `canvas` npm package, which this lane does not add), so `echarts/core`'s
 * `init` is stubbed to a no-op chart handle and ResizeObserver is
 * polyfilled. That isolates the layout assertion — the real production
 * height/style computation in ChartBlock and VisualArtifactBlock — from the
 * unrelated (and here, unavailable) canvas paint step.
 */
import { describe, it, expect, vi, afterEach } from "vitest";
import { act, render, screen } from "@testing-library/react";
import type { ReactNode } from "react";
import { ChatProvider } from "@tessallite/shared-ui/providers/ChatProvider";
import type { AgentChatAdapter } from "@tessallite/shared-ui/types/adapter";
import {
  COMPACT_CHART_HEIGHT,
  COMPACT_CHART_HEIGHT_CEILING,
} from "@tessallite/shared-ui/utils/chartLayout";
import { chatT } from "../i18n/chatStrings";

vi.mock("echarts/core", async (importOriginal) => {
  const actual = await importOriginal<typeof import("echarts/core")>();
  return {
    ...actual,
    init: vi.fn(() => ({
      setOption: vi.fn(),
      resize: vi.fn(),
      dispose: vi.fn(),
      on: vi.fn(),
      off: vi.fn(),
    })),
  };
});

class FakeResizeObserver {
  observe() {}
  unobserve() {}
  disconnect() {}
}
// jsdom does not implement ResizeObserver; ChartBlock/VisualArtifactBlock
// use one to keep the echarts canvas in sync with its container.
// @ts-expect-error test polyfill, not a full ResizeObserver
globalThis.ResizeObserver = globalThis.ResizeObserver ?? FakeResizeObserver;

const NOOP_ADAPTER: AgentChatAdapter = {
  getConversations: vi.fn().mockResolvedValue([]),
  createConversation: vi.fn().mockResolvedValue({ id: "c1", title: null, pinned_model_id: null }),
  getConversation: vi.fn().mockResolvedValue({ id: "c1", title: null, pinned_model_id: null }),
  updateConversation: vi.fn().mockResolvedValue({ id: "c1", title: null, pinned_model_id: null }),
  deleteConversation: vi.fn().mockResolvedValue(undefined),
  getTurns: vi.fn().mockResolvedValue([]),
  streamMessageRaw: vi.fn().mockResolvedValue(new Response()),
  submitFeedback: vi.fn().mockResolvedValue(undefined),
  getSelectableModels: vi.fn().mockResolvedValue([]),
};

function Wrapper({ children }: { children: ReactNode }) {
  return (
    <ChatProvider adapter={NOOP_ADAPTER} t={chatT} projectId="p1" config={null}>
      {children}
    </ChatProvider>
  );
}

// Two measure columns (multi-series) with short, non-temporal labels so
// buildAutoChartSpec resolves to a plain "bar" chart — not the pie/hbar/line
// kinds that take a different sizing branch.
const BAR_ROWS = [
  { region: "North", revenue: 100, cost: 40 },
  { region: "South", revenue: 200, cost: 80 },
  { region: "East", revenue: 150, cost: 60 },
];

const MIN_USABLE_HEIGHT = 200;
const MAX_USABLE_HEIGHT = 260;

// window.innerHeight and window.devicePixelRatio are both configurable
// accessor properties on the jsdom Window, so tests can pin the "screen" the
// component renders under and restore the ambient defaults afterward.
const ORIGINAL_INNER_HEIGHT = window.innerHeight;
const ORIGINAL_DPR = window.devicePixelRatio;

function setViewportHeight(px: number) {
  Object.defineProperty(window, "innerHeight", {
    value: px,
    configurable: true,
    writable: true,
  });
}

function setDevicePixelRatio(ratio: number) {
  Object.defineProperty(window, "devicePixelRatio", {
    value: ratio,
    configurable: true,
    writable: true,
  });
}

afterEach(() => {
  setViewportHeight(ORIGINAL_INNER_HEIGHT);
  setDevicePixelRatio(ORIGINAL_DPR);
});

describe("compact chart height (Bug-9921)", () => {
  // A generous timeout: this mounts a real React tree (provider + MUI sx/
  // emotion styling), which can run past the 5s default under a heavily
  // loaded dev machine even though it resolves in ~1-2s in isolation.
  const RENDER_TIMEOUT = 20000;

  it(
    "gives a ChartBlock bar chart a usable height at a 500px pane, not a collapsed strip",
    async () => {
      setViewportHeight(500);
      const { ChartBlock } = await import("@tessallite/shared-ui/components/ChartBlock");
      render(
        <Wrapper>
          <ChartBlock rows={BAR_ROWS} compact />
        </Wrapper>,
      );
      const canvasHost = screen.getByRole("img");
      const height = parseFloat(getComputedStyle(canvasHost).height);
      expect(height).toBeGreaterThanOrEqual(MIN_USABLE_HEIGHT);
      expect(height).toBeLessThanOrEqual(MAX_USABLE_HEIGHT);
      // ...and within the floor/ceiling clamp that governs every pane size.
      expect(height).toBeGreaterThanOrEqual(COMPACT_CHART_HEIGHT);
      expect(height).toBeLessThanOrEqual(COMPACT_CHART_HEIGHT_CEILING);
    },
    RENDER_TIMEOUT,
  );

  it(
    "grows a ChartBlock bar chart taller at a 900px pane, still inside the clamp",
    async () => {
      setViewportHeight(900);
      const { ChartBlock } = await import("@tessallite/shared-ui/components/ChartBlock");
      render(
        <Wrapper>
          <ChartBlock rows={BAR_ROWS} compact />
        </Wrapper>,
      );
      const canvasHost = screen.getByRole("img");
      const height = parseFloat(getComputedStyle(canvasHost).height);
      // ~40% of 900 = 360: meaningfully taller than the 500px-pane case
      // above, and comfortably inside the floor/ceiling clamp.
      expect(height).toBeGreaterThan(MAX_USABLE_HEIGHT);
      expect(height).toBeGreaterThanOrEqual(COMPACT_CHART_HEIGHT);
      expect(height).toBeLessThanOrEqual(COMPACT_CHART_HEIGHT_CEILING);
    },
    RENDER_TIMEOUT,
  );

  it(
    "floors at COMPACT_CHART_HEIGHT for a very short pane instead of collapsing further",
    async () => {
      setViewportHeight(100); // 40% of 100 = 40, well under the floor
      const { ChartBlock } = await import("@tessallite/shared-ui/components/ChartBlock");
      render(
        <Wrapper>
          <ChartBlock rows={BAR_ROWS} compact />
        </Wrapper>,
      );
      const canvasHost = screen.getByRole("img");
      const height = parseFloat(getComputedStyle(canvasHost).height);
      expect(height).toBe(COMPACT_CHART_HEIGHT);
    },
    RENDER_TIMEOUT,
  );

  it(
    "does not change with devicePixelRatio — only the CSS-pixel viewport height",
    async () => {
      const { ChartBlock } = await import("@tessallite/shared-ui/components/ChartBlock");

      setViewportHeight(500);
      setDevicePixelRatio(1);
      const { unmount } = render(
        <Wrapper>
          <ChartBlock rows={BAR_ROWS} compact />
        </Wrapper>,
      );
      const heightAtDpr1 = parseFloat(
        getComputedStyle(screen.getByRole("img")).height,
      );
      unmount();

      // Same CSS-pixel viewport height, a laptop at 150%/3x display scaling.
      setDevicePixelRatio(3);
      render(
        <Wrapper>
          <ChartBlock rows={BAR_ROWS} compact />
        </Wrapper>,
      );
      const heightAtDpr3 = parseFloat(
        getComputedStyle(screen.getByRole("img")).height,
      );

      expect(heightAtDpr3).toBe(heightAtDpr1);
    },
    RENDER_TIMEOUT,
  );

  it(
    "re-measures on window resize without remounting",
    async () => {
      setViewportHeight(500);
      const { ChartBlock } = await import("@tessallite/shared-ui/components/ChartBlock");
      render(
        <Wrapper>
          <ChartBlock rows={BAR_ROWS} compact />
        </Wrapper>,
      );
      const canvasHost = screen.getByRole("img");
      const heightAt500 = parseFloat(getComputedStyle(canvasHost).height);

      await act(async () => {
        setViewportHeight(900);
        window.dispatchEvent(new Event("resize"));
      });

      const heightAt900 = parseFloat(getComputedStyle(canvasHost).height);
      expect(heightAt900).toBeGreaterThan(heightAt500);
      expect(heightAt900).toBeLessThanOrEqual(COMPACT_CHART_HEIGHT_CEILING);
    },
    RENDER_TIMEOUT,
  );

  it(
    "gives a VisualArtifactBlock chart artifact a usable height at a 500px pane",
    async () => {
      setViewportHeight(500);
      const { VisualArtifactBlock } = await import(
        "@tessallite/shared-ui/components/VisualArtifactBlock"
      );
      render(
        <Wrapper>
          <VisualArtifactBlock
            compact
            artifact={{
              kind: "tessallite.visual.v1",
              renderer: "echarts",
              chart_type: "bar",
              columns: ["region", "revenue"],
              rows: BAR_ROWS,
            }}
          />
        </Wrapper>,
      );
      const canvasHost = screen.getByRole("img");
      const height = parseFloat(getComputedStyle(canvasHost).height);
      expect(height).toBeGreaterThanOrEqual(MIN_USABLE_HEIGHT);
      expect(height).toBeLessThanOrEqual(MAX_USABLE_HEIGHT);
    },
    RENDER_TIMEOUT,
  );

  it(
    "grows a VisualArtifactBlock chart taller at a 900px pane, still inside the clamp",
    async () => {
      setViewportHeight(900);
      const { VisualArtifactBlock } = await import(
        "@tessallite/shared-ui/components/VisualArtifactBlock"
      );
      render(
        <Wrapper>
          <VisualArtifactBlock
            compact
            artifact={{
              kind: "tessallite.visual.v1",
              renderer: "echarts",
              chart_type: "bar",
              columns: ["region", "revenue"],
              rows: BAR_ROWS,
            }}
          />
        </Wrapper>,
      );
      const canvasHost = screen.getByRole("img");
      const height = parseFloat(getComputedStyle(canvasHost).height);
      expect(height).toBeGreaterThan(MAX_USABLE_HEIGHT);
      expect(height).toBeGreaterThanOrEqual(COMPACT_CHART_HEIGHT);
      expect(height).toBeLessThanOrEqual(COMPACT_CHART_HEIGHT_CEILING);
    },
    RENDER_TIMEOUT,
  );
});
