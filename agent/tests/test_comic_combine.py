"""Tests for combine_panels (4 panels → 2×2 9:16) + the stitch helper."""

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import cv2
import numpy as np
import pytest

from flowboard.services import media as media_service
from flowboard.services.comic import panels as panel_svc
from flowboard.worker.processor import (
    _handle_combine_panels,
    _handle_regen_cell,
    _handle_restitch_cells,
    _handle_export_all_panels,
)


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
async def test_combine_prepads_wide_strip_to_916_canvas():
    """A wide strip panel (letterboxed close-up) must reach the bridge as a FULL
    9:16 canvas with edge-replicated seed bands — otherwise the model letterboxes
    and the cell comes back as a face between empty bars instead of a fully
    painted 9:16 frame."""
    page = str(uuid.uuid4())
    media_service.ingest_inline_bytes(page, _png(900, 1200), kind="image", mime="image/png")
    specs = [{"page_media_id": page, "box": {"x": 0, "y": 0, "w": 800, "h": 200}}]  # 4:1 strip

    sent = {}
    async def fake_edit(image_bytes, prompt, **kw):
        sent["img"] = image_bytes
        return _png(400, 711)
    with patch("flowboard.services.comic.bridge.edit_image", side_effect=fake_edit):
        _, err = await _handle_combine_panels({"project_id": "p", "panels": specs})
    assert err is None
    img = panel_svc.decode_bgr(sent["img"])
    assert img.shape[1] / img.shape[0] == pytest.approx(9 / 16, rel=0.02)


@pytest.mark.asyncio
async def test_regen_cell_prepads_source_to_916_canvas():
    page = str(uuid.uuid4())
    media_service.ingest_inline_bytes(page, _png(900, 1200), kind="image", mime="image/png")
    cells = [_ingest(_png(400, 711)) for _ in range(4)]
    panel = {"page_media_id": page, "box": {"x": 0, "y": 0, "w": 800, "h": 150}}  # extreme strip

    sent = {}
    async def fake_edit(image_bytes, prompt, **kw):
        sent["img"] = image_bytes
        return _png(400, 711)
    with patch("flowboard.services.comic.bridge.edit_image", side_effect=fake_edit):
        _, err = await _handle_regen_cell({"project_id": "p", "panel": panel, "cells": cells, "index": 0})
    assert err is None
    img = panel_svc.decode_bgr(sent["img"])
    assert img.shape[1] / img.shape[0] == pytest.approx(9 / 16, rel=0.02)


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
async def test_regen_cell_variant_count_returns_candidates_without_committing():
    """x4: regen returns 4 candidates for the cell and does NOT touch the grid
    (no mediaId/cells) — the user picks one, then restitch_cells commits it."""
    page = str(uuid.uuid4())
    media_service.ingest_inline_bytes(page, _png(900, 1200), kind="image", mime="image/png")
    cells = [_ingest(_png(400, 711)) for _ in range(4)]
    panel = {"page_media_id": page, "box": {"x": 0, "y": 0, "w": 400, "h": 200}}

    variants = AsyncMock(return_value=[_png(400, 711) for _ in range(4)])
    with patch("flowboard.services.comic.bridge.edit_image_variants", variants):
        result, err = await _handle_regen_cell(
            {"project_id": "p", "panel": panel, "cells": cells, "index": 2, "variant_count": 4}
        )
    assert err is None
    assert variants.await_args.kwargs["variant_count"] == 4
    assert result.get("index") == 2
    assert len(result["candidates"]) == 4
    assert "mediaId" not in result and "cells" not in result  # grid untouched
    for mid in result["candidates"]:
        assert media_service.status(mid).get("available") is True


@pytest.mark.asyncio
async def test_restitch_cells_builds_composite_from_cells():
    cells = [_ingest(_png(400, 711)) for _ in range(4)]
    result, err = await _handle_restitch_cells({"cells": cells})
    assert err is None
    assert result["cells"] == cells
    assert media_service.status(result["mediaId"]).get("available") is True
    assert result["width"] > 0 and result["height"] > 0


@pytest.mark.asyncio
async def test_restitch_cells_errors_without_cells():
    assert (await _handle_restitch_cells({}))[1] == "missing_cells"


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
    prompt = edit.await_args.args[1]
    assert prompts.COMBINE_CHARACTER_REFERENCE_CLAUSE in prompt
    assert prompts._FRAMING_LOCK in prompt  # shot stays the source's, ref guides look only
    assert result["panels_cleaned"] == 1


