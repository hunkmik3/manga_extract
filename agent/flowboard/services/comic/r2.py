"""Cloudflare R2 uploader — gives Atrium a stable PUBLIC url for input images
without exposing the agent via a tunnel.

The agent PUSHES the input image to R2 (S3 PutObject, outbound HTTPS — which is
reliable, unlike an inbound tunnel) and hands Atrium the bucket's public
``r2.dev`` URL. Atrium then fetches the image from R2.

Config (.env, gitignored):
  R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY, R2_BUCKET, R2_ENDPOINT
    (https://<account>.r2.cloudflarestorage.com), R2_PUBLIC_URL
    (the bucket's public base, e.g. https://pub-xxxx.r2.dev)
"""
from __future__ import annotations

import logging
import os
import threading
from pathlib import Path
from typing import Optional

from flowboard.services.comic.transfer_gate import transfer_gate

logger = logging.getLogger(__name__)

# Key prefix inside the bucket — keeps Flow Studio media tidy under one folder.
_PREFIX = "media"

# Per-process cache of keys we've already uploaded, so a character ref reused
# across many generations is pushed to R2 only once.
_uploaded: set[str] = set()
_lock = threading.Lock()
_client = None

_CT = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp"}


def _env(name: str) -> str:
    return os.getenv(name, "").strip()


def is_configured() -> bool:
    return all(
        _env(k)
        for k in ("R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY", "R2_BUCKET", "R2_ENDPOINT", "R2_PUBLIC_URL")
    )


def public_base() -> str:
    return _env("R2_PUBLIC_URL").rstrip("/")


def _get_client():
    global _client
    if _client is None:
        import boto3
        from botocore.config import Config

        _client = boto3.client(
            "s3",
            endpoint_url=_env("R2_ENDPOINT"),
            aws_access_key_id=_env("R2_ACCESS_KEY_ID"),
            aws_secret_access_key=_env("R2_SECRET_ACCESS_KEY"),
            region_name="auto",
            config=Config(signature_version="s3v4", retries={"max_attempts": 3}),
        )
    return _client


def upload_file(path: Path) -> str:
    """Upload a local media file to R2 (idempotent per-process) and return its
    public URL. Synchronous (boto3 + disk read) — call via ``asyncio.to_thread``.
    Raises on failure so the caller can surface it."""
    key = f"{_PREFIX}/{path.name}"
    url = f"{public_base()}/{key}"
    with _lock:
        if key in _uploaded:
            return url
    ct = _CT.get(path.suffix.lower(), "application/octet-stream")
    _get_client().put_object(Bucket=_env("R2_BUCKET"), Key=key, Body=path.read_bytes(), ContentType=ct)
    with _lock:
        _uploaded.add(key)
    logger.info("r2: uploaded %s", key)
    return url


# Max long edge for an Atrium INPUT image. Atrium caps input media at 20MB/file;
# a Pro/4K PNG blows past that. References/sources don't need full resolution, so
# we downscale + JPEG-encode (q90) → a few MB, well under the limit. The local
# original (storage/media) is untouched — only the R2 input copy is shrunk.
_INPUT_MAX_EDGE = 3072


def _encode_input(path) -> Optional[bytes]:
    """Downscale (≤ _INPUT_MAX_EDGE) + JPEG-encode the image for Atrium input.
    Returns None if the file can't be decoded as an image."""
    try:
        import cv2
        import numpy as np

        raw = path.read_bytes()
        img = cv2.imdecode(np.frombuffer(raw, np.uint8), cv2.IMREAD_COLOR)
        if img is None:
            return None
        h, w = img.shape[:2]
        if max(h, w) > _INPUT_MAX_EDGE:
            s = _INPUT_MAX_EDGE / max(h, w)
            img = cv2.resize(img, (round(w * s), round(h * s)), interpolation=cv2.INTER_AREA)
        ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 90])
        return buf.tobytes() if ok else None
    except Exception as exc:  # noqa: BLE001
        logger.warning("r2: input encode failed: %s", exc)
        return None


def _input_key(media_id: str) -> str:
    # Deterministic key (always .jpg after re-encode) so upload/delete agree.
    return f"{_PREFIX}/{media_id}.jpg"


