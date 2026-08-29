import { useEffect, useMemo, useRef, useState } from "react";
import { useColorizeStore } from "../store/colorize";
import { useAppModeStore } from "../store/appMode";
import { thumbUrl, mediaUrl, detectColorizePanels, samPoint, createRequest, getRequest } from "../api/client";
import "./colorize.css";

/** Small client-side summary of a bible dict for the checkpoint header. */
function bibleSummary(bible: Record<string, unknown> | null): string {
  if (!bible) return "";
  const chars = (bible.characters as Record<string, { name?: string }>) ?? {};
  const outfits = (bible.outfits as Record<string, unknown>) ?? {};
  const scenes = (bible.scenes as Record<string, unknown>) ?? {};
  const pages = (bible.pages as Record<string, unknown>) ?? {};
  const names = Object.entries(chars)
    .map(([id, c]) => `${c.name || id}`)
    .join(", ");
  return `${Object.keys(chars).length} character(s)${names ? ` (${names})` : ""} · ${Object.keys(outfits).length} outfit(s) · ${Object.keys(scenes).length} scene(s) · ${Object.keys(pages).length} page(s) mapped`;
}

export function ColorizeApp() {
  const setMode = useAppModeStore((s) => s.setMode);
  const {
    chapters, current, loading, busy, error,
    pageState, load, newChapter, open, close, remove,
    uploadPages, setStyleRef, buildBible, buildSheets, saveBible, colorizePage, colorizeAll, clearError,
    variantCount, setVariantCount, selectVariant, fixRegion,
  } = useColorizeStore();

  const [newName, setNewName] = useState("");
  const [compareIdx, setCompareIdx] = useState<number | null>(null);
  const [sheetView, setSheetView] = useState<string | null>(null);
  const [fixIdx, setFixIdx] = useState<number | null>(null);
  const pagesInput = useRef<HTMLInputElement>(null);
  const styleInput = useRef<HTMLInputElement>(null);

  useEffect(() => {
    if (!current) load();
  }, [current, load]);

  return (
    <div className="cz">
      <header className="cz__top">
        <div className="cz__brand">🎨 Manga Colorizer</div>
        <div className="cz__nav">
          {current && (
            <button className="cz__btn" onClick={close}>← Chapters</button>
          )}
          <button className="cz__btn cz__btn--ghost" onClick={() => setMode("flow")}>GiantFlow</button>
          <button className="cz__btn cz__btn--ghost" onClick={() => setMode("manga")}>Manga Extract</button>
        </div>
      </header>

      {error && (
        <div className="cz__err" onClick={clearError} title="dismiss">⚠ {error}</div>
      )}
      {busy && <div className="cz__busy">⏳ {busy}</div>}

      {!current ? (
        <ChapterList
          chapters={chapters}
          loading={loading}
          newName={newName}
          setNewName={setNewName}
          onCreate={() => { newChapter(newName); setNewName(""); }}
          onOpen={open}
          onDelete={remove}
        />
      ) : (
        <div className="cz__work">
          <h2 className="cz__h">{current.name || `Chapter #${current.id}`}</h2>

          {/* STEP 1 — pages + style ref */}
          <section className="cz__sec">
            <div className="cz__sec-head">
              <span className="cz__step">1</span> Pages & style reference
              <div className="cz__actions">
                <button className="cz__btn" onClick={() => pagesInput.current?.click()}>+ Add pages</button>
                <button className="cz__btn cz__btn--ghost" onClick={() => styleInput.current?.click()}>
                  {current.style_ref_media_id ? "Change style ref" : "+ Style ref"}
                </button>
              </div>
            </div>
            <input
              ref={pagesInput} type="file" accept="image/*" multiple hidden
              onChange={(e) => { if (e.target.files) uploadPages([...e.target.files]); e.target.value = ""; }}
            />
            <input
              ref={styleInput} type="file" accept="image/*" hidden
              onChange={(e) => { if (e.target.files?.[0]) setStyleRef(e.target.files[0]); e.target.value = ""; }}
            />
            <div className="cz__thumbs">
              {current.style_ref_media_id && (
                <div className="cz__thumb cz__thumb--style" title="style reference">
                  <img src={thumbUrl(current.style_ref_media_id, 160)} alt="style" />
                  <span className="cz__badge">STYLE</span>
                </div>
              )}
              {current.page_media_ids.map((mid, i) => (
                <div className="cz__thumb" key={mid} title={`page ${i + 1}`}>
                  <img src={thumbUrl(mid, 160)} alt={`page ${i + 1}`} />
                  <span className="cz__idx">{i + 1}</span>
                </div>
              ))}
              {current.page_media_ids.length === 0 && <div className="cz__hint">No pages yet — add a folder of B&W pages.</div>}
            </div>
          </section>

          {/* STEP 2 — bible */}
          <section className="cz__sec">
            <div className="cz__sec-head">
              <span className="cz__step">2</span> Color bible
              <div className="cz__actions">
                <button
                  className="cz__btn" disabled={current.page_media_ids.length === 0 || !!busy}
                  onClick={buildBible}
                >
                  {current.bible ? "Rebuild bible" : "Build bible (read chapter)"}
                </button>
              </div>
            </div>
            {current.bible ? (
              <BibleEditor bible={current.bible} onSave={saveBible} />
            ) : (
              <div className="cz__hint">Run “Build bible” — a vision model reads all pages and proposes fixed colors. You then review/edit before colorizing.</div>
            )}
          </section>

          {/* STEP 2.5 — character sheets (per outfit) */}
          <section className="cz__sec">
            <div className="cz__sec-head">
              <span className="cz__step">3</span> Character sheets
              <div className="cz__actions">
                <button
                  className="cz__btn" disabled={!current.bible || !!busy}
                  onClick={buildSheets}
                >
                  {current.sheets && Object.keys(current.sheets).length ? "Rebuild sheets" : "Build sheets (per outfit)"}
                </button>
              </div>
            </div>
            {current.sheets && Object.keys(current.sheets).length > 0 ? (
              <div className="cz__sheets">
                {Object.entries(current.sheets).map(([oid, mid]) => {
                  const o = (current.bible?.outfits as Record<string, { char?: string; desc?: string }> | undefined)?.[oid];
                  const c = o?.char ? (current.bible?.characters as Record<string, { name?: string }> | undefined)?.[o.char] : undefined;
                  return (
                    <div
                      className="cz__sheet cz__sheet--clickable"
                      key={oid}
                      title={o?.desc || "Click to view"}
                      onClick={() => setSheetView(mid)}
                    >
                      <img src={thumbUrl(mid, 200)} alt={oid} />
                      <span className="cz__sheet-lbl">{c?.name ? `${c.name} · ` : ""}{oid.split("__").pop()}</span>
                    </div>
                  );
                })}
              </div>
            ) : (
              <div className="cz__hint">Tô 1 trang mẫu cho mỗi outfit → dùng làm ảnh reference khi tô cả chương (khoá áo nhiều lớp + chibi). Cần có bible trước.</div>
            )}
          </section>

          {/* STEP 4 — colorize */}
          <section className="cz__sec">
            <div className="cz__sec-head">
              <span className="cz__step">4</span> Colorize
              <div className="cz__actions">
                <label className="cz__vcount" title="Số bản gen cho mỗi trang (chọn bản đẹp sau)">
                  Variants/trang:
                  <select value={variantCount} onChange={(e) => setVariantCount(Number(e.target.value))}>
                    {[1, 2, 3, 4].map((n) => <option key={n} value={n}>{n}</option>)}
                  </select>
                </label>
                {Object.keys(current.outputs || {}).length > 0 && (
                  <a
                    className="cz__btn"
                    href={`/api/colorize/chapters/${current.id}/download`}
                    download
                    title="Download all colorized pages as a ZIP"
                  >
                    ⤓ Download all ({Object.keys(current.outputs || {}).length})
                  </a>
                )}
                <button
                  className="cz__btn cz__btn--primary"
                  disabled={!current.bible || current.page_media_ids.length === 0 || !!busy}
                  onClick={colorizeAll}
                >
                  Colorize all
                </button>
              </div>
            </div>
            <div className="cz__cols">
              {/* LEFT — original B&W pages */}
              <div className="cz__col">
                <div className="cz__col-head">Trang gốc ({current.page_media_ids.length})</div>
                <div className="cz__cells">
                  {current.page_media_ids.map((mid, i) => (
                    <div className="cz__cell" key={mid}>
                      <img src={thumbUrl(mid, 220)} alt={`page ${i + 1}`} />
                      <span className="cz__idx">{i + 1}</span>
                    </div>
                  ))}
                </div>
              </div>

              {/* RIGHT — colorized pages (retry on each done cell) */}
              <div className="cz__col">
                <div className="cz__col-head">
                  Đã tô màu ({Object.keys(current.outputs || {}).length}/{current.page_media_ids.length})
                </div>
                <div className="cz__cells">
                  {current.page_media_ids.map((mid, i) => {
                    const out = current.outputs?.[mid];
                    const st = pageState[i];
                    const vars = current.variants?.[mid] ?? [];
                    return (
                      <div className={`cz__cell${out ? " cz__cell--done" : ""}`} key={mid}>
                        {out ? (
                          <>
                            <img
                              src={thumbUrl(out, 220)} alt={`color ${i + 1}`}
                              className="cz__cell-clickable"
                              onClick={() => setCompareIdx(i)}
                              title="Click to compare (zoom)"
                            />
                            <button
                              className="cz__retry" title="Retry / tô lại cả trang"
                              disabled={st === "running" || !!busy}
                              onClick={() => colorizePage(i)}
                            >↻</button>
                            <button
                              className="cz__fix" title="Fix panel / vùng"
                              disabled={st === "running" || !!busy}
                              onClick={() => setFixIdx(i)}
                            >🔧</button>
                            {vars.length >= 2 && (
                              <div className="cz__variants" title="Chọn bản đẹp">
                                {vars.map((vmid, vi) => (
                                  <img
                                    key={vmid}
                                    className={`cz__variant${out === vmid ? " is-on" : ""}`}
                                    src={thumbUrl(vmid, 80)} alt={`v${vi + 1}`}
                                    onClick={() => selectVariant(mid, vmid)}
                                  />
                                ))}
                              </div>
                            )}
                          </>
                        ) : (
                          <div className={`cz__ph cz__ph--${st ?? "idle"}`}>
                            {st === "running" ? "…" : st === "error" ? "failed" : (
                              <button
                                className="cz__cell-go" disabled={!current.bible || !!busy}
                                onClick={() => colorizePage(i)}
                              >colorize</button>
                            )}
                          </div>
                        )}
                        <span className="cz__idx">{i + 1}</span>
                      </div>
                    );
                  })}
                </div>
              </div>
            </div>
          </section>

          {compareIdx !== null && (
            <CompareModal
              pages={current.page_media_ids}
              outputs={current.outputs ?? null}
              index={compareIdx}
              onClose={() => setCompareIdx(null)}
              onNav={(d) =>
                setCompareIdx((p) => {
                  if (p === null) return p;
                  const n = p + d;
                  return n >= 0 && n < current.page_media_ids.length ? n : p;
                })
              }
            />
          )}

          {sheetView && <SheetViewer mid={sheetView} onClose={() => setSheetView(null)} />}

          {fixIdx !== null && current.outputs?.[current.page_media_ids[fixIdx]] && (
            <FixModal
              pageMid={current.page_media_ids[fixIdx]}
              outMid={current.outputs[current.page_media_ids[fixIdx]]}
              running={pageState[fixIdx] === "running"}
              onFix={(box, useMask, prompt) => fixRegion(fixIdx, box, useMask, prompt)}
              onClose={() => setFixIdx(null)}
            />
          )}
        </div>
      )}
    </div>
  );
}

