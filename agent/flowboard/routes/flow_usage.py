"""Flow Studio usage stats.

Counts the images this tool has generated (from the `flow_gen_image` request
history) so the UI can show today's count and an *estimated* remaining daily
quota. Atrium does not expose its quota via the API, but since this deploy's
Atrium key is used only by this tool, today's count ≈ the day's Atrium usage,
so remaining ≈ daily_quota − today. The day boundary is the server's local
midnight (labelled as such in the UI); Atrium's real reset may differ slightly.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone

from fastapi import APIRouter
from sqlmodel import select

from flowboard.db import get_session
from flowboard.db.models import Request

router = APIRouter(prefix="/api/flow", tags=["flow-usage"])

DAILY_QUOTA = int(os.getenv("FLOWBOARD_DAILY_QUOTA", "1000"))


def _images_in(result: object) -> int:
    if not isinstance(result, dict):
        return 0
    mids = result.get("media_ids")
    if not isinstance(mids, list):
        return 0
    return sum(1 for m in mids if isinstance(m, str) and m)


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


@router.get("/usage")
def flow_usage() -> dict:
    # Local-midnight boundary, as an aware datetime so it compares with the
    # UTC-stored created_at.
    start = datetime.now().astimezone().replace(hour=0, minute=0, second=0, microsecond=0)
    today = total = 0
    with get_session() as s:
        rows = s.exec(
            select(Request).where(Request.type == "flow_gen_image", Request.status == "done")
        ).all()
    for r in rows:
        imgs = _images_in(r.result)
        total += imgs
        if r.created_at and _aware(r.created_at) >= start:
            today += imgs
    return {
        "today": today,
        "total": total,
        "daily_quota": DAILY_QUOTA,
        "remaining_est": max(0, DAILY_QUOTA - today),
    }
