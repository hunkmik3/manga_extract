import { useEffect, useRef, useState } from "react";
import { useBoardStore, type FlowboardNodeData } from "../store/board";
import {
  createRequest,
  getRequest,
  mediaUrl,
  patchNode,
  uploadComicPages,
  type RequestDTO,
} from "../api/client";

// Shape mirrors the extract_panels worker result (worker/processor.py).
interface PanelItem {
  idx: number;
  pageIndex: number;
  pageName: string;
  panelIndex: number;
  box: { x: number; y: number; w: number; h: number };
  mediaId: string;
  status?: string;
}
interface PageItem {
  pageIndex: number;
  pageName: string;
  panelCount: number;
  debugMediaId?: string | null;
  error?: string | null;
}

const POLL_MS = 1200;
const PAGE_EXTS = [".webp", ".png", ".jpg", ".jpeg", ".bmp"];

// Strip surrounding whitespace + quotes — pasting a path from Finder/shell
// often wraps it in quotes, which made the backend report "not a directory".
function cleanPath(value: string): string {
  return value.trim().replace(/^['"]+/, "").replace(/['"]+$/, "").trim();
}

/**
 * Node 1 — Import & Extract Panels. Either type a server-side folder path OR
 * upload a folder of pages from the browser; both dispatch the `extract_panels`
 * worker task (pure OpenCV, no Flow bridge) and render the resulting crops.
 * Self-contained request/poll loop so it doesn't perturb the image/video path.
 */
export function ComicImportBody({ rfId, data }: { rfId: string; data: FlowboardNodeData }) {
  const update = useBoardStore((s) => s.updateNodeData);

  const folder = typeof data.folder === "string" ? data.folder : "";
  const debug = Boolean(data.debug);
  const detector = typeof data.detector === "string" ? data.detector : "heuristic";
  const panels = (Array.isArray(data.panels) ? data.panels : []) as PanelItem[];
  const pages = (Array.isArray(data.pages) ? data.pages : []) as PageItem[];
  const panelCount = typeof data.panelCount === "number" ? data.panelCount : panels.length;
  const pageCount = typeof data.pageCount === "number" ? data.pageCount : pages.length;

  const status = typeof data.status === "string" ? data.status : "idle";
  const isBusy = status === "queued" || status === "running";
  const errorMsg = typeof data.error === "string" ? data.error : undefined;

  const [draftFolder, setDraftFolder] = useState(folder);
  const dirInputRef = useRef<HTMLInputElement | null>(null);

  const dbId = parseInt(rfId, 10);

  // <input webkitdirectory> isn't typed in React's JSX — set it on the element.
  useEffect(() => {
    const el = dirInputRef.current;
    if (el) {
      el.setAttribute("webkitdirectory", "");
      el.setAttribute("directory", "");
    }
  }, []);

  function persistFolder(value: string) {
    const cleaned = cleanPath(value);
    if (cleaned !== draftFolder) setDraftFolder(cleaned);
    update(rfId, { folder: cleaned });
    if (!Number.isNaN(dbId)) {
      patchNode(dbId, { data: { folder: cleaned } }).catch(() => {});
    }
    return cleaned;
  }

  function toggleDebug(next: boolean) {
    update(rfId, { debug: next });
    if (!Number.isNaN(dbId)) {
      patchNode(dbId, { data: { debug: next } }).catch(() => {});
    }
  }

  function setDetector(next: string) {
    update(rfId, { detector: next });
    if (!Number.isNaN(dbId)) {
      patchNode(dbId, { data: { detector: next } }).catch(() => {});
    }
  }

  // Shared dispatch: optimistic status, then poll the request to completion.
  async function dispatch(makeReq: () => Promise<RequestDTO>) {
    if (isBusy || Number.isNaN(dbId)) return;
    update(rfId, { status: "queued", error: undefined });
    try {
      const req = await makeReq();
      update(rfId, { status: "running" });

      const poll = async () => {
        try {
          const r = await getRequest(req.id);
          if (r.status === "done") {
            const res = r.result as {
              panels?: PanelItem[];
              pages?: PageItem[];
              panel_count?: number;
              page_count?: number;
            };
            const patch = {
              panels: res.panels ?? [],
              pages: res.pages ?? [],
              panelCount: res.panel_count ?? (res.panels?.length ?? 0),
              pageCount: res.page_count ?? (res.pages?.length ?? 0),
            };
            update(rfId, { status: "done", error: undefined, ...patch });
            patchNode(dbId, { status: "done", data: patch }).catch(() => {});
          } else if (r.status === "failed" || r.status === "timeout" || r.status === "canceled") {
            const msg = r.error ?? "extract_failed";
            update(rfId, { status: "error", error: msg });
            patchNode(dbId, { status: "error" }).catch(() => {});
          } else {
            setTimeout(poll, POLL_MS);
          }
        } catch (err) {
          update(rfId, { status: "error", error: String(err) });
        }
      };
      setTimeout(poll, 800);
    } catch (err) {
      update(rfId, { status: "error", error: String(err) });
    }
  }

  function extractFromPath() {
    const f = persistFolder(draftFolder);
    if (!f) return;
    dispatch(() => createRequest({
      type: "extract_panels",
      node_id: dbId,
      params: { folder: f, debug, detector },
    }));
  }

  async function handleUpload(e: React.ChangeEvent<HTMLInputElement>) {
    const all = Array.from(e.target.files ?? []);
    e.target.value = ""; // allow re-picking the same folder
    const imgs = all
      .filter((f) => PAGE_EXTS.some((ext) => f.name.toLowerCase().endsWith(ext)))
      .sort((a, b) => a.name.localeCompare(b.name));
    if (imgs.length === 0) {
      update(rfId, { status: "error", error: "No page images in that folder" });
      return;
    }
    dispatch(() => uploadComicPages(imgs, { nodeId: dbId, debug, detector }));
  }

  return (
    <div className="node-body node-body--comic-import" style={{ display: "flex", flexDirection: "column", gap: 8 }}>
      <label style={{ fontSize: 11, opacity: 0.75 }}>Comic pages — upload a folder, or type a path</label>

      <div style={{ display: "flex", gap: 6 }}>
        <input
          ref={dirInputRef}
          type="file"
          multiple
          accept="image/*"
          style={{ display: "none" }}
          onChange={handleUpload}
        />
        <button
          className="node-header__btn"
          onClick={() => dirInputRef.current?.click()}
          disabled={isBusy}
          title="Pick a folder of comic pages to upload"
          style={{ fontSize: 12, padding: "4px 10px", flex: 1 }}
        >
          {isBusy ? "Working…" : "⬆ Upload folder"}
        </button>
      </div>

      <input
        type="text"
        value={draftFolder}
        placeholder="…or paste a server-side path: /path/to/pages"
        spellCheck={false}
        onChange={(e) => setDraftFolder(e.target.value)}
        onBlur={(e) => persistFolder(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === "Enter") extractFromPath();
        }}
        style={{ width: "100%", boxSizing: "border-box", fontSize: 12, padding: "4px 6px" }}
      />

      <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: 8 }}>
        <label style={{ fontSize: 11, display: "flex", alignItems: "center", gap: 4 }}>
          <input type="checkbox" checked={debug} onChange={(e) => toggleDebug(e.target.checked)} />
          Debug overview
        </label>
        <label style={{ fontSize: 11, display: "flex", alignItems: "center", gap: 4 }} title="Panel detector">
          Detector
          <select
            value={detector}
            onChange={(e) => setDetector(e.target.value)}
            style={{ fontSize: 11, padding: "2px 4px" }}
          >
            <option value="heuristic">Heuristic</option>
            <option value="ml">YOLO (ML)</option>
            <option value="auto">Auto (best)</option>
          </select>
        </label>
      </div>

      <button
        className="node-header__btn"
        onClick={extractFromPath}
        disabled={isBusy || cleanPath(draftFolder).length === 0}
        title="Extract panels from the folder path"
        style={{ fontSize: 12, padding: "4px 10px", alignSelf: "flex-end" }}
      >
        {isBusy ? "Extracting…" : "Extract from path"}
      </button>

      {errorMsg && status === "error" && (
        <p className="brief-hint" style={{ color: "#ef4444", fontSize: 11 }}>⚠ {errorMsg}</p>
      )}

      {panelCount > 0 && (
        <p style={{ fontSize: 11, opacity: 0.8, margin: 0 }}>
          {pageCount} page{pageCount === 1 ? "" : "s"} → {panelCount} panel{panelCount === 1 ? "" : "s"}
        </p>
      )}

      {panels.length > 0 && (
        <div
          style={{
            display: "grid",
            gridTemplateColumns: "repeat(auto-fill, minmax(72px, 1fr))",
            gap: 6,
            maxHeight: 260,
            overflowY: "auto",
          }}
        >
          {panels.map((p) => (
            <figure key={p.idx} style={{ margin: 0 }} title={`${p.pageName} · panel ${p.panelIndex + 1}`}>
              <img
                src={mediaUrl(p.mediaId)}
                alt={`panel ${p.idx + 1}`}
                loading="lazy"
                style={{ width: "100%", borderRadius: 4, display: "block", background: "var(--border)" }}
              />
              <figcaption style={{ fontSize: 9, opacity: 0.6, textAlign: "center" }}>#{p.idx + 1}</figcaption>
            </figure>
          ))}
        </div>
      )}

      {debug && pages.some((pg) => pg.debugMediaId) && (
        <details>
          <summary style={{ fontSize: 11, cursor: "pointer", opacity: 0.75 }}>Debug overviews</summary>
          <div style={{ display: "flex", flexDirection: "column", gap: 6, marginTop: 6 }}>
            {pages
              .filter((pg) => pg.debugMediaId)
              .map((pg) => (
                <img
                  key={pg.pageIndex}
                  src={mediaUrl(pg.debugMediaId as string)}
                  alt={`overview ${pg.pageName}`}
                  loading="lazy"
                  style={{ width: "100%", borderRadius: 4 }}
                />
              ))}
          </div>
        </details>
      )}
    </div>
  );
}
