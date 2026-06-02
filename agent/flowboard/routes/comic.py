"""Comic pipeline — debug / acceptance surface.

A thin HTTP entry point over ``services.comic.bridge.edit_image`` so the
Phase-2 acceptance ("call edit_image(test_image, 'make it grayscale') through
the bridge → get a valid image back") can be exercised end-to-end against a
running agent + connected Flow extension, without yet wiring a node.

This intentionally runs the edit *inline* (like ``/api/upload`` does), not via
the worker queue — the comic nodes will dispatch through the queue later; this
route is just the developer-facing smoke seam. Nothing here touches the
relay / session machinery.
"""
from __future__ import annotations

import logging
import uuid
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from fastapi.responses import Response

from flowboard.config import STORAGE_DIR
from flowboard.db import get_session
from flowboard.db.models import Node, Request
from flowboard.routes.upload import (
    ALLOWED_UPLOAD_MIMES,
    MAX_UPLOAD_BYTES,
    _sniff_image_mime,
)
from flowboard.services.comic.bridge import BridgeEditError, edit_image
from flowboard.services.comic.panels import PAGE_EXTS
from flowboard.services.flow_sdk import is_valid_project_id
from flowboard.worker.processor import get_worker

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/comic", tags=["comic"])

# Where uploaded comic pages land before extraction. Each upload gets its own
# subfolder so iter_page_paths() sees only that batch, sorted by filename.
COMIC_UPLOAD_DIR = STORAGE_DIR / "comic_uploads"
MAX_PAGES_PER_UPLOAD = 300


async def _read_image(file: UploadFile) -> tuple[bytes, str]:
    raw = await file.read(MAX_UPLOAD_BYTES + 1)
    if not raw:
        raise HTTPException(status_code=400, detail="empty file")
    if len(raw) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="file too large")
    # Trust the magic bytes over the browser-supplied content-type.
    mime = _sniff_image_mime(raw) or (file.content_type or "").split(";")[0].strip().lower()
    if mime not in ALLOWED_UPLOAD_MIMES:
        raise HTTPException(status_code=415, detail=f"unsupported image mime: {mime!r}")
    return raw, mime


@router.post("/edit-image")
async def edit_image_route(
    project_id: str = Form(...),
    prompt: str = Form(...),
    file: UploadFile = File(...),
    aspect_ratio: str = Form("IMAGE_ASPECT_RATIO_LANDSCAPE"),
    image_model: Optional[str] = Form(None),
    refs: list[UploadFile] = File(default=[]),
):
    """Edit one image through the Flow bridge and return the result bytes.

    Returns the raw edited image (Content-Type sniffed from the result bytes),
    with ``X-Comic-Source-Mime`` for debugging. 502 on bridge failure.
    """
    if not is_valid_project_id(project_id):
        raise HTTPException(status_code=400, detail="invalid project_id")

    raw, mime = await _read_image(file)
    ref_bytes: list[bytes] = []
    for rf in refs or []:
        rb, _ = await _read_image(rf)
        ref_bytes.append(rb)

    try:
        out = await edit_image(
            raw,
            prompt,
            reference_images=ref_bytes or None,
            project_id=project_id,
            aspect_ratio=aspect_ratio,
            image_model=image_model or None,
            mime=mime,
        )
    except BridgeEditError as exc:
        logger.warning("comic edit-image failed: %s", exc)
        raise HTTPException(
            status_code=502,
            detail={"message": "bridge_edit_failed", "reason": exc.reason, "attempts": exc.attempts},
        )

    out_mime = _sniff_image_mime(out) or "application/octet-stream"
    return Response(content=out, media_type=out_mime, headers={"X-Comic-Source-Mime": mime})


def _safe_page_name(raw_name: Optional[str], index: int) -> Optional[str]:
    """Sanitize an uploaded filename to a flat basename with an allowed page
    extension. Returns None to skip non-page files (subfolder junk, .DS_Store)."""
    base = Path(raw_name or "").name  # strip any directory components
    if not base or base.startswith("."):
        return None
    if Path(base).suffix.lower() not in PAGE_EXTS:
        return None
    return base


@router.post("/upload-pages")
async def upload_pages(
    files: list[UploadFile] = File(...),
    node_id: Optional[int] = Form(None),
    debug: bool = Form(False),
    detector: str = Form("heuristic"),
):
    """Accept an uploaded folder of comic pages (browser ``webkitdirectory``),
    save them to a per-upload temp folder, then dispatch the same
    ``extract_panels`` worker task against that folder.

    Returns the queued Request so the frontend polls it exactly like the
    path-based flow. This is the upload alternative to typing a server-side
    folder path — handy when the pages aren't already on the agent host.
    """
    if node_id is not None:
        with get_session() as s:
            if not s.get(Node, node_id):
                raise HTTPException(404, "node not found")

    batch_dir = COMIC_UPLOAD_DIR / uuid.uuid4().hex
    batch_dir.mkdir(parents=True, exist_ok=True)

    saved = 0
    used_names: set[str] = set()
    for i, f in enumerate(files):
        name = _safe_page_name(f.filename, i)
        if name is None:
            continue
        raw = await f.read(MAX_UPLOAD_BYTES + 1)
        if not raw or len(raw) > MAX_UPLOAD_BYTES:
            continue
        # De-dupe collisions (same basename from different subfolders) while
        # keeping the original name so filename sort order is preserved.
        if name in used_names:
            stem, suf = Path(name).stem, Path(name).suffix
            name = f"{stem}_{i}{suf}"
        used_names.add(name)
        (batch_dir / name).write_bytes(raw)
        saved += 1
        if saved >= MAX_PAGES_PER_UPLOAD:
            break

    if saved == 0:
        # Nothing usable — clean up the empty dir and tell the caller.
        try:
            batch_dir.rmdir()
        except OSError:
            pass
        raise HTTPException(
            status_code=400,
            detail=f"no usable page images (allowed: {', '.join(PAGE_EXTS)})",
        )

    # Node 1 only ingests the full pages; panel detection happens in Node 2.
    # (detector/debug form fields are accepted for backward-compat but unused.)
    with get_session() as s:
        req = Request(
            node_id=node_id,
            type="import_pages",
            params={"folder": str(batch_dir)},
            status="queued",
        )
        s.add(req)
        s.commit()
        s.refresh(req)
        rid = req.id
        row = req

    assert rid is not None
    get_worker().enqueue(rid)
    logger.info("comic upload-pages: %d page(s) → %s (req=%s)", saved, batch_dir, rid)
    return row
