import "@testing-library/jest-dom/vitest";

const canvasContext = {
  arc: () => undefined,
  beginPath: () => undefined,
  bezierCurveTo: () => undefined,
  clearRect: () => undefined,
  clip: () => undefined,
  closePath: () => undefined,
  createLinearGradient: () => ({ addColorStop: () => undefined }),
  createPattern: () => null,
  createRadialGradient: () => ({ addColorStop: () => undefined }),
  drawImage: () => undefined,
  fill: () => undefined,
  fillRect: () => undefined,
  fillText: () => undefined,
  getImageData: () => ({ data: new Uint8ClampedArray(4) }),
  lineTo: () => undefined,
  measureText: (text: string) => ({ width: text.length * 7 }),
  moveTo: () => undefined,
  putImageData: () => undefined,
  rect: () => undefined,
  restore: () => undefined,
  rotate: () => undefined,
  save: () => undefined,
  scale: () => undefined,
  setLineDash: () => undefined,
  setTransform: () => undefined,
  stroke: () => undefined,
  strokeRect: () => undefined,
  strokeText: () => undefined,
  transform: () => undefined,
  translate: () => undefined,
};

Object.defineProperty(HTMLCanvasElement.prototype, "getContext", {
  configurable: true,
  value: () => canvasContext,
});

class ResizeObserverMock {
  observe() {}
  unobserve() {}
  disconnect() {}
}

if (!globalThis.ResizeObserver) {
  globalThis.ResizeObserver = ResizeObserverMock as typeof ResizeObserver;
}