/** Fix one region of a colorized page: pick a detected PANEL (rectangle re-colour)
 * or DRAG a box (SAM-mask re-colour) → re-colorizes just that area. */
function FixModal({
  pageMid, outMid, running, onFix, onClose,
}: {
  pageMid: string;
  outMid: string;
  running: boolean;
  onFix: (box: number[], useMask: boolean, prompt?: string) => Promise<void>;
  onClose: () => void;
}) {
  const [mode, setMode] = useState<"panel" | "objects" | "sam" | "region">("panel");
  const [panels, setPanels] = useState<number[][] | null>(null);
  const [objects, setObjects] = useState<{ label: string; box: number[] }[] | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [sel, setSel] = useState<number[] | null>(null);
  const [prompt, setPrompt] = useState("");
  const [drag, setDrag] = useState<{ x0: number; y0: number; x1: number; y1: number } | null>(null);
  const imgWrap = useRef<HTMLDivElement>(null);
  const dragging = useRef(false);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") onClose(); };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  const norm = (e: React.PointerEvent) => {
    const r = imgWrap.current!.getBoundingClientRect();
    return {
      x: Math.max(0, Math.min(1000, ((e.clientX - r.left) / r.width) * 1000)),
      y: Math.max(0, Math.min(1000, ((e.clientY - r.top) / r.height) * 1000)),
    };
  };
  const pct = (b: number[]) => ({
    left: `${b[1] / 10}%`, top: `${b[0] / 10}%`, width: `${(b[3] - b[1]) / 10}%`, height: `${(b[2] - b[0]) / 10}%`,
  });

  const detectPanels = async () => {
    setBusy("Detect panels…");
    try { setPanels((await detectColorizePanels(pageMid)).panels); } catch { setPanels([]); } finally { setBusy(null); }
  };
  const detectObjects = async () => {
    setBusy("Detect objects…");
    try {
      const req = await createRequest({ type: "detect_objects", params: { media_id: outMid } });
      let row = await getRequest(req.id);
      for (let i = 0; i < 120 && (row.status === "queued" || row.status === "running"); i++) {
        await new Promise((r) => setTimeout(r, i < 8 ? 800 : 2000)); row = await getRequest(req.id);
      }
      const segs = ((row.result as { segments?: { label: string; box: number[] }[] })?.segments) ?? [];
      setObjects(segs);
    } catch { setObjects([]); } finally { setBusy(null); }
  };
  const samClick = async (e: React.PointerEvent) => {
    const r = imgWrap.current!.getBoundingClientRect();
    const x = ((e.clientX - r.left) / r.width) * 1000, y = ((e.clientY - r.top) / r.height) * 1000;
    setBusy("SAM…");
    try { setSel((await samPoint(outMid, x, y)).box); } catch { /* nothing */ } finally { setBusy(null); }
  };

  const apply = async () => {
    if (!sel) return;
    await onFix(sel, mode !== "panel", prompt.trim() || undefined);
    onClose();
  };
  const switchMode = (m: typeof mode) => { setMode(m); setSel(null); setDrag(null); };

  return (
    <div className="czc" onClick={onClose}>
      <div className="czc__bar" onClick={(e) => e.stopPropagation()}>
        <span className="czc__label">Fix / Edit vùng</span>
        <div className="czf__modes">
          <button className={`czc__btn${mode === "panel" ? " czc__btn--reset" : ""}`} onClick={() => switchMode("panel")}>Panel</button>
          <button className={`czc__btn${mode === "objects" ? " czc__btn--reset" : ""}`} onClick={() => switchMode("objects")}>Objects</button>
          <button className={`czc__btn${mode === "sam" ? " czc__btn--reset" : ""}`} onClick={() => switchMode("sam")}>SAM click</button>
          <button className={`czc__btn${mode === "region" ? " czc__btn--reset" : ""}`} onClick={() => switchMode("region")}>Kéo</button>
        </div>
        {mode === "panel" && <button className="czc__btn" onClick={detectPanels} disabled={!!busy}>{panels ? "Detect lại" : "Detect panels"}</button>}
        {mode === "objects" && <button className="czc__btn" onClick={detectObjects} disabled={!!busy}>{objects ? "Detect lại" : "Detect objects"}</button>}
        <span className="czc__hint">{busy || (mode === "panel" ? "bấm panel" : mode === "objects" ? "detect → bấm vật" : mode === "sam" ? "click vào vật" : "kéo khoanh vùng")} · Esc đóng</span>
        <button className="czc__btn" onClick={onClose}>✕</button>
      </div>

      <div className="czc__body" onClick={(e) => e.stopPropagation()} style={{ cursor: mode === "region" ? "crosshair" : mode === "sam" ? "pointer" : "default" }}>
        <div
          ref={imgWrap}
          className="czf__imgwrap"
          onPointerDown={
            mode === "region" ? (e) => { dragging.current = true; const p = norm(e); setDrag({ x0: p.x, y0: p.y, x1: p.x, y1: p.y }); setSel(null); }
            : mode === "sam" ? (e) => { void samClick(e); }
            : undefined
          }
          onPointerMove={mode === "region" ? (e) => { if (dragging.current) { const p = norm(e); setDrag((d) => d && { ...d, x1: p.x, y1: p.y }); } } : undefined}
          onPointerUp={mode === "region" ? () => { dragging.current = false; if (drag) setSel([Math.round(Math.min(drag.y0, drag.y1)), Math.round(Math.min(drag.x0, drag.x1)), Math.round(Math.max(drag.y0, drag.y1)), Math.round(Math.max(drag.x0, drag.x1))]); } : undefined}
        >
          <img src={mediaUrl(outMid)} alt="colorized" draggable={false} />
          {(running || busy) && <div className="czf__busy">{running ? "Đang xử lý…" : busy}</div>}
          {mode === "panel" && (panels ?? []).map((b, i) => (
            <div key={i} className={`czf__panel${sel === b ? " is-on" : ""}`} style={pct(b)} onClick={() => setSel(b)}><span>{i + 1}</span></div>
          ))}
          {mode === "objects" && (objects ?? []).map((o, i) => (
            <div key={i} className={`czf__panel${sel === o.box ? " is-on" : ""}`} style={pct(o.box)} onClick={() => setSel(o.box)}><span>{o.label}</span></div>
          ))}
          {sel && mode !== "region" && <div className="czf__selbox" style={pct(sel)} />}
          {mode === "region" && drag && <div className="czf__sel" style={{ left: `${Math.min(drag.x0, drag.x1) / 10}%`, top: `${Math.min(drag.y0, drag.y1) / 10}%`, width: `${Math.abs(drag.x1 - drag.x0) / 10}%`, height: `${Math.abs(drag.y1 - drag.y0) / 10}%` }} />}
        </div>
      </div>

      <div className="czf__foot" onClick={(e) => e.stopPropagation()}>
        <input
          className="czf__prompt"
          placeholder={sel ? "Prompt sửa (để trống = tô lại). VD: đổi áo sang xanh dương, sửa khuôn mặt…" : "Chọn 1 vùng trước…"}
          value={prompt} onChange={(e) => setPrompt(e.target.value)}
          onKeyDown={(e) => { if (e.key === "Enter" && sel && !running) apply(); }}
        />
        <button className="czc__btn czc__btn--reset" disabled={!sel || running} onClick={apply}>
          {running ? "Đang xử lý…" : prompt.trim() ? "Edit vùng" : "Tô lại vùng"}
        </button>
      </div>
    </div>
  );
}


