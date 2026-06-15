import { useEffect, useRef, useState } from "react";
import { useReactFlow } from "@xyflow/react";
import { useBoardStore, type FlowboardNodeData } from "../store/board";
import { createNodesBulk, mediaUrl, uploadComicPages, type BulkNodeInput } from "../api/client";
import {
  createRequest,
  downstreamPageNodes,
  findCharacterDb,
  nodePosition,
  patchComicNode,
  relayoutComicChains,
  relayoutComicCombineChains,
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
  const [busy, setBusy] = useState<null | "pages" | "panels" | "combine" | "download" | "assign">(null);
  const [spawnErr, setSpawnErr] = useState<string | undefined>();
  const [assignInfo, setAssignInfo] = useState<string | undefined>();
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

  // Download EVERY panel (detected + hand-adjusted boxes) across all pages as
  // individual image files, bundled into ONE .zip in reading order.
  async function downloadAllPanels() {
    if (busy) return;
    const pageNodes = downstreamPageNodes(rfId);
    if (pageNodes.length === 0) { setSpawnErr("Create page nodes first"); return; }
    setSpawnErr(undefined);
    setBusy("download");
    try {
      const sorted = [...pageNodes].sort((a, b) => ((a.data.pageIdx as number) ?? 0) - ((b.data.pageIdx as number) ?? 0));
      const panels: Array<{ page_media_id: string; box: { x: number; y: number; w: number; h: number } }> = [];
      for (const pn of sorted) {
        const mediaId = pn.data.pageMediaId;
        if (typeof mediaId !== "string" || !mediaId) continue;
        const boxes = ((pn.data.boxes as BoxItem[]) ?? [])
          .slice()
          .sort((a, b) => (a.y - b.y) || (a.x - b.x)); // reading order: top→bottom, left→right
        for (const b of boxes) panels.push({ page_media_id: mediaId, box: { x: b.x, y: b.y, w: b.w, h: b.h } });
      }
      if (panels.length === 0) { setSpawnErr("No panels — detect/adjust boxes first"); setBusy(null); return; }
      const result = await runRequestToResult(
        createRequest({ type: "export_all_panels", node_id: parseInt(rfId, 10), params: { panels } }),
      );
      const mid = result.mediaId as string | undefined;
      if (!mid) { setSpawnErr("Export failed"); return; }
      const a = document.createElement("a");
      a.href = mediaUrl(mid);
      a.download = `comic-all-panels-${data.shortId ?? rfId}.zip`;
      document.body.appendChild(a);
      a.click();
      a.remove();
    } catch (e) {
      setSpawnErr(String(e));
    } finally {
      setBusy(null);
    }
  }

  // 🪄 Director (chapter-wide WHO): run magiv2 locally over every page with the
  // Character DB as a named bank, then write the per-panel character
  // assignments into every combine node's per-cell settings. No Flow calls.
  async function magiAssignChapter() {
    if (busy) return;
    const characterRefs = findCharacterDb();
    if (!characterRefs?.length) { setSpawnErr("Define characters first (Character DB node)"); return; }
    const pageNodes = downstreamPageNodes(rfId);
    if (pageNodes.length === 0) { setSpawnErr("Create page nodes first"); return; }
    setSpawnErr(undefined);
    setAssignInfo(undefined);
    setBusy("assign");
    try {
      const sorted = [...pageNodes].sort((a, b) => ((a.data.pageIdx as number) ?? 0) - ((b.data.pageIdx as number) ?? 0));
      const pagesParam = sorted
        .map((pn) => ({
          media_id: pn.data.pageMediaId as string,
          boxes: ((pn.data.boxes as BoxItem[]) ?? []).map((b) => ({ id: b.id, x: b.x, y: b.y, w: b.w, h: b.h })),
        }))
        .filter((p) => typeof p.media_id === "string" && p.media_id && p.boxes.length > 0);
      if (pagesParam.length === 0) { setSpawnErr("No detected panels — run detection first"); setBusy(null); return; }
      const cast = characterRefs.map((c) => ({ id: c.id, name: c.name, refMediaIds: c.refMediaIds, sampleMediaId: c.sampleMediaId }));
      const result = await runRequestToResult(
        createRequest({ type: "magi_assign_panels", node_id: parseInt(rfId, 10), params: { pages: pagesParam, characters: cast } }),
      );
      const assignments = (result.assignments as Record<string, Record<string, string>>) ?? {};
      // Route the assignments into every combine node's per-cell settings
      // (matched by the cell's page + box identity). Existing manual choices
      // for other fields (🔒/outfit) are preserved.
      const store = useBoardStore.getState();
      let applied = 0;
      for (const n of store.nodes) {
        if (n.data.type !== "comic_combine") continue;
        const panels = Array.isArray(n.data.panels) ? (n.data.panels as Array<Record<string, unknown>>) : [];
        const cur = ((n.data.cellAssign as Record<string, Record<string, unknown>>) ?? {});
        const merged = { ...cur };
        let changed = false;
        panels.slice(0, 4).forEach((p, i) => {
          const pageId = p.pageMediaId as string | undefined;
          const boxId = p.boxId as string | undefined;
          const cid = pageId && boxId ? assignments[pageId]?.[boxId] : undefined;
          if (cid) {
            merged[String(i)] = { ...(merged[String(i)] ?? {}), charId: cid };
            changed = true;
            applied++;
          }
        });
        if (changed) patchComicNode(n.id, { cellAssign: merged });
      }
      const total = Number(result.panels_assigned ?? 0);
      setAssignInfo(`Magi assigned ${total} panel(s) · routed into ${applied} combine cell(s)`);
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
      // Pull each combine's source panels beside it + drop them from the page
      // column (the combine now "owns" their layout).
      setTimeout(() => {
        relayoutComicCombineChains();
        relayoutComicChains();
      }, 0);
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
                <option value="hybrid">Hybrid (ML+webtoon)</option>
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
          <button className="comic-btn" onClick={downloadAllPanels} disabled={busy !== null} style={{ fontSize: 12, padding: "4px 10px" }} title="Crop every panel (detected + hand-adjusted) into separate files, bundled into ONE .zip">
            {busy === "download" ? "Exporting…" : "⬇ Download all panels (.zip)"}
          </button>
          <button
            className="comic-btn"
            onClick={magiAssignChapter}
            disabled={busy !== null}
            style={{ fontSize: 12, padding: "4px 10px" }}
            title="Director (local Magi v2): read the whole chapter with the Character DB as a named bank and auto-assign each panel's character into every combine cell. No Flow calls. First run loads the model (~30s), then ~2-3s per page."
          >
            {busy === "assign" ? "Assigning… (local model)" : "🪄 Auto-assign characters (chapter)"}
          </button>
        </>
      )}

      {assignInfo && <p className="brief-hint" style={{ color: "#22c55e", fontSize: 11 }}>✓ {assignInfo}</p>}
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
