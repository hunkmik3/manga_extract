import { useEffect, useMemo, useRef, useState } from "react";
import { createPortal } from "react-dom";
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
  modelProvider,
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
  const settings = useFlowStudioStore((s) => s.settings);
  const setSettings = useFlowStudioStore((s) => s.setSettings);
  const generate = useFlowStudioStore((s) => s.generate);
  const prompt = useFlowStudioStore((s) => s.composerPrompt);
  const setPrompt = useFlowStudioStore((s) => s.setComposerPrompt);
  const setComposerCaret = useFlowStudioStore((s) => s.setComposerCaret);
  const pendingCaretApply = useFlowStudioStore((s) => s.pendingCaretApply);
  const clearPendingCaretApply = useFlowStudioStore((s) => s.clearPendingCaretApply);
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
  // Expand the prompt box while focused (typing) or manually toggled via the
  // grow/collapse button; collapse to one line when neither holds.
  const [focused, setFocused] = useState(false);
  const [manualExpand, setManualExpand] = useState(false);
  const expanded = focused || manualExpand;
  // Manual full-screen editor for the prompt (big distraction-free textarea).
  const [fullscreen, setFullscreen] = useState(false);
  const fsRef = useRef<HTMLTextAreaElement>(null);
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

  // Grow the prompt box while focused/manually-expanded, collapse to one line
  // when idle. (mirror follows via inset:0; CSS transition animates the height change.)
  useEffect(() => {
    const ta = taRef.current;
    if (!ta) return;
    if (!expanded) {
      // Idle (blurred + pointer outside) → collapse to a single line, showing
      // the START of the prompt (not a scrolled-to-caret mid-position).
      ta.style.height = "38px";
      ta.scrollTop = 0;
      if (mirrorRef.current) mirrorRef.current.scrollTop = 0;
      return;
    }
    // Focused/manually-expanded → grow to fit the prompt (incl. long pastes); cap at ~40%
    // of the viewport, then scroll. Keep in sync with .fc__input max-height.
    ta.style.height = "auto";
    const cap = Math.max(140, Math.round(window.innerHeight * 0.4));
    ta.style.height = `${Math.min(ta.scrollHeight, cap)}px`;
  }, [prompt, expanded]);

  // Apply an externally-triggered insert (a grid card's "@" button, which runs
  // in a different component and can't touch this textarea's DOM directly).
  // Changing the React `value` alone doesn't move the browser's native caret,
  // so once the new text lands, explicitly relocate it — then consume the
  // one-shot signal so it doesn't reapply on the next unrelated render.
  useEffect(() => {
    if (pendingCaretApply === null) return;
    const ta = taRef.current;
    if (ta) {
      // preventScroll: focusing an element the browser deems "off-screen"
      // (e.g. below a long, scrolled grid) otherwise auto-scrolls the whole
      // page to reveal it — jarring when the composer bar is already visible.
      // setSelectionRange still scrolls the TEXTAREA'S OWN content to show
      // the caret, which is the only scroll we actually want here.
      ta.focus({ preventScroll: true });
      ta.setSelectionRange(pendingCaretApply, pendingCaretApply);
    }
    clearPendingCaretApply();
  }, [pendingCaretApply, clearPendingCaretApply]);

  // Full-screen editor: focus it on open, close on Escape.
  useEffect(() => {
    if (!fullscreen) return;
    fsRef.current?.focus();
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setFullscreen(false);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [fullscreen]);

  // Paste an image anywhere → upload it and attach it as a reference. (When the
  // detail viewer is open it handles its own paste, so bail here.)
  useEffect(() => {
    const onPaste = async (e: ClipboardEvent) => {
      if (useFlowStudioStore.getState().selectedMediaId) return; // viewer handles it
      const files = Array.from(e.clipboardData?.items ?? [])
        .filter((it) => it.kind === "file" && it.type.startsWith("image/"))
        .map((it) => it.getAsFile())
        .filter((f): f is File => !!f);
      if (!files.length) return; // let normal text paste through
      e.preventDefault();
      for (const f of files) {
        const id = await uploadAsset(f);
        if (id) addRef(id);
      }
    };
    window.addEventListener("paste", onPaste);
    return () => window.removeEventListener("paste", onPaste);
  }, [uploadAsset, addRef]);

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
      name: a.label || "Image",
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
    // Registers the mention; if the display name collides with a DIFFERENT
    // image/group already tagged, this comes back disambiguated ("@name 2") —
    // use THAT text, not the raw requested name.
    const { token } = addComposerMention({ token: `@${e.name}`, cover: e.cover, mediaIds: e.mediaIds });
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
    if (!prompt.trim()) return; // no wait-for-finish — fire as many as you like
    generate(prompt); // clears composerPrompt + refs + mentions in the store
  };

  return (
    <div className="fc">
      {composerRefs.length > 0 && (
        <div className="fc__refs">
          {composerRefs.map((m, i) => (
            // Index in the key: the same image can now be attached more than
            // once (duplicate chips), and mediaId alone would collide as a key.
            <span key={`${m}-${i}`} className="fc__refchip">
              <img src={thumbUrl(m, 96)} alt="" loading="lazy" decoding="async" onClick={() => select(m)} />
              <button type="button" onClick={() => removeRef(m)} aria-label="Remove reference">
                ✕
              </button>
            </span>
          ))}
        </div>
      )}

      {mention && filtered.length > 0 && (
        <div className="fc__mention" role="listbox">
          <div className="fc__mention-head">Insert reference — @{mention.query || "…"}</div>
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
                  ? `👤 Character · ${e.mediaIds.length}`
                  : e.kind === "scene"
                    ? `🎬 Scene · ${e.mediaIds.length}`
                    : "Image"}
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
          title="Upload image / add reference"
        >
          ＋
        </button>
        <input
          ref={fileInput}
          type="file"
          accept="image/*"
          multiple
          hidden
          onChange={async (e) => {
            const files = Array.from(e.target.files ?? []);
            e.target.value = "";
            for (const f of files) {
              const id = await uploadAsset(f);
              if (id) addRef(id); // attach as a reference, ready to use
            }
          }}
        />

        <div className="fc__field">
          <textarea
            ref={taRef}
            className="fc__input"
            placeholder="What do you want to create?  (type @ to insert a character / scene / image)"
            value={prompt}
            onFocus={() => setFocused(true)}
            onBlur={() => setFocused(false)}
            onChange={onChange}
            onSelect={(e) => setComposerCaret(e.currentTarget.selectionStart ?? 0)}
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
                    const nextPrompt = prompt.slice(0, start) + prompt.slice(caret);
                    setPrompt(nextPrompt);
                    // One mention entry can represent an image tagged SEVERAL
                    // times (each tap inserts its own text occurrence but
                    // shares one registry entry, since it's the same image).
                    // Detach only the one ref this backspace removed; only
                    // forget the mention once NO occurrence of its token is
                    // left in the text — otherwise the remaining occurrences
                    // would lose their pill highlighting even though their
                    // images are still attached.
                    m.mediaIds.forEach(removeRef);
                    if (!nextPrompt.includes(m.token)) {
                      removeComposerMention(m.token);
                    }
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
          <button type="button" className="fc__model" onClick={() => setOpenSet((v) => !v)} title="Image generation settings">
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
                <label className="fc__pop-label">Aspect ratio</label>
                <div className="fc__chips">
                  {FLOW_ASPECTS.map((a) => (
                    <button key={a} type="button" className={`fc__chip${settings.aspect === a ? " is-on" : ""}`} onClick={() => setSettings({ aspect: a })}>
                      {a}
                    </button>
                  ))}
                </div>
              </div>
              <div className="fc__pop-section">
                <label className="fc__pop-label">Count</label>
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
                  {/* "Pro only" only makes sense for the Nano Banana family (switch
                      model to unlock 4K); Seedream can't do 4K on ANY tier, so the
                      hint would be misleading there — just omit it. */}
                  Resolution {cap !== "4K" && modelProvider(settings.model) !== "avis" && (
                    <span className="fc__muted">(4K: Pro only)</span>
                  )}
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
              <div className="fc__pop-section">
                <label className="fc__pop-label">
                  Colors <span className="fc__muted">(with references)</span>
                </label>
                <div className="fc__chips">
                  <button
                    type="button"
                    className={`fc__chip${settings.preserveColors ? " is-on" : ""}`}
                    onClick={() => setSettings({ preserveColors: !settings.preserveColors })}
                    title="Tell the model to match the reference's palette — no colour grading, no pink/warm tint. Only applied when the generation has reference/material images."
                  >
                    Keep original colors
                  </button>
                </div>
              </div>
            </div>
          )}
        </div>

        <button
          type="button"
          className="fc__icon fc__expand"
          onClick={() => setManualExpand((v) => !v)}
          title={manualExpand ? "Collapse prompt box" : "Expand prompt box"}
        >
          {manualExpand ? "⤡" : "⤢"}
        </button>

        <button
          type="button"
          className="fc__icon fc__expand"
          onClick={() => setFullscreen(true)}
          title="Expand prompt to full screen"
        >
          ⛶
        </button>

        <button type="button" className="fc__send" onClick={submit} disabled={!prompt.trim()} title="Generate (Enter)">
          →
        </button>
      </div>

      {fullscreen &&
        createPortal(
          <div className="fc-fs" role="dialog" aria-modal="true">
          <div className="fc-fs__panel">
            <div className="fc-fs__head">
              <span className="fc-fs__title">Edit prompt</span>
              <button
                type="button"
                className="fc__icon"
                onClick={() => setFullscreen(false)}
                title="Collapse (Esc)"
              >
                ⤡
              </button>
            </div>
            <textarea
              ref={fsRef}
              className="fc-fs__ta"
              placeholder="What do you want to create?"
              value={prompt}
              onChange={(e) => setPrompt(e.target.value)}
              onKeyDown={(e) => {
                if ((e.metaKey || e.ctrlKey) && e.key === "Enter") {
                  e.preventDefault();
                  submit();
                  setFullscreen(false);
                }
              }}
            />
            <div className="fc-fs__foot">
              <span className="fc-fs__hint">⌘/Ctrl + Enter to generate · Esc to close</span>
              <button
                type="button"
                className="fc-fs__gen"
                onClick={() => {
                  submit();
                  setFullscreen(false);
                }}
                disabled={!prompt.trim()}
              >
                Generate →
              </button>
            </div>
          </div>
        </div>,
          document.body,
        )}
    </div>
  );
}