/** Single-image lightbox with scroll-to-zoom + drag-to-pan — for viewing a
 * character sheet full size. Esc or click the backdrop to close. */
function SheetViewer({ mid, onClose }: { mid: string; onClose: () => void }) {
  const [t, setT] = useState({ s: 1, x: 0, y: 0 });
  const drag = useRef<{ px: number; py: number; ox: number; oy: number } | null>(null);
  const view = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") onClose(); };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  useEffect(() => {
    const el = view.current;
    if (!el) return;
    const onWheel = (e: WheelEvent) => {
      e.preventDefault();
      const r = el.getBoundingClientRect();
      const cx = e.clientX - r.left - r.width / 2;
      const cy = e.clientY - r.top - r.height / 2;
      setT((p) => {
        const ns = Math.min(16, Math.max(1, p.s * (e.deltaY < 0 ? 1.15 : 1 / 1.15)));
        const k = ns / p.s;
        if (ns === 1) return { s: 1, x: 0, y: 0 };
        return { s: ns, x: cx - (cx - p.x) * k, y: cy - (cy - p.y) * k };
      });
    };
    el.addEventListener("wheel", onWheel, { passive: false });
    return () => el.removeEventListener("wheel", onWheel);
  }, []);

  const onDown = (e: React.PointerEvent) => {
    (e.target as HTMLElement).setPointerCapture?.(e.pointerId);
    drag.current = { px: e.clientX, py: e.clientY, ox: t.x, oy: t.y };
  };
  const onMove = (e: React.PointerEvent) => {
    if (!drag.current) return;
    setT((p) => ({ ...p, x: drag.current!.ox + (e.clientX - drag.current!.px), y: drag.current!.oy + (e.clientY - drag.current!.py) }));
  };
  const onUp = () => { drag.current = null; };

  return (
    <div className="czc" onClick={onClose}>
      <div className="czc__bar" onClick={(e) => e.stopPropagation()}>
        <span className="czc__label">Character sheet</span>
        <span className="czc__hint">scroll = zoom · drag = pan · Esc close</span>
        <button className="czc__btn czc__btn--reset" onClick={() => setT({ s: 1, x: 0, y: 0 })}>Reset</button>
        <button className="czc__btn" onClick={onClose}>✕</button>
      </div>
      <div
        ref={view}
        className="czc__body"
        onClick={(e) => e.stopPropagation()}
        onPointerDown={onDown}
        onPointerMove={onMove}
        onPointerUp={onUp}
        onPointerLeave={onUp}
      >
        <div className="czc__pane">
          <img
            src={mediaUrl(mid)}
            alt="character sheet"
            style={{ transform: `translate(${t.x}px, ${t.y}px) scale(${t.s})` }}
            draggable={false}
          />
        </div>
      </div>
    </div>
  );
}

