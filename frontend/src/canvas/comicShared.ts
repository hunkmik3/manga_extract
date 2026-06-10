import {
  createNodesBulk,
  createRequest,
  deleteNode,
  getRequest,
  patchNode,
  type BulkNodeInput,
  type RequestDTO,
} from "../api/client";
import { useBoardStore, type FlowboardNodeData, type FlowNode } from "../store/board";

// Fan-out layout constants (shared by spawn + relayout so they stay in sync).
export const PAGE_GAP_X = 360; // upload → page column x offset
export const PAGE_W = 240;
export const PAGE_H = 300;
export const PANEL_COLS = 3;
export const PANEL_CELL_W = 165;
export const PANEL_CELL_H = 185;
export const ROW_GAP = 60;
const COMBINE_GAP_Y = 56;
const DEFAULT_COMBINE_H = 420;

export const COMIC_TYPES = new Set([
  "comic_import",
  "comic_page",
  "comic_panel",
  "comic_detect",
  "comic_panels",
]);

export interface PageItem {
  idx: number;
  name: string;
  mediaId: string;
  w?: number;
  h?: number;
  boxes?: BoxItem[];
  error?: string | null;
}
export interface BoxItem {
  id: string;
  x: number;
  y: number;
  w: number;
  h: number;
}
export interface PanelItem {
  idx: number;
  pageIndex?: number;
  pageName?: string;
  panelIndex?: number;
  box?: { x: number; y: number; w: number; h: number };
  mediaId: string;
}

/** Data of the single upstream comic node feeding `rfId` (via one edge), or null. */
export function getUpstreamComicData(rfId: string): FlowboardNodeData | null {
  const { nodes, edges } = useBoardStore.getState();
  for (const e of edges) {
    if (e.target !== rfId) continue;
    const src = nodes.find((n) => n.id === e.source);
    if (src && COMIC_TYPES.has(src.data.type)) return src.data;
  }
  return null;
}

/**
 * Shared dispatch: optimistic status → create request → poll → apply result.
 * `onResult` maps the worker result into a node.data patch (also persisted).
 * Returns immediately; the poll runs in the background.
 */
export function runComicRequest(
  rfId: string,
  makeReq: () => Promise<RequestDTO>,
  onResult: (result: Record<string, unknown>) => Partial<FlowboardNodeData>,
): void {
  const update = useBoardStore.getState().updateNodeData;
  const dbId = parseInt(rfId, 10);
  if (Number.isNaN(dbId)) return;

  update(rfId, { status: "queued", error: undefined });
  (async () => {
    let req: RequestDTO;
    try {
      req = await makeReq();
    } catch (err) {
      update(rfId, { status: "error", error: String(err) });
      return;
    }
    update(rfId, { status: "running" });

    const poll = async () => {
      try {
        const r = await getRequest(req.id);
        if (r.status === "done") {
          const patch = onResult(r.result ?? {});
          update(rfId, { status: "done", error: undefined, ...patch });
          patchNode(dbId, { status: "done", data: patch }).catch(() => {});
        } else if (r.status === "failed" || r.status === "timeout" || r.status === "canceled") {
          update(rfId, { status: "error", error: r.error ?? "failed" });
          patchNode(dbId, { status: "error" }).catch(() => {});
        } else {
          setTimeout(poll, 1200);
        }
      } catch (err) {
        update(rfId, { status: "error", error: String(err) });
      }
    };
    setTimeout(poll, 800);
  })();
}

/** Persist a node.data patch both locally (store) and to the backend. */
export function patchComicNode(rfId: string, patch: Partial<FlowboardNodeData>): void {
  useBoardStore.getState().updateNodeData(rfId, patch);
  const dbId = parseInt(rfId, 10);
  if (!Number.isNaN(dbId)) patchNode(dbId, { data: patch }).catch(() => {});
}

/** Create a request and await its terminal result (for fan-out spawning where
 * we need the result inline, not just a node-data patch). */
export async function runRequestToResult(reqPromise: Promise<RequestDTO>): Promise<Record<string, unknown>> {
  const dto = await reqPromise;
  for (let i = 0; i < 900; i++) {
    const r = await getRequest(dto.id);
    if (r.status === "done") return r.result ?? {};
    if (r.status === "failed" || r.status === "timeout" || r.status === "canceled") {
      throw new Error(r.error ?? r.status);
    }
    await new Promise((res) => setTimeout(res, 1000));
  }
  throw new Error("timeout waiting for request");
}

/** Canvas position of a node by rfId (for laying out spawned children). */
export function nodePosition(rfId: string): { x: number; y: number } {
  const n = useBoardStore.getState().nodes.find((nn) => nn.id === rfId);
  return n ? { x: n.position.x, y: n.position.y } : { x: 0, y: 0 };
}

