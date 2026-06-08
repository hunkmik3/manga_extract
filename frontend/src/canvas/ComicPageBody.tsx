import { useState } from "react";
import { type FlowboardNodeData } from "../store/board";
import {
  createRequest,
  patchComicNode,
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
    <div className="node-body node-body--comic-page" style={{ display: "flex", flexDirection: "column", gap: 6 }}>
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
