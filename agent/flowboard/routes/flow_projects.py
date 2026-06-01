"""One-way sync: ensure each Flowboard board has a live Flow project.

This is a LOCAL → FLOW direction sync. We do NOT import Flow's project
list into Flowboard's UI. The flow:

  GET  /api/flow/projects  → per-board sync status (does this board's
                              flow_project_id still exist on Flow?)
  POST /api/flow/projects/sync-up → for every board whose flow_project_id
                                     is missing on Flow (or no binding
                                     at all), CREATE a new Flow project
                                     and update BoardFlowProject so the
                                     dashboard's projects exist on Flow's
                                     side too. Idempotent: boards that
                                     already exist on Flow are left
                                     untouched.

The Flow project list is fetched internally (via the existing TRPC
search endpoint) just to diff against local binds; the list itself is
not exposed in the response.
"""
from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, HTTPException

from flowboard.db import get_session
from flowboard.db.models import Board, BoardFlowProject
from flowboard.services.flow_sdk import get_flow_sdk, is_valid_project_id

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/flow/projects", tags=["flow-projects"])


async def _resolve_remote_ids(tool: str) -> set[str]:
    """Pull the user's Flow project list and return just the id set.
    Raises HTTPException(502) on extension/TRPC failure."""
    result = await get_flow_sdk().list_user_projects_all(tool=tool)
    if result.get("error"):
        raise HTTPException(
            status_code=502,
            detail={"message": result["error"]},
        )
    projects = result.get("projects") or []
    return {p["project_id"] for p in projects if isinstance(p, dict) and p.get("project_id")}


@router.get("")
async def get_sync_status(tool: str = "PINHOLE"):
    """Per-board sync status. Does NOT expose the Flow project list —
    this is a one-way sync (local → Flow), so the frontend only needs
    to know which boards are still synced.

    Response:
        {
          "board_status": [
            {board_id, board_name, flow_project_id, exists_on_flow},
            ...
          ]
        }
    """
    remote_ids = await _resolve_remote_ids(tool)
    with get_session() as s:
        boards = s.query(Board).order_by(Board.created_at.desc()).all()
        binds = {
            b.board_id: b.flow_project_id
            for b in s.query(BoardFlowProject).all()
        }
        board_status = []
        for b in boards:
            pid: Optional[str] = binds.get(b.id)
            board_status.append({
                "board_id": b.id,
                "board_name": b.name,
                "flow_project_id": pid,
                "exists_on_flow": (pid in remote_ids) if pid else False,
            })
    return {"board_status": board_status}


@router.post("/sync-up")
async def sync_up(tool: str = "PINHOLE"):
    """Push every orphan board up to Flow. For each board where the
    bound flow_project_id is missing from Flow's remote list (or no
    binding exists at all), create a new Flow project and replace the
    BoardFlowProject row.

    Idempotent: boards already in sync are skipped. Returns a per-board
    action log so the UI can summarise ("synced N boards").
    """
    remote_ids = await _resolve_remote_ids(tool)

    # Snapshot the boards that need work. Read-only pass to avoid
    # holding the session during the TRPC round-trips below.
    with get_session() as s:
        boards = s.query(Board).all()
        binds = {
            b.board_id: b.flow_project_id
            for b in s.query(BoardFlowProject).all()
        }
        to_sync = [
            (b.id, b.name, binds.get(b.id))
            for b in boards
            if binds.get(b.id) is None or binds.get(b.id) not in remote_ids
        ]

    actions: list[dict] = []
    sdk = get_flow_sdk()

    for board_id, board_name, old_pid in to_sync:
        resp = await sdk.create_project(title=board_name or "Untitled")
        if resp.get("error"):
            actions.append({
                "board_id": board_id,
                "board_name": board_name,
                "old_flow_project_id": old_pid,
                "new_flow_project_id": None,
                "status": "failed",
                "error": str(resp["error"])[:200],
            })
            continue
        new_pid = resp.get("project_id")
        if not isinstance(new_pid, str) or not is_valid_project_id(new_pid):
            actions.append({
                "board_id": board_id,
                "board_name": board_name,
                "old_flow_project_id": old_pid,
                "new_flow_project_id": None,
                "status": "failed",
                "error": "invalid project_id from Flow",
            })
            continue

        with get_session() as s:
            row = s.get(BoardFlowProject, board_id)
            if row is None:
                row = BoardFlowProject(
                    board_id=board_id, flow_project_id=new_pid
                )
            else:
                row.flow_project_id = new_pid
            s.add(row)
            s.commit()
        logger.info(
            "sync-up: board %s %s → new flow_project %s",
            board_id,
            f"(was {old_pid})" if old_pid else "(no prior bind)",
            new_pid,
        )
        actions.append({
            "board_id": board_id,
            "board_name": board_name,
            "old_flow_project_id": old_pid,
            "new_flow_project_id": new_pid,
            "status": "rebound" if old_pid else "created",
            "error": None,
        })

    return {
        "synced": [a for a in actions if a["status"] in ("created", "rebound")],
        "failed": [a for a in actions if a["status"] == "failed"],
        "total_boards": len(to_sync) + (
            # boards that were already synced
            len([1 for b in (binds.values()) if isinstance(b, str) and b in remote_ids])
        ),
    }