/** Resolve the image feeding `rfId` from its single upstream node, as input
 * for the bridge clean/enhance tasks: a comic_panel → (its page image + box),
 * any node with a result mediaId (e.g. a clean node) → that media. */
export interface UpstreamImage {
  sourceMediaId?: string;
  pageMediaId?: string;
  box?: BoxItem;
  label?: string;
}
export function resolveUpstreamImage(rfId: string): UpstreamImage | null {
  const { nodes, edges } = useBoardStore.getState();
  for (const e of edges) {
    if (e.target !== rfId) continue;
    const src = nodes.find((n) => n.id === e.source);
    if (!src) continue;
    const d = src.data;
    if (d.type === "comic_panel") {
      const pageMediaId = d.pageMediaId as string | undefined;
      const boxId = d.boxId as string | undefined;
      const pageNodeId = d.pageNodeId as string | undefined;
      if (pageMediaId && boxId && pageNodeId) {
        const pageNode = nodes.find((n) => n.id === pageNodeId);
        const box = ((pageNode?.data.boxes as BoxItem[]) ?? []).find((b) => b.id === boxId);
        if (box) return { pageMediaId, box, label: `${d.pageName ?? ""} #${((d.panelIndex as number) ?? 0) + 1}` };
      }
    }
    if (typeof d.mediaId === "string" && d.mediaId) {
      // carry the originating page (stored by the clean node) so enhance can
      // still pass it as a consistency reference
      return {
        sourceMediaId: d.mediaId,
        pageMediaId: typeof d.pageMediaId === "string" ? d.pageMediaId : undefined,
        label: (d.title as string) ?? "image",
      };
    }
  }
  return null;
}

export interface CharacterItem {
  id: string;
  name: string;
  count: number;
  refMediaIds: string[];
  sampleMediaId: string;
  pages?: number[];
}
/** The character DB from the (single) comic_chars node on the board, if built.
 * Passed to enhance so it can auto-match each panel's character. */
export function findCharacterDb(): CharacterItem[] | null {
  const { nodes } = useBoardStore.getState();
  for (const n of nodes) {
    if (n.data.type === "comic_chars" && Array.isArray(n.data.characters) && n.data.characters.length) {
      return n.data.characters as CharacterItem[];
    }
  }
  return null;
}

export interface DownstreamPage {
  dbId: number;
  data: FlowboardNodeData;
}
/** comic_page nodes connected directly downstream of `rfId` (the upload node). */
export function downstreamPageNodes(rfId: string): DownstreamPage[] {
  const { nodes, edges } = useBoardStore.getState();
  const out: DownstreamPage[] = [];
  for (const e of edges) {
    if (e.source !== rfId) continue;
    const tgt = nodes.find((n) => n.id === e.target);
    if (tgt && tgt.data.type === "comic_page") out.push({ dbId: parseInt(tgt.id, 10), data: tgt.data });
  }
  return out;
}

/**
 * Re-pack every comic chain (upload → pages → panels) into the canonical
 * layout: pages stacked in a column under their upload, each page's panels in a
 * grid beside it, row heights sized to panel count. Closes gaps left by deleted
 * nodes (the ones below "run up"). Updates the store immediately + persists
 * positions. Order is preserved by pageIdx / panelIndex.
 */
const panelX = (baseX: number, j: number) => baseX + PAGE_W + 60 + (j % PANEL_COLS) * PANEL_CELL_W;
const panelY = (baseY: number, j: number) => baseY + Math.floor(j / PANEL_COLS) * PANEL_CELL_H;

/**
 * Reconcile the panel nodes of ONE page with its current boxes: create a panel
 * for any new box, delete the panel of any removed box, and lay them in a grid
 * beside the page (in box order). Existing panels update their crop live via
 * CSS, so this only runs creates/deletes/repositions. Call after a box edit so
 * "draw a box → its panel appears at node 3" works without re-running ②.
 */
