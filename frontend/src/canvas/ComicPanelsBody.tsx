import { type FlowboardNodeData } from "../store/board";
import { mediaUrl } from "../api/client";
import {
  createRequest,
  getUpstreamComicData,
  runComicRequest,
  type PageItem,
  type PanelItem,
} from "./comicShared";

/**
 * Node 3 — Extracted Panels. Reads the (auto-detected, possibly hand-edited)
 * panel boxes from the upstream Detect node and crops each one into an
 * individual panel image. Output (data.panels) is ready for downstream
 * clean / enhance nodes.
 */
export function ComicPanelsBody({ rfId, data }: { rfId: string; data: FlowboardNodeData }) {
  const panels = (Array.isArray(data.panels) ? data.panels : []) as PanelItem[];
  const panelCount = typeof data.panelCount === "number" ? data.panelCount : panels.length;
  const status = typeof data.status === "string" ? data.status : "idle";
  const isBusy = status === "queued" || status === "running";
  const errorMsg = typeof data.error === "string" ? data.error : undefined;

  const upstream = getUpstreamComicData(rfId);
  const upstreamPages = (Array.isArray(upstream?.pages) ? upstream?.pages : []) as PageItem[];
  const upstreamBoxes = upstreamPages.reduce((n, p) => n + (Array.isArray(p.boxes) ? p.boxes.length : 0), 0);
  const ready = upstreamBoxes > 0;

  function extract() {
    if (isBusy || !ready) return;
    const dbId = parseInt(rfId, 10);
    runComicRequest(
      rfId,
      () => createRequest({ type: "crop_panels", node_id: dbId, params: { pages: upstreamPages } }),
      (result) => ({
        panels: (result.panels as PanelItem[]) ?? [],
        panelCount: (result.panel_count as number) ?? 0,
      }),
    );
  }

  return (
    <div className="node-body node-body--comic-panels" style={{ display: "flex", flexDirection: "column", gap: 8 }}>
      <label style={{ fontSize: 11, opacity: 0.75 }}>3 · Extract panels</label>

      {!ready && (
        <p style={{ fontSize: 11, opacity: 0.7, margin: 0 }}>
          Connect a <b>Detect</b> node with panel boxes upstream.
        </p>
      )}
      <button
        className="comic-btn"
        onClick={extract}
        disabled={isBusy || !ready}
        title="Crop each panel box into its own image"
        style={{ fontSize: 12, padding: "4px 10px", alignSelf: "flex-start" }}
      >
        {isBusy ? "Extracting…" : `Extract ${upstreamBoxes || ""} panels`}
      </button>

      {errorMsg && status === "error" && (
        <p className="brief-hint" style={{ color: "#ef4444", fontSize: 11 }}>⚠ {errorMsg}</p>
      )}
      {panelCount > 0 && (
        <p style={{ fontSize: 11, opacity: 0.8, margin: 0 }}>{panelCount} panel{panelCount === 1 ? "" : "s"}</p>
      )}

      {panels.length > 0 && (
        <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fill, minmax(72px, 1fr))", gap: 6, maxHeight: 280, overflowY: "auto" }}>
          {panels.map((p) => (
            <figure key={p.idx} style={{ margin: 0 }} title={`${p.pageName ?? ""} · panel ${(p.panelIndex ?? 0) + 1}`}>
              <img src={mediaUrl(p.mediaId)} alt={`panel ${p.idx + 1}`} loading="lazy" style={{ width: "100%", borderRadius: 4, display: "block", background: "var(--border)" }} />
              <figcaption style={{ fontSize: 9, opacity: 0.6, textAlign: "center" }}>#{p.idx + 1}</figcaption>
            </figure>
          ))}
        </div>
      )}
    </div>
  );
}
