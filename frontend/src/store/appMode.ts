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
export type AppMode = "manga" | "flow" | "bubble" | "colorize" | "edit";

/**
 * Flow-only deployment flag. Set `VITE_FLOW_ONLY=1` at build time (the team
 * deploy) and the whole app becomes Flow Studio: `/` serves Flow, the Manga
 * board is never reachable, and the "↤ Manga Extract" link is hidden. Unset
 * (normal dev) keeps both branches at `/` and `/flow`.
 */
export const FLOW_ONLY = import.meta.env.VITE_FLOW_ONLY === "1";

function modeFromPath(): AppMode {
  if (FLOW_ONLY) return "flow";
  try {
    const p = window.location.pathname;
    if (p.startsWith("/flow")) return "flow";
    if (p.startsWith("/bubble")) return "bubble";
    if (p.startsWith("/colorize")) return "colorize";
    if (p.startsWith("/edit")) return "edit";
    return "manga";
  } catch {
    return "manga";
  }
}

function pushPath(mode: AppMode): void {
  if (FLOW_ONLY) return; // single surface — never rewrite the URL
  try {
    const path =
      mode === "flow" ? "/flow" : mode === "bubble" ? "/bubble" : mode === "colorize" ? "/colorize" : mode === "edit" ? "/edit" : "/";
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
