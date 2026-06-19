import { useEffect, useMemo, useRef, useState } from "react";
import { thumbUrl } from "../api/client";
import {
  CHAR_PREFIX,
  FLOW_ASPECTS,
  FLOW_MODELS,
  FLOW_SIZES,
  SCENE_PREFIX,
  deriveGroups,
  groupName,
  modelMaxSize,
  useFlowStudioStore,
  type FlowSize,
  type Mention,
} from "../store/flowStudio";

interface MentionEntry {
  key: string;
  kind: "char" | "scene" | "image";
  name: string;
  cover: string;
  mediaIds: string[];
}

/**
 * Render the prompt with `@token` mentions highlighted as clickable pills. The
 * textarea above it holds the real (transparent) text + caret; this mirror sits
 * exactly on top showing the styled text, with `pointer-events: none` so typing
 * passes through — except the token pills, which capture clicks to preview the
 * referenced image.
 */
function renderMirror(
  text: string,
  mentions: Mention[],
  onToken: (mediaId: string) => void,
): React.ReactNode {
  const tokens = [...new Set(mentions.map((m) => m.token))]
    .filter(Boolean)
    .sort((a, b) => b.length - a.length); // longest-first greedy match
  if (tokens.length === 0) return text;
  const out: React.ReactNode[] = [];
  let i = 0;
  let key = 0;
  while (i < text.length) {
    const hit = tokens.find((t) => text.startsWith(t, i));
    if (hit) {
      const mediaId = mentions.find((m) => m.token === hit)?.cover;
      out.push(
        <span
          key={key++}
          className="fc__token"
          onMouseDown={(e) => {
            e.preventDefault();
            if (mediaId) onToken(mediaId);
          }}
        >
          {hit}
        </span>,
      );
      i += hit.length;
    } else {
      let j = i + 1;
      while (j < text.length && !tokens.some((t) => text.startsWith(t, j))) j++;
      out.push(<span key={key++}>{text.slice(i, j)}</span>);
      i = j;
    }
  }
  return out;
}

