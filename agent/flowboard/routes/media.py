"""Media cache routes.

`GET /media/:media_id` streams bytes (cache hit → immediate; miss → one-shot
fetch from GCS then cache). `GET /api/media/:media_id/status` exposes cache
state for the frontend to poll while it waits for a URL to arrive.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse, JSONResponse

from flowboard.services import media as media_service

logger = logging.getLogger(__name__)

bytes_router = APIRouter(tags=["media"])
api_router = APIRouter(prefix="/api/media", tags=["media"])


@bytes_router.get("/media/{media_id:path}")
async def get_media_bytes(media_id: str):
    media_id = media_service.normalize_media_id(media_id)
    if not media_service.is_valid_media_id(media_id):
        raise HTTPException(status_code=400, detail="invalid media_id")

    cached = media_service.cached_path(media_id)
    if cached is not None:
        return FileResponse(
            path=str(cached),
            media_type=media_service._mime_from_ext(cached.suffix),
        )

    # Cache miss — try one fetch through the stored URL.
    result = await media_service.fetch_and_cache(media_id)
    if result is None:
        status = media_service.status(media_id)
        return JSONResponse(status_code=404, content=status)
    _bytes, mime, path = result
    return FileResponse(path=str(path), media_type=mime)


_THUMB_DIR = media_service.MEDIA_CACHE_DIR / "thumbs"
_THUMB_DIR.mkdir(parents=True, exist_ok=True)


@api_router.get("/{media_id}/thumb")
def get_media_thumb(media_id: str, w: int = 256):
    """Downscaled JPEG thumbnail for grids/pickers — avoids shipping multi-MB
    full-res images to render 40-250px tiles. Generated once, cached on disk and
    in the browser (Cache-Control)."""
    media_id = media_service.normalize_media_id(media_id)
    if not media_service.is_valid_media_id(media_id):
        raise HTTPException(status_code=400, detail="invalid media_id")
    # Up to 1536 so the viewer can use a sharp-but-light "preview" thumb as an
    # instant placeholder while the full image loads (grids still ask for ≤640).
    w = max(64, min(int(w), 1536))
    src = media_service.cached_path(media_id)
    if src is None:
        raise HTTPException(status_code=404, detail="not cached")

    thumb = _THUMB_DIR / f"{media_id}_{w}.jpg"
    if not thumb.exists() or thumb.stat().st_mtime < src.stat().st_mtime:
        try:
            import cv2
            import numpy as np

            img = cv2.imdecode(np.frombuffer(src.read_bytes(), np.uint8), cv2.IMREAD_COLOR)
            if img is None:
                return FileResponse(path=str(src))  # not decodable → original
            h, wd = img.shape[:2]
            if max(h, wd) > w:
                s = w / max(h, wd)
                img = cv2.resize(img, (max(1, round(wd * s)), max(1, round(h * s))), interpolation=cv2.INTER_AREA)
            ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 80])
            if not ok:
                return FileResponse(path=str(src))
            thumb.write_bytes(buf.tobytes())
        except Exception:  # noqa: BLE001 — any failure → serve the original
            return FileResponse(path=str(src))

    return FileResponse(
        path=str(thumb),
        media_type="image/jpeg",
        headers={"Cache-Control": "public, max-age=86400"},
    )


@api_router.get("/{media_id}/status")
def get_media_status(media_id: str):
    media_id = media_service.normalize_media_id(media_id)
    if not media_service.is_valid_media_id(media_id):
        return JSONResponse(
            status_code=400,
            content={"available": False, "has_url": False, "reason": "invalid_id"},
        )
    return media_service.status(media_id)


@api_router.get("/_debug/assets")
def debug_assets():
    """Dev-only dump of every Asset row so we can see what URLs the extension
    has actually pushed to the agent. Remove once media flow is stable.
    """
    from sqlmodel import select as _select

    from flowboard.db import get_session
    from flowboard.db.models import Asset

    with get_session() as s:
        rows = s.exec(_select(Asset)).all()
        return {
            "count": len(rows),
            "rows": [
                {
                    "id": r.id,
                    "media_id": r.uuid_media_id,
                    "has_url": bool(r.url),
                    "url_head": (r.url or "")[:80] if r.url else None,
                    "mime": r.mime,
                    "cached": bool(r.local_path),
                    "node_id": r.node_id,
                }
                for r in rows
            ],
        }
