import { useEffect, useLayoutEffect, useRef, useState, type CSSProperties } from "react";
import { mediaUrl } from "../api/client";
import { type BoxItem, type PageItem } from "./comicShared";

const MIN_SIZE = 12; // min box size in page pixels
const DEFAULT_BOX = 220; // default new-box size (page px)
let _boxSeq = 0;
const newId = () => `b${Date.now().toString(36)}${(_boxSeq++).toString(36)}`;

// 8 resize handles: 4 corners + 4 edge midpoints. The letters in `h` say which
// edges that handle moves (n/s = top/bottom, w/e = left/right); the opposite
// edges stay anchored.
type ResizeHandle = "nw" | "n" | "ne" | "e" | "se" | "s" | "sw" | "w";
const RESIZE_HANDLES: { h: ResizeHandle; cursor: string; style: CSSProperties }[] = [
  { h: "nw", cursor: "nwse-resize", style: { left: -6, top: -6 } },
  { h: "n", cursor: "ns-resize", style: { left: "calc(50% - 5.5px)", top: -6 } },
  { h: "ne", cursor: "nesw-resize", style: { right: -6, top: -6 } },
  { h: "e", cursor: "ew-resize", style: { right: -6, top: "calc(50% - 5.5px)" } },
  { h: "se", cursor: "nwse-resize", style: { right: -6, bottom: -6 } },
  { h: "s", cursor: "ns-resize", style: { left: "calc(50% - 5.5px)", bottom: -6 } },
  { h: "sw", cursor: "nesw-resize", style: { left: -6, bottom: -6 } },
  { h: "w", cursor: "ew-resize", style: { left: -6, top: "calc(50% - 5.5px)" } },
];

type Drag =
  | { mode: "draw"; x0: number; y0: number }
  | { mode: "move"; id: string; dx: number; dy: number }
  | { mode: "resize"; id: string; handle: ResizeHandle };

const clamp = (v: number, lo: number, hi: number) => Math.max(lo, Math.min(hi, v));

/**
 * Interactive panel-box editor for ONE page. Add a box by dragging an empty
 * area OR right-click → "Add box". Drag a box to move, drag any corner or edge
 * handle to resize, click to select, ✕ / Delete / right-click → "Delete box" to remove.
 * `onChange` gets the full box list after each edit. Coordinates are zoom-aware
 * (overlay scaled by image clientWidth; pointer mapping via live rect).
 */
