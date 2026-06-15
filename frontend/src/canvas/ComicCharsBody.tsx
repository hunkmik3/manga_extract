import { useRef, useState } from "react";
import { type FlowboardNodeData } from "../store/board";
import { mediaUrl, uploadComicSheet } from "../api/client";
import {
  createRequest,
  getUpstreamComicData,
  patchComicNode,
  runComicRequest,
  runRequestToResult,
  type CharacterItem,
  type PageItem,
} from "./comicShared";

interface SheetView {
  kind: "face" | "body" | "back";
  mediaId: string;
  keep: boolean;
}

const KIND_CYCLE: SheetView["kind"][] = ["face", "body", "back"];

function loadImageFile(file: File): Promise<HTMLImageElement> {
  return new Promise((resolve, reject) => {
    const url = URL.createObjectURL(file);
    const img = new Image();
    img.onload = () => { URL.revokeObjectURL(url); resolve(img); };
    img.onerror = (e) => { URL.revokeObjectURL(url); reject(e); };
    img.src = url;
  });
}

/** Shrink a big sheet in-browser before upload: a turnaround sheet can be 8-20 MB
 * / 5000+ px, far past the upload cap and far more than segmentation/refs need.
 * Caps the long side and re-encodes to JPEG. Falls back to the original on any
 * canvas error or when the image is already small. */
