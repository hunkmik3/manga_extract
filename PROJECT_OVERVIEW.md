# Project Overview — Manhwa → Anime Comic Pipeline (context brief for an AI assistant)

> **Purpose of this file.** This is a self-contained briefing so a fresh AI
> assistant (e.g. another Claude session) can understand what this project does,
> how it's built, what's already working, what's planned, and — most importantly
> — **the hard open problems where help is wanted**. It is deliberately blunt
> about constraints and failures so an assistant doesn't propose things that
> can't work here.
>
> Two companion docs exist: `README.md` (upstream Flowboard docs) and
> `COMIC_PIPELINE.md` (clone-and-run setup). This file is the "what & why &
> what's-hard", not the setup guide.

---

## 1. What the project is

A tool that turns a **manhwa / webtoon** (Korean-style vertical comic) into an
**anime-style adaptation**, page by page. It is a fork of *Flowboard* — an
infinite-canvas (ReactFlow) node-graph workspace that drives **Google Flow**
(image model **Nano Banana Pro** = Gemini image, internal id `GEM_PIX_2`)
through a Chrome extension riding the user's own logged-in Flow session. **No
AI API billing** — it uses the user's Flow subscription via browser automation.

The per-page flow today:

1. **Import** a folder of comic pages.
2. **Detect panels** on each page (boxes), with manual box editing.
3. **Combine**: take 4 panels (reading order) → for each, *clean* (remove
   speech bubbles / text and reconstruct art) + *extend* to a 9:16 portrait →
   **code-stitch** the 4 into one 2×2 9:16 "storyboard" image.
4. **Per-cell regen** (the "↻"/x4): re-generate one cell, pick from up to 4
   candidates.
5. **Enhance** (separate step): re-render a panel as a cinematic anime still.
6. **Upscale** (2K/4K via Flow) + **export** (download all panels as a .zip of
   individual files, in reading order).

### The actual goal (and the hard part)

> **Maintain visual consistency across many pages and many chapters** — same
> character looks the same (face, hair, body, outfit), recurring locations look
> the same, art style and lighting stay coherent — **on a pipeline that can only
> use a text prompt + reference images, with a transform-based model, and no
> ability to train anything.**

This is the central unsolved-at-scale problem and the main reason this brief
exists. See §5 (design) and §6 (constraints).

---

## 2. Architecture (3 processes + Google Flow)

```
Chrome MV3 extension  ──WS :9223──  FastAPI agent (:8101)  ──  SQLite + storage/media
 (on labs.google/fx)                 + in-process worker queue
        │                            + LLM CLI bridge (Claude/Gemini/Codex)
        │ rides user's Flow session         │
        ▼                                    ▼
   Google Flow  ◄───────────────────  React + Vite (:5173)
   (Nano Banana Pro image)            ReactFlow canvas + Zustand store
```

- **Agent** — `agent/` — FastAPI + SQLModel + SQLite. Owns board/node/edge
  state and an **in-process async worker queue**. All comic image ops are
  worker "handlers". Shells out to an LLM CLI for vision/auto-prompt (not used
  much in the comic path yet).
- **Frontend** — `frontend/` — Vite + React 18 + ReactFlow 12 + Zustand 5,
  TypeScript strict. The canvas + node bodies. No direct Flow calls.
- **Extension** — `extension/` — Chrome MV3. Intercepts Flow's API calls
  (gets the reCAPTCHA Enterprise token from the MAIN world) and proxies them
  over a localhost WebSocket. **Mandatory** — the agent has no other path to
  Flow. (Verified byte-identical to upstream; not the source of any custom
  behavior.)
- **Storage** — local SQLite + `storage/media/` byte cache (Flow's signed CDN
  URLs are short-lived, so media is fetched and re-served locally).

Ports: agent **:8101**, extension WS **:9223**, Vite dev **:5173**.
`FLOWBOARD_FLOW_API_KEY` is the public `key=AIza…` from labs.google, kept in a
gitignored `.env` (never committed; tests use a dummy value).