export function FlowComposer() {
  const generating = useFlowStudioStore((s) => s.generating);
  const settings = useFlowStudioStore((s) => s.settings);
  const setSettings = useFlowStudioStore((s) => s.setSettings);
  const generate = useFlowStudioStore((s) => s.generate);
  const prompt = useFlowStudioStore((s) => s.composerPrompt);
  const setPrompt = useFlowStudioStore((s) => s.setComposerPrompt);
  const composerRefs = useFlowStudioStore((s) => s.composerRefs);
  const removeRef = useFlowStudioStore((s) => s.removeRef);
  const addRef = useFlowStudioStore((s) => s.addRef);
  const uploadAsset = useFlowStudioStore((s) => s.uploadAsset);
  const assets = useFlowStudioStore((s) => s.assets);
  const select = useFlowStudioStore((s) => s.select);
  // Mention registry now lives in the store so grid cards can tag too.
  const mentions = useFlowStudioStore((s) => s.composerMentions);
  const addComposerMention = useFlowStudioStore((s) => s.addComposerMention);
  const removeComposerMention = useFlowStudioStore((s) => s.removeComposerMention);

  const [openSet, setOpenSet] = useState(false);
  const [mention, setMention] = useState<{ query: string; start: number } | null>(null);
  const fileInput = useRef<HTMLInputElement>(null);
  const popRef = useRef<HTMLDivElement>(null);
  const taRef = useRef<HTMLTextAreaElement>(null);
  const mirrorRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!openSet) return;
    const onDoc = (e: MouseEvent) => {
      if (popRef.current && !popRef.current.contains(e.target as Node)) setOpenSet(false);
    };
    document.addEventListener("mousedown", onDoc);
    return () => document.removeEventListener("mousedown", onDoc);
  }, [openSet]);

  // Auto-grow the textarea to fit the prompt (mirror follows via inset:0).
  useEffect(() => {
    const ta = taRef.current;
    if (!ta) return;
    ta.style.height = "auto";
    ta.style.height = `${Math.min(ta.scrollHeight, 140)}px`;
  }, [prompt]);

  const modelLabel = FLOW_MODELS.find((m) => m.id === settings.model)?.label ?? settings.model;
  const cap = modelMaxSize(settings.model);
  const sizeAllowed = (sz: FlowSize) =>
    sz === "1K" || (sz === "2K" && cap !== "1K") || (sz === "4K" && cap === "4K");

  const detectMention = (value: string, caret: number) => {
    const sub = value.slice(0, caret);
    const m = sub.match(/@([\p{L}\p{N}_]*)$/u);
    if (m) setMention({ query: m[1], start: caret - m[0].length });
    else setMention(null);
  };

  const onChange = (e: React.ChangeEvent<HTMLTextAreaElement>) => {
    const value = e.target.value;
    setPrompt(value);
    detectMention(value, e.target.selectionStart ?? value.length);
    // A @token deleted/broken in the prompt → drop its attached reference(s).
    const stale = mentions.filter((m) => !value.includes(m.token));
    if (stale.length) {
      stale.forEach((m) => {
        m.mediaIds.forEach(removeRef);
        removeComposerMention(m.token);
      });
    }
  };

  const entries = useMemo<MentionEntry[]>(() => {
    const chars = deriveGroups(assets, CHAR_PREFIX).map((g) => ({
      key: `c:${g.name}`,
      kind: "char" as const,
      name: g.name,
      cover: g.cover,
      mediaIds: assets.filter((a) => groupName(a.tags, CHAR_PREFIX) === g.name).map((a) => a.mediaId),
    }));
    const scenes = deriveGroups(assets, SCENE_PREFIX).map((g) => ({
      key: `s:${g.name}`,
      kind: "scene" as const,
      name: g.name,
      cover: g.cover,
      mediaIds: assets.filter((a) => groupName(a.tags, SCENE_PREFIX) === g.name).map((a) => a.mediaId),
    }));
    const images = assets.map((a) => ({
      key: `i:${a.refId}`,
      kind: "image" as const,
      name: a.label || "Ảnh",
      cover: a.mediaId,
      mediaIds: [a.mediaId],
    }));
    return [...chars, ...scenes, ...images];
  }, [assets]);

  const filtered = useMemo(() => {
    if (!mention) return [];
    const q = mention.query.toLowerCase();
    return (q ? entries.filter((e) => e.name.toLowerCase().includes(q)) : entries).slice(0, 24);
  }, [entries, mention]);

  const pickMention = (e: MentionEntry) => {
    e.mediaIds.forEach(addRef);
    const token = `@${e.name}`;
    addComposerMention({ token, cover: e.cover, mediaIds: e.mediaIds });
    // Read the LIVE textarea value + caret (avoids stale React state that left a
    // stray "@" → "@@token"). Re-find the "@query" ending at the caret and
    // replace it wholesale with the token.
    const ta = taRef.current;
    const value = ta ? ta.value : prompt;
    const caret = ta && ta.selectionStart !== null ? ta.selectionStart : value.length;
    const m = value.slice(0, caret).match(/@[\p{L}\p{N}_]*$/u);
    const start = m ? caret - m[0].length : caret;
    const before = value.slice(0, start);
    const after = value.slice(caret).replace(/^\s+/, "");
    const next = `${before}${token} ${after}`;
    setPrompt(next);
    const newCaret = before.length + token.length + 1;
    requestAnimationFrame(() => {
      const t = taRef.current;
      if (t) {
        t.focus();
        t.setSelectionRange(newCaret, newCaret);
      }
    });
    setMention(null);
  };

  const submit = () => {
    if (generating || !prompt.trim()) return;
    generate(prompt); // clears composerPrompt + refs + mentions in the store
  };

  return (
    <div className="fc">
      {composerRefs.length > 0 && (
        <div className="fc__refs">
          {composerRefs.map((m) => (
            <span key={m} className="fc__refchip">
              <img src={thumbUrl(m, 96)} alt="" loading="lazy" decoding="async" onClick={() => select(m)} />
              <button type="button" onClick={() => removeRef(m)} aria-label="Bỏ tham chiếu">
                ✕
              </button>
            </span>
          ))}
        </div>
      )}

      {mention && filtered.length > 0 && (
        <div className="fc__mention" role="listbox">
          <div className="fc__mention-head">Chèn tham chiếu — @{mention.query || "…"}</div>
          {filtered.map((e) => (
            <button
              key={e.key}
              type="button"
              className="fc__mention-row"
              onMouseDown={(ev) => {
                ev.preventDefault();
                pickMention(e);
              }}
            >
              <img src={thumbUrl(e.cover, 96)} alt="" loading="lazy" decoding="async" />
              <span className="fc__mention-name">{e.name}</span>
              <span className="fc__mention-kind">
                {e.kind === "char"
                  ? `👤 Nhân vật · ${e.mediaIds.length}`
                  : e.kind === "scene"
                    ? `🎬 Cảnh · ${e.mediaIds.length}`
                    : "Ảnh"}
              </span>
            </button>
          ))}
        </div>
      )}

      <div className="fc__bar">
        <button
          type="button"
          className="fc__icon"
          onClick={() => fileInput.current?.click()}
          title="Tải ảnh / thêm tham chiếu"
        >
          ＋
        </button>
        <input
          ref={fileInput}
          type="file"
          accept="image/*"
          hidden
          onChange={(e) => {
            const f = e.target.files?.[0];
            if (f) uploadAsset(f);
            e.target.value = "";
          }}
        />

        <div className="fc__field">
          <textarea
            ref={taRef}
            className="fc__input"
            placeholder="Bạn muốn tạo gì?  (gõ @ để chèn nhân vật / cảnh / ảnh)"
            value={prompt}
            onChange={onChange}
            onScroll={() => {
              if (mirrorRef.current && taRef.current) mirrorRef.current.scrollTop = taRef.current.scrollTop;
            }}
            onKeyDown={(e) => {
              if (mention && filtered.length > 0 && e.key === "Enter") {
                e.preventDefault();
                pickMention(filtered[0]);
                return;
              }
              if (e.key === "Escape" && mention) {
                setMention(null);
                return;
              }
              // Backspace right after a @token deletes the WHOLE token (and its
              // refs) atomically — the mention behaves like a chip, not text.
              if (e.key === "Backspace") {
                const ta = taRef.current;
                const caret = ta?.selectionStart ?? prompt.length;
                if (ta && caret === ta.selectionEnd) {
                  const before = prompt.slice(0, caret);
                  const m = mentions
                    .filter((x) => before.endsWith(`${x.token} `) || before.endsWith(x.token))
                    .sort((a, b) => b.token.length - a.token.length)[0];
                  if (m) {
                    e.preventDefault();
                    const removeLen = before.endsWith(`${m.token} `) ? m.token.length + 1 : m.token.length;
                    const start = caret - removeLen;
                    setPrompt(prompt.slice(0, start) + prompt.slice(caret));
                    m.mediaIds.forEach(removeRef);
                    removeComposerMention(m.token);
                    requestAnimationFrame(() => {
                      const t = taRef.current;
                      if (t) {
                        t.focus();
                        t.setSelectionRange(start, start);
                      }
                    });
                    return;
                  }
                }
              }
              if (e.key === "Enter" && !e.shiftKey) {
                e.preventDefault();
                submit();
              }
            }}
            rows={1}
          />
          <div className="fc__mirror" ref={mirrorRef} aria-hidden="true">
            {renderMirror(prompt, mentions, (mediaId) => select(mediaId))}
            {"​"}
          </div>
        </div>

        <div className="fc__settings" ref={popRef}>
          <button type="button" className="fc__model" onClick={() => setOpenSet((v) => !v)} title="Cài đặt tạo ảnh">
            <span className="fc__model-name">{modelLabel}</span>
            <span className="fc__model-meta">{settings.aspect} · {settings.count}x</span>
          </button>

          {openSet && (
            <div className="fc__pop" role="dialog">
              <div className="fc__pop-section">
                <label className="fc__pop-label">Model</label>
                <select className="fc__select" value={settings.model} onChange={(e) => setSettings({ model: e.target.value })}>
                  {FLOW_MODELS.map((m) => (
                    <option key={m.id} value={m.id}>{m.label}</option>
                  ))}
                </select>
              </div>
              <div className="fc__pop-section">
                <label className="fc__pop-label">Tỉ lệ</label>
                <div className="fc__chips">
                  {FLOW_ASPECTS.map((a) => (
                    <button key={a} type="button" className={`fc__chip${settings.aspect === a ? " is-on" : ""}`} onClick={() => setSettings({ aspect: a })}>
                      {a}
                    </button>
                  ))}
                </div>
              </div>
              <div className="fc__pop-section">
                <label className="fc__pop-label">Số lượng</label>
                <div className="fc__chips">
                  {[1, 2, 3, 4].map((n) => (
                    <button key={n} type="button" className={`fc__chip${settings.count === n ? " is-on" : ""}`} onClick={() => setSettings({ count: n })}>
                      {n}x
                    </button>
                  ))}
                </div>
              </div>
              <div className="fc__pop-section">
                <label className="fc__pop-label">
                  Độ phân giải {cap !== "4K" && <span className="fc__muted">(4K: chỉ Pro)</span>}
                </label>
                <div className="fc__chips">
                  {FLOW_SIZES.map((sz) => (
                    <button
                      key={sz}
                      type="button"
                      className={`fc__chip${settings.size === sz ? " is-on" : ""}`}
                      disabled={!sizeAllowed(sz)}
                      onClick={() => sizeAllowed(sz) && setSettings({ size: sz })}
                    >
                      {sz}
                    </button>
                  ))}
                </div>
              </div>
            </div>
          )}
        </div>

        <button type="button" className="fc__send" onClick={submit} disabled={generating || !prompt.trim()} title={generating ? "Đang tạo…" : "Tạo (Enter)"}>
          {generating ? "…" : "→"}
        </button>
      </div>
    </div>
  );
}
