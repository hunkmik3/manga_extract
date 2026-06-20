import { useEffect, useRef, useState } from "react";
import { getFlowUsage, thumbUrl, type FlowUsage } from "../api/client";
import { FLOW_ONLY, useAppModeStore } from "../store/appMode";
import { useFlowProjectsStore } from "../store/flowProjects";
import { CHAR_PREFIX, REF_PREFIX, SCENE_PREFIX, groupName, useFlowStudioStore } from "../store/flowStudio";
import { FlowComposer } from "./FlowComposer";
import { FlowViewer } from "./FlowViewer";

/**
 * Flow Studio — a Google-Flow-style image workspace, fully separate from the
 * Manga branch (own URL, own projects). Left rail = the project list; center =
 * the project's asset grid + bottom composer; clicking a tile opens a detail
 * overlay. Generation settings live in the composer popover.
 */
export function FlowApp() {
  const setMode = useAppModeStore((s) => s.setMode);

  const assets = useFlowStudioStore((s) => s.assets);
  const loading = useFlowStudioStore((s) => s.loading);
  const generating = useFlowStudioStore((s) => s.generating);
  const loadAssets = useFlowStudioStore((s) => s.load);
  const select = useFlowStudioStore((s) => s.select);
  const selectedMediaId = useFlowStudioStore((s) => s.selectedMediaId);
  const uploadAsset = useFlowStudioStore((s) => s.uploadAsset);
  const reusePrompt = useFlowStudioStore((s) => s.reusePrompt);
  const togglePin = useFlowStudioStore((s) => s.togglePin);
  const tagToPrompt = useFlowStudioStore((s) => s.tagToPrompt);
  const error = useFlowStudioStore((s) => s.error);
  const clearError = useFlowStudioStore((s) => s.clearError);
  const notice = useFlowStudioStore((s) => s.notice);
  const clearNotice = useFlowStudioStore((s) => s.clearNotice);

  const projects = useFlowProjectsStore((s) => s.projects);
  const activeId = useFlowProjectsStore((s) => s.activeId);
  const loadProjects = useFlowProjectsStore((s) => s.load);
  const switchTo = useFlowProjectsStore((s) => s.switchTo);
  const createProject = useFlowProjectsStore((s) => s.create);
  const renameProject = useFlowProjectsStore((s) => s.rename);
  const removeProject = useFlowProjectsStore((s) => s.remove);

  const [query, setQuery] = useState("");
  const [collapsed, setCollapsed] = useState(false);
  const [dragOver, setDragOver] = useState(false);
  const dragDepth = useRef(0);

  useEffect(() => {
    loadProjects();
  }, [loadProjects]);
  // Reload the active project's library whenever the project changes.
  useEffect(() => {
    loadAssets();
  }, [loadAssets, activeId]);

  let gridAssets = assets;
  if (query.trim()) {
    const q = query.trim().toLowerCase();
    gridAssets = gridAssets.filter(
      (a) => a.label.toLowerCase().includes(q) || (a.prompt ?? "").toLowerCase().includes(q),
    );
  }
  gridAssets = [...gridAssets].sort((a, b) => Number(b.pinned) - Number(a.pinned));

  const onDragEnter = (e: React.DragEvent) => {
    if (![...e.dataTransfer.types].includes("Files")) return;
    e.preventDefault();
    dragDepth.current += 1;
    setDragOver(true);
  };
  const onDragOver = (e: React.DragEvent) => {
    if ([...e.dataTransfer.types].includes("Files")) e.preventDefault();
  };
  const onDragLeave = () => {
    dragDepth.current = Math.max(0, dragDepth.current - 1);
    if (dragDepth.current === 0) setDragOver(false);
  };
  const onDrop = async (e: React.DragEvent) => {
    e.preventDefault();
    dragDepth.current = 0;
    setDragOver(false);
    const files = Array.from(e.dataTransfer.files).filter((f) => f.type.startsWith("image/"));
    for (const f of files) await uploadAsset(f);
  };

  const newProject = () => {
    const name = window.prompt("Tên project mới:", `Project ${projects.length + 1}`);
    if (name === null) return;
    createProject(name.trim() || "Untitled");
  };

  return (
    <div className={`flow-app${collapsed ? " flow-app--rail-collapsed" : ""}`}>
      {/* ── left rail = project list ── */}
      <aside className="fn">
        <div className="fn__brand">
          <span className="fn__logo">✦</span>
          {!collapsed && <span className="fn__title">Flow Studio</span>}
        </div>

        {!collapsed && (
          <button type="button" className="fn__newproj" onClick={newProject}>
            ＋ Project mới
          </button>
        )}

        <div className="fn__projects">
          {projects.map((p) => (
            <div
              key={p.id}
              className={`fn__prow${p.id === activeId ? " is-on" : ""}`}
              onClick={() => p.id !== undefined && switchTo(p.id)}
              title={p.name}
            >
              <span className="fn__prow-name">{collapsed ? "▦" : p.name}</span>
              {!collapsed && (
                <span className="fn__prow-actions">
                  <button
                    type="button"
                    className="fn__prow-act"
                    title="Đổi tên"
                    onClick={(e) => {
                      e.stopPropagation();
                      const n = window.prompt("Đổi tên project:", p.name);
                      if (n && n.trim() && p.id !== undefined) renameProject(p.id, n.trim());
                    }}
                  >
                    ✎
                  </button>
                  {projects.length > 1 && (
                    <button
                      type="button"
                      className="fn__prow-act"
                      title="Xoá project"
                      onClick={(e) => {
                        e.stopPropagation();
                        if (p.id !== undefined && window.confirm(`Xoá project "${p.name}"?`)) removeProject(p.id);
                      }}
                    >
                      🗑
                    </button>
                  )}
                </span>
              )}
            </div>
          ))}
        </div>

        <div className="fn__spacer" />
        <FlowUsageBadge collapsed={collapsed} />
        <button
          type="button"
          className="fn__ghost"
          onClick={() => setCollapsed((c) => !c)}
          title={collapsed ? "Mở rộng" : "Thu gọn"}
        >
          {collapsed ? "»" : "« Thu gọn"}
        </button>
        {!FLOW_ONLY && (
          <button type="button" className="fn__ghost" onClick={() => setMode("manga")} title="Sang Manga Extract">
            {collapsed ? "↤" : "↤ Manga Extract"}
          </button>
        )}
      </aside>

      {/* ── center: top bar + grid + composer ── */}
      <main
        className="fc-center"
        onDragEnter={onDragEnter}
        onDragOver={onDragOver}
        onDragLeave={onDragLeave}
        onDrop={onDrop}
      >
        {dragOver && (
          <div className="fc-drop">
            <div className="fc-drop__inner">⬇ Thả ảnh vào đây để tải lên</div>
          </div>
        )}

        <div className="fc-top">
          <input
            className="fc-search"
            placeholder="Tìm theo prompt…"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
          />
          <span className="fc-count">{gridAssets.length} mục</span>
        </div>

        {error && (
          <div className="fc-banner fc-banner--err" onClick={clearError} role="alert">
            ⚠ {error}
            <span className="fc-banner__x">✕</span>
          </div>
        )}
        {notice && (
          <div className="fc-banner fc-banner--info" onClick={clearNotice} role="status">
            ℹ {notice}
            <span className="fc-banner__x">✕</span>
          </div>
        )}

        <div className="fc-scroll">
          {loading ? (
            <div className="fc-empty">Đang tải…</div>
          ) : gridAssets.length === 0 && !generating ? (
            <div className="fc-empty">
              <div className="fc-empty__icon">❀</div>
              <div className="fc-empty__text">
                {query.trim()
                  ? "Không tìm thấy ảnh khớp."
                  : "Bắt đầu tạo: gõ prompt ở dưới, hoặc kéo-thả ảnh vào đây."}
              </div>
            </div>
          ) : (
            <div className="fc-grid">
              {generating && <GenPlaceholders />}
              {gridAssets.map((a) => (
                <div
                  key={a.refId}
                  role="button"
                  tabIndex={0}
                  className={`fc-card${selectedMediaId === a.mediaId ? " is-selected" : ""}`}
                  onClick={() => select(a.mediaId)}
                  onKeyDown={(e) => {
                    if (e.key === "Enter" || e.key === " ") {
                      e.preventDefault();
                      select(a.mediaId);
                    }
                  }}
                  title={a.prompt ?? a.label}
                >
                  <img src={thumbUrl(a.mediaId, 400)} alt={a.label} loading="lazy" decoding="async" />
                  {a.pinned && <span className="fc-badge fc-badge--pin">📌</span>}
                  {groupName(a.tags, CHAR_PREFIX) && <span className="fc-badge">👤</span>}
                  {groupName(a.tags, SCENE_PREFIX) && <span className="fc-badge fc-badge--scene">🎬</span>}
                  <div className="fc-card__bar">
                    <button
                      type="button"
                      className="fc-card__act"
                      title="Thêm ảnh vào câu lệnh"
                      onClick={(e) => {
                        e.stopPropagation();
                        const cn = groupName(a.tags, CHAR_PREFIX);
                        const sn = groupName(a.tags, SCENE_PREFIX);
                        tagToPrompt(cn || sn || a.label || "ảnh", a.mediaId, [a.mediaId]);
                      }}
                    >
                      @
                    </button>
                    {a.prompt && (
                      <button
                        type="button"
                        className="fc-card__act"
                        title="Sử dụng lại câu lệnh"
                        onClick={(e) => {
                          e.stopPropagation();
                          const refIds = a.tags
                            .filter((t) => t.startsWith(REF_PREFIX))
                            .map((t) => t.slice(REF_PREFIX.length));
                          reusePrompt(a.prompt ?? "", refIds);
                        }}
                      >
                        ↩
                      </button>
                    )}
                    <button
                      type="button"
                      className={`fc-card__act${a.pinned ? " is-on" : ""}`}
                      title={a.pinned ? "Bỏ ghim" : "Ghim"}
                      onClick={(e) => {
                        e.stopPropagation();
                        togglePin(a.refId);
                      }}
                    >
                      📌
                    </button>
                  </div>
                  {a.prompt && <div className="fc-card__cap">{a.prompt}</div>}
                </div>
              ))}
            </div>
          )}
        </div>

        <FlowComposer />
      </main>

      <FlowViewer />
    </div>
  );
}

