import { create } from "zustand";
import {
  createRequest,
  getRequest,
  createReference,
  listReferences,
  patchReference,
  deleteReference,
  uploadComicSheet,
  type ReferenceItem,
} from "../api/client";
import { useFlowProjectsStore } from "./flowProjects";

/** The active Flow project (a `kind="flow"` board). Flow assets are scoped to
 *  it via source_board_id — separate from Manga's boards. */
function currentBoardId(): number | null {
  return useFlowProjectsStore.getState().activeId;
}

/**
 * Google-Flow-style image studio state (three-pane UI).
 *
 * Backend reuse (no new tables):
 *   - Generation rides the generic `/api/requests` queue with the
 *     `flow_gen_image` worker handler (Gemini image API, no extension).
 *   - Every generated / edited / uploaded image is persisted as a Reference
 *     row (kind="image") tagged `flow` so it survives reload and lists in the
 *     grid. References are idempotent on media_id.
 *   - "Characters" (Nhân vật) and "Scenes" (Cảnh) are NOT separate rows —
 *     the same image assets carry a `char:<Name>` / `scene:<Name>` tag, so
 *     "one character = many views" falls out for free and `@Name` in a prompt
 *     resolves to all media_ids tagged for that character/scene.
 */

export const FLOW_TAG = "flow";
export const CHAR_PREFIX = "char:";
export const SCENE_PREFIX = "scene:";
// Reference media ids used to generate this image, stored as tags so "reuse
// prompt" can re-attach + re-tag the same images (not just the text).
export const REF_PREFIX = "ref:";

export const FLOW_ASPECTS = ["16:9", "4:3", "1:1", "3:4", "9:16"] as const;
export type FlowAspect = (typeof FLOW_ASPECTS)[number];

export const FLOW_SIZES = ["1K", "2K", "4K"] as const;
export type FlowSize = (typeof FLOW_SIZES)[number];

// Image engine behind the studio. "gemini" = direct Gemini API key (refs/edit
// work locally, sent inline). "atrium" = Atrium passthrough (refs/edit need a
// public media URL / tunnel; plain text→image works anywhere).
export type FlowProvider = "gemini" | "atrium";
export const FLOW_PROVIDERS: { id: FlowProvider; label: string }[] = [
  { id: "gemini", label: "Gemini trực tiếp" },
  { id: "atrium", label: "Atrium" },
];

export interface FlowModelOption {
  id: string;
  label: string;
  max: FlowSize; // largest resolution this model supports
}
export const FLOW_MODELS: FlowModelOption[] = [
  { id: "gemini-3.1-flash-image", label: "Nano Banana 2", max: "4K" },
  { id: "gemini-3-pro-image", label: "Nano Banana Pro", max: "4K" },
  { id: "gemini-2.5-flash-image", label: "Nano Banana", max: "2K" },
];

export function modelMaxSize(modelId: string): FlowSize {
  return FLOW_MODELS.find((m) => m.id === modelId)?.max ?? "2K";
}

export type FlowTab = "all" | "characters" | "scenes";

/** A @token pill in the composer prompt: text + the media to preview + the
 *  reference media ids to drop when the token is removed. */
export interface Mention {
  token: string; // e.g. "@Aria"
  cover: string; // media id to preview when clicked
  mediaIds: string[]; // refs to drop when the token is deleted
}

export interface FlowAsset {
  refId: number;
  mediaId: string;
  label: string;
  prompt: string | null;
  aspectRatio: string | null;
  tags: string[];
  pinned: boolean;
  createdAt: string;
}

interface FlowGenSettings {
  aspect: FlowAspect;
  count: number; // 1-4
  model: string;
  size: FlowSize;
  provider: FlowProvider;
}

interface FlowStudioState {
  assets: FlowAsset[];
  loading: boolean;
  generating: boolean;
  tab: FlowTab;
  error: string | null;
  notice: string | null; // non-error info (e.g. engine auto-fell-back to Gemini)
  genProgress: { done: number; total: number } | null; // live "k/N" while generating
  selectedMediaId: string | null; // drives the inspector detail pane
  composerRefs: string[]; // media ids attached as references for the next gen
  composerPrompt: string; // shared prompt text (composer input + history fill)
  composerMentions: Mention[]; // @token pills in the prompt (shared so cards can tag too)
  recentPrompts: string[]; // lightweight "history" in the composer
  settings: FlowGenSettings;

