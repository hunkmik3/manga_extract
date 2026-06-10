"""Tests for the Phase 4/5 bridge tasks clean_panel / enhance_panel.
The Flow bridge (services.comic.bridge.edit_image) is mocked — we verify the
prompt/aspect wiring, source-bytes resolution, and 9:16 padding without a live
extension.
"""

import uuid
from unittest.mock import AsyncMock, patch

import cv2
import numpy as np
import pytest

from flowboard.services import media as media_service
from flowboard.services.comic import prompts
from flowboard.worker.processor import _handle_clean_panel, _handle_enhance_panel


def _png(w: int, h: int, val: int = 120) -> bytes:
    ok, buf = cv2.imencode(".png", np.full((h, w, 3), val, np.uint8))
    return buf.tobytes()


def _ingest_source(w=300, h=500) -> str:
    mid = str(uuid.uuid4())
    media_service.ingest_inline_bytes(mid, _png(w, h), kind="image", mime="image/png")
    return mid


@pytest.mark.asyncio
async def test_clean_panel_uses_clean_prompt_and_returns_media():
    src = _ingest_source(300, 500)  # portrait source
    edit = AsyncMock(return_value=_png(300, 500))
    with patch("flowboard.services.comic.bridge.edit_image", edit):
        result, err = await _handle_clean_panel({"project_id": "proj1", "source_media_id": src})
    assert err is None
    assert media_service.status(result["mediaId"]).get("available") is True
    args, kwargs = edit.await_args
    assert args[1].startswith(prompts.CLEAN_PROMPT[:40])      # CLEAN prompt
    assert prompts.EXTEND_9_16.strip()[:20] not in args[1]    # no 9:16 clause
    assert kwargs["aspect_ratio"] == "IMAGE_ASPECT_RATIO_PORTRAIT"  # matches source orientation


@pytest.mark.asyncio
async def test_clean_panel_variant_count_returns_all_media_ids():
    """x4: clean asks the bridge for 4 candidates and returns them in mediaIds
    (mediaId = the first). Uses edit_image_variants (not edit_image)."""
    src = _ingest_source(300, 500)
    variants = AsyncMock(return_value=[_png(300, 500) for _ in range(4)])
    with patch("flowboard.services.comic.bridge.edit_image_variants", variants):
        result, err = await _handle_clean_panel(
            {"project_id": "proj1", "source_media_id": src, "variant_count": 4}
        )
    assert err is None
    assert variants.await_count == 1
    assert variants.await_args.kwargs["variant_count"] == 4
    assert len(result["mediaIds"]) == 4
    assert result["mediaId"] == result["mediaIds"][0]
    for mid in result["mediaIds"]:
        assert media_service.status(mid).get("available") is True


@pytest.mark.asyncio
async def test_clean_panel_extend_916_pads_and_requests_portrait():
    src = _ingest_source(400, 400)  # square source
    edit = AsyncMock(return_value=_png(400, 400))  # model returns square
    with patch("flowboard.services.comic.bridge.edit_image", edit):
        result, err = await _handle_clean_panel({"project_id": "p", "source_media_id": src, "extend_916": True})
    assert err is None
    args, kwargs = edit.await_args
    assert prompts.EXTEND_9_16.strip()[:20] in args[1]          # 9:16 clause appended
    assert kwargs["aspect_ratio"] == "IMAGE_ASPECT_RATIO_PORTRAIT"
    # output letterboxed to ~9:16
    ratio = result["width"] / result["height"]
    assert abs(ratio - 9 / 16) < 0.03, (result["width"], result["height"])


@pytest.mark.asyncio
async def test_enhance_panel_uses_enhance_prompt():
    src = _ingest_source(500, 300)  # landscape
    edit = AsyncMock(return_value=_png(500, 300))
    with patch("flowboard.services.comic.bridge.edit_image", edit):
        result, err = await _handle_enhance_panel({"project_id": "p", "source_media_id": src})
    assert err is None
    args, kwargs = edit.await_args
    assert args[1].startswith(prompts.ENHANCE_PROMPT[:40])
    assert kwargs["aspect_ratio"] == "IMAGE_ASPECT_RATIO_LANDSCAPE"


@pytest.mark.asyncio
async def test_clean_panel_crops_from_page_and_box():
    # source provided as page_media_id + box (the comic_panel reference shape)
    page = str(uuid.uuid4())
    media_service.ingest_inline_bytes(page, _png(900, 1200), kind="image", mime="image/png")
    captured = {}
    async def fake_edit(image_bytes, prompt, **kw):
        captured["bytes"] = image_bytes
        return _png(200, 300)
    with patch("flowboard.services.comic.bridge.edit_image", side_effect=fake_edit):
        result, err = await _handle_clean_panel(
            {"project_id": "p", "page_media_id": page, "box": {"x": 10, "y": 20, "w": 200, "h": 300}}
        )
    assert err is None
    # the cropped source decodes to the box size
    crop = cv2.imdecode(np.frombuffer(captured["bytes"], np.uint8), cv2.IMREAD_COLOR)
    assert crop.shape[1] == 200 and crop.shape[0] == 300


@pytest.mark.asyncio
async def test_error_cases():
    assert (await _handle_clean_panel({"source_media_id": "x"}))[1] == "missing_project_id"
    assert (await _handle_clean_panel({"project_id": "p"}))[1] == "no_source_image"
    # bridge failure surfaces
    src = _ingest_source()
    from flowboard.services.comic.bridge import BridgeEditError
    with patch("flowboard.services.comic.bridge.edit_image", AsyncMock(side_effect=BridgeEditError("paygate_tier_unknown", attempts=0))):
        _, err = await _handle_clean_panel({"project_id": "p", "source_media_id": src})
    assert err and err.startswith("bridge_failed")
