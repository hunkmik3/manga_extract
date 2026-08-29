"""Manga colorizer — chapter CRUD.

Bible-building and per-page colorizing run through the normal request queue
(POST /api/requests with type "colorize_build_bible" / "colorize_page"), so this
router only manages the chapter row: pages, style ref, and the editable bible.
"""
from __future__ import annotations

import io
import uuid
import zipfile
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, File, HTTPException, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel
from sqlmodel import select

from flowboard.db import get_session
from flowboard.db.models import ColorizeChapter
from flowboard.services import media as media_service

router = APIRouter(prefix="/api/colorize", tags=["colorize"])

# Local page ingest — NOT the Flow /api/upload path (that pushes to Google Flow
# via the extension and 502s when it's disconnected). Manga scans can be large,
# so allow more than the 20 MB Flow cap.
_ALLOWED_MIMES = {"image/png", "image/jpeg", "image/webp"}
_MAX_PAGE_BYTES = 40 * 1024 * 1024


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@router.post("/upload-page")
async def upload_page(file: UploadFile = File(...)):
    """Save one page/style image straight to the local media cache → media_id.
    Purely local; no Flow/extension involved."""
    mime = (file.content_type or "").lower().split(";")[0].strip()
    if mime not in _ALLOWED_MIMES:
        raise HTTPException(415, f"unsupported mime: {mime!r}")
    raw = await file.read(_MAX_PAGE_BYTES + 1)
    if not raw:
        raise HTTPException(400, "empty file")
    if len(raw) > _MAX_PAGE_BYTES:
        raise HTTPException(413, f"file too large: {len(raw)} > {_MAX_PAGE_BYTES}")
    mid = str(uuid.uuid4())
    if not media_service.ingest_inline_bytes(mid, raw, kind="image", mime=mime):
        raise HTTPException(500, "ingest failed")
    return {"media_id": mid, "mime": mime, "size": len(raw)}


class ChapterCreate(BaseModel):
    name: str = ""
    page_media_ids: list[str] = []
    style_ref_media_id: Optional[str] = None


class ChapterPatch(BaseModel):
    name: Optional[str] = None
    page_media_ids: Optional[list[str]] = None
    style_ref_media_id: Optional[str] = None
    bible: Optional[dict] = None  # human-edited bible at the checkpoint


def _summary(ch: ColorizeChapter) -> dict:
    return {
        "id": ch.id,
        "name": ch.name,
        "pages": len(ch.page_media_ids or []),
        "has_bible": ch.bible is not None,
        "colorized": len(ch.outputs or {}),
        "updated_at": ch.updated_at.isoformat() if ch.updated_at else None,
    }


@router.post("/chapters")
def create_chapter(body: ChapterCreate):
    with get_session() as s:
        ch = ColorizeChapter(
            name=body.name or "",
            page_media_ids=list(body.page_media_ids or []),
            style_ref_media_id=body.style_ref_media_id,
        )
        s.add(ch)
        s.commit()
        s.refresh(ch)
        return ch


@router.get("/chapters")
def list_chapters():
    with get_session() as s:
        rows = s.exec(select(ColorizeChapter).order_by(ColorizeChapter.id.desc())).all()
        return [_summary(c) for c in rows]


@router.get("/chapters/{chapter_id}")
def get_chapter(chapter_id: int):
    with get_session() as s:
        ch = s.get(ColorizeChapter, chapter_id)
        if ch is None:
            raise HTTPException(404, "chapter not found")
        return ch


class DetectPanels(BaseModel):
    media_id: str


@router.post("/detect-panels")
def detect_panels_route(body: DetectPanels):
    """Detect panels of a page → boxes as [ymin,xmin,ymax,xmax] normalized 0-1000
    (reading order). Used by the per-panel fix picker."""
    import cv2
    import numpy as np

    from flowboard.services.comic import panel_ml

    p = media_service.cached_path(body.media_id)
    if p is None:
        raise HTTPException(404, "media not found")
    try:
        bgr = cv2.imdecode(np.frombuffer(p.read_bytes(), np.uint8), cv2.IMREAD_COLOR)
    except OSError:
        raise HTTPException(404, "media unreadable")
    if bgr is None:
        raise HTTPException(422, "decode failed")
    h, w = bgr.shape[:2]
    try:
        boxes = panel_ml.detect_panels_ml(bgr)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(503, f"panel detector unavailable: {exc}")
    out = []
    for (x, y, bw, bh) in boxes:
        out.append([
            max(0, min(1000, int(y / h * 1000))),
            max(0, min(1000, int(x / w * 1000))),
            max(0, min(1000, int((y + bh) / h * 1000))),
            max(0, min(1000, int((x + bw) / w * 1000))),
        ])
    return {"panels": out}


