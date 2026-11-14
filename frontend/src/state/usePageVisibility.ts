import { useEffect, useRef, useState } from "react";

/*
 * Page Visibility API. A hidden page (screen locked, other app in front) may be throttled or
 * frozen by the browser: timers, the socket, GPS and audio can stop without notice. On return
 * the caller resynchronises with the server instead of trusting what it held before.
 */

export type VisibilityState = {
  hidden: boolean;
  /** Last completed hidden period, for telling the user what may have been missed. */
  lastAway: { from: number; to: number } | null;
};

export function usePageVisibility(onVisible?: () => void): VisibilityState {
  const [state, setState] = useState<VisibilityState>({ hidden: document.hidden, lastAway: null });
  const hiddenSince = useRef<number | null>(document.hidden ? Date.now() : null);
  const callback = useRef(onVisible);
  callback.current = onVisible;

  useEffect(() => {
    const onChange = () => {
      if (document.hidden) {
        hiddenSince.current = Date.now();
        setState((current) => ({ ...current, hidden: true }));
        return;
      }
      const from = hiddenSince.current;
      hiddenSince.current = null;
      setState({ hidden: false, lastAway: from ? { from, to: Date.now() } : null });
      callback.current?.();
    };
    document.addEventListener("visibilitychange", onChange);
    // pageshow covers pages restored from the back/forward cache.
    const onPageShow = (event: PageTransitionEvent) => {
      if (event.persisted) callback.current?.();
    };
    window.addEventListener("pageshow", onPageShow);
    return () => {
      document.removeEventListener("visibilitychange", onChange);
      window.removeEventListener("pageshow", onPageShow);
    };
  }, []);

  return state;
}
