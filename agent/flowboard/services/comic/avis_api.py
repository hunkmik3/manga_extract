"""Avis image engine (https://api.avis.xyz) — reaches BytePlus **Seedream** via
Avis' async endpoint. Used by the manga colorizer (and available to Flow).

  POST /api/v1/image/generations/async   → {data:{generationId}}
  GET  /api/v1/image/generations/async/:id → poll until status succeeded/failed

Nicer than the Ark-direct path for local use: reference/source images are sent
**inline** (base64 data — no R2/tunnel), and the sync gateway's 504 on slow (Pro)
gens is avoided by polling the async job. Auth: ``AVIS_API_KEY`` sent as both
``Authorization: Bearer`` and ``X-Api-Key``.
"""
from __future__ import annotations

import asyncio
import base64
import logging
import math
import os
import time
from typing import Callable, Optional, Sequence

import httpx

logger = logging.getLogger(__name__)

_DEFAULT_BASE = "https://api.avis.xyz"
_TIMEOUT_S = 120.0
MAX_ATTEMPTS = 5
SAFETY_MAX_ATTEMPTS = 3
_BACKOFF_S = 2.0
_FATAL_STATUSES = {400, 401, 403, 404}
_DEFAULT_MODEL = "dola-seedream-5-0-pro"

_POLL_INTERVAL_S = 3.0
_POLL_TIMEOUT_S = 360.0

# Seedream valid area band (same model whether via Avis or Ark).
_RATIOS = {
    "1:1": (1, 1), "16:9": (16, 9), "9:16": (9, 16), "4:3": (4, 3),
    "3:4": (3, 4), "3:2": (3, 2), "2:3": (2, 3), "21:9": (21, 9), "9:21": (9, 21),
}
_EDGE = {"1K": 1024, "2K": 2048, "4K": 4096}
_MIN_PIXELS = 3_686_400
_MAX_PIXELS = 4_624_220
_MAX_EDGE = 4096


def _is_moderation_block(err: str) -> bool:
    """BytePlus/Avis input-or-output content filter — retrying the same payload
    never helps (and burns quota). Callers should fail the variant immediately."""
    low = (err or "").lower()
    return "sensitive information" in low or "contentfilter" in low.replace(" ", "")


class _AvisEmpty(RuntimeError):
    """Job succeeded but no image — retryable."""


def base_url() -> str:
    return (os.getenv("AVIS_BASE_URL", "").strip() or _DEFAULT_BASE).rstrip("/")


def api_key() -> Optional[str]:
    k = os.getenv("AVIS_API_KEY", "").strip()
    return k or None


def is_configured() -> bool:
    return api_key() is not None


def _headers() -> dict:
    k = api_key() or ""
    return {"Authorization": f"Bearer {k}", "X-Api-Key": k}


def _round16(x: float) -> int:
    return max(256, int(round(x / 16.0)) * 16)


def _clamp_band(W: float, H: float) -> str:
    m = max(W, H)
    if m > _MAX_EDGE:
        W, H = W * _MAX_EDGE / m, H * _MAX_EDGE / m
    area = W * H
    lo, hi = _MIN_PIXELS * 1.02, _MAX_PIXELS * 0.98
    if area < lo:
        k = math.sqrt(lo / area); W, H = W * k, H * k
    elif area > hi:
        k = math.sqrt(hi / area); W, H = W * k, H * k
    return f"{_round16(W)}x{_round16(H)}"


def size_token(aspect_ratio: Optional[str], image_size: Optional[str]) -> Optional[str]:
    edge = _EDGE.get((image_size or "").upper())
    if edge is None:
        return None
    w, h = _RATIOS.get(aspect_ratio or "1:1", (1, 1))
    if w >= h:
        W, H = float(edge), edge * h / w
    else:
        W, H = edge * w / h, float(edge)
    return _clamp_band(W, H)


def size_for_dims(w: int, h: int) -> Optional[str]:
    """<W>x<H> from arbitrary pixel dims, clamped to Seedream's band."""
    if not w or not h or w < 1 or h < 1:
        return None
    return _clamp_band(float(w), float(h))


def _mime_of(data: bytes) -> str:
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return "image/png"


def _build_body(prompt: str, images: Optional[Sequence[bytes]], model: str, size: Optional[str]) -> dict:
    content: list[dict] = []
    for data in images or []:
        content.append({
            "type": "imageBase64",
            "data": base64.b64encode(data).decode("ascii"),
            "mediaType": _mime_of(data),
        })
    content.append({"type": "text", "text": prompt})
    body: dict = {"model": model, "content": content, "numberOfImages": 1,
                  "responseFormat": "b64_json", "outputFormat": "png",
                  # Seedream stamps an "AI generated" watermark unless disabled.
                  # Send the flag under the common spellings so whichever the
                  # Avis wrapper forwards takes effect.
                  "watermark": False, "addWatermark": False}
    if size:
        body["size"] = size
    return body


def _error_detail(resp: httpx.Response) -> str:
    try:
        j = resp.json()
        if isinstance(j, dict):
            errs = j.get("errors")
            if isinstance(errs, list) and errs:
                return f"{resp.status_code}: {str(errs[0])[:300]}"
            msg = j.get("message") or ((j.get("error") or {}).get("message") if isinstance(j.get("error"), dict) else "") or ""
            if msg:
                return f"{resp.status_code}: {str(msg)[:300]}"
    except Exception:  # noqa: BLE001
        pass
    return f"http_{resp.status_code}"