async function downscaleForUpload(file: File, maxDim = 2560, quality = 0.9): Promise<File> {
  try {
    const img = await loadImageFile(file);
    const scale = Math.min(1, maxDim / Math.max(img.width, img.height));
    if (scale >= 1 && file.size <= 4 * 1024 * 1024) return file; // small enough already
    const w = Math.max(1, Math.round(img.width * scale));
    const h = Math.max(1, Math.round(img.height * scale));
    const canvas = document.createElement("canvas");
    canvas.width = w;
    canvas.height = h;
    const ctx = canvas.getContext("2d");
    if (!ctx) return file;
    ctx.drawImage(img, 0, 0, w, h);
    const blob: Blob | null = await new Promise((res) => canvas.toBlob((b) => res(b), "image/jpeg", quality));
    if (!blob) return file;
    return new File([blob], file.name.replace(/\.[^.]+$/, "") + ".jpg", { type: "image/jpeg" });
  } catch {
    return file;
  }
}

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

  const fileRef = useRef<HTMLInputElement | null>(null);
  const [sheetBusy, setSheetBusy] = useState(false);
  const [sheetErr, setSheetErr] = useState<string | undefined>();
  const [pendingViews, setPendingViews] = useState<SheetView[]>([]);
  const [sheetName, setSheetName] = useState("");

  // Project STYLE FRAME — a uniform target art style applied to every panel.
  const styleRef = typeof data.styleRefMediaId === "string" ? data.styleRefMediaId : "";
  const styleDescriptor = typeof data.styleDescriptor === "string" ? data.styleDescriptor : "";
  const styleFileRef = useRef<HTMLInputElement | null>(null);
  const [styleBusy, setStyleBusy] = useState(false);

  async function onPickStyle(file: File | undefined) {
    if (!file) return;
    setStyleBusy(true);
    try {
      const small = await downscaleForUpload(file);
      const { media_id } = await uploadComicSheet(small);
      patchComicNode(rfId, { styleRefMediaId: media_id });
    } catch {
      /* ignore — user can retry */
    } finally {
      setStyleBusy(false);
    }
  }

  function build() {
    if (isBusy || !ready) return;
    runComicRequest(
      rfId,
      () => createRequest({ type: "build_character_db", node_id: parseInt(rfId, 10), params: { pages } }),
      (result) => ({ characters: (result.characters as CharacterItem[]) ?? [] }),
    );
  }

  // Upload a turnaround/model sheet → auto-crop into face/body views to review.
  async function onPickSheet(file: File | undefined) {
    if (!file) return;
    setSheetBusy(true);
    setSheetErr(undefined);
    setPendingViews([]);
    try {
      const small = await downscaleForUpload(file);
      const { media_id } = await uploadComicSheet(small);
      const result = await runRequestToResult(
        createRequest({ type: "segment_character_sheet", node_id: parseInt(rfId, 10), params: { media_id } }),
      );
      const views = (result.views as Array<{ kind: string; mediaId: string }>) ?? [];
      if (!views.length) setSheetErr("No views detected — is the sheet on a plain background?");
      setPendingViews(views.map((v) => ({ kind: v.kind === "body" ? "body" : "face", mediaId: v.mediaId, keep: true })));
    } catch (err) {
      setSheetErr(String(err));
    } finally {
      setSheetBusy(false);
    }
  }

  function toggleView(i: number) {
    setPendingViews((prev) => prev.map((v, j) => (j === i ? { ...v, keep: !v.keep } : v)));
  }

  // Cycle a view's kind label (face → body → back) — lets the user mark the
  // sheet's back-of-head crop as "back" so back-facing panels get the right ref.
  function cycleKind(i: number) {
    setPendingViews((prev) => prev.map((v, j) => {
      if (j !== i) return v;
      const next = KIND_CYCLE[(KIND_CYCLE.indexOf(v.kind) + 1) % KIND_CYCLE.length];
      return { ...v, kind: next };
    }));
  }

  // Create a new character from the kept views (faces first → primacy for
  // close-ups; bodies for wide shots). These become its frozen canon refs, with
  // their view kinds stored so the Director can pick by panel camera.
  function createFromViews() {
    const kept = pendingViews.filter((v) => v.keep);
    if (!kept.length) return;
    const kindRank = (k: SheetView["kind"]) => (k === "face" ? 0 : k === "body" ? 1 : 2);
    const ordered = [...kept].sort((a, b) => kindRank(a.kind) - kindRank(b.kind));
    const refMediaIds = ordered.map((v) => v.mediaId);
    const ids = new Set(characters.map((c) => c.id));
    let n = characters.length;
    let id = `char_${n}`;
    while (ids.has(id)) id = `char_${++n}`;
    const newChar: CharacterItem = {
      id,
      name: sheetName.trim() || `Character ${characters.length + 1}`,
      count: 0,
      refMediaIds,
      sampleMediaId: refMediaIds[0],
      refViews: ordered.map((v) => ({ mediaId: v.mediaId, kind: v.kind })),
    };
    patchComicNode(rfId, { characters: [...characters, newChar] });
    setPendingViews([]);
    setSheetName("");
  }

  // Persist a character's canonical descriptor (reused verbatim in prompts).
  function setDescriptor(charId: string, value: string) {
    const next = characters.map((c) => (c.id === charId ? { ...c, descriptor: value } : c));
    patchComicNode(rfId, { characters: next });
  }

  return (
    <div className="node-body node-body--comic-chars" style={{ display: "flex", flexDirection: "column", gap: 8 }}>
      <label style={{ fontSize: 11, opacity: 0.75 }}>Character DB (consistency refs)</label>

      {!ready && (
        <p style={{ fontSize: 11, opacity: 0.7, margin: 0 }}>Connect a <b>Comic upload</b> node upstream.</p>
      )}
      <button className="comic-btn" onClick={build} disabled={isBusy || !ready} title="Detect + cluster characters across the comic (CCIP — unreliable on some art)">
        {isBusy ? "Building… (detect + cluster)" : characters.length ? "Rebuild" : `Build from ${pages.length} pages`}
      </button>

      {/* Project STYLE FRAME — a uniform target art style fed into every panel
          gen (ref image + text descriptor). Applies across the whole chapter. */}
      <div style={{ display: "flex", flexDirection: "column", gap: 6, borderTop: "1px solid var(--border)", paddingTop: 8 }}>
        <label style={{ fontSize: 10, opacity: 0.72 }}>🎨 Style frame (applied to every panel):</label>
        <input
          ref={styleFileRef}
          type="file"
          accept="image/*"
          style={{ display: "none" }}
          onChange={(e) => { void onPickStyle(e.target.files?.[0]); e.target.value = ""; }}
        />
        <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
          {styleRef ? (
            <img src={mediaUrl(styleRef)} alt="style frame" loading="lazy" style={{ width: 48, height: 48, objectFit: "cover", borderRadius: 4, background: "var(--border)" }} />
          ) : null}
          <button
            className="comic-btn"
            disabled={styleBusy}
            onClick={() => styleFileRef.current?.click()}
            title="Upload one look reference — its art style (line work, shading, palette) is applied to every panel; its content is NOT copied in."
            style={{ flex: 1 }}
          >
            {styleBusy ? "Uploading…" : styleRef ? "↻ Replace style frame" : "⬆ Upload style frame"}
          </button>
          {styleRef ? (
            <button className="comic-btn comic-btn--sm" title="Remove style frame" onClick={() => patchComicNode(rfId, { styleRefMediaId: undefined })}>✕</button>
          ) : null}
        </div>
        <input
          defaultValue={styleDescriptor}
          onBlur={(e) => { if ((e.target.value || "") !== styleDescriptor) patchComicNode(rfId, { styleDescriptor: e.target.value }); }}
          placeholder="Style descriptor (optional) — e.g. 'flat cel-shaded webtoon, soft pastel palette, clean line art'"
          spellCheck={false}
          style={{ fontSize: 10, padding: "3px 6px", width: "100%", boxSizing: "border-box" }}
        />
      </div>

      {/* Define a character from a reference / turnaround sheet — auto-cropped
          into face + full-body views (no CCIP needed). */}
      <div style={{ display: "flex", flexDirection: "column", gap: 6, borderTop: "1px solid var(--border)", paddingTop: 8 }}>
        <label style={{ fontSize: 10, opacity: 0.72 }}>Or define a character from a sheet:</label>
        <input
          ref={fileRef}
          type="file"
          accept="image/*"
          style={{ display: "none" }}
          onChange={(e) => { void onPickSheet(e.target.files?.[0]); e.target.value = ""; }}
        />
        <button
          className="comic-btn"
          disabled={sheetBusy}
          onClick={() => fileRef.current?.click()}
          title="Upload a turnaround / model sheet — it's auto-cropped into face + full-body views you keep as this character's frozen refs"
        >
          {sheetBusy ? "Segmenting…" : "⬆ Upload character sheet"}
        </button>
        {sheetErr && <p style={{ color: "#ef4444", fontSize: 10, margin: 0 }}>⚠ {sheetErr}</p>}

        {pendingViews.length > 0 && (
          <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
            <input
              value={sheetName}
              onChange={(e) => setSheetName(e.target.value)}
              placeholder="Character name"
              style={{ fontSize: 11, padding: "3px 6px", boxSizing: "border-box", width: "100%" }}
            />
            <p style={{ fontSize: 9, opacity: 0.55, margin: 0 }}>Tap a view to keep / skip. Faces feed close-ups, bodies feed wide shots.</p>
            <div style={{ display: "grid", gridTemplateColumns: "repeat(4, 1fr)", gap: 4 }}>
              {pendingViews.map((v, i) => (
                <div
                  key={i}
                  onClick={() => toggleView(i)}
                  title={`${v.kind} — ${v.keep ? "kept (click to skip)" : "skipped (click to keep)"}. Click the label to change face/body/back.`}
                  style={{
                    position: "relative", cursor: "pointer", borderRadius: 3, overflow: "hidden",
                    outline: v.keep ? "2px solid #6aa3ff" : "1px solid var(--border)", outlineOffset: -2,
                    opacity: v.keep ? 1 : 0.4,
                  }}
                >
                  <img src={mediaUrl(v.mediaId)} alt={v.kind} loading="lazy" style={{ width: "100%", display: "block", background: "var(--border)" }} />
                  <span
                    onClick={(e) => { e.stopPropagation(); cycleKind(i); }}
                    title="Click to relabel: face → body → back (back = the back-of-head view, used for back-facing panels)"
                    style={{ position: "absolute", top: 1, left: 1, fontSize: 8, padding: "0 3px", borderRadius: 2, background: "rgba(0,0,0,0.75)", color: "#fff", cursor: "pointer" }}
                  >{v.kind} ↻</span>
                </div>
              ))}
            </div>
            <div style={{ display: "flex", gap: 6 }}>
              <button className="comic-btn" style={{ flex: 1 }} disabled={!pendingViews.some((v) => v.keep)} onClick={createFromViews}>
                ✓ Create character ({pendingViews.filter((v) => v.keep).length})
              </button>
              <button className="comic-btn" style={{ flex: 1 }} onClick={() => { setPendingViews([]); setSheetName(""); }}>✕ Cancel</button>
            </div>
          </div>
        )}
      </div>

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

      <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
        {characters.map((c) => (
          <div key={c.id} style={{ display: "flex", flexDirection: "column", gap: 3 }}>
            <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
              <img src={mediaUrl(c.sampleMediaId)} alt={c.name} loading="lazy" style={{ width: 44, height: 56, objectFit: "cover", borderRadius: 4, background: "var(--border)" }} />
              <div style={{ fontSize: 11, lineHeight: 1.3 }}>
                <div>{c.name}</div>
                <div style={{ opacity: 0.6 }}>
                  {c.count} appearances · {c.refMediaIds.length} refs
                  {Array.isArray(c.refViews) && c.refViews.length
                    ? ` · ${c.refViews.map((v) => v.kind).join("/")}`
                    : ""}
                </div>
              </div>
            </div>
            <input
              defaultValue={c.descriptor ?? ""}
              onBlur={(e) => { if ((e.target.value || "") !== (c.descriptor ?? "")) setDescriptor(c.id, e.target.value); }}
              placeholder="Descriptor (verbatim in every prompt) — e.g. 'young man, black messy hair, yellow military cap and uniform'"
              spellCheck={false}
              title="Canonical appearance description. Reused word-for-word in every prompt this character appears in — consistent wording measurably improves cross-panel consistency."
              style={{ fontSize: 10, padding: "3px 6px", width: "100%", boxSizing: "border-box", opacity: 0.9 }}
            />
          </div>
        ))}
      </div>
    </div>
  );
}