---

## 3. The comic pipeline internals

### Node types (frontend `comic_*`)
`comic_import` (Comic upload) · `comic_page` · `comic_panel` · `comic_panels` ·
`comic_detect` · `comic_clean` · `comic_enhance` · `comic_chars` (Character DB) ·
`comic_combine` (the 2×2). Bodies live in `frontend/src/canvas/Comic*Body.tsx`;
shared helpers in `frontend/src/canvas/comicShared.ts`.

### Worker handlers (agent `agent/flowboard/worker/processor.py`)
`import_pages`, `detect_page_panels`, `extract_panels`, `crop_panels`,
`clean_panel`, `enhance_panel`, `build_character_db`, `combine_panels`,
`regen_cell`, `restitch_cells`, `export_all_panels`, `upsample_image`
(+ the generic `gen_image`/`gen_video`/`edit_image`).

### Comic services (`agent/flowboard/services/comic/`)
- **`panels.py`** — panel detection. Heuristic XY-cut (`detect_panels`),
  webtoon band-segmentation (`detect_panels_webtoon`, with `crop="color"|"content"`),
  and a **hybrid** (`detect_panels_hybrid` = ML frame detection + heuristic
  backfill of uncovered bands). Also `stitch_2x2`, `decode_bgr`, `encode_png`,
  `pad_to_aspect`.
- **`panel_ml.py`** — YOLO `deepghs/manga109_yolo`, "frame" class → `detect_panels_ml`.
- **`characters.py`** — manga109 YOLO body detection + **CCIP** clustering /
  matching (`build_clusters`, `match_character`). **NOTE: CCIP is effectively
  abandoned for this art — see §6.**
- **`bridge.py`** — the Flow bridge. `edit_image`, `edit_image_variants`
  (the "x4"), `upsample_image`. Uploads source + refs once (de-duped cache),
  then a retry loop (`DEFAULT_MAX_ATTEMPTS=3`). **Input order to Flow:
  BASE_IMAGE (the source panel) first, then reference images.**
- **`prompts.py`** — all prompts (sent verbatim to Nano Banana Pro):
  `CLEAN_PROMPT`, `EXTEND_9_16`, `ENHANCE_PROMPT`,
  `COMBINE_CHARACTER_REFERENCE_CLAUSE`, and the axis-aware builder
  `combine_reference_clause(...)`. (`COMBINE_2X2_PROMPT` is legacy/unused — the
  combine cleans each panel individually with `CLEAN_PROMPT + EXTEND_9_16`.)

### Combine mechanics (important)
Each cell's input is the **live crop** of `page_media_id + box` from the page
image (`_source_image_bytes`). Each panel is cleaned+extended to 9:16 by a
separate `edit_image` call (the model never does the 2×2 layout — **code**
stitches it via `stitch_2x2`, so the grid is exact). Per-cell regen returns up
to 4 candidates; the user picks one; `restitch_cells` rebuilds the 2×2.

---

## 4. Consistency design ("Story Bible") — the plan

A multi-agent design pass produced this target architecture. The principle:
**frozen, human-blessed reference assets that a per-panel routing layer resolves
into the right refs at edit time, plus prompts that can let canon override an
off-model source.** Key pieces:

- **Canon library (Story Bible)** — per character (and later per location /
  style), an immutable, version-pinned set of reference media. Refs are **always
  read from canon, never from the previous output** → the "copy-of-a-copy" drift
  loop is structurally impossible (every panel is ≤1 hop from a real anchor).
- **char_id routing** — each combine cell can be assigned a `char_id`; the
  worker loads that character's frozen refs directly (`_assigned_char_refs`),
  **bypassing CCIP**.