/** Side-by-side compare (original ↔ colorized) with synced infinite zoom + pan.
 * Scroll to zoom toward the cursor, drag to pan; both panes move together so you
 * inspect the exact same region on each. ←/→ switch pages, Esc closes. */
function CompareModal({
  pages, outputs, index, onClose, onNav,
}: {
  pages: string[];
  outputs: Record<string, string> | null;
  index: number;
  onClose: () => void;
  onNav: (delta: number) => void;
}) {
  const bw = pages[index];
  const color = outputs?.[bw] ?? null;
  const [t, setT] = useState({ s: 1, x: 0, y: 0 });
  const drag = useRef<{ px: number; py: number; ox: number; oy: number } | null>(null);
  const view = useRef<HTMLDivElement>(null);

  // Reset transform whenever the page changes.
  useEffect(() => setT({ s: 1, x: 0, y: 0 }), [index]);

  // Keyboard: Esc close, ← → navigate.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
      else if (e.key === "ArrowLeft") onNav(-1);
      else if (e.key === "ArrowRight") onNav(1);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose, onNav]);

  // Native non-passive wheel so we can zoom toward the cursor without page-scroll.
  useEffect(() => {
    const el = view.current;
    if (!el) return;
    const onWheel = (e: WheelEvent) => {
      e.preventDefault();
      const r = el.getBoundingClientRect();
      const cx = e.clientX - r.left - r.width / 2;
      const cy = e.clientY - r.top - r.height / 2;
      setT((p) => {
        const ns = Math.min(24, Math.max(1, p.s * (e.deltaY < 0 ? 1.15 : 1 / 1.15)));
        const k = ns / p.s;
        if (ns === 1) return { s: 1, x: 0, y: 0 };
        return { s: ns, x: cx - (cx - p.x) * k, y: cy - (cy - p.y) * k };
      });
    };
    el.addEventListener("wheel", onWheel, { passive: false });
    return () => el.removeEventListener("wheel", onWheel);
  }, []);

  const onDown = (e: React.PointerEvent) => {
    (e.target as HTMLElement).setPointerCapture?.(e.pointerId);
    drag.current = { px: e.clientX, py: e.clientY, ox: t.x, oy: t.y };
  };
  const onMove = (e: React.PointerEvent) => {
    if (!drag.current) return;
    setT((p) => ({ ...p, x: drag.current!.ox + (e.clientX - drag.current!.px), y: drag.current!.oy + (e.clientY - drag.current!.py) }));
  };
  const onUp = () => { drag.current = null; };

  const tf = { transform: `translate(${t.x}px, ${t.y}px) scale(${t.s})` };

  return (
    <div className="czc" onClick={onClose}>
      <div className="czc__bar" onClick={(e) => e.stopPropagation()}>
        <button className="czc__btn" onClick={() => onNav(-1)} disabled={index <= 0}>‹</button>
        <span className="czc__label">Page {index + 1} / {pages.length}</span>
        <button className="czc__btn" onClick={() => onNav(1)} disabled={index >= pages.length - 1}>›</button>
        <span className="czc__hint">scroll = zoom · drag = pan · ←/→ page · Esc close</span>
        <button className="czc__btn czc__btn--reset" onClick={() => setT({ s: 1, x: 0, y: 0 })}>Reset</button>
        <button className="czc__btn" onClick={onClose}>✕</button>
      </div>
      <div
        ref={view}
        className="czc__body"
        onClick={(e) => e.stopPropagation()}
        onPointerDown={onDown}
        onPointerMove={onMove}
        onPointerUp={onUp}
        onPointerLeave={onUp}
      >
        <div className="czc__pane">
          <span className="czc__tag">Original</span>
          <img src={mediaUrl(bw)} alt="original" style={tf} draggable={false} />
        </div>
        <div className="czc__split" />
        <div className="czc__pane">
          <span className="czc__tag">Colorized</span>
          {color ? (
            <img src={mediaUrl(color)} alt="colorized" style={tf} draggable={false} />
          ) : (
            <div className="czc__none">not colorized yet</div>
          )}
        </div>
      </div>
    </div>
  );
}

