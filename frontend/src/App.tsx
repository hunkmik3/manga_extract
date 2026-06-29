import { useEffect } from "react";
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

// Three top-level branches share this app (see store/appMode):
//   - "manga"  → node board, splits pages into PANELS.
//   - "bubble" → same node board, splits pages into SPEECH BUBBLES (own
//                workspace: board kind "bubble", bubble detector).
//   - "flow"   → FlowApp, a Google-Flow-style image studio.

export function App() {
  // Hooks run unconditionally (before the mode branch) so hook order stays
  // stable across renders regardless of which branch is active.
  const mode = useAppModeStore((s) => s.mode);
  const setMode = useAppModeStore((s) => s.setMode);
  const syncFromUrl = useAppModeStore((s) => s.syncFromUrl);
  const loadInitialBoard = useBoardStore((s) => s.loadInitialBoard);
  const loading = useBoardStore((s) => s.loading);
  const boardId = useBoardStore((s) => s.boardId);

  // Load the board workspace for the active node-board branch. Manga and Bubble
  // are separate workspaces (board kind "manga" / "bubble"); switching between
  // them reloads the right one. Flow has no board (skip — avoids a stray board).
  useEffect(() => {
    if (mode === "manga" || mode === "bubble") loadInitialBoard(mode);
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
          <div className="mode-nav">
            {/* Switch to the OTHER node-board branch. */}
            {mode === "manga" ? (
              <button
                type="button"
                className="mode-switch"
                onClick={() => setMode("bubble")}
                title="Open Bubble Extract — split pages into speech bubbles"
              >
                💬 Bubble Extract
              </button>
            ) : (
              <button
                type="button"
                className="mode-switch"
                onClick={() => setMode("manga")}
                title="Open Manga Extract — split pages into panels"
              >
                🗂 Manga Extract
              </button>
            )}
            <button
              type="button"
              className="mode-switch"
              onClick={() => setMode("flow")}
              title="Open Flow Studio — generate Google Flow-style images via API"
            >
              ✦ Flow Studio
            </button>
          </div>
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
