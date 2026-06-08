"""Comic pipeline ↔ Flow bridge: a bytes-in / bytes-out image-edit seam.

This is a *thin wrapper* over the image-edit path that already ships in
Flowboard — it does NOT touch the relay / WebSocket / login-session machinery
(``flow_client`` + the extension). It only composes three primitives that
already exist:

  1. ``flow_sdk.upload_image``  — raw bytes → a Flow ``media_id``
  2. ``flow_sdk.edit_image``    — (source + optional refs, prompt) → result
                                  ``media_id`` via Nano Banana Pro (GEM_PIX_2)
  3. ``media.ingest_urls`` + ``media.fetch_and_cache`` — result ``media_id`` →
                                  bytes (downloads the signed CDN URL once)

On top of that composition it adds the two things the comic pipeline needs
that the raw ``edit_image`` worker task does not provide:

  * **bounded retry** on the *generate* step — generative output varies and
    the bridge can drop a transient error; uploads happen once and are reused
    across retries.
  * **loud, typed failure** (``BridgeEditError``) so callers (the comic node
    handlers) can flag a panel for human QA / rerun instead of silently
    producing garbage.

Contract notes (mirror the existing ``_handle_edit_image`` worker handler):
  * ``project_id`` is the board's Flow project — see ``routes/projects.py`` /
    the ``BoardFlowProject`` table. Callers resolve it the same way the image
    / video nodes already do (``ensureBoardProject``) and pass it in.
  * ``paygate_tier`` falls back to the live ``flow_client`` tier when omitted.
  * Requires a connected extension + logged-in Flow tab at call time, exactly
    like the existing image/video generation. Must run inside the agent
    process that owns the WebSocket server (not a detached script).
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import logging
from typing import Optional, Sequence

from flowboard.services import media as media_service
from flowboard.services.flow_client import flow_client
from flowboard.services.flow_sdk import get_flow_sdk, is_valid_project_id

logger = logging.getLogger(__name__)

DEFAULT_MAX_ATTEMPTS = 3
RETRY_BACKOFF_S = 1.5
# Comic panels default to portrait; callers (Node 2 9:16, Node 3) override.
DEFAULT_ASPECT_RATIO = "IMAGE_ASPECT_RATIO_LANDSCAPE"

# Upload de-dupe cache: (project_id, sha256(bytes)) → Flow media_id. The same
# bytes (a shared character ref across 4 combine panels, or the same source on a
# re-gen) upload only once per project. Flow media_ids are stable for the
# project's lifetime; a rare stale id just makes the next generate retry/fail,
# which the user can re-run. Bounded so a long session can't grow it unbounded.
_UPLOAD_CACHE: dict[tuple[str, str], str] = {}
_UPLOAD_CACHE_MAX = 2000


class BridgeEditError(RuntimeError):
    """An ``edit_image`` call could not produce a usable image.

    ``reason`` is a short machine-ish string (e.g. the Flow error, or
    ``"paygate_tier_unknown"``); ``attempts`` is how many generate attempts
    were spent before giving up (0 for pre-flight failures like a bad upload).
    """

    def __init__(self, reason: str, *, attempts: int) -> None:
        super().__init__(f"edit_image failed after {attempts} attempt(s): {reason}")
        self.reason = reason
        self.attempts = attempts


async def _upload(
    sdk,
    image_bytes: bytes,
    *,
    project_id: str,
    mime: str,
    file_name: str,
) -> str:
    """Push one image into the Flow project, return its ``media_id``.

    Retries transient failures (e.g. ``extension_disconnected`` while the
    extension is reconnecting after an agent reload) with backoff. Raises
    ``BridgeEditError`` (attempts=0) only after exhausting retries.
    """
    cache_key = (project_id, hashlib.sha256(bytes(image_bytes)).hexdigest())
    cached = _UPLOAD_CACHE.get(cache_key)
    if cached is not None:
        return cached

    b64 = base64.b64encode(image_bytes).decode("ascii")
    last = "unknown"
    for attempt in range(1, DEFAULT_MAX_ATTEMPTS + 1):
        try:
            resp = await sdk.upload_image(
                image_base64=b64, mime_type=mime, project_id=project_id, file_name=file_name,
            )
        except Exception as exc:  # noqa: BLE001 — transport errors are retryable
            last = f"upload_raised: {exc}"
            logger.warning("upload attempt %d/%d raised: %s", attempt, DEFAULT_MAX_ATTEMPTS, exc)
            await _backoff(attempt, DEFAULT_MAX_ATTEMPTS)
            continue
        if isinstance(resp, dict) and resp.get("error"):
            last = f"upload_failed: {str(resp['error'])[:200]}"
            logger.warning("upload attempt %d/%d error: %s", attempt, DEFAULT_MAX_ATTEMPTS, last)
            await _backoff(attempt, DEFAULT_MAX_ATTEMPTS)
            continue
        media_id = resp.get("media_id") if isinstance(resp, dict) else None
        if isinstance(media_id, str) and media_service.is_valid_media_id(media_id):
            if len(_UPLOAD_CACHE) >= _UPLOAD_CACHE_MAX:
                _UPLOAD_CACHE.clear()
            _UPLOAD_CACHE[cache_key] = media_id
            return media_id
        last = "upload returned no valid media_id"
        await _backoff(attempt, DEFAULT_MAX_ATTEMPTS)
    raise BridgeEditError(last, attempts=0)


async def edit_image(
    image_bytes: bytes,
    prompt: str,
    reference_images: Optional[Sequence[bytes]] = None,
    *,
    project_id: str,
    aspect_ratio: str = DEFAULT_ASPECT_RATIO,
    image_model: Optional[str] = None,
    paygate_tier: Optional[str] = None,
    mime: str = "image/png",
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
) -> bytes:
    """Edit one image through the Flow bridge and return the result bytes.

    Thin wrapper over :func:`edit_image_variants` for the common single-output
    case — returns the first (only) variant's bytes.
    """
    outs = await edit_image_variants(
        image_bytes, prompt, reference_images,
        project_id=project_id, aspect_ratio=aspect_ratio, image_model=image_model,
        paygate_tier=paygate_tier, mime=mime, max_attempts=max_attempts, variant_count=1,
    )
    return outs[0]


async def edit_image_variants(
    image_bytes: bytes,
    prompt: str,
    reference_images: Optional[Sequence[bytes]] = None,
    *,
    project_id: str,
    aspect_ratio: str = DEFAULT_ASPECT_RATIO,
    image_model: Optional[str] = None,
    paygate_tier: Optional[str] = None,
    mime: str = "image/png",
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    variant_count: int = 1,
) -> list[bytes]:
    """Edit one image and return up to ``variant_count`` result candidates.

    Uploads ``image_bytes`` (and each of ``reference_images``) into the board's
    Flow project once, then runs Nano Banana Pro with ``variant_count`` (1-4)
    replicated seeds — the "x4" on the Flow UI — and downloads every candidate
    back. The generate step is retried up to ``max_attempts`` times (uploads are
    reused). Returns the candidates that downloaded (≥1); raises
    ``BridgeEditError`` if none come back. The order of inputs to Flow is
    BASE_IMAGE (``image_bytes``) first, then the references.
    """
    if not isinstance(image_bytes, (bytes, bytearray)) or len(image_bytes) == 0:
        raise ValueError("image_bytes must be non-empty bytes")
    if not isinstance(prompt, str) or not prompt.strip():
        raise ValueError("prompt must be a non-empty string")
    if not is_valid_project_id(project_id):
        raise ValueError(f"invalid project_id: {project_id!r}")
    if max_attempts < 1:
        raise ValueError("max_attempts must be >= 1")

    tier = paygate_tier or flow_client.paygate_tier
    if tier is None:
        # Same failure the worker handler raises — the extension hasn't sniffed
        # a Flow subscription tier yet (no logged-in Flow tab / token).
        raise BridgeEditError("paygate_tier_unknown", attempts=0)

    sdk = get_flow_sdk()

    # Uploads are stable across generate retries → do them once, and in parallel
    # (source + every ref at once). _upload de-dupes identical bytes via cache.
    refs_to_upload = [(i, bytes(ref)) for i, ref in enumerate(reference_images or []) if ref]
    uploaded = await asyncio.gather(
        _upload(sdk, bytes(image_bytes), project_id=project_id, mime=mime, file_name="comic_source.png"),
        *[
            _upload(sdk, rb, project_id=project_id, mime=mime, file_name=f"comic_ref_{i}.png")
            for i, rb in refs_to_upload
        ],
    )
    source_media_id = uploaded[0]
    ref_media_ids: list[str] = list(uploaded[1:])

    last_reason = "unknown"
    for attempt in range(1, max_attempts + 1):
        try:
            resp = await sdk.edit_image(
                prompt=prompt.strip(),
                project_id=project_id,
                source_media_id=source_media_id,
                ref_media_ids=ref_media_ids or None,
                aspect_ratio=aspect_ratio,
                paygate_tier=tier,
                image_model=image_model,
                variant_count=variant_count,
            )
        except Exception as exc:  # noqa: BLE001 — transport/bridge errors are retryable
            last_reason = f"sdk_raised: {exc}"
            logger.warning("edit_image attempt %d/%d raised: %s", attempt, max_attempts, exc)
            await _backoff(attempt, max_attempts)
            continue

        if not isinstance(resp, dict) or resp.get("error"):
            last_reason = str(resp.get("error") if isinstance(resp, dict) else resp)[:200]
            logger.warning("edit_image attempt %d/%d error: %s", attempt, max_attempts, last_reason)
            await _backoff(attempt, max_attempts)
            continue

        entries = [
            e for e in (resp.get("media_entries") or [])
            if isinstance(e, dict) and e.get("url") and e.get("media_id")
        ]
        if not entries:
            last_reason = "no_media_entries_with_url"
            logger.warning("edit_image attempt %d/%d: %s", attempt, max_attempts, last_reason)
            await _backoff(attempt, max_attempts)
            continue

        # Persist the signed URL(s) so fetch_and_cache can download them.
        try:
            media_service.ingest_urls(entries)
        except Exception:  # noqa: BLE001 — caching bookkeeping must not abort the edit
            logger.exception("ingest_urls failed (continuing to fetch result)")

        outs: list[bytes] = []
        for e in entries:
            fetched = await media_service.fetch_and_cache(e["media_id"])
            if fetched is not None:
                outs.append(fetched[0])
        if not outs:
            last_reason = f"fetch_failed: {entries[0]['media_id']}"
            logger.warning("edit_image attempt %d/%d: %s", attempt, max_attempts, last_reason)
            await _backoff(attempt, max_attempts)
            continue

        logger.info(
            "edit_image ok on attempt %d/%d: %d/%d variant(s) (%d bytes first)",
            attempt, max_attempts, len(outs), len(entries), len(outs[0]),
        )
        return outs

    raise BridgeEditError(last_reason, attempts=max_attempts)


async def _backoff(attempt: int, max_attempts: int) -> None:
    """Linear backoff between generate retries; no sleep after the last try."""
    if attempt < max_attempts:
        await asyncio.sleep(RETRY_BACKOFF_S * attempt)