function ChapterList({
  chapters, loading, newName, setNewName, onCreate, onOpen, onDelete,
}: {
  chapters: ReturnType<typeof useColorizeStore.getState>["chapters"];
  loading: boolean;
  newName: string;
  setNewName: (v: string) => void;
  onCreate: () => void;
  onOpen: (id: number) => void;
  onDelete: (id: number) => void;
}) {
  return (
    <div className="cz__list">
      <div className="cz__new">
        <input
          className="cz__input" placeholder="New chapter name…" value={newName}
          onChange={(e) => setNewName(e.target.value)}
          onKeyDown={(e) => e.key === "Enter" && onCreate()}
        />
        <button className="cz__btn cz__btn--primary" onClick={onCreate}>+ New chapter</button>
      </div>
      {loading && <div className="cz__hint">Loading…</div>}
      {!loading && chapters.length === 0 && <div className="cz__hint">No chapters yet.</div>}
      <div className="cz__cards">
        {chapters.map((c) => (
          <div className="cz__card" key={c.id} onClick={() => onOpen(c.id)}>
            <div className="cz__card-name">{c.name || `Chapter #${c.id}`}</div>
            <div className="cz__card-meta">
              {c.pages} pages · {c.has_bible ? "bible ✓" : "no bible"} · {c.colorized} colorized
            </div>
            <button
              className="cz__x" title="delete"
              onClick={(e) => { e.stopPropagation(); if (confirm("Delete this chapter?")) onDelete(c.id); }}
            >×</button>
          </div>
        ))}
      </div>
    </div>
  );
}