- **Axis-aware OVERRIDE prompt** — the *load-bearing* fix. By default the source
  panel is ground truth and refs are gentle identity hints. When a cell is
  flagged "off-model" on an axis (identity / outfit / style), the prompt
  **inverts authority** for that axis: source keeps pose/layout/camera/framing,
  the reference becomes the design of record for face/hair/outfit. Plus a
  **framing-lock** so a strong ref can't drag its own composition into the panel.
- **Promote-to-canon (⭐)** — the only sanctioned way canon grows: a human
  blesses a good cell as a frozen ref (newest-first). Planned guard:
  `gen_depth ≤ 1` + a similarity check vs existing canon.
- **Outfit versioning (planned)** — keys like `char:lina:outfit_redcoat@v2`;
  same character, different clothes = same `char_id`, different `outfit_key`.
- **Sequences / scenes (planned, under discussion)** — the *environment*
  counterpart to characters: group consecutive panels into a scene that carries
  an environment **text descriptor** + lighting/time (+ optional location
  "plate" image **only on wide/establishing shots**). Injected into enhance so
  backgrounds/lighting stay coherent within a scene.
- **VLM chapter-read manifest (planned)** — a one-time pass that reads the
  chapter in reading order and emits per-panel routing keys
  (`char_ids`, `outfit_key`, `location_key`, `scene_id`, `lighting`,
  `disagrees_axes`, confidence), keyed to `(page, box)` not ordinal index. It
  disambiguates *who* by dialogue/context (the only thing that works since CCIP
  fails). It only **produces** keys; the worker **consumes** them.

### What is BUILT vs PLANNED

