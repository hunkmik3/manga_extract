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
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter
from sqlmodel import select

from flowboard.db import get_session
from flowboard.db.models import Request

router = APIRouter(prefix="/api/flow", tags=["flow-usage"])

# Gemini/Atrium has a daily quota (≈ Atrium's 1000/day). Seedream (Avis) is
# pay-per-use — no quota, so we show money SPENT instead.
#
# Rates below are measured directly against the Avis account (credit-balance
# diff around a real generation) and cross-checked against 9 real entries in
# the Avis dashboard's own per-generation VND cost history on 2026-07-30 —
# every single one landed on exactly one of two values, no exceptions:
#   1K image: $0.059125   2K image: $0.11825 (exactly 2x — including a 2K job
#   with no reference image, so reference images are free; price scales with
#   output resolution only).
# Override via env if Avis's price changes.
DAILY_QUOTA = int(os.getenv("FLOWBOARD_DAILY_QUOTA", "1000"))
SEEDREAM_USD_PER_IMAGE_1K = float(os.getenv("FLOWBOARD_SEEDREAM_USD_PER_IMAGE_1K", "0.059125"))
SEEDREAM_USD_PER_IMAGE_2K = float(os.getenv("FLOWBOARD_SEEDREAM_USD_PER_IMAGE_2K", "0.11825"))


def _images_in(result: object) -> int:
    if not isinstance(result, dict):
        return 0
    mids = result.get("media_ids")
    if not isinstance(mids, list):
        return 0
    return sum(1 for m in mids if isinstance(m, str) and m)


def _resolution_of(params: object) -> str:
    """"2K" if the request asked for 2K/4K output, else "1K" (the model's
    default — the frontend omits `image_size` from params for 1K, see
    flowStudio.ts's genParams())."""
    if not isinstance(params, dict):
        return "1K"
    size = str(params.get("image_size") or "").strip().upper()
    return "2K" if size in ("2K", "4K") else "1K"


def _engine_of(params: object) -> Optional[str]:
    """Bucket a request into a user-facing engine group by its provider.
    "avis" → "seedream"; the decommissioned direct-BytePlus "ark" provider →
    None (excluded from every bucket, not just relabelled — it's dead history,
    not live Gemini/Atrium usage either); anything else → "gemini"."""
    p = ""
    if isinstance(params, dict):
        p = str(params.get("provider") or "").lower()
    if p == "avis":
        return "seedream"
    if p == "ark":
        return None
    return "gemini"


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


# This endpoint scans the ENTIRE done-flow_gen_image history (thousands of rows,
# growing forever) and the UI polls it continuously across every open tab. Run
# concurrently that scan measured ~2.7s each and saturated the request
# threadpool, so unrelated requests (page loads) queued behind it and the whole
# app felt like it "hung". A short TTL cache + a single-flight lock fixes that:
# at most ONE scan runs at a time and its result is reused for _CACHE_TTL_S, so
# 30 tabs polling cost one ~200ms scan every 15s instead of 30 concurrent ones.
# (A usage counter being a few seconds stale is imperceptible.)
_CACHE_TTL_S = float(os.getenv("FLOWBOARD_USAGE_CACHE_TTL_S", "15"))
_refresh_lock = threading.Lock()
_cache_value: Optional[dict] = None
_cache_ts = 0.0


@router.get("/usage")
def flow_usage() -> dict:
    """Cached, single-flight, stale-while-revalidate. A fresh value is served
    straight from memory; a stale one is served immediately while ONE caller
    refreshes in the foreground; only a completely cold cache blocks callers."""
    global _cache_value, _cache_ts
    if _cache_value is not None and (time.monotonic() - _cache_ts) < _CACHE_TTL_S:
        return _cache_value
    # Stale/cold: exactly one caller does the scan. Must block only when cold
    # (no value to serve yet); when merely stale, non-winners serve the old value.
    if _refresh_lock.acquire(blocking=_cache_value is None):
        try:
            if _cache_value is None or (time.monotonic() - _cache_ts) >= _CACHE_TTL_S:
                _cache_value = _compute_usage()
                _cache_ts = time.monotonic()
        finally:
            _refresh_lock.release()
    return _cache_value if _cache_value is not None else _compute_usage()


def _compute_usage() -> dict:
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
        eng = _engine_of(r.params)
        if eng is None:
            continue  # decommissioned "ark" provider — dead history, not counted anywhere
        imgs = _images_in(r.result)
        is_today = bool(r.created_at and _aware(r.created_at) >= start)
        total[eng] += imgs
        if is_today:
            today[eng] += imgs
        if eng == "seedream" and imgs:
            per_image = (
                SEEDREAM_USD_PER_IMAGE_2K if _resolution_of(r.params) == "2K" else SEEDREAM_USD_PER_IMAGE_1K
            )
            cost = imgs * per_image
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
        # Seedream: pay-per-use → report money spent. usd_per_image is the
        # actual blended average (1K/2K mix) over everything generated so far,
        # not a single flat rate.
        "seedream": {
            "today": today["seedream"],
            "total": total["seedream"],
            "usd_per_image": round(
                sd_cost["total"] / total["seedream"] if total["seedream"] else SEEDREAM_USD_PER_IMAGE_1K, 6
            ),
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
