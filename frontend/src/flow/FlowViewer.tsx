import { useCallback, useEffect, useRef, useState } from "react";
import { mediaUrl, thumbUrl, uploadComicSheet } from "../api/client";
import { FLOW_MODELS, useFlowStudioStore } from "../store/flowStudio";

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
const MIN_SCALE = 0.2;
const DEFAULT_MAX_SCALE = 8;
// GPU hard limit on a single raster surface is ~16384px; on a Retina display
// CSS px are doubled in device px, so a scaled image whose device-pixel size
// crosses that limit renders BLACK. Stay well under it (target ~12000 device
// px) — the per-image cap below is derived from this.
const SAFE_DEVICE_PX = 12000;
const clamp = (v: number, lo: number, hi: number) => Math.min(hi, Math.max(lo, v));

/** Largest zoom that keeps the scaled image's device-pixel size safe for the
 *  GPU. Derived from the rendered fit-size and devicePixelRatio. */
function safeMaxScale(fitMaxPx: number): number {
  const dpr = typeof window !== "undefined" ? window.devicePixelRatio || 1 : 1;
  const cap = SAFE_DEVICE_PX / dpr / Math.max(1, fitMaxPx);
  return clamp(cap, 2, 16);
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
  const generating = useFlowStudioStore((s) => s.generating);
  const model = useFlowStudioStore((s) => s.settings.model);
  const setSettings = useFlowStudioStore((s) => s.setSettings);

  const [edit, setEdit] = useState("");
  const [editRefs, setEditRefs] = useState<string[]>([]);
  const [modelOpen, setModelOpen] = useState(false);
  const [view, setView] = useState<View>(RESET);
  const [grabbing, setGrabbing] = useState(false);
  const [fullReady, setFullReady] = useState(false);
  const stageRef = useRef<HTMLDivElement>(null);
  const imgRef = useRef<HTMLImageElement>(null);
  const fileRef = useRef<HTMLInputElement>(null);
  const stripRef = useRef<HTMLDivElement>(null);
  const drag = useRef<{ x: number; y: number; tx: number; ty: number } | null>(null);
  const maxScaleRef = useRef(DEFAULT_MAX_SCALE);

  // Reset zoom + hide the (still-loading) full image whenever we switch — the
  // thumbnail placeholder shows instantly until the full image is ready.
  useEffect(() => {
    maxScaleRef.current = DEFAULT_MAX_SCALE;
    setView(RESET);
    setFullReady(false);
  }, [mediaId]);

  // Keep the active filmstrip thumbnail scrolled into view.
  useEffect(() => {
    const el = stripRef.current?.querySelector<HTMLElement>('[data-active="1"]');
    el?.scrollIntoView({ behavior: "smooth", inline: "center", block: "nearest" });
  }, [mediaId]);

  // Warm the browser cache with the neighbouring full images so ←/→ (and
  // back-and-forth between two images) shows instantly instead of re-fetching.
  useEffect(() => {
    if (!mediaId) return;
    const i = assets.findIndex((a) => a.mediaId === mediaId);
    if (i < 0) return;
    const neighbours = [assets[i - 1]?.mediaId, assets[i + 1]?.mediaId].filter(
      (x): x is string => typeof x === "string",
    );
    const imgs = neighbours.map((id) => {
      const im = new Image();
      im.decoding = "async";
      im.src = mediaUrl(id);
      return im;
    });
    return () => imgs.forEach((im) => (im.src = "")); // cancel if we navigate away fast
  }, [mediaId, assets]);

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
  }, [mediaId, close, modelOpen]);

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
      e.preventDefault();
      zoomAt(e.clientX, e.clientY, Math.exp(-e.deltaY * 0.0015));
    };
    el.addEventListener("wheel", onWheel, { passive: false });
    return () => el.removeEventListener("wheel", onWheel);
  }, [zoomAt, mediaId]);

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

  const submitEdit = () => {
    const t = edit.trim();
    if (!t || generating) return;
    refine(mediaId, t, editRefs.length ? editRefs : undefined);
    setEdit("");
    setEditRefs([]);
  };

  const onPickRefs = async (files: FileList | null) => {
    if (!files?.length) return;
    for (const f of Array.from(files)) {
      try {
        const { media_id } = await uploadComicSheet(f);
        setEditRefs((r) => (r.includes(media_id) ? r : [...r, media_id]));
      } catch {
        /* ignore a single bad file */
      }
    }
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
        <button type="button" className="fv__tool" title="Dùng làm tham chiếu" onClick={() => addRef(mediaId)}>
          ➕
        </button>
        {asset?.prompt && (
          <button
            type="button"
            className="fv__tool"
            title="Tạo lại"
            disabled={generating}
            onClick={() => regenerate(mediaId)}
          >
            ♻
          </button>
        )}
        {asset && (
          <button
            type="button"
            className={`fv__tool${asset.pinned ? " is-on" : ""}`}
            title={asset.pinned ? "Bỏ ghim" : "Ghim"}
            onClick={() => togglePin(asset.refId)}
          >
            📌
          </button>
        )}
        <a className="fv__tool" title="Tải" href={mediaUrl(mediaId)} download={`${mediaId}.png`}>
          ⬇
        </a>
        {asset && (
          <button
            type="button"
            className="fv__tool fv__tool--danger"
            title="Xoá"
            onClick={() => {
              if (window.confirm("Xoá ảnh này khỏi thư viện?")) {
                remove(asset.refId);
                close(null);
              }
            }}
          >
            🗑
          </button>
        )}
      </div>

      <button type="button" className="fv__close" onClick={() => close(null)} aria-label="Đóng">
        ✕
      </button>

      {/* On-screen prev / next (also ←/→ keys). */}
      {prevId && (
        <button
          type="button"
          className="fv__nav fv__nav--prev"
          title="Ảnh trước (←)"
          onClick={() => close(prevId)}
        >
          ‹
        </button>
      )}
      {nextId && (
        <button
          type="button"
          className="fv__nav fv__nav--next"
          title="Ảnh sau (→)"
          onClick={() => close(nextId)}
        >
          ›
        </button>
      )}

      {/* Infinity-zoom stage. */}
      <div
        ref={stageRef}
        className={`fv__stage${grabbing ? " is-grabbing" : ""}`}
        onPointerDown={onPointerDown}
        onPointerMove={onPointerMove}
        onPointerUp={onPointerUp}
        onPointerCancel={onPointerUp}
        onDoubleClick={() => setView(RESET)}
      >
        <div
          className="fv__canvas"
          style={{ transform: `translate(${view.tx}px, ${view.ty}px) scale(${view.scale})` }}
        >
          {/* Sharp-but-light thumbnail — shows instantly while the full loads. */}
          <img className="fv__ph" src={thumbUrl(mediaId, 1536)} alt="" draggable={false} aria-hidden="true" />
          <img
            className="fv__full"
            ref={imgRef}
            src={mediaUrl(mediaId)}
            alt={asset?.label ?? ""}
            draggable={false}
            decoding="async"
            style={{ opacity: fullReady ? 1 : 0 }}
            onLoad={(e) => {
              // Derive the GPU-safe zoom cap from the actual rendered fit-size.
              const img = e.currentTarget;
              const fit = Math.max(img.clientWidth, img.clientHeight);
              maxScaleRef.current = safeMaxScale(fit);
              setFullReady(true);
            }}
          />
        </div>
      </div>

      {/* Zoom controls (bottom-left). */}
      <div className="fv__zoom" onPointerDown={(e) => e.stopPropagation()}>
        <button
          type="button"
          className="fv__zbtn"
          title="Thu nhỏ"
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
        <button type="button" className="fv__zlevel" title="Đặt lại" onClick={() => setView(RESET)}>
          {Math.round(view.scale * 100)}%
        </button>
        <button
          type="button"
          className="fv__zbtn"
          title="Phóng to"
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
                  title="Bỏ"
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
              onPickRefs(e.target.files);
              e.target.value = "";
            }}
          />
          <button
            type="button"
            className="fv__bar-add"
            title="Thêm ảnh tham chiếu"
            onClick={() => fileRef.current?.click()}
          >
            +
          </button>
          <input
            className="fv__bar-input"
            placeholder="Bạn muốn thay đổi điều gì?"
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
              title="Chọn model"
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
                    <span className="fv__model-max">tối đa {m.max}</span>
                  </button>
                ))}
              </div>
            )}
          </div>
          <button
            type="button"
            className="fv__bar-send"
            title="Sửa bằng AI"
            disabled={generating || !edit.trim()}
            onClick={submitEdit}
          >
            {generating ? "…" : "→"}
          </button>
        </div>
      </div>
    </div>
  );
}