@pytest.mark.asyncio
async def test_combine_assigned_char_id_bypasses_ccip():
    """An explicit per-cell char_id feeds THAT character's frozen refs directly
    and never invokes CCIP — even when CCIP would have matched a different one."""
    page = _ingest(_png(900, 1200))
    ref0 = _ingest(_png(80, 120, 30))
    ref1 = _ingest(_png(80, 120, 210))
    specs = [{"page_media_id": page, "box": {"x": 10, "y": 10, "w": 400, "h": 180}, "char_id": "char_1"}]
    chars = [
        {"id": "char_0", "name": "Alpha", "sampleMediaId": ref0, "refMediaIds": [ref0]},
        {"id": "char_1", "name": "Beta", "sampleMediaId": ref1, "refMediaIds": [ref1]},
    ]

    edit = AsyncMock(return_value=_png(400, 711))
    match = MagicMock(return_value="char_0")  # CCIP would pick the WRONG character
    with patch("flowboard.services.comic.bridge.edit_image", edit), \
         patch("flowboard.services.comic.characters.match_character", match):
        result, err = await _handle_combine_panels({"project_id": "p", "panels": specs, "characters": chars})

    assert err is None
    match.assert_not_called()  # assigned char_id wins → CCIP never runs
    refs = edit.await_args.kwargs["reference_images"]
    assert refs == [media_service.cached_path(ref1).read_bytes()]  # char_1's frozen ref, not char_0's
    # default clause names the assigned character
    assert edit.await_args.args[1].endswith("The character is Beta.")


@pytest.mark.asyncio
async def test_combine_auto_match_false_disables_ccip_fallback():
    """With no char_id and auto_match=False, the CCIP fallback is off → no
    character refs are attached even though a Character DB is present."""
    page = _ingest(_png(900, 1200))
    ref = _ingest(_png(80, 120, 220))
    specs = [{"page_media_id": page, "box": {"x": 10, "y": 10, "w": 400, "h": 180}}]
    chars = [{"id": "char_0", "name": "Alpha", "sampleMediaId": ref, "refMediaIds": [ref]}]

    edit = AsyncMock(return_value=_png(400, 711))
    match = MagicMock(return_value="char_0")
    with patch("flowboard.services.comic.bridge.edit_image", edit), \
         patch("flowboard.services.comic.characters.match_character", match):
        _, err = await _handle_combine_panels(
            {"project_id": "p", "panels": specs, "characters": chars, "auto_match": False}
        )

    assert err is None
    match.assert_not_called()
    assert edit.await_args.kwargs["reference_images"] is None  # no refs → CCIP suppressed


@pytest.mark.asyncio
async def test_combine_override_outfit_clause_in_prompt():
    """A cell flagged canon-disagreeing on outfit swaps in the OVERRIDE clause so
    the canon ref wins the outfit, instead of the default 'stay faithful' hint."""
    page = _ingest(_png(900, 1200))
    ref = _ingest(_png(80, 120, 220))
    specs = [{
        "page_media_id": page, "box": {"x": 10, "y": 10, "w": 400, "h": 180},
        "char_id": "char_0", "outfit": "red winter coat", "override_axes": ["outfit"],
    }]
    chars = [{"id": "char_0", "name": "Alpha", "sampleMediaId": ref, "refMediaIds": [ref]}]

    edit = AsyncMock(return_value=_png(400, 711))
    with patch("flowboard.services.comic.bridge.edit_image", edit):
        _, err = await _handle_combine_panels({"project_id": "p", "panels": specs, "characters": chars})

    assert err is None
    prompt = edit.await_args.args[1]
    from flowboard.services.comic import prompts
    assert "OFF-MODEL" in prompt
    assert "red winter coat" in prompt
    assert prompt.endswith(prompts._OVERRIDE_TAIL)
    assert prompts.COMBINE_CHARACTER_REFERENCE_CLAUSE not in prompt  # default hint replaced


