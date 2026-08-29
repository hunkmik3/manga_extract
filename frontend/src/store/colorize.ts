import { create } from "zustand";
import {
  listColorizeChapters,
  createColorizeChapter,
  getColorizeChapter,
  patchColorizeChapter,
  deleteColorizeChapter,
  createRequest,
  getRequest,
  uploadColorizePage,
  selectColorizeVariant,
  type ColorizeChapterSummary,
  type ColorizeChapter,
} from "../api/client";

const sleep = (ms: number) => new Promise<void>((r) => setTimeout(r, ms));

/** Poll a queued request to a terminal state; returns its result dict. */
async function pollRequest(id: number): Promise<Record<string, unknown>> {
  let row = await getRequest(id);
  for (let i = 0; i < 500 && (row.status === "queued" || row.status === "running"); i++) {
    await sleep(i < 8 ? 800 : 2500);
    row = await getRequest(id);
  }
  if (row.status !== "done") throw new Error(row.error || `request ${row.status}`);
  return (row.result as Record<string, unknown>) ?? {};
}

export type PageState = "idle" | "running" | "done" | "error";

interface ColorizeState {
  chapters: ColorizeChapterSummary[];
  current: ColorizeChapter | null;
  loading: boolean;
  busy: string | null; // status line while a long op runs
  error: string | null;
  pageState: Record<number, PageState>;
  variantCount: number; // how many variants to generate per page (1-4)

  setVariantCount(n: number): void;
  selectVariant(pageMediaId: string, outputMediaId: string): Promise<void>;
  fixRegion(index: number, box: number[], useMask: boolean, prompt?: string): Promise<void>;
  load(): Promise<void>;
  newChapter(name: string): Promise<void>;
  open(id: number): Promise<void>;
  close(): void;
  remove(id: number): Promise<void>;
  uploadPages(files: File[]): Promise<void>;
  setStyleRef(file: File): Promise<void>;
  buildBible(): Promise<void>;
  buildSheets(): Promise<void>;
  saveBible(text: string): Promise<string | null>; // returns error string or null
  colorizePage(index: number): Promise<void>;
  colorizeAll(): Promise<void>;
  clearError(): void;
}

