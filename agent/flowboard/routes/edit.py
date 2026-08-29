"""Grok-style segment editor — on-demand object cutout.

Detection runs through the queue (POST /api/requests type "detect_objects").
Editing reuses Flow's ``flow_gen_image`` with the Grok model. This router only
serves the fast, on-click cutout (grabCut) for a selected segment box.
"""
from __future__ import annotations

import asyncio
import uuid

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from flowboard.services import detect, media as media_service
from flowboard.services import sam

router = APIRouter(prefix="/api/edit", tags=["edit"])


class CutoutBody(BaseModel):
    media_id: str
    box: list[int]  # [ymin, xmin, ymax, xmax] normalized 0-1000


@router.post("/cutout")
async def make_cutout(body: CutoutBody):
    """Isolate the object in ``box`` into a transparent PNG media → {media_id}."""
    if len(body.box) != 4:
        raise HTTPException(400, "box must be [ymin,xmin,ymax,xmax]")
    p = media_service.cached_path(body.media_id)
    if p is None:
        raise HTTPException(404, "media not found")
    try:
        raw = p.read_bytes()
    except OSError:
        raise HTTPException(404, "media unreadable")
    # MobileSAM first (learned mask, encode-once/click-fast); grabCut fallback
    # if the models are missing or SAM fails on this box.
    png = None
    if sam.available():
        png = await asyncio.to_thread(sam.cutout_box, raw, body.box, media_id=body.media_id)
    if not png:
        png = await asyncio.to_thread(detect.cutout, raw, body.box)
    if not png:
        raise HTTPException(422, "cutout failed")
    mid = str(uuid.uuid4())
    if not media_service.ingest_inline_bytes(mid, png, kind="image", mime="image/png"):
        raise HTTPException(500, "ingest failed")
    return {"media_id": mid}
