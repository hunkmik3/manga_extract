"""Direct Gemini-API image engine — the bridge's alternative backend.

Same contract as the Flow path (``bridge.edit_image_variants``): source panel +
optional reference images + prompt → edited image bytes. But it calls the
public ``generateContent`` endpoint with an API key instead of driving the
Flow web app through the extension — so it needs **no Chrome extension, no
logged-in Flow tab, no reCAPTCHA token**, and it does not consume Flow's
per-model daily quota (it bills/limits against the API key instead).

Selected per node via the model dropdown: any ``image_model`` starting with
``gemini-`` (e.g. ``gemini-3-pro-image`` = Nano Banana Pro,
``gemini-2.5-flash-image`` = Nano Banana) routes here; the ``NANO_BANANA_*``
nicknames keep using the Flow bridge.

Key: ``GEMINI_API_KEY`` in the gitignored ``.env``.
"""
from __future__ import annotations

import asyncio
import base64
import logging
import os
from typing import Optional, Sequence

import httpx

logger = logging.getLogger(__name__)

_API_BASE = "https://generativelanguage.googleapis.com/v1beta/models"
# Image gen latency under peak load can exceed 2-3 minutes before the request
# completes — a short client timeout turns "slow but would succeed" into a
# spurious failure. (Observed: same call timing out at 120 s, then completing
# in 11-17 s once load eased.)
_TIMEOUT_S = 240.0
MAX_ATTEMPTS = 3
_BACKOFF_S = 2.0

# Flow aspect enum → Gemini imageConfig.aspectRatio
_ASPECTS = {
    "IMAGE_ASPECT_RATIO_PORTRAIT": "9:16",
    "IMAGE_ASPECT_RATIO_LANDSCAPE": "16:9",
    "IMAGE_ASPECT_RATIO_SQUARE": "1:1",
}

# API errors retrying can never fix → fail fast (mirrors the Flow bridge's
# classifier): quota, auth, and content blocks. 503/504/timeouts ARE retried —
# that's the "high demand" case that does clear.
_FATAL_STATUSES = {400, 401, 403, 404, 429}


def is_api_model(image_model: object) -> bool:
    return isinstance(image_model, str) and image_model.startswith("gemini-")


def api_key() -> Optional[str]:
    key = os.getenv("GEMINI_API_KEY", "").strip()
    return key or None


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def _build_body(
    image_bytes: bytes,
    prompt: str,
    reference_images: Optional[Sequence[bytes]],
    aspect_ratio: str,
    mime: str,
) -> dict:
    # Input order mirrors the Flow bridge: the source panel first (ground
    # truth), then the reference images, then the instruction text.
    parts: list[dict] = [{"inline_data": {"mime_type": mime, "data": _b64(bytes(image_bytes))}}]
    for ref in reference_images or []:
        if ref:
            parts.append({"inline_data": {"mime_type": "image/png", "data": _b64(bytes(ref))}})
    parts.append({"text": prompt})
    body: dict = {
        "contents": [{"parts": parts}],
        "generationConfig": {"responseModalities": ["IMAGE"]},
    }
    ar = _ASPECTS.get(aspect_ratio)
    if ar:
        body["generationConfig"]["imageConfig"] = {"aspectRatio": ar}
    return body


def _extract_image(payload: dict) -> Optional[bytes]:
    for cand in payload.get("candidates") or []:
        for part in (cand.get("content") or {}).get("parts") or []:
            inline = part.get("inlineData") or part.get("inline_data")
            if isinstance(inline, dict) and inline.get("data"):
                try:
                    return base64.b64decode(inline["data"])
                except Exception:  # noqa: BLE001
                    return None
    return None


async def _generate_once(client: httpx.AsyncClient, model: str, key: str, body: dict) -> bytes:
    """One generateContent call → image bytes. Raises BridgeEditError on a
    fatal (non-retryable) API response; RuntimeError on retryable trouble."""
    from flowboard.services.comic.bridge import BridgeEditError

    resp = await client.post(f"{_API_BASE}/{model}:generateContent", params={"key": key}, json=body)
    if resp.status_code != 200:
        try:
            err = resp.json().get("error", {})
            detail = f"{err.get('status', resp.status_code)}: {str(err.get('message', ''))[:200]}"
        except Exception:  # noqa: BLE001
            detail = f"http_{resp.status_code}"
        if resp.status_code in _FATAL_STATUSES:
            raise BridgeEditError(f"gemini_api: {detail}", attempts=1)
        raise RuntimeError(f"gemini_api retryable: {detail}")
    out = _extract_image(resp.json())
    if not out:
        # No image part — usually a safety block; deterministic, don't retry.
        raise BridgeEditError("gemini_api: no image in response (safety block?)", attempts=1)
    return out


async def edit_image_variants(
    image_bytes: bytes,
    prompt: str,
    reference_images: Optional[Sequence[bytes]] = None,
    *,
    image_model: str,
    aspect_ratio: str = "IMAGE_ASPECT_RATIO_LANDSCAPE",
    mime: str = "image/png",
    variant_count: int = 1,
    max_attempts: int = MAX_ATTEMPTS,
) -> list[bytes]:
    """Drop-in equivalent of the Flow bridge's ``edit_image_variants`` for
    ``gemini-*`` models. Variants are separate sequential calls (the API yields
    one image per call); 503/"high demand"/transport errors are retried with
    backoff, fatal API errors fail fast. Returns ≥1 image or raises
    ``BridgeEditError``."""
    from flowboard.services.comic.bridge import BridgeEditError

    key = api_key()
    if not key:
        raise BridgeEditError("gemini_api: GEMINI_API_KEY not set in .env", attempts=0)

    body = _build_body(image_bytes, prompt, reference_images, aspect_ratio, mime)
    n = max(1, min(int(variant_count or 1), 4))
    outs: list[bytes] = []
    last = "unknown"
    async with httpx.AsyncClient(timeout=_TIMEOUT_S) as client:
        for _ in range(n):
            for attempt in range(1, max_attempts + 1):
                try:
                    outs.append(await _generate_once(client, image_model, key, body))
                    break
                except BridgeEditError:
                    if outs:
                        # Partial success (some variants already in hand) — a
                        # later variant hitting a block shouldn't void them.
                        logger.warning("gemini_api: variant failed fatally after %d ok", len(outs))
                        return outs
                    raise
                except Exception as exc:  # noqa: BLE001 — 503 / timeouts / transport
                    # httpx timeout exceptions stringify to "" — keep the class
                    # name so the surfaced error is never blank.
                    last = f"{type(exc).__name__}: {exc}"[:200].rstrip(": ")
                    logger.warning("gemini_api attempt %d/%d: %s", attempt, max_attempts, last)
                    if attempt < max_attempts:
                        await asyncio.sleep(_BACKOFF_S * attempt)
            else:
                if outs:
                    return outs
                raise BridgeEditError(f"gemini_api: {last}", attempts=max_attempts)
    logger.info("gemini_api ok: %d/%d variant(s) via %s", len(outs), n, image_model)
    return outs