def _gid(payload: dict) -> Optional[str]:
    root = payload.get("data") if isinstance(payload, dict) and isinstance(payload.get("data"), dict) else payload
    g = (root.get("generationId") or root.get("id")) if isinstance(root, dict) else None
    return g if isinstance(g, str) and g else None


def _extract_image(payload: dict) -> tuple[Optional[bytes], Optional[str]]:
    root = payload.get("data") if isinstance(payload, dict) and isinstance(payload.get("data"), dict) else payload
    if isinstance(root, dict) and isinstance(root.get("result"), dict):
        root = root["result"]
    imgs = root.get("images") if isinstance(root, dict) else None
    if not isinstance(imgs, list) or not imgs:
        return None, None
    first = imgs[0] if isinstance(imgs[0], dict) else {}
    b64 = first.get("b64") or first.get("b64_json")
    if isinstance(b64, str) and b64:
        try:
            return base64.b64decode(b64), None
        except Exception:  # noqa: BLE001
            pass
    url = first.get("url") or first.get("downloadUrl") or first.get("download_url")
    if isinstance(url, str) and url:
        return None, url
    return None, None


async def _generate_once(client: httpx.AsyncClient, body: dict) -> bytes:
    from flowboard.services.comic.bridge import BridgeEditError

    resp = await client.post(f"{base_url()}/api/v1/image/generations/async", headers=_headers(), json=body)
    if resp.status_code not in (200, 202):
        detail = _error_detail(resp)
        if resp.status_code in _FATAL_STATUSES:
            raise BridgeEditError(f"avis: {detail}", attempts=1)
        raise RuntimeError(f"avis retryable: {detail}")
    gid = _gid(resp.json())
    if not gid:
        raise RuntimeError("avis: async submit returned no generationId")

    poll_url = f"{base_url()}/api/v1/image/generations/async/{gid}"
    deadline = time.monotonic() + _POLL_TIMEOUT_S
    while time.monotonic() < deadline:
        await asyncio.sleep(_POLL_INTERVAL_S)
        p = await client.get(poll_url, headers=_headers())
        if p.status_code != 200:
            continue
        payload = p.json()
        root = payload.get("data") if isinstance(payload, dict) and isinstance(payload.get("data"), dict) else payload
        status = (root.get("status") if isinstance(root, dict) else "") or ""
        if status == "succeeded":
            inline, url = _extract_image(payload)
            if inline:
                return inline
            if url:
                img = await client.get(url)
                if img.status_code != 200 or not img.content:
                    raise RuntimeError(f"avis image fetch http_{img.status_code}")
                return img.content
            raise _AvisEmpty("avis: job succeeded but no image")
        if status == "failed":
            err = str(root.get("error") if isinstance(root, dict) else "")[:200]
            if _is_moderation_block(err):
                raise BridgeEditError(f"avis: {err}", attempts=1)
            raise RuntimeError(f"avis job failed: {err}")
    raise RuntimeError("avis: async poll timed out")


async def generate_image_variants(
    prompt: str,
    images: Optional[Sequence[bytes]] = None,
    *,
    image_model: str,
    aspect_ratio: str = "1:1",
    variant_count: int = 1,
    image_size: Optional[str] = None,
    size_override: Optional[str] = None,
    max_attempts: int = MAX_ATTEMPTS,
    on_progress: Optional[Callable[[int, int], None]] = None,
) -> list[bytes]:
    """Text→image (and image-conditioned) via Avis Seedream. ``images`` are raw
    bytes sent inline. Variants run IN PARALLEL; each retries with backoff.
    Returns ≥1 PNG or raises BridgeEditError."""
    from flowboard.services.comic.bridge import BridgeEditError

    if not is_configured():
        raise BridgeEditError("avis: AVIS_API_KEY not set in .env", attempts=0)
    model = image_model.strip() if isinstance(image_model, str) and image_model.strip() else _DEFAULT_MODEL
    size = size_override or size_token(aspect_ratio, image_size)
    body = _build_body(prompt, images, model, size)
    n = max(1, min(int(variant_count or 1), 4))
    completed = 0

    async def _one(client: httpx.AsyncClient) -> bytes:
        nonlocal completed
        last = "unknown"
        empty = 0
        for attempt in range(1, max_attempts + 1):
            try:
                out = await _generate_once(client, body)
                completed += 1
                if on_progress:
                    on_progress(completed, n)
                return out
            except BridgeEditError:
                raise
            except _AvisEmpty as exc:
                empty += 1
                last = str(exc)[:200]
                if empty >= SAFETY_MAX_ATTEMPTS:
                    raise BridgeEditError(f"{last} — empty after {empty} tries", attempts=empty)
                await asyncio.sleep(_BACKOFF_S * attempt)
            except Exception as exc:  # noqa: BLE001
                last = f"{type(exc).__name__}: {exc}"[:200].rstrip(": ")
                logger.warning("avis attempt %d/%d: %s", attempt, max_attempts, last)
                if attempt < max_attempts:
                    await asyncio.sleep(_BACKOFF_S * attempt)
        raise BridgeEditError(f"avis: {last}", attempts=max_attempts)

    async with httpx.AsyncClient(timeout=_TIMEOUT_S) as client:
        results = await asyncio.gather(*[_one(client) for _ in range(n)], return_exceptions=True)
    outs = [r for r in results if not isinstance(r, BaseException)]
    if outs:
        return outs
    first = next((r for r in results if isinstance(r, BaseException)), None)
    if isinstance(first, BridgeEditError):
        raise first
    raise BridgeEditError(f"avis: {first}", attempts=max_attempts)
