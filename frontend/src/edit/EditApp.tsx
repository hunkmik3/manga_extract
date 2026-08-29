import { useRef, useState } from "react";
import { useEditStore } from "../store/edit";
import { useAppModeStore } from "../store/appMode";
import { mediaUrl, thumbUrl } from "../api/client";
import "./edit.css";

export function EditApp() {
  const setMode = useAppModeStore((s) => s.setMode);
  const {
    mediaId, history, segments, selected, cutouts, busy, error,
    loadFile, detect, select, edit, undo, reset, clearError,
  } = useEditStore();
  const fileInput = useRef<HTMLInputElement>(null);
  const [prompt, setPrompt] = useState("");

  const sel = segments.find((s) => s.id === selected) || null;

  return (
    <div className="ed">
      <header className="ed__top">
        <div className="ed__brand">🪄 Grok Editor</div>
        <div className="ed__nav">
          {history.length > 0 && <button className="ed__btn" onClick={undo}>↶ Undo</button>}
          {mediaId && <button className="ed__btn ed__btn--ghost" onClick={reset}>New image</button>}
          <button className="ed__btn ed__btn--ghost" onClick={() => setMode("flow")}>GiantFlow</button>
        </div>
      </header>

      {error && <div className="ed__err" onClick={clearError}>⚠ {error}</div>}
      {busy && <div className="ed__busy">⏳ {busy}</div>}

      <div className="ed__body">
        {/* main canvas */}
        <div className="ed__canvas">
          {mediaId ? (
            <div className="ed__imgwrap">
              <img src={thumbUrl(mediaId, 1400)} alt="edit" className="ed__img" />
              {sel && (
                <div
                  className="ed__box"
                  style={{
                    left: `${sel.box[1] / 10}%`,
                    top: `${sel.box[0] / 10}%`,
                    width: `${(sel.box[3] - sel.box[1]) / 10}%`,
                    height: `${(sel.box[2] - sel.box[0]) / 10}%`,
                  }}
                />
              )}
            </div>
          ) : (
            <div className="ed__drop" onClick={() => fileInput.current?.click()}>
              <div className="ed__drop-i">＋</div>
              <div>Upload an image to edit</div>
            </div>
          )}
          <input
            ref={fileInput} type="file" accept="image/*" hidden
            onChange={(e) => { if (e.target.files?.[0]) loadFile(e.target.files[0]); e.target.value = ""; }}
          />
          {mediaId && (
            <div className="ed__composer">
              <input
                className="ed__prompt"
                placeholder={sel ? `Edit the ${sel.label}… (e.g. make it red)` : "Describe your edit… (@ a part or just type)"}
                value={prompt}
                onChange={(e) => setPrompt(e.target.value)}
                onKeyDown={(e) => { if (e.key === "Enter" && prompt.trim()) { edit(sel ? `${prompt} (${sel.label})` : prompt); setPrompt(""); } }}
              />
              <button
                className="ed__send" disabled={!prompt.trim() || !!busy}
                onClick={() => { edit(sel ? `${prompt} (${sel.label})` : prompt); setPrompt(""); }}
              >→</button>
            </div>
          )}
        </div>

        {/* segment panel */}
        {mediaId && (
          <aside className="ed__panel">
            <div className="ed__panel-head">
              <span>Các phân đoạn</span>
              <button className="ed__btn" disabled={!!busy} onClick={detect}>
                {segments.length ? "Re-detect" : "Detect parts"}
              </button>
            </div>
            {segments.length === 0 ? (
              <div className="ed__hint">Bấm "Detect parts" — AI tự nhận diện từng bộ phận để bạn chọn & tách nền.</div>
            ) : (
              <div className="ed__segs">
                {segments.map((s) => (
                  <div
                    key={s.id}
                    className={`ed__seg${selected === s.id ? " is-on" : ""}`}
                    onClick={() => select(selected === s.id ? null : s.id)}
                  >
                    <div className="ed__seg-thumb">
                      {cutouts[s.id]
                        ? <img src={mediaUrl(cutouts[s.id])} alt={s.label} />
                        : <span className="ed__seg-ph">◫</span>}
                    </div>
                    <span className="ed__seg-label">{s.label}</span>
                  </div>
                ))}
              </div>
            )}
          </aside>
        )}
      </div>
    </div>
  );
}
