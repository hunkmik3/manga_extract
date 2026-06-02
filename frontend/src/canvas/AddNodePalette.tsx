import { useReactFlow } from "@xyflow/react";
import { useBoardStore } from "../store/board";
import type { NodeType } from "../store/board";

interface Chip {
  type: NodeType;
  icon: string;
  label: string;
}

// Only the comic-pipeline entry node is offered. The page/panel nodes are
// spawned by the upload node, not added manually; the inherited flowboard
// node types (character/image/video/…) are hidden to keep this focused on the
// manhwa → panel extraction workflow.
const CHIPS: Chip[] = [
  { type: "comic_import", icon: "▤", label: "Comic upload" },
  { type: "comic_clean", icon: "✂", label: "Comic clean" },
  { type: "comic_enhance", icon: "✧", label: "Comic enhance" },
];

export function AddNodePalette() {
  const { screenToFlowPosition } = useReactFlow();
  const addNodeOfType = useBoardStore((s) => s.addNodeOfType);

  function handleAdd(type: NodeType) {
    const position = screenToFlowPosition({
      x: window.innerWidth / 2,
      y: window.innerHeight / 2,
    });
    addNodeOfType(type, position);
  }

  return (
    <div className="add-node-palette" aria-label="Add node">
      <span className="add-node-plus" aria-hidden="true">+</span>
      {CHIPS.map((chip) => (
        <button
          key={chip.type}
          className="add-node-chip"
          aria-label={`Add ${chip.label} node`}
          onClick={() => handleAdd(chip.type)}
        >
          <span aria-hidden="true">{chip.icon}</span>
          {chip.label}
        </button>
      ))}
    </div>
  );
}
