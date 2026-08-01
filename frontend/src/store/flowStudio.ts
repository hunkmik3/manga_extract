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

// Image engine behind the studio. Atrium passthrough is the only engine; the
// direct-Gemini option was retired. ("gemini" stays in the type only as the
// backend's internal fallback if Atrium can't read a referenced image.)
export type FlowProvider = "gemini" | "atrium" | "avis";
export const FLOW_PROVIDERS: { id: FlowProvider; label: string }[] = [
  { id: "atrium", label: "Atrium" },
  { id: "avis", label: "Avis" },
];

export interface FlowModelOption {
  id: string;
  label: string;
  max: FlowSize; // largest resolution this model supports
  provider: FlowProvider; // engine that runs this model (picked automatically)
}
export const FLOW_MODELS: FlowModelOption[] = [
  { id: "gemini-3.1-flash-image", label: "Nano Banana 2", max: "4K", provider: "atrium" },
  { id: "gemini-3-pro-image", label: "Nano Banana Pro", max: "4K", provider: "atrium" },
  { id: "gemini-2.5-flash-image", label: "Nano Banana", max: "2K", provider: "atrium" },
  // Seedream 5 caps at ~4.6M px (≈2K); it can't do true 4K, so max is 2K.
  { id: "dola-seedream-5-0-pro", label: "Seedream 5.0 Pro", max: "2K", provider: "avis" },
];

export function modelMaxSize(modelId: string): FlowSize {
  return FLOW_MODELS.find((m) => m.id === modelId)?.max ?? "2K";
}

/** Engine that runs a given model id (defaults to Atrium for unknown ids). */
export function modelProvider(modelId: string): FlowProvider {
  return FLOW_MODELS.find((m) => m.id === modelId)?.provider ?? "atrium";
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
  modelUsed: string | null;
  tags: string[];
  pinned: boolean;
  createdAt: string;
}

/** Human-readable label for a model id (falls back to the raw id for models
 *  the frontend doesn't know about yet — e.g. right after a backend update). */
export function modelLabel(modelId: string | null): string | null {
  if (!modelId) return null;
  return FLOW_MODELS.find((m) => m.id === modelId)?.label ?? modelId;
}

interface FlowGenSettings {
  aspect: FlowAspect;
  count: number; // 1-4
  model: string;
  size: FlowSize;
  provider: FlowProvider;
  // Ask the model to match the reference's palette (no colour grading). Only
  // sent when the gen actually has a reference/source image.
  preserveColors: boolean;
}

/** One in-flight generation batch. Several can run at once (no wait-for-finish);
 *  each tracks its own progress so the grid can show all pending tiles. */
export interface GenJob {
  id: number;
  requestId?: number; // backend request this tile is polling (used to re-attach after an F5)
  done: number;
  total: number;
  prompt: string; // exact prompt of this in-flight gen (for "reuse while generating")
  refs: string[]; // exact material/reference media ids used for this gen
}

interface FlowStudioState {
  assets: FlowAsset[];
  loading: boolean;
  generating: boolean; // = genJobs.length > 0 (kept in sync for convenience)
  genJobs: GenJob[]; // every generation currently in flight
  tab: FlowTab;
  error: string | null;
  notice: string | null; // non-error info (e.g. engine auto-fell-back to Gemini)
  selectedMediaId: string | null; // drives the inspector detail pane
  composerRefs: string[]; // media ids attached as references for the next gen
  composerPrompt: string; // shared prompt text (composer input + history fill)
  composerMentions: Mention[]; // @token pills in the prompt (shared so cards can tag too)
  // Last known caret position in the composer textarea. Tracked so a tag
  // triggered from OUTSIDE the textarea (a grid card's "@" button) inserts at
  // wherever the user last placed their cursor, not always at the end.
  composerCaret: number;
  // One-shot signal: after an external insert, FlowComposer moves the real DOM
  // caret here (React alone won't — changing a controlled textarea's value
  // from another component doesn't relocate the browser's native caret).
  pendingCaretApply: number | null;
  recentPrompts: string[]; // lightweight "history" in the composer
  settings: FlowGenSettings;