@pytest.mark.asyncio
async def test_regen_cell_assigned_char_id_overrides_identity():
    page = _ingest(_png(900, 1200))
    cells = [_ingest(_png(400, 711)) for _ in range(4)]
    ref = _ingest(_png(80, 120, 200))
    panel = {
        "page_media_id": page, "box": {"x": 0, "y": 0, "w": 400, "h": 200},
        "char_id": "char_0", "override_axes": ["identity"],
    }
    chars = [{"id": "char_0", "name": "Lina", "sampleMediaId": ref, "refMediaIds": [ref]}]

    edit = AsyncMock(return_value=_png(400, 711))
    match = MagicMock(return_value=None)
    with patch("flowboard.services.comic.bridge.edit_image", edit), \
         patch("flowboard.services.comic.characters.match_character", match):
        _, err = await _handle_regen_cell(
            {"project_id": "p", "panel": panel, "cells": cells, "index": 0, "characters": chars}
        )
    assert err is None
    match.assert_not_called()
    assert edit.await_args.kwargs["reference_images"] == [media_service.cached_path(ref).read_bytes()]
    prompt = edit.await_args.args[1]
    assert "authoritative design" in prompt and "Lina" in prompt


@pytest.mark.asyncio
async def test_assigned_refs_pick_view_by_shot_and_orientation():
    """Typed refViews route by camera: close-up → face view first; wide → body
    first; back-facing → back view first. This is the Director's view picker."""
    from flowboard.worker.processor import _assigned_char_refs

    face = _ingest(_png(80, 80, 10))
    body = _ingest(_png(80, 240, 120))
    back = _ingest(_png(80, 240, 230))
    char = {
        "id": "char_0", "name": "Zero",
        "refMediaIds": [face, body, back],
        "refViews": [
            {"mediaId": face, "kind": "face"},
            {"mediaId": body, "kind": "body"},
            {"mediaId": back, "kind": "back"},
        ],
    }
    fb = media_service.cached_path(face).read_bytes()
    bb = media_service.cached_path(body).read_bytes()
    kb = media_service.cached_path(back).read_bytes()

    spec = {"char_id": "char_0", "shot": "closeup", "orientation": "front"}
    assert _assigned_char_refs(spec, [char])[0] == fb          # close-up leads with face
    spec = {"char_id": "char_0", "shot": "wide"}
    assert _assigned_char_refs(spec, [char])[0] == bb          # wide leads with body
    spec = {"char_id": "char_0", "orientation": "back"}
    assert _assigned_char_refs(spec, [char])[0] == kb          # back panel leads with back view
    spec = {"char_id": "char_0"}                               # untagged → face-first default
    assert _assigned_char_refs(spec, [char])[0] == fb


@pytest.mark.asyncio
async def test_assigned_refs_kind_diverse_not_three_identical_sheet_crops():
    """Three near-identical face crops from a sheet overpower the source panel
    (the model reproduces the sheet). The picker must take ONE view per kind —
    best face + best body — not face/face/face."""
    from flowboard.worker.processor import _assigned_char_refs

    f1 = _ingest(_png(80, 80, 10))
    f2 = _ingest(_png(80, 80, 40))
    f3 = _ingest(_png(80, 80, 70))
    body = _ingest(_png(80, 240, 120))
    char = {
        "id": "char_0",
        "refMediaIds": [f1, f2, f3, body],
        "refViews": [
            {"mediaId": f1, "kind": "face"},
            {"mediaId": f2, "kind": "face"},
            {"mediaId": f3, "kind": "face"},
            {"mediaId": body, "kind": "body"},
        ],
    }
    refs = _assigned_char_refs({"char_id": "char_0", "shot": "closeup"}, [char])
    assert len(refs) == 2                                              # capped at 2
    assert refs[0] == media_service.cached_path(f1).read_bytes()       # best face
    assert refs[1] == media_service.cached_path(body).read_bytes()     # then body — NOT another face


@pytest.mark.asyncio
async def test_combine_appends_verbatim_descriptor():
    page = _ingest(_png(900, 1200))
    ref = _ingest(_png(80, 120, 220))
    specs = [{"page_media_id": page, "box": {"x": 10, "y": 10, "w": 400, "h": 180}, "char_id": "char_0"}]
    chars = [{
        "id": "char_0", "name": "Zero", "sampleMediaId": ref, "refMediaIds": [ref],
        "descriptor": "young man, black messy hair, yellow military cap and uniform, amber eyes",
    }]
    edit = AsyncMock(return_value=_png(400, 711))
    with patch("flowboard.services.comic.bridge.edit_image", edit):
        _, err = await _handle_combine_panels({"project_id": "p", "panels": specs, "characters": chars})
    assert err is None
    prompt = edit.await_args.args[1]
    assert "The character is Zero: young man, black messy hair, yellow military cap and uniform, amber eyes" in prompt


