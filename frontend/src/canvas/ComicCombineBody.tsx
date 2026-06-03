import { ensureBoardProject, mediaUrl } from "../api/client";
import { useBoardStore, type FlowboardNodeData } from "../store/board";
import { createRequest, patchComicNode, runComicRequest } from "./comicShared";

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
  if (!p.pageMediaId || !p.w || !p.h || !p.box?.w || !p.box?.h) {
    return <div style={{ background: "var(--border)", borderRadius: 3 }} />;
  }
  return (
    <div style={{ width: "100%", aspectRatio: "1 / 1", overflow: "hidden", position: "relative", borderRadius: 3, background: "var(--border)" }}>
      <img
        src={mediaUrl(p.pageMediaId)}
        alt=""
        draggable={false}
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
 * Combine node — takes 4 consecutive panels (reading order) and produces ONE
 * vertical 9:16 image with the panels in a clean 2×2 grid (text removed,
 * characters kept faithful, backgrounds extended) via the Flow bridge.
 */
export function ComicCombineBody({ rfId, data }: { rfId: string; data: FlowboardNodeData }) {
  const panels = (Array.isArray(data.panels) ? data.panels : []) as PanelSpec[];
  const mediaId = typeof data.mediaId === "string" ? data.mediaId : "";
  const status = typeof data.status === "string" ? data.status : "idle";
  const isBusy = status === "queued" || status === "running";
  const errorMsg = typeof data.error === "string" ? data.error : undefined;

  async function run() {
    if (isBusy || panels.length === 0) return;
    const boardId = useBoardStore.getState().boardId;
    if (boardId == null) return;
    const dbId = parseInt(rfId, 10);
    let projectId: string;
    try {
      projectId = (await ensureBoardProject(boardId)).flow_project_id;
    } catch (err) {
      patchComicNode(rfId, { status: "error", error: `project: ${String(err)}` });
      return;
    }
    const specs = panels.map((p) => ({ page_media_id: p.pageMediaId, box: p.box }));
    runComicRequest(
      rfId,
      () => createRequest({ type: "combine_panels", node_id: dbId, params: { project_id: projectId, panels: specs } }),
      (result) => ({ mediaId: (result.mediaId as string) ?? "", width: result.width, height: result.height }),
    );
  }

  return (
    <div className="node-body node-body--comic-combine" style={{ display: "flex", flexDirection: "column", gap: 6 }}>
      <label style={{ fontSize: 11, opacity: 0.75 }}>Combine 2×2 · {panels.length} panels</label>

      {mediaId ? (
        <img src={mediaUrl(mediaId)} alt="2x2 storyboard" loading="lazy" style={{ width: "100%", borderRadius: 4, display: "block", background: "var(--border)" }} />
      ) : (
        <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 3 }}>
          {[0, 1, 2, 3].map((i) => (panels[i] ? <CellCrop key={i} p={panels[i]} /> : <div key={i} style={{ aspectRatio: "1 / 1", background: "var(--border)", borderRadius: 3, opacity: 0.4 }} />))}
        </div>
      )}

      <button className="comic-btn" onClick={run} disabled={isBusy || panels.length === 0} title="Combine into one 9:16 2×2 image via the bridge">
        {isBusy ? "Combining…" : mediaId ? "Re-combine" : "Combine → 2×2 9:16"}
      </button>
      {errorMsg && status === "error" && (
        <p className="brief-hint" style={{ color: "#ef4444", fontSize: 11 }}>⚠ {errorMsg}</p>
      )}
    </div>
  );
}