**Built (and test-covered):**
- Per-cell `char_id` assignment → frozen refs, **CCIP bypassed**. CCIP is now an
  opt-in fallback behind an `auto_match` flag (off unless "Auto-match
  characters" is ticked, and only for cells with no `char_id`).
- Axis-aware `combine_reference_clause()` (identity / outfit / style override) +
  a **framing-lock** clause; wired into combine + regen.
- ⭐ **promote-to-canon** (prepend to a character's `refMediaIds`, cap 8).
- **Cold-start**: an inline "＋ New character…" entry in each cell's dropdown
  creates the Character DB node (if missing) + a character seeded with that cell
  as its first frozen ref. The per-cell control strip is `[▾ character] [🔒
  force-override] [⭐ promote]`, shown under each combine cell.
- **Anti-halo** wording added to CLEAN / EXTEND / ENHANCE prompts (see §6).

**Planned / not built:** outfit_key versioning UI, location & style canon, the
sequence/scene environment layer, the VLM manifest auto-tagging, the promote
drift-guard (`gen_depth` + similarity), serialized two-hander (multi-character
cells), and anti-flag/human-pace rate limiting.

---

## 5. Where help is wanted (open problems)

1. **Cross-chapter consistency at scale.** The design above is the plan; only
   the foundation (char routing + override + promote + cold-start) is built.
   The sequence/environment layer and the VLM manifest are the next big pieces.
   How to make a creator keep ~10 characters + recurring locations consistent
   across dozens of chapters without enormous manual effort?
2. **The reference-composition-leak problem.** A strong reference (especially a
   full-body character sheet) tends to drag its OWN framing/pose/zoom into the
   target — e.g. an extreme **close-up of a face gets redrawn as the ref's full
   body**. Current mitigations are prompt-only (framing-lock; prefer TEXT
   descriptors over image plates for environment; reserve image plates for wide
   shots). Is there a more reliable approach within the platform's limits?
3. **Reducing Google Flow flags / rate limits.** The tool trips Google's
   anti-abuse (reCAPTCHA / "unusual activity") faster than manual use. Suspected
   amplifier: the bridge's retry-on-everything (×3) plus combine volume
   (4 cleans + x4 variants + upscale). Want legitimate pacing / fail-fast on
   non-retryable errors — **NOT** detection-evasion / bot-signal spoofing
   (explicitly out of scope).
4. **The "empty close-up" extend case.** A panel that is almost all white with a
   tiny detail (e.g. just a pair of glasses) has too little content to anchor an
   extend; the model leans entirely on the ref. Hard case.

---

## 6. Hard constraints & lessons (read before proposing anything)

- **Platform levers are ONLY: (1) the text prompt, (2) reference images,
  (3) the source panel.** There is **no training** — no LoRA, DreamBooth,
  textual-inversion, ControlNet, embeddings, or weight access. Generic
  image-consistency advice that assumes training is **not applicable**.
- **The model is transform-based** (clean/extend an existing panel), not
  generate-from-scratch. Prompt constraints are *soft* — the model can ignore
  them, especially on hard poses or empty sources.
- **CCIP failed to separate this comic's characters** (max inter-character
  feature distance ~0.37, overlapping same-character distances 0.03–0.13). The
  pivot is **human-approved refs assigned by `char_id`**, not appearance
  clustering. Color histograms made it worse. Don't reintroduce auto-clustering
  as the identity source.
- **Reference images leak composition** (see §5.2). Lesson baked into the
  design: identity/environment consistency should lean on TEXT + framing-locks;
  image plates only where their composition fits (wide/establishing).
- **White halo / cut-out fringe** can appear around characters after
  clean/extend (a matte/seam artifact). Mitigated by explicit anti-halo wording
  in CLEAN/EXTEND/ENHANCE; re-gen (it's partly stochastic) usually clears a
  stubborn one.
- **Google content filter (RAI) silently drops variants** — requesting x4 can
  return 3 (or fewer). This is normal; the bridge returns whatever came back
  (≥1) rather than erroring.
- **No git push / release tagging without explicit user permission.** Local
  commits are fine when asked; the user controls commit grouping and push
  timing. Commit messages co-author trailer: `Co-Authored-By: Claude …`.

---

## 7. Run & test

```bash
# Agent (FastAPI :8101, extension WS :9223). For dev: --reload.
cd agent && .venv/bin/uvicorn flowboard.main:app --reload --port 8101
# Frontend (Vite :5173)
cd frontend && npm run dev

# Tests
cd agent && FLOWBOARD_FLOW_API_KEY=AIzaDummy .venv/bin/python -m pytest -q   # ~455 passing
cd frontend && npx tsc --noEmit                                              # type-check
```

ML extras (YOLO detector + CCIP) are optional: `cd agent && uv pip install
--python .venv/bin/python -e ".[ml]"`. Without them, the heuristic detector
still works; the Character DB *build* (CCIP) needs them, but the new
**manual** character assignment + cold-start path does **not** need ML.

---

## 8. Key files (where to look)

| Area | File |
|---|---|
| Panel detection | `agent/flowboard/services/comic/panels.py`, `panel_ml.py` |
| Character CCIP (legacy) | `agent/flowboard/services/comic/characters.py` |
| Flow bridge (edit/variants/upscale) | `agent/flowboard/services/comic/bridge.py` |
| Prompts (clean/extend/enhance + override builder) | `agent/flowboard/services/comic/prompts.py` |
| Worker handlers (combine/regen/export/…) | `agent/flowboard/worker/processor.py` |
| Media cache + serving | `agent/flowboard/services/media.py` |
| Combine node UI (per-cell char/force/promote) | `frontend/src/canvas/ComicCombineBody.tsx` |
| Character DB node UI | `frontend/src/canvas/ComicCharsBody.tsx` |
| Shared frontend helpers (char DB, promote, addCharacter) | `frontend/src/canvas/comicShared.ts` |
| Upload / detect node UIs | `frontend/src/canvas/ComicImportBody.tsx`, `ComicDetectBody.tsx`, `ComicPageBody.tsx` |
| Comic tests | `agent/tests/test_comic_*.py` |

---

*Current state at time of writing: the consistency foundation (Stage 1+2+3 of
the design + cold-start character creation) is implemented and verified
(~455 backend tests pass, frontend type-checks clean). The sequence/environment
layer and the VLM manifest are designed but not yet built.*
