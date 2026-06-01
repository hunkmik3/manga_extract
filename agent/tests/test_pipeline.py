"""Tests for the pipeline executor + plan-run routes (Phase 5)."""
from __future__ import annotations

import asyncio

import pytest
from sqlmodel import Session, select

from flowboard.db import get_session
from flowboard.db.models import (
    Board, BoardFlowProject, Edge, Node, PipelineRun, Plan, Request,
)
from flowboard.services import pipeline_executor


# ── helpers ───────────────────────────────────────────────────────────────


def _make_board(client, name="P") -> dict:
    return client.post("/api/boards", json={"name": name}).json()


def _make_plan(board_id: int, spec: dict) -> int:
    with get_session() as s:
        plan = Plan(board_id=board_id, spec=spec, status="draft")
        s.add(plan)
        s.commit()
        s.refresh(plan)
        return plan.id  # type: ignore[return-value]


# ── auto_layout ───────────────────────────────────────────────────────────


def test_auto_layout_uses_topo_depth():
    nodes = [
        {"tmp_id": "a", "type": "character"},
        {"tmp_id": "b", "type": "image"},
        {"tmp_id": "c", "type": "video"},
    ]
    edges = [
        {"from": "a", "to": "b"},
        {"from": "b", "to": "c"},
    ]
    layout = pipeline_executor.auto_layout(nodes, edges)
    assert layout["a"][0] < layout["b"][0] < layout["c"][0]
    # Same row when stacked linearly.
    assert layout["a"][1] == layout["b"][1] == layout["c"][1]


def test_auto_layout_stacks_siblings_vertically():
    nodes = [
        {"tmp_id": "root", "type": "prompt"},
        {"tmp_id": "child1", "type": "image"},
        {"tmp_id": "child2", "type": "image"},
    ]
    edges = [
        {"from": "root", "to": "child1"},
        {"from": "root", "to": "child2"},
    ]
    layout = pipeline_executor.auto_layout(nodes, edges)
    assert layout["child1"][0] == layout["child2"][0]  # same column
    assert layout["child1"][1] != layout["child2"][1]  # different rows


# ── materialize_plan ──────────────────────────────────────────────────────


def test_materialize_plan_creates_nodes_and_edges(client):
    b = _make_board(client)
    plan_id = _make_plan(
        b["id"],
        {
            "nodes": [
                {"tmp_id": "p", "type": "prompt", "params": {"prompt": "hello"}},
                {"tmp_id": "i", "type": "image", "params": {"prompt": "a cat"}},
            ],
            "edges": [{"from": "p", "to": "i"}],
        },
    )
    with get_session() as s:
        summary = pipeline_executor.materialize_plan(s, plan_id)
        s.commit()

    assert len(summary["node_ids"]) == 2
    with get_session() as s:
        nodes = s.exec(select(Node).where(Node.id.in_(summary["node_ids"]))).all()
        edges = s.exec(select(Edge).where(Edge.board_id == b["id"])).all()
    by_type = {n.type for n in nodes}
    assert by_type == {"prompt", "image"}
    assert len(edges) == 1
    # Auto-layout column ordering: prompt left of image
    by_id = {n.id: n for n in nodes}
    src = by_id[edges[0].source_id]
    tgt = by_id[edges[0].target_id]
    assert src.x < tgt.x


def test_materialize_plan_resolves_existing_short_id(client):
    """An edge endpoint that uses #shortId should resolve to the existing node."""
    b = _make_board(client)
    # Pre-create a node we'll reference.
    n = client.post(
        "/api/nodes", json={"board_id": b["id"], "type": "character"}
    ).json()
    short_id = n["short_id"]
    plan_id = _make_plan(
        b["id"],
        {
            "nodes": [{"tmp_id": "i", "type": "image", "params": {"prompt": "x"}}],
            "edges": [{"from": f"#{short_id}", "to": "i"}],
        },
    )
    with get_session() as s:
        pipeline_executor.materialize_plan(s, plan_id)
        s.commit()
    with get_session() as s:
        edges = s.exec(select(Edge).where(Edge.board_id == b["id"])).all()
        existing = s.get(Node, n["id"])
        assert len(edges) == 1
        assert edges[0].source_id == existing.id


