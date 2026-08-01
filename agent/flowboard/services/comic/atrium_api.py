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
import threading
import time
from typing import Callable, Optional, Sequence

import httpx

from flowboard.services.comic.transfer_gate import transfer_gate

logger = logging.getLogger(__name__)

_DEFAULT_BASE = "https://studio.atrium.art"
_TIMEOUT_S = 240.0
# Fetching the generated image from its presigned S3 downloadUrl (~20MB for a
# 4K PNG) is the real bottleneck. The key fact — confirmed on the same machine
# and network as the working Mac client — is that S3 throttles PER CONNECTION
# (per TCP 4-tuple), NOT per IP: at the same instant one connection runs at
# multiple MB/s while another crawls at tens of KB/s. So a slow download isn't
# "the network is slow" — this particular connection lost the lottery, and a
# FRESH connection (new source port → new 4-tuple) is very likely to be fast.
#
# Hence a transfer-RATE watchdog (a client-side tail-latency / hedging pattern):
# stream the file while timing the average rate; after a short grace for TCP
# slow-start, if the rate is under the floor, abandon THIS connection at once and
# re-draw a fresh one rather than wait out a ~10-minute crawl. A rate FLOOR (not
# a hard timeout) is deliberate: a hard timeout would wrongly kill a legitimately
# large 4K download moving at a fine-but-moderate speed, whereas a rate check
# tells "throttled" apart from "just a big file".
#
# We keep re-drawing fresh connections for up to _DOWNLOAD_BUDGET_S. A fresh draw
# is cheap (~6s for a fast one) and free (no generation quota), and most draws
# are un-throttled, so the download almost always lands on the first or second
# try. If the whole budget elapses without a completed download, the image is
# FAILED outright — NOT re-generated: re-generating wouldn't help (the throttle
# is per-connection, not per-image), and undownloadable images are rare enough
# that failing fast (the user just re-runs, drawing fresh connections again)
# beats hanging for minutes or burning quota on pointless re-gens.
_DOWNLOAD_BUDGET_S = float(os.getenv("FLOWBOARD_ATRIUM_DOWNLOAD_BUDGET_S", "90"))  # keep re-drawing this long, then FAIL the image
_DOWNLOAD_GRACE_S = 8.0            # let TCP slow-start ramp before judging the rate
_DOWNLOAD_MIN_RATE = 300 * 1024   # bytes/s — below this after grace ⇒ throttled connection, re-draw
_DOWNLOAD_READ_TIMEOUT_S = 15.0   # a DEAD (0-byte) connection redraws after this
_DOWNLOAD_ATTEMPT_CAP_S = 120.0   # ceiling for one accepted (≥floor) connection to finish
MAX_ATTEMPTS = 5
# An empty (no-image) response is usually a safety filter — often a FALSE
# POSITIVE on anime art that clears on retry. Retry a few times only (a genuine
# block won't pass and each try costs quota).
SAFETY_MAX_ATTEMPTS = 3
_BACKOFF_S = 2.0


class _AtriumSafetyEmpty(RuntimeError):
    """200 OK but no image — likely a (often transient) safety block."""


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


def media_input_url(media_id: str) -> Optional[str]:
    """Self-hosted input URL for Atrium — a downscaled JPEG thumbnail served
    straight off this machine through the tunnel, no R2 round-trip. w=2048/q90
    mirrors what the old R2 input path uploaded (≤3072px JPEG), so Atrium fetches
    a small file (~hundreds of KB), not a multi-MB original. The ``/thumb`` route
    never 302-redirects to the R2 CDN (unlike bare ``/media/<id>``), so Atrium
    always gets the bytes directly from this box."""
    base = public_media_base()
    return f"{base}/api/media/{media_id}/thumb?w=2048" if base else None


def _mime_for(url: str) -> str:
    low = url.lower().split("?", 1)[0]
    # Our self-hosted input URL (…/thumb) always serves JPEG.
    if low.endswith("/thumb") or low.endswith(".jpg") or low.endswith(".jpeg"):
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


def _download_watchdog(url: str) -> bytes:
    """Stream the whole file on ONE FRESH connection, watchdogging the average
    transfer rate. A fresh ``httpx.Client`` per call guarantees a new TCP 4-tuple
    (not a pooled/reused socket that may still be throttled). After
    ``_DOWNLOAD_GRACE_S`` (TCP slow-start headroom), if the average rate is under
    ``_DOWNLOAD_MIN_RATE`` the connection is abandoned so the caller can re-draw.
    A short read timeout drops a DEAD (0-byte) connection — which the rate check
    can't see, as it only runs once bytes arrive — so it's re-drawn quickly."""
    timeout = httpx.Timeout(connect=10.0, read=_DOWNLOAD_READ_TIMEOUT_S, write=10.0, pool=10.0)
    with transfer_gate:
        with httpx.Client(timeout=timeout) as c:
            with c.stream("GET", url) as r:
                if r.status_code != 200:
                    raise RuntimeError(f"atrium downloadUrl fetch http_{r.status_code}")
                buf = bytearray()
                start = time.monotonic()
                for chunk in r.iter_bytes(1 << 16):
                    buf.extend(chunk)
                    elapsed = time.monotonic() - start
                    if elapsed > _DOWNLOAD_ATTEMPT_CAP_S:
                        raise TimeoutError(f"download exceeded {_DOWNLOAD_ATTEMPT_CAP_S:.0f}s ({len(buf)} bytes)")
                    if elapsed > _DOWNLOAD_GRACE_S:
                        rate = len(buf) / elapsed
                        if rate < _DOWNLOAD_MIN_RATE:
                            raise TimeoutError(
                                f"throttled connection: {rate / 1024:.0f} KB/s after "
                                f"{elapsed:.0f}s ({len(buf)} bytes) — redrawing"
                            )
    if not buf:
        raise RuntimeError("atrium downloadUrl fetch: empty body")
    return bytes(buf)


