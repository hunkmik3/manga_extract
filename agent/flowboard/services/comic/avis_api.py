"""Avis (multi-provider AI gateway) image engine — routes Seedream 5.0 Pro
through Avis's unified async API. Avis proxies to BytePlus under the hood for
this model, but the account/billing/API surface is Avis's, not BytePlus
directly (an earlier direct-BytePlus-Ark integration was removed 2026-07-30
in favor of this).

Flow (confirmed live against GET /api/v1/ai/models on 2026-07-28):
  POST /api/v1/image/generations/async               -> {"data": {"generationId": "..."}, ...}
  GET  /api/v1/image/generations/async/:generationId  -> poll until
       data.status is "succeeded" or "failed"

IMPORTANT: responses are wrapped in a `{"data": {...}, "success": true,
"status": 200, "timestamp": ...}` envelope — confirmed live; the docs implied
a flat body. Unwrap "data" everywhere.

Auth: ``AVIS_API_KEY`` as an ``x-api-key`` header.
Model: ``dola-seedream-5-0-pro`` — exact catalog id, no version/date suffix
(unlike the old direct-BytePlus id ``dola-seedream-5-0-pro-260628``).

Model capabilities (dola-seedream-5-0-pro, capabilitiesProviderId=byteplus):
  - numberOfImages: max 1 per call — UNLIKE Atrium, Avis does not support
    multi-image-per-request for this model. Each requested "variant" is its
    own async job, run in parallel (same shape as atrium_api.py).
  - resolution: "1K" | "2K" only (no true 4K — matches the old Ark ceiling).
  - ratio: 1:1, 4:3, 3:4, 16:9, 9:16, 3:2, 2:3, 21:9.
  - outputFormat: png | jpeg.
  - up to 10 input/reference images, sent inline as base64.
  - responseFormat "b64_json" was requested but NOT honored in practice —
    results come back as url/downloadUrl only; code below fetches via URL.
"""
from __future__ import annotations

import asyncio
import base64
import logging
import os
import time
from typing import Callable, Optional, Sequence

import httpx

from flowboard.services.comic.transfer_gate import transfer_gate

logger = logging.getLogger(__name__)

_DEFAULT_BASE = "https://api.avis.xyz"
_DEFAULT_MODEL = "dola-seedream-5-0-pro"
_HTTP_TIMEOUT_S = 30.0  # per individual HTTP call (create or one poll) — polling itself is a loop, not one long request
_POLL_INTERVAL_S = 3.0
_POLL_BUDGET_S = 280.0  # a Pro Seedream gen takes ~70-190s observed/per BytePlus; leave headroom
# How many CONSECUTIVE bad poll responses (502/503/504 gateway blips, transport
# hiccups) to ride out before giving up on an in-progress job. Avis's gateway
# occasionally 5xx's mid-generation; the job is still running on their side, so
# abandoning it on the first blip wastes a paid-for generation. ~8 × 3s ≈ 24s.
_POLL_MAX_ERRORS = 8
MAX_ATTEMPTS = 3  # retries the whole create+poll cycle on transient failure
_BACKOFF_S = 2.0
_FATAL_STATUSES = {400, 401, 403, 404}
_RATIOS = {"1:1", "4:3", "3:4", "16:9", "9:16", "3:2", "2:3", "21:9"}
# Reference/source inputs are sent INLINE as base64, so a big (multi-MB 4K PNG)
# input blows past Avis's request-body limit → HTTP 413. Downscale to ≤ this edge
# and JPEG-encode first: 2048 is exactly Seedream's own 2K ceiling, so it's full
# conditioning quality with no practical loss, at a fraction of the bytes.
_INPUT_MAX_EDGE = int(os.getenv("FLOWBOARD_AVIS_INPUT_MAX_EDGE", "2048"))
_INPUT_JPEG_QUALITY = 88


class _AvisPollTimeout(RuntimeError):
    """Job never reached succeeded/failed within the poll budget."""


def base_url() -> str:
    return (os.getenv("AVIS_BASE_URL", "").strip() or _DEFAULT_BASE).rstrip("/")


def api_key() -> Optional[str]:
    k = os.getenv("AVIS_API_KEY", "").strip()
    return k or None


def is_configured() -> bool:
    return api_key() is not None


def _headers() -> dict:
    return {"Content-Type": "application/json", "x-api-key": api_key() or ""}


def _mime_of(data: bytes) -> str:
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return "image/png"


