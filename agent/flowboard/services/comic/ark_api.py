"""BytePlus Ark image engine — a 5th image backend for the Flow studio, calling
BytePlus **Seedream** models DIRECTLY (no Avis passthrough)::

    POST /api/v3/images/generations       (synchronous; the Ark gateway tolerates
                                            the ~100-190s a Pro gen takes)

Notes vs the other engines:

  * **Region ap-southeast (Singapore)** — close to us, so no transpacific/S3
    throttling like the Atrium downloadUrl path.
  * **Inline output.** We request ``response_format: "b64_json"`` → the image
    comes back as ``data[0].b64_json`` (no separate download hop).
  * **Inline input.** Reference/source images go in the ``image`` field as
    base64 data URLs (one string, or an array for multiple) — works locally.

Auth: ``ARK_API_KEY`` as ``Authorization: Bearer``. Model:
``dola-seedream-5-0-pro-260628`` (BytePlus versioned id).
"""
from __future__ import annotations

import asyncio
import base64
import logging
import math
import os
from typing import Callable, Optional, Sequence

import httpx

logger = logging.getLogger(__name__)

_DEFAULT_BASE = "https://ark.ap-southeast.bytepluses.com"
_TIMEOUT_S = 300.0
MAX_ATTEMPTS = 5
SAFETY_MAX_ATTEMPTS = 3
_BACKOFF_S = 2.0
_FATAL_STATUSES = {400, 401, 403, 404}

_DEFAULT_MODEL = "dola-seedream-5-0-pro-260628"

# Seedream 5 requires total pixels within [~1920², 4096²] (same model whether
# reached via Ark or Avis — smaller requests 400 with "must be at least …").
_RATIOS = {
    "1:1": (1, 1), "16:9": (16, 9), "9:16": (9, 16), "4:3": (4, 3),
    "3:4": (3, 4), "3:2": (3, 2), "2:3": (2, 3), "21:9": (21, 9), "9:21": (9, 21),
}
_EDGE = {"1K": 1024, "2K": 2048, "4K": 4096}
# Seedream 5 valid area band: [~1920², ~2150²]. Below → "must be at least …";
# above → "must be at most 4624220 pixels". So the useful ceiling is ~2K, not 4K.
_MIN_PIXELS = 3_686_400
_MAX_PIXELS = 4_624_220
_MAX_EDGE = 4096


class _ArkEmpty(RuntimeError):
    """200 OK but no image — treat as a transient hiccup and retry."""


def base_url() -> str:
    return (os.getenv("ARK_BASE_URL", "").strip() or _DEFAULT_BASE).rstrip("/")


def api_key() -> Optional[str]:
    k = os.getenv("ARK_API_KEY", "").strip()
    return k or None


def is_configured() -> bool:
    return api_key() is not None


def _headers() -> dict:
    return {"Content-Type": "application/json", "Authorization": f"Bearer {api_key() or ''}"}


def _round16(x: float) -> int:
    return max(256, int(round(x / 16.0)) * 16)


def size_token(aspect_ratio: Optional[str], image_size: Optional[str]) -> Optional[str]:
    """(aspect_ratio, image_size) → ``<W>x<H>`` clamped into Seedream's valid
    pixel band [MIN, MAX]. None when there's no size hint (Ark then defaults).
    Rounds to a multiple of 16 with a small headroom so rounding can't push the
    area back outside the band."""
    edge = _EDGE.get((image_size or "").upper())
    if edge is None:
        return None
    w, h = _RATIOS.get(aspect_ratio or "1:1", (1, 1))
    if w >= h:
        W, H = float(edge), edge * h / w
    else:
        W, H = edge * w / h, float(edge)
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


def _mime_of(data: bytes) -> str:
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return "image/png"


def _data_url(data: bytes) -> str:
    return f"data:{_mime_of(data)};base64," + base64.b64encode(data).decode("ascii")


def _build_body(prompt: str, images: Optional[Sequence[bytes]], model: str, size: Optional[str]) -> dict:
    body: dict = {
        "model": model,
        "prompt": prompt,
        "response_format": "b64_json",
        "watermark": False,
        "stream": False,
    }
    if size:
        body["size"] = size
    urls = [_data_url(b) for b in (images or []) if b]
    if urls:
        # Ark accepts a single data URL or an array for multi-reference.
        body["image"] = urls[0] if len(urls) == 1 else urls
    return body


def _error_detail(resp: httpx.Response) -> str:
    try:
        j = resp.json()
        if isinstance(j, dict):
            err = j.get("error")
            if isinstance(err, dict):
                msg = err.get("message") or err.get("code") or ""
                if msg:
                    return f"{resp.status_code}: {str(msg)[:300]}"
            if j.get("message"):
                return f"{resp.status_code}: {str(j['message'])[:300]}"
    except Exception:  # noqa: BLE001
        pass
    return f"http_{resp.status_code}"


