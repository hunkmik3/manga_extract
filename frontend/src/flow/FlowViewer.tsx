import { useCallback, useEffect, useRef, useState } from "react";
import { mediaUrl, thumbUrl, uploadComicSheet } from "../api/client";
import { FLOW_MODELS, useFlowStudioStore, type FlowAsset } from "../store/flowStudio";

/**
 * Flow-style image viewer — a clean full-screen overlay opened by clicking a
 * grid tile. Just the image (with infinity zoom: wheel zooms toward the
 * cursor, drag pans, double-click resets) and a minimal bottom composer to
 * edit it (Nano-Banana-style natural-language edit), exactly like Google Flow.
 *
 * Secondary actions (reference, regenerate, pin, character/scene, download,
 * delete) live in a slim floating toolbar so nothing is lost but the heavy
 * config panel is gone.
 */

interface View {
  scale: number;
  tx: number;
  ty: number;
}
const RESET: View = { scale: 1, tx: 0, ty: 0 };

/** A freehand annotation stroke, in displayed-image (canvas CSS) coordinates. */
interface Stroke {
  color: string;
  size: number;
  pts: { x: number; y: number }[];
}
const BRUSH_COLORS = ["#ff3b30", "#ffcc00", "#34c759", "#0a84ff", "#ffffff", "#111111"];
const COLOR_NAMES: Record<string, string> = {
  "#ff3b30": "red",
  "#ffcc00": "yellow",
  "#34c759": "green",
  "#0a84ff": "blue",
  "#ffffff": "white",
  "#111111": "black",
};

const MIN_SCALE = 0.2;
const DEFAULT_MAX_SCALE = 8;
// GPU hard limit on a single raster surface is ~16384px; on a Retina display
// CSS px are doubled in device px, so a scaled image whose device-pixel size
// crosses that limit renders BLACK. Stay well under it (target ~12000 device
// px) — the per-image cap below is derived from this.
const SAFE_DEVICE_PX = 12000;
const clamp = (v: number, lo: number, hi: number) => Math.min(hi, Math.max(lo, v));

/** Largest zoom that keeps the scaled image's device-pixel size safe for the
 *  GPU. Derived from the VIEWPORT (not the image), so it never depends on image
 *  load timing — the previous image-measured version could race and momentarily
 *  allow a catastrophic cap, which blacked out the layer when panning. The fit
 *  image is at most 88vw × 84vh, so that × scale × dpr is the surface bound. */
function safeMaxScale(): number {
  if (typeof window === "undefined") return DEFAULT_MAX_SCALE;
  const dpr = window.devicePixelRatio || 1;
  const vfit = Math.max(window.innerWidth * 0.88, window.innerHeight * 0.84);
  return clamp(SAFE_DEVICE_PX / dpr / Math.max(1, vfit), 1, 16);
}

/** Keep the view sane: never NaN/Infinity, never zoomed past the GPU-safe cap,
 *  and never panned so far that the image leaves the viewport (so the user can
 *  always drag/zoom it back). */
function clampView(v: View, rect: DOMRect | null, maxScale: number): View {
  let { scale, tx, ty } = v;
  if (!Number.isFinite(scale)) scale = 1;
  if (!Number.isFinite(tx)) tx = 0;
  if (!Number.isFinite(ty)) ty = 0;
  scale = clamp(scale, MIN_SCALE, maxScale);
  if (rect) {
    const limX = (rect.width / 2) * scale + rect.width * 0.35;
    const limY = (rect.height / 2) * scale + rect.height * 0.35;
    tx = clamp(tx, -limX, limX);
    ty = clamp(ty, -limY, limY);
  }
  return { scale, tx, ty };
}

