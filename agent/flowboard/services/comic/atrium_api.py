"""Atrium partner-API image engine — a third backend alongside the Flow bridge
and the direct Gemini API.

Atrium (https://studio.atrium.art) is a thin passthrough to Google's image
models. Two things differ from the direct Gemini engine:

  1. **Input media is by URL.** Reference/source images are sent as
     ``fileData.fileUri`` (a public http(s) URL Atrium downloads server-side).
     Raw inline base64 is rejected with 400. So the worker must hand us PUBLIC
     URLs — see ``public_media_base`` / ``PUBLIC_MEDIA_BASE_URL`` (the tunnel).
  2. **Output is a downloadUrl.** Each generated image part carries a
     ``downloadUrl`` (valid 24h) instead of inline bytes; we fetch it to get
     the PNG bytes, then the worker caches them locally like any other media.

Auth: ``ATRIUM_CLIENT_ID`` / ``ATRIUM_CLIENT_SECRET`` headers (in .env).
Models: ``gemini-3-pro-image`` / ``gemini-2.5-flash-image`` / ``gemini-3.1-flash-image``.
Limit: 1000 images / 24h per client.
"""
from __future__ import annotations

import asyncio
import logging
import os
from typing import Callable, Optional, Sequence

import httpx

logger = logging.getLogger(__name__)

_DEFAULT_BASE = "https://studio.atrium.art"
_TIMEOUT_S = 240.0
MAX_ATTEMPTS = 5
_BACKOFF_S = 2.0

# Fail fast only on errors retrying can't fix: bad request, auth, not-found.
# 429 is retried — Atrium confirmed it's an intermittent Nano Banana / Veo model
# error (NOT an Atrium rate limit; they have no per-second/concurrent limits,
# only a daily quota) that usually clears after one or more retries.
_FATAL_STATUSES = {400, 401, 403, 404}


def base_url() -> str:
    return (os.getenv("ATRIUM_BASE_URL", "").strip() or _DEFAULT_BASE).rstrip("/")


def client_creds() -> Optional[tuple[str, str]]:
    cid = os.getenv("ATRIUM_CLIENT_ID", "").strip()
    secret = os.getenv("ATRIUM_CLIENT_SECRET", "").strip()
    if cid and secret:
        return cid, secret
    return None


def is_configured() -> bool:
    return client_creds() is not None


def public_media_base() -> Optional[str]:
    """Public base URL the agent's ``/media/<id>`` route is reachable at (e.g. a
    tunnel). Required for reference/source images on Atrium — without it we can
    only do pure text→image. Returns None when unset."""
    v = os.getenv("PUBLIC_MEDIA_BASE_URL", "").strip()
    return v.rstrip("/") or None


def media_public_url(media_id: str) -> Optional[str]:
    base = public_media_base()
    return f"{base}/media/{media_id}" if base else None


def _mime_for(url: str) -> str:
    low = url.lower().split("?", 1)[0]
    if low.endswith(".jpg") or low.endswith(".jpeg"):
        return "image/jpeg"
    if low.endswith(".webp"):
        return "image/webp"
    return "image/png"


def _build_body(
    prompt: str,
    image_urls: Optional[Sequence[str]],
    image_model: str,
    aspect_ratio: Optional[str],
    image_size: Optional[str],
) -> dict:
    contents: list[dict] = []
    for url in image_urls or []:
        if url:
            contents.append({"fileData": {"mimeType": _mime_for(url), "fileUri": url}})
    contents.append({"text": prompt})
    body: dict = {
        "model": image_model,
        "contents": contents,
        "config": {"responseModalities": ["IMAGE"]},
    }
    image_config: dict = {}
    if aspect_ratio:
        image_config["aspectRatio"] = aspect_ratio
    if image_size:
        image_config["imageSize"] = image_size
    if image_config:
        body["config"]["imageConfig"] = image_config
    return body


def _extract_download_url(payload: dict) -> Optional[str]:
    for cand in payload.get("candidates") or []:
        for part in (cand.get("content") or {}).get("parts") or []:
            du = part.get("downloadUrl") or part.get("download_url")
            if isinstance(du, str) and du:
                return du
    return None