class SamPoint(BaseModel):
    media_id: str
    x: float  # click X, normalized 0-1000
    y: float  # click Y, normalized 0-1000


@router.post("/sam-point")
def sam_point_route(body: SamPoint):
    """Click-to-segment: SAM segments the object at the click → its bounding box
    [ymin,xmin,ymax,xmax] (0-1000). Used by the Fix modal's SAM-click mode."""
    from flowboard.services import sam

    if not sam.available():
        raise HTTPException(503, "SAM models not available")
    p = media_service.cached_path(body.media_id)
    if p is None:
        raise HTTPException(404, "media not found")
    try:
        raw = p.read_bytes()
    except OSError:
        raise HTTPException(404, "media unreadable")
    box = sam.segment_point_box(raw, body.x, body.y, media_id=body.media_id)
    if box is None:
        raise HTTPException(422, "nothing segmented at that point")
    return {"box": box}


class SelectVariant(BaseModel):
    page_media_id: str
    output_media_id: str


@router.post("/chapters/{chapter_id}/select-variant")
def select_variant(chapter_id: int, body: SelectVariant):
    """Pick which colorized variant of a page is the chosen output."""
    with get_session() as s:
        ch = s.get(ColorizeChapter, chapter_id)
        if ch is None:
            raise HTTPException(404, "chapter not found")
        cands = list((ch.variants or {}).get(body.page_media_id) or [])
        if body.output_media_id not in cands:
            raise HTTPException(400, "output_media_id is not a variant of this page")
        outputs = dict(ch.outputs or {})
        outputs[body.page_media_id] = body.output_media_id
        ch.outputs = outputs
        ch.updated_at = _utcnow()
        s.add(ch)
        s.commit()
        s.refresh(ch)
        return ch


@router.get("/chapters/{chapter_id}/download")
def download_colorized(chapter_id: int):
    """ZIP of all colorized pages, named by reading order (001.png, 002.png…)."""
    with get_session() as s:
        ch = s.get(ColorizeChapter, chapter_id)
        if ch is None:
            raise HTTPException(404, "chapter not found")
        page_ids = list(ch.page_media_ids or [])
        outputs = dict(ch.outputs or {})
        name = (ch.name or f"chapter_{chapter_id}").strip()
    if not outputs:
        raise HTTPException(404, "no colorized pages yet")

    buf = io.BytesIO()
    n = 0
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for i, pid in enumerate(page_ids, start=1):
            out_mid = outputs.get(pid)
            if not out_mid:
                continue
            p = media_service.cached_path(out_mid)
            if p is None:
                continue
            try:
                data = p.read_bytes()
            except OSError:
                continue
            ext = p.suffix or ".png"
            z.writestr(f"{i:03d}{ext}", data)
            n += 1
    if n == 0:
        raise HTTPException(404, "no colorized pages available")

    safe = "".join(c if (c.isalnum() or c in "-_ ") else "_" for c in name).strip() or f"chapter_{chapter_id}"
    return Response(
        content=buf.getvalue(),
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{safe}_colorized.zip"'},
    )


@router.patch("/chapters/{chapter_id}")
def patch_chapter(chapter_id: int, body: ChapterPatch):
    with get_session() as s:
        ch = s.get(ColorizeChapter, chapter_id)
        if ch is None:
            raise HTTPException(404, "chapter not found")
        if body.name is not None:
            ch.name = body.name
        if body.page_media_ids is not None:
            ch.page_media_ids = list(body.page_media_ids)
        if body.style_ref_media_id is not None:
            ch.style_ref_media_id = body.style_ref_media_id or None
        if body.bible is not None:
            ch.bible = body.bible
        ch.updated_at = _utcnow()
        s.add(ch)
        s.commit()
        s.refresh(ch)
        return ch


@router.delete("/chapters/{chapter_id}")
def delete_chapter(chapter_id: int):
    with get_session() as s:
        ch = s.get(ColorizeChapter, chapter_id)
        if ch is None:
            raise HTTPException(404, "chapter not found")
        s.delete(ch)
        s.commit()
    return {"ok": True}
