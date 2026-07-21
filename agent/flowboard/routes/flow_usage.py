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
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter
from sqlmodel import select

from flowboard.db import get_session
from flowboard.db.models import Request

router = APIRouter(prefix="/api/flow", tags=["flow-usage"])

# Gemini/Atrium has a daily quota (≈ Atrium's 1000/day). Seedream (Avis +
# BytePlus Ark) is pay-per-use — no quota, so we show money SPENT instead.
# BytePlus Dola-Seedream-5.0-pro bills per IMAGE (per piece): output ≈
# $0.045/image, input/reference ≈ $0.003/image with the FIRST input free.
# Override the rates via env if your tier/price differs.
DAILY_QUOTA = int(os.getenv("FLOWBOARD_DAILY_QUOTA", "1000"))
SEEDREAM_USD_PER_IMAGE = float(os.getenv("FLOWBOARD_SEEDREAM_USD_PER_IMAGE", "0.045"))
SEEDREAM_USD_PER_INPUT = float(os.getenv("FLOWBOARD_SEEDREAM_USD_PER_INPUT", "0.003"))


def _images_in(result: object) -> int:
    if not isinstance(result, dict):
        return 0
    mids = result.get("media_ids")
    if not isinstance(mids, list):
        return 0
    return sum(1 for m in mids if isinstance(m, str) and m)


def _input_count(params: object) -> int:
    """Number of input/reference images sent with a gen (source + refs). Used to
    bill Seedream input images (first one is free)."""
    if not isinstance(params, dict):
        return 0
    n = 1 if params.get("source_media_id") else 0
    refs = params.get("ref_media_ids")
    if isinstance(refs, list):
        n += sum(1 for r in refs if isinstance(r, str) and r)
    return n


def _engine_of(params: object) -> str:
    """Bucket a request into a user-facing engine group by its provider.
    avis/ark → "seedream"; atrium/gemini/anything-else → "gemini"."""
    p = ""
    if isinstance(params, dict):
        p = str(params.get("provider") or "").lower()
    return "seedream" if p in ("avis", "ark") else "gemini"


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


@router.get("/usage")
def flow_usage() -> dict:
    # Local-midnight boundary, as an aware datetime so it compares with the
    # UTC-stored created_at.
    now = datetime.now().astimezone()
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    # The count rolls over at the server's next local midnight (that's how
    # `today` is bucketed), so report that as the reset instant + seconds left.
    resets_at = start + timedelta(days=1)
    today = {"gemini": 0, "seedream": 0}
    total = {"gemini": 0, "seedream": 0}
    sd_cost = {"today": 0.0, "total": 0.0}  # Seedream USD spent
    with get_session() as s:
        rows = s.exec(
            select(Request).where(Request.type == "flow_gen_image", Request.status == "done")
        ).all()
    for r in rows:
        imgs = _images_in(r.result)
        eng = _engine_of(r.params)
        is_today = bool(r.created_at and _aware(r.created_at) >= start)
        total[eng] += imgs
        if is_today:
            today[eng] += imgs
        if eng == "seedream" and imgs:
            # output images billed in full; input/reference images billed after
            # the first (which is free).
            billable_inputs = max(0, _input_count(r.params) - 1)
            cost = imgs * SEEDREAM_USD_PER_IMAGE + billable_inputs * SEEDREAM_USD_PER_INPUT
            sd_cost["total"] += cost
            if is_today:
                sd_cost["today"] += cost

    engines = {
        # Gemini/Atrium: quota-based.
        "gemini": {
            "today": today["gemini"],
            "total": total["gemini"],
            "daily_quota": DAILY_QUOTA,
            "remaining_est": max(0, DAILY_QUOTA - today["gemini"]),
        },
        # Seedream: pay-per-use → report money spent (BytePlus per-image pricing).
        "seedream": {
            "today": today["seedream"],
            "total": total["seedream"],
            "usd_per_image": round(SEEDREAM_USD_PER_IMAGE, 6),
            "cost_today": round(sd_cost["today"], 4),
            "cost_total": round(sd_cost["total"], 4),
        },
    }
    return {
        # Top-level fields kept for backward compatibility (Gemini/Atrium view).
        "today": today["gemini"] + today["seedream"],
        "total": total["gemini"] + total["seedream"],
        "daily_quota": DAILY_QUOTA,
        "remaining_est": max(0, DAILY_QUOTA - today["gemini"]),
        "resets_at": resets_at.isoformat(),
        "seconds_until_reset": max(0, int((resets_at - now).total_seconds())),
        "engines": engines,
    }