export function PanelBoxEditor({
  page,
  onChange,
}: {
  page: PageItem;
  onChange: (boxes: BoxItem[]) => void;
}) {
  const boxes = (Array.isArray(page.boxes) ? page.boxes : []) as BoxItem[];
  const pageW = page.w || 1000;
  const pageH = page.h || 1500;

  const imgRef = useRef<HTMLImageElement | null>(null);
  const [cssW, setCssW] = useState(0);
  const [selected, setSelected] = useState<string | null>(null);
  const [draft, setDraft] = useState<BoxItem | null>(null);
  const [menu, setMenu] = useState<{ px: number; py: number; boxId?: string } | null>(null);
  const dragRef = useRef<Drag | null>(null);

  useLayoutEffect(() => {
    const el = imgRef.current;
    if (!el) return;
    const measure = () => setCssW(el.clientWidth);
    measure();
    const ro = new ResizeObserver(measure);
    ro.observe(el);
    return () => ro.disconnect();
  }, []);

  // Close the context menu on any outside pointerdown / Escape.
  useEffect(() => {
    if (!menu) return;
    const close = () => setMenu(null);
    window.addEventListener("pointerdown", close);
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") setMenu(null); };
    window.addEventListener("keydown", onKey);
    return () => { window.removeEventListener("pointerdown", close); window.removeEventListener("keydown", onKey); };
  }, [menu]);

  const scale = cssW > 0 ? cssW / pageW : 0;

  const toPage = (clientX: number, clientY: number) => {
    const el = imgRef.current;
    if (!el) return { x: 0, y: 0 };
    const r = el.getBoundingClientRect();
    return {
      x: clamp(((clientX - r.left) / Math.max(1, r.width)) * pageW, 0, pageW),
      y: clamp(((clientY - r.top) / Math.max(1, r.height)) * pageH, 0, pageH),
    };
  };

  function addBoxAt(px: number, py: number) {
    const w = Math.min(DEFAULT_BOX, Math.max(MIN_SIZE, pageW - px));
    const h = Math.min(DEFAULT_BOX, Math.max(MIN_SIZE, pageH - py));
    const id = newId();
    onChange([...boxes, { id, x: Math.round(px), y: Math.round(py), w: Math.round(w), h: Math.round(h) }]);
    setSelected(id);
  }

  function deleteBox(id: string) {
    onChange(boxes.filter((b) => b.id !== id));
    if (selected === id) setSelected(null);
  }

  useEffect(() => {
    function onMove(ev: PointerEvent) {
      const d = dragRef.current;
      if (!d) return;
      const p = toPage(ev.clientX, ev.clientY);
      if (d.mode === "draw") {
        setDraft({ id: "__draft__", x: Math.min(d.x0, p.x), y: Math.min(d.y0, p.y), w: Math.abs(p.x - d.x0), h: Math.abs(p.y - d.y0) });
      } else if (d.mode === "move") {
        const b = boxes.find((bb) => bb.id === d.id);
        if (!b) return;
        setDraft({ ...b, x: clamp(p.x - d.dx, 0, pageW - b.w), y: clamp(p.y - d.dy, 0, pageH - b.h) });
      } else if (d.mode === "resize") {
        const b = boxes.find((bb) => bb.id === d.id);
        if (!b) return;
        let left = b.x, right = b.x + b.w, top = b.y, bottom = b.y + b.h;
        if (d.handle.includes("w")) left = clamp(p.x, 0, right - MIN_SIZE);
        if (d.handle.includes("e")) right = clamp(p.x, left + MIN_SIZE, pageW);
        if (d.handle.includes("n")) top = clamp(p.y, 0, bottom - MIN_SIZE);
        if (d.handle.includes("s")) bottom = clamp(p.y, top + MIN_SIZE, pageH);
        setDraft({ ...b, x: left, y: top, w: right - left, h: bottom - top });
      }
    }
    function onUp() {
      const d = dragRef.current;
      dragRef.current = null;
      if (!d) return;
      const dr = draft;
      setDraft(null);
      if (!dr) return;
      const box = { id: dr.id, x: Math.round(dr.x), y: Math.round(dr.y), w: Math.round(dr.w), h: Math.round(dr.h) };
      if (d.mode === "draw") {
        if (box.w >= MIN_SIZE && box.h >= MIN_SIZE) {
          const id = newId();
          onChange([...boxes, { ...box, id }]);
          setSelected(id);
        }
      } else {
        onChange(boxes.map((b) => (b.id === d.id ? { ...b, x: box.x, y: box.y, w: box.w, h: box.h } : b)));
      }
    }
    window.addEventListener("pointermove", onMove);
    window.addEventListener("pointerup", onUp);
    return () => {
      window.removeEventListener("pointermove", onMove);
      window.removeEventListener("pointerup", onUp);
    };
  });

  function startDraw(e: React.PointerEvent) {
    if (e.button !== 0) return; // left button only — right-click is for the menu
    if (e.target !== imgRef.current) return; // only when clicking the image (empty area)
    setMenu(null);
    const p = toPage(e.clientX, e.clientY);
    dragRef.current = { mode: "draw", x0: p.x, y0: p.y };
    setSelected(null);
  }

  const render = draft && draft.id !== "__draft__" ? boxes.map((b) => (b.id === draft.id ? draft : b)) : boxes;
  const showDraft = draft && draft.id === "__draft__" ? draft : null;

  return (
    <div
      className="nodrag"
      onPointerDown={startDraw}
      onContextMenu={(e) => { e.preventDefault(); const p = toPage(e.clientX, e.clientY); setMenu({ px: p.x, py: p.y }); }}
      onKeyDown={(e) => { if ((e.key === "Delete" || e.key === "Backspace") && selected) deleteBox(selected); }}
      tabIndex={0}
      style={{ position: "relative", width: "100%", userSelect: "none", cursor: "crosshair", lineHeight: 0 }}
    >
      <img ref={imgRef} src={mediaUrl(page.mediaId)} alt={page.name} draggable={false} onLoad={() => setCssW(imgRef.current?.clientWidth ?? 0)} style={{ width: "100%", display: "block", borderRadius: 4 }} />

      {scale > 0 && render.map((b) => {
        const sel = b.id === selected;
        return (
          <div
            key={b.id}
            onPointerDown={(e) => {
              if (e.button !== 0) { setSelected(b.id); return; }
              e.stopPropagation();
              setSelected(b.id);
              const p = toPage(e.clientX, e.clientY);
              dragRef.current = { mode: "move", id: b.id, dx: p.x - b.x, dy: p.y - b.y };
            }}
            onContextMenu={(e) => { e.preventDefault(); e.stopPropagation(); setSelected(b.id); const p = toPage(e.clientX, e.clientY); setMenu({ px: p.x, py: p.y, boxId: b.id }); }}
            style={{
              position: "absolute",
              left: b.x * scale, top: b.y * scale, width: b.w * scale, height: b.h * scale,
              border: `2px solid ${sel ? "#f5b301" : "#ef4444"}`,
              background: sel ? "rgba(245,179,1,0.12)" : "transparent",
              boxSizing: "border-box", cursor: "move",
            }}
          >
            {sel && (
              <>
                <button
                  onPointerDown={(e) => e.stopPropagation()}
                  onClick={(e) => { e.stopPropagation(); deleteBox(b.id); }}
                  title="Delete box"
                  style={{ position: "absolute", top: -24, right: -10, width: 18, height: 18, borderRadius: 9, border: "none", background: "#ef4444", color: "#fff", fontSize: 11, lineHeight: "18px", cursor: "pointer", padding: 0, zIndex: 5 }}
                >×</button>
                {RESIZE_HANDLES.map((H) => (
                  <div
                    key={H.h}
                    onPointerDown={(e) => { if (e.button !== 0) return; e.stopPropagation(); dragRef.current = { mode: "resize", id: b.id, handle: H.h }; }}
                    style={{ position: "absolute", width: 11, height: 11, background: "#f5b301", border: "1px solid #fff", borderRadius: 2, ...H.style, cursor: H.cursor }}
                  />
                ))}
              </>
            )}
          </div>
        );
      })}

      {showDraft && scale > 0 && (
        <div style={{ position: "absolute", left: showDraft.x * scale, top: showDraft.y * scale, width: showDraft.w * scale, height: showDraft.h * scale, border: "2px dashed #f5b301", boxSizing: "border-box", pointerEvents: "none" }} />
      )}

      {menu && scale > 0 && (
        <div
          onPointerDown={(e) => e.stopPropagation()}
          onContextMenu={(e) => e.preventDefault()}
          style={{
            position: "absolute", left: clamp(menu.px * scale, 0, cssW - 120), top: menu.py * scale, zIndex: 20,
            background: "var(--panel)", border: "1px solid var(--border)", borderRadius: 6, padding: 4,
            display: "flex", flexDirection: "column", gap: 2, minWidth: 116, lineHeight: "normal",
            boxShadow: "0 6px 18px rgba(0,0,0,0.45)",
          }}
        >
          <button
            className="comic-btn comic-btn--sm"
            style={{ width: "100%", justifyContent: "flex-start" }}
            onPointerDown={(e) => { e.stopPropagation(); addBoxAt(menu.px, menu.py); setMenu(null); }}
          >＋ Add box</button>
          {menu.boxId && (
            <button
              className="comic-btn comic-btn--sm"
              style={{ width: "100%", justifyContent: "flex-start", color: "#ef4444" }}
              onPointerDown={(e) => { e.stopPropagation(); deleteBox(menu.boxId!); setMenu(null); }}
            >× Delete box</button>
          )}
        </div>
      )}
    </div>
  );
}
