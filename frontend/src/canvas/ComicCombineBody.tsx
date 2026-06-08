import { useEffect, useRef, useState, type MouseEvent } from "react";
import { ensureBoardProject, mediaUrl } from "../api/client";
import { useBoardStore, type FlowboardNodeData } from "../store/board";
import {
  createRequest,
  findCharacterDb,
  patchComicNode,
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
  const [regenning, setRegenning] = useState<number[]>([]); // cells queued/in-flight
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

  // Keep the freshest values for the async re-gen queue (avoids stale closures).
  const latestRef = useRef({ panels, cells, mediaId, useCharacterRefs, characterRefs, regenPrompt });
  latestRef.current = { panels, cells, mediaId, useCharacterRefs, characterRefs, regenPrompt };

  async function project(): Promise<string | null> {
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
    const specs = panels.map((p) => ({ page_media_id: p.pageMediaId, box: p.box }));
    const params: Record<string, unknown> = { project_id: projectId, panels: specs };
    if (useCharacterRefs && characterRefs?.length) params.characters = characterRefs;
    runComicRequest(
      rfId,
      () => createRequest({ type: "combine_panels", node_id: parseInt(rfId, 10), params }),
      (result) => ({ mediaId: (result.mediaId as string) ?? "", cells: (result.cells as (string | null)[]) ?? [], width: result.width, height: result.height }),
    );
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
      const panel = { page_media_id: p.pageMediaId, box: p.box };
      const params: Record<string, unknown> = { project_id: projectId, panel, cells: working, index: i };
      if (L.useCharacterRefs && L.characterRefs?.length) params.characters = L.characterRefs;
      if (L.regenPrompt.trim()) params.prompt = L.regenPrompt.trim();
      try {
        const result = await runRequestToResult(createRequest({ type: "regen_cell", node_id: parseInt(rfId, 10), params }));
        working = (result.cells as (string | null)[]) ?? working;
        patchComicNode(rfId, {
          mediaId: (result.mediaId as string) ?? L.mediaId,
          cells: working,
          width: result.width,
          height: result.height,
          status: "done",
          error: undefined,
        });
      } catch (err) {
        patchComicNode(rfId, { status: "error", error: `cell ${i + 1}: ${String(err)}` });
      } finally {
        setRegenning((prev) => prev.filter((x) => x !== i));
      }
    }
    processingRef.current = false;
  }

  function triggerDownload(url: string, name: string) {
    const a = document.createElement("a");
    a.href = url;
    a.download = name;
    document.body.appendChild(a);
    a.click();
    a.remove();
  }

  function downloadCombined(e: MouseEvent<HTMLButtonElement>) {
    e.stopPropagation();
    if (!mediaId) return;
    triggerDownload(mediaUrl(mediaId), `comic-combine-${data.shortId ?? rfId}.png`);
  }

  function downloadCells(e: MouseEvent<HTMLButtonElement>) {
    e.stopPropagation();
    // Save each cleaned cell as its own file. Stagger the clicks so the browser
    // doesn't drop the later ones as "concurrent download" spam.
    let n = 0;
    cells.forEach((cid, i) => {
      if (typeof cid !== "string" || !cid) return;
      const delay = n++ * 250;
      const url = mediaUrl(cid);
      setTimeout(() => triggerDownload(url, `comic-combine-${data.shortId ?? rfId}-cell-${i + 1}.png`), delay);
    });
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
      {characterRefs?.length ? (
        <label style={{ fontSize: 10, opacity: 0.72, margin: 0, display: "flex", alignItems: "center", gap: 5 }}>
          <input
            type="checkbox"
            checked={useCharacterRefs}
            onChange={(e) => patchComicNode(rfId, { useCharacterRefs: e.target.checked })}
          />
          Use Character DB refs · {characterRefs.length}
        </label>
      ) : null}

      {mediaId ? (
        <img src={mediaUrl(mediaId)} alt="2x2 storyboard" loading="lazy" style={{ width: "100%", borderRadius: 4, display: "block", background: "var(--border)" }} />
      ) : (
        <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 3 }}>
          {[0, 1, 2, 3].map((i) => (panels[i] ? <CellCrop key={i} p={panels[i]} /> : <div key={i} style={{ aspectRatio: "1 / 1", background: "var(--border)", borderRadius: 3, opacity: 0.4 }} />))}
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
          <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 4 }}>
            {[0, 1, 2, 3].map((i) => {
              const busyCell = regenning.includes(i);
              const waiting = queueRef.current.includes(i); // still in queue = not the active one
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
                  <button
                    className="comic-btn comic-btn--sm"
                    onClick={() => regenCell(i)}
                    disabled={isBusy || busyCell || !panels[i]}
                    title={regenPrompt.trim() ? `Re-gen cell ${i + 1} with your custom prompt` : `Re-gen cell ${i + 1} (default clean + extend)`}
                    style={{ position: "absolute", top: 2, right: 2, padding: "1px 6px", fontSize: 11, background: "rgba(0,0,0,0.55)" }}
                  >↻</button>
                </div>
              );
            })}
          </div>
        </>
      )}

      {panels.length > 0 && (
        <div style={{ display: "flex", gap: 6, flexWrap: "wrap" }}>
          <button className="comic-btn" onClick={downloadRaw} style={{ flex: "1 1 30%" }} title="Download the raw (un-cleaned) source panels">
            ⬇ Raw panels
          </button>
          {mediaId && (
            <button className="comic-btn" onClick={downloadCombined} style={{ flex: "1 1 30%" }} title="Download the single combined 2×2 image">
              ⬇ 2×2 image
            </button>
          )}
          {cells.some((c) => typeof c === "string" && c) && (
            <button className="comic-btn" onClick={downloadCells} style={{ flex: "1 1 30%" }} title="Download the 4 cleaned cells as separate images">
              ⬇ 4 cells
            </button>
          )}
        </div>
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
