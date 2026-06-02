"""Tests for character tracking: build_character_db + enhance auto-match.
The ML pieces (manga109 detection, CCIP cluster/match) are mocked."""

import uuid
from unittest.mock import AsyncMock, patch

import cv2
import numpy as np
import pytest

from flowboard.services import media as media_service
from flowboard.worker.processor import _handle_build_character_db, _handle_enhance_panel


def _png(w=120, h=160, v=110) -> bytes:
    return cv2.imencode(".png", np.full((h, w, 3), v, np.uint8))[1].tobytes()


def _ingest(data=None) -> str:
    mid = str(uuid.uuid4())
    media_service.ingest_inline_bytes(mid, data or _png(), kind="image", mime="image/png")
    return mid


@pytest.mark.asyncio
async def test_build_character_db_ingests_refs_and_shapes_result():
    pages = [{"mediaId": _ingest(), "name": "p01.png", "idx": 0},
             {"mediaId": _ingest(), "name": "p02.png", "idx": 1}]
    fake_clusters = [
        {"count": 12, "ref_png": [_png(), _png()], "pages": [0, 1]},
        {"count": 4, "ref_png": [_png()], "pages": [1]},
    ]
    with patch("flowboard.services.comic.characters.build_clusters", return_value=fake_clusters):
        result, err = await _handle_build_character_db({"pages": pages, "__node_id": 5})
    assert err is None
    assert result["character_count"] == 2
    c0 = result["characters"][0]
    assert c0["name"] == "Character 1" and c0["count"] == 12
    assert len(c0["refMediaIds"]) == 2
    assert c0["sampleMediaId"] == c0["refMediaIds"][0]
    for c in result["characters"]:
        for mid in c["refMediaIds"]:
            assert media_service.status(mid).get("available") is True


@pytest.mark.asyncio
async def test_build_character_db_errors():
    assert (await _handle_build_character_db({}))[1] == "missing_pages"
    assert (await _handle_build_character_db({"pages": [{"mediaId": "nope-not-cached"}]}))[1] == "no_readable_pages"


@pytest.mark.asyncio
async def test_build_character_db_ml_unavailable_surfaces():
    pages = [{"mediaId": _ingest(), "name": "p", "idx": 0}]
    from flowboard.services.comic.panel_ml import MLUnavailable
    with patch("flowboard.services.comic.characters.build_clusters", side_effect=MLUnavailable("install .[ml]")):
        _, err = await _handle_build_character_db({"pages": pages})
    assert err and err.startswith("ml_unavailable")


@pytest.mark.asyncio
async def test_enhance_auto_matches_character_and_adds_refs():
    panel = _ingest(_png(300, 400))
    sample = _ingest(_png(120, 160))
    ref = _ingest(_png(120, 160, v=200))
    chars = [{"id": "char_0", "sampleMediaId": sample, "refMediaIds": [ref]}]

    edit = AsyncMock(return_value=_png(300, 400))
    with patch("flowboard.services.comic.bridge.edit_image", edit), \
         patch("flowboard.services.comic.characters.match_character", return_value="char_0"):
        result, err = await _handle_enhance_panel(
            {"project_id": "p", "source_media_id": panel, "characters": chars}
        )
    assert err is None
    args, kwargs = edit.await_args
    refs = kwargs["reference_images"]
    assert refs is not None and len(refs) == 1   # the matched character's ref crop
    from flowboard.services.comic import prompts
    assert args[1].endswith(prompts.REFERENCE_CLAUSE)


@pytest.mark.asyncio
async def test_enhance_no_match_no_refs():
    panel = _ingest(_png(300, 400))
    chars = [{"id": "char_0", "sampleMediaId": _ingest(), "refMediaIds": [_ingest()]}]
    edit = AsyncMock(return_value=_png(300, 400))
    with patch("flowboard.services.comic.bridge.edit_image", edit), \
         patch("flowboard.services.comic.characters.match_character", return_value=None):
        _, err = await _handle_enhance_panel({"project_id": "p", "source_media_id": panel, "characters": chars})
    assert err is None
    _, kwargs = edit.await_args
    assert kwargs["reference_images"] is None