def _encode_input(data: bytes) -> tuple[bytes, str]:
    """Downscale (≤ _INPUT_MAX_EDGE) + JPEG-encode an input image so a large
    reference/source doesn't overflow Avis's request body as inline base64
    (HTTP 413). Falls back to the raw bytes if it can't be decoded as an image.
    Returns (bytes, mime)."""
    try:
        import cv2
        import numpy as np

        img = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
        if img is None:
            return data, _mime_of(data)
        h, w = img.shape[:2]
        if max(h, w) > _INPUT_MAX_EDGE:
            s = _INPUT_MAX_EDGE / max(h, w)
            img = cv2.resize(img, (round(w * s), round(h * s)), interpolation=cv2.INTER_AREA)
        ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, _INPUT_JPEG_QUALITY])
        if ok:
            return buf.tobytes(), "image/jpeg"
    except Exception as exc:  # noqa: BLE001 — never break a gen over input encoding
        logger.warning("avis: input encode failed, sending raw: %s", exc)
    return data, _mime_of(data)


def _image_part(data: bytes) -> dict:
    enc, mime = _encode_input(data)
    return {
        "type": "imageBase64",
        "data": base64.b64encode(enc).decode("ascii"),
        "mediaType": mime,
    }


def _build_body(
    prompt: str,
    images: Optional[Sequence[bytes]],
    model: str,
    aspect_ratio: Optional[str],
    image_size: Optional[str],
) -> dict:
    # Images first, prompt text last — mirrors atrium_api._build_body's
    # convention and the doc's own note that content order is preserved.
    content: list[dict] = [_image_part(b) for b in (images or []) if b]
    content.append({"type": "text", "text": prompt})
    body: dict = {
        "model": model,
        "content": content,
        "numberOfImages": 1,  # this model caps at 1/call regardless of app variant_count
        "outputFormat": "png",
        "responseFormat": "b64_json",
        "watermark": False,
    }
    ratio = (aspect_ratio or "").strip()
    if ratio in _RATIOS:
        body["ratio"] = ratio
    size = (image_size or "").strip().upper()
    if size in ("1K", "2K"):
        body["resolution"] = size
    elif size == "4K":
        # Model caps at 2K; the frontend already disables 4K for this model,
        # so this is a defensive clamp that should never actually trigger.
        body["resolution"] = "2K"
    return body


def _extract_image(result: object) -> tuple[Optional[bytes], Optional[str]]:
    images = result.get("images") if isinstance(result, dict) else None
    if not isinstance(images, list) or not images:
        return None, None
    first = images[0] if isinstance(images[0], dict) else {}
    b64 = first.get("b64")
    if isinstance(b64, str) and b64:
        try:
            return base64.b64decode(b64), None
        except Exception:  # noqa: BLE001
            pass
    url = first.get("downloadUrl") or first.get("url")
    if isinstance(url, str) and url:
        return None, url
    return None, None


def _error_detail(resp: httpx.Response) -> str:
    try:
        j = resp.json()
        if isinstance(j, dict):
            msg = j.get("message") or j.get("error")
            if isinstance(msg, dict):
                msg = msg.get("message")
            if msg:
                return f"{resp.status_code}: {str(msg)[:300]}"
    except Exception:  # noqa: BLE001
        pass
    return f"http_{resp.status_code}"


def _unwrap(payload: object) -> dict:
    """Avis wraps every response as {"data": {...}, "success": true, ...} —
    confirmed live 2026-07-28, contradicting the docs' flat-body examples."""
    if isinstance(payload, dict) and isinstance(payload.get("data"), dict):
        return payload["data"]
    return payload if isinstance(payload, dict) else {}


async def _create_job(client: httpx.AsyncClient, body: dict) -> str:
    """POST .../async -> generationId. BridgeEditError on fatal, RuntimeError on retryable."""
    from flowboard.services.comic.bridge import BridgeEditError

    resp = await client.post(
        f"{base_url()}/api/v1/image/generations/async", headers=_headers(), json=body
    )
    if resp.status_code not in (200, 201, 202):
        detail = _error_detail(resp)
        if resp.status_code in _FATAL_STATUSES:
            raise BridgeEditError(f"avis: {detail}", attempts=1)
        raise RuntimeError(f"avis retryable: {detail}")
    data = _unwrap(resp.json())
    gid = data.get("generationId")
    if not isinstance(gid, str) or not gid:
        raise RuntimeError("avis: no generationId in create response")
    return gid