  load(): Promise<void>;
  setTab(tab: FlowTab): void;
  setSettings(patch: Partial<FlowGenSettings>): void;
  setComposerPrompt(value: string): void;
  setComposerCaret(pos: number): void;
  clearPendingCaretApply(): void;
  // Attach an asset as a reference AND insert its @token into the prompt AT
  // THE CURSOR (the card "add to prompt" button + the @ picker both feed this
  // concept).
  tagToPrompt(name: string, cover: string, mediaIds: string[]): void;
  // Registers the mention and returns the ACTUAL token to insert — may differ
  // from m.token when the display name collides with a DIFFERENT image/group
  // already tagged (e.g. two variants from the same batch share one label);
  // the token gets disambiguated ("@name 2") so each stays independently
  // clickable/removable instead of silently merging into one.
  addComposerMention(m: Mention): Mention;
  removeComposerMention(token: string): void;
  // Reuse a past prompt: load its text AND re-attach + re-tag the images it
  // referenced (from the asset's ref: tags), so @tokens light up again.
  reusePrompt(prompt: string, refMediaIds: string[]): void;
  generate(prompt: string): Promise<void>;
  regenerate(mediaId: string): Promise<void>;
  refine(mediaId: string, prompt: string, refs?: string[]): Promise<void>;
  uploadAsset(file: File): Promise<string | null>; // returns the new media_id
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
    provider: "atrium",
    preserveColors: true,
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
        count: s.count && s.count >= 1 && s.count <= 4 ? s.count : fallback.count,
        model: FLOW_MODELS.some((m) => m.id === s.model) ? (s.model as string) : fallback.model,
        size: (FLOW_SIZES as readonly string[]).includes(s.size ?? "") ? (s.size as FlowSize) : fallback.size,
        // Provider follows the chosen model (Atrium for Gemini/Nano-Banana, Avis for Seedream).
        provider: modelProvider(FLOW_MODELS.some((m) => m.id === s.model) ? (s.model as string) : fallback.model),
        preserveColors: typeof s.preserveColors === "boolean" ? s.preserveColors : fallback.preserveColors,
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

// In-flight generations, persisted so a page refresh (F5) can re-attach their
// pending tiles and keep polling instead of losing them. Each single-image gen
// is one entry; it's removed once the image lands (or the request fails).
const PENDING_KEY = "flowboard.flowStudio.pending.v1";
const PENDING_TTL_MS = 60 * 60 * 1000; // don't resurrect anything older than an hour

interface PendingGen {
  requestId: number;
  boardId: number | null; // only resume gens belonging to the board being opened
  prompt: string;
  aspect: string | null;
  refs: string[]; // material/reference ids to store as tags on the result
  model: string | null;
  provider: string | null;
  ts: number;
}

function loadPendingGens(): PendingGen[] {
  try {
    const raw = localStorage.getItem(PENDING_KEY);
    if (!raw) return [];
    const list = JSON.parse(raw) as PendingGen[];
    return Array.isArray(list) ? list.filter((p) => Date.now() - (p?.ts ?? 0) < PENDING_TTL_MS) : [];
  } catch {
    return [];
  }
}

function writePendingGens(list: PendingGen[]): void {
  try {
    localStorage.setItem(PENDING_KEY, JSON.stringify(list));
  } catch {
    // non-fatal
  }
}

function savePendingGen(p: PendingGen): void {
  writePendingGens([...loadPendingGens().filter((x) => x.requestId !== p.requestId), p]);
}

function removePendingGen(requestId: number): void {
  writePendingGens(loadPendingGens().filter((x) => x.requestId !== requestId));
}

// ── helpers ───────────────────────────────────────────────────────────────
function toAsset(ref: ReferenceItem): FlowAsset {
  return {
    refId: ref.id,
    mediaId: ref.mediaId,
    label: ref.label,
    prompt: ref.aiBrief,
    aspectRatio: ref.aspectRatio,
    modelUsed: ref.modelUsed,
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

let _jobSeq = 0; // monotonic id for in-flight generation jobs

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
  // Colour preservation only makes sense when there's a reference/source to
  // match; the agent appends the instruction to the prompt.
  if (settings.preserveColors && (refs.length > 0 || extra.source_media_id)) {
    params.preserve_colors = true;
  }
  return params;
}

/** Poll an EXISTING request id until it settles; returns its images. */
async function pollGen(
  requestId: number,
  onProgress?: (done: number, total: number) => void,
): Promise<{ mediaIds: string[]; providerUsed: string | null; modelUsed: string | null }> {
  let row = await getRequest(requestId);
  // Poll fast at first (700ms) so a finished image lands almost immediately,
  // then back off to 1500ms for the long tail. ~6min total budget.
  for (let i = 0; i < 260 && (row.status === "queued" || row.status === "running"); i++) {
    await sleep(i < 10 ? 700 : 1500);
    row = await getRequest(requestId);
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
  const modelUsed = (row.result["image_model"] as string | undefined) ?? null;
  return { mediaIds, providerUsed, modelUsed };
}

/** Show a pending tile for `requestId`, poll it, and drop the resulting image
 *  into the grid the moment IT lands — independent of any sibling gens. Shared
 *  by fresh gens and by F5 re-attach. Returns the assets it created. */
async function trackGen(
  requestId: number,
  meta: { prompt: string; aspect: string | null; refs: string[]; model: string | null; provider: string | null },
): Promise<FlowAsset[]> {
  const jobId = ++_jobSeq;
  useFlowStudioStore.setState((s) => ({
    genJobs: [...s.genJobs, { id: jobId, requestId, done: 0, total: 1, prompt: meta.prompt, refs: meta.refs }],
    generating: true,
  }));
  try {
    const { mediaIds, providerUsed, modelUsed } = await pollGen(requestId, (done, total) =>
      useFlowStudioStore.setState((s) => ({
        genJobs: s.genJobs.map((j) => (j.id === jobId ? { ...j, done, total } : j)),
      })),
    );
    // Skip any media already on-screen — an F5 that landed after the row was
    // persisted but before its pending entry was cleared must not re-create it.
    const have = new Set(useFlowStudioStore.getState().assets.map((a) => a.mediaId));
    const fresh = mediaIds.filter((m) => !have.has(m));
    const created = fresh.length
      ? await persistGenerated(fresh, meta.prompt, meta.aspect, meta.refs, meta.model ?? modelUsed)
      : [];
    useFlowStudioStore.setState((s) => ({
      assets: [...created, ...s.assets],
      notice: fallbackNotice(meta.provider ?? "", providerUsed) ?? s.notice,
    }));
    return created;
  } catch (err) {
    useFlowStudioStore.setState({ error: err instanceof Error ? err.message : "generation failed" });
    return [];
  } finally {
    removePendingGen(requestId);
    useFlowStudioStore.setState((s) => {
      const genJobs = s.genJobs.filter((j) => j.id !== jobId);
      return { genJobs, generating: genJobs.length > 0 };
    });
  }
}

/** Fire ONE single-image generation: create the request, remember it for F5,
 *  then track it to completion. Returns the assets it created. */
async function runOneGen(
  text: string,
  settings: FlowGenSettings,
  genRefs: string[],
  opts: { persistRefs?: string[]; aspect?: string | null; extra?: Record<string, unknown> } = {},
): Promise<FlowAsset[]> {
  const persistRefs = opts.persistRefs ?? genRefs;
  const aspect = opts.aspect === undefined ? settings.aspect : opts.aspect;
  let requestId: number;
  try {
    const req = await createRequest({
      type: "flow_gen_image",
      params: genParams(text, { ...settings, count: 1 }, genRefs, opts.extra ?? {}),
    });
    requestId = req.id;
  } catch (err) {
    useFlowStudioStore.setState({ error: err instanceof Error ? err.message : "generation failed" });
    return [];
  }
  savePendingGen({
    requestId,
    boardId: currentBoardId(),
    prompt: text,
    aspect,
    refs: persistRefs,
    model: settings.model,
    provider: settings.provider,
    ts: Date.now(),
  });
  return trackGen(requestId, {
    prompt: text,
    aspect,
    refs: persistRefs,
    model: settings.model,
    provider: settings.provider,
  });
}

/** After a refresh, re-attach the pending tiles for the board being opened and
 *  keep polling them (fire-and-forget). */
function resumePendingGens(): void {
  const boardId = currentBoardId();
  for (const p of loadPendingGens()) {
    if (p.boardId !== boardId) continue;
    void trackGen(p.requestId, {
      prompt: p.prompt,
      aspect: p.aspect,
      refs: p.refs,
      model: p.model,
      provider: p.provider,
    });
  }
}

// Notice shown when the backend silently swapped engines (Atrium can't read
// local media without a tunnel, so refs/edit run on Gemini instead).
function fallbackNotice(requested: string, used: string | null): string | null {
  if (used && used !== requested) {
    return "References/refine ran on Gemini (Atrium needs a PUBLIC_MEDIA_BASE_URL tunnel to read local images).";
  }
  return null;
}

/** Map a raw backend gen error to a clear, actionable Vietnamese message.
 *  Returns null when nothing matches (the caller then shows the sanitized raw
 *  error as-is). Deliberately never names the backend provider — just "nhà cung
 *  cấp" — so end users don't see which vendor sits behind an engine. */
export function humanizeGenError(raw: string): string | null {
  const s = (raw || "").toLowerCase();
  const has = (...keys: string[]) => keys.some((k) => s.includes(k));
  if (has("413", "too large", "payload too large"))
    return "Ảnh tham chiếu quá lớn để gửi lên model. Thử ảnh nhỏ hơn.";
  if (has("502", "503", "504", "gateway", "consecutive errors", "unavailable", "bad gateway"))
    return "Máy chủ nhà cung cấp đang quá tải hoặc chập chờn. Thử gen lại sau vài giây.";
  if (has("safety", "blocked", "no image in response"))
    return "Ảnh bị bộ lọc an toàn của model chặn. Thử chỉnh lại prompt (bớt nội dung nhạy cảm).";
  if (has("fetch media url"))
    return "Nhà cung cấp tạm thời không tải được ảnh tham chiếu. Thử gen lại.";
  if (has("download did not complete", "downloadurl fetch", "download exceeded", "throttled"))
    return "Tải ảnh kết quả về bị nghẽn mạng. Thử gen lại (thường lần sau là được).";
  if (has("429", "rate limit", "quota"))
    return "Đã chạm giới hạn/quota của engine. Thử lại sau một lúc.";
  if (has("did not resolve within", "timeout", "timed out"))
    return "Gen quá lâu, quá thời gian chờ. Thử gen lại.";
  if (has("not_configured", "avis_api_key", "atrium_client", "not set", "api key"))
    return "Engine này chưa được cấu hình API key. Liên hệ admin.";
  if (has("needs_public_url", "public_media_base", "input_unavailable", "r2_upload_failed"))
    return "Ảnh tham chiếu chưa sẵn sàng để gửi cho engine. Thử gen lại.";
  if (has("missing_prompt")) return "Chưa nhập prompt.";
  if (has("source_not_found")) return "Không tìm thấy ảnh nguồn để chỉnh sửa.";
  if (has("no_image_generated", "no_image", "succeeded but no image"))
    return "Model không trả về ảnh nào. Thử gen lại hoặc đổi prompt.";
  return null;
}

/** Mask internal backend-provider names out of a raw error before it's shown to
 *  end users, while keeping the exact technical detail (status codes, reason).
 *  Users see the precise cause but not which vendor powers an engine. */
export function sanitizeErrorDetail(raw: string): string {
  return (raw || "")
    .replace(/AVIS_API_KEY/gi, "API_KEY")
    .replace(/ATRIUM_CLIENT_ID\/ATRIUM_CLIENT_SECRET/gi, "API credentials")
    .replace(/\bavis\b/gi, "nhà cung cấp")
    .replace(/\batrium\b/gi, "nhà cung cấp")
    .replace(/\bark\b/gi, "nhà cung cấp");
}

const initial = loadPersisted();

export const useFlowStudioStore = create<FlowStudioState>((set, get) => ({
  assets: [],
  loading: false,
  generating: false,
  genJobs: [],
  tab: "all",
  error: null,
  notice: null,
  selectedMediaId: null,
  composerRefs: [],
  composerPrompt: "",
  composerMentions: [],
  composerCaret: 0,
  pendingCaretApply: null,
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
      // Re-attach any generations that were still in flight before a refresh, so
      // their pending tiles reappear and finish landing their images.
      resumePendingGens();
    } catch (err) {
      set({ loading: false, error: err instanceof Error ? err.message : "failed to load assets" });
    }
  },

  setTab(tab) {
    set({ tab });
  },

  tagToPrompt(name, cover, mediaIds) {
    mediaIds.forEach((id) => get().addRef(id));
    // Register FIRST — a name collision with a different image/group gets
    // disambiguated ("@name 2"), and we must insert THAT actual token text,
    // not the raw requested name.
    const { token } = get().addComposerMention({ token: `@${name}`, cover, mediaIds });
    const p = get().composerPrompt;
    // Insert at the last known caret, not always the end — a grid card's "@"
    // click should land wherever the user's cursor was, like typing it would.
    const caret = Math.max(0, Math.min(get().composerCaret, p.length));
    const before = p.slice(0, caret);
    const after = p.slice(caret);
    const leadSep = before.length === 0 || /\s$/.test(before) ? "" : " ";
    const trailSep = /^\s/.test(after) ? "" : " ";
    const insert = `${leadSep}${token}${trailSep}`;
    const newCaret = before.length + insert.length;
    set({
      composerPrompt: `${before}${insert}${after}`,
      composerCaret: newCaret,
      pendingCaretApply: newCaret,
    });
  },

  addComposerMention(m) {
    const s = get();
    const sameIds = (a: string[], b: string[]) =>
      a.length === b.length && a.every((id, i) => id === b[i]);
    const findByToken = (t: string) => s.composerMentions.find((x) => x.token === t);

    let token = m.token;
    const existing = findByToken(token);
    if (existing) {
      if (sameIds(existing.mediaIds, m.mediaIds)) return existing; // truly the same tag — no-op
      // Same display name, DIFFERENT image(s) (e.g. two variants sharing an
      // auto-generated label) — disambiguate instead of silently merging them
      // into one mention (that broke per-image pill click + tag removal).
      let n = 2;
      while (findByToken(`${token} ${n}`)) n++;
      token = `${token} ${n}`;
    }
    const resolved: Mention = { ...m, token };
    set({ composerMentions: [...s.composerMentions, resolved] });
    return resolved;
  },

  removeComposerMention(token) {
    set((s) => ({ composerMentions: s.composerMentions.filter((x) => x.token !== token) }));
  },

  reusePrompt(prompt, refMediaIds) {
    const assets = get().assets;
    const nameOf = (a: FlowAsset) =>
      groupName(a.tags, CHAR_PREFIX) || groupName(a.tags, SCENE_PREFIX) || a.label || "image";
    const ids = new Set<string>();
    // 1. Exact: the refs stored with the image when it was generated.
    for (const id of refMediaIds) if (assets.some((a) => a.mediaId === id)) ids.add(id);
    // 2. Fallback ONLY when the image has no stored refs (older images): resolve
    //    the @tokens against character/scene NAMES via resolveMentions. We must
    //    NOT match on a.label here — many uploads share a generic label like
    //    "image.png", and a substring `prompt.includes("@"+label)` would then
    //    pull in EVERY such image (the "pile of wrong refs" bug).
    if (ids.size === 0) {
      for (const id of get().resolveMentions(prompt)) ids.add(id);
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
  setComposerCaret(pos) {
    set({ composerCaret: pos });
  },
  clearPendingCaretApply() {
    set({ pendingCaretApply: null });
  },

  setSettings(patch) {
    const next = { ...get().settings, ...patch };
    // The engine is decided by the model, not chosen separately.
    next.provider = modelProvider(next.model);
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
    if (!text) return; // no wait-for-finish: multiple generations can run at once
    const settings = get().settings;
    // Capture prompt + material BEFORE firing so each in-flight tile can offer
    // "reuse exact prompt + refs" even while it's still generating.
    const refs = [...new Set([...get().composerRefs, ...get().resolveMentions(text)])];
    // Sent → clear the composer (prompt + attached refs) like Flow does.
    set({ composerPrompt: "", composerRefs: [], composerMentions: [], error: null });
    set((s) => ({ recentPrompts: pushRecent(s.recentPrompts, text) }));
    persist(get().settings, get().recentPrompts);
    // Fire `count` INDEPENDENT single-image gens: each one lands in the grid the
    // MOMENT it finishes (no waiting for the whole batch), each fails on its own,
    // and each is remembered in localStorage so a refresh re-attaches its tile.
    const count = Math.max(1, settings.count);
    await Promise.allSettled(Array.from({ length: count }, () => runOneGen(text, settings, refs)));
  },

  async regenerate(mediaId) {
    const asset = get().assets.find((a) => a.mediaId === mediaId);
    if (!asset?.prompt) return;
    await get().generate(asset.prompt);
  },

  async refine(mediaId, prompt, refs) {
    const text = prompt.trim();
    if (!text) return; // concurrent allowed
    const settings = get().settings;
    set({ error: null });
    set((s) => ({ recentPrompts: pushRecent(s.recentPrompts, text) }));
    persist(get().settings, get().recentPrompts);
    // One single-image edit: preserve the source frame (aspect = null) and store
    // the source + any refs as tags. Tracked/persisted like a normal gen.
    const created = await runOneGen(text, settings, refs ?? [], {
      persistRefs: [mediaId, ...(refs ?? [])],
      aspect: null,
      extra: { source_media_id: mediaId },
    });
    // A refine is "show me the edited result" → switch the viewer to it.
    if (created[0]) set({ selectedMediaId: created[0].mediaId });
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
      // Drop it into the grid — don't auto-open the detail viewer.
      set((s) => ({
        assets: [asset, ...s.assets.filter((a) => a.refId !== asset.refId)],
      }));
      return media_id;
    } catch (err) {
      set({ error: err instanceof Error ? err.message : "upload failed" });
      return null;
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
    // Intentionally allows duplicates — tagging the same image more than once
    // (e.g. clicking a grid card's @ button on it repeatedly) should attach it
    // that many times, each as its own removable chip.
    set((s) => ({ composerRefs: [...s.composerRefs, mediaId] }));
  },
  removeRef(mediaId) {
    // Removes ONE occurrence (the first found), not every copy of this image —
    // duplicate chips are visually identical, so which specific slot is
    // dropped doesn't matter, only that the count goes down by exactly one.
    set((s) => {
      const idx = s.composerRefs.indexOf(mediaId);
      if (idx === -1) return s;
      const next = s.composerRefs.slice();
      next.splice(idx, 1);
      return { composerRefs: next };
    });
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
  modelUsed: string | null = null,
): Promise<FlowAsset[]> {
  const refTags = [...new Set(refs)].map((id) => REF_PREFIX + id);
  // Persist all variants in PARALLEL (was sequential — N round-trips back-to-back
  // delayed the grid update for multi-image gens). Order is preserved.
  const settled = await Promise.all(
    mediaIds.map(async (mid): Promise<FlowAsset | null> => {
      try {
        const ref = await createReference({
          media_id: mid,
          kind: "image",
          label: prompt.slice(0, 60),
          ai_brief: prompt,
          aspect_ratio: aspect,
          model_used: modelUsed,
          tags: [FLOW_TAG, ...refTags],
          source_board_id: currentBoardId() ?? undefined,
        });
        return toAsset(ref);
      } catch {
        // non-fatal: the image is still generated & cached, just not persisted
        return null;
      }
    }),
  );
  const created: FlowAsset[] = settled.filter((a): a is FlowAsset => a !== null);
  return created;
}

function pushRecent(recent: string[], prompt: string): string[] {
  return [prompt, ...recent.filter((p) => p !== prompt)].slice(0, 8);
}
