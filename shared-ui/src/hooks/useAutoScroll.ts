import { useCallback, useEffect, useRef, useState } from "react";

export function useAutoScroll(deps: unknown[] = []) {
  const containerRef = useRef<HTMLDivElement>(null);
  const [isNearBottom, setIsNearBottom] = useState(true);

  const checkNearBottom = useCallback(() => {
    const el = containerRef.current;
    if (!el) return true;
    return el.scrollHeight - el.scrollTop - el.clientHeight < 100;
  }, []);

  useEffect(() => {
    const el = containerRef.current;
    if (!el) return;

    const handler = () => setIsNearBottom(checkNearBottom());
    el.addEventListener("scroll", handler, { passive: true });
    return () => el.removeEventListener("scroll", handler);
  }, [checkNearBottom]);

  useEffect(() => {
    if (isNearBottom) {
      const el = containerRef.current;
      if (el) {
        el.scrollTop = el.scrollHeight;
      }
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [isNearBottom, ...deps]);

  const scrollToBottom = useCallback(() => {
    const el = containerRef.current;
    if (el) {
      el.scrollTo({ top: el.scrollHeight, behavior: "smooth" });
    }
  }, []);

  return {
    containerRef,
    isNearBottom,
    scrollToBottom,
    showScrollButton: !isNearBottom,
  };
}
