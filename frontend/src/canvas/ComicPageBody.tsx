import { useEffect, useRef, useState } from "react";
import { useBoardStore, type FlowboardNodeData } from "../store/board";
import {
  createRequest,
  patchComicNode,
  relayoutComicChains,
  runRequestToResult,
  syncPanelsForPage,
  type BoxItem,
  type PageItem,
} from "./comicShared";
import { PanelBoxEditor } from "./PanelBoxEditor";

/**
 * Node — one comic page (fan-out). Shows its page with auto-detected panel
 * boxes and lets the user hand-correct them (draw / move / resize / delete).
 * The edited boxes feed the "Create all panel nodes" action on the upload node.
 */
export function ComicPageBody({ rfId, data }: { rfId: string; data: FlowboardNodeData }) {
  const mediaId = typeof data.pageMediaId === "string" ? data.pageMediaId : "";
  const name = typeof data.pageName === "string" ? data.pageName : "page";
  const boxes = (Array.isArray(data.boxes) ? data.boxes : []) as BoxItem[];
  const detector = typeof data.detector === "string" ? data.detector : "auto";
  const page: PageItem = {
    idx: (data.pageIdx as number) ?? 0,
    name,
    mediaId,
    w: (data.w as number) ?? 0,
    h: (data.h as number) ?? 0,
    boxes,
  };
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | undefined>();
  const rootRef = useRef<HTMLDivElement | null>(null);
  const lastHeightRef = useRef(0);
  const relayoutTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  // Tall webtoon pages make this node much taller than the default — report the
  // measured height so relayoutComicChains spaces the pages by their real size
  // (otherwise the next page node overlaps this one).
  useEffect(() => {
    const card = rootRef.current?.closest(".node-card") as HTMLElement | null;
    if (!card) return;
    const observer = new ResizeObserver((entries) => {
      const next = Math.ceil(entries[0]?.contentRect.height ?? 0);
      if (!Number.isFinite(next) || next <= 0) return;
      if (Math.abs(next - lastHeightRef.current) < 4) return;
      lastHeightRef.current = next;
      useBoardStore.getState().updateNodeData(rfId, { __measuredHeight: next });
      if (relayoutTimerRef.current) clearTimeout(relayoutTimerRef.current);
      relayoutTimerRef.current = setTimeout(() => relayoutComicChains(), 80);
    });
    observer.observe(card);
    return () => {
      observer.disconnect();
      if (relayoutTimerRef.current) clearTimeout(relayoutTimerRef.current);
    };
  }, [rfId]);

  function onBoxes(next: BoxItem[]) {
    patchComicNode(rfId, { boxes: next });
    // Live-sync this page's panel nodes (node 3): new box → new panel appears,
    // deleted box → its panel is removed.
    void syncPanelsForPage(rfId);
  }

  async function redetect() {
    if (busy || !mediaId) return;
    setErr(undefined);
    setBusy(true);
    try {
      const result = await runRequestToResult(
        createRequest({ type: "detect_page_panels", node_id: parseInt(rfId, 10), params: { pages: [{ idx: page.idx, name, mediaId, w: page.w, h: page.h }], detector } }),
      );
      const p = (result.pages as PageItem[])?.[0];
      patchComicNode(rfId, { boxes: p?.boxes ?? [] });
      void syncPanelsForPage(rfId);
    } catch (e) {
      setErr(String(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div ref={rootRef} className="node-body node-body--comic-page" style={{ display: "flex", flexDirection: "column", gap: 6 }}>
      <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", fontSize: 11 }}>
        <span title={name} style={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{name}</span>
        <span style={{ opacity: 0.7 }}>{boxes.length} box</span>
      </div>
      {mediaId ? (
        <PanelBoxEditor page={page} onChange={onBoxes} />
      ) : (
        <p style={{ fontSize: 11, opacity: 0.7 }}>No page image.</p>
      )}
      <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
        <select value={detector} onChange={(e) => patchComicNode(rfId, { detector: e.target.value })} style={{ fontSize: 11, padding: "2px 4px" }}>
          <option value="heuristic">Heuristic</option>
          <option value="ml">YOLO</option>
          <option value="webtoon">Webtoon</option>
          <option value="hybrid">Hybrid (ML+webtoon)</option>
          <option value="auto">Auto</option>
        </select>
        <button className="comic-btn comic-btn--sm" onClick={redetect} disabled={busy} style={{ fontSize: 11, padding: "3px 8px" }}>
          {busy ? "Detecting…" : "Re-detect"}
        </button>
      </div>
      <p style={{ fontSize: 9, opacity: 0.55, margin: 0 }}>Right-click = Add box · drag empty = new · drag = move · corner = resize · × = delete</p>
      {err && <p className="brief-hint" style={{ color: "#ef4444", fontSize: 11 }}>⚠ {err}</p>}
    </div>
  );
}
