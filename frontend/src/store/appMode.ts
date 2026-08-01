import { create } from "zustand";

/**
 * Top-level branch selector, driven by the URL so the two surfaces live at
 * different links and are fully independent:
 *
 *   "/"      → Manga Extract (node board)
 *   "/flow"  → Flow Studio (image studio)
 *
 * The URL is the source of truth (refreshing /flow stays on Flow). setMode
 * pushes a new path; the popstate listener in App.tsx keeps state in sync with
 * back/forward navigation.
 */
export type AppMode = "manga" | "flow" | "bubble";

/**
 * Flow-only deployment flag. Set `VITE_FLOW_ONLY=1` at build time (the team
 * deploy) and the whole app becomes Flow Studio: `/` serves Flow and the
 * "↤ Manga Extract" link is hidden. One explicit SIDE DOOR stays open even in
 * this mode: `/manga_extract` serves the Manga board (shared by URL only —
 * nothing in the Flow UI links to it). Bubble stays off. Unset (normal dev)
 * keeps all branches at `/`, `/flow` and `/bubble`.
 */
export const FLOW_ONLY = import.meta.env.VITE_FLOW_ONLY === "1";

function modeFromPath(): AppMode {
  try {
    // Side door — works in flow-only deploys too (checked before the flag).
    if (window.location.pathname.startsWith("/manga_extract")) return "manga";
  } catch {
    /* fall through */
  }
  if (FLOW_ONLY) return "flow";
  try {
    const p = window.location.pathname;
    if (p.startsWith("/flow")) return "flow";
    if (p.startsWith("/bubble")) return "bubble";
    return "manga";
  } catch {
    return "manga";
  }
}

function pushPath(mode: AppMode): void {
  try {
    let path: string;
    if (FLOW_ONLY) {
      // Flow owns every path except the /manga_extract side door; bubble is off.
      if (mode === "bubble") return;
      path = mode === "manga" ? "/manga_extract" : "/";
    } else {
      path = mode === "flow" ? "/flow" : mode === "bubble" ? "/bubble" : "/";
    }
    if (window.location.pathname !== path) window.history.pushState({}, "", path);
  } catch {
    /* non-fatal */
  }
}

interface AppModeState {
  mode: AppMode;
  setMode(mode: AppMode): void; // user action → navigate (push URL)
  syncFromUrl(): void; // popstate → reflect URL without pushing
}

export const useAppModeStore = create<AppModeState>((set) => ({
  mode: modeFromPath(),
  setMode(mode) {
    pushPath(mode);
    set({ mode });
  },
  syncFromUrl() {
    set({ mode: modeFromPath() });
  },
}));