export async function syncPanelsForPage(pageRfId: string): Promise<void> {
  const store = useBoardStore.getState();
  const page = store.nodes.find((n) => n.id === pageRfId);
  if (!page || page.data.type !== "comic_page") return;
  const boardId = store.boardId;
  if (boardId == null) return;

  const boxes = (page.data.boxes as BoxItem[]) ?? [];
  const pageMediaId = page.data.pageMediaId as string;
  const pageName = (page.data.pageName as string) ?? "";
  const pageDbId = parseInt(pageRfId, 10);
  const bx = page.position.x;
  const by = page.position.y;

  const existing = new Map<string, string>(); // boxId → panel rfId
  for (const e of store.edges) {
    if (e.source !== pageRfId) continue;
    const t = store.nodes.find((n) => n.id === e.target);
    if (t && t.data.type === "comic_panel" && typeof t.data.boxId === "string") existing.set(t.data.boxId, t.id);
  }
  const currentIds = new Set(boxes.map((b) => b.id));

  // create panels for boxes that don't have one yet
  const toCreate = boxes.map((b, j) => ({ b, j })).filter(({ b }) => !existing.has(b.id));
  if (toCreate.length > 0) {
    const bulk: BulkNodeInput[] = toCreate.map(({ b, j }) => ({
      type: "comic_panel",
      x: panelX(bx, j),
      y: panelY(by, j),
      w: 150,
      h: 150,
      data: { pageNodeId: pageRfId, pageMediaId, boxId: b.id, pageName, panelIndex: j, title: `${pageName} #${j + 1}` },
      source_id: pageDbId,
    }));
    const res = await createNodesBulk(boardId, bulk);
    useBoardStore.getState().appendNodesBulk(res.nodes, res.edges);
  }

  // delete panels whose box was removed
  const toDelete = [...existing].filter(([boxId]) => !currentIds.has(boxId)).map(([, rfId]) => rfId);
  if (toDelete.length > 0) {
    const del = new Set(toDelete);
    const s2 = useBoardStore.getState();
    s2.setNodes(s2.nodes.filter((n) => !del.has(n.id)));
    s2.setEdges(s2.edges.filter((e) => !del.has(e.target)));
    toDelete.forEach((rfId) => { const id = parseInt(rfId, 10); if (!Number.isNaN(id)) deleteNode(id).catch(() => {}); });
  }

  // keep this page's panels tidy: grid by box order
  const after = useBoardStore.getState();
  const moves: { id: string; x: number; y: number }[] = [];
  for (const e of after.edges) {
    if (e.source !== pageRfId) continue;
    const t = after.nodes.find((n) => n.id === e.target);
    if (!t || t.data.type !== "comic_panel") continue;
    const j = boxes.findIndex((b) => b.id === t.data.boxId);
    if (j < 0) continue;
    const nx = panelX(bx, j);
    const ny = panelY(by, j);
    if (t.position.x !== nx || t.position.y !== ny) moves.push({ id: t.id, x: nx, y: ny });
  }
  if (moves.length > 0) {
    const m = new Map(moves.map((mv) => [mv.id, mv]));
    const s3 = useBoardStore.getState();
    s3.setNodes(s3.nodes.map((n) => { const mv = m.get(n.id); return mv ? { ...n, position: { x: mv.x, y: mv.y } } : n; }));
    moves.forEach((mv) => { const id = parseInt(mv.id, 10); if (!Number.isNaN(id)) patchNode(id, { x: mv.x, y: mv.y }).catch(() => {}); });
  }
}

/** boxIds of every panel that belongs to a combine group. Those panels are laid
 * out next to their combine (see relayoutComicCombineChains) instead of in the
 * page→panel grid, so this set tells relayoutComicChains to skip them. */
function combineOwnedBoxIds(nodes: FlowNode[]): Set<string> {
  const owned = new Set<string>();
  for (const c of nodes.filter((n) => n.data.type === "comic_combine")) {
    const panels = Array.isArray(c.data.panels) ? (c.data.panels as Array<Record<string, unknown>>) : [];
    for (const p of panels) {
      if (p && typeof p.boxId === "string") owned.add(p.boxId);
    }
  }
  return owned;
}

