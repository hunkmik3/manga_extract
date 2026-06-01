"""Plan + PipelineRun routes.

The chat handler creates ``Plan`` rows in ``draft`` status. This module exposes:

- ``GET  /api/plans/{plan_id}`` — read-only fetch.
- ``POST /api/plans/{plan_id}/run`` — materialise the plan onto the canvas
  (auto-laid-out Node + Edge rows) and kick off background execution.
- ``GET  /api/pipeline-runs/{run_id}`` — status row for the frontend poll.

POST is idempotent: if a run for the plan is already ``pending`` or
``running``, we return the existing row instead of starting a second.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException

from flowboard.db import get_session
from flowboard.db.models import Plan, PipelineRun
from flowboard.services.pipeline_executor import materialize_plan, run_pipeline

logger = logging.getLogger(__name__)

router = APIRouter(tags=["plans"])

# Track in-flight executor tasks so we can avoid double-spawning if Python
# garbage-collects the future. asyncio.create_task returns Task objects that
# the event loop keeps alive while running, but holding a strong ref here is
# defence-in-depth and gives tests a hook.
_active_tasks: dict[int, asyncio.Task] = {}


@router.get("/api/plans/{plan_id}")
def get_plan(plan_id: int):
    with get_session() as s:
        plan = s.get(Plan, plan_id)
        if plan is None:
            raise HTTPException(404, "plan not found")
        return plan


@router.post("/api/plans/{plan_id}/run")
async def run_plan(plan_id: int):
    with get_session() as s:
        plan = s.get(Plan, plan_id)
        if plan is None:
            raise HTTPException(404, "plan not found")

        # Idempotency: if there's already an in-progress run for this plan,
        # return it instead of starting another.
        from sqlmodel import select

        existing = s.exec(
            select(PipelineRun)
            .where(PipelineRun.plan_id == plan_id)
            .where(PipelineRun.status.in_(("pending", "running")))  # type: ignore[attr-defined]
        ).first()
        if existing is not None:
            return existing

        # Materialise within this same transaction.
        try:
            summary = materialize_plan(s, plan_id)
        except ValueError as exc:
            raise HTTPException(404, str(exc))

        run = PipelineRun(plan_id=plan_id, status="pending")
        s.add(run)
        s.commit()
        s.refresh(run)
        rid = run.id
        assert rid is not None
        logger.info(
            "plan %s: materialised %d node(s), pipeline run %s scheduled",
            plan_id, len(summary.get("node_ids") or []), rid,
        )
        run_row = run

    # Spawn the executor in the background.
    task = asyncio.create_task(run_pipeline(rid), name=f"pipeline-run-{rid}")
    _active_tasks[rid] = task

    def _cleanup(t: asyncio.Task) -> None:
        _active_tasks.pop(rid, None)
        if t.cancelled():
            return
        exc = t.exception()
        if exc is not None:
            logger.exception("pipeline run %s crashed", rid, exc_info=exc)
            # Stamp the run as failed so the frontend doesn't hang.
            with get_session() as s2:
                row = s2.get(PipelineRun, rid)
                if row is not None and row.status not in ("done", "failed"):
                    row.status = "failed"
                    row.error = f"crash:{exc!r}"[:500]
                    row.finished_at = datetime.now(timezone.utc)
                    s2.add(row)
                    s2.commit()

    task.add_done_callback(_cleanup)
    return run_row


@router.get("/api/pipeline-runs/{run_id}")
def get_pipeline_run(run_id: int):
    with get_session() as s:
        row = s.get(PipelineRun, run_id)
        if row is None:
            raise HTTPException(404, "pipeline run not found")
        return row
