import { ensureBoardProject, mediaUrl } from "../api/client";
import { useBoardStore, type FlowboardNodeData } from "../store/board";
import { createRequest, findCharacterDb, patchComicNode, resolveUpstreamImage, runComicRequest } from "./comicShared";

/**
 * Phase 4/5 node — runs the Flow bridge (Nano Banana Pro) on the upstream image:
 *   kind="clean"   → remove text/bubbles (+ optional 9:16 extend)
 *   kind="enhance" → re-render as anime
 * Input = a comic_panel (page image + box) or any node with a result mediaId
 * (so panel → clean → enhance chains). Output (data.mediaId) feeds downstream.
 */
export function ComicEditBody({
  rfId,
  data,
  kind,
}: {
  rfId: string;
  data: FlowboardNodeData;
  kind: "clean" | "enhance";
}) {
  const status = typeof data.status === "string" ? data.status : "idle";
  const isBusy = status === "queued" || status === "running";
  const errorMsg = typeof data.error === "string" ? data.error : undefined;
  const mediaId = typeof data.mediaId === "string" ? data.mediaId : "";
  const extend916 = Boolean(data.extend916);

  const up = resolveUpstreamImage(rfId);
  const ready = up !== null;
  const taskType = kind === "clean" ? "clean_panel" : "enhance_panel";
  const titleLabel = kind === "clean" ? "Clean (remove text)" : "Enhance (anime)";

  async function run() {
    if (isBusy || !ready) return;
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
    const params: Record<string, unknown> = { project_id: projectId };
    if (up!.sourceMediaId) {
      params.source_media_id = up!.sourceMediaId; // an already-cleaned image
      if (up!.pageMediaId) params.page_media_id = up!.pageMediaId; // page = reference
    } else {
      params.page_media_id = up!.pageMediaId; // crop the panel from the page
      params.box = up!.box;
    }
    if (kind === "clean") params.extend_916 = extend916;
    if (kind === "enhance") {
      // auto-feed the board's character DB so enhance keeps identity consistent
      const chars = findCharacterDb();
      if (chars) params.characters = chars;
    }
    const pageMediaId = up!.pageMediaId; // persist so a downstream enhance can reference it

    runComicRequest(
      rfId,
      () => createRequest({ type: taskType, node_id: dbId, params }),
      (result) => ({ mediaId: (result.mediaId as string) ?? "", width: result.width, height: result.height, pageMediaId }),
    );
  }

  return (
    <div className={`node-body node-body--comic-${kind}`} style={{ display: "flex", flexDirection: "column", gap: 8 }}>
      <label style={{ fontSize: 11, opacity: 0.75 }}>{kind === "clean" ? "4 · " : "5 · "}{titleLabel}</label>

      {!ready && (
        <p style={{ fontSize: 11, opacity: 0.7, margin: 0 }}>
          Connect a {kind === "clean" ? <b>panel</b> : <><b>Clean</b> node</>} upstream.
        </p>
      )}

      {kind === "clean" && (
        <label style={{ fontSize: 11, display: "flex", alignItems: "center", gap: 4 }}>
          <input type="checkbox" checked={extend916} onChange={(e) => patchComicNode(rfId, { extend916: e.target.checked })} />
          Extend to 9:16
        </label>
      )}

      <button className="comic-btn" onClick={run} disabled={isBusy || !ready} title="Run through the Flow bridge (Nano Banana Pro)">
        {isBusy ? "Running…" : kind === "clean" ? "Clean panel" : "Enhance to anime"}
      </button>

      {errorMsg && status === "error" && (
        <p className="brief-hint" style={{ color: "#ef4444", fontSize: 11 }}>⚠ {errorMsg}</p>
      )}

      {mediaId ? (
        <img src={mediaUrl(mediaId)} alt={titleLabel} loading="lazy" style={{ width: "100%", borderRadius: 4, display: "block", background: "var(--border)" }} />
      ) : (
        ready && !isBusy && <p style={{ fontSize: 10, opacity: 0.55, margin: 0 }}>Run to generate · costs 1 Flow generation</p>
      )}
    </div>
  );
}