function BibleEditor({
  bible, onSave,
}: {
  bible: Record<string, unknown>;
  onSave: (text: string) => Promise<string | null>;
}) {
  const [text, setText] = useState(() => JSON.stringify(bible, null, 2));
  const [msg, setMsg] = useState<string | null>(null);
  const [view, setView] = useState<"read" | "json">("read");
  // Re-seed when a new bible arrives (rebuild).
  const seed = useMemo(() => JSON.stringify(bible, null, 2), [bible]);
  useEffect(() => setText(seed), [seed]);

  return (
    <div className="cz__bible">
      <div className="cz__bible-head">
        <div className="cz__bible-sum">{bibleSummary(bible)}</div>
        <div className="cz__viewtoggle">
          <button className={`cz__tab${view === "read" ? " is-on" : ""}`} onClick={() => setView("read")}>Dễ đọc</button>
          <button className={`cz__tab${view === "json" ? " is-on" : ""}`} onClick={() => setView("json")}>JSON</button>
        </div>
      </div>

      {view === "read" ? (
        <BibleReadable bible={bible} />
      ) : (
        <textarea className="cz__ta" spellCheck={false} value={text} onChange={(e) => setText(e.target.value)} />
      )}

      <div className="cz__bible-foot">
        {view === "json" ? (
          <button
            className="cz__btn cz__btn--primary"
            onClick={async () => { setMsg("saving…"); setMsg(await onSave(text) ?? "saved ✓"); }}
          >Save bible</button>
        ) : (
          <span className="cz__reviewhint">📷 = màu đọc từ trang màu (tin được) · ≈ = suy đoán từ B&W (nên kiểm tra). Sửa ở tab JSON.</span>
        )}
        {msg && <span className="cz__savemsg">{msg}</span>}
      </div>
    </div>
  );
}