export function FlowViewer() {
  const mediaId = useFlowStudioStore((s) => s.selectedMediaId);
  const assets = useFlowStudioStore((s) => s.assets);
  const asset = useFlowStudioStore((s) =>
    s.assets.find((a) => a.mediaId === s.selectedMediaId) ?? null,
  );
  const close = useFlowStudioStore((s) => s.select);
  const refine = useFlowStudioStore((s) => s.refine);
  const regenerate = useFlowStudioStore((s) => s.regenerate);
  const addRef = useFlowStudioStore((s) => s.addRef);
  const togglePin = useFlowStudioStore((s) => s.togglePin);
  const remove = useFlowStudioStore((s) => s.remove);
  const model = useFlowStudioStore((s) => s.settings.model);
  const setSettings = useFlowStudioStore((s) => s.setSettings);

  const [edit, setEdit] = useState("");
  const [editRefs, setEditRefs] = useState<string[]>([]);
  const [modelOpen, setModelOpen] = useState(false);
  const [view, setView] = useState<View>(RESET);
  const [grabbing, setGrabbing] = useState(false);
  const [fullReady, setFullReady] = useState(false);
  // Browse on a light ~2048 "view" image (fast over a tunnel); upgrade to the
  // full original only once the user zooms in to inspect detail.
  const [hiRes, setHiRes] = useState(false);
  // Freehand annotation ("khoanh vùng"): draw marks on the image to guide the
  // edit; on submit the marks are flattened onto the image and sent as source.
  const [drawMode, setDrawMode] = useState(false);
  const [strokes, setStrokes] = useState<Stroke[]>([]);
  const [brushColor, setBrushColor] = useState(BRUSH_COLORS[0]);
  const [brushSize, setBrushSize] = useState(6);
  const stageRef = useRef<HTMLDivElement>(null);
  const fileRef = useRef<HTMLInputElement>(null);
  const stripRef = useRef<HTMLDivElement>(null);
  const drawRef = useRef<HTMLCanvasElement>(null);
  const curStroke = useRef<Stroke | null>(null);
  const drag = useRef<{ x: number; y: number; tx: number; ty: number } | null>(null);
  const maxScaleRef = useRef(DEFAULT_MAX_SCALE);

  // Reset zoom + hide the (still-loading) full image whenever we switch — the
  // thumbnail placeholder shows instantly until the full image is ready.
  useEffect(() => {
    setView(RESET);
    setFullReady(false);
    setHiRes(false);
    setDrawMode(false);
    setStrokes([]);
  }, [mediaId]);

  // Drawing forces the image to fit (scale 1, no pan) so canvas coords line up.
  useEffect(() => {
    if (drawMode) setView(RESET);
  }, [drawMode]);

  // Redraw the annotation canvas whenever strokes change or we enter draw mode.
  const redrawStrokes = useCallback(() => {
    const cv = drawRef.current;
    if (!cv) return;
    if (cv.width !== cv.clientWidth || cv.height !== cv.clientHeight) {
      cv.width = cv.clientWidth;
      cv.height = cv.clientHeight;
    }
    const ctx = cv.getContext("2d");
    if (!ctx) return;
    ctx.clearRect(0, 0, cv.width, cv.height);
    ctx.lineJoin = "round";
    ctx.lineCap = "round";
    const all = curStroke.current ? [...strokes, curStroke.current] : strokes;
    for (const s of all) {
      if (s.pts.length === 0) continue;
      ctx.strokeStyle = s.color;
      ctx.lineWidth = s.size;
      ctx.beginPath();
      s.pts.forEach((p, i) => (i === 0 ? ctx.moveTo(p.x, p.y) : ctx.lineTo(p.x, p.y)));
      ctx.stroke();
    }
  }, [strokes]);

  useEffect(() => {
    if (drawMode) redrawStrokes();
  }, [drawMode, strokes, redrawStrokes]);

  // GPU-safe zoom cap from the viewport (stable; recomputed on resize).
  useEffect(() => {
    const update = () => {
      maxScaleRef.current = safeMaxScale();
    };
    update();
    window.addEventListener("resize", update);
    return () => window.removeEventListener("resize", update);
  }, []);

  // Once zoomed in past ~1.8×, swap the light view image for the full original
  // so deep zoom stays crisp.
  useEffect(() => {
    if (view.scale > 1.8) setHiRes(true);
  }, [view.scale]);

  // Keep the active filmstrip thumbnail scrolled into view.
  useEffect(() => {
    const el = stripRef.current?.querySelector<HTMLElement>('[data-active="1"]');
    el?.scrollIntoView({ behavior: "smooth", inline: "center", block: "nearest" });
  }, [mediaId]);

  // Warm the cache with the light view image (and its placeholder) for the
  // images within ±2 so ←/→ and nearby filmstrip clicks show instantly. We
  // preload the light versions, not the multi-MB originals, to stay tunnel-cheap.
  useEffect(() => {
    if (!mediaId) return;
    const i = assets.findIndex((a) => a.mediaId === mediaId);
    if (i < 0) return;
    const neighbours = [assets[i - 2], assets[i - 1], assets[i + 1], assets[i + 2]]
      .filter((a): a is FlowAsset => !!a)
      .map((a) => a.mediaId);
    const urls = neighbours.flatMap((id) => [thumbUrl(id, 2048), thumbUrl(id, 1536)]);
    const imgs = urls.map((u) => {
      const im = new Image();
      im.decoding = "async";
      im.src = u;
      return im;
    });
    return () => imgs.forEach((im) => (im.src = "")); // cancel if we navigate away fast
  }, [mediaId, assets]);

  // Upload image file(s) and attach them as references for the edit.
  const addEditRefs = useCallback(async (files: File[]) => {
    for (const f of files) {
      try {
        const { media_id } = await uploadComicSheet(f);
        setEditRefs((r) => (r.includes(media_id) ? r : [...r, media_id]));
      } catch {
        /* ignore a single bad file */
      }
    }
  }, []);

  // Paste an image while the viewer is open → attach it as an edit reference.
  useEffect(() => {
    if (!mediaId) return;
    const onPaste = (e: ClipboardEvent) => {
      const files = Array.from(e.clipboardData?.items ?? [])
        .filter((it) => it.kind === "file" && it.type.startsWith("image/"))
        .map((it) => it.getAsFile())
        .filter((f): f is File => !!f);
      if (!files.length) return;
      e.preventDefault();
      addEditRefs(files);
    };
    window.addEventListener("paste", onPaste);
    return () => window.removeEventListener("paste", onPaste);
  }, [mediaId, addEditRefs]);

  // Keyboard: Esc closes; ←/→ step through images (unless typing in the composer).
  useEffect(() => {
    if (!mediaId) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        if (modelOpen) setModelOpen(false);
        else close(null);
        return;
      }
      if (e.key === "ArrowLeft" || e.key === "ArrowRight") {
        if (drawMode) return; // don't switch images mid-drawing
        const tag = (document.activeElement?.tagName ?? "").toLowerCase();
        if (tag === "input" || tag === "textarea") return; // let the caret move
        const st = useFlowStudioStore.getState();
        const i = st.assets.findIndex((a) => a.mediaId === st.selectedMediaId);
        if (i < 0) return;
        const ni = e.key === "ArrowLeft" ? i - 1 : i + 1;
        if (ni >= 0 && ni < st.assets.length) {
          e.preventDefault();
          st.select(st.assets[ni].mediaId);
        }
      }
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [mediaId, close, modelOpen, drawMode]);

  // Infinity zoom: wheel zooms toward the cursor (keeps the point under the
  // pointer fixed). Non-passive listener so we can preventDefault the page
  // scroll. Bound imperatively because React's onWheel is passive.
  const zoomAt = useCallback((clientX: number, clientY: number, factor: number) => {
    const el = stageRef.current;
    if (!el) return;
    const r = el.getBoundingClientRect();
    const ox = clientX - r.left - r.width / 2;
    const oy = clientY - r.top - r.height / 2;
    setView((v) => {
      const s2 = clamp(v.scale * factor, MIN_SCALE, maxScaleRef.current);
      if (s2 === v.scale) return v;
      const k = s2 / v.scale;
      return clampView({ scale: s2, tx: ox - k * (ox - v.tx), ty: oy - k * (oy - v.ty) }, r, maxScaleRef.current);
    });
  }, []);

  useEffect(() => {
    const el = stageRef.current;
    if (!el) return;
    const onWheel = (e: WheelEvent) => {
      if (drawMode) return; // no zoom while annotating
      e.preventDefault();
      zoomAt(e.clientX, e.clientY, Math.exp(-e.deltaY * 0.0015));
    };
    el.addEventListener("wheel", onWheel, { passive: false });
    return () => el.removeEventListener("wheel", onWheel);
  }, [zoomAt, mediaId, drawMode]);

  if (!mediaId) return null;
  const modelLabel = FLOW_MODELS.find((m) => m.id === model)?.label ?? model;
  const index = assets.findIndex((a) => a.mediaId === mediaId);
  const prevId = index > 0 ? assets[index - 1].mediaId : null;
  const nextId = index >= 0 && index < assets.length - 1 ? assets[index + 1].mediaId : null;

  const onPointerDown = (e: React.PointerEvent) => {
    if (e.button !== 0) return;
    (e.target as HTMLElement).setPointerCapture?.(e.pointerId);
    drag.current = { x: e.clientX, y: e.clientY, tx: view.tx, ty: view.ty };
    setGrabbing(true);
  };
  const onPointerMove = (e: React.PointerEvent) => {
    if (!drag.current) return;
    const rect = stageRef.current?.getBoundingClientRect() ?? null;
    setView((v) =>
      clampView(
        {
          ...v,
          tx: drag.current!.tx + (e.clientX - drag.current!.x),
          ty: drag.current!.ty + (e.clientY - drag.current!.y),
        },
        rect,
        maxScaleRef.current,
      ),
    );
  };
  const onPointerUp = () => {
    drag.current = null;
    setGrabbing(false);
  };

  // ── annotation drawing (only when drawMode) ──
  const drawDown = (e: React.PointerEvent<HTMLCanvasElement>) => {
    if (e.button !== 0) return;
    e.stopPropagation(); // don't let the stage pan
    e.currentTarget.setPointerCapture?.(e.pointerId);
    curStroke.current = { color: brushColor, size: brushSize, pts: [{ x: e.nativeEvent.offsetX, y: e.nativeEvent.offsetY }] };
    redrawStrokes();
  };
  const drawMove = (e: React.PointerEvent<HTMLCanvasElement>) => {
    if (!curStroke.current) return;
    e.stopPropagation();
    curStroke.current.pts.push({ x: e.nativeEvent.offsetX, y: e.nativeEvent.offsetY });
    redrawStrokes();
  };
  const drawUp = (e: React.PointerEvent<HTMLCanvasElement>) => {
    if (!curStroke.current) return;
    e.stopPropagation();
    const s = curStroke.current;
    curStroke.current = null;
    setStrokes((prev) => [...prev, s]);
  };

  // Flatten the original image + the strokes into one PNG to send as the edit
  // source, scaling strokes from display coords up to the image's native size.
  const flattenAnnotated = (): Promise<File | null> =>
    new Promise((resolve) => {
      const cv = drawRef.current;
      if (!cv || strokes.length === 0) return resolve(null);
      const boxW = cv.clientWidth || cv.width;
      const img = new Image();
      img.onload = () => {
        const off = document.createElement("canvas");
        off.width = img.naturalWidth;
        off.height = img.naturalHeight;
        const ctx = off.getContext("2d");
        if (!ctx) return resolve(null);
        ctx.drawImage(img, 0, 0, off.width, off.height);
        const k = off.width / Math.max(1, boxW); // display → native
        ctx.lineJoin = "round";
        ctx.lineCap = "round";
        for (const s of strokes) {
          ctx.strokeStyle = s.color;
          ctx.lineWidth = s.size * k;
          ctx.beginPath();
          s.pts.forEach((p, i) => (i === 0 ? ctx.moveTo(p.x * k, p.y * k) : ctx.lineTo(p.x * k, p.y * k)));
          ctx.stroke();
        }
        try {
          off.toBlob(
            (b) => resolve(b ? new File([b], "annotated.png", { type: "image/png" }) : null),
            "image/png",
          );
        } catch {
          resolve(null); // tainted canvas / encode failure → fall back
        }
      };
      img.onerror = () => resolve(null);
      img.src = mediaUrl(mediaId);
    });

  const submitEdit = async () => {
    const t = edit.trim();
    if (!t) return;
    let sourceId = mediaId;
    let promptText = t;
    if (drawMode && strokes.length) {
      const file = await flattenAnnotated();
      if (file) {
        try {
          const up = await uploadComicSheet(file);
          sourceId = up.media_id; // edit the marked-up image
          // Tell the model the marks are annotations indicating the region, so
          // it acts on that area AND removes the outline from the result.
          const names = [...new Set(strokes.map((s) => COLOR_NAMES[s.color] ?? "colored"))].join(" and ");
          promptText = `${t}\n\n(Note: the image has a hand-drawn ${names} outline I added to mark the target region — apply the instruction to that marked area, and remove the ${names} outline itself from the final image.)`;
        } catch {
          /* upload failed → fall back to the un-annotated source + plain prompt */
        }
      }
    }
    refine(sourceId, promptText, editRefs.length ? editRefs : undefined);
    setEdit("");
    setEditRefs([]);
    setStrokes([]);
    setDrawMode(false);
  };

  return (
    <div className="fv" role="dialog" aria-modal="true">
      {/* Filmstrip of all images (top-center). */}
      {assets.length > 1 && (
        <div className="fv__strip" ref={stripRef}>
          {assets.map((a) => (
            <button
              key={a.refId}
              type="button"
              data-active={a.mediaId === mediaId ? "1" : undefined}
              className={`fv__thumb${a.mediaId === mediaId ? " is-active" : ""}`}
              title={a.label}
              onClick={() => close(a.mediaId)}
            >
              <img src={thumbUrl(a.mediaId, 96)} alt="" loading="lazy" decoding="async" />
            </button>
          ))}
        </div>
      )}

      {/* Action toolbar — top-left, in the dark margin (off the image). */}
      <div className="fv__tools">
        <button type="button" className="fv__tool" title="Use as reference" onClick={() => addRef(mediaId)}>
          ➕
        </button>
        <button
          type="button"
          className={`fv__tool${drawMode ? " is-on" : ""}`}
          title="Annotate (draw on the image to mark the region to edit)"
          onClick={() => setDrawMode((d) => !d)}
        >
          ✏️
        </button>
        {asset?.prompt && (
          <button
            type="button"
            className="fv__tool"
            title="Regenerate"
            onClick={() => regenerate(mediaId)}
          >
            ♻
          </button>
        )}
        {asset && (
          <button
            type="button"
            className={`fv__tool${asset.pinned ? " is-on" : ""}`}
            title={asset.pinned ? "Unpin" : "Pin"}
            onClick={() => togglePin(asset.refId)}
          >
            📌
          </button>
        )}
        <a className="fv__tool" title="Download" href={mediaUrl(mediaId)} download={`${mediaId}.png`}>
          ⬇
        </a>
        {asset && (
          <button
            type="button"
            className="fv__tool fv__tool--danger"
            title="Delete"
            onClick={() => {
              if (window.confirm("Delete this image from the library?")) {
                remove(asset.refId);
                close(null);
              }
            }}
          >
            🗑
          </button>
        )}
      </div>

      {/* Annotation sub-toolbar (only while drawing). */}
      {drawMode && (
        <div className="fv__draw-bar" onPointerDown={(e) => e.stopPropagation()}>
          <div className="fv__swatches">
            {BRUSH_COLORS.map((c) => (
              <button
                key={c}
                type="button"
                className={`fv__swatch${brushColor === c ? " is-on" : ""}`}
                style={{ background: c }}
                title={c}
                onClick={() => setBrushColor(c)}
              />
            ))}
          </div>
          <label className="fv__draw-size">
            {brushSize}px
            <input
              type="range"
              min={2}
              max={40}
              value={brushSize}
              onChange={(e) => setBrushSize(Number(e.target.value))}
            />
          </label>
          <div className="fv__draw-actions">
            <button
              type="button"
              className="fv__draw-btn"
              title="Undo stroke"
              disabled={strokes.length === 0}
              onClick={() => setStrokes((s) => s.slice(0, -1))}
            >
              ↶
            </button>
            <button
              type="button"
              className="fv__draw-btn"
              title="Clear all strokes"
              disabled={strokes.length === 0}
              onClick={() => setStrokes([])}
            >
              🗑
            </button>
          </div>
        </div>
      )}

      <button type="button" className="fv__close" onClick={() => close(null)} aria-label="Close">
        ✕
      </button>

      {/* On-screen prev / next (also ←/→ keys). */}
      {prevId && (
        <button
          type="button"
          className="fv__nav fv__nav--prev"
          title="Previous image (←)"
          onClick={() => close(prevId)}
        >
          ‹
        </button>
      )}
      {nextId && (
        <button
          type="button"
          className="fv__nav fv__nav--next"
          title="Next image (→)"
          onClick={() => close(nextId)}
        >
          ›
        </button>
      )}

      {/* Infinity-zoom stage. */}
      <div
        ref={stageRef}
        className={`fv__stage${grabbing ? " is-grabbing" : ""}${drawMode ? " is-drawing" : ""}`}
        onPointerDown={drawMode ? undefined : onPointerDown}
        onPointerMove={drawMode ? undefined : onPointerMove}
        onPointerUp={drawMode ? undefined : onPointerUp}
        onPointerCancel={drawMode ? undefined : onPointerUp}
        onDoubleClick={drawMode ? undefined : () => setView(RESET)}
      >
        <div
          className="fv__canvas"
          style={{ transform: `translate(${view.tx}px, ${view.ty}px) scale(${view.scale})` }}
        >
          {/* Sharp-but-light thumbnail — shows instantly while the full loads. */}
          <img className="fv__ph" src={thumbUrl(mediaId, 1536)} alt="" draggable={false} aria-hidden="true" />
          <img
            className="fv__full"
            src={hiRes ? mediaUrl(mediaId) : thumbUrl(mediaId, 2048)}
            alt={asset?.label ?? ""}
            draggable={false}
            decoding="async"
            style={{ opacity: fullReady ? 1 : 0 }}
            onLoad={() => setFullReady(true)}
          />
          {/* Annotation layer — draw on top of the image to mark a region. */}
          {drawMode && (
            <canvas
              ref={drawRef}
              className="fv__draw"
              onPointerDown={drawDown}
              onPointerMove={drawMove}
              onPointerUp={drawUp}
              onPointerCancel={drawUp}
            />
          )}
        </div>
      </div>

      {/* Zoom controls (bottom-left). */}
      <div className="fv__zoom" onPointerDown={(e) => e.stopPropagation()}>
        <button
          type="button"
          className="fv__zbtn"
          title="Zoom out"
          onClick={() => {
            const el = stageRef.current;
            if (el) {
              const r = el.getBoundingClientRect();
              zoomAt(r.left + r.width / 2, r.top + r.height / 2, 1 / 1.3);
            }
          }}
        >
          −
        </button>
        <button type="button" className="fv__zlevel" title="Reset" onClick={() => setView(RESET)}>
          {Math.round(view.scale * 100)}%
        </button>
        <button
          type="button"
          className="fv__zbtn"
          title="Zoom in"
          onClick={() => {
            const el = stageRef.current;
            if (el) {
              const r = el.getBoundingClientRect();
              zoomAt(r.left + r.width / 2, r.top + r.height / 2, 1.3);
            }
          }}
        >
          +
        </button>
      </div>

      {/* Bottom edit composer — Flow-style pill. */}
      <div className="fv__bar" onPointerDown={(e) => e.stopPropagation()}>
        {editRefs.length > 0 && (
          <div className="fv__bar-refs">
            {editRefs.map((id) => (
              <span key={id} className="fv__bar-ref">
                <img src={thumbUrl(id, 96)} alt="" />
                <button
                  type="button"
                  className="fv__bar-refx"
                  title="Remove"
                  onClick={() => setEditRefs((r) => r.filter((x) => x !== id))}
                >
                  ✕
                </button>
              </span>
            ))}
          </div>
        )}
        <div className="fv__bar-row">
          <input
            ref={fileRef}
            type="file"
            accept="image/*"
            multiple
            hidden
            onChange={(e) => {
              addEditRefs(Array.from(e.target.files ?? []));
              e.target.value = "";
            }}
          />
          <button
            type="button"
            className="fv__bar-add"
            title="Add reference image"
            onClick={() => fileRef.current?.click()}
          >
            +
          </button>
          <input
            className="fv__bar-input"
            placeholder="What do you want to change?"
            value={edit}
            onChange={(e) => setEdit(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter" && !e.shiftKey) {
                e.preventDefault();
                submitEdit();
              }
            }}
          />
          <div className="fv__bar-model-wrap">
            <button
              type="button"
              className="fv__bar-model"
              onClick={() => setModelOpen((o) => !o)}
              title="Choose model"
            >
              🍌 {modelLabel}
            </button>
            {modelOpen && (
              <div className="fv__model-pop">
                {FLOW_MODELS.map((m) => (
                  <button
                    key={m.id}
                    type="button"
                    className={`fv__model-opt${m.id === model ? " is-on" : ""}`}
                    onClick={() => {
                      setSettings({ model: m.id });
                      setModelOpen(false);
                    }}
                  >
                    🍌 {m.label}
                    <span className="fv__model-max">max {m.max}</span>
                  </button>
                ))}
              </div>
            )}
          </div>
          <button
            type="button"
            className="fv__bar-send"
            title="Edit with AI"
            disabled={!edit.trim()}
            onClick={submitEdit}
          >
            →
          </button>
        </div>
      </div>
    </div>
  );
}
