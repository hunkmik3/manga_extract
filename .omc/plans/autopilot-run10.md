# Run 10 — Phase 5: Pipeline executor (polling-based)

Branch: `claude/build-flowboard-canvas-gUlV5`
Scope: Make a chat-proposed plan executable. User clicks "Run" on the
PlanPreviewCard, the executor materializes the plan's nodes/edges on the
canvas (auto-laid-out), runs generation in topological order, and the
frontend reflects progress by polling.

## What ships

### Backend
- `services/pipeline_executor.py` (NEW)
  - `auto_layout(spec) -> {tmp_id: (x, y)}`: column = topo depth × 280, row = sibling rank × 200, anchored at (200, 200).
  - `materialize_plan(session, plan_id) -> {created_nodes, created_edges}`: turns `spec.nodes`/`spec.edges` into Node/Edge DB rows. Edge endpoints accept `tmp_id` (for new plan nodes) or `#shortId` (existing nodes); unresolved endpoints are skipped (logged) rather than failing the run.
  - `run_pipeline(run_id)`: async background task. Topologically sorts the materialized DAG, walks it, and for `image`/`video` nodes with a `prompt` dispatches a Request row through the existing worker; awaits each before moving downstream. Marks `Node.status` (queued/running/done/error) and stamps `data.mediaId/mediaIds` from the request result. Pipeline status: `pending → running → done|failed`.
  - Error policy: a single node's gen failure marks that node `error` but lets independent branches keep running. Dependent (downstream) nodes are marked `error` with cause `upstream_failed`.
- `routes/plans.py` (NEW)
  - `GET  /api/plans/{plan_id}` → plan row.
  - `POST /api/plans/{plan_id}/run` → idempotent: if a `running` PipelineRun exists for the plan, returns it; otherwise creates one, schedules `run_pipeline` as `asyncio.create_task`, returns the row immediately.
  - `GET  /api/pipeline-runs/{run_id}` → status row (used by frontend polling).
- `main.py` registers the router.

### Frontend
- `api/client.ts`: `PipelineRunDTO`, `runPlan(planId)`, `getPipelineRun(runId)`, `getPlan(planId)`.
- `store/pipeline.ts` (NEW): Zustand slice tracking `activeRun: {runId, planId} | null` + a single shared poll loop. Each poll: fetch run status; if `running`, also re-fetch the board (`getBoard`) and apply nodes/edges via `setNodes`/`setEdges` so newly materialized nodes appear; on `done|failed`, stop polling and surface a toast.
- `components/ChatSidebar.tsx`: enable the previously-disabled `Review` button as **Run** (renamed). Disabled if `activeRun !== null`. Click → `runPlan` + start polling.

### Tests
- `test_pipeline.py`:
  - `test_auto_layout_uses_topo_depth`
  - `test_materialize_plan_creates_nodes_and_edges`
  - `test_materialize_plan_resolves_existing_short_id_endpoint`
  - `test_materialize_plan_skips_unresolved_endpoint`
  - `test_run_pipeline_dispatches_image_nodes_topologically` (stub flow_sdk)
  - `test_run_pipeline_marks_downstream_failed_when_upstream_errors`
  - `test_post_plan_run_idempotent`
  - `test_get_pipeline_run_returns_status`

Targets pytest **121 → ~129** green.

## Out of scope (Run 11+)
- WS streaming (`/ws/board/:id`) — frontend polls every 1.5s for now.
- Ghost-node Review Mode (Phase 8).
- Cancel-running-pipeline endpoint.
- Edge-kind-aware generation (we treat every incoming character edge as a `character_media_ids` ref regardless of `kind`).

## Files touched
- `agent/flowboard/services/pipeline_executor.py` — NEW
- `agent/flowboard/routes/plans.py` — NEW
- `agent/flowboard/main.py` — register router
- `agent/tests/test_pipeline.py` — NEW
- `frontend/src/api/client.ts` — pipeline DTOs + methods
- `frontend/src/store/pipeline.ts` — NEW
- `frontend/src/components/ChatSidebar.tsx` — Run button wired

## Verification
- `pytest agent/tests` clean
- `tsc --noEmit` + `vite build` clean
- Manual smoke: chat a plan → click Run → see nodes appear on canvas → see image gen complete on each → final layout reasonable
