import { type FlowboardNodeData } from "../store/board";
import { mediaUrl } from "../api/client";
import {
  createRequest,
  getUpstreamComicData,
  runComicRequest,
  type CharacterItem,
  type PageItem,
} from "./comicShared";

function characterErrorView(errorMsg: string | undefined) {
  if (!errorMsg) return null;
  if (errorMsg.startsWith("ml_unavailable")) {
    return {
      title: "ML extras missing",
      message: "Character DB needs torch, ultralytics, dghs-imgutils, and onnxruntime.",
      command: 'cd agent && uv pip install --python .venv/bin/python -e ".[ml]"',
      detail: errorMsg.replace(/^ml_unavailable:\s*/, ""),
    };
  }
  return {
    title: "Character DB failed",
    message: errorMsg,
    command: undefined,
    detail: errorMsg,
  };
}

/**
 * Character DB node — detects + clusters characters across the whole comic
 * (manga109 + CCIP) and shows one entry per character. Connect it to a Comic
 * upload node. The enhance nodes auto-read this DB to keep each character's
 * identity/costume consistent.
 */
export function ComicCharsBody({ rfId, data }: { rfId: string; data: FlowboardNodeData }) {
  const characters = (Array.isArray(data.characters) ? data.characters : []) as CharacterItem[];
  const status = typeof data.status === "string" ? data.status : "idle";
  const isBusy = status === "queued" || status === "running";
  const errorMsg = typeof data.error === "string" ? data.error : undefined;
  const displayError = status === "error" ? characterErrorView(errorMsg) : null;
  const emptyDone = status === "done" && characters.length === 0;

  const upstream = getUpstreamComicData(rfId);
  const pages = (Array.isArray(upstream?.pages) ? upstream?.pages : []) as PageItem[];
  const ready = pages.length > 0;

  function build() {
    if (isBusy || !ready) return;
    runComicRequest(
      rfId,
      () => createRequest({ type: "build_character_db", node_id: parseInt(rfId, 10), params: { pages } }),
      (result) => ({ characters: (result.characters as CharacterItem[]) ?? [] }),
    );
  }

  return (
    <div className="node-body node-body--comic-chars" style={{ display: "flex", flexDirection: "column", gap: 8 }}>
      <label style={{ fontSize: 11, opacity: 0.75 }}>Character DB (consistency refs)</label>

      {!ready && (
        <p style={{ fontSize: 11, opacity: 0.7, margin: 0 }}>Connect a <b>Comic upload</b> node upstream.</p>
      )}
      <button className="comic-btn" onClick={build} disabled={isBusy || !ready} title="Detect + cluster characters across the comic">
        {isBusy ? "Building… (detect + cluster)" : characters.length ? "Rebuild" : `Build from ${pages.length} pages`}
      </button>

      {isBusy && (
        <div className="comic-status-note">
          <strong>Working</strong>
          <span>Detecting character bodies and clustering refs. First run can take a minute while models warm up.</span>
        </div>
      )}
      {emptyDone && (
        <div className="comic-status-note comic-status-note--empty">
          <strong>No character refs found</strong>
          <span>The run finished, but no reusable character clusters were produced.</span>
        </div>
      )}
      {displayError && (
        <div className="comic-error-note" title={displayError.detail}>
          <strong>{displayError.title}</strong>
          <span>{displayError.message}</span>
          {displayError.command && <code>{displayError.command}</code>}
        </div>
      )}
      {characters.length > 0 && (
        <p style={{ fontSize: 11, opacity: 0.8, margin: 0 }}>{characters.length} character{characters.length === 1 ? "" : "s"} → auto-used by Enhance</p>
      )}

      <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
        {characters.map((c) => (
          <div key={c.id} style={{ display: "flex", alignItems: "center", gap: 8 }}>
            <img src={mediaUrl(c.sampleMediaId)} alt={c.name} loading="lazy" style={{ width: 44, height: 56, objectFit: "cover", borderRadius: 4, background: "var(--border)" }} />
            <div style={{ fontSize: 11, lineHeight: 1.3 }}>
              <div>{c.name}</div>
              <div style={{ opacity: 0.6 }}>{c.count} appearances · {c.refMediaIds.length} refs</div>
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}
