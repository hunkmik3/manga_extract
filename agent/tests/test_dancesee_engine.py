"""DanceSee B2B unmoderated image engine + colorize provider routing."""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import cv2
import httpx
import numpy as np
import pytest

from flowboard.services.comic import dancesee_api
from flowboard.services.comic.bridge import BridgeEditError
from flowboard.worker.processor import _colorize_engine


def _png(w=80, h=80, v=120) -> bytes:
    return cv2.imencode(".png", np.full((h, w, 3), v, np.uint8))[1].tobytes()


@pytest.fixture
def dancesee_key(monkeypatch):
    monkeypatch.setenv("DANCESEE_API_KEY", "ds-test-key")
    monkeypatch.delenv("AVIS_API_KEY", raising=False)
    monkeypatch.delenv("DANCESEE_BASE_URL", raising=False)


def test_colorize_engine_defaults_to_dancesee(monkeypatch):
    monkeypatch.delenv("COLORIZE_PROVIDER", raising=False)
    engine, model, provider = _colorize_engine()
    assert provider == "dancesee"
    assert model == "dola-seedream-5-0-pro"
    assert engine is dancesee_api


def test_colorize_engine_b2b_alias(monkeypatch):
    monkeypatch.setenv("COLORIZE_PROVIDER", "b2b")
    engine, model, provider = _colorize_engine()
    assert provider == "dancesee"
    assert engine is dancesee_api
    assert model == "dola-seedream-5-0-pro"


def test_colorize_engine_avis_still_available(monkeypatch):
    from flowboard.services.comic import avis_api

    monkeypatch.setenv("COLORIZE_PROVIDER", "avis")
    engine, model, provider = _colorize_engine()
    assert provider == "avis"
    assert engine is avis_api
    assert model == "dola-seedream-5-0-pro"


def test_resolve_model_aliases_and_rejects():
    assert dancesee_api.resolve_model("dola-seedream-5-0-pro-260628") == "dola-seedream-5-0-pro"
    assert dancesee_api.resolve_model("seedream-4.5") == "seedream-4-5"
    assert dancesee_api.resolve_model("") == "dola-seedream-5-0-pro"
    with pytest.raises(BridgeEditError) as ei:
        dancesee_api.resolve_model("grok-imagine-image-quality")
    assert "does not support contentFilterDisabled" in str(ei.value)


def test_moderation_block_is_detected():
    from flowboard.services.comic.avis_api import _is_moderation_block

    assert _is_moderation_block("400 The request failed because the input image may contain sensitive information.")
    assert _is_moderation_block("does not support contentFilterDisabled")
    assert not _is_moderation_block("avis: async poll timed out")


def test_api_key_falls_back_to_avis(monkeypatch):
    monkeypatch.delenv("DANCESEE_API_KEY", raising=False)
    monkeypatch.setenv("AVIS_API_KEY", "avis-key")
    assert dancesee_api.api_key() == "avis-key"
    assert dancesee_api.is_configured()


@pytest.mark.asyncio
async def test_dancesee_posts_b2b_async_and_polls(dancesee_key):
    captured = {}
    png = _png()

    async def fake_post(self, url, headers=None, json=None):
        captured["url"] = url
        captured["headers"] = headers
        captured["body"] = json
        return httpx.Response(202, json={"data": {"generationId": "gen-abc"}, "success": True, "status": 202})

    async def fake_get(self, url, headers=None):
        captured.setdefault("gets", []).append(url)
        if url.endswith("/gen-abc"):
            return httpx.Response(200, json={
                "data": {
                    "status": "succeeded",
                    "generationId": "gen-abc",
                    "result": {"images": [{"url": "https://cdn.example/out.png"}]},
                },
                "success": True,
                "status": 200,
            })
        return httpx.Response(200, content=png)

    with patch.object(httpx.AsyncClient, "post", fake_post), \
         patch.object(httpx.AsyncClient, "get", fake_get), \
         patch("flowboard.services.comic.dancesee_api.asyncio.sleep", new=AsyncMock()):
        outs = await dancesee_api.generate_image_variants(
            "colorize this page", [_png()], image_model="dola-seedream-5-0-pro-260628",
        )

    assert len(outs) == 1 and outs[0] == png
    assert captured["url"] == "https://api.dancesee.io/api/v1/b2b/image/generations/async"
    assert captured["headers"]["Authorization"] == "Bearer ds-test-key"
    assert captured["headers"]["X-Api-Key"] == "ds-test-key"
    assert captured["body"]["model"] == "dola-seedream-5-0-pro"
    types = [c["type"] for c in captured["body"]["content"]]
    assert "imageBase64" in types
    assert "text" in types
    assert any(u.endswith("/api/v1/b2b/image/generations/async/gen-abc") for u in captured["gets"])


@pytest.mark.asyncio
async def test_dancesee_403_is_fatal(dancesee_key):
    async def fake_post(self, url, headers=None, json=None):
        return httpx.Response(403, json={"message": "userType is not B2B"})

    with patch.object(httpx.AsyncClient, "post", fake_post):
        with pytest.raises(BridgeEditError) as ei:
            await dancesee_api.generate_image_variants("x", None, image_model="seedream-4-0")
    assert "403" in str(ei.value)
    assert "userType is not B2B" in str(ei.value)


@pytest.mark.asyncio
async def test_dancesee_expired_job_retries_then_fails(dancesee_key):
    posts = {"n": 0}

    async def fake_post(self, url, headers=None, json=None):
        posts["n"] += 1
        return httpx.Response(202, json={"data": {"generationId": f"gen-{posts['n']}"}})

    async def fake_get(self, url, headers=None):
        return httpx.Response(200, json={"data": {"status": "expired", "error": "retention elapsed"}})

    with patch.object(httpx.AsyncClient, "post", fake_post), \
         patch.object(httpx.AsyncClient, "get", fake_get), \
         patch("flowboard.services.comic.dancesee_api.asyncio.sleep", new=AsyncMock()):
        with pytest.raises(BridgeEditError) as ei:
            await dancesee_api.generate_image_variants(
                "x", None, image_model="seedream-5-0", max_attempts=2,
            )
    assert posts["n"] == 2
    assert "expired" in str(ei.value)