async def _poll_job(client: httpx.AsyncClient, generation_id: str) -> bytes:
    """GET .../async/:id repeatedly until succeeded/failed. Transient trouble —
    a 502/503/504 gateway blip, a transport hiccup, or a stumble downloading the
    finished image — does NOT abandon the job (it's still running/valid on Avis's
    side, and re-creating would waste a paid-for generation). We tolerate up to
    _POLL_MAX_ERRORS such blips in a row, then give up."""
    from flowboard.services.comic.bridge import BridgeEditError

    deadline = time.monotonic() + _POLL_BUDGET_S
    errors = 0
    while True:
        detail: Optional[str] = None
        try:
            resp = await client.get(
                f"{base_url()}/api/v1/image/generations/async/{generation_id}", headers=_headers()
            )
            if resp.status_code != 200:
                detail = f"poll {_error_detail(resp)}"
            else:
                data = _unwrap(resp.json())
                status = data.get("status")
                if status == "failed":
                    raise BridgeEditError(f"avis: {data.get('error') or 'generation failed'}", attempts=1)
                if status == "succeeded":
                    inline, url = _extract_image(data.get("result") or {})
                    if inline:
                        return inline
                    if not url:
                        raise RuntimeError("avis: succeeded but no image in result")
                    # Gate the actual fetch alongside Atrium's downloads and R2's
                    # uploads (same uplink) — acquire via to_thread so a blocking
                    # threading.Semaphore wait can't stall this async event loop.
                    await asyncio.to_thread(transfer_gate.acquire)
                    try:
                        img = await client.get(url)
                    finally:
                        transfer_gate.release()
                    if img.status_code == 200 and img.content:
                        return img.content
                    detail = f"image fetch http_{img.status_code}"  # transient → re-poll & re-fetch
                # else: still queued/running → not an error, fall through to sleep
        except BridgeEditError:
            raise  # generation genuinely failed — fatal for this variant
        except httpx.HTTPError as exc:
            detail = f"{type(exc).__name__}"

        if detail is not None:
            errors += 1
            if errors > _POLL_MAX_ERRORS:
                raise RuntimeError(f"avis poll: gave up after {errors} consecutive errors ({detail})")
        else:
            errors = 0

        if time.monotonic() > deadline:
            raise _AvisPollTimeout(f"avis: job {generation_id} did not resolve within {_POLL_BUDGET_S:.0f}s")
        await asyncio.sleep(_POLL_INTERVAL_S)


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
    """Text→image (and image-conditioned) via Avis/Seedream. This model allows
    only ONE image per job, so each requested variant is its own async job,
    run IN PARALLEL; each retries transient failures with backoff. Partial
    success wins. Returns >=1 image or raises BridgeEditError."""
    from flowboard.services.comic.bridge import BridgeEditError

    if not is_configured():
        raise BridgeEditError("avis: AVIS_API_KEY not set in .env", attempts=0)

    model = image_model.strip() if isinstance(image_model, str) and image_model.strip() else _DEFAULT_MODEL
    body = _build_body(prompt, images, model, aspect_ratio, image_size)
    n = max(1, min(int(variant_count or 1), 4))
    completed = 0

    async def _one(client: httpx.AsyncClient) -> bytes:
        nonlocal completed
        last = "unknown"
        for attempt in range(1, max_attempts + 1):
            try:
                gid = await _create_job(client, body)
                out = await _poll_job(client, gid)
                completed += 1
                if on_progress:
                    on_progress(completed, n)
                return out
            except BridgeEditError:
                raise  # fatal for this variant
            except Exception as exc:  # noqa: BLE001 — 5xx / timeouts / transport / poll-timeout
                last = f"{type(exc).__name__}: {exc}"[:200].rstrip(": ")
                logger.warning("avis attempt %d/%d: %s", attempt, max_attempts, last)
                if attempt < max_attempts:
                    await asyncio.sleep(_BACKOFF_S * attempt)
        raise BridgeEditError(f"avis: {last}", attempts=max_attempts)

    # A modest PER-CALL timeout (create call, or one poll GET) — the overall
    # multi-minute wait for a job to finish is handled by _poll_job's own
    # wall-clock budget/sleep loop, not by a single long-held HTTP request.
    async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT_S) as client:
        results = await asyncio.gather(*[_one(client) for _ in range(n)], return_exceptions=True)

    outs = [r for r in results if not isinstance(r, BaseException)]
    if outs:
        if len(outs) < n:
            logger.warning("avis: %d/%d variant(s) ok (partial)", len(outs), n)
        else:
            logger.info("avis ok: %d/%d via %s", len(outs), n, model)
        return outs
    first = next((r for r in results if isinstance(r, BaseException)), None)
    if isinstance(first, BridgeEditError):
        raise first
    raise BridgeEditError(f"avis: {first}", attempts=max_attempts)
