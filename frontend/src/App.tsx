import { useEffect, useRef } from "react";
import { ReactFlowProvider } from "@xyflow/react";
import { Board } from "./canvas/Board";
import { AddNodePalette } from "./canvas/AddNodePalette";
import { StatusBar } from "./components/StatusBar";
import { Toolbar } from "./components/Toolbar";
import { ProjectSidebar } from "./components/ProjectSidebar";
import { Toaster } from "./components/Toaster";
import { useBoardStore } from "./store/board";

// Inherited flowboard surfaces (References panel, image/video Generation dialog,
// Result viewer, AI-provider setup gate, chat) are intentionally not rendered —
// this build is focused on the manhwa → panel extraction workflow.

export function App() {
  const loadInitialBoard = useBoardStore((s) => s.loadInitialBoard);
  const loading = useBoardStore((s) => s.loading);
  const boardId = useBoardStore((s) => s.boardId);
  const ran = useRef(false);

  useEffect(() => {
    if (ran.current) return;
    ran.current = true;
    loadInitialBoard();
  }, [loadInitialBoard]);

  return (
    <div className="app">
      <ProjectSidebar />
      <ReactFlowProvider>
        <div className="canvas-wrap">
          <Toolbar />
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