export const useColorizeStore = create<ColorizeState>((set, get) => ({
  chapters: [],
  current: null,
  loading: false,
  busy: null,
  error: null,
  pageState: {},
  variantCount: 1,

  setVariantCount(n) {
    set({ variantCount: Math.max(1, Math.min(4, Math.round(n) || 1)) });
  },

  async selectVariant(pageMediaId, outputMediaId) {
    const cur = get().current;
    if (!cur) return;
    try {
      set({ current: await selectColorizeVariant(cur.id, pageMediaId, outputMediaId) });
    } catch (e) {
      set({ error: `Select variant failed: ${(e as Error).message}` });
    }
  },

  async fixRegion(index, box, useMask, prompt) {
    const cur = get().current;
    if (!cur) return;
    set((st) => ({ pageState: { ...st.pageState, [index]: "running" }, error: null }));
    try {
      const req = await createRequest({
        type: "colorize_fix_region",
        params: { chapter_id: cur.id, page_index: index, box, use_mask: useMask, prompt: prompt || "" },
      });
      await pollRequest(req.id);
      const updated = await getColorizeChapter(cur.id);
      set((st) => ({ current: updated, pageState: { ...st.pageState, [index]: "done" } }));
    } catch (e) {
      set((st) => ({ pageState: { ...st.pageState, [index]: "error" }, error: `Fix failed: ${(e as Error).message}` }));
    }
  },

  async load() {
    set({ loading: true, error: null });
    try {
      set({ chapters: await listColorizeChapters() });
    } catch (e) {
      set({ error: (e as Error).message });
    } finally {
      set({ loading: false });
    }
  },

  async newChapter(name) {
    try {
      const ch = await createColorizeChapter({ name: name || "Untitled chapter" });
      await get().load();
      await get().open(ch.id);
    } catch (e) {
      set({ error: (e as Error).message });
    }
  },

  async open(id) {
    set({ loading: true, error: null });
    try {
      set({ current: await getColorizeChapter(id), pageState: {} });
    } catch (e) {
      set({ error: (e as Error).message });
    } finally {
      set({ loading: false });
    }
  },

  close() {
    set({ current: null, pageState: {} });
    get().load();
  },

  async remove(id) {
    try {
      await deleteColorizeChapter(id);
      if (get().current?.id === id) set({ current: null });
      await get().load();
    } catch (e) {
      set({ error: (e as Error).message });
    }
  },

  async uploadPages(files) {
    const cur = get().current;
    if (!cur || files.length === 0) return;
    set({ busy: `Uploading 0/${files.length} pages…`, error: null });
    try {
      // Sort by filename so reading order is deterministic (folder order).
      const sorted = [...files].sort((a, b) => a.name.localeCompare(b.name, undefined, { numeric: true }));
      const ids: string[] = [...cur.page_media_ids];
      for (let i = 0; i < sorted.length; i++) {
        const up = await uploadColorizePage(sorted[i]);
        ids.push(up.media_id);
        set({ busy: `Uploading ${i + 1}/${sorted.length} pages…` });
      }
      const updated = await patchColorizeChapter(cur.id, { page_media_ids: ids });
      set({ current: updated });
    } catch (e) {
      set({ error: (e as Error).message });
    } finally {
      set({ busy: null });
    }
  },

  async setStyleRef(file) {
    const cur = get().current;
    if (!cur) return;
    set({ busy: "Uploading style reference…", error: null });
    try {
      const up = await uploadColorizePage(file);
      set({ current: await patchColorizeChapter(cur.id, { style_ref_media_id: up.media_id }) });
    } catch (e) {
      set({ error: (e as Error).message });
    } finally {
      set({ busy: null });
    }
  },

  async buildBible() {
    const cur = get().current;
    if (!cur) return;
    set({ busy: "Reading chapter → building bible (this can take a minute)…", error: null });
    try {
      const req = await createRequest({ type: "colorize_build_bible", params: { chapter_id: cur.id } });
      await pollRequest(req.id);
      set({ current: await getColorizeChapter(cur.id) });
    } catch (e) {
      set({ error: `Build bible failed: ${(e as Error).message}` });
    } finally {
      set({ busy: null });
    }
  },

  async buildSheets() {
    const cur = get().current;
    if (!cur) return;
    set({ busy: "Building character sheets (one per outfit)…", error: null });
    try {
      const req = await createRequest({ type: "colorize_build_sheets", params: { chapter_id: cur.id } });
      await pollRequest(req.id);
      set({ current: await getColorizeChapter(cur.id) });
    } catch (e) {
      set({ error: `Build sheets failed: ${(e as Error).message}` });
    } finally {
      set({ busy: null });
    }
  },

  async saveBible(text) {
    const cur = get().current;
    if (!cur) return "no chapter";
    let parsed: Record<string, unknown>;
    try {
      parsed = JSON.parse(text);
    } catch (e) {
      return `Invalid JSON: ${(e as Error).message}`;
    }
    try {
      set({ current: await patchColorizeChapter(cur.id, { bible: parsed }) });
      return null;
    } catch (e) {
      return (e as Error).message;
    }
  },

  async colorizePage(index) {
    const cur = get().current;
    if (!cur) return;
    set((st) => ({ pageState: { ...st.pageState, [index]: "running" }, error: null }));
    try {
      const req = await createRequest({ type: "colorize_page", params: { chapter_id: cur.id, page_index: index, variant_count: get().variantCount } });
      await pollRequest(req.id);
      const updated = await getColorizeChapter(cur.id);
      set((st) => ({ current: updated, pageState: { ...st.pageState, [index]: "done" } }));
    } catch (e) {
      set((st) => ({ pageState: { ...st.pageState, [index]: "error" }, error: `Page ${index + 1}: ${(e as Error).message}` }));
    }
  },

  async colorizeAll() {
    const cur = get().current;
    if (!cur) return;
    // Sequential — keeps download concurrency low (avoids the S3 throttle) and
    // makes per-page consistency easy to debug.
    set({ busy: "Colorizing all pages…" });
    for (let i = 0; i < cur.page_media_ids.length; i++) {
      const done = get().current?.outputs?.[cur.page_media_ids[i]];
      if (done) continue; // skip already-colorized
      set({ busy: `Colorizing page ${i + 1}/${cur.page_media_ids.length}…` });
      await get().colorizePage(i);
    }
    set({ busy: null });
  },

  clearError() {
    set({ error: null });
  },
}));
