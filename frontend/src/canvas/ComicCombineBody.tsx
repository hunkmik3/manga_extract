import { useEffect, useRef, useState, type MouseEvent } from "react";
import { ensureBoardProject, mediaUrl } from "../api/client";
import { useBoardStore, type FlowboardNodeData } from "../store/board";
import {
  addCharacter,
  createRequest,
  findCharacterDb,
  findStyleFrame,
  patchComicNode,
  promoteCellToCharacter,
  relayoutComicCombineChains,
  runComicRequest,
  runRequestToResult,
} from "./comicShared";

interface PanelSpec {
  pageMediaId: string;
  box: { x: number; y: number; w: number; h: number };
  w: number;
  h: number;
  pageName?: string;
  panelIndex?: number;
}

/** Per-cell character assignment: which canon character drives this cell's refs
 * (char_id), an optional outfit label, which axes the canon ref should OVERRIDE
 * the (off-model) source on, plus the Director's camera tags (shot scale +
 * facing orientation — drive view selection and override safety). Keyed by cell
 * index in node.data.cellAssign. */
interface CellAssign {
  charId?: string;
  outfit?: string;
  override?: string[];
  shot?: string;        // closeup | medium | wide
  orientation?: string; // front | profile | back
  env?: string;         // scene setting descriptor (from 🎬 Auto-scenes)
  bgType?: string;      // real-location | flat-band | abstract-action
  mood?: string;        // flashback-pale | night | … (atmosphere grade)
}

/** Build a combine/regen panel spec, merging the per-cell character + scene assignment. */
function buildCellSpec(p: PanelSpec, a: CellAssign | undefined): Record<string, unknown> {
  const s: Record<string, unknown> = { page_media_id: p.pageMediaId, box: p.box };
  if (a?.charId) s.char_id = a.charId;
  if (a?.outfit && a.outfit.trim()) s.outfit = a.outfit.trim();
  if (Array.isArray(a?.override) && a.override.length) s.override_axes = a.override;
  if (a?.shot) s.shot = a.shot;
  if (a?.orientation) s.orientation = a.orientation;
  if (a?.env && a.env.trim()) s.env_descriptor = a.env.trim();
  if (a?.bgType) s.bg_type = a.bgType;
  if (a?.mood) s.mood = a.mood;
  return s;
}

/** A 2×2-cell preview: CSS-crops one source panel from its page image. */
function CellCrop({ p }: { p: PanelSpec }) {
  if (!p?.pageMediaId || !p.w || !p.h || !p.box?.w || !p.box?.h) {
    return <div style={{ background: "var(--border)", borderRadius: 3 }} />;
  }
  return (
    <div style={{ width: "100%", aspectRatio: "1 / 1", overflow: "hidden", position: "relative", borderRadius: 3, background: "var(--border)" }}>
      <img
        src={mediaUrl(p.pageMediaId)} alt="" draggable={false}
        style={{
          position: "absolute", top: 0, left: 0,
          width: `${(p.w / p.box.w) * 100}%`, height: "auto",
          transform: `translate(${(-p.box.x / p.w) * 100}%, ${(-p.box.y / p.h) * 100}%)`,
          transformOrigin: "top left", maxWidth: "none",
        }}
      />
    </div>
  );
}

/**
 * Combine node — cleans 4 panels individually then code-stitches a 2×2 9:16.
 * After combining, each cleaned cell is kept separately so a single bad cell can
 * be re-generated (↻) without re-running all four.
 */