/* Human-readable bible for the review checkpoint — natural language + colour
 * swatches + a source flag so the reviewer knows which colours to trust. */
type Hex = string;
interface BChar { name?: string; role?: string; desc?: string; features?: string; hair?: Hex; skin?: Hex; eyes?: Hex; color_source?: string }
interface BOutfit { char?: string; desc?: string; top?: Hex; bottom?: Hex; shoes?: Hex; accents?: Hex[]; color_source?: string }
interface BProp { name?: string; desc?: string; owner?: string | null; colors?: Hex[]; color_source?: string }
interface BScene { desc?: string; palette?: Hex[]; lighting?: string; key_elements?: string[]; elements?: Record<string, Hex> }
interface BPage { scene?: string; present?: { char: string; outfit?: string }[]; props?: string[]; beat?: string; mood?: string }

function Sw({ c }: { c?: string }) {
  if (!c) return null;
  return <span className="cz__sw" style={{ background: c }} title={c} />;
}
function Src({ s }: { s?: string }) {
  const real = s === "color_page";
  return <span className={`cz__src${real ? " is-real" : ""}`} title={real ? "màu đọc từ trang màu" : "suy đoán từ B&W — nên kiểm tra"}>{real ? "📷" : "≈"}</span>;
}

function BibleReadable({ bible }: { bible: Record<string, unknown> }) {
  const story = (bible.story ?? {}) as { synopsis?: string; genre?: string; tone?: string };
  const chars = (bible.characters ?? {}) as Record<string, BChar>;
  const outfits = (bible.outfits ?? {}) as Record<string, BOutfit>;
  const props = (bible.props ?? {}) as Record<string, BProp>;
  const scenes = (bible.scenes ?? {}) as Record<string, BScene>;
  const pages = (bible.pages ?? {}) as Record<string, BPage>;
  const order = ((bible.meta as { page_order?: string[] })?.page_order) ?? Object.keys(pages);

  return (
    <div className="cz__read">
      {(story.synopsis || story.genre) && (
        <section className="cz__rsec">
          <h4>📖 Câu chuyện</h4>
          {(story.genre || story.tone) && <div className="cz__rmeta">{[story.genre, story.tone].filter(Boolean).join(" · ")}</div>}
          {story.synopsis && <p>{story.synopsis}</p>}
        </section>
      )}

      <section className="cz__rsec">
        <h4>👥 Nhân vật ({Object.keys(chars).length})</h4>
        {Object.entries(chars).map(([id, c]) => (
          <div className="cz__rchar" key={id}>
            <div className="cz__rline">
              <strong>{c.name || id}</strong>
              {c.role && <span className="cz__rrole">{c.role}</span>}
              <Src s={c.color_source} />
              <span className="cz__rsw">tóc <Sw c={c.hair} /> · da <Sw c={c.skin} /> · mắt <Sw c={c.eyes} /></span>
            </div>
            {c.features && <div className="cz__rsub">{c.features}</div>}
            {Object.entries(outfits).filter(([, o]) => o.char === id).map(([oid, o]) => (
              <div className="cz__rout" key={oid}>
                <Sw c={o.top} /><Sw c={o.bottom} /><Sw c={o.shoes} />
                {(o.accents ?? []).map((a, i) => <Sw c={a} key={i} />)}
                <Src s={o.color_source} />
                <span className="cz__routd">{o.desc || oid}</span>
              </div>
            ))}
          </div>
        ))}
      </section>

      {Object.keys(props).length > 0 && (
        <section className="cz__rsec">
          <h4>🎁 Vật dụng ({Object.keys(props).length})</h4>
          {Object.entries(props).map(([id, p]) => (
            <div className="cz__rline" key={id}>
              {(p.colors ?? []).map((c, i) => <Sw c={c} key={i} />)}
              <Src s={p.color_source} />
              <span>{p.name || id}{p.owner && chars[p.owner] ? ` — ${chars[p.owner].name}` : ""}</span>
            </div>
          ))}
        </section>
      )}

      {Object.keys(scenes).length > 0 && (
        <section className="cz__rsec">
          <h4>🏛️ Bối cảnh ({Object.keys(scenes).length})</h4>
          {Object.entries(scenes).map(([id, s]) => (
            <div className="cz__rscene" key={id}>
              <div className="cz__rline">
                {(s.palette ?? []).map((c, i) => <Sw c={c} key={i} />)}
                <strong>{s.desc || id}</strong>
              </div>
              {s.lighting && <div className="cz__rsub">💡 {s.lighting}</div>}
              {s.elements && Object.keys(s.elements).length > 0 ? (
                <div className="cz__relems">
                  {Object.entries(s.elements).map(([name, c]) => (
                    <span className="cz__relem" key={name}><Sw c={c} /> {name}</span>
                  ))}
                </div>
              ) : (
                (s.key_elements ?? []).length > 0 && <div className="cz__rsub">🔩 {(s.key_elements ?? []).join(", ")}</div>
              )}
            </div>
          ))}
        </section>
      )}

      <section className="cz__rsec">
        <h4>📄 Từng trang ({order.length})</h4>
        <div className="cz__rpages">
          {order.map((pg) => {
            const p = pages[pg];
            if (!p) return null;
            const who = (p.present ?? []).map((pr) => chars[pr.char]?.name || pr.char).join(", ");
            return (
              <div className="cz__rpage" key={pg}>
                <span className="cz__rpg">{pg.replace("page_", "")}</span>
                {p.mood && <span className="cz__rmood">{p.mood}</span>}
                <span className="cz__rbeat">{p.beat || "—"}</span>
                {who && <span className="cz__rwho">{who}</span>}
              </div>
            );
          })}
        </div>
      </section>
    </div>
  );
}