export function relayoutComicChains(): void {
  const store = useBoardStore.getState();
  const { nodes, edges } = store;
  const moves = new Map<string, { x: number; y: number }>();
  const owned = combineOwnedBoxIds(nodes); // panels claimed by a combine sit beside it

  for (const up of nodes.filter((n) => n.data.type === "comic_import")) {
    const pageX = up.position.x + PAGE_GAP_X;
    let curY = up.position.y;
    const pageNodes = edges
      .filter((e) => e.source === up.id)
      .map((e) => nodes.find((n) => n.id === e.target))
      .filter((n): n is FlowNode => !!n && n.data.type === "comic_page")
      .sort((a, b) => ((a.data.pageIdx as number) ?? 0) - ((b.data.pageIdx as number) ?? 0));

    for (const pn of pageNodes) {
      const pageBoxes = (pn.data.boxes as BoxItem[]) ?? [];
      const boxOrder = (n: FlowNode) => {
        const i = pageBoxes.findIndex((b) => b.id === n.data.boxId);
        return i < 0 ? 1e9 : i;
      };
      const panelNodes = edges
        .filter((e) => e.source === pn.id)
        .map((e) => nodes.find((n) => n.id === e.target))
        .filter((n): n is FlowNode => !!n && n.data.type === "comic_panel")
        .filter((n) => !owned.has(n.data.boxId as string)) // combine-owned panels move beside it
        .sort((a, b) => boxOrder(a) - boxOrder(b));

      const count = panelNodes.length || 1;
      const rows = Math.max(1, Math.ceil(count / PANEL_COLS));
      // Use the page node's real (measured) height — tall webtoon pages are far
      // taller than PAGE_H, so a fixed advance would overlap the next page.
      const rowH = Math.max(pageNodeHeight(pn), rows * PANEL_CELL_H) + ROW_GAP;

      moves.set(pn.id, { x: pageX, y: curY });
      panelNodes.forEach((qn, j) => {
        moves.set(qn.id, { x: panelX(pageX, j), y: panelY(curY, j) });
      });
      curY += rowH;
    }
  }

  if (moves.size === 0) return;
  const newNodes = nodes.map((n) => {
    const m = moves.get(n.id);
    return m && (n.position.x !== m.x || n.position.y !== m.y) ? { ...n, position: m } : n;
  });
  store.setNodes(newNodes);
  for (const [rfId, p] of moves) {
    const dbId = parseInt(rfId, 10);
    if (!Number.isNaN(dbId)) patchNode(dbId, { x: p.x, y: p.y }).catch(() => {});
  }
}

function pageNodeHeight(n: FlowNode): number {
  const measured = n.data.__measuredHeight;
  if (typeof measured === "number" && Number.isFinite(measured) && measured > 0) {
    return Math.max(PAGE_H, measured);
  }
  return PAGE_H;
}

function combineNodeHeight(n: FlowNode): number {
  const measured = n.data.__measuredHeight;
  if (typeof measured === "number" && Number.isFinite(measured) && measured > 0) {
    return Math.max(180, measured);
  }
  return DEFAULT_COMBINE_H;
}

/** Re-pack combine nodes in each upload chain using their measured card height.
 * Combine cards grow after a 9:16 result/cell grid appears; without moving the
 * later combine nodes, the enlarged card visually overlaps the next one. */
export function relayoutComicCombineChains(): void {
  const store = useBoardStore.getState();
  const { nodes, edges } = store;
  const moves = new Map<string, { x: number; y: number }>();

  for (const up of nodes.filter((n) => n.data.type === "comic_import")) {
    const combineNodes = edges
      .filter((e) => e.source === up.id)
      .map((e) => nodes.find((n) => n.id === e.target))
      .filter((n): n is FlowNode => !!n && n.data.type === "comic_combine")
      .sort((a, b) => a.position.y - b.position.y);

    if (combineNodes.length === 0) continue;

    let curY = combineNodes[0].position.y;
    const SRC_COLS = 2;
    const SRC_GAP_X = 40; // gap between a combine's source-panel grid and the card
    const gridW = SRC_COLS * PANEL_CELL_W;
    for (const node of combineNodes) {
      moves.set(node.id, { x: node.position.x, y: curY });
      // Pin this combine's 4 source panel nodes (matched by boxId) in a 2×2 to
      // its left, mirroring the 2×2 output for easy input→output comparison.
      const panels = Array.isArray(node.data.panels)
        ? (node.data.panels as Array<Record<string, unknown>>)
        : [];
      panels.slice(0, 4).forEach((spec, j) => {
        const boxId = spec?.boxId;
        if (typeof boxId !== "string") return;
        const pnode = nodes.find(
          (n) => n.data.type === "comic_panel" && n.data.boxId === boxId,
        );
        if (!pnode) return;
        const col = j % SRC_COLS;
        const row = Math.floor(j / SRC_COLS);
        moves.set(pnode.id, {
          x: node.position.x - SRC_GAP_X - gridW + col * PANEL_CELL_W,
          y: curY + row * PANEL_CELL_H,
        });
      });
      curY += combineNodeHeight(node) + COMBINE_GAP_Y;
    }
  }

  if (moves.size === 0) return;
  const changed = new Map<string, { x: number; y: number }>();
  const nextNodes = nodes.map((n) => {
    const mv = moves.get(n.id);
    if (!mv || (Math.abs(n.position.x - mv.x) < 1 && Math.abs(n.position.y - mv.y) < 1)) {
      return n;
    }
    changed.set(n.id, mv);
    return { ...n, position: { x: mv.x, y: mv.y } };
  });
  if (changed.size === 0) return;
  store.setNodes(nextNodes);
  for (const [rfId, p] of changed) {
    const dbId = parseInt(rfId, 10);
    if (!Number.isNaN(dbId)) patchNode(dbId, { x: p.x, y: p.y }).catch(() => {});
  }
}

export { createRequest };