function cssAspect(a: string): string {
  const [w, h] = a.split(":");
  return w && h ? `${w} / ${h}` : "1 / 1";
}

/** Rough expected seconds PER IMAGE for the progress estimate, from the chosen
 *  model + resolution. The image API gives no true intra-image %, so this just
 *  paces the estimate to real-ish durations (Pro/4K is much slower than flash). */
function expectedSecondsPerImage(model: string, size: string): number {
  const base = model.includes("pro") ? 30 : 13;
  const mult = size === "4K" ? 2.8 : size === "2K" ? 1.7 : 1;
  return base * mult;
}

/** Per-tile climbing %, paced to ~real durations. Variants now generate in
 *  PARALLEL, so all tiles climb together: each eases 0→88 over the expected
 *  per-image time then crawls 88→99 if it overruns. The backend's real `done`
 *  count snaps that many tiles to a true 100 as variants finish. */
function useTilePcts(
  progress: { done: number; total: number } | null,
  generating: boolean,
  total: number,
  expS: number,
): number[] {
  const [pcts, setPcts] = useState<number[]>(() => Array(total).fill(0));
  const ref = useRef(progress);
  ref.current = progress;
  useEffect(() => {
    if (!generating) {
      setPcts(Array(total).fill(0));
      return;
    }
    const start = Date.now();
    const id = setInterval(() => {
      const done = Math.min(ref.current?.done ?? 0, total);
      const e = (Date.now() - start) / 1000;
      const linear = Math.min((e / expS) * 88, 88);
      const tail = e <= expS ? 0 : (1 - Math.exp(-(e - expS) / (expS * 1.5))) * 11;
      const est = Math.min(99, Math.round(linear + tail));
      setPcts(Array.from({ length: total }, (_, i) => (i < done ? 100 : est)));
    }, 300);
    return () => clearInterval(id);
  }, [generating, total, expS]);
  return pcts;
}

