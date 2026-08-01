"""Tests for the Flow-clone studio handler (flow_gen_image): pure text→image
plus image edit/refine via the Gemini API engine (no Flow bridge, no node)."""

import uuid
from unittest.mock import patch

import cv2
import numpy as np
import pytest

from flowboard.services import media as media_service
from flowboard.worker.processor import _handle_flow_gen_image


def _png(w=200, h=200, v=120) -> bytes:
    return cv2.imencode(".png", np.full((h, w, 3), v, np.uint8))[1].tobytes()


@pytest.mark.asyncio
async def test_flow_gen_image_text_to_image_stores_variants():
    seen = {}

    async def fake_gen(prompt, refs, **kw):
        seen["prompt"] = prompt
        seen["refs"] = refs
        seen["kw"] = kw
        return [_png(), _png()]

    with patch("flowboard.services.comic.gemini_api.generate_image_variants", side_effect=fake_gen):
        result, err = await _handle_flow_gen_image(
            {
                "prompt": "a duck in goggles",
                "aspect_ratio": "16:9",
                "image_model": "gemini-3-pro-image",
                "variant_count": 2,
            }
        )

    assert err is None
    assert seen["prompt"] == "a duck in goggles"
    assert seen["refs"] is None  # no refs supplied
    assert seen["kw"]["image_model"] == "gemini-3-pro-image"
    assert seen["kw"]["aspect_ratio"] == "16:9"
    assert seen["kw"]["variant_count"] == 2
    # both variants persisted + servable
    media_ids = result["media_ids"]
    assert len(media_ids) == 2
    assert all(media_service.cached_path(m) is not None for m in media_ids)


@pytest.mark.asyncio
async def test_flow_gen_image_forwards_reference_bytes():
    ref_id = str(uuid.uuid4())
    media_service.ingest_inline_bytes(ref_id, _png(64, 64, 200), kind="image", mime="image/png")
    captured = {}

    async def fake_gen(prompt, refs, **kw):
        captured["refs"] = refs
        return [_png()]

    with patch("flowboard.services.comic.gemini_api.generate_image_variants", side_effect=fake_gen):
        _, err = await _handle_flow_gen_image(
            {"prompt": "@Aria at the airport", "ref_media_ids": [ref_id]}
        )

    assert err is None
    assert captured["refs"] is not None and len(captured["refs"]) == 1


@pytest.mark.asyncio
async def test_flow_gen_image_edit_mode_uses_source():
    src_id = str(uuid.uuid4())
    media_service.ingest_inline_bytes(src_id, _png(128, 128), kind="image", mime="image/png")
    captured = {}

    async def fake_edit(image_bytes, prompt, refs, **kw):
        captured["has_source"] = bool(image_bytes)
        captured["aspect"] = kw.get("aspect_ratio")
        return [_png()]

    # edit path must NOT call the text→image generator
    with patch("flowboard.services.comic.gemini_api.edit_image_variants", side_effect=fake_edit), \
         patch("flowboard.services.comic.gemini_api.generate_image_variants") as gen:
        result, err = await _handle_flow_gen_image(
            {"prompt": "make it night", "source_media_id": src_id}
        )

    assert err is None
    gen.assert_not_called()
    assert captured["has_source"] is True
    assert captured["aspect"] == ""  # preserves the source frame
    assert len(result["media_ids"]) == 1


@pytest.mark.asyncio
async def test_on_progress_fires_per_variant(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    from flowboard.services.comic import gemini_api

    calls = []

    async def fake_once(client, model, key, body):
        return _png()

    with patch("flowboard.services.comic.gemini_api._generate_once", side_effect=fake_once):
        await gemini_api.generate_image_variants(
            "x", None, image_model="gemini-2.5-flash-image", variant_count=3,
            on_progress=lambda d, t: calls.append((d, t)),
        )
    assert calls == [(1, 3), (2, 3), (3, 3)]


@pytest.mark.asyncio
async def test_flow_gen_image_requires_prompt():
    result, err = await _handle_flow_gen_image({"prompt": "   "})
    assert err == "missing_prompt"
    assert result == {}


@pytest.mark.asyncio
async def test_flow_gen_image_missing_source_errors():
    _, err = await _handle_flow_gen_image(
        {"prompt": "edit me", "source_media_id": str(uuid.uuid4())}
    )
    assert err == "source_not_found"