  load(): Promise<void>;
  setTab(tab: FlowTab): void;
  setSettings(patch: Partial<FlowGenSettings>): void;
  setComposerPrompt(value: string): void;
  // Attach an asset as a reference AND append its @token to the prompt (the
  // card "add to prompt" button + the @ picker both feed this concept).
  tagToPrompt(name: string, cover: string, mediaIds: string[]): void;
  addComposerMention(m: Mention): void;
  removeComposerMention(token: string): void;
  // Reuse a past prompt: load its text AND re-attach + re-tag the images it
  // referenced (from the asset's ref: tags), so @tokens light up again.
  reusePrompt(prompt: string, refMediaIds: string[]): void;
  generate(prompt: string): Promise<void>;
  regenerate(mediaId: string): Promise<void>;
  refine(mediaId: string, prompt: string, refs?: string[]): Promise<void>;
  uploadAsset(file: File): Promise<void>;
  setCharacter(mediaId: string, name: string | null): Promise<void>;
  setScene(mediaId: string, name: string | null): Promise<void>;
  togglePin(refId: number): Promise<void>;
  remove(refId: number): Promise<void>;
  addRef(mediaId: string): void;
  removeRef(mediaId: string): void;
  clearRefs(): void;
  select(mediaId: string | null): void;
  clearError(): void;
  clearNotice(): void;
  resolveMentions(prompt: string): string[];
}

// ── persistence ─────────────────────────────────────────────────────────────
const SETTINGS_KEY = "flowboard.flowStudio.v2";

function loadPersisted(): { settings: FlowGenSettings; recentPrompts: string[] } {
  const fallback: FlowGenSettings = {
    aspect: "16:9",
    count: 2,
    model: "gemini-2.5-flash-image", // by id, not index — cheapest default, order-independent
    size: "1K",
    provider: "gemini",
  };
  try {
    const raw = localStorage.getItem(SETTINGS_KEY);
    if (!raw) return { settings: fallback, recentPrompts: [] };
    const p = JSON.parse(raw) as { settings?: Partial<FlowGenSettings>; recentPrompts?: string[] };
    const s = p.settings ?? {};
    return {
      settings: {
        aspect: (FLOW_ASPECTS as readonly string[]).includes(s.aspect ?? "")
          ? (s.aspect as FlowAspect)
          : fallback.aspect,
        count: s.count && s.count >= 1 && s.count <= 2 ? s.count : fallback.count,
        model: FLOW_MODELS.some((m) => m.id === s.model) ? (s.model as string) : fallback.model,
        size: (FLOW_SIZES as readonly string[]).includes(s.size ?? "") ? (s.size as FlowSize) : fallback.size,
        provider: s.provider === "atrium" ? "atrium" : "gemini",
      },
      recentPrompts: Array.isArray(p.recentPrompts) ? p.recentPrompts.slice(0, 8) : [],
    };
  } catch {
    return { settings: fallback, recentPrompts: [] };
  }
}

function persist(settings: FlowGenSettings, recentPrompts: string[]): void {
  try {
    localStorage.setItem(SETTINGS_KEY, JSON.stringify({ settings, recentPrompts }));
  } catch {
    // non-fatal
  }
}

// ── helpers ───────────────────────────────────────────────────────────────
function toAsset(ref: ReferenceItem): FlowAsset {
  return {
    refId: ref.id,
    mediaId: ref.mediaId,
    label: ref.label,
    prompt: ref.aiBrief,
    aspectRatio: ref.aspectRatio,
    tags: ref.tags ?? [],
    pinned: ref.pinned,
    createdAt: ref.createdAt,
  };
}

function withGroupTag(tags: string[], prefix: string, name: string | null): string[] {
  const kept = tags.filter((t) => !t.startsWith(prefix));
  const clean = name?.trim();
  return clean ? [...kept, prefix + clean] : kept;
}

export function groupName(tags: string[], prefix: string): string | null {
  const hit = tags.find((t) => t.startsWith(prefix));
  return hit ? hit.slice(prefix.length) : null;
}

export function deriveGroups(
  assets: FlowAsset[],
  prefix: string,
): { name: string; cover: string; count: number }[] {
  const map = new Map<string, { cover: string; count: number }>();
  for (const a of assets) {
    const name = groupName(a.tags, prefix);
    if (!name) continue;
    const cur = map.get(name);
    if (cur) cur.count += 1;
    else map.set(name, { cover: a.mediaId, count: 1 });
  }
  return [...map.entries()]
    .map(([name, v]) => ({ name, cover: v.cover, count: v.count }))
    .sort((a, b) => a.name.localeCompare(b.name));
}

const sleep = (ms: number) => new Promise<void>((r) => setTimeout(r, ms));

