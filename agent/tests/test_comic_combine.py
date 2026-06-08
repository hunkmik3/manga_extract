"""Tests for combine_panels (4 panels → 2×2 9:16) + the stitch helper."""

import uuid
from unittest.mock import AsyncMock, patch

import cv2
import numpy as np
import pytest

from flowboard.services import media as media_service
from flowboard.services.comic import panels as panel_svc
from flowboard.worker.processor import _handle_combine_panels, _handle_regen_cell


def _png(w, h, v=120) -> bytes:
    return cv2.imencode(".png", np.full((h, w, 3), v, np.uint8))[1].tobytes()


def _ingest(data=None) -> str:
    mid = str(uuid.uuid4())
    media_service.ingest_inline_bytes(mid, data or _png(120, 160), kind="image", mime="image/png")
    return mid


def test_stitch_2x2_tight_grid_with_panels():
    panels = [panel_svc.decode_bgr(_png(300, 200, v)) for v in (50, 100, 150, 200)]
    out = panel_svc.stitch_2x2(panels, cell_w=540, gutter=16)
    assert out.shape[1] == 540 * 2 + 16 * 3  # 2 cols + gutters
    assert out.shape[0] > 0
    # panels present (not an all-white canvas)
    assert int(out.min()) < 230


def test_stitch_handles_missing_panels():
    out = panel_svc.stitch_2x2([panel_svc.decode_bgr(_png(300, 200)), None, None, None])
    assert out.shape[1] == 540 * 2 + 16 * 3 and out.shape[0] > 0


@pytest.mark.asyncio
async def test_combine_cleans_each_panel_then_code_stitches():
    page = str(uuid.uuid4())
    media_service.ingest_inline_bytes(page, _png(900, 1200), kind="image", mime="image/png")
    specs = [{"page_media_id": page, "box": {"x": 10, "y": 10 + i * 200, "w": 400, "h": 180}} for i in range(4)]

    prompts_seen = []
    aspects_seen = []
    refs_seen = []
    async def fake_edit(image_bytes, prompt, **kw):
        prompts_seen.append(prompt)
        aspects_seen.append(kw.get("aspect_ratio"))
        refs_seen.append(kw.get("reference_images"))
        return _png(400, 300)  # a "cleaned" panel
    with patch("flowboard.services.comic.bridge.edit_image", side_effect=fake_edit):
        result, err = await _handle_combine_panels({"project_id": "p", "panels": specs})

    assert err is None
    from flowboard.services.comic import prompts
    # the bridge cleaned+extended EACH panel (4×) to 9:16 — not one combine call.
    # Default combine stays source-only; references are opt-in to avoid copying
    # unrelated page/background content across cells.
    assert len(prompts_seen) == 4
    assert all(p == prompts.CLEAN_PROMPT + prompts.EXTEND_9_16 for p in prompts_seen)
    assert aspects_seen == ["IMAGE_ASPECT_RATIO_PORTRAIT"] * 4
    assert all(r is None for r in refs_seen)
    assert result["panels_cleaned"] == 4
    assert result["width"] == 540 * 2 + 16 * 3   # code-stitched 2×2
    assert media_service.status(result["mediaId"]).get("available") is True
    # each cleaned cell is kept individually (for per-cell re-gen)
    assert len(result["cells"]) == 4
    assert all(media_service.status(c).get("available") for c in result["cells"])
    for cell_id in result["cells"]:
        cell = panel_svc.decode_bgr(media_service.cached_path(cell_id).read_bytes())
        assert cell.shape[1] / cell.shape[0] == pytest.approx(9 / 16, rel=0.02)


@pytest.mark.asyncio
async def test_combine_extends_portrait_panels_to_9x16():
    page = str(uuid.uuid4())
    media_service.ingest_inline_bytes(page, _png(900, 1200), kind="image", mime="image/png")
    specs = [{"page_media_id": page, "box": {"x": 10, "y": 10, "w": 300, "h": 620}}]

    prompts_seen = []
    aspects_seen = []

    async def fake_edit(image_bytes, prompt, **kw):
        prompts_seen.append(prompt)
        aspects_seen.append(kw.get("aspect_ratio"))
        return _png(400, 711)

    with patch("flowboard.services.comic.bridge.edit_image", side_effect=fake_edit):
        result, err = await _handle_combine_panels({"project_id": "p", "panels": specs})

    assert err is None
    from flowboard.services.comic import prompts
    assert prompts_seen == [prompts.CLEAN_PROMPT + prompts.EXTEND_9_16]
    assert aspects_seen == ["IMAGE_ASPECT_RATIO_PORTRAIT"]
    assert result["panels_cleaned"] == 1


