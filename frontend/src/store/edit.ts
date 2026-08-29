import { create } from "zustand";
import {
  createRequest,
  getRequest,
  uploadColorizePage,
  makeCutout,
  type Segment,
} from "../api/client";

const sleep = (ms: number) => new Promise<void>((r) => setTimeout(r, ms));

async function poll(id: number): Promise<Record<string, unknown>> {
  let row = await getRequest(id);
  for (let i = 0; i < 400 && (row.status === "queued" || row.status === "running"); i++) {
    await sleep(i < 8 ? 700 : 2000);
    row = await getRequest(id);
  }
  if (row.status !== "done") throw new Error(row.error || `request ${row.status}`);
  return (row.result as Record<string, unknown>) ?? {};
}

interface EditState {
  mediaId: string | null; // current (possibly edited) image
  history: string[]; // previous versions for undo
  segments: Segment[];
  selected: number | null; // selected segment id
  cutouts: Record<number, string>; // segment id → cutout media id
  busy: string | null;
  error: string | null;

  loadFile(file: File): Promise<void>;
  loadMedia(mediaId: string): void;
  detect(): Promise<void>;
  select(id: number | null): Promise<void>;
  edit(prompt: string): Promise<void>;
  undo(): void;
  reset(): void;
  clearError(): void;
}

export const useEditStore = create<EditState>((set, get) => ({
  mediaId: null,
  history: [],
  segments: [],
  selected: null,
  cutouts: {},
  busy: null,
  error: null,

  async loadFile(file) {
    set({ busy: "Uploading…", error: null });
    try {
      const up = await uploadColorizePage(file);
      set({ mediaId: up.media_id, history: [], segments: [], selected: null, cutouts: {} });
    } catch (e) {
      set({ error: (e as Error).message });
    } finally {
      set({ busy: null });
    }
  },

  loadMedia(mediaId) {
    set({ mediaId, history: [], segments: [], selected: null, cutouts: {} });
  },

  async detect() {
    const mid = get().mediaId;
    if (!mid) return;
    set({ busy: "Detecting parts…", error: null, segments: [], selected: null, cutouts: {} });
    try {
      const req = await createRequest({ type: "detect_objects", params: { media_id: mid } });
      const res = await poll(req.id);
      set({ segments: (res.segments as Segment[]) ?? [] });
    } catch (e) {
      set({ error: `Detect failed: ${(e as Error).message}` });
    } finally {
      set({ busy: null });
    }
  },

  async select(id) {
    set({ selected: id });
    if (id == null) return;
    const { cutouts, mediaId, segments } = get();
    if (cutouts[id] || !mediaId) return; // cached
    const seg = segments.find((s) => s.id === id);
    if (!seg) return;
    try {
      const { media_id } = await makeCutout(mediaId, seg.box);
      set((st) => ({ cutouts: { ...st.cutouts, [id]: media_id } }));
    } catch {
      /* cutout is best-effort */
    }
  },

  async edit(prompt) {
    const mid = get().mediaId;
    if (!mid || !prompt.trim()) return;
    set({ busy: "Editing with Grok…", error: null });
    try {
      const req = await createRequest({
        type: "flow_gen_image",
        params: {
          prompt: prompt.trim(),
          provider: "avis",
          image_model: "grok-imagine-image-quality",
          source_media_id: mid,
          variant_count: 1,
        },
      });
      const res = await poll(req.id);
      const outs = (res.media_ids as string[]) ?? [];
      if (outs[0]) {
        set((st) => ({
          history: [...st.history, mid],
          mediaId: outs[0],
          segments: [], // stale after an edit — re-detect on the new image
          selected: null,
          cutouts: {},
        }));
      }
    } catch (e) {
      set({ error: `Edit failed: ${(e as Error).message}` });
    } finally {
      set({ busy: null });
    }
  },

  undo() {
    const { history } = get();
    if (!history.length) return;
    const prev = history[history.length - 1];
    set({ mediaId: prev, history: history.slice(0, -1), segments: [], selected: null, cutouts: {} });
  },

  reset() {
    set({ mediaId: null, history: [], segments: [], selected: null, cutouts: {}, error: null, busy: null });
  },

  clearError() {
    set({ error: null });
  },
}));