def _extract_image(payload: dict) -> tuple[Optional[bytes], Optional[str]]:
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, list) or not data:
        return None, None
    first = data[0] if isinstance(data[0], dict) else {}
    b64 = first.get("b64_json")
    if isinstance(b64, str) and b64:
        try:
            return base64.b64decode(b64), None
        except Exception:  # noqa: BLE001
            pass
    url = first.get("url")
    if isinstance(url, str) and url:
        return None, url
    return None, None


async def _generate_once(client: httpx.AsyncClient, body: dict) -> bytes:
    """One /images/generations call → image bytes. Raises BridgeEditError on a
    fatal API response, RuntimeError on retryable, _ArkEmpty on 200-but-empty."""
    from flowboard.services.comic.bridge import BridgeEditError

    resp = await client.post(
        f"{base_url()}/api/v3/images/generations", headers=_headers(), json=body
    )
    if resp.status_code != 200:
        detail = _error_detail(resp)
        if resp.status_code in _FATAL_STATUSES:
            raise BridgeEditError(f"ark: {detail}", attempts=1)
        raise RuntimeError(f"ark retryable: {detail}")

    inline, url = _extract_image(resp.json())
    if inline:
        return inline
    if url:
        img = await client.get(url)
        if img.status_code != 200 or not img.content:
            raise RuntimeError(f"ark image fetch http_{img.status_code}")
        return img.content
    raise _ArkEmpty("ark: no image in response")


async def generate_image_variants(
    prompt: str,
    images: Optional[Sequence[bytes]] = None,
    *,
    image_model: str,
    aspect_ratio: str = "1:1",
    variant_count: int = 1,
    image_size: Optional[str] = None,
    max_attempts: int = MAX_ATTEMPTS,
    on_progress: Optional[Callable[[int, int], None]] = None,
) -> list[bytes]:
    """Text→image (and image-conditioned) via BytePlus Ark Seedream. ``images``
    are raw bytes for source/reference frames, sent inline as data URLs. Variants
    run IN PARALLEL; each retries 5xx/timeouts with backoff, fatal API errors
    fail that variant. Partial success wins. Returns ≥1 image or BridgeEditError."""
    from flowboard.services.comic.bridge import BridgeEditError

    if not is_configured():
        raise BridgeEditError("ark: ARK_API_KEY not set in .env", attempts=0)

    model = image_model.strip() if isinstance(image_model, str) and image_model.strip() else _DEFAULT_MODEL
    size = size_token(aspect_ratio, image_size)
    body = _build_body(prompt, images, model, size)
    n = max(1, min(int(variant_count or 1), 4))
    completed = 0

    async def _one(client: httpx.AsyncClient) -> bytes:
        nonlocal completed
        last = "unknown"
        empty_tries = 0
        for attempt in range(1, max_attempts + 1):
            try:
                out = await _generate_once(client, body)
                completed += 1
                if on_progress:
                    on_progress(completed, n)
                return out
            except BridgeEditError:
                raise
            except _ArkEmpty as exc:
                empty_tries += 1
                last = str(exc)[:200]
                logger.warning("ark empty %d/%d", empty_tries, SAFETY_MAX_ATTEMPTS)
                if empty_tries >= SAFETY_MAX_ATTEMPTS:
                    raise BridgeEditError(f"{last} — empty after {empty_tries} tries", attempts=empty_tries)
                await asyncio.sleep(_BACKOFF_S * attempt)
            except Exception as exc:  # noqa: BLE001
                last = f"{type(exc).__name__}: {exc}"[:200].rstrip(": ")
                logger.warning("ark attempt %d/%d: %s", attempt, max_attempts, last)
                if attempt < max_attempts:
                    await asyncio.sleep(_BACKOFF_S * attempt)
        raise BridgeEditError(f"ark: {last}", attempts=max_attempts)

    async with httpx.AsyncClient(timeout=_TIMEOUT_S) as client:
        results = await asyncio.gather(*[_one(client) for _ in range(n)], return_exceptions=True)

    outs = [r for r in results if not isinstance(r, BaseException)]
    if outs:
        if len(outs) < n:
            logger.warning("ark: %d/%d variant(s) ok (partial)", len(outs), n)
        else:
            logger.info("ark ok: %d/%d via %s", len(outs), n, model)
        return outs
    first = next((r for r in results if isinstance(r, BaseException)), None)
    if isinstance(first, BridgeEditError):
        raise first
    raise BridgeEditError(f"ark: {first}", attempts=max_attempts)
