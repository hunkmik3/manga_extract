import { useEffect, useRef } from "react";
import { ReactFlowProvider } from "@xyflow/react";
import { Board } from "./canvas/Board";
import { AddNodePalette } from "./canvas/AddNodePalette";
import { StatusBar } from "./components/StatusBar";
import { Toolbar } from "./components/Toolbar";
import { ProjectSidebar } from "./components/ProjectSidebar";
import { Toaster } from "./components/Toaster";
import { useBoardStore } from "./store/board";
import { useAppModeStore } from "./store/appMode";
import { FlowApp } from "./flow/FlowApp";

// Inherited flowboard surfaces (References panel, image/video Generation dialog,
// Result viewer, AI-provider setup gate, chat) are intentionally not rendered —
// this build is focused on the manhwa → panel extraction workflow.
//
// Two top-level branches share this app (see store/appMode):
//   - "manga" → the node board below (original tool).
//   - "flow"  → FlowApp, a Google-Flow-style image studio.

export function App() {
  // Hooks run unconditionally (before the mode branch) so hook order stays
  // stable across renders regardless of which branch is active.
  const mode = useAppModeStore((s) => s.mode);
  const setMode = useAppModeStore((s) => s.setMode);
  const syncFromUrl = useAppModeStore((s) => s.syncFromUrl);
  const loadInitialBoard = useBoardStore((s) => s.loadInitialBoard);
  const loading = useBoardStore((s) => s.loading);
  const boardId = useBoardStore((s) => s.boardId);
  const ran = useRef(false);

  // Load the Manga board only when on the Manga surface — avoids creating a
  // stray "Untitled" manga board for someone who only opens /flow.
  useEffect(() => {
    if (mode !== "manga" || ran.current) return;
    ran.current = true;
    loadInitialBoard();
  }, [mode, loadInitialBoard]);

  // Keep mode in sync with browser back/forward.
  useEffect(() => {
    const onPop = () => syncFromUrl();
    window.addEventListener("popstate", onPop);
    return () => window.removeEventListener("popstate", onPop);
  }, [syncFromUrl]);

  if (mode === "flow") return <FlowApp />;

  return (
    <div className="app">
      <ProjectSidebar />
      <ReactFlowProvider>
        <div className="canvas-wrap">
          <Toolbar />
          <button
            type="button"
            className="mode-switch"
            onClick={() => setMode("flow")}
            title="Open Flow Studio — generate Google Flow-style images via API"
          >
            ✦ Flow Studio
          </button>
          {loading && boardId === null ? (
            <div className="canvas-loading">Loading board…</div>
          ) : (
            <>
              <Board />
              <AddNodePalette />
            </>
          )}
          <StatusBar />
        </div>
      </ReactFlowProvider>
      <Toaster />
    </div>
  );
}