def test_parse_panel_tags_sanitizes():
    from flowboard.worker.processor import _parse_panel_tags

    ok = _parse_panel_tags(
        'noise [{"panel":1,"char_id":"char_0","shot":"close-up","orientation":"back","confidence":0.9},'
        '{"panel":2,"char_id":"ghost","shot":"huge","orientation":"front"}] trailing',
        2, {"char_0"},
    )
    assert ok == [
        {"char_id": "char_0", "shot": "closeup", "orientation": "back", "confidence": 0.9},
        {"char_id": None, "shot": None, "orientation": "front", "confidence": None},  # unknown id/shot dropped
    ]
    assert _parse_panel_tags("no json here", 1, set()) is None
    # short array → padded with empty tags
    padded = _parse_panel_tags('[{"panel":1,"shot":"wide"}]', 2, set())
    assert padded[1] == {"char_id": None, "shot": None, "orientation": None, "confidence": None}


@pytest.mark.asyncio
async def test_tag_panels_calls_vision_and_returns_tags():
    from flowboard.worker.processor import _handle_tag_panels

    page = _ingest(_png(900, 1200))
    sample = _ingest(_png(80, 120, 200))
    specs = [
        {"page_media_id": page, "box": {"x": 0, "y": 0, "w": 400, "h": 300}},
        {"page_media_id": page, "box": {"x": 0, "y": 300, "w": 400, "h": 300}},
    ]
    chars = [{"id": "char_0", "name": "Zero", "sampleMediaId": sample}]

    seen = {}
    async def fake_llm(feature, user_prompt, **kw):
        seen["feature"] = feature
        seen["attachments"] = kw.get("attachments")
        seen["prompt"] = user_prompt
        return ('[{"panel":1,"char_id":"char_0","shot":"closeup","orientation":"front","confidence":0.95},'
                '{"panel":2,"char_id":null,"shot":"wide","orientation":"back","confidence":0.6}]')

    with patch("flowboard.services.llm.registry.run_llm", side_effect=fake_llm):
        result, err = await _handle_tag_panels({"panels": specs, "characters": chars})

    assert err is None
    assert seen["feature"] == "vision"
    assert len(seen["attachments"]) == 3            # 1 cast sample + 2 panel crops
    assert 'char_0' in seen["prompt"]
    assert result["tags"] == [
        {"char_id": "char_0", "shot": "closeup", "orientation": "front", "confidence": 0.95},
        {"char_id": None, "shot": "wide", "orientation": "back", "confidence": 0.6},
    ]


@pytest.mark.asyncio
async def test_tag_panels_errors():
    from flowboard.worker.processor import _handle_tag_panels
    assert (await _handle_tag_panels({}))[1] == "missing_panels"
    bad = {"panels": [{"page_media_id": "ghost", "box": {"x": 0, "y": 0, "w": 9, "h": 9}}]}
    assert (await _handle_tag_panels(bad))[1] == "no_source_image"


def test_primary_char_per_panel_maps_largest_contained_figure():
    from flowboard.worker.processor import _primary_char_per_panel

    panels = [
        {"id": "b1", "x": 0, "y": 0, "w": 500, "h": 400},
        {"id": "b2", "x": 0, "y": 420, "w": 500, "h": 400},
    ]
    chars = [
        ((50, 50, 100, 200), "char_0"),    # inside b1
        ((200, 60, 200, 300), "char_1"),   # inside b1, LARGER → wins b1
        ((100, 500, 80, 160), "char_0"),   # inside b2
        ((900, 900, 50, 50), "char_1"),    # outside every panel → dropped
    ]
    assert _primary_char_per_panel(panels, chars) == {"b1": "char_1", "b2": "char_0"}


