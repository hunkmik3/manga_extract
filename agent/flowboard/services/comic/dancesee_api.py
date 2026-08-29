"""DanceSee B2B unmoderated image engine (https://api.dancesee.io).

The manga colorizer's default Seedream backend. Hits the content-filter-disabled
endpoints under ``/api/v1/b2b`` so adult/explicit pages aren't rejected at
BytePlus's default input/output moderation layer.

  POST /api/v1/b2b/image/generations/async   → {data:{generationId}}
  GET  /api/v1/b2b/image/generations/async/:id → poll until succeeded/failed/expired

Same request/response shape as the regular (moderated) Avis async image API;
auth is unchanged (Bearer JWT or ``x-api-key``). The account MUST be
``userType: B2B`` or every call 403s — that flag is a manual, internal BE
action, never self-service.

Auth: ``DANCESEE_API_KEY`` (falls back to ``AVIS_API_KEY``) sent as both
``Authorization: Bearer`` and ``X-Api-Key``.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Callable, Optional, Sequence

import httpx

from flowboard.services.comic.avis_api import (
    MAX_ATTEMPTS,
    SAFETY_MAX_ATTEMPTS,
    _AvisEmpty,
    _BACKOFF_S,
    _FATAL_STATUSES,
    _POLL_INTERVAL_S,
    _POLL_TIMEOUT_S,
    _TIMEOUT_S,
    _build_body,
    _error_detail,
    _extract_image,
    _gid,
    _is_moderation_block,
    size_token,
)
from flowboard.services.comic.avis_api import size_for_dims as size_for_dims  # noqa: F401

logger = logging.getLogger(__name__)

_DEFAULT_BASE = "https://api.dancesee.io"
_DEFAULT_MODEL = "dola-seedream-5-0-pro"
_SUBMIT_PATH = "/api/v1/b2b/image/generations/async"

# Only these have a configured unmoderated inference endpoint. Anything else
# is a client-side 400 (the server would 400 the same way).
IMAGE_MODELS = frozenset({
    "seedream-4-0",
    "seedream-4-5",
    "seedream-5-0",
    "dola-seedream-5-0-pro",
})

# Ark-direct / UI ids → the B2B model slug.
_MODEL_ALIASES = {
    "dola-seedream-5-0-pro-260628": "dola-seedream-5-0-pro",
    "seedream-4.0": "seedream-4-0",
    "seedream-4.5": "seedream-4-5",
    "seedream-5.0": "seedream-5-0",
    "seedream-5": "seedream-5-0",
}


def base_url() -> str:
    return (os.getenv("DANCESEE_BASE_URL", "").strip() or _DEFAULT_BASE).rstrip("/")


def api_key() -> Optional[str]:
    k = os.getenv("DANCESEE_API_KEY", "").strip()
    if k:
        return k
    # Same product family as Avis — a B2B-whitelisted Avis key works here.
    k = os.getenv("AVIS_API_KEY", "").strip()
    return k or None


def is_configured() -> bool:
    return api_key() is not None


def _headers() -> dict:
    k = api_key() or ""
    return {"Authorization": f"Bearer {k}", "X-Api-Key": k}


def resolve_model(image_model: str) -> str:
    """Map a caller model id onto a B2B-supported slug, or raise BridgeEditError."""
    from flowboard.services.comic.bridge import BridgeEditError

    raw = image_model.strip() if isinstance(image_model, str) and image_model.strip() else _DEFAULT_MODEL
    model = _MODEL_ALIASES.get(raw, raw)
    if model not in IMAGE_MODELS:
        raise BridgeEditError(
            f"dancesee: model {raw!r} does not support contentFilterDisabled "
            f"(supported: {', '.join(sorted(IMAGE_MODELS))})",
            attempts=0,
        )
    return model


async def _generate_once(client: httpx.AsyncClient, body: dict) -> bytes:
    from flowboard.services.comic.bridge import BridgeEditError

    submit = f"{base_url()}{_SUBMIT_PATH}"
    resp = await client.post(submit, headers=_headers(), json=body)
    if resp.status_code not in (200, 202):
        detail = _error_detail(resp)
        if resp.status_code in _FATAL_STATUSES:
            raise BridgeEditError(f"dancesee: {detail}", attempts=1)
        raise RuntimeError(f"dancesee retryable: {detail}")
    gid = _gid(resp.json())
    if not gid:
        raise RuntimeError("dancesee: async submit returned no generationId")

    poll_url = f"{submit}/{gid}"
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
                    raise RuntimeError(f"dancesee image fetch http_{img.status_code}")
                return img.content
            raise _AvisEmpty("dancesee: job succeeded but no image")
        if status in ("failed", "expired"):
            err = str(root.get("error") if isinstance(root, dict) else "")[:200]
            if _is_moderation_block(err):
                raise BridgeEditError(f"dancesee: {err}", attempts=1)
            raise RuntimeError(f"dancesee job {status}: {err}")
    raise RuntimeError("dancesee: async poll timed out")


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
    """Text→image (and image-conditioned) via DanceSee B2B Seedream.
    ``images`` are raw bytes sent inline as ``imageBase64`` content parts.
    Variants run IN PARALLEL; each retries with backoff. Returns ≥1 PNG or
    raises BridgeEditError."""
    from flowboard.services.comic.bridge import BridgeEditError

    if not is_configured():
        raise BridgeEditError(
            "dancesee: DANCESEE_API_KEY (or AVIS_API_KEY) not set in .env", attempts=0,
        )
    model = resolve_model(image_model)
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
                logger.warning("dancesee attempt %d/%d: %s", attempt, max_attempts, last)
                if attempt < max_attempts:
                    await asyncio.sleep(_BACKOFF_S * attempt)
        raise BridgeEditError(f"dancesee: {last}", attempts=max_attempts)

    async with httpx.AsyncClient(timeout=_TIMEOUT_S) as client:
        results = await asyncio.gather(*[_one(client) for _ in range(n)], return_exceptions=True)
    outs = [r for r in results if not isinstance(r, BaseException)]
    if outs:
        logger.info("dancesee ok: %d/%d via %s", len(outs), n, model)
        return outs
    first = next((r for r in results if isinstance(r, BaseException)), None)
    if isinstance(first, BridgeEditError):
        raise first
    raise BridgeEditError(f"dancesee: {first}", attempts=max_attempts)
