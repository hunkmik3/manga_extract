import { type FlowboardNodeData } from "../store/board";
import { mediaUrl } from "../api/client";
import { useAppModeStore } from "../store/appMode";
import {
  createRequest,
  getUpstreamComicData,
  patchComicNode,
  runComicRequest,
  type PageItem,
  type PanelItem,
} from "./comicShared";

/** One Grok-cleaned bubble result (from the clean_bubbles handler). */
interface CleanedItem {
  idx: number;
  sourceMediaId?: string;
  status: "cleaned" | "error";
  mediaId?: string;
  error?: string;
}

/**
 * Node 3 — Extracted Panels. Reads the (auto-detected, possibly hand-edited)
 * panel boxes from the upstream Detect node and crops each one into an
 * individual panel image. Output (data.panels) is ready for downstream
 * clean / enhance nodes.
 */
export function ComicPanelsBody({ rfId, data }: { rfId: string; data: FlowboardNodeData }) {
  const bubble = useAppModeStore((s) => s.mode) === "bubble";
  const noun = bubble ? "bubble" : "panel";
  const panels = (Array.isArray(data.panels) ? data.panels : []) as PanelItem[];
  const panelCount = typeof data.panelCount === "number" ? data.panelCount : panels.length;
  const cleaned = (Array.isArray(data.cleaned) ? data.cleaned : []) as CleanedItem[];
  const cleanedCount = typeof data.cleanedCount === "number" ? data.cleanedCount : cleaned.filter((c) => c.status === "cleaned").length;
  const cleanEngine = typeof data.cleanEngine === "string" ? data.cleanEngine : "atrium";
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
        cleaned: [],
        cleanedCount: 0,
      }),
    );
  }

  // Bubble Extract — final step: send every cropped bubble through Grok's
  // faithful image-edit endpoint (keep text, green bg, close bubble, sharpen).
  function cleanBubbles() {
    if (isBusy || panels.length === 0) return;
    const dbId = parseInt(rfId, 10);
    runComicRequest(
      rfId,
      () => createRequest({ type: "clean_bubbles", node_id: dbId, params: { panels, engine: cleanEngine } }),
      (result) => ({
        cleaned: (result.cleaned as CleanedItem[]) ?? [],
        cleanedCount: (result.cleaned_count as number) ?? 0,
      }),
    );
  }

  return (
    <div className="node-body node-body--comic-panels" style={{ display: "flex", flexDirection: "column", gap: 8 }}>
      <label style={{ fontSize: 11, opacity: 0.75 }}>3 · Extract {noun}s</label>

      {!ready && (
        <p style={{ fontSize: 11, opacity: 0.7, margin: 0 }}>
          Connect a <b>Detect</b> node with {noun} boxes upstream.
        </p>
      )}
      <button
        className="comic-btn"
        onClick={extract}
        disabled={isBusy || !ready}
        title={`Crop each ${noun} box into its own image`}
        style={{ fontSize: 12, padding: "4px 10px", alignSelf: "flex-start" }}
      >
        {isBusy ? "Extracting…" : `Extract ${upstreamBoxes || ""} ${noun}s`}
      </button>

      {errorMsg && status === "error" && (
        <p className="brief-hint" style={{ color: "#ef4444", fontSize: 11 }}>⚠ {errorMsg}</p>
      )}
      {panelCount > 0 && (
        <p style={{ fontSize: 11, opacity: 0.8, margin: 0 }}>{panelCount} {noun}{panelCount === 1 ? "" : "s"}</p>
      )}

      {panels.length > 0 && (
        <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fill, minmax(72px, 1fr))", gap: 6, maxHeight: 280, overflowY: "auto" }}>
          {panels.map((p) => (
            <figure key={p.idx} style={{ margin: 0 }} title={`${p.pageName ?? ""} · ${noun} ${(p.panelIndex ?? 0) + 1}`}>
              <img src={mediaUrl(p.mediaId)} alt={`${noun} ${p.idx + 1}`} loading="lazy" style={{ width: "100%", borderRadius: 4, display: "block", background: "var(--border)" }} />
              <figcaption style={{ fontSize: 9, opacity: 0.6, textAlign: "center" }}>#{p.idx + 1}</figcaption>
            </figure>
          ))}
        </div>
      )}

      {bubble && panels.length > 0 && (
        <>
          <hr style={{ width: "100%", border: 0, borderTop: "1px solid var(--border)", margin: "4px 0" }} />
          <label style={{ fontSize: 11, opacity: 0.75 }}>4 · Clean bubbles → transparent PNG</label>
          <div style={{ display: "flex", alignItems: "center", gap: 6, flexWrap: "wrap" }}>
            <select
              value={cleanEngine}
              onChange={(e) => patchComicNode(rfId, { cleanEngine: e.target.value })}
              disabled={isBusy}
              title="Image-edit engine. Background removal is always done in code (chroma-key)."
              style={{ fontSize: 11, padding: "2px 4px" }}
            >
              <option value="atrium">Atrium (Gemini 3 Pro)</option>
              <option value="gemini">Gemini 3 Pro (direct key)</option>
              <option value="grok">Grok (quality)</option>
            </select>
            <button
              className="comic-btn"
              onClick={cleanBubbles}
              disabled={isBusy}
              title="Edit each bubble (keep text, solid green, close bubble, sharpen) then chroma-key the green to transparent"
              style={{ fontSize: 12, padding: "4px 10px" }}
            >
              {isBusy ? "Cleaning…" : `✨ Clean ${panels.length} bubble${panels.length === 1 ? "" : "s"}`}
            </button>
          </div>
          {cleaned.length > 0 && (
            <p style={{ fontSize: 11, opacity: 0.8, margin: 0 }}>
              {cleanedCount}/{cleaned.length} cleaned
              {cleaned.length - cleanedCount > 0 && <span style={{ color: "#ef4444" }}> · {cleaned.length - cleanedCount} failed</span>}
            </p>
          )}
          {cleaned.length > 0 && (
            <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fill, minmax(80px, 1fr))", gap: 6, maxHeight: 320, overflowY: "auto" }}>
              {cleaned.map((c) =>
                c.status === "cleaned" && c.mediaId ? (
                  <figure key={c.idx} style={{ margin: 0 }} title={`bubble ${c.idx + 1} · cleaned`}>
                    <a href={mediaUrl(c.mediaId)} download={`bubble-${c.idx + 1}.png`} target="_blank" rel="noreferrer" title="Transparent PNG — click to download">
                      <img
                        src={mediaUrl(c.mediaId)}
                        alt={`cleaned bubble ${c.idx + 1}`}
                        loading="lazy"
                        style={{
                          width: "100%",
                          borderRadius: 4,
                          display: "block",
                          // checkerboard so the transparent background is visible
                          backgroundColor: "#fff",
                          backgroundImage:
                            "linear-gradient(45deg,#ccc 25%,transparent 25%),linear-gradient(-45deg,#ccc 25%,transparent 25%),linear-gradient(45deg,transparent 75%,#ccc 75%),linear-gradient(-45deg,transparent 75%,#ccc 75%)",
                          backgroundSize: "12px 12px",
                          backgroundPosition: "0 0,0 6px,6px -6px,-6px 0",
                        }}
                      />
                    </a>
                    <figcaption style={{ fontSize: 9, opacity: 0.6, textAlign: "center" }}>#{c.idx + 1}</figcaption>
                  </figure>
                ) : (
                  <figure key={c.idx} style={{ margin: 0 }} title={c.error ?? "failed"}>
                    <div style={{ width: "100%", aspectRatio: "1", borderRadius: 4, background: "rgba(239,68,68,0.12)", display: "flex", alignItems: "center", justifyContent: "center", fontSize: 16 }}>⚠</div>
                    <figcaption style={{ fontSize: 9, opacity: 0.6, textAlign: "center" }}>#{c.idx + 1}</figcaption>
                  </figure>
                ),
              )}
            </div>
          )}
        </>
      )}
    </div>
  );
}
