"use client";

import { useCallback, useEffect, useLayoutEffect, useRef } from "react";

// useLayoutEffect runs before paint (required for FLIP's Invert step to avoid a
// flash), but warns during SSR. Use the isomorphic variant so server renders
// fall back to useEffect silently.
export const useIsomorphicLayoutEffect =
  typeof window !== "undefined" ? useLayoutEffect : useEffect;

export interface CardExpansionApi<T extends HTMLElement> {
  /** Attach to each card: ref={(el) => { cardRefs.current[id] = el }}. */
  cardRefs: React.MutableRefObject<Record<string, T | null>>;
  /** Call in the click/close handler BEFORE the setState that triggers reflow (First). */
  snapshot: () => void;
  /** Call in a layout effect after the reflow — animates every moved card to its new slot (Invert + Play). */
  flip: () => void;
}

const DURATION = 320;
const EASE = "cubic-bezier(0.2, 0.85, 0.25, 1)";

/**
 * FLIP-based reflow animation for a grid of cards.
 *
 * When one card expands (spanning more grid tracks), CSS reflows every other
 * card instantly. CSS cannot transition that reflow, so this hook smooths it:
 * `snapshot()` records each card's pre-change position; after the DOM reflows,
 * `flip()` translates each moved card back to its old spot then transitions it
 * to identity, making the reflow appear to slide.
 *
 * Only position changes are tweened (via `transform: translate`, compositor-only
 * and non-distorting). The expanding card's own size change is handled by CSS
 * (it snaps to its new tracks; its content fades in).
 */
export function useCardExpansion<T extends HTMLElement = HTMLElement>(): CardExpansionApi<T> {
  const cardRefs = useRef<Record<string, T | null>>({});
  const firstRects = useRef<Map<string, { left: number; top: number }>>(new Map());

  const snapshot = useCallback(() => {
    const map = new Map<string, { left: number; top: number }>();
    for (const [id, el] of Object.entries(cardRefs.current)) {
      if (!el) continue;
      const r = el.getBoundingClientRect();
      map.set(id, { left: r.left, top: r.top });
    }
    firstRects.current = map;
  }, []);

  const flip = useCallback(() => {
    if (typeof window === "undefined") return;

    const first = firstRects.current;
    firstRects.current = new Map();
    if (first.size === 0) return; // nothing was snapshotted (e.g. initial mount)

    const reduced =
      typeof window.matchMedia === "function" &&
      window.matchMedia("(prefers-reduced-motion: reduce)").matches;

    const movers: { el: HTMLElement; dx: number; dy: number }[] = [];
    for (const [id, el] of Object.entries(cardRefs.current)) {
      if (!el) continue;
      const f = first.get(id);
      if (!f) continue; // card wasn't present at snapshot time — let it appear
      const l = el.getBoundingClientRect();
      const dx = f.left - l.left;
      const dy = f.top - l.top;
      if (dx === 0 && dy === 0) continue; // didn't move (includes the expanding card when its corner stays put)
      movers.push({ el, dx, dy });
    }

    if (movers.length === 0 || reduced) return;

    // Invert: visually place each mover at its old slot.
    for (const m of movers) {
      m.el.style.transition = "none";
      m.el.style.transform = `translate(${m.dx}px, ${m.dy}px)`;
    }

    // Play: next frame, transition to identity so they slide into the new layout.
    requestAnimationFrame(() => {
      for (const m of movers) {
        m.el.style.transition = `transform ${DURATION}ms ${EASE}`;
        m.el.style.transform = "";
      }
    });

    // Clear inline transition once settled so it never blocks future changes.
    window.setTimeout(() => {
      for (const m of movers) {
        m.el.style.transition = "";
      }
    }, DURATION + 60);
  }, []);

  return { cardRefs, snapshot, flip };
}