async def _generate_once(client: httpx.AsyncClient, headers: dict, body: dict) -> bytes:
    """One /image/generate call → image bytes (fetched from the downloadUrl).
    Raises BridgeEditError on a fatal API response; RuntimeError on retryable."""
    from flowboard.services.comic.bridge import BridgeEditError

    resp = await client.post(
        f"{base_url()}/api/partner/image/generate", headers=headers, json=body
    )
    if resp.status_code != 200:
        try:
            j = resp.json()
            err = j.get("error", {}) if isinstance(j, dict) else {}
            # Model errors use error.message; Atrium WRAPPER errors (e.g. "Failed
            # to fetch media URL", rate limit) put it at the TOP level.
            msg = err.get("message") or (j.get("message") if isinstance(j, dict) else "") or ""
            detail = f"{err.get('status', resp.status_code)}: {str(msg)[:300]}"
        except Exception:  # noqa: BLE001
            detail = f"http_{resp.status_code}"
        if resp.status_code in _FATAL_STATUSES:
            raise BridgeEditError(f"atrium: {detail}", attempts=1)
        raise RuntimeError(f"atrium retryable: {detail}")

    url = _extract_download_url(resp.json())
    if not url:
        raise BridgeEditError("atrium: no image in response (safety block?)", attempts=1)
    # The downloadUrl is a presigned S3 link — fetch with no auth headers.
    img = await client.get(url)
    if img.status_code != 200 or not img.content:
        raise RuntimeError(f"atrium downloadUrl fetch http_{img.status_code}")
    return img.content


async def generate_image_variants(
    prompt: str,
    image_urls: Optional[Sequence[str]] = None,
    *,
    image_model: str,
    aspect_ratio: str = "1:1",
    variant_count: int = 1,
    image_size: Optional[str] = None,
    max_attempts: int = MAX_ATTEMPTS,
    on_progress: Optional[Callable[[int, int], None]] = None,
) -> list[bytes]:
    """Text→image (and image-conditioned) generation via Atrium. ``image_urls``
    are PUBLIC urls for source/reference frames (empty for plain text→image).
    Variants run IN PARALLEL (Atrium has no per-second/concurrent limit — only a
    daily quota); each retries 5xx/timeouts/429 with backoff, fatal API errors
    fail that variant. Partial success wins. ``on_progress(done, total)`` fires
    as each variant completes. Returns ≥1 image or raises BridgeEditError."""
    from flowboard.services.comic.bridge import BridgeEditError

    creds = client_creds()
    if not creds:
        raise BridgeEditError("atrium: ATRIUM_CLIENT_ID/ATRIUM_CLIENT_SECRET not set in .env", attempts=0)
    headers = {"x-client-id": creds[0], "x-client-secret": creds[1]}

    body = _build_body(prompt, image_urls, image_model, aspect_ratio, image_size)
    n = max(1, min(int(variant_count or 1), 4))
    completed = 0

    async def _one(client: httpx.AsyncClient) -> bytes:
        nonlocal completed
        last = "unknown"
        for attempt in range(1, max_attempts + 1):
            try:
                out = await _generate_once(client, headers, body)
                completed += 1  # asyncio single-threaded → no lock needed
                if on_progress:
                    on_progress(completed, n)
                return out
            except BridgeEditError:
                raise  # fatal for this variant
            except Exception as exc:  # noqa: BLE001 — 5xx / 429 / timeouts / transport
                last = f"{type(exc).__name__}: {exc}"[:200].rstrip(": ")
                logger.warning("atrium attempt %d/%d: %s", attempt, max_attempts, last)
                if attempt < max_attempts:
                    await asyncio.sleep(_BACKOFF_S * attempt)
        raise BridgeEditError(f"atrium: {last}", attempts=max_attempts)

    async with httpx.AsyncClient(timeout=_TIMEOUT_S) as client:
        results = await asyncio.gather(*[_one(client) for _ in range(n)], return_exceptions=True)

    outs = [r for r in results if not isinstance(r, BaseException)]
    if outs:
        if len(outs) < n:
            logger.warning("atrium: %d/%d variant(s) ok (partial)", len(outs), n)
        else:
            logger.info("atrium ok: %d/%d via %s", len(outs), n, image_model)
        return outs
    first = next((r for r in results if isinstance(r, BaseException)), None)
    if isinstance(first, BridgeEditError):
        raise first
    raise BridgeEditError(f"atrium: {first}", attempts=max_attempts)