@pytest.mark.asyncio
async def test_magi_assign_panels_maps_detections_to_boxes():
    from flowboard.worker.processor import _handle_magi_assign_panels

    page = _ingest(_png(800, 1200))
    ref = _ingest(_png(80, 120, 200))
    pages = [{"media_id": page, "boxes": [
        {"id": "b1", "x": 0, "y": 0, "w": 800, "h": 600},
        {"id": "b2", "x": 0, "y": 620, "w": 800, "h": 580},
    ]}]
    chars = [{"id": "char_0", "name": "Zero", "refMediaIds": [ref], "sampleMediaId": ref}]

    def fake_predict(page_bgrs, bank_imgs, bank_names):
        assert bank_names == ["char_0"]            # bank named by char id
        assert len(page_bgrs) == 1
        return [[((100, 100, 200, 300), "char_0"),  # → b1
                 ((100, 700, 150, 300), "Other")]]  # unnamed → ignored
    with patch("flowboard.services.comic.magi.predict_page_characters", side_effect=fake_predict):
        result, err = await _handle_magi_assign_panels({"pages": pages, "characters": chars})

    assert err is None
    assert result["assignments"] == {page: {"b1": "char_0"}}
    assert result["panels_assigned"] == 1


@pytest.mark.asyncio
async def test_magi_assign_panels_errors():
    from flowboard.worker.processor import _handle_magi_assign_panels
    assert (await _handle_magi_assign_panels({}))[1] == "missing_pages"
    assert (await _handle_magi_assign_panels({"pages": [{}]}))[1] == "missing_characters"
    bad = {"pages": [{"media_id": "ghost", "boxes": [{"id": "b", "x": 0, "y": 0, "w": 9, "h": 9}]}],
           "characters": [{"id": "c", "refMediaIds": ["ghost2"]}]}
    assert (await _handle_magi_assign_panels(bad))[1] == "no_readable_inputs"


def test_combine_reference_clause_default_and_overrides():
    from flowboard.services.comic import prompts
    # default = the gentle faithful hint + the shot/framing lock
    default = prompts.combine_reference_clause()
    assert prompts.COMBINE_CHARACTER_REFERENCE_CLAUSE in default and prompts._FRAMING_LOCK in default
    assert prompts.combine_reference_clause(char_name="Lina").endswith("The character is Lina.")
    # every override path also carries the framing lock (the load-bearing guard)
    assert prompts._FRAMING_LOCK in prompts.combine_reference_clause(override_axes=["identity"], char_name="X")
    # outfit override weaves in the garment label + inverts authority
    outfit = prompts.combine_reference_clause(override_axes=["outfit"], outfit="blue uniform")
    assert "OFF-MODEL" in outfit and "blue uniform" in outfit
    # identity override names the character as the design of record + keeps orientation
    ident = prompts.combine_reference_clause(override_axes=["identity"], char_name="Aria")
    assert "Aria" in ident and "authoritative design" in ident
    assert "facing away" in ident and "rotate" in ident  # orientation lock: never flip a back/profile view
    # multiple axes compose; style adds its line about palette
    multi = prompts.combine_reference_clause(override_axes=["identity", "outfit", "style"], char_name="Aria", outfit="armor")
    assert "Aria" in multi and "armor" in multi and "colour palette" in multi
    # unknown axes ignored → falls back to default hint (which includes the framing lock)
    bogus = prompts.combine_reference_clause(override_axes=["bogus"])
    assert prompts.COMBINE_CHARACTER_REFERENCE_CLAUSE in bogus and "OFF-MODEL" not in bogus


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


@pytest.mark.asyncio
async def test_export_all_panels_bundles_individual_pngs_in_zip():
    import zipfile
    page = str(uuid.uuid4())
    media_service.ingest_inline_bytes(page, _png(800, 1200), kind="image", mime="image/png")
    panels = [
        {"page_media_id": page, "box": {"x": 0, "y": 0, "w": 400, "h": 300}},
        {"page_media_id": page, "box": {"x": 0, "y": 300, "w": 600, "h": 200}},
    ]
    result, err = await _handle_export_all_panels({"panels": panels})
    assert err is None
    assert result["count"] == 2
    path = media_service.cached_path(result["mediaId"])
    assert path is not None and path.suffix == ".zip"
    with zipfile.ZipFile(path) as zf:
        names = zf.namelist()
        assert names == ["panel-0001.png", "panel-0002.png"]   # individual files, in order
        assert zf.read(names[0])[:8] == b"\x89PNG\r\n\x1a\n"    # each entry is a real PNG


@pytest.mark.asyncio
async def test_export_all_panels_errors():
    assert (await _handle_export_all_panels({}))[1] == "missing_panels"
    bad = {"panels": [{"page_media_id": "ghost", "box": {"x": 0, "y": 0, "w": 9, "h": 9}}]}
    assert (await _handle_export_all_panels(bad))[1] == "no_panels"