def test_materialize_plan_skips_unresolvable_endpoint(client):
    b = _make_board(client)
    plan_id = _make_plan(
        b["id"],
        {
            "nodes": [{"tmp_id": "i", "type": "image"}],
            "edges": [
                {"from": "ghost", "to": "i"},  # unknown
                {"from": "#zzzz", "to": "i"},  # unknown short_id
            ],
        },
    )
    with get_session() as s:
        pipeline_executor.materialize_plan(s, plan_id)
        s.commit()
    with get_session() as s:
        assert len(s.exec(select(Edge).where(Edge.board_id == b["id"])).all()) == 0
        # The image node still got created.
        assert len(s.exec(select(Node).where(Node.board_id == b["id"])).all()) == 1


def test_materialize_plan_idempotent(client):
    b = _make_board(client)
    plan_id = _make_plan(
        b["id"],
        {"nodes": [{"tmp_id": "i", "type": "image"}], "edges": []},
    )
    with get_session() as s:
        first = pipeline_executor.materialize_plan(s, plan_id)
        s.commit()
    with get_session() as s:
        second = pipeline_executor.materialize_plan(s, plan_id)
        s.commit()
    assert first["created"] is True
    assert second["created"] is False
    assert first["node_ids"] == second["node_ids"]


# ── routes ────────────────────────────────────────────────────────────────


def test_post_plan_run_returns_pipeline_run(client, monkeypatch):
    # Stub run_pipeline so we don't actually execute anything.
    async def noop(rid, **kwargs):
        return None

    monkeypatch.setattr(
        "flowboard.routes.plans.run_pipeline", noop
    )

    b = _make_board(client)
    plan_id = _make_plan(
        b["id"],
        {"nodes": [{"tmp_id": "i", "type": "image"}], "edges": []},
    )
    r = client.post(f"/api/plans/{plan_id}/run")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["plan_id"] == plan_id
    assert body["status"] == "pending"
    # And we materialised the plan.
    with get_session() as s:
        nodes = s.exec(select(Node).where(Node.board_id == b["id"])).all()
        assert len(nodes) == 1


def test_post_plan_run_idempotent(client, monkeypatch):
    async def slow(rid, **kwargs):
        # Hold the run in-flight for long enough that two posts overlap.
        await asyncio.sleep(0.5)

    monkeypatch.setattr("flowboard.routes.plans.run_pipeline", slow)

    b = _make_board(client)
    plan_id = _make_plan(
        b["id"], {"nodes": [{"tmp_id": "i", "type": "image"}], "edges": []}
    )
    r1 = client.post(f"/api/plans/{plan_id}/run").json()
    r2 = client.post(f"/api/plans/{plan_id}/run").json()
    assert r1["id"] == r2["id"]


def test_get_plan_returns_404_when_missing(client):
    assert client.get("/api/plans/9999").status_code == 404


def test_get_pipeline_run_returns_404_when_missing(client):
    assert client.get("/api/pipeline-runs/9999").status_code == 404


# ── run_pipeline ─────────────────────────────────────────────────────────


def _make_board_with_project(client, project_id="abcd1234"):
    b = _make_board(client)
    with get_session() as s:
        s.add(BoardFlowProject(board_id=b["id"], flow_project_id=project_id))
        s.commit()
    return b


