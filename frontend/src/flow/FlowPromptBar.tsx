import { useState } from "react";
import { FLOW_ASPECTS, FLOW_MODELS, useFlowStudioStore } from "../store/flowStudio";

/**
 * Bottom command bar — the Flow studio's center of gravity. Type a prompt,
 * pick aspect / count / model, hit send. Mirrors Google Flow's generation
 * panel (Hình ảnh / Video tabs, aspect chips, 1x–4x, model dropdown).
 */
export function FlowPromptBar() {
  const generating = useFlowStudioStore((s) => s.generating);
  const settings = useFlowStudioStore((s) => s.settings);
  const setSettings = useFlowStudioStore((s) => s.setSettings);
  const generate = useFlowStudioStore((s) => s.generate);

  const [prompt, setPrompt] = useState("");
  const [kind, setKind] = useState<"image" | "video">("image");

  const submit = () => {
    const text = prompt.trim();
    if (!text || generating || kind === "video") return;
    generate(text);
  };

  return (
    <div className="flow-promptbar">
      <div className="flow-promptbar__settings">
        <div className="flow-seg">
          <button
            type="button"
            className={`flow-seg__btn${kind === "image" ? " is-active" : ""}`}
            onClick={() => setKind("image")}
          >
            🖼 Image
          </button>
          <button
            type="button"
            className={`flow-seg__btn${kind === "video" ? " is-active" : ""}`}
            onClick={() => setKind("video")}
            title="Coming soon — this build focuses on image generation"
          >
            ▶ Video
          </button>
        </div>

        <div className="flow-chips" role="group" aria-label="Aspect ratio">
          {FLOW_ASPECTS.map((a) => (
            <button
              key={a}
              type="button"
              className={`flow-chip${settings.aspect === a ? " is-active" : ""}`}
              onClick={() => setSettings({ aspect: a })}
            >
              {a}
            </button>
          ))}
        </div>

        <div className="flow-chips" role="group" aria-label="Count">
          {[1, 2, 3, 4].map((n) => (
            <button
              key={n}
              type="button"
              className={`flow-chip${settings.count === n ? " is-active" : ""}`}
              onClick={() => setSettings({ count: n })}
            >
              {n}x
            </button>
          ))}
        </div>

        <select
          className="flow-model"
          value={settings.model}
          onChange={(e) => setSettings({ model: e.target.value })}
          title="Image model"
        >
          {FLOW_MODELS.map((m) => (
            <option key={m.id} value={m.id}>
              {m.label}
            </option>
          ))}
        </select>
      </div>

      {kind === "video" && (
        <div className="flow-promptbar__note">
          This build focuses on image generation — video (Veo) is coming later.
        </div>
      )}

      <div className="flow-promptbar__row">
        <textarea
          className="flow-promptbar__input"
          placeholder="What do you want to create?  (type @CharacterName to keep characters consistent)"
          value={prompt}
          onChange={(e) => setPrompt(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) {
              e.preventDefault();
              submit();
            }
          }}
          rows={1}
        />
        <button
          type="button"
          className="flow-promptbar__send"
          onClick={submit}
          disabled={generating || kind === "video" || !prompt.trim()}
          title={generating ? "Generating…" : "Generate (⌘/Ctrl + Enter)"}
        >
          {generating ? "…" : "→"}
        </button>
      </div>
    </div>
  );
}