/** Daily usage badge in the rail. Counts images this tool generated today
 *  (≈ Atrium usage, since this key is tool-only) and an estimated remaining
 *  quota. Refetches whenever a generation lands (assets count changes). */
function FlowUsageBadge({ collapsed }: { collapsed: boolean }) {
  const assetCount = useFlowStudioStore((s) => s.assets.length);
  const generating = useFlowStudioStore((s) => s.generating);
  const [usage, setUsage] = useState<FlowUsage | null>(null);
  useEffect(() => {
    let live = true;
    getFlowUsage()
      .then((u) => live && setUsage(u))
      .catch(() => {});
    return () => {
      live = false;
    };
  }, [assetCount, generating]);
  if (!usage) return null;
  const pct = Math.min(100, Math.round((usage.today / Math.max(1, usage.daily_quota)) * 100));
  if (collapsed) {
    return (
      <div className="fn__usage fn__usage--mini" title={`Hôm nay: ${usage.today}/${usage.daily_quota} ảnh`}>
        {usage.today}
      </div>
    );
  }
  return (
    <div className="fn__usage" title={`Tổng đã tạo: ${usage.total} ảnh`}>
      <div className="fn__usage-row">
        <span>Hôm nay</span>
        <span>
          {usage.today}/{usage.daily_quota}
        </span>
      </div>
      <div className="fn__usage-bar">
        <div className="fn__usage-fill" style={{ width: `${pct}%` }} />
      </div>
      <div className="fn__usage-sub">~{usage.remaining_est} ảnh còn lại hôm nay (ước tính)</div>
    </div>
  );
}

/** Flow-style loading tiles — gradient frame in the chosen aspect ratio, image
 *  icon top-left, own climbing % top-right. */
function GenPlaceholders() {
  // Aggregate every in-flight generation into one set of tiles (several can run
  // at once now), so each pending image shows its own climbing %.
  const jobs = useFlowStudioStore((s) => s.genJobs);
  const aspect = useFlowStudioStore((s) => s.settings.aspect);
  const model = useFlowStudioStore((s) => s.settings.model);
  const size = useFlowStudioStore((s) => s.settings.size);
  const total = Math.max(1, jobs.reduce((n, j) => n + j.total, 0));
  const done = jobs.reduce((n, j) => n + j.done, 0);
  const pcts = useTilePcts({ done, total }, jobs.length > 0, total, expectedSecondsPerImage(model, size));
  return (
    <>
      {Array.from({ length: total }).map((_, i) => (
        <div key={`gen-${i}`} className="fc-card fc-card--loading" style={{ aspectRatio: cssAspect(aspect) }}>
          <span className="fc-load__icon" aria-hidden="true">🖼</span>
          <span className="fc-load__pct">{pcts[i] ?? 0}%</span>
        </div>
      ))}
    </>
  );
}
