# Run 11 — Variant control + processing FX + visual_asset node

Branch: `claude/build-flowboard-canvas-gUlV5`

User-flagged pain points (from screenshot):
1. Image node placeholder always shows 4 tiles — wrong; should match the requested variant count, default 1.
2. No processing animation while gen is running — feels frozen.
3. When gen returns N variants, no way to pick one as the canonical primary for downstream nodes.
4. Need a new node type `visual_asset` that can be a) uploaded, b) generated from prompt, c) refined using an uploaded reference image.

## What ships

### Backend
- `flow_sdk.gen_image(..., variant_count: int = 1)` — replicates the request_item N times with distinct seeds. Flow returns one media entry per request_item.
- `flow_sdk.edit_image(prompt, project_id, source_media_id, ref_media_ids: list[str] = [])` — uses `imageInputs` with `IMAGE_INPUT_TYPE_BASE_IMAGE` for the source + `IMAGE_INPUT_TYPE_REFERENCE` for refs. Same captcha + endpoint as gen_image.
- `_handle_gen_image` — forwards `variant_count` (clamped 1-4).
- `_handle_edit_image` — new worker handler analogous to `_handle_gen_image`; auto-ingests fifeUrls.
- `routes/nodes.py` + frontend `NodeType` literal — extend with `"visual_asset"`.
- Tests:
  - `test_gen_image_variant_count_dispatches_n_requests`
  - `test_gen_image_variant_count_clamps_to_4`
  - `test_handle_edit_image_happy_path`
  - `test_handle_edit_image_rejects_missing_source`
  - `test_create_visual_asset_node`

### Frontend
- `GenerationDialog`: enable variants stepper (image kind only, 1-4); pipe `variantCount` into `dispatchGeneration`.
- `store/generation.ts`:
  - `dispatchGeneration` accepts `variantCount`, persists it on `node.data.variantCount` so the placeholder grid knows how many tiles to render before generation finishes.
  - New `applyVariant(rfId, idx)` — sets `data.mediaId = mediaIds[idx]`, persists via `patchNode`. Used to "apply" one of N variants as primary.
  - New `refineImage(rfId, refMediaId, prompt)` — dispatches `edit_image` request with current `mediaId` as source + `refMediaId` as REFERENCE.
- `NodeCard`:
  - `ImageBody` reworked: tile count = `variantCount || mediaIds.length || 1`; renders 1/2/3/4-tile layouts. Shimmer FX while `status` ∈ `queued|running`. Click a tile to apply it as primary.
  - New `VisualAssetBody`: empty state offers Upload / Generate; with media, shows image + Refine button. Refine flow opens a small inline picker for the reference image (file upload + prompt).
- `canvas/AddNodePalette.tsx` — add visual_asset chip.
- `canvas/Board.tsx` — register `visual_asset` in `nodeTypes`.
- `store/board.ts` — `NodeType` extended (re-exported from api/client) + TYPE_TITLE entry.
- `styles.css` — `.processing-shimmer`, `.thumbnail-grid--1/2/3/4`, visual_asset styles.

### Out of scope (Run 12+)
- WebSocket streaming for the canvas (still polling).
- Drag-drop reordering of variants.
- Batch refinement (multiple ref images at once via UI; the SDK accepts a list, the UI exposes one slot).

## Verification
- pytest target ~140 (133 baseline + ~7 new).
- `tsc --noEmit` + `vite build` clean.
- Live smoke: open dialog on image node → set variants=4 → see 4 placeholder tiles with shimmer → 4 tiles populate → click tile #2 → tile #2 highlights as primary.
- Live smoke: add visual_asset node → upload → see image → click Refine → upload ref + prompt → new image replaces previous.