@pytest.mark.asyncio
async def test_run_pipeline_dispatches_image_in_topo_order(client, monkeypatch):
    """Image node with a prompt should hit gen_image; non-gen nodes are skipped."""
    from flowboard.services import flow_sdk

    dispatch_log: list[dict] = []

    class _Stub:
        async def gen_image(self, **kwargs):
            dispatch_log.append(kwargs)
            return {"raw": {}, "media_ids": ["m-1"], "media_entries": []}

    monkeypatch.setattr(flow_sdk, "_sdk", _Stub())

    b = _make_board_with_project(client)
    plan_id = _make_plan(
        b["id"],
        {
            "nodes": [
                {"tmp_id": "p", "type": "prompt", "params": {"prompt": "hi"}},
                {"tmp_id": "i", "type": "image", "params": {"prompt": "a cat"}},
            ],
            "edges": [{"from": "p", "to": "i"}],
        },
    )
    # Materialise + create run row directly.
    with get_session() as s:
        pipeline_executor.materialize_plan(s, plan_id)
        run = PipelineRun(plan_id=plan_id, status="pending")
        s.add(run)
        s.commit()
        s.refresh(run)
        rid = run.id

    # Need the worker running for Request rows to settle.
    from flowboard.worker.processor import WorkerController, _DEFAULT_HANDLERS
    w = WorkerController(handlers=_DEFAULT_HANDLERS)
    # Inject this controller as the global so pipeline_executor's
    # `get_worker().enqueue` picks it up.
    from flowboard.worker import processor as proc
    monkeypatch.setattr(proc, "_worker", w)

    worker_task = asyncio.create_task(w.start())
    try:
        await pipeline_executor.run_pipeline(rid, request_timeout_s=5.0, poll_interval_s=0.05)
    finally:
        w.request_shutdown()
        await asyncio.wait_for(worker_task, timeout=2.0)

    assert len(dispatch_log) == 1
    assert dispatch_log[0]["prompt"] == "a cat"
    assert dispatch_log[0]["project_id"] == "abcd1234"

    # Verify pipeline + plan + image node finished cleanly.
    with get_session() as s:
        run = s.get(PipelineRun, rid)
        plan = s.get(Plan, plan_id)
        assert run is not None and run.status == "done"
        assert plan is not None and plan.status == "done"
        nodes = s.exec(select(Node).where(Node.board_id == b["id"])).all()
        by_type = {n.type: n for n in nodes}
        assert by_type["image"].status == "done"
        assert by_type["image"].data.get("mediaId") == "m-1"
        # Prompt node is left idle (no gen step).
        assert by_type["prompt"].status == "idle"


@pytest.mark.asyncio
async def test_run_pipeline_marks_downstream_failed_on_upstream_error(client, monkeypatch):
    from flowboard.services import flow_sdk

    class _Stub:
        async def gen_image(self, **kwargs):
            return {"raw": {}, "error": "captcha_failed"}

    monkeypatch.setattr(flow_sdk, "_sdk", _Stub())

    b = _make_board_with_project(client)
    plan_id = _make_plan(
        b["id"],
        {
            "nodes": [
                {"tmp_id": "i1", "type": "image", "params": {"prompt": "first"}},
                {"tmp_id": "v", "type": "video", "params": {"prompt": "next"}},
            ],
            "edges": [{"from": "i1", "to": "v"}],
        },
    )
    with get_session() as s:
        pipeline_executor.materialize_plan(s, plan_id)
        run = PipelineRun(plan_id=plan_id, status="pending")
        s.add(run)
        s.commit()
        s.refresh(run)
        rid = run.id

    from flowboard.worker.processor import WorkerController, _DEFAULT_HANDLERS
    from flowboard.worker import processor as proc
    w = WorkerController(handlers=_DEFAULT_HANDLERS)
    monkeypatch.setattr(proc, "_worker", w)

    worker_task = asyncio.create_task(w.start())
    try:
        await pipeline_executor.run_pipeline(rid, request_timeout_s=5.0, poll_interval_s=0.05)
    finally:
        w.request_shutdown()
        await asyncio.wait_for(worker_task, timeout=2.0)

    with get_session() as s:
        run = s.get(PipelineRun, rid)
        assert run is not None and run.status == "failed"
        nodes = s.exec(select(Node).where(Node.board_id == b["id"])).all()
        by_type = {n.type: n for n in nodes}
        assert by_type["image"].status == "error"
        assert by_type["video"].status == "error"
        assert by_type["video"].data.get("error") == "upstream_failed"
