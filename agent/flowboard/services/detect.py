"""Object detection + cutout for the Grok-style segment editor.

Two cheap building blocks (no new heavy deps):

  * ``detect_objects`` — Gemini (via Atrium) returns the editable parts of an
    image as ``{label, box_2d}`` (normalized 0-1000). Boxes only (no masks) so
    the JSON stays small and never truncates.
  * ``cutout`` — OpenCV ``grabCut`` isolates the object inside a box into a
    transparent PNG. Classical, model-free, runs locally.
"""
from __future__ import annotations

import json
import logging
from typing import Optional

logger = logging.getLogger(__name__)

_DETECT_MODEL = "gemini-2.5-flash"
_DETECT_PROMPT = (
    "Detect the editable parts of this image for a photo-editor segment panel. "
    "Return each as a SEPARATE instance with a TIGHT box around only that item's "
    "visible pixels.\n"
    "WHAT TO INCLUDE (and nothing else):\n"
    "- the whole person (one entry)\n"
    "- each WHOLE garment as one entry (blazer, shirt, trousers, dress, skirt, "
    "coat) — do NOT split a garment into lapels/sleeves/legs\n"
    "- each accessory as its OWN instance: a pair = two entries (left earring AND "
    "right earring; left shoe AND right shoe); also bag, belt, glasses, watch, ring\n"
    "- hair (one entry)\n"
    "- background regions: wall, floor, sky\n"
    "DO NOT output skin regions (face, neck, chest, hands) or sub-parts of a garment.\n"
    "Labels: short and descriptive with color/material when clear (e.g. "
    "'black blazer', 'black trousers', 'black pointed heel', 'gold hoop earring', "
    "'black hair', 'concrete wall', 'concrete floor').\n"
    "Output ONLY a JSON array; each item {\"label\": name, \"box_2d\": "
    "[ymin,xmin,ymax,xmax] normalized 0-1000}. No masks, no prose."
)


async def detect_objects(media_id: str) -> list[dict]:
    """→ [{label, box}] where box is [ymin,xmin,ymax,xmax] in 0-1000.

    Prefers Grounding DINO (a real open-vocabulary detector, runs locally on the
    GPU — tight per-instance boxes for shoes/earrings). Falls back to the Gemini
    VLM (via Atrium) only if Grounding DINO is unavailable or returns nothing.
    """
    import asyncio

    from flowboard.services import gdino, media as media_service

    if gdino.available():
        p = media_service.cached_path(media_id)
        if p is not None:
            try:
                raw = await asyncio.to_thread(p.read_bytes)
                parts = await asyncio.to_thread(gdino.detect_parts, raw)
            except Exception as exc:  # noqa: BLE001
                logger.warning("gdino path failed, falling back to VLM: %s", exc)
                parts = []
            if parts:
                return [{"label": d["label"], "box": d["box"]} for d in parts]

    return await _detect_objects_vlm(media_id)


async def _detect_objects_vlm(media_id: str) -> list[dict]:
    """Gemini VLM fallback — image hosted on R2, boxes by coordinates."""
    import httpx

    from flowboard.services.comic import atrium_api, r2

    creds = atrium_api.client_creds()
    if not creds:
        raise RuntimeError("detect: ATRIUM creds not set")
    if not r2.is_configured():
        raise RuntimeError("detect: R2 not configured (image must be hosted by URL)")

    import asyncio

    url = await asyncio.to_thread(r2.upload_media, media_id)
    if not url:
        raise RuntimeError("detect: could not host image on R2")
    try:
        body = {
            "model": _DETECT_MODEL,
            "contents": [{"role": "user", "parts": [
                {"text": _DETECT_PROMPT},
                {"fileData": {"mimeType": "image/jpeg", "fileUri": url}},
            ]}],
            "config": {"responseMimeType": "application/json", "temperature": 0.0},
        }
        headers = {"x-client-id": creds[0], "x-client-secret": creds[1]}
        async with httpx.AsyncClient(timeout=120.0) as c:
            r = await c.post(f"{atrium_api.base_url()}/api/partner/llm/generate", headers=headers, json=body)
        if r.status_code != 200:
            raise RuntimeError(f"detect: http_{r.status_code}: {r.text[:200]}")
        txt = "".join(
            p.get("text", "")
            for cand in r.json().get("candidates", [])
            for p in (cand.get("content") or {}).get("parts", [])
        )
        data = json.loads(txt)
        out: list[dict] = []
        for d in data if isinstance(data, list) else []:
            box = d.get("box_2d")
            label = d.get("label")
            if isinstance(label, str) and isinstance(box, list) and len(box) == 4:
                out.append({"label": label.strip(), "box": [int(x) for x in box]})
        return out
    finally:
        await asyncio.to_thread(r2.delete_media, media_id)


def cutout(img_bytes: bytes, box_2d: list[int], *, feather: int = 2) -> Optional[bytes]:
    """Isolate the object inside ``box_2d`` ([ymin,xmin,ymax,xmax] 0-1000) into a
    transparent PNG using grabCut. Returns None on failure. CPU-heavy — call via
    ``asyncio.to_thread``."""
    try:
        import cv2
        import numpy as np

        img = cv2.imdecode(np.frombuffer(img_bytes, np.uint8), cv2.IMREAD_COLOR)
        if img is None:
            return None
        H, W = img.shape[:2]
        ymin, xmin, ymax, xmax = box_2d
        x0 = max(0, int(xmin / 1000 * W)); x1 = min(W, int(xmax / 1000 * W))
        y0 = max(0, int(ymin / 1000 * H)); y1 = min(H, int(ymax / 1000 * H))
        if x1 - x0 < 4 or y1 - y0 < 4:
            return None
        # grabCut needs a margin around the object rect inside the image.
        pad_x = max(2, (x1 - x0) // 20); pad_y = max(2, (y1 - y0) // 20)
        cx0, cy0 = max(0, x0 - pad_x), max(0, y0 - pad_y)
        cx1, cy1 = min(W, x1 + pad_x), min(H, y1 + pad_y)
        crop = img[cy0:cy1, cx0:cx1]
        ch, cw = crop.shape[:2]
        mask = np.zeros((ch, cw), np.uint8)
        rect = (x0 - cx0, y0 - cy0, x1 - x0, y1 - y0)
        bgd = np.zeros((1, 65), np.float64); fgd = np.zeros((1, 65), np.float64)
        cv2.grabCut(crop, mask, rect, bgd, fgd, 5, cv2.GC_INIT_WITH_RECT)
        fg = np.where((mask == cv2.GC_FGD) | (mask == cv2.GC_PR_FGD), 255, 0).astype(np.uint8)
        if feather > 0:
            fg = cv2.GaussianBlur(fg, (0, 0), feather)
        bgra = cv2.cvtColor(crop, cv2.COLOR_BGR2BGRA)
        bgra[:, :, 3] = fg
        # Tight-crop to the object rect (drop the grabCut margin).
        bgra = bgra[y0 - cy0:y1 - cy0, x0 - cx0:x1 - cx0]
        ok, buf = cv2.imencode(".png", bgra)
        return buf.tobytes() if ok else None
    except Exception as exc:  # noqa: BLE001
        logger.warning("cutout failed: %s", exc)
        return None
