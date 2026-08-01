"""Tests for the Atrium image engine + provider routing in flow_gen_image.

Atrium specifics under test: x-client-id/secret headers, native generateContent
body with config.imageConfig, downloadUrl→bytes fetch, and the worker guard
that blocks reference/edit without a public media URL."""

import uuid
from unittest.mock import AsyncMock, patch

import cv2
import httpx
import numpy as np
import pytest

from flowboard.services import media as media_service
from flowboard.services.comic import atrium_api
from flowboard.worker.processor import _handle_flow_gen_image


def _png(w=80, h=80, v=120) -> bytes:
    return cv2.imencode(".png", np.full((h, w, 3), v, np.uint8))[1].tobytes()


_R2_ENVS = (
    "R2_ACCOUNT_ID", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY",
    "R2_BUCKET", "R2_ENDPOINT", "R2_PUBLIC_URL",
)


def _clear_public_urls(monkeypatch):
    """Drop every public-URL mechanism (tunnel + R2) so the 'no public url'
    branches can be exercised even when the dev .env has R2 configured."""
    monkeypatch.delenv("PUBLIC_MEDIA_BASE_URL", raising=False)
    for k in _R2_ENVS:
        monkeypatch.delenv(k, raising=False)


def _set_r2(monkeypatch):
    monkeypatch.setenv("R2_ACCESS_KEY_ID", "k")
    monkeypatch.setenv("R2_SECRET_ACCESS_KEY", "s")
    monkeypatch.setenv("R2_BUCKET", "b")
    monkeypatch.setenv("R2_ENDPOINT", "https://acct.r2.cloudflarestorage.com")
    monkeypatch.setenv("R2_PUBLIC_URL", "https://pub-x.r2.dev")


@pytest.mark.asyncio
async def test_atrium_generate_sends_creds_and_fetches_downloadurl(monkeypatch):
    monkeypatch.setenv("ATRIUM_CLIENT_ID", "MQPKXXXX")
    monkeypatch.setenv("ATRIUM_CLIENT_SECRET", "secret123")
    captured = {}

    async def fake_post(self, url, headers=None, json=None):
        captured["url"] = url
        captured["headers"] = headers
        captured["body"] = json
        return httpx.Response(
            200,
            json={"candidates": [{"content": {"parts": [{"downloadUrl": "https://s3/img.png"}]}}]},
        )

    async def fake_get(self, url):
        captured["fetched"] = url
        return httpx.Response(200, content=_png())

    with patch.object(httpx.AsyncClient, "post", fake_post), patch.object(httpx.AsyncClient, "get", fake_get):
        outs = await atrium_api.generate_image_variants(
            "a duck", None, image_model="gemini-3-pro-image", aspect_ratio="16:9", image_size="4K"
        )

    assert len(outs) == 1 and outs[0]
    assert captured["url"].endswith("/api/partner/image/generate")
    assert captured["headers"]["x-client-id"] == "MQPKXXXX"
    assert captured["headers"]["x-client-secret"] == "secret123"
    assert captured["body"]["model"] == "gemini-3-pro-image"
    assert captured["body"]["config"]["imageConfig"] == {"aspectRatio": "16:9", "imageSize": "4K"}
    assert captured["fetched"] == "https://s3/img.png"


@pytest.mark.asyncio
async def test_atrium_429_retries_then_succeeds(monkeypatch):
    # Atrium confirmed 429 = intermittent Nano Banana error → retry succeeds.
    monkeypatch.setenv("ATRIUM_CLIENT_ID", "id")
    monkeypatch.setenv("ATRIUM_CLIENT_SECRET", "sec")
    calls = {"post": 0}

    async def fake_post(self, url, headers=None, json=None):
        calls["post"] += 1
        if calls["post"] == 1:
            return httpx.Response(429, json={"error": {"status": "RESOURCE_EXHAUSTED", "message": "model busy"}})
        return httpx.Response(
            200, json={"candidates": [{"content": {"parts": [{"downloadUrl": "https://s3/x.png"}]}}]}
        )

    async def fake_get(self, url):
        return httpx.Response(200, content=_png())

    with patch.object(httpx.AsyncClient, "post", fake_post), \
         patch.object(httpx.AsyncClient, "get", fake_get), \
         patch("flowboard.services.comic.atrium_api.asyncio.sleep", new=AsyncMock()):
        outs = await atrium_api.generate_image_variants("x", None, image_model="gemini-2.5-flash-image")

    assert len(outs) == 1 and outs[0]
    assert calls["post"] == 2  # one 429, one success


@pytest.mark.asyncio
async def test_atrium_passes_reference_urls(monkeypatch):
    monkeypatch.setenv("ATRIUM_CLIENT_ID", "id")
    monkeypatch.setenv("ATRIUM_CLIENT_SECRET", "sec")
    captured = {}

    async def fake_post(self, url, headers=None, json=None):
        captured["body"] = json
        return httpx.Response(
            200, json={"candidates": [{"content": {"parts": [{"downloadUrl": "https://s3/x.png"}]}}]}
        )

    async def fake_get(self, url):
        return httpx.Response(200, content=_png())

    with patch.object(httpx.AsyncClient, "post", fake_post), patch.object(httpx.AsyncClient, "get", fake_get):
        await atrium_api.generate_image_variants(
            "scene", ["https://pub/media/abc"], image_model="gemini-2.5-flash-image"
        )

    parts = captured["body"]["contents"]
    assert parts[0]["fileData"]["fileUri"] == "https://pub/media/abc"
    assert parts[-1]["text"] == "scene"


