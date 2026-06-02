import { mediaUrl } from "../api/client";
import { useBoardStore, type FlowboardNodeData } from "../store/board";
import { type BoxItem } from "./comicShared";

/**
 * Node — one extracted panel (leaf of the comic fan-out). Renders a LIVE crop:
 * it reads the box (by id) straight from its source page node and shows that
 * region of the page image via CSS. So when the user drags/resizes the box on
 * the page node, this panel updates instantly — no re-crop round-trip.
 */
export function ComicPanelBody({ data }: { rfId: string; data: FlowboardNodeData }) {
  const pageNodeId = typeof data.pageNodeId === "string" ? data.pageNodeId : "";
  const pageMediaId = typeof data.pageMediaId === "string" ? data.pageMediaId : "";
  const boxId = typeof data.boxId === "string" ? data.boxId : "";
  const pageName = typeof data.pageName === "string" ? data.pageName : "";
  const panelIndex = typeof data.panelIndex === "number" ? data.panelIndex : undefined;

  // Subscribe to the source page node so box edits there re-render us live.
  const pageData = useBoardStore((s) => s.nodes.find((n) => n.id === pageNodeId)?.data);
  const boxes = (Array.isArray(pageData?.boxes) ? pageData?.boxes : []) as BoxItem[];
  const box = boxes.find((b) => b.id === boxId);
  const pageW = (pageData?.w as number) || 0;
  const pageH = (pageData?.h as number) || 0;

  const label = (
    <p style={{ fontSize: 9, opacity: 0.55, margin: 0, textAlign: "center" }}>
      {pageName}{panelIndex !== undefined ? ` · #${panelIndex + 1}` : ""}
    </p>
  );

  if (!pageMediaId || !box || pageW <= 0 || pageH <= 0 || box.w <= 0 || box.h <= 0) {
    return (
      <div className="node-body node-body--comic-panel" style={{ display: "flex", flexDirection: "column", gap: 4 }}>
        <p style={{ fontSize: 11, opacity: 0.6 }}>{box ? "Loading…" : "Box removed upstream"}</p>
        {label}
      </div>
    );
  }

  return (
    <div className="node-body node-body--comic-panel" style={{ display: "flex", flexDirection: "column", gap: 4 }}>
      <div style={{ width: "100%", aspectRatio: `${box.w} / ${box.h}`, overflow: "hidden", position: "relative", borderRadius: 4, background: "var(--border)" }}>
        <img
          src={mediaUrl(pageMediaId)}
          alt={pageName}
          draggable={false}
          style={{
            position: "absolute",
            top: 0,
            left: 0,
            width: `${(pageW / box.w) * 100}%`,
            height: "auto",
            transform: `translate(${(-box.x / pageW) * 100}%, ${(-box.y / pageH) * 100}%)`,
            transformOrigin: "top left",
            maxWidth: "none",
          }}
        />
      </div>
      {label}
    </div>
  );
}
