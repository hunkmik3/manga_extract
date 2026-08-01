"""Media cache routes.

`GET /media/:media_id` streams bytes (cache hit → immediate; miss → one-shot
fetch from GCS then cache). `GET /api/media/:media_id/status` exposes cache
state for the frontend to poll while it waits for a URL to arrive.
"""
from __future__ import annotations

import logging
import re

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse

from flowboard.services import media as media_service

logger = logging.getLogger(__name__)

bytes_router = APIRouter(tags=["media"])
api_router = APIRouter(prefix="/api/media", tags=["media"])

_LOCAL_HOSTS = ("127.0.0.1", "localhost", "[::1]")
_DOWNLOAD_NAME_RE = re.compile(r"[^A-Za-z0-9._-]+")


def _download_name(media_id: str, suffix: str, filename: str | None) -> str:
    if isinstance(filename, str) and filename.strip():
        name = filename.strip().replace("\\", "/").rsplit("/", 1)[-1]
        name = _DOWNLOAD_NAME_RE.sub("_", name).strip("._")
        if name:
            if len(name) <= 160:
                return name
            if "." in name:
                stem, ext = name.rsplit(".", 1)
                ext = f".{ext[:16]}"
                return f"{stem[: max(1, 160 - len(ext))]}{ext}"
            return name[:160]
    return f"{media_id}{suffix}"


def _download_headers(media_id: str, suffix: str, filename: str | None = None) -> dict[str, str]:
    return {
        "Content-Disposition": f'attachment; filename="{_download_name(media_id, suffix, filename)}"',
    }


@bytes_router.get("/media/{media_id:path}")
async def get_media_bytes(
    media_id: str,
    request: Request,
    raw: int = 0,
    download: int = 0,
    filename: str | None = None,
):
    media_id = media_service.normalize_media_id(media_id)
    if not media_service.is_valid_media_id(media_id):
        raise HTTPException(status_code=400, detail="invalid media_id")

    as_download = bool(download)
    cached = media_service.cached_path(media_id)
    if cached is not None:
        # Viewers on the public (tunnel) hostname get result images from the R2
        # CDN copy when one exists — the ~20MB originals then don't stream up
        # this machine's uplink on every view. Local hosts keep reading straight
        # from disk (fastest, works offline, and no CDN round-trip). `?raw=1`
        # forces the same-origin file (canvas consumers — annotate-edit — need
        # it: the r2.dev bucket has no CORS policy, so a cross-origin redirect
        # would taint/fail the canvas).
        host = (request.headers.get("host") or "").lower()
        if not raw and not as_download and not host.startswith(_LOCAL_HOSTS):
            from flowboard.services.comic import r2

            cdn = r2.result_public_url(media_id)
            if cdn is not None:
                return RedirectResponse(cdn, status_code=302)
        return FileResponse(
            path=str(cached),
            media_type=media_service._mime_from_ext(cached.suffix),
            headers=_download_headers(media_id, cached.suffix, filename) if as_download else None,
        )

    # Cache miss — try one fetch through the stored URL.
    result = await media_service.fetch_and_cache(media_id)
    if result is None:
        status = media_service.status(media_id)
        return JSONResponse(status_code=404, content=status)
    _bytes, mime, path = result
    return FileResponse(
        path=str(path),
        media_type=mime,
        headers=_download_headers(media_id, path.suffix, filename) if as_download else None,
    )


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
    # Up to 2048: the viewer streams a light "view" image (~hundreds of KB) at
    # this size instead of the multi-MB original, so switching images is fast
    # over a tunnel; the full original is only fetched on deep zoom / download.
    # Grids/pickers still ask for ≤640.
    w = max(64, min(int(w), 2048))
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
            # Higher quality for the large "view" sizes (keeps lineart crisp);
            # small grid thumbs stay at 80 to stay tiny.
            quality = 90 if w >= 1024 else 80
            ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, quality])
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