def _fetch_download(client: httpx.Client, url: str) -> bytes:
    """Fetch a presigned S3 downloadUrl by re-drawing fresh connections under a
    rate watchdog until one completes, for up to ``_DOWNLOAD_BUDGET_S`` total. If
    the budget elapses without a completed download, raise ``BridgeEditError`` so
    this image FAILS cleanly — no wasteful re-generate (the throttle is
    per-connection, not per-image, so a fresh gen wouldn't download any faster).

    ``client`` is intentionally unused — downloads use a fresh client per attempt
    to force a new TCP 4-tuple — but kept in the signature for call-site symmetry."""
    from flowboard.services.comic.bridge import BridgeEditError

    deadline = time.monotonic() + _DOWNLOAD_BUDGET_S
    last_exc: Optional[BaseException] = None
    dl_try = 0
    while time.monotonic() < deadline:
        dl_try += 1
        try:
            return _download_watchdog(url)
        except (httpx.HTTPError, TimeoutError, RuntimeError) as exc:
            last_exc = exc
            logger.warning("atrium downloadUrl slow/failed (draw %d): %r", dl_try, exc)
    # Budget spent without a good connection — genuinely undownloadable right now.
    raise BridgeEditError(
        f"atrium: image download did not complete within {_DOWNLOAD_BUDGET_S:.0f}s "
        f"after {dl_try} fresh connections ({last_exc!r})",
        attempts=dl_try,
    )


def _generate_once(client: httpx.Client, headers: dict, body: dict) -> bytes:
    """One /image/generate call → image bytes (fetched from the downloadUrl).
    Raises BridgeEditError on a fatal API response; RuntimeError on retryable.
    SYNC on purpose — runs in a worker thread (see generate_image_variants):
    Windows' asyncio proactor loop can lose socket events under load, leaving
    an async POST/GET awaiting forever on a connection the peer already closed
    (CLOSE_WAIT, 0 B/s, no timeout ever fires). Blocking sockets in a thread
    use OS-level timeouts that always fire."""
    from flowboard.services.comic.bridge import BridgeEditError

    resp = client.post(
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
        # "Failed to fetch media URL" is Atrium's OWN upstream fetch of our R2
        # input url failing — empirically transient (the same still-live R2
        # object fetches fine seconds later), not a real bad-request on our
        # end, so retry it despite the 400/404 status instead of failing fast.
        if resp.status_code in _FATAL_STATUSES and "fetch media url" not in detail.lower():
            raise BridgeEditError(f"atrium: {detail}", attempts=1)
        raise RuntimeError(f"atrium retryable: {detail}")

    url = _extract_download_url(resp.json())
    if not url:
        # Retryable: empty responses are frequently a transient false-positive
        # safety block on anime art (see SAFETY_MAX_ATTEMPTS).
        raise _AtriumSafetyEmpty("atrium: no image in response (safety block?)")
    return _fetch_download(client, url)


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
    completed_lock = threading.Lock()

    def _one_sync() -> bytes:
        # Whole variant (POST + download + retries) runs in a worker thread with
        # a sync client — immune to the Windows proactor event-loss hang (see
        # _generate_once). One client per variant: setup cost is trivial next to
        # a 30-60s generation, and no pooled connection can be a shared zombie.
        nonlocal completed
        last = "unknown"
        safety_tries = 0
        with httpx.Client(timeout=_TIMEOUT_S) as client:
            for attempt in range(1, max_attempts + 1):
                try:
                    out = _generate_once(client, headers, body)
                    with completed_lock:
                        completed += 1
                        done_now = completed
                    if on_progress:
                        on_progress(done_now, n)
                    return out
                except BridgeEditError:
                    raise  # fatal for this variant
                except _AtriumSafetyEmpty as exc:
                    safety_tries += 1
                    last = str(exc)[:200]
                    logger.warning("atrium safety-empty %d/%d", safety_tries, SAFETY_MAX_ATTEMPTS)
                    if safety_tries >= SAFETY_MAX_ATTEMPTS:
                        raise BridgeEditError(
                            f"{last} — blocked after {safety_tries} tries", attempts=safety_tries
                        )
                    time.sleep(_BACKOFF_S * attempt)
                except Exception as exc:  # noqa: BLE001 — 5xx / 429 / timeouts / transport
                    last = f"{type(exc).__name__}: {exc}"[:200].rstrip(": ")
                    logger.warning("atrium attempt %d/%d: %s", attempt, max_attempts, last)
                    if attempt < max_attempts:
                        time.sleep(_BACKOFF_S * attempt)
        raise BridgeEditError(f"atrium: {last}", attempts=max_attempts)

    results = await asyncio.gather(
        *[asyncio.to_thread(_one_sync) for _ in range(n)], return_exceptions=True
    )

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