@pytest.mark.asyncio
async def test_regen_cell_recleans_one_and_restitches():
    page = str(uuid.uuid4())
    media_service.ingest_inline_bytes(page, _png(900, 1200), kind="image", mime="image/png")
    cells = [_ingest(_png(400, 711)) for _ in range(4)]  # current cleaned cells
    panel = {"page_media_id": page, "box": {"x": 0, "y": 0, "w": 400, "h": 200}}

    edit = AsyncMock(return_value=_png(400, 711))
    with patch("flowboard.services.comic.bridge.edit_image", edit):
        result, err = await _handle_regen_cell({"project_id": "p", "panel": panel, "cells": cells, "index": 1})
    assert err is None
    assert edit.await_count == 1                          # only ONE panel re-cleaned
    assert edit.await_args.kwargs["reference_images"] is None
    assert result["cells"][1] != cells[1]                  # cell 1 replaced
    assert result["cells"][0] == cells[0] and result["cells"][2] == cells[2]  # others reused
    assert media_service.status(result["mediaId"]).get("available") is True


@pytest.mark.asyncio
async def test_regen_cell_uses_custom_prompt():
    page = str(uuid.uuid4())
    media_service.ingest_inline_bytes(page, _png(900, 1200), kind="image", mime="image/png")
    cells = [_ingest(_png(400, 711)) for _ in range(4)]
    panel = {"page_media_id": page, "box": {"x": 0, "y": 0, "w": 400, "h": 200}}

    edit = AsyncMock(return_value=_png(400, 711))
    with patch("flowboard.services.comic.bridge.edit_image", edit):
        _, err = await _handle_regen_cell(
            {"project_id": "p", "panel": panel, "cells": cells, "index": 0, "prompt": "make the lighting warmer"}
        )
    assert err is None
    assert edit.await_args.args[1] == "make the lighting warmer"  # custom prompt, no default


@pytest.mark.asyncio
async def test_regen_cell_blank_prompt_falls_back_to_default():
    page = str(uuid.uuid4())
    media_service.ingest_inline_bytes(page, _png(900, 1200), kind="image", mime="image/png")
    cells = [_ingest(_png(400, 711)) for _ in range(4)]
    panel = {"page_media_id": page, "box": {"x": 0, "y": 0, "w": 400, "h": 200}}

    edit = AsyncMock(return_value=_png(400, 711))
    with patch("flowboard.services.comic.bridge.edit_image", edit):
        await _handle_regen_cell(
            {"project_id": "p", "panel": panel, "cells": cells, "index": 0, "prompt": "   "}
        )
    from flowboard.services.comic import prompts
    assert edit.await_args.args[1] == prompts.CLEAN_PROMPT + prompts.EXTEND_9_16


@pytest.mark.asyncio
async def test_combine_uses_character_db_refs_when_available():
    page = _ingest(_png(900, 1200))
    ref = _ingest(_png(80, 120, 220))
    specs = [{"page_media_id": page, "box": {"x": 10, "y": 10, "w": 400, "h": 180}}]
    chars = [{"id": "char_0", "sampleMediaId": ref, "refMediaIds": [ref]}]

    edit = AsyncMock(return_value=_png(400, 711))
    with patch("flowboard.services.comic.bridge.edit_image", edit), \
         patch("flowboard.services.comic.characters.match_character", return_value="char_0"):
        result, err = await _handle_combine_panels({"project_id": "p", "panels": specs, "characters": chars})

    assert err is None
    refs = edit.await_args.kwargs["reference_images"]
    assert refs is not None and len(refs) == 1  # matched character crop only
    from flowboard.services.comic import prompts
    assert edit.await_args.args[1].endswith(prompts.COMBINE_CHARACTER_REFERENCE_CLAUSE)
    assert result["panels_cleaned"] == 1


@pytest.mark.asyncio
async def test_regen_cell_errors():
    cells = [_ingest() for _ in range(4)]
    assert (await _handle_regen_cell({"panel": {}, "cells": cells, "index": 0}))[1] == "missing_project_id"
    assert (await _handle_regen_cell({"project_id": "p", "cells": cells, "index": 0}))[1] == "missing_panel"
    assert (await _handle_regen_cell({"project_id": "p", "panel": {"source_media_id": "x"}, "cells": cells, "index": 9}))[1] == "bad_index"


@pytest.mark.asyncio
async def test_combine_panels_errors():
    assert (await _handle_combine_panels({"panels": [{}]}))[1] == "missing_project_id"
    assert (await _handle_combine_panels({"project_id": "p"}))[1] == "missing_panels"
    assert (await _handle_combine_panels({"project_id": "p", "panels": [{"page_media_id": "ghost", "box": {"x": 0, "y": 0, "w": 9, "h": 9}}]}))[1] == "no_source_image"
