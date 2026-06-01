# Plan — Storyboard Node Type

> Status: **decisions locked, ready for executor**
> Last updated: 2026-05-09 (v2)
> Author/owner: anh Tuan
> Grounding: 5 parallel `Explore` passes over `frontend/src/`, `agent/flowboard/`, `extension/`, `docs/`, `.omc/`. Every claim cites `file:line`. No speculation — gaps are flagged.

---

## 0. TL;DR

Add a new top-level node type `Storyboard` that produces **N=1–8 ordered narrative shots**. Generation is **continuity-tree-aware**: each shot is either a **root** (planner-decided new scene → `gen_image`) or a **continuation** (`parentShotIdx=j` → `edit_image(base=shots[j].mediaId)`). The planner LLM emits BOTH `prompts[]` and `parents[]` in one JSON object.

Dispatch:
1. **Phase A** — all roots in parallel `gen_image` chunks of ≤4 (Flow's hard cap).
2. **Phase B** — BFS through tree; siblings whose parent just finished dispatch in parallel as `edit_image`.

Refs are **global only** (every upstream edge applies to every shot). Failures keep `partial` status; user retries individual shots; descendants of a failed shot are `blocked` until parent is retried.

All 7 OPEN-* decisions resolved on 2026-05-09 — see §3.

---

## 1. Current state — grounded facts

### 1.1 Frontend

- `frontend/src/canvas/Board.tsx:23-30` — react-flow node-type registry. ALL types render through one component:
  ```tsx
  const nodeTypes = { character: NodeCard, image: NodeCard, video: NodeCard,
                      prompt: NodeCard, note: NodeCard, visual_asset: NodeCard };
  ```
- `frontend/src/canvas/NodeCard.tsx` — single component, discriminator = `data.type`.
  - Image variant grid: `tileCountFor(data) = max(1, min(variantCount || mediaIds.length, 4))` (lines 648–672). **Hard 4-tile clamp today.**
  - `data` shape: `mediaId, mediaIds[], variantCount, slotErrors[], aiBrief, aspectRatio, status` (`store/board.ts:22-81`, `FlowboardNodeData`).
  - Refs UI = upstream edges; per-edge variant pin via `edge.data.sourceVariantIdx` (NodeCard.tsx:401, `patchEdge`).
  - Edit UI exists **only on `visual_asset`** as "Refine" panel (lines 1046–1209): single-output, single-ref, text-only. No mask/inpaint.
- `frontend/src/components/GenerationDialog.tsx` — modal. Fields: prompt, aspect, variants (1–4), camera (video). Fires `dispatchGeneration(rfId, opts)` (`store/generation.ts:147–156`).
- `store/generation.ts:336-398` `refineImage()` — dispatches `type:"edit_image"` from visual_asset.

### 1.2 Backend

- `agent/flowboard/routes/requests.py:19-38` — `POST /api/requests` creates a `Request` row, enqueues by id.
- `agent/flowboard/db/models.py` — `Request {id, node_id, type, params(JSON), status, result(JSON), error}`. **No `subtype` / `parent_request_id` / `kind`** — `type` is the only discriminator.
- Worker: `agent/flowboard/worker/processor.py`
  - `_handle_gen_image` (71–142) → `flow_sdk.gen_image(...)`.
  - `_handle_edit_image` (331–380) registered as `"edit_image"` at line 388.
- Flow SDK: `agent/flowboard/services/flow_sdk.py`
  - `IMAGE_MODELS = {"NANO_BANANA_PRO":"GEM_PIX_2", "NANO_BANANA_2":"NARWHAL"}` (line 37). Both support gen AND edit.
  - **Hard cap `MAX_VARIANT_COUNT = 4`** (line 222) — server-side.
  - One HTTP call: `POST /v1/projects/{project_id}/flowMedia:batchGenerateImages` with `requests[]` array of N items, each with own `seed`, `structuredPrompt`, optional `imageInputs` (lines 263, 446–476, 482).
  - Edit uses **same endpoint**, distinguished by `imageInputs.IMAGE_INPUT_TYPE_BASE_IMAGE` + optional `IMAGE_INPUT_TYPE_REFERENCE` items.
  - Refs payload: `{"name": media_id, "imageInputType": "IMAGE_INPUT_TYPE_REFERENCE"}` (line 447).
- DB gap: **`Asset` has no `parent_asset_id`** — edit lineage lives implicit in `Request.params` + node graph.

### 1.3 LLM planner

- `agent/flowboard/services/prompt_synth.py`
  - `auto_prompt_batch(node_id, count)` (489) — single Claude/Gemini call, JSON array of `count` prompts.
  - `_BATCH_SUFFIX` (line 475) — currently optimised for **pose-distinct variants** ("DIFFERENT stance from pool"). **Wrong objective for storyboard.**
  - `_format_user_message` (362) labels upstream refs `ref_image_1..N`.
  - System prompt is gaze-locked, neutral-mouth editorial fashion (line 29). Storyboard adds an addendum, not replacement.
- Vision: `services/vision.py:51` `describe_media()` produces `aiBrief` ~200-char from any image — used to enrich refs in planner context.

### 1.4 Hard limits (verified)

| Limit | Value | Source |
|---|---|---|
| Max variants per Flow API call | **4** | `flow_sdk.py:218,222` |
| Image models | `GEM_PIX_2`, `NARWHAL` | `flow_sdk.py:37` |
| Aspect ratios (image) | LANDSCAPE / PORTRAIT / SQUARE | `flow_sdk.py`, `routes/upload.py:70-85` |
| Edit endpoint | Same as gen, via `imageInputs.IMAGE_INPUT_TYPE_BASE_IMAGE` | `flow_sdk.py:503-565` |
| Existing storyboard logic | **None** | green-field |

### 1.5 Prior art

- `docs/PLAN.md` Phase 9 mentions "batch" generically — **no storyboard spec**.
- `README.md:619` references sister project `flowkit` for "YouTube story videos" — explicitly out of scope here.
- `docs/design/stitch-phase1/*.html` uses "Scene 01" but means **board session**, not multi-shot node.

> Conclusion: green-field. Forward-compat constraint only — keep current variant-image flow working unchanged.

---

## 2. Goal

Add node type `Storyboard` (locked, OPEN-1) that:

1. Holds **N=1..8** ordered shots, each with a media_id + per-shot prompt + parent pointer.
2. Generates the N shots via a **continuity tree**:
   - Roots (`parentShotIdx=null`) → `gen_image` (parallel batches of ≤4).
   - Children (`parentShotIdx=j`) → `edit_image(base=shots[j].mediaId, refs=globalRefs)`. Children dispatch as soon as their parent is `done`. Sibling children run in parallel.
3. Planner LLM synthesizes `{prompts[N], parents[N]}` from a `narrativeSeed` + global refs' aiBriefs.
4. **Global refs only**: upstream edges apply to every shot. No per-shot overrides (locked, OPEN-4).
5. **Per-shot retry**: any failed shot retried independently. Children of a still-failed parent are `blocked`.
6. Persist progress per-shot — partial failure (batch 2 fails, batch 1 done) preserved as `partial` node status.

### Non-goals (this phase)

- Multi-board / cross-board storyboards.
- Video stitching / export — `docs/PLAN.md` Phase 11 territory.
- Audio / narration.
- Storyboard nesting.
- Auto-character-locking via Imagen Subject (Flow doesn't expose for `GEM_PIX_2`).
- Cascading auto-retry of blocked descendants when parent succeeds — user retries each level. Future work.

---

## 3. Resolved decisions (2026-05-09)

All 7 OPEN-* questions answered. Locked path:

| # | Question | Locked answer | Rationale |
|---|----------|---------------|-----------|
| 1 | Node type discriminator | **`Storyboard`** (top-level, registered next to `image`/`video`/`character`) | Owner pick; bare name. Backend `Node.type` is just a string — no schema change. |
| 2 | Default generation strategy | **Continuity tree**: roots → `gen_image` (parallel ≤4); children → `edit_image(base=parent.mediaId)` (sibling parallel). Planner emits `parents[]` per shot. | Owner: "shot 2 continuation của shot 1 → edit shot 1 → ra shot 2; không continuation → gen batch; cứ gen root trước, edit children sau." |
| 3 | Shot storage | **`Node.data.shots[]`** JSON, no `Shot` table. | Mirrors existing `mediaIds[]`. Zero migration. |
| 4 | Refs strategy | **Global only**. Upstream edges = pool used by every shot. | Owner: "ref là global để reuse." |
| 5 | Planner pass | **1-pass** — emits `{prompts[], parents[]}` in one JSON object. | Owner: "1 pass." Lower latency + cost. |
| 6 | Failure handling | **Keep partial**, retry per failed shot standalone. | Owner: "Keep partial + có thể retry failed shot." |
| 7 | Mixed-status node | New `NodeStatus = "partial"` value (≥1 done AND ≥1 error). | Owner: "partial có retry tại standalone shot fail." |

---

## 4. Architecture

### 4.1 Data model — `Node.data.shots`

```ts
// frontend type — extends frontend/src/store/board.ts FlowboardNodeData
type ShotStatus = "idle" | "queued" | "running" | "done" | "error" | "blocked";

interface StoryboardShot {
  idx: number;                       // 0..7
  prompt: string;                    // per-beat prompt; planner-output, user-editable
  parentShotIdx: number | null;      // null = root → gen_image
                                     // j (0 ≤ j < idx) = continuation → edit_image
  mediaId?: string;                  // populated after dispatch
  status: ShotStatus;
  error?: string;                    // e.g. "missing_media" | "parent_failed" | "<exception text>"
}

interface FlowboardNodeData {
  type: "Storyboard" | /* existing */ NodeType;
  shots?: StoryboardShot[];
  shotCount?: number;                // 1..8 (mirrors shots.length)
  narrativeSeed?: string;            // user free-text; feeds planner
  // existing/reused fields:
  mediaIds?: (string | null)[];      // mirror of shots[*].mediaId — keeps thumbnail code happy
  aspectRatio?: string;
  status?: NodeStatus;               // adds "partial"
  ...
}

type NodeStatus = "idle" | "queued" | "running" | "done" | "error" | "partial";
```

`status="blocked"` (shot-level) means: ready to dispatch, waiting for parent. Distinct from `"queued"` (= queued at request level) and `"error"` (= dispatch failed).

`mediaIds[]` stays in lock-step with `shots[*].mediaId` so the existing `ImageTile`/`tileCountFor` rendering keeps working with one cap-lift for storyboard nodes.

### 4.2 Backend — request types

| `type` | Purpose | Handler |
|---|---|---|
| `gen_storyboard` | Plan + dispatch all N shots: planner returns `{prompts[], parents[]}`; worker walks the continuity tree (Phase A roots → Phase B BFS children). | new `_handle_gen_storyboard` |
| `retry_storyboard_shot` | Re-run a single shot k. Worker reads `shots[k].parentShotIdx`: null → `gen_image`, else → `edit_image(base=shots[parent].mediaId)`. Rejects if parent not `done`. | new `_handle_retry_storyboard_shot` |
| `gen_image`, `edit_image`, `gen_video`, ... | Untouched. |

### 4.3 Continuity-tree dispatch — full sequence

```
plan_beats(narrative_seed, refs, count=N) via auto_prompt_storyboard()
   → returns {"prompts": [s_0..s_{N-1}], "parents": [p_0..p_{N-1}]}
       # validation:
       #   parents[0] must be null
       #   parents[k] is null OR an int in [0, k)
       #   no cycles (parents[k] < k enforces this)

build dispatch graph:
   roots         = [k for k in range(N) if parents[k] is None]
   children_of   = {parent: [k for k where parents[k] == parent]}

PHASE A — dispatch roots (parallel, batched ≤4):
   chunks = [roots[i:i+4] for i in range(0, len(roots), 4)]
   asyncio.gather(
     *[ flow_sdk.gen_image(
            prompts=[prompts[k] for k in chunk],
            variant_count=len(chunk),
            ref_media_ids=global_refs,
            ...)
        for chunk in chunks ],
     return_exceptions=True)
   → write back shots[*].mediaId / status / error
   → propagate "blocked" to descendants of any failed root

PHASE B — BFS children, level by level:
   while True:
     eligible = [k for k where shots[k].status=="queued"
                              and parents[k] is not None
                              and shots[parents[k]].status=="done"]
     if not eligible: break
     await asyncio.gather(
       *[ flow_sdk.edit_image(
              base_media_id=shots[parents[k]].mediaId,
              prompt=prompts[k],
              ref_media_ids=global_refs,
              ...)
          for k in eligible ],
       return_exceptions=True)
     → write back; propagate blocked
     # next iteration picks up shots whose parent just turned "done"

aggregate node.status:
   all done → "done"
   any done AND any error/blocked → "partial"
   all error/blocked → "error"
```

Both phases write back to `Node.data.shots[]` and broadcast a WS update after each level so the frontend reveals tiles progressively.

### 4.4 Retry semantics

User clicks **Retry** on a failed tile k → `POST /api/requests {type:"retry_storyboard_shot", node_id, params:{shot_idx:k}}`.

Worker:
- Reads `shots[k]` from `Node.data` (prompt, parentShotIdx).
- If `parentShotIdx == null` → dispatch `gen_image` (variant_count=1, prompts=[shots[k].prompt], refs=globalRefs).
- Else if `shots[parent].status == "done"` → dispatch `edit_image(base=shots[parent].mediaId, prompt=shots[k].prompt, refs=globalRefs)`.
- Else → return 422 `{"error":"parent_not_ready"}`. UI must surface that the parent shot needs to be retried first.

On retry success, the shot transitions `error → done` (or `blocked → done` if it had been propagated). **Descendants stay `blocked`** — no auto-cascade for MVP. User retries each level themselves. (This is the dumbest-thing-that-works; cascading auto-retry is in §12 future.)

### 4.5 Refs (global only — locked)

- `globalRefMediaIds` = upstream edges' source mediaIds, collected exactly like the current image flow does (`prompt_synth._collect_upstream` → `_collect_ref_media_ids`).
- Same array passed to every dispatch (root `gen_image` and every child `edit_image`).
- No per-shot ref override. If the user wants different refs for a sub-section, they make a separate Storyboard node and route different upstream edges into it.
- Variant pinning per upstream edge (existing `edge.data.sourceVariantIdx`) still works — selects which variant of an upstream image/character feeds in as a ref.

---

## 5. Phased implementation

5 phases. Each is a self-contained merge unit; tests + lint must pass at each end. File:line refs are the existing landmarks where the change attaches.

### Phase 1 — Schema + types (½ day)

**Backend**
- `agent/flowboard/services/pipeline_executor.py:136` — add `"Storyboard"` to `_VALID_NODE_TYPES`.
- `agent/flowboard/services/prompt_synth.py:232` — add `"Storyboard"` to `_REF_SOURCE_TYPES` so a storyboard shot can be selected as a ref for downstream nodes.
- No DB migration (locked OPEN-3). Add type-hint comment on `Node.type` listing valid values.

**Frontend**
- `frontend/src/store/board.ts:22-81` — extend `FlowboardNodeData` with `shots?: StoryboardShot[]`, `shotCount?`, `narrativeSeed?`. Add `"Storyboard"` literal to `NodeType` union. Add `"partial"` to `NodeStatus` union. Add `"blocked"` to `ShotStatus` (new).
- `frontend/src/canvas/Board.tsx:23-30` — register `Storyboard: NodeCard`.

**Acceptance**
- A storyboard node round-trips through `GET /api/nodes/{id}` with `data.shots[]`, `data.shotCount`, `data.narrativeSeed` preserved.
- Existing variant `image` nodes render and dispatch unchanged.
- `npx tsc --noEmit` clean. `pytest` unchanged count + green.

### Phase 2 — Planner addendum (½ day)

**Backend**
- `agent/flowboard/services/prompt_synth.py` — add `auto_prompt_storyboard(node_id, count, *, narrative_seed)` (~60 LOC): calls the LLM with the existing system prompt + new `_STORYBOARD_SUFFIX`. Parses output as JSON OBJECT (not array — different from `auto_prompt_batch`). Validates `parents[]` constraints. Returns `{"prompts": [...], "parents": [...]}`.
- Existing `auto_prompt_batch` untouched (still emits pose-distinct array for the variant Image node).
- Unit tests: stub LLM CLI, capture system_prompt; assert `_STORYBOARD_SUFFIX` substring present and `_BATCH_SUFFIX` absent. Stub LLM stdout to return `{"prompts":[...8...],"parents":[null,0,1,2,3,null,5,6]}`; assert parsed correctly. Stub invalid `parents=[0,...]` (parents[0] non-null) → assert raises.

**Acceptance**
- `auto_prompt_storyboard(..., count=8)` returns a dict with `prompts: list[str]` length 8 and `parents: list[int|None]` length 8 in <120 s; pytest fixture passes.
- Validation rejects bad `parents[]` (cycle, OOB, parents[0] != null).

### Phase 3 — Worker handlers + retry (1 day)

**Backend**
- `agent/flowboard/worker/processor.py` — add `_handle_gen_storyboard` (~120 LOC). Implements §4.3 dispatch.
- Same file — add `_handle_retry_storyboard_shot` (~50 LOC). Implements §4.4 retry semantics.
- Register both in `_DEFAULT_HANDLERS` (line ~386).
- Add helpers `_propagate_blocked` and `_aggregate_node_status` as module-private functions.
- Result shape pushes `{"shots": [...], "node_status": "..."}` so the WS update tells the frontend which tile changed.

**Acceptance** (tests below in §9)
- Happy path (mixed tree): all 8 shots populated, dispatch order respects tree.
- Failure (root fails): descendants marked `blocked` with `error="parent_failed"`; siblings of failed root unaffected.
- Retry root: shot transitions `error → done` on retry; descendants stay `blocked`.
- Retry child with parent still failed: 422 `parent_not_ready`.

### Phase 4 — Frontend renderer + create flow (1 day)

**Frontend**
- `frontend/src/canvas/NodeCard.tsx` — add `StoryboardBody` next to `ImageBody`. Lift the 4-tile cap inside this body. Reuse `ImageTile` for thumbnails.
  - Header: title + shot counter (e.g. "5 / 8 done").
  - Body: horizontal strip up to 8 tiles. Each tile shows thumbnail/skeleton + status pill + a small badge:
    - root tiles: blank (no badge)
    - continuation tiles: `↩j` showing parent index (e.g. `↩2` means child of shot 2)
    - blocked tiles: `🔒` icon + "waiting on shot j"
  - Per-tile menu (3-dot): **Retry** (visible when `error`/`blocked`+parent done), **Edit prompt only** (replans this shot's prompt without dispatching), **View full**.
  - Footer: `narrativeSeed` textarea + "Replan beats" button (re-runs planner without dispatching) + "Generate all" / "Generate missing".
- `frontend/src/components/GenerationDialog.tsx` — when target type is `Storyboard`:
  - Replace "Variants" stepper with **"Shots" stepper (1..8)**.
  - Show `narrativeSeed` textarea.
  - Hide camera (video-only).
  - Submit posts `type:"gen_storyboard"`.
- `frontend/src/store/generation.ts` — add `dispatchStoryboard(rfId, opts)` mirroring `dispatchGeneration` but with `type:"gen_storyboard"`. Add `retryStoryboardShot(rfId, shotIdx)` posting `type:"retry_storyboard_shot"`.
- Add a "+ Storyboard" entry to whatever palette/menu adds image/video/character nodes today. Search `addNode` / palette JSX.

**Acceptance**
- User adds a Storyboard node via palette, sets `shotCount=8` + a seed, clicks Generate; tiles populate progressively (roots first, children after).
- A shot with `parentShotIdx=2` shows `↩2` badge; a blocked shot shows lock icon + parent index.
- Outgoing edge from a storyboard tile to a downstream image/video pins by `sourceShotIdx` (added to edge data — see §10 ADR consequences).

### Phase 5 — Per-shot retry + replan + polish (½ day)

**Frontend**
- Wire the per-tile **Retry** action → `retryStoryboardShot`.
- "Edit prompt only" — opens an inline textarea; on save, mutates `shots[k].prompt` without dispatching. (Useful for users who want to tweak before retrying.)
- "Replan beats" button — re-runs planner with current `narrativeSeed` + global refs; replaces `shots[k].prompt` and `shots[k].parentShotIdx` for shots whose `mediaId` is null. Shots that already have media keep their prompt unless user opts into a full replan via a confirm dialog.
- Telemetry to existing activity log (`routes/activity.py`): `gen_storyboard` count + per-shot success rate.

**Polish**
- README — one-paragraph "Storyboard nodes" section in the Architecture block.
- `docs/PLAN.md` — mark storyboard as Phase 7.5 done.
- Type-check + pytest gate.

---

## 6. Concrete artefacts

### 6.1 Request schemas (Pydantic models added to `agent/flowboard/routes/requests.py`)

```python
# type=gen_storyboard
{
  "shot_count": 1..8,                       # required
  "narrative_seed": str,                    # required (can be "")
  "aspect_ratio": "IMAGE_ASPECT_RATIO_*",   # required
  "image_model": "NANO_BANANA_PRO",         # optional, default
  "global_ref_media_ids": [str, ...],       # from upstream edges; can be []
  "paygate_tier": "PAYGATE_TIER_TWO",       # required
  "project_id": str,                        # required (Flow project)
  # optional escape hatch — skip planner:
  "shot_prompts":  [str, ...] | None,       # iff present, len must == shot_count
  "shot_parents":  [int|None, ...] | None,  # required iff shot_prompts present
}

# type=retry_storyboard_shot
{
  "shot_idx": 0..7,                         # required
  # All other params inferred from Node.data.shots[shot_idx] +
  # node-level config persisted by the original gen_storyboard request.
}
```

### 6.2 `_STORYBOARD_SUFFIX` (planner template — locked output schema)

```python
# agent/flowboard/services/prompt_synth.py
_STORYBOARD_SUFFIX = (
    "\n\nSTORYBOARD MODE: Output ONE JSON OBJECT with exactly these keys:\n"
    "  \"prompts\": array of EXACTLY {count} strings (≤280 chars each),\n"
    "                each describing one beat of a continuous narrative —\n"
    "                index 0 is the first beat, index {count}-1 the last.\n"
    "  \"parents\": array of EXACTLY {count} entries, each null OR an integer.\n"
    "                parents[k] = null  → beat k is a NEW SCENE/ROOT (will be\n"
    "                  generated fresh — use ONLY when location/subject/visual\n"
    "                  context legitimately changes from the prior beat).\n"
    "                parents[k] = j (0 ≤ j < k) → beat k VISUALLY CONTINUES\n"
    "                  from beat j — same room, same wardrobe, same framing\n"
    "                  carry-over. The image will be EDITED from beat j's\n"
    "                  output, so beat k's prompt MUST describe ONLY THE DELTA\n"
    "                  (e.g. \"now opens the package\", \"now wearing the shirt\")\n"
    "                  — DO NOT re-describe identity, room, lighting.\n"
    "                Constraints: parents[0] MUST be null; parents[k] < k.\n"
    "Coherence rules (every beat):\n"
    "  • SAME subject identity across the whole sequence — anchor on\n"
    "    `ref_image_1` if a person reference exists.\n"
    "  • SAME products/wardrobe wherever the narrative places them.\n"
    "  • Consistent lighting + colour palette within a continuity chain.\n"
    "Per-beat:\n"
    "  • photoreal editorial shot, GAZE engages camera, neutral closed-mouth.\n"
    "  • each beat advances the story; no two beats interchangeable.\n"
    "{narrative_seed_block}"
    "Output ONLY the JSON object — no preamble, no markdown fences. Example:\n"
    "{\n"
    "  \"prompts\": [\n"
    "    \"Editorial photo, woman in living room, hands empty, neutral pose, …\",\n"
    "    \"Same scene, woman now holds sealed brown package on lap…\",\n"
    "    \"Same scene, woman opens package, blue jacket emerging from tissue…\",\n"
    "    \"Same scene, woman tries on the blue jacket…\",\n"
    "    \"Editorial photo, same woman, mirror selfie wearing blue jacket…\",\n"
    "    \"Editorial photo, exterior city street, woman walking, blue jacket on, …\",\n"
    "    \"Same exterior, walking past café window, jacket reflected…\",\n"
    "    \"Same exterior, wide shot, woman crossing crosswalk, jacket on…\"\n"
    "  ],\n"
    "  \"parents\": [null, 0, 1, 2, 3, null, 5, 6]\n"
    "}"
)
```

The `{narrative_seed_block}` substitution:
```python
narrative_seed_block = (
    f"\nNarrative seed (user intent — beats MUST follow this arc):\n"
    f"  {narrative_seed.strip()}\n\n"
) if narrative_seed.strip() else ""
```

### 6.3 Worker — `_handle_gen_storyboard` (full pseudocode)

```python
async def _handle_gen_storyboard(req: Request) -> dict:
    p = req.params
    n = int(p["shot_count"])
    if not 1 <= n <= 8:
        raise ValueError(f"shot_count {n} out of range [1,8]")

    # ── 1. Plan beats (or use caller-supplied) ─────────────
    if p.get("shot_prompts") is not None:
        prompts = list(p["shot_prompts"])
        parents = list(p.get("shot_parents") or [])
        if len(prompts) != n or len(parents) != n:
            raise ValueError("shot_prompts/shot_parents length mismatch")
    else:
        plan = await prompt_synth.auto_prompt_storyboard(
            req.node_id, count=n,
            narrative_seed=p.get("narrative_seed", ""),
        )
        prompts, parents = plan["prompts"], plan["parents"]

    # ── 2. Validate parents ────────────────────────────────
    if parents[0] is not None:
        raise ValueError("parents[0] must be null")
    for k in range(1, n):
        v = parents[k]
        if v is not None and not (0 <= v < k):
            raise ValueError(f"parents[{k}]={v} out of range [0, {k})")

    refs = list(p.get("global_ref_media_ids") or [])
    aspect = p["aspect_ratio"]
    tier = p["paygate_tier"]
    model = p.get("image_model")
    project_id = p["project_id"]

    # ── 3. Initialise shots state ──────────────────────────
    shots = [{
        "idx": k,
        "prompt": prompts[k],
        "parentShotIdx": parents[k],
        "mediaId": None,
        "status": "queued",
        "error": None,
    } for k in range(n)]
    _persist_shots(req.node_id, shots, narrative_seed=p.get("narrative_seed",""),
                   node_status="running")

    # ── 4. Phase A — dispatch roots in chunks of ≤4 ────────
    roots = [k for k in range(n) if parents[k] is None]
    for chunk in [roots[i:i+4] for i in range(0, len(roots), 4)]:
        try:
            res = await flow_sdk.gen_image(
                prompt=prompts[chunk[0]],            # API legacy required field
                project_id=project_id,
                aspect_ratio=aspect,
                paygate_tier=tier,
                ref_media_ids=refs,
                variant_count=len(chunk),
                prompts=[prompts[k] for k in chunk],
                image_model=model,
            )
            ids = (res or {}).get("media_ids") or []
            for i, k in enumerate(chunk):
                mid = ids[i] if i < len(ids) else None
                shots[k]["mediaId"] = mid
                shots[k]["status"]  = "done" if mid else "error"
                shots[k]["error"]   = None if mid else "missing_media"
        except Exception as e:
            logger.exception("gen_storyboard root chunk failed")
            for k in chunk:
                shots[k]["status"] = "error"
                shots[k]["error"]  = str(e)
    _propagate_blocked(shots)
    _persist_shots(req.node_id, shots, node_status=_aggregate(shots))

    # ── 5. Phase B — BFS children level by level ───────────
    while True:
        eligible = [
            k for k in range(n)
            if shots[k]["status"] == "queued"
            and parents[k] is not None
            and shots[parents[k]]["status"] == "done"
        ]
        if not eligible:
            break

        async def edit_one(k):
            try:
                res = await flow_sdk.edit_image(
                    project_id=project_id,
                    base_media_id=shots[parents[k]]["mediaId"],
                    prompt=prompts[k],
                    ref_media_ids=refs,
                    aspect_ratio=aspect,
                    paygate_tier=tier,
                    image_model=model,
                )
                mid = (res or {}).get("media_ids", [None])[0]
                shots[k]["mediaId"] = mid
                shots[k]["status"]  = "done" if mid else "error"
                shots[k]["error"]   = None if mid else "missing_media"
            except Exception as e:
                logger.exception(f"gen_storyboard child {k} failed")
                shots[k]["status"] = "error"
                shots[k]["error"]  = str(e)

        await asyncio.gather(*[edit_one(k) for k in eligible])
        _propagate_blocked(shots)
        _persist_shots(req.node_id, shots, node_status=_aggregate(shots))

    return {
        "media_ids": [s["mediaId"] for s in shots],
        "shots": shots,
        "node_status": _aggregate(shots),
    }


def _propagate_blocked(shots):
    """Any shot whose parent is error/blocked → mark blocked + parent_failed."""
    n = len(shots)
    changed = True
    while changed:
        changed = False
        for k in range(n):
            if shots[k]["status"] not in ("queued",): continue
            p = shots[k]["parentShotIdx"]
            if p is None: continue
            if shots[p]["status"] in ("error", "blocked"):
                shots[k]["status"] = "blocked"
                shots[k]["error"]  = "parent_failed"
                changed = True


def _aggregate(shots):
    statuses = {s["status"] for s in shots}
    if statuses == {"done"}:                        return "done"
    if statuses <= {"error", "blocked"}:            return "error"
    if "done" in statuses:                          return "partial"
    return "running"
```

### 6.4 Worker — `_handle_retry_storyboard_shot`

```python
async def _handle_retry_storyboard_shot(req: Request) -> dict:
    p = req.params
    k = int(p["shot_idx"])

    # Read shot + node-level config from Node.data
    with get_session() as s:
        node = s.get(Node, req.node_id)
        data = dict(node.data or {})
        shots = list(data.get("shots") or [])
        if not (0 <= k < len(shots)):
            raise ValueError("shot_idx out of range")
        shot = dict(shots[k])
        prompt = shot["prompt"]
        parent = shot["parentShotIdx"]
        aspect = data.get("aspectRatio")
        refs   = list(data.get("globalRefMediaIds") or [])
        model  = data.get("imageModel")
        tier   = data.get("paygateTier")
        project_id = data.get("projectId")

    if parent is None:
        # root retry → gen_image
        res = await flow_sdk.gen_image(
            prompt=prompt, project_id=project_id, aspect_ratio=aspect,
            paygate_tier=tier, ref_media_ids=refs, variant_count=1,
            prompts=[prompt], image_model=model,
        )
        new_mid = (res or {}).get("media_ids", [None])[0]
    else:
        if shots[parent].get("status") != "done":
            raise ValueError("parent_not_ready")
        res = await flow_sdk.edit_image(
            project_id=project_id, base_media_id=shots[parent]["mediaId"],
            prompt=prompt, ref_media_ids=refs,
            aspect_ratio=aspect, paygate_tier=tier, image_model=model,
        )
        new_mid = (res or {}).get("media_ids", [None])[0]

    shots[k]["mediaId"] = new_mid
    shots[k]["status"]  = "done" if new_mid else "error"
    shots[k]["error"]   = None if new_mid else "missing_media"
    # NB: descendants stay blocked — user retries the next level next.
    _persist_shots(req.node_id, shots, node_status=_aggregate(shots))
    return {"shot_idx": k, "media_id": new_mid, "node_status": _aggregate(shots)}
```

### 6.5 `_persist_shots` (helper)

```python
def _persist_shots(node_id: int, shots: list[dict], *,
                   narrative_seed: str | None = None,
                   node_status: str | None = None) -> None:
    with get_session() as s:
        node = s.get(Node, node_id)
        if not node: return
        new_data = dict(node.data or {})
        new_data["shots"] = shots
        new_data["shotCount"] = len(shots)
        new_data["mediaIds"] = [sh.get("mediaId") for sh in shots]
        if narrative_seed is not None:
            new_data["narrativeSeed"] = narrative_seed
        node.data = new_data
        if node_status is not None:
            node.status = node_status
        s.add(node); s.commit()
```

---

## 7. Acceptance criteria (testable)

1. **Schema round-trip** — a `Node` with `type="Storyboard"` round-trips through `GET /api/nodes/{id}` with `data.shots[]`, `data.shotCount`, `data.narrativeSeed` preserved.
2. **Planner output shape** — `auto_prompt_storyboard(node_id, count=8)` returns `{prompts:[8 strings ≤280 chars each], parents:[8 entries each null or int<k]}` in <120 s. Validation rejects `parents[0] != null`, OOB indices, len mismatch.
3. **Happy path — all-continuation chain** (`parents=[null,0,1,2,3,4,5,6]`): all 8 shots populated. Phase A dispatches 1× `gen_image` (variant_count=1). Phase B dispatches 7 `edit_image` calls in 7 sequential level expansions (each level has 1 shot — serial because chain).
4. **Happy path — mixed tree** (`parents=[null,0,1,2,3,null,5,6]`): Phase A dispatches 1× `gen_image` (variant_count=2 for shots 0 + 5). Phase B levels: L1={1,6}, L2={2,7}, L3={3}, L4={4}. Sibling parallelism kicks in at L1.
5. **Happy path — all roots** (`parents=[null]*8`): single Phase A call → 2× chunks of 4 in parallel; no Phase B.
6. **Failure with blocked descendants** — stub root shot 0 to fail; assert `shots[1..4].status="blocked"`, `error="parent_failed"`. If shots 5+ have an independent root that succeeded, they're unaffected.
7. **Retry root** — with shot 0 in `error`, POST `retry_storyboard_shot {shot_idx:0}`; on success `shots[0].status="done"`. Descendants stay `blocked` (no auto-cascade).
8. **Retry child rejects when parent unhealthy** — shot 0 still error, POST `retry_storyboard_shot {shot_idx:1}`; assert worker returns `parent_not_ready`.
9. **Frontend** — Storyboard node renders horizontal strip up to 8 tiles; continuation tiles show `↩j`; blocked tiles show lock + parent idx; failed tiles expose **Retry** menu item.
10. **Refs propagation** — attaching upstream `visual_asset` ref via edge passes its mediaId in `global_ref_media_ids` for every dispatched call (root + every child). Verified via dispatched payload assertion.
11. **Backwards compat** — existing variant `image` node, edit on `visual_asset`, character/video flows unchanged. Full pytest pass.
12. **Type-check + tests gate** — `uv run pytest` (target: existing 339 + new tests ≥ ~360 pass), `npx tsc --noEmit` clean.

---

## 8. Risks + mitigations

| # | Risk | Severity | Mitigation |
|---|------|----------|------------|
| R1 | LLM produces inconsistent characters across beats (drift) — same risk as variants today | High | Anchor planner addendum on `ref_image_1` for person identity. Recommend user attach a `character` node upstream — its portrait drives identity. Continuation tree's `edit_image` chain is itself a strong identity-lock for child shots. |
| R2 | `GEM_PIX_2` edit drifts after 4–5 hops (shadow/identity slip) | Medium | Tree mostly-flat by design (planner can split into 2+ roots when scene transitions). Surface a soft warning in UI when continuation chain depth ≥4. |
| R3 | `MAX_VARIANT_COUNT=4` could change server-side without notice | Low | Read `MAX_VARIANT_COUNT` at chunk time so a future bump-down doesn't crash; chunker fans out into more chunks if needed. |
| R4 | Credit cost surprise — N=8 = up to 8 dispatches | Medium | Show predicted credits in `GenerationDialog` before submit; reuse `services/flow_client.py:fetch_paygate_tier`. |
| R5 | `Node.data` JSON growing — 8 shots × prompts + global refs | Low | <10 KB even for fully populated. SQLite limit not in play. |
| R6 | `asyncio.gather(..., return_exceptions=True)` swallows tracebacks | Low | Always `logger.exception()` before swallowing (pattern matches existing handlers, e.g. `worker/processor.py:140`). |
| R7 | User clicks Retry on shot 5 while gen_storyboard is still running | Medium | Reject `retry_storyboard_shot` if `shots[idx].status in {"queued","running"}` AND there's an active `gen_storyboard` request for the node. |
| R8 | Replan after partial generation overwrites done-shot prompts | Medium | Replan default touches only shots with `mediaId==null`. Full replan requires explicit confirm in UI. |
| R9 | UI overcrowded with 8 tiles + badges + menus | Medium | Horizontal scroll w/ snap; tile size = `ImageTile` 1:1; `↩j` badge minimal. Layout audit at Phase 4 with real shoot. |
| R10 | Edge data needs `sourceShotIdx` in addition to existing `sourceVariantIdx` | Medium | Add `sourceShotIdx` field; worker reads it first when source node `type==="Storyboard"`, falls back to `sourceVariantIdx` otherwise. Pure additive — old edges keep working (default 0). |
| R11 | Continuation chain dispatched serially → 8-deep chain = ~80 s | Medium | This is the worst case (user-chosen). Default planner emits a mostly-flat tree (multiple roots, shallow children). Document expected latency in UI ("Estimated 12–60 s depending on continuity choices"). |
| R12 | Auto-cascade absence — when user retries parent, blocked children don't auto-run | Low | Acceptable for MVP. Doc the limitation. Future work: optional auto-cascade toggle. |

---

## 9. Test plan

### 9.1 Unit (pytest)

- `test_storyboard_request_schema_valid` — `RequestCreate` accepts `gen_storyboard` with full param block.
- `test_storyboard_request_schema_rejects_oob_shot_count` — 0 / 9 → 422.
- `test_planner_storyboard_addendum_present` — stub LLM, capture system_prompt; assert `_STORYBOARD_SUFFIX` in; `_BATCH_SUFFIX` out.
- `test_planner_storyboard_returns_object` — stub stdout to `{"prompts":[...],"parents":[...]}`; assert parsed.
- `test_planner_storyboard_validates_parents` — bad inputs (parents[0]!=null, parents[k]>=k, OOB, len mismatch) → ValueError.
- `test_propagate_blocked` — small fixture; assert blocked propagation is correct including through chains (`error → blocked → blocked`).
- `test_aggregate_status` — combinations producing done / error / partial / running.
- `test_handle_gen_storyboard_happy_mixed_tree` — stub flow_sdk; assert dispatch count + per-shot mediaId + node_status.
- `test_handle_gen_storyboard_root_failure` — stub batch raise; assert descendants blocked.
- `test_handle_retry_storyboard_shot_root` — happy.
- `test_handle_retry_storyboard_shot_child_done_parent` — happy.
- `test_handle_retry_storyboard_shot_child_unready_parent` — raises `parent_not_ready`.
- `test_persist_shots_keeps_other_data` — node has unrelated keys in `data`; assert preserved.

### 9.2 Integration (FastAPI TestClient + stubbed flow_sdk)

- POST `/api/requests {type:"gen_storyboard",...}` → poll `/api/requests/{id}` until done → assert node has 8 shots + node_status correct.
- Inject failure at chunk 2 → poll → assert node_status="partial" + shots reflect.
- POST `retry_storyboard_shot` for the failed shot → assert `partial → done` (or → error if retry also fails).

### 9.3 Frontend

- `npx tsc --noEmit` gate.
- Manual smoke flow documented in PR description (with screenshots): create Storyboard, generate, retry one root, retry one child, edit prompt only — all using a real labs.google session.

### 9.4 Gate

`uv run pytest` + `npx tsc --noEmit` + at least one real-labs smoke run before tagging the release.

---

## 10. ADR — Architectural Decision Record

**Decision**: introduce a top-level node type `Storyboard`. Persist N=1..8 shots as `Node.data.shots[]`. Each shot declares `parentShotIdx: number|null` — `null = root → gen_image`; `j → edit_image(base=shots[j].mediaId)`. Two new request types `gen_storyboard` (plan+dispatch) and `retry_storyboard_shot` (single-shot retry). Refs are global only (per-shot extras explicitly out of scope).

**Drivers**:
1. Owner intent: "shot 2 continuation của shot 1 → edit shot 1 → shot 2; không continuation thì gen batch; gen root trước, edit children sau." — direct mapping to a continuity tree.
2. Server cap N=4 (`flow_sdk.py:222`) is hard — chunking is mandatory.
3. Existing `_handle_edit_image` (`processor.py:331-380`) reusable as-is for child shots.
4. `Node.data` JSON already holds variant arrays — no DB migration needed.

**Alternatives considered**:
- *Mode-flag per shot* (`mode: "gen"|"edit_from_prev"|"edit_from_anchor"|"edit_from_ref"`). Rejected: overlaps with `parentShotIdx` semantics (any tree shape expressible by `parentShotIdx` covers anchor/prev/external trivially; "external base" is a separate non-storyboard ref attachment which is out of scope).
- *Per-shot extra refs*. Rejected by owner ("ref là global để reuse").
- *Plan-of-image-nodes* (Storyboard as a board-level Plan materialising 8 image nodes). Rejected: owner wants ONE node UX.
- *New `Shot` table*. Rejected: zero query benefit; migration cost.
- *Edit-chain default* (every shot edits previous). Rejected: 8× latency + drift; owner's "gen root first, edit children after" already rules out this fallback.

**Why chosen**: smallest blast radius — additive node type, additive request kinds, additive UI body, zero migration. Continuity tree expresses every shape the user described (chain, mixed roots, all-roots) without a separate mode enum.

**Consequences**:
- `"Storyboard"` discriminator becomes load-bearing — must be enumerated in `_VALID_NODE_TYPES`, frontend `NodeType` union, registry. Add a parity test.
- `Node.data.mediaIds[]` overloaded: 1..4 entries for variants, 1..8 for storyboard. Callers hard-coding 4 must read `data.shotCount` first if `type==="Storyboard"`. Identified callsites: `tileCountFor` in `NodeCard.tsx:648-672`. Lift the cap inside `StoryboardBody` only.
- Edge data gains `sourceShotIdx`. Existing `sourceVariantIdx` untouched; worker prefers `sourceShotIdx` when source is `Storyboard`.
- New `NodeStatus = "partial"` value — must be handled by status pill renderer + status filter in activity log.

**Follow-ups (post-MVP, separate plans)**:
- Cascading auto-retry of blocked descendants when parent succeeds.
- Auto-character-locking via Imagen Subject (when Flow exposes it for `GEM_PIX_2`).
- Storyboard → video: walk shots, dispatch `gen_video` with i2v anchor = each shot, ffmpeg composite (overlaps PLAN.md Phase 11).
- Versioned shot history (per-shot rollback to prior gen) — needs `data.shots[k].history[]`.
- Storyboard templates (e-commerce unboxing, travel vlog, product hero) seeding `narrativeSeed` from a prompt library.
- Per-shot extra refs (re-open OPEN-4) — if a real shoot demands it.

---

## 11. Resolved decisions

All 7 OPEN-* questions answered on 2026-05-09. See §3 for the locked table. No remaining blockers; executor can start at Phase 1.

---

## 12. Out of scope / future

- Audio narration sync per shot.
- Auto-export to MP4 timeline (use existing video node + Phase 11 ffmpeg).
- Realtime collab on a storyboard.
- Storyboard import/export as a JSON template.
- Multi-character storyboards with face-locked identity (deferred until Flow exposes a stable Subject API for `GEM_PIX_2`).
- Storyboard length > 8 shots — capped at 8 to match the brief; revisit when 16 has a concrete use case.
- Per-shot ref overrides (locked OPEN-4 = global only).
- Cascading auto-retry of blocked descendants.

---

## 13. Changelog

- **2026-05-09 v1** — initial draft after 5-agent codebase grounding pass; 7 OPEN-* decisions surfaced.
- **2026-05-09 v2** — locked all 7 OPEN-* per owner. Architecture changed from "mode flag per shot" to **continuity tree (`parentShotIdx`)**; refs simplified to global-only; planner output shape is now `{prompts[], parents[]}` (object, not array). §0, §3, §4, §6, §7, §8, §10, §11 rewritten. Phase 5 simplified (no per-shot extras UI). Total file size grew because §6 worker pseudocode is now spec-grade.
