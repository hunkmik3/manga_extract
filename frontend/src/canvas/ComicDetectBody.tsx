import { useState } from "react";
import { type FlowboardNodeData } from "../store/board";
import {
  createRequest,
  getUpstreamComicData,
  patchComicNode,
  runComicRequest,
  type BoxItem,
  type PageItem,
} from "./comicShared";
import { PanelBoxEditor } from "./PanelBoxEditor";

/**
 * Node 2 — Pages & Panel Regions. Auto-detects panel boxes for each upstream
 * page (Heuristic / YOLO / Auto), then lets the user hand-correct them per page
 * (draw / move / resize / delete) before they flow to Node 3. data.pages (with
 * boxes) is the output consumed by the Panels node.
 */
export function ComicDetectBody({ rfId, data }: { rfId: string; data: FlowboardNodeData }) {
  const pages = (Array.isArray(data.pages) ? data.pages : []) as PageItem[];
  const detector = typeof data.detector === "string" ? data.detector : "auto";
  const status = typeof data.status === "string" ? data.status : "idle";
  const isBusy = status === "queued" || status === "running";
  const errorMsg = typeof data.error === "string" ? data.error : undefined;

  const upstream = getUpstreamComicData(rfId);
  const upstreamPages = (Array.isArray(upstream?.pages) ? upstream?.pages : []) as PageItem[];

  const [cur, setCur] = useState(0);
  const totalBoxes = pages.reduce((n, p) => n + (Array.isArray(p.boxes) ? p.boxes.length : 0), 0);
  const idx = Math.min(cur, Math.max(0, pages.length - 1));
  const page = pages[idx];

  function detect() {
    if (isBusy || upstreamPages.length === 0) return;
    const dbId = parseInt(rfId, 10);
    runComicRequest(
      rfId,
      () => createRequest({ type: "detect_page_panels", node_id: dbId, params: { pages: upstreamPages, detector } }),
      (result) => ({ pages: (result.pages as PageItem[]) ?? [], detector: (result.detector as string) ?? detector }),
    );
  }

  function setDetector(next: string) {
    patchComicNode(rfId, { detector: next });
  }

  function onBoxes(pageIdx: number, boxes: BoxItem[]) {
    const next = pages.map((p, i) => (i === pageIdx ? { ...p, boxes } : p));
    patchComicNode(rfId, { pages: next });
  }

  return (
    <div className="node-body node-body--comic-detect" style={{ display: "flex", flexDirection: "column", gap: 8 }}>
      <label style={{ fontSize: 11, opacity: 0.75 }}>2 · Detect panels (auto + manual fix)</label>

      {upstreamPages.length === 0 && pages.length === 0 && (
        <p style={{ fontSize: 11, opacity: 0.7, margin: 0 }}>
          Connect a <b>Comic Upload</b> node upstream.
        </p>
      )}

      <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
        <label style={{ fontSize: 11, display: "flex", alignItems: "center", gap: 4 }} title="Panel detector">
          Detector
          <select value={detector} onChange={(e) => setDetector(e.target.value)} style={{ fontSize: 11, padding: "2px 4px" }}>
            <option value="heuristic">Heuristic</option>
            <option value="ml">YOLO (ML)</option>
            <option value="webtoon">Webtoon</option>
            <option value="auto">Auto (best)</option>
          </select>
        </label>
        <button
          className="comic-btn"
          onClick={detect}
          disabled={isBusy || upstreamPages.length === 0}
          title="Auto-detect panel boxes on every page"
          style={{ fontSize: 12, padding: "4px 10px" }}
        >
          {isBusy ? "Detecting…" : pages.length ? "Re-detect" : `Detect ${upstreamPages.length || ""}`}
        </button>
      </div>

      {errorMsg && status === "error" && (
        <p className="brief-hint" style={{ color: "#ef4444", fontSize: 11 }}>⚠ {errorMsg}</p>
      )}

      {pages.length > 0 && page && (
        <>
          <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", fontSize: 11 }}>
            <button className="comic-btn comic-btn--sm" onClick={() => setCur(Math.max(0, idx - 1))} disabled={idx === 0} style={{ padding: "2px 8px" }}>◀</button>
            <span title={page.name}>
              {page.name} · {idx + 1}/{pages.length} · {(page.boxes?.length ?? 0)} box
            </span>
            <button className="comic-btn comic-btn--sm" onClick={() => setCur(Math.min(pages.length - 1, idx + 1))} disabled={idx === pages.length - 1} style={{ padding: "2px 8px" }}>▶</button>
          </div>
          <p style={{ fontSize: 10, opacity: 0.6, margin: 0 }}>Drag empty area = new box · drag box = move · corner = resize · × = delete</p>
          <PanelBoxEditor page={page} onChange={(boxes) => onBoxes(idx, boxes)} />
          <p style={{ fontSize: 11, opacity: 0.8, margin: 0 }}>{totalBoxes} boxes total → connect a <b>Panels</b> node</p>
        </>
      )}
    </div>
  );
}