@pytest.mark.asyncio
async def test_flow_gen_routes_to_atrium(monkeypatch):
    monkeypatch.setenv("ATRIUM_CLIENT_ID", "id")
    monkeypatch.setenv("ATRIUM_CLIENT_SECRET", "sec")

    async def fake_gen(prompt, urls, **kw):
        return [_png(), _png()]

    with patch("flowboard.services.comic.atrium_api.generate_image_variants", side_effect=fake_gen):
        result, err = await _handle_flow_gen_image(
            {"prompt": "hello", "provider": "atrium", "variant_count": 2}
        )

    assert err is None
    assert len(result["media_ids"]) == 2
    assert all(media_service.cached_path(m) is not None for m in result["media_ids"])


@pytest.mark.asyncio
async def test_flow_gen_atrium_refs_fall_back_to_gemini_locally(monkeypatch):
    # Atrium + refs + no public URL, but a Gemini key IS present → the request
    # transparently runs on Gemini (inline bytes) and reports provider_used.
    monkeypatch.setenv("ATRIUM_CLIENT_ID", "id")
    monkeypatch.setenv("ATRIUM_CLIENT_SECRET", "sec")
    monkeypatch.setenv("GEMINI_API_KEY", "testkey")
    _clear_public_urls(monkeypatch)
    ref = str(uuid.uuid4())
    media_service.ingest_inline_bytes(ref, _png(), kind="image", mime="image/png")

    async def fake_gen(prompt, refs, **kw):
        return [_png()]

    with patch("flowboard.services.comic.gemini_api.generate_image_variants", side_effect=fake_gen):
        result, err = await _handle_flow_gen_image(
            {"prompt": "with ref", "provider": "atrium", "ref_media_ids": [ref]}
        )
    assert err is None
    assert result["provider_used"] == "gemini"
    assert len(result["media_ids"]) == 1


@pytest.mark.asyncio
async def test_flow_gen_atrium_uses_r2_for_refs(monkeypatch):
    # Atrium + refs + R2 configured → uploads to R2 and passes the r2.dev URL to
    # Atrium (no Gemini fallback, no tunnel).
    monkeypatch.setenv("ATRIUM_CLIENT_ID", "id")
    monkeypatch.setenv("ATRIUM_CLIENT_SECRET", "sec")
    _set_r2(monkeypatch)
    monkeypatch.delenv("PUBLIC_MEDIA_BASE_URL", raising=False)
    ref = str(uuid.uuid4())
    media_service.ingest_inline_bytes(ref, _png(), kind="image", mime="image/png")
    captured = {}
    deleted = []

    def fake_upload(media_id):
        return f"https://pub-x.r2.dev/media/{media_id}.png"

    async def fake_gen(prompt, urls, **kw):
        captured["urls"] = urls
        return [_png()]

    with patch("flowboard.services.comic.r2.upload_media", side_effect=fake_upload), \
         patch("flowboard.services.comic.r2.delete_media", side_effect=lambda m: deleted.append(m)), \
         patch("flowboard.services.comic.atrium_api.generate_image_variants", side_effect=fake_gen):
        result, err = await _handle_flow_gen_image(
            {"prompt": "with ref", "provider": "atrium", "ref_media_ids": [ref]}
        )
    assert err is None
    assert result["provider_used"] == "atrium"
    assert captured["urls"] and captured["urls"][0] == f"https://pub-x.r2.dev/media/{ref}.png"
    # input image cleaned up from R2 right after generation
    assert deleted == [ref]


@pytest.mark.asyncio
async def test_flow_gen_atrium_blocks_refs_when_no_gemini_fallback(monkeypatch):
    # Atrium + refs + no public URL AND no Gemini key → hard error (no fallback).
    monkeypatch.setenv("ATRIUM_CLIENT_ID", "id")
    monkeypatch.setenv("ATRIUM_CLIENT_SECRET", "sec")
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    _clear_public_urls(monkeypatch)
    ref = str(uuid.uuid4())
    media_service.ingest_inline_bytes(ref, _png(), kind="image", mime="image/png")

    result, err = await _handle_flow_gen_image(
        {"prompt": "with ref", "provider": "atrium", "ref_media_ids": [ref]}
    )
    assert result == {}
    assert err is not None and err.startswith("atrium_needs_public_url")


@pytest.mark.asyncio
async def test_flow_gen_atrium_not_configured(monkeypatch):
    monkeypatch.delenv("ATRIUM_CLIENT_ID", raising=False)
    monkeypatch.delenv("ATRIUM_CLIENT_SECRET", raising=False)
    result, err = await _handle_flow_gen_image({"prompt": "x", "provider": "atrium"})
    assert result == {}
    assert err is not None and err.startswith("atrium_not_configured")