def upload_media(media_id: str) -> Optional[str]:
    """Upload a media id's cached image to R2 as a downscaled JPEG (for Atrium
    input) and return its public URL. Idempotent per-process. Returns None if
    the file isn't cached or can't be encoded."""
    from flowboard.services import media as media_service

    path = media_service.cached_path(media_id)
    if path is None:
        return None
    key = _input_key(media_id)
    url = f"{public_base()}/{key}"
    with _lock:
        if key in _uploaded:
            return url
    data = _encode_input(path)
    if data is None:
        return None
    _get_client().put_object(Bucket=_env("R2_BUCKET"), Key=key, Body=data, ContentType="image/jpeg")
    with _lock:
        _uploaded.add(key)
    logger.info("r2: uploaded %s (%d bytes)", key, len(data))
    return url


# ── Result offload (CDN serving) ────────────────────────────────────────────
# Generated RESULT images (~20MB 4K PNGs) are pushed to R2 once so viewers on
# the public tunnel hostname get them from Cloudflare's CDN instead of
# streaming through this machine's uplink on every view (routes/media.py
# redirects non-local Hosts). Unlike the `media/` INPUT copies above these are
# not auto-deleted — they're the canonical CDN copy. A marker file per media id
# (storing the extension) makes the upload idempotent across restarts.
_RESULT_PREFIX = "results"

from flowboard.config import STORAGE_DIR as _STORAGE_DIR  # noqa: E402

_RESULT_MARK_DIR = _STORAGE_DIR / "r2_results"
_RESULT_MARK_DIR.mkdir(parents=True, exist_ok=True)


def upload_result(media_id: str) -> Optional[str]:
    """Upload a cached media file to R2 under ``results/`` and return its public
    URL. Idempotent (marker file). Returns None when R2 is unconfigured or the
    file isn't cached. Sync (boto3 + disk read) — call via ``asyncio.to_thread``."""
    if not is_configured():
        return None
    from flowboard.services import media as media_service

    path = media_service.cached_path(media_id)
    if path is None:
        return None
    ext = path.suffix.lower()
    key = f"{_RESULT_PREFIX}/{media_id}{ext}"
    url = f"{public_base()}/{key}"
    mark = _RESULT_MARK_DIR / media_id
    if mark.exists():
        return url
    ct = _CT.get(ext, "application/octet-stream")
    # Results can be ~20MB 4K PNGs — gate the actual upload alongside Atrium's
    # downloads and Avis's fetches so concurrent jobs don't saturate the same
    # uplink and starve each other (see transfer_gate's docstring).
    with transfer_gate:
        _get_client().put_object(
            Bucket=_env("R2_BUCKET"), Key=key, Body=path.read_bytes(), ContentType=ct,
            CacheControl="public, max-age=31536000, immutable",  # media ids never change content
        )
    mark.write_text(ext)
    logger.info("r2: result offloaded %s (%d bytes)", key, path.stat().st_size)
    return url


def result_public_url(media_id: str) -> Optional[str]:
    """Public CDN URL for a previously-offloaded result, else None. Cheap
    (marker-file check only, no network)."""
    if not is_configured():
        return None
    mark = _RESULT_MARK_DIR / media_id
    if not mark.exists():
        return None
    try:
        ext = mark.read_text().strip() or ".png"
    except OSError:
        return None
    return f"{public_base()}/{_RESULT_PREFIX}/{media_id}{ext}"


def delete_media(media_id: str) -> None:
    """Remove a previously-uploaded input image from R2. Called right after a
    generation completes — Atrium has already fetched it, so the object only
    ever lives in the bucket for the few seconds of one generation. The local
    cached file (storage/media) is untouched. Best-effort: never raises."""
    key = _input_key(media_id)
    try:
        _get_client().delete_object(Bucket=_env("R2_BUCKET"), Key=key)
        logger.info("r2: deleted %s", key)
    except Exception as exc:  # noqa: BLE001 — cleanup must never break a gen
        logger.warning("r2: delete failed for %s: %s", key, exc)
    finally:
        with _lock:
            _uploaded.discard(key)