/** Build the gen params, honouring model resolution caps (4K is Pro-only). */
function genParams(
  prompt: string,
  settings: FlowGenSettings,
  refs: string[],
  extra: Record<string, unknown> = {},
): Record<string, unknown> {
  const params: Record<string, unknown> = {
    prompt,
    aspect_ratio: settings.aspect,
    image_model: settings.model,
    variant_count: settings.count,
    provider: settings.provider,
    ...extra,
  };
  // Only send a non-default imageSize the chosen model actually supports.
  const cap = modelMaxSize(settings.model);
  const wanted = settings.size;
  if (wanted !== "1K" && (cap === "4K" || (cap === "2K" && wanted === "2K"))) {
    params.image_size = wanted;
  }
  if (refs.length) params.ref_media_ids = refs;
  return params;
}

async function dispatchFlow(
  params: Record<string, unknown>,
  onProgress?: (done: number, total: number) => void,
): Promise<{ mediaIds: string[]; providerUsed: string | null }> {
  const req = await createRequest({ type: "flow_gen_image", params });
  let row = req;
  for (let i = 0; i < 240 && (row.status === "queued" || row.status === "running"); i++) {
    await sleep(1500);
    row = await getRequest(req.id);
    // While running, the backend stamps {progress:{done,total}} after each
    // variant lands — surface it for the live "k/N · pct%" placeholder.
    const p = (row.result as { progress?: { done?: number; total?: number } } | undefined)?.progress;
    if (p && typeof p.done === "number" && typeof p.total === "number") {
      onProgress?.(p.done, p.total);
    }
  }
  if (row.status !== "done") {
    throw new Error(row.error || `generation ${row.status}`);
  }
  const mediaIds = ((row.result["media_ids"] as (string | null)[] | undefined) ?? []).filter(
    (m): m is string => typeof m === "string" && m.length > 0,
  );
  const providerUsed = (row.result["provider_used"] as string | undefined) ?? null;
  return { mediaIds, providerUsed };
}

// Notice shown when the backend silently swapped engines (Atrium can't read
// local media without a tunnel, so refs/edit run on Gemini instead).
function fallbackNotice(requested: string, used: string | null): string | null {
  if (used && used !== requested) {
    return "Tham chiếu/tinh chỉnh đã chạy bằng Gemini (Atrium cần tunnel PUBLIC_MEDIA_BASE_URL để đọc ảnh local).";
  }
  return null;
}

const initial = loadPersisted();

