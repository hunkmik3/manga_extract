import { create } from "zustand";
import { listBoards, createBoard, patchBoard, deleteBoard, type Board } from "../api/client";

/**
 * Flow Studio projects — fully separate from Manga boards (they're boards with
 * `kind="flow"`). Each project is its own image library; flowStudio scopes its
 * assets to the active project's id (source_board_id). Independent active id +
 * persistence so switching apps doesn't cross-contaminate.
 */
const ACTIVE_KEY = "flowboard.flowProjectId";

function loadActive(): number | null {
  try {
    const raw = localStorage.getItem(ACTIVE_KEY);
    const n = raw ? parseInt(raw, 10) : NaN;
    return Number.isFinite(n) && n > 0 ? n : null;
  } catch {
    return null;
  }
}
function persistActive(id: number | null): void {
  try {
    if (id === null) localStorage.removeItem(ACTIVE_KEY);
    else localStorage.setItem(ACTIVE_KEY, String(id));
  } catch {
    /* non-fatal */
  }
}

interface FlowProjectsState {
  projects: Board[];
  activeId: number | null;
  loading: boolean;
  error: string | null;
  load(): Promise<void>;
  switchTo(id: number): void;
  create(name: string): Promise<number | null>;
  rename(id: number, name: string): Promise<void>;
  remove(id: number): Promise<void>;
}

export const useFlowProjectsStore = create<FlowProjectsState>((set, get) => ({
  projects: [],
  activeId: null,
  loading: false,
  error: null,

  async load() {
    set({ loading: true });
    try {
      let projects = await listBoards("flow");
      if (projects.length === 0) {
        // First run — every team member who opens the link gets a starter
        // project so the studio isn't empty/unusable.
        const p = await createBoard("Project 1", "flow");
        projects = [p];
      }
      const persisted = loadActive();
      const active = projects.find((p) => p.id === persisted) ?? projects[0];
      set({ projects, activeId: active?.id ?? null, loading: false });
      persistActive(active?.id ?? null);
    } catch (err) {
      set({ loading: false, error: err instanceof Error ? err.message : String(err) });
    }
  },

  switchTo(id) {
    if (id === get().activeId) return;
    set({ activeId: id });
    persistActive(id);
  },

  async create(name) {
    try {
      const p = await createBoard(name || "Untitled", "flow");
      set((s) => ({ projects: [p, ...s.projects], activeId: p.id }));
      persistActive(p.id ?? null);
      return p.id;
    } catch (err) {
      set({ error: err instanceof Error ? err.message : String(err) });
      return null;
    }
  },

  async rename(id, name) {
    try {
      const updated = await patchBoard(id, name);
      set((s) => ({ projects: s.projects.map((p) => (p.id === id ? { ...p, name: updated.name } : p)) }));
    } catch {
      /* keep local */
    }
  },

  async remove(id) {
    try {
      await deleteBoard(id);
    } catch (err) {
      set({ error: err instanceof Error ? err.message : String(err) });
      return;
    }
    const remaining = get().projects.filter((p) => p.id !== id);
    set({ projects: remaining });
    if (get().activeId === id) {
      if (remaining.length > 0) {
        get().switchTo(remaining[0].id!);
      } else {
        await get().create("Project 1");
      }
    }
  },
}));
