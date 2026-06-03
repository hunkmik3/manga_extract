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
  const panels = (Array.isArray(data.panels) ? data.panels : []) as PanelSpec[];
  const cells = (Array.isArray(data.cells) ? data.cells : []) as (string | null)[];
  const mediaId = typeof data.mediaId === "string" ? data.mediaId : "";
  const status = typeof data.status === "string" ? data.status : "idle";
  const isBusy = status === "queued" || status === "running";
  const errorMsg = typeof data.error === "string" ? data.error : undefined;

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
    runComicRequest(
      rfId,
      () => createRequest({ type: "combine_panels", node_id: parseInt(rfId, 10), params: { project_id: projectId, panels: specs } }),
      (result) => ({ mediaId: (result.mediaId as string) ?? "", cells: (result.cells as (string | null)[]) ?? [], width: result.width, height: result.height }),
    );
  }

  async function regenCell(i: number) {
    if (isBusy || !panels[i]) return;
    const projectId = await project();
    if (!projectId) return;
    const panel = { page_media_id: panels[i].pageMediaId, box: panels[i].box };
    runComicRequest(
      rfId,
      () => createRequest({ type: "regen_cell", node_id: parseInt(rfId, 10), params: { project_id: projectId, panel, cells, index: i } }),
      (result) => ({ mediaId: (result.mediaId as string) ?? mediaId, cells: (result.cells as (string | null)[]) ?? cells, width: result.width, height: result.height }),
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

      {/* per-cell re-gen: only after a combine produced cells */}
      {cells.length > 0 && (
        <>
          <p style={{ fontSize: 9, opacity: 0.55, margin: 0 }}>Re-gen a single cell if one came out wrong:</p>
          <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 4 }}>
            {[0, 1, 2, 3].map((i) => (
              <div key={i} style={{ position: "relative" }}>
                {cells[i] ? (
                  <img src={mediaUrl(cells[i] as string)} alt={`cell ${i + 1}`} loading="lazy" style={{ width: "100%", borderRadius: 3, display: "block", background: "var(--border)" }} />
                ) : (
                  <div style={{ aspectRatio: "9 / 16", background: "var(--border)", borderRadius: 3, opacity: 0.4 }} />
                )}
                <button
                  className="comic-btn comic-btn--sm"
                  onClick={() => regenCell(i)}
                  disabled={isBusy || !panels[i]}
                  title={`Re-generate cell ${i + 1}`}
                  style={{ position: "absolute", top: 2, right: 2, padding: "1px 6px", fontSize: 11, background: "rgba(0,0,0,0.55)" }}
                >↻</button>
              </div>
            ))}
          </div>
        </>
      )}

      <button className="comic-btn" onClick={run} disabled={isBusy || panels.length === 0} title="Clean each panel then stitch a 2×2 9:16">
        {isBusy ? "Working…" : mediaId ? "Re-combine (all 4)" : "Combine → 2×2 9:16"}
      </button>
      {errorMsg && status === "error" && (
        <p className="brief-hint" style={{ color: "#ef4444", fontSize: 11 }}>⚠ {errorMsg}</p>
      )}
    </div>
  );
}