export const useFlowStudioStore = create<FlowStudioState>((set, get) => ({
  assets: [],
  loading: false,
  generating: false,
  tab: "all",
  error: null,
  notice: null,
  genProgress: null,
  selectedMediaId: null,
  composerRefs: [],
  composerPrompt: "",
  composerMentions: [],
  recentPrompts: initial.recentPrompts,
  settings: initial.settings,

  async load() {
    const boardId = currentBoardId();
    set({ loading: true });
    if (boardId === null) {
      set({ assets: [], loading: false });
      return;
    }
    try {
      const refs = await listReferences({ limit: 500, source_board_id: boardId });
      const assets = refs.filter((r) => (r.tags ?? []).includes(FLOW_TAG)).map(toAsset);
      set({ assets, loading: false });
    } catch (err) {
      set({ loading: false, error: err instanceof Error ? err.message : "failed to load assets" });
    }
  },

  setTab(tab) {
    set({ tab });
  },

  tagToPrompt(name, cover, mediaIds) {
    mediaIds.forEach((id) => get().addRef(id));
    const token = `@${name}`;
    const p = get().composerPrompt;
    const sep = p.length === 0 || /\s$/.test(p) ? "" : " ";
    set({ composerPrompt: `${p}${sep}${token} ` });
    get().addComposerMention({ token, cover, mediaIds });
  },

  addComposerMention(m) {
    set((s) =>
      s.composerMentions.some((x) => x.token === m.token) ? {} : { composerMentions: [...s.composerMentions, m] },
    );
  },

  removeComposerMention(token) {
    set((s) => ({ composerMentions: s.composerMentions.filter((x) => x.token !== token) }));
  },

  reusePrompt(prompt, refMediaIds) {
    const assets = get().assets;
    const nameOf = (a: FlowAsset) =>
      groupName(a.tags, CHAR_PREFIX) || groupName(a.tags, SCENE_PREFIX) || a.label || "ảnh";
    const ids = new Set<string>();
    // 1. Exact: the refs stored with the image when it was generated.
    for (const id of refMediaIds) if (assets.some((a) => a.mediaId === id)) ids.add(id);
    // 2. Fallback (also covers images made before refs were stored): re-resolve
    //    the @tokens in the text against the names/labels of existing images.
    for (const a of assets) {
      const n = nameOf(a);
      if (n && n !== "ảnh" && prompt.includes(`@${n}`)) ids.add(a.mediaId);
    }
    // Rebuild the @token pills, grouping refs under their derived token (a
    // character/scene → all its views; otherwise the image's own label) so the
    // tokens match those baked into the prompt text and light up again.
    const byToken = new Map<string, { cover: string; mediaIds: string[] }>();
    for (const id of ids) {
      const a = assets.find((x) => x.mediaId === id)!;
      const token = `@${nameOf(a)}`;
      const cur = byToken.get(token);
      if (cur) cur.mediaIds.push(id);
      else byToken.set(token, { cover: id, mediaIds: [id] });
    }
    set({
      composerPrompt: prompt,
      composerRefs: [...ids],
      composerMentions: [...byToken.entries()].map(([token, v]) => ({
        token,
        cover: v.cover,
        mediaIds: v.mediaIds,
      })),
    });
  },

  setComposerPrompt(value) {
    set({ composerPrompt: value });
  },

  setSettings(patch) {
    const next = { ...get().settings, ...patch };
    // Clamp size to the model's cap when switching to a weaker model.
    const cap = modelMaxSize(next.model);
    if (cap === "2K" && next.size === "4K") next.size = "2K";
    set({ settings: next });
    persist(next, get().recentPrompts);
  },

  resolveMentions(prompt) {
    const tokens = [...prompt.matchAll(/@([A-Za-z0-9_]+)/g)].map((m) => m[1].toLowerCase());
    if (tokens.length === 0) return [];
    const norm = (s: string) => s.toLowerCase().replace(/\s+/g, "");
    const ids: string[] = [];
    for (const a of get().assets) {
      const cn = groupName(a.tags, CHAR_PREFIX);
      const sn = groupName(a.tags, SCENE_PREFIX);
      if ((cn && tokens.includes(norm(cn))) || (sn && tokens.includes(norm(sn)))) {
        ids.push(a.mediaId);
      }
    }
    return [...new Set(ids)];
  },

  async generate(prompt) {
    const text = prompt.trim();
    if (!text || get().generating) return;
    const settings = get().settings;
    set({ generating: true, error: null, genProgress: { done: 0, total: settings.count } });
    const refs = [...new Set([...get().composerRefs, ...get().resolveMentions(text)])];
    // Sent → clear the composer (prompt + attached refs) like Flow does; the
    // in-flight generation already captured `text` and `refs`.
    set({ composerPrompt: "", composerRefs: [], composerMentions: [] });
    try {
      const { mediaIds, providerUsed } = await dispatchFlow(
        genParams(text, settings, refs),
        (done, total) => set({ genProgress: { done, total } }),
      );
      const created = await persistGenerated(mediaIds, text, settings.aspect, refs);
      set((s) => ({
        assets: [...created, ...s.assets],
        selectedMediaId: created[0]?.mediaId ?? s.selectedMediaId,
        recentPrompts: pushRecent(s.recentPrompts, text),
        notice: fallbackNotice(settings.provider, providerUsed),
      }));
      persist(get().settings, get().recentPrompts);
    } catch (err) {
      set({ error: err instanceof Error ? err.message : "generation failed" });
    } finally {
      set({ generating: false, genProgress: null });
    }
  },

  async regenerate(mediaId) {
    const asset = get().assets.find((a) => a.mediaId === mediaId);
    if (!asset?.prompt) return;
    await get().generate(asset.prompt);
  },

  async refine(mediaId, prompt, refs) {
    const text = prompt.trim();
    if (!text || get().generating) return;
    set({ generating: true, error: null, genProgress: { done: 0, total: 1 } });
    const settings = get().settings;
    try {
      const { mediaIds, providerUsed } = await dispatchFlow(
        genParams(text, { ...settings, count: 1 }, refs ?? [], { source_media_id: mediaId }),
        (done, total) => set({ genProgress: { done, total } }),
      );
      const created = await persistGenerated(mediaIds, text, null, [mediaId, ...(refs ?? [])]);
      set((s) => ({
        assets: [...created, ...s.assets],
        selectedMediaId: created[0]?.mediaId ?? s.selectedMediaId,
        recentPrompts: pushRecent(s.recentPrompts, text),
        notice: fallbackNotice(settings.provider, providerUsed),
      }));
      persist(get().settings, get().recentPrompts);
    } catch (err) {
      set({ error: err instanceof Error ? err.message : "refine failed" });
    } finally {
      set({ generating: false, genProgress: null });
    }
  },

  async uploadAsset(file) {
    set({ error: null });
    try {
      const { media_id } = await uploadComicSheet(file);
      const ref = await createReference({
        media_id,
        kind: "image",
        label: file.name.slice(0, 60) || "upload",
        tags: [FLOW_TAG],
        source_board_id: currentBoardId() ?? undefined,
      });
      const asset = toAsset(ref);
      // Just drop it into the grid — don't auto-open the detail viewer.
      set((s) => ({
        assets: [asset, ...s.assets.filter((a) => a.refId !== asset.refId)],
      }));
    } catch (err) {
      set({ error: err instanceof Error ? err.message : "upload failed" });
    }
  },

  async setCharacter(mediaId, name) {
    const asset = get().assets.find((a) => a.mediaId === mediaId);
    if (!asset) return;
    try {
      const ref = await patchReference(asset.refId, {
        tags: withGroupTag(asset.tags, CHAR_PREFIX, name),
      });
      set((s) => ({ assets: s.assets.map((a) => (a.refId === asset.refId ? toAsset(ref) : a)) }));
    } catch (err) {
      set({ error: err instanceof Error ? err.message : "failed to tag character" });
    }
  },

  async setScene(mediaId, name) {
    const asset = get().assets.find((a) => a.mediaId === mediaId);
    if (!asset) return;
    try {
      const ref = await patchReference(asset.refId, {
        tags: withGroupTag(asset.tags, SCENE_PREFIX, name),
      });
      set((s) => ({ assets: s.assets.map((a) => (a.refId === asset.refId ? toAsset(ref) : a)) }));
    } catch (err) {
      set({ error: err instanceof Error ? err.message : "failed to tag scene" });
    }
  },

  async togglePin(refId) {
    const asset = get().assets.find((a) => a.refId === refId);
    if (!asset) return;
    try {
      const ref = await patchReference(refId, { pinned: !asset.pinned });
      set((s) => ({ assets: s.assets.map((a) => (a.refId === refId ? toAsset(ref) : a)) }));
    } catch (err) {
      set({ error: err instanceof Error ? err.message : "failed to pin" });
    }
  },

  async remove(refId) {
    try {
      await deleteReference(refId);
      set((s) => {
        const gone = s.assets.find((a) => a.refId === refId);
        return {
          assets: s.assets.filter((a) => a.refId !== refId),
          composerRefs: gone ? s.composerRefs.filter((m) => m !== gone.mediaId) : s.composerRefs,
          selectedMediaId:
            gone && s.selectedMediaId === gone.mediaId ? null : s.selectedMediaId,
        };
      });
    } catch (err) {
      set({ error: err instanceof Error ? err.message : "failed to delete" });
    }
  },

  addRef(mediaId) {
    set((s) =>
      s.composerRefs.includes(mediaId)
        ? s
        : { composerRefs: [...s.composerRefs, mediaId].slice(0, 6) },
    );
  },
  removeRef(mediaId) {
    set((s) => ({ composerRefs: s.composerRefs.filter((m) => m !== mediaId) }));
  },
  clearRefs() {
    set({ composerRefs: [] });
  },

  select(mediaId) {
    set({ selectedMediaId: mediaId });
  },
  clearError() {
    set({ error: null });
  },
  clearNotice() {
    set({ notice: null });
  },
}));

// ── module-level helpers (no `this`) ─────────────────────────────────────────
async function persistGenerated(
  mediaIds: string[],
  prompt: string,
  aspect: string | null,
  refs: string[] = [],
): Promise<FlowAsset[]> {
  const refTags = [...new Set(refs)].map((id) => REF_PREFIX + id);
  const created: FlowAsset[] = [];
  for (const mid of mediaIds) {
    try {
      const ref = await createReference({
        media_id: mid,
        kind: "image",
        label: prompt.slice(0, 60),
        ai_brief: prompt,
        aspect_ratio: aspect,
        tags: [FLOW_TAG, ...refTags],
        source_board_id: currentBoardId() ?? undefined,
      });
      created.push(toAsset(ref));
    } catch {
      // non-fatal: the image is still generated & cached, just not persisted
    }
  }
  return created;
}

function pushRecent(recent: string[], prompt: string): string[] {
  return [prompt, ...recent.filter((p) => p !== prompt)].slice(0, 8);
}
