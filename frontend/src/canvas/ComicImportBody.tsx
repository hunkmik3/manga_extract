import { useEffect, useRef, useState } from "react";
import { useReactFlow } from "@xyflow/react";
import { useBoardStore, type FlowboardNodeData } from "../store/board";
import { createNodesBulk, mediaUrl, uploadComicPages, type BulkNodeInput } from "../api/client";
import {
  createRequest,
  downstreamPageNodes,
  nodePosition,
  patchComicNode,
  runComicRequest,
  runRequestToResult,
  syncPanelsForPage,
  PAGE_GAP_X,
  PAGE_W,
  PAGE_H,
  PANEL_COLS,
  PANEL_CELL_H,
  ROW_GAP,
  type BoxItem,
  type PageItem,
} from "./comicShared";

const PAGE_EXTS = [".webp", ".png", ".jpg", ".jpeg", ".bmp"];
const cleanPath = (v: string) => v.trim().replace(/^['"]+/, "").replace(/['"]+$/, "").trim();

/**
 * Node 1 — Comic Upload + fan-out controls. Imports a folder of pages, then:
 *   ① "Create page nodes"  → one comic_page node per page (auto-detected boxes,
 *      hand-editable on each node).
 *   ② "Create all panels"  → one comic_panel node per box across all page nodes
 *      (uses the possibly hand-edited boxes).
 */
export function ComicImportBody({ rfId, data }: { rfId: string; data: FlowboardNodeData }) {
  const folder = typeof data.folder === "string" ? data.folder : "";
  const pages = (Array.isArray(data.pages) ? data.pages : []) as PageItem[];
  const pageCount = typeof data.pageCount === "number" ? data.pageCount : pages.length;
  const detector = typeof data.detector === "string" ? data.detector : "auto";
  const status = typeof data.status === "string" ? data.status : "idle";
  const isImporting = status === "queued" || status === "running";

  const [draftFolder, setDraftFolder] = useState(folder);
  const [busy, setBusy] = useState<null | "pages" | "panels" | "combine">(null);
  const [spawnErr, setSpawnErr] = useState<string | undefined>();
  const dirInputRef = useRef<HTMLInputElement | null>(null);
  const rf = useReactFlow();

  // After spawning, pan/zoom the canvas to the freshly created nodes so the
  // user actually sees them (otherwise they land far off-screen).
  function focusNew(nodes: { id: number }[]) {
    if (nodes.length === 0) return;
    const ids = nodes.map((n) => ({ id: String(n.id) }));
    setTimeout(() => { try { rf.fitView({ nodes: ids, padding: 0.25, duration: 600 }); } catch { /* noop */ } }, 80);
  }

  useEffect(() => {
    const el = dirInputRef.current;
    if (el) { el.setAttribute("webkitdirectory", ""); el.setAttribute("directory", ""); }
  }, []);

  const applyPages = (result: Record<string, unknown>) => ({
    pages: (result.pages as PageItem[]) ?? [],
    pageCount: (result.page_count as number) ?? 0,
  });

  function persistFolder(value: string) {
    const cleaned = cleanPath(value);
    if (cleaned !== draftFolder) setDraftFolder(cleaned);
    patchComicNode(rfId, { folder: cleaned });
    return cleaned;
  }

  function importFromPath() {
    const f = persistFolder(draftFolder);
    if (!f || isImporting) return;
    runComicRequest(rfId, () => createRequest({ type: "import_pages", node_id: parseInt(rfId, 10), params: { folder: f } }), applyPages);
  }

  function handleUpload(e: React.ChangeEvent<HTMLInputElement>) {
    const all = Array.from(e.target.files ?? []);
    e.target.value = "";
    const imgs = all.filter((f) => PAGE_EXTS.some((ext) => f.name.toLowerCase().endsWith(ext))).sort((a, b) => a.name.localeCompare(b.name));
    if (imgs.length === 0) { patchComicNode(rfId, { status: "error", error: "No page images in that folder" }); return; }
    runComicRequest(rfId, () => uploadComicPages(imgs, { nodeId: parseInt(rfId, 10) }), applyPages);
  }

  // ① spawn one comic_page node per page (with auto-detected boxes)
  async function spawnPages() {
    if (busy || pages.length === 0) return;
    const boardId = useBoardStore.getState().boardId;
    if (boardId == null) return;
    const uploadDb = parseInt(rfId, 10);
    const pos = nodePosition(rfId);
    setSpawnErr(undefined);
    setBusy("pages");
    try {
      const result = await runRequestToResult(createRequest({ type: "detect_page_panels", node_id: uploadDb, params: { pages, detector } }));
      const detPages = (result.pages as PageItem[]) ?? [];
      // Pages stacked in a single vertical column. Each row is tall enough to
      // hold that page's panels beside it (laid out in spawnPanels), so panels
      // never collide with the next page → a readable per-page grouping.
      const pageX = pos.x + PAGE_GAP_X;
      let curY = pos.y;
      const bulk: BulkNodeInput[] = detPages.map((p) => {
        const nBoxes = p.boxes?.length ?? 0;
        const panelRows = Math.max(1, Math.ceil(nBoxes / PANEL_COLS));
        const rowH = Math.max(PAGE_H, panelRows * PANEL_CELL_H) + ROW_GAP;
        const node: BulkNodeInput = {
          type: "comic_page",
          x: pageX,
          y: curY,
          w: PAGE_W,
          h: PAGE_H,
          data: { pageMediaId: p.mediaId, pageName: p.name, pageIdx: p.idx, w: p.w, h: p.h, boxes: p.boxes ?? [], detector, title: p.name },
          source_id: uploadDb,
        };
        curY += rowH;
        return node;
      });
      const res = await createNodesBulk(boardId, bulk);
      useBoardStore.getState().appendNodesBulk(res.nodes, res.edges);
      focusNew(res.nodes);
    } catch (e) {
      setSpawnErr(String(e));
    } finally {
      setBusy(null);
    }
  }

  // ② materialize one comic_panel node per box across all downstream page nodes
  async function spawnPanels() {
    if (busy) return;
    const pageNodes = downstreamPageNodes(rfId);
    if (pageNodes.length === 0) { setSpawnErr("Create page nodes first"); return; }
    setSpawnErr(undefined);
    setBusy("panels");
    try {
      // Sync each page's panels to its boxes (creates missing, removes stale).
      // Idempotent — safe to click repeatedly. Per-box live sync also runs on
      // every box edit, so this is just the initial bulk materialization.
      for (const pn of pageNodes) {
        await syncPanelsForPage(String(pn.dbId));
      }
      patchComicNode(rfId, { panelsMaterialized: true });
      setTimeout(() => { try { rf.fitView({ duration: 600 }); } catch { /* noop */ } }, 80);
    } catch (e) {
      setSpawnErr(String(e));
    } finally {
      setBusy(null);
    }
  }

  // ③ flatten panels in reading order → groups of 4 → one Combine (2×2) node each
  async function spawnCombine() {
    if (busy) return;
    const boardId = useBoardStore.getState().boardId;
    if (boardId == null) return;
    const uploadDb = parseInt(rfId, 10);
    const pageNodes = downstreamPageNodes(rfId);
    if (pageNodes.length === 0) { setSpawnErr("Create page nodes first"); return; }
    setSpawnErr(undefined);
    setBusy("combine");
    try {
      const store = useBoardStore.getState();
      const sorted = [...pageNodes].sort((a, b) => ((a.data.pageIdx as number) ?? 0) - ((b.data.pageIdx as number) ?? 0));
      const flat: Array<Record<string, unknown>> = [];
      const pageIds = new Set(pageNodes.map((p) => String(p.dbId)));
      const panelLayerExists =
        Boolean(store.nodes.find((n) => n.id === rfId)?.data.panelsMaterialized)
        || store.edges.some((e) => {
          const tgt = store.nodes.find((n) => n.id === e.target);
          return pageIds.has(e.source) && tgt?.data.type === "comic_panel";
        });

      if (panelLayerExists) {
        for (const pn of sorted) {
          const boxes = ((pn.data.boxes as BoxItem[]) ?? []).filter((b) => b?.id);
          const panelByBoxId = new Map<string, string>();
          for (const e of store.edges) {
            if (e.source !== String(pn.dbId)) continue;
            const panel = store.nodes.find((n) => n.id === e.target);
            const boxId = panel?.data.boxId;
            if (panel?.data.type === "comic_panel" && typeof boxId === "string") {
              panelByBoxId.set(boxId, panel.id);
            }
          }
          boxes.forEach((b, j) => {
            if (!panelByBoxId.has(b.id)) return;
            flat.push({
              pageMediaId: pn.data.pageMediaId,
              box: b,
              w: pn.data.w,
              h: pn.data.h,
              pageName: pn.data.pageName,
              panelIndex: j,
              boxId: b.id,
            });
          });
        }
      } else {
        for (const pn of sorted) {
          const boxes = (pn.data.boxes as BoxItem[]) ?? [];
          boxes.forEach((b, j) =>
            flat.push({
              pageMediaId: pn.data.pageMediaId, box: b, w: pn.data.w, h: pn.data.h,
              pageName: pn.data.pageName, panelIndex: j,
            }),
          );
        }
      }
      if (flat.length === 0) {
        setSpawnErr(panelLayerExists ? "No current panel nodes — create or keep at least one panel" : "No panel boxes — detect/draw first");
        return;
      }
      const pos = nodePosition(rfId);
      const bulk: BulkNodeInput[] = [];
      for (let i = 0; i < flat.length; i += 4) {
        bulk.push({
          type: "comic_combine",
          x: pos.x + PAGE_GAP_X + 820,
          y: pos.y + (i / 4) * 700,
          w: 300,
          h: 560,
          data: { panels: flat.slice(i, i + 4), title: `2x2 #${i / 4 + 1}` },
          source_id: uploadDb,
        });
      }
      const res = await createNodesBulk(boardId, bulk);
      useBoardStore.getState().appendNodesBulk(res.nodes, res.edges);
      focusNew(res.nodes);
    } catch (e) {
      setSpawnErr(String(e));
    } finally {
      setBusy(null);
    }
  }

  return (
    <div className="node-body node-body--comic-import" style={{ display: "flex", flexDirection: "column", gap: 8 }}>
      <label style={{ fontSize: 11, opacity: 0.75 }}>1 · Upload comic pages</label>

      <input ref={dirInputRef} type="file" multiple accept="image/*" style={{ display: "none" }} onChange={handleUpload} />
      <button className="comic-btn" onClick={() => dirInputRef.current?.click()} disabled={isImporting} style={{ fontSize: 12, padding: "4px 10px" }}>
        {isImporting ? "Importing…" : "⬆ Upload folder"}
      </button>
      <input
        type="text" value={draftFolder} placeholder="…or paste a server-side path" spellCheck={false}
        onChange={(e) => setDraftFolder(e.target.value)} onBlur={(e) => persistFolder(e.target.value)}
        onKeyDown={(e) => { if (e.key === "Enter") importFromPath(); }}
        style={{ width: "100%", boxSizing: "border-box", fontSize: 12, padding: "4px 6px" }}
      />

      {pageCount > 0 && (
        <>
          <p style={{ fontSize: 11, opacity: 0.85, margin: 0 }}>{pageCount} pages imported</p>
          <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
            <label style={{ fontSize: 11, display: "flex", alignItems: "center", gap: 3 }}>
              Detector
              <select value={detector} onChange={(e) => patchComicNode(rfId, { detector: e.target.value })} style={{ fontSize: 11, padding: "2px 4px" }}>
                <option value="heuristic">Heuristic</option>
                <option value="ml">YOLO</option>
                <option value="webtoon">Webtoon</option>
                <option value="auto">Auto</option>
              </select>
            </label>
          </div>
          <button className="comic-btn" onClick={spawnPages} disabled={busy !== null} style={{ fontSize: 12, padding: "4px 10px" }}>
            {busy === "pages" ? "Creating pages…" : `① Create ${pageCount} page nodes`}
          </button>
          <button className="comic-btn" onClick={spawnPanels} disabled={busy !== null} style={{ fontSize: 12, padding: "4px 10px" }} title="Crop every page's boxes into individual panel nodes">
            {busy === "panels" ? "Creating panels…" : "② Create all panel nodes"}
          </button>
          <button className="comic-btn" onClick={spawnCombine} disabled={busy !== null} style={{ fontSize: 12, padding: "4px 10px" }} title="Group panels in reading order into 2×2 storyboard images (4 per group)">
            {busy === "combine" ? "Creating combine…" : "③ Combine 2×2 (groups of 4)"}
          </button>
        </>
      )}

      {spawnErr && <p className="brief-hint" style={{ color: "#ef4444", fontSize: 11 }}>⚠ {spawnErr}</p>}
      {status === "error" && typeof data.error === "string" && (
        <p className="brief-hint" style={{ color: "#ef4444", fontSize: 11 }}>⚠ {data.error}</p>
      )}

      {pages.length > 0 && (
        <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fill, minmax(56px, 1fr))", gap: 5, maxHeight: 160, overflowY: "auto" }}>
          {pages.slice(0, 40).map((p) => (
            <img key={p.idx} src={mediaUrl(p.mediaId)} alt={p.name} loading="lazy" title={p.name} style={{ width: "100%", borderRadius: 3, display: "block", background: "var(--border)" }} />
          ))}
        </div>
      )}
    </div>
  );
}