export function ComicCombineBody({ rfId, data }: { rfId: string; data: FlowboardNodeData }) {
  const rootRef = useRef<HTMLDivElement | null>(null);
  const lastHeightRef = useRef(0);
  const relayoutTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const [regenPrompt, setRegenPrompt] = useState("");
  const [regenVariants, setRegenVariants] = useState(1); // "x4" on Flow — N candidates per ↻
  const [regenning, setRegenning] = useState<number[]>([]); // cells queued/in-flight
  const [cellCandidates, setCellCandidates] = useState<Record<number, (string | null)[]>>({}); // cell → variants awaiting a pick
  const [showHistory, setShowHistory] = useState(false);
  const [dlRes, setDlRes] = useState<"1K" | "2K" | "4K">("1K"); // download resolution (2K/4K = Flow upscale)
  const [upscaling, setUpscaling] = useState(false);
  // Every image ever produced for each cell (combine + all re-gen candidates),
  // keyed by cell index as a string. Persisted on the node so the user can
  // re-pick an earlier result from the history panel.
  const cellHistory = (data.cellHistory && typeof data.cellHistory === "object"
    ? (data.cellHistory as Record<string, (string | null)[]>)
    : {});
  const queueRef = useRef<number[]>([]);
  const processingRef = useRef(false);
  const panels = (Array.isArray(data.panels) ? data.panels : []) as PanelSpec[];
  const cells = (Array.isArray(data.cells) ? data.cells : []) as (string | null)[];
  const mediaId = typeof data.mediaId === "string" ? data.mediaId : "";
  const status = typeof data.status === "string" ? data.status : "idle";
  const isBusy = status === "queued" || status === "running";
  const errorMsg = typeof data.error === "string" ? data.error : undefined;
  const characterRefs = findCharacterDb();
  const useCharacterRefs = Boolean(data.useCharacterRefs);
  // Engine + model in one pick. `gemini-*` = direct Gemini API via the key in
  // .env (default — no extension/Flow tab needed, runs on API credits);
  // `NANO_BANANA_*` = the Flow bridge through the extension (subscription).
  // Flow's quota is PER MODEL per day, so within Flow, NB2 keeps working after
  // Pro's quota is exhausted.
  const imageModel = typeof data.imageModel === "string" && data.imageModel ? data.imageModel : "gemini-3-pro-image";
  const [promoted, setPromoted] = useState<number | null>(null); // cell just ⭐-promoted (transient)
  // Per-cell character assignment (char_id + outfit + override axes), keyed by cell index.
  const cellAssign = (data.cellAssign && typeof data.cellAssign === "object"
    ? (data.cellAssign as Record<string, CellAssign>)
    : {});

  // Keep the freshest values for the async re-gen queue (avoids stale closures).
  const latestRef = useRef({ panels, cells, mediaId, useCharacterRefs, characterRefs, regenPrompt, regenVariants, cellAssign, imageModel });
  latestRef.current = { panels, cells, mediaId, useCharacterRefs, characterRefs, regenPrompt, regenVariants, cellAssign, imageModel };

  // Persist a per-cell assignment patch (merging with the freshest stored map).
  function setCellAssign(i: number, patch: CellAssign) {
    const node = useBoardStore.getState().nodes.find((n) => n.id === rfId);
    const cur = ((node?.data?.cellAssign as Record<string, CellAssign>) ?? {});
    const key = String(i);
    patchComicNode(rfId, { cellAssign: { ...cur, [key]: { ...(cur[key] ?? {}), ...patch } } });
  }

  // ⭐ — bless the current cell image as a frozen reference for its assigned
  // character (newest first). Disabled until a character is chosen for the cell.
  function promoteCell(i: number) {
    const cid = cells[i];
    const charId = cellAssign[String(i)]?.charId;
    if (typeof cid !== "string" || !cid || !charId) return;
    if (promoteCellToCharacter(charId, cid)) {
      setPromoted(i);
      setTimeout(() => setPromoted((v) => (v === i ? null : v)), 1500);
    }
  }

  // Edit a cell's scene setting by hand — fixes a mis-tagged insert/cutaway
  // panel (e.g. a TV-screen or map insert that 🎬 grouped with the wrong scene).
  function editEnv(i: number) {
    const cur = cellAssign[String(i)]?.env ?? "";
    const next = window.prompt("Scene setting for this cell (blank = none):", cur);
    if (next === null) return;
    setCellAssign(i, { env: next.trim() || undefined });
  }

  // 🔒 force — when on, the canon ref OVERRIDES the source's face/hair + outfit
  // (for off-model panels). Off = gentle identity hint, source stays faithful.
  function toggleForce(i: number) {
    const on = (cellAssign[String(i)]?.override ?? []).length > 0;
    setCellAssign(i, { override: on ? [] : ["identity", "outfit"] });
  }

  // ✨ Director — one vision call tags every cell (who / shot scale / facing),
  // then applies the routing automatically: char_id assignment, camera tags for
  // view selection, and SAFETY: the identity override (🔒) is force-cleared on
  // back/profile cells (it would rotate the character to face the camera).
  const [tagging, setTagging] = useState(false);
  async function autoAssign() {
    if (tagging || panels.length === 0 || !characterRefs?.length) return;
    setTagging(true);
    try {
      const specs = panels.slice(0, 4).map((p) => ({ page_media_id: p.pageMediaId, box: p.box }));
      const cast = characterRefs.map((c) => ({ id: c.id, name: c.name, sampleMediaId: c.sampleMediaId }));
      const result = await runRequestToResult(
        createRequest({ type: "tag_panels", node_id: parseInt(rfId, 10), params: { panels: specs, characters: cast } }),
      );
      const tags = (result.tags as Array<{ char_id?: string | null; shot?: string | null; orientation?: string | null }>) ?? [];
      const node = useBoardStore.getState().nodes.find((n) => n.id === rfId);
      const cur = ((node?.data?.cellAssign as Record<string, CellAssign>) ?? {});
      const merged: Record<string, CellAssign> = { ...cur };
      tags.forEach((t, i) => {
        const key = String(i);
        const prev = merged[key] ?? {};
        const unsafe = t.orientation === "back" || t.orientation === "profile";
        merged[key] = {
          ...prev,
          charId: t.char_id ?? prev.charId,
          shot: t.shot ?? prev.shot,
          orientation: t.orientation ?? prev.orientation,
          // Never let 🔒 rotate a back/profile panel toward the camera.
          override: unsafe ? [] : prev.override,
        };
      });
      patchComicNode(rfId, { cellAssign: merged });
    } catch (err) {
      patchComicNode(rfId, { status: "error", error: `auto-assign: ${String(err)}` });
    } finally {
      setTagging(false);
    }
  }

  // Cell character <select>: pick an existing character, clear, or define a new
  // one inline (cold-start, no CCIP) — seeding it with this cell as its first
  // frozen reference, then assigning it to the cell.
  async function onSelectChar(i: number, value: string) {
    if (value === "__new__") {
      const name = window.prompt("New character name:", `Character ${(characterRefs?.length ?? 0) + 1}`);
      if (name == null) return; // cancelled
      const cid = cells[i];
      const id = await addCharacter(name, typeof cid === "string" && cid ? cid : undefined);
      if (id) setCellAssign(i, { charId: id });
      return;
    }
    setCellAssign(i, { charId: value || undefined });
  }

  // Merge the project-wide style frame (ref image + descriptor) into a request's
  // params. Read live from the board so the freshest style applies.
  function applyStyleFrame(params: Record<string, unknown>) {
    const sf = findStyleFrame();
    if (sf?.styleRefMediaId) params.style_ref_media_id = sf.styleRefMediaId;
    if (sf?.styleDescriptor && sf.styleDescriptor.trim()) params.style_descriptor = sf.styleDescriptor.trim();
  }

  async function project(opts?: { needsFlow?: boolean }): Promise<string | null> {
    // API engine doesn't touch Flow at all — skip the board→Flow-project
    // handshake (which needs a live extension token) and use a placeholder id,
    // so combine/regen run with no Flow session whatsoever. Flow-only features
    // (2K/4K upscale) pass needsFlow to always get the real project.
    if (!opts?.needsFlow && latestRef.current.imageModel.startsWith("gemini-")) return "api-local";
    const boardId = useBoardStore.getState().boardId;
    if (boardId == null) return null;
    try {
      return (await ensureBoardProject(boardId)).flow_project_id;
    } catch (err) {
      patchComicNode(rfId, { status: "error", error: `project: ${String(err)}` });
      return null;
    }
  }

  async function run() {
    if (isBusy || panels.length === 0) return;
    const projectId = await project();
    if (!projectId) return;
    const specs = panels.map((p, i) => buildCellSpec(p, cellAssign[String(i)]));
    const params: Record<string, unknown> = { project_id: projectId, panels: specs };
    // Pass the character DB whenever it exists so per-cell char_id can resolve to
    // frozen refs; CCIP appearance-match runs only when "Use Character DB refs"
    // is ticked (auto_match). An assigned char_id always wins regardless.
    if (characterRefs?.length) params.characters = characterRefs;
    params.auto_match = useCharacterRefs;
    params.image_model = imageModel;
    applyStyleFrame(params);
    runComicRequest(
      rfId,
      () => createRequest({ type: "combine_panels", node_id: parseInt(rfId, 10), params }),
      (result) => {
        const newCells = (result.cells as (string | null)[]) ?? [];
        // A fresh combine resets history to the cells it just produced.
        const hist: Record<string, (string | null)[]> = {};
        newCells.forEach((c, i) => { if (typeof c === "string" && c) hist[String(i)] = [c]; });
        return { mediaId: (result.mediaId as string) ?? "", cells: newCells, width: result.width, height: result.height, cellHistory: hist };
      },
    );
  }

  // Append image ids to a cell's history (dedup, cap), reading the freshest
  // value from the store so concurrent re-gens don't clobber each other.
  function pushHistory(index: number, ids: (string | null)[]) {
    const node = useBoardStore.getState().nodes.find((n) => n.id === rfId);
    const cur = ((node?.data?.cellHistory as Record<string, (string | null)[]>) ?? {});
    const key = String(index);
    const merged = Array.isArray(cur[key]) ? [...cur[key]] : [];
    for (const id of ids) {
      if (typeof id === "string" && id && !merged.includes(id)) merged.push(id);
    }
    patchComicNode(rfId, { cellHistory: { ...cur, [key]: merged.slice(-24) } });
  }

  // Queue a cell for re-gen. Several can be queued at once; they run one at a
  // time (each restitches the 2×2, so sequential avoids a race) while every
  // queued/in-flight cell shows its own progress spinner.
  function regenCell(i: number) {
    if (isBusy || !panels[i]) return;
    if (queueRef.current.includes(i) || regenning.includes(i)) return;
    queueRef.current.push(i);
    setRegenning((prev) => (prev.includes(i) ? prev : [...prev, i]));
    void processRegenQueue();
  }

  async function processRegenQueue() {
    if (processingRef.current) return;
    processingRef.current = true;
    const projectId = await project();
    if (!projectId) {
      queueRef.current = [];
      setRegenning([]);
      processingRef.current = false;
      return;
    }
    let working = [...latestRef.current.cells]; // updated from each result → no race
    while (queueRef.current.length) {
      const i = queueRef.current.shift() as number;
      const L = latestRef.current;
      const p = L.panels[i];
      if (!p) {
        setRegenning((prev) => prev.filter((x) => x !== i));
        continue;
      }
      const panel = buildCellSpec(p, L.cellAssign[String(i)]);
      const params: Record<string, unknown> = { project_id: projectId, panel, cells: working, index: i };
      if (L.characterRefs?.length) params.characters = L.characterRefs;
      params.auto_match = L.useCharacterRefs;
      params.image_model = L.imageModel;
      applyStyleFrame(params);
      if (L.regenPrompt.trim()) params.prompt = L.regenPrompt.trim();
      if (L.regenVariants > 1) params.variant_count = L.regenVariants;
      try {
        const result = await runRequestToResult(createRequest({ type: "regen_cell", node_id: parseInt(rfId, 10), params }));
        if (Array.isArray(result.candidates)) {
          // x4: stash the candidates for this cell — the user picks one (pickCandidate).
          const ids = result.candidates as (string | null)[];
          setCellCandidates((prev) => ({ ...prev, [i]: ids }));
          pushHistory(i, ids); // keep every candidate in the cell's history
        } else {
          working = (result.cells as (string | null)[]) ?? working;
          patchComicNode(rfId, {
            mediaId: (result.mediaId as string) ?? L.mediaId,
            cells: working,
            width: result.width,
            height: result.height,
            status: "done",
            error: undefined,
          });
          pushHistory(i, [working[i]]); // single re-gen → record the new image
        }
      } catch (err) {
        patchComicNode(rfId, { status: "error", error: `cell ${i + 1}: ${String(err)}` });
      } finally {
        setRegenning((prev) => prev.filter((x) => x !== i));
      }
    }
    processingRef.current = false;
  }

  // Commit a chosen re-gen candidate into the grid, then re-stitch the 2×2.
  async function pickCandidate(index: number, chosen: string) {
    const next = [...latestRef.current.cells];
    next[index] = chosen;
    setCellCandidates((prev) => {
      const copy = { ...prev };
      delete copy[index];
      return copy;
    });
    try {
      const result = await runRequestToResult(
        createRequest({ type: "restitch_cells", node_id: parseInt(rfId, 10), params: { cells: next } }),
      );
      patchComicNode(rfId, {
        mediaId: (result.mediaId as string) ?? mediaId,
        cells: (result.cells as (string | null)[]) ?? next,
        width: result.width,
        height: result.height,
        status: "done",
        error: undefined,
      });
    } catch (err) {
      patchComicNode(rfId, { status: "error", error: `restitch: ${String(err)}` });
    }
  }

  function triggerDownload(url: string, name: string) {
    const a = document.createElement("a");
    a.href = url;
    a.download = name;
    document.body.appendChild(a);
    a.click();
    a.remove();
  }

  // Upscale a cached image to 2K/4K via Flow; returns the new media id or null.
  async function upscaleMedia(localMediaId: string, res: "2K" | "4K"): Promise<string | null> {
    const projectId = await project({ needsFlow: true }); // upscale is Flow-only
    if (!projectId) return null;
    try {
      const result = await runRequestToResult(
        createRequest({ type: "upsample_image", node_id: parseInt(rfId, 10), params: { project_id: projectId, media_id: localMediaId, resolution: res } }),
      );
      return (result.mediaId as string) ?? null;
    } catch (err) {
      patchComicNode(rfId, { status: "error", error: `upscale ${res}: ${String(err)}` });
      return null;
    }
  }

  async function downloadCombined(e: MouseEvent<HTMLButtonElement>) {
    e.stopPropagation();
    if (!mediaId || upscaling) return;
    const base = `comic-combine-${data.shortId ?? rfId}`;
    if (dlRes === "1K") {
      triggerDownload(mediaUrl(mediaId), `${base}.png`);
      return;
    }
    setUpscaling(true);
    const up = await upscaleMedia(mediaId, dlRes);
    setUpscaling(false);
    if (up) triggerDownload(mediaUrl(up), `${base}-${dlRes}.jpg`);
  }

  async function downloadCells(e: MouseEvent<HTMLButtonElement>) {
    e.stopPropagation();
    if (upscaling) return;
    const base = `comic-combine-${data.shortId ?? rfId}`;
    if (dlRes === "1K") {
      // Save each cleaned cell as-is. Stagger so the browser doesn't drop them.
      let n = 0;
      cells.forEach((cid, i) => {
        if (typeof cid !== "string" || !cid) return;
        const delay = n++ * 250;
        const url = mediaUrl(cid);
        setTimeout(() => triggerDownload(url, `${base}-cell-${i + 1}.png`), delay);
      });
      return;
    }
    // Upscale each cell, then download (sequential — Flow is the bottleneck).
    setUpscaling(true);
    for (let i = 0; i < cells.length; i++) {
      const cid = cells[i];
      if (typeof cid !== "string" || !cid) continue;
      const up = await upscaleMedia(cid, dlRes);
      if (up) {
        triggerDownload(mediaUrl(up), `${base}-cell-${i + 1}-${dlRes}.jpg`);
        await new Promise((r) => setTimeout(r, 250));
      }
    }
    setUpscaling(false);
  }

  function loadImage(src: string): Promise<HTMLImageElement> {
    return new Promise((resolve, reject) => {
      const img = new Image();
      img.crossOrigin = "anonymous";
      img.onload = () => resolve(img);
      img.onerror = reject;
      img.src = src;
    });
  }

  // Download the raw (un-cleaned) source panels — crop each box from its page
  // client-side, available even before a combine has run.
  async function downloadRaw(e: MouseEvent<HTMLButtonElement>) {
    e.stopPropagation();
    const cache = new Map<string, Promise<HTMLImageElement>>();
    for (let i = 0; i < Math.min(panels.length, 4); i++) {
      const p = panels[i];
      if (!p?.pageMediaId || !p.box?.w || !p.box?.h) continue;
      try {
        if (!cache.has(p.pageMediaId)) cache.set(p.pageMediaId, loadImage(mediaUrl(p.pageMediaId)));
        const img = await cache.get(p.pageMediaId)!;
        const canvas = document.createElement("canvas");
        canvas.width = p.box.w;
        canvas.height = p.box.h;
        canvas.getContext("2d")?.drawImage(img, p.box.x, p.box.y, p.box.w, p.box.h, 0, 0, p.box.w, p.box.h);
        triggerDownload(canvas.toDataURL("image/png"), `comic-raw-${data.shortId ?? rfId}-panel-${i + 1}.png`);
        await new Promise((r) => setTimeout(r, 250)); // stagger so downloads aren't dropped
      } catch {
        /* skip a panel whose page image can't be read */
      }
    }
  }

  useEffect(() => {
    const root = rootRef.current;
    const card = root?.closest(".node-card") as HTMLElement | null;
    if (!card) return;
    const observer = new ResizeObserver((entries) => {
      const next = Math.ceil(entries[0]?.contentRect.height ?? 0);
      if (!Number.isFinite(next) || next <= 0) return;
      if (Math.abs(next - lastHeightRef.current) < 4) return;
      lastHeightRef.current = next;
      useBoardStore.getState().updateNodeData(rfId, { __measuredHeight: next });
      if (relayoutTimerRef.current) clearTimeout(relayoutTimerRef.current);
      relayoutTimerRef.current = setTimeout(() => {
        relayoutComicCombineChains();
      }, 80);
    });
    observer.observe(card);
    return () => {
      observer.disconnect();
      if (relayoutTimerRef.current) {
        clearTimeout(relayoutTimerRef.current);
        relayoutTimerRef.current = null;
      }
    };
  }, [rfId]);

  return (
    <div ref={rootRef} className="node-body node-body--comic-combine" style={{ display: "flex", flexDirection: "column", gap: 6 }}>
      <label style={{ fontSize: 11, opacity: 0.75 }}>Combine 2×2 · {panels.length} panels</label>
      <label
        style={{ fontSize: 10, opacity: 0.75, display: "flex", alignItems: "center", gap: 5 }}
        title="Engine + model. API = direct Gemini API via your key in .env (no extension/Flow tab, separate quota/billing). Flow = the extension bridge on your Flow subscription; Flow quota is PER MODEL per day, so NB2 still works after Pro's quota is exhausted."
      >
        Model
        <select
          className="nodrag"
          value={imageModel}
          onChange={(e) => patchComicNode(rfId, { imageModel: e.target.value })}
          style={{ flex: 1, minWidth: 0, fontSize: 10, padding: "1px 4px" }}
        >
          <optgroup label="Gemini API (key — no extension)">
            <option value="gemini-3-pro-image">API · Nano Banana Pro</option>
            <option value="gemini-2.5-flash-image">API · Nano Banana (flash)</option>
          </optgroup>
          <optgroup label="Flow (extension bridge)">
            <option value="NANO_BANANA_PRO">Flow · Nano Banana Pro</option>
            <option value="NANO_BANANA_2">Flow · Nano Banana 2</option>
          </optgroup>
        </select>
      </label>
      {characterRefs?.length ? (
        <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
          <button
            className="comic-btn comic-btn--sm"
            onClick={() => void autoAssign()}
            disabled={tagging || panels.length === 0}
            title="Director: one vision pass tags every cell (which character / shot scale / facing direction), assigns characters, picks the matching sheet view, and disables 🔒 on back/profile cells."
            style={{ flex: 1 }}
          >
            {tagging ? "Tagging…" : "✨ Auto-assign cells"}
          </button>
          <label
            style={{ fontSize: 10, opacity: 0.72, margin: 0, display: "flex", alignItems: "center", gap: 4, whiteSpace: "nowrap" }}
            title="CCIP appearance auto-match for cells with NO character picked. Per-cell assignment always takes priority."
          >
            <input
              type="checkbox"
              checked={useCharacterRefs}
              onChange={(e) => patchComicNode(rfId, { useCharacterRefs: e.target.checked })}
            />
            CCIP fallback
          </label>
        </div>
      ) : null}

      {mediaId ? (
        <img src={mediaUrl(mediaId)} alt="2x2 storyboard" loading="lazy" style={{ width: "100%", borderRadius: 4, display: "block", background: "var(--border)" }} />
      ) : (
        <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 3 }}>
          {[0, 1, 2, 3].map((i) => {
            if (!panels[i]) {
              return <div key={i} style={{ aspectRatio: "1 / 1", background: "var(--border)", borderRadius: 3, opacity: 0.4 }} />;
            }
            // Pre-combine: show each source crop WITH its routing strip so 🪄/✨
            // assignments are visible (and editable) before the first combine —
            // they steer that very first generation.
            const assign = cellAssign[String(i)] ?? {};
            const forced = (assign.override ?? []).length > 0;
            const camTag = [assign.shot, assign.orientation].filter(Boolean).join(" · ");
            return (
              <div key={i} style={{ minWidth: 0 }}>
                <CellCrop p={panels[i]} />
                <div className="nodrag nopan" style={{ display: "flex", alignItems: "center", gap: 2, marginTop: 2, width: "100%", minWidth: 0, boxSizing: "border-box", overflow: "hidden" }}>
                  <select
                    value={assign.charId ?? ""}
                    onChange={(e) => void onSelectChar(i, e.target.value)}
                    title="Assign this cell's character → feeds its frozen refs on the first combine. Pick ＋ New to define one."
                    style={{ flex: "1 1 0%", minWidth: 0, width: 0, fontSize: 9, padding: "1px 2px" }}
                  >
                    <option value="">— char</option>
                    {(characterRefs ?? []).map((c) => (
                      <option key={c.id} value={c.id}>{c.name}</option>
                    ))}
                    <option value="__new__">＋ New character…</option>
                  </select>
                  <button
                    className="comic-btn comic-btn--sm"
                    onClick={() => toggleForce(i)}
                    disabled={!assign.charId}
                    title="Force the canon ref to OVERRIDE the source's face/hair + outfit (only for FRONT-facing off-model panels)."
                    style={{ flexShrink: 0, padding: "1px 4px", fontSize: 10, opacity: forced ? 1 : 0.45, fontWeight: forced ? 700 : 400 }}
                  >🔒</button>
                </div>
                {camTag ? (
                  <p
                    title="Director camera tags (shot scale · facing) from ✨ — drive which sheet view is attached."
                    style={{ fontSize: 8, opacity: 0.55, margin: "1px 0 0", textAlign: "center" }}
                  >🎬 {camTag}</p>
                ) : null}
                <p
                  className="nodrag"
                  onClick={() => editEnv(i)}
                  title={assign.env ? `Scene setting (click to edit): ${assign.env}` : "Set a scene setting for this cell"}
                  style={{ fontSize: 8, opacity: assign.env ? 0.55 : 0.35, margin: 0, textAlign: "center", whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis", cursor: "pointer" }}
                >🏞 {assign.env || "set scene ✎"}</p>
              </div>
            );
          })}
        </div>
      )}

      {/* per-cell re-gen: only after a combine produced cells */}
      {cells.length > 0 && (
        <>
          <p style={{ fontSize: 9, opacity: 0.55, margin: 0 }}>Re-gen one or more cells (↻) — they run one by one with progress on each:</p>
          <textarea
            value={regenPrompt}
            onChange={(e) => setRegenPrompt(e.target.value)}
            placeholder="Custom re-gen prompt for ↻ (blank = default clean + extend to 9:16)"
            rows={2}
            spellCheck={false}
            style={{ width: "100%", boxSizing: "border-box", fontSize: 10, padding: "4px 6px", resize: "vertical", fontFamily: "inherit" }}
          />
          <div style={{ display: "flex", alignItems: "center", gap: 5, fontSize: 10 }} title="Candidates per ↻ (Flow x1–x4). >1 lets you pick the best.">
            <span style={{ opacity: 0.7 }}>Variants</span>
            {[1, 2, 3, 4].map((k) => (
              <button
                key={k}
                className="comic-btn comic-btn--sm"
                onClick={() => setRegenVariants(k)}
                style={{ flex: 1, opacity: regenVariants === k ? 1 : 0.5, fontWeight: regenVariants === k ? 700 : 400 }}
              >x{k}</button>
            ))}
          </div>
          <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 4 }}>
            {[0, 1, 2, 3].map((i) => {
              const busyCell = regenning.includes(i);
              const waiting = queueRef.current.includes(i); // still in queue = not the active one
              const cand = cellCandidates[i];
              const assign = cellAssign[String(i)] ?? {};
              const forced = (assign.override ?? []).length > 0;
              const camTag = [assign.shot, assign.orientation].filter(Boolean).join(" · ");
              return (
                <div key={i} style={{ position: "relative" }}>
                  {cells[i] ? (
                    <img src={mediaUrl(cells[i] as string)} alt={`cell ${i + 1}`} loading="lazy" style={{ width: "100%", borderRadius: 3, display: "block", background: "var(--border)" }} />
                  ) : (
                    <div style={{ aspectRatio: "9 / 16", background: "var(--border)", borderRadius: 3, opacity: 0.4 }} />
                  )}
                  {busyCell && (
                    <div className="comic-cell-regen">
                      <div className="comic-cell-spin" />
                      <span>{waiting ? "queued…" : "re-generating…"}</span>
                    </div>
                  )}
                  {cand && (
                    <div className="comic-cell-regen nodrag nopan" style={{ gap: 3, padding: 3, display: "grid", gridTemplateColumns: "1fr 1fr", alignContent: "center", overflow: "auto" }}>
                      <span style={{ gridColumn: "1 / -1", textAlign: "center" }}>pick one:</span>
                      {cand.map((cid, k) =>
                        cid ? (
                          <img
                            key={k}
                            className="nodrag"
                            src={mediaUrl(cid)}
                            alt={`candidate ${k + 1}`}
                            loading="lazy"
                            onClick={() => pickCandidate(i, cid)}
                            title={`Use candidate ${k + 1}`}
                            style={{ width: "100%", borderRadius: 2, display: "block", cursor: "pointer" }}
                          />
                        ) : null
                      )}
                    </div>
                  )}
                  <button
                    className="comic-btn comic-btn--sm"
                    onClick={() => regenCell(i)}
                    disabled={isBusy || busyCell || !!cand || !panels[i]}
                    title={regenVariants > 1 ? `Re-gen cell ${i + 1} → ${regenVariants} candidates to pick` : regenPrompt.trim() ? `Re-gen cell ${i + 1} with your custom prompt` : `Re-gen cell ${i + 1} (default clean + extend)`}
                    style={{ position: "absolute", top: 2, right: 2, padding: "1px 6px", fontSize: 11, background: "rgba(0,0,0,0.55)" }}
                  >↻</button>
                  {cells[i] ? (
                    <div className="nodrag nopan" style={{ display: "flex", alignItems: "center", gap: 2, marginTop: 2, width: "100%", minWidth: 0, boxSizing: "border-box", overflow: "hidden" }}>
                      <select
                        value={assign.charId ?? ""}
                        onChange={(e) => void onSelectChar(i, e.target.value)}
                        title="Assign this cell's character → feeds its frozen refs directly (bypasses CCIP). Pick ＋ New to define one."
                        style={{ flex: "1 1 0%", minWidth: 0, width: 0, fontSize: 9, padding: "1px 2px" }}
                      >
                        <option value="">— char</option>
                        {(characterRefs ?? []).map((c) => (
                          <option key={c.id} value={c.id}>{c.name}</option>
                        ))}
                        <option value="__new__">＋ New character…</option>
                      </select>
                      <button
                        className="comic-btn comic-btn--sm"
                        onClick={() => toggleForce(i)}
                        disabled={!assign.charId}
                        title="Force the canon ref to OVERRIDE the source's face/hair + outfit (use when the panel is off-model). Off = gentle hint, source stays faithful."
                        style={{ flexShrink: 0, padding: "1px 4px", fontSize: 10, opacity: forced ? 1 : 0.45, fontWeight: forced ? 700 : 400 }}
                      >🔒</button>
                      <button
                        className="comic-btn comic-btn--sm"
                        onClick={() => promoteCell(i)}
                        disabled={!assign.charId || !cells[i]}
                        title={assign.charId ? "⭐ Bless this cell as a frozen reference for the chosen character (newest first)" : "Pick a character first"}
                        style={{ flexShrink: 0, padding: "1px 4px", fontSize: 10 }}
                      >{promoted === i ? "✓" : "⭐"}</button>
                    </div>
                  ) : null}
                  {cells[i] && camTag ? (
                    <p
                      title="Director camera tags (shot scale · facing). Drive which sheet view is attached; 🔒 is auto-disabled on back/profile."
                      style={{ fontSize: 8, opacity: 0.55, margin: "1px 0 0", textAlign: "center" }}
                    >🎬 {camTag}</p>
                  ) : null}
                  {cells[i] ? (
                    <p
                      className="nodrag"
                      onClick={() => editEnv(i)}
                      title={assign.env ? `Scene setting (click to edit): ${assign.env}` : "Set a scene setting for this cell"}
                      style={{ fontSize: 8, opacity: assign.env ? 0.55 : 0.35, margin: 0, textAlign: "center", whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis", cursor: "pointer" }}
                    >🏞 {assign.env || "set scene ✎"}</p>
                  ) : null}
                </div>
              );
            })}
          </div>
        </>
      )}

      {(mediaId || cells.some((c) => typeof c === "string" && c)) && (
        <div style={{ display: "flex", alignItems: "center", gap: 5, fontSize: 10 }} title="Download resolution — 2K/4K upscale via Flow (Ultra/paid)">
          <span style={{ opacity: 0.7 }}>Quality</span>
          {(["1K", "2K", "4K"] as const).map((r) => (
            <button
              key={r}
              className="comic-btn comic-btn--sm"
              onClick={() => setDlRes(r)}
              disabled={upscaling}
              style={{ flex: 1, opacity: dlRes === r ? 1 : 0.5, fontWeight: dlRes === r ? 700 : 400 }}
              title={r === "1K" ? "Original size" : `Upscaled to ${r} via Flow`}
            >{r}</button>
          ))}
        </div>
      )}

      {panels.length > 0 && (
        <div style={{ display: "flex", gap: 6, flexWrap: "wrap" }}>
          <button className="comic-btn" onClick={downloadRaw} disabled={upscaling} style={{ flex: "1 1 30%" }} title="Download the raw (un-cleaned) source panels (1K)">
            ⬇ Raw panels
          </button>
          {mediaId && (
            <button className="comic-btn" onClick={downloadCombined} disabled={upscaling} style={{ flex: "1 1 30%" }} title="Download the combined 2×2 image at the chosen quality">
              {upscaling ? "Upscaling…" : `⬇ 2×2 ${dlRes}`}
            </button>
          )}
          {cells.some((c) => typeof c === "string" && c) && (
            <button className="comic-btn" onClick={downloadCells} disabled={upscaling} style={{ flex: "1 1 30%" }} title="Download the 4 cleaned cells at the chosen quality">
              {upscaling ? "Upscaling…" : `⬇ 4 cells ${dlRes}`}
            </button>
          )}
        </div>
      )}

      {Object.values(cellHistory).some((h) => Array.isArray(h) && h.length > 1) && (
        <>
          <button
            className="comic-btn"
            onClick={() => setShowHistory((v) => !v)}
            title="Show every image generated for each cell — click one to restore it"
          >
            🕘 {showHistory ? "Hide history" : "History"}
          </button>
          {showHistory && (
            <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
              {[0, 1, 2, 3].map((i) => {
                const hist = (cellHistory[String(i)] ?? []).filter(
                  (x): x is string => typeof x === "string" && !!x,
                );
                if (hist.length === 0) return null;
                return (
                  <div key={i}>
                    <p style={{ fontSize: 9, opacity: 0.55, margin: "0 0 2px" }}>Cell {i + 1} · {hist.length}</p>
                    <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr 1fr", gap: 3 }}>
                      {hist.map((cid, k) => {
                        const isCurrent = cells[i] === cid;
                        return (
                          <img
                            key={k}
                            className="nodrag"
                            src={mediaUrl(cid)}
                            alt={`cell ${i + 1} v${k + 1}`}
                            loading="lazy"
                            onClick={() => { if (!isCurrent) void pickCandidate(i, cid); }}
                            title={isCurrent ? "Current" : "Restore this version"}
                            style={{
                              width: "100%", borderRadius: 3, display: "block",
                              cursor: isCurrent ? "default" : "pointer", background: "var(--border)",
                              outline: isCurrent ? "2px solid #6aa3ff" : "1px solid transparent",
                              outlineOffset: -2, opacity: isCurrent ? 1 : 0.85,
                            }}
                          />
                        );
                      })}
                    </div>
                  </div>
                );
              })}
            </div>
          )}
        </>
      )}

      <button className="comic-btn" onClick={run} disabled={isBusy || regenning.length > 0 || panels.length === 0} title="Clean each panel then stitch a 2×2 9:16">
        {isBusy ? "Working…" : regenning.length > 0 ? `Re-generating ${regenning.length} cell(s)…` : mediaId ? "Re-combine (all 4)" : "Combine → 2×2 9:16"}
      </button>
      {errorMsg && status === "error" && (
        <p className="brief-hint" style={{ color: "#ef4444", fontSize: 11 }}>⚠ {errorMsg}</p>
      )}
    </div>
  );
}
