"""End-to-end tests for the 3-node comic pipeline tasks:
import_pages (Node 1) → detect_page_panels (Node 2) → crop_panels (Node 3),
including a hand-edited-boxes path. Uses the temp DB + storage from conftest.
"""

import cv2
import numpy as np
import pytest

from flowboard.services import media as media_service
from flowboard.worker.processor import (
    _handle_crop_panels,
    _handle_detect_page_panels,
    _handle_import_pages,
)


def _single(path):
    img = np.full((1500, 1000, 3), 255, np.uint8)
    img[60:1440, 60:940] = 90
    cv2.imwrite(str(path), img)


def _grid_2x2(path):
    img = np.full((1500, 1000, 3), 255, np.uint8)
    for (x, y) in [(40, 40), (520, 40), (40, 760), (520, 760)]:
        img[y:y + 640, x:x + 440] = 90
    cv2.imwrite(str(path), img)


@pytest.mark.asyncio
async def test_full_pipeline_import_detect_crop(tmp_path):
    _single(tmp_path / "p01.png")
    _grid_2x2(tmp_path / "p02.png")

    # Node 1 — import full pages (no split)
    imp, err = await _handle_import_pages({"folder": str(tmp_path), "__node_id": 1})
    assert err is None and imp["page_count"] == 2
    assert all(media_service.status(p["mediaId"]).get("available") for p in imp["pages"])
    assert all(p["w"] > 0 and p["h"] > 0 for p in imp["pages"])  # dims captured

    # Node 2 — detect boxes per page
    det, err = await _handle_detect_page_panels({"pages": imp["pages"], "detector": "heuristic"})
    assert err is None
    by_name = {p["name"]: p for p in det["pages"]}
    assert len(by_name["p01.png"]["boxes"]) == 1
    assert len(by_name["p02.png"]["boxes"]) == 4
    assert det["box_count"] == 5
    # boxes carry stable ids
    assert all("id" in b for p in det["pages"] for b in p["boxes"])

    # Node 3 — crop the boxes into panels
    crop, err = await _handle_crop_panels({"pages": det["pages"]})
    assert err is None and crop["panel_count"] == 5
    assert [p["idx"] for p in crop["panels"]] == [0, 1, 2, 3, 4]
    for p in crop["panels"]:
        st = media_service.status(p["mediaId"])
        assert st.get("available") is True


@pytest.mark.asyncio
async def test_crop_respects_hand_edited_boxes(tmp_path):
    _single(tmp_path / "p01.png")
    imp, _ = await _handle_import_pages({"folder": str(tmp_path)})
    page = imp["pages"][0]

    # Simulate the manual editor: replace auto boxes with two hand-drawn ones.
    page = {**page, "boxes": [
        {"id": "a", "x": 10, "y": 10, "w": 300, "h": 400},
        {"id": "b", "x": 400, "y": 500, "w": 250, "h": 250},
    ]}
    crop, err = await _handle_crop_panels({"pages": [page]})
    assert err is None and crop["panel_count"] == 2
    # crop boxes are clamped to page bounds and match what we drew
    boxes = sorted((p["box"]["x"], p["box"]["y"]) for p in crop["panels"])
    assert boxes == [(10, 10), (400, 500)]


@pytest.mark.asyncio
async def test_crop_clamps_out_of_bounds_box(tmp_path):
    _single(tmp_path / "p01.png")
    imp, _ = await _handle_import_pages({"folder": str(tmp_path)})
    page = {**imp["pages"][0], "boxes": [{"id": "x", "x": 900, "y": 1400, "w": 5000, "h": 5000}]}
    crop, err = await _handle_crop_panels({"pages": [page]})
    assert err is None and crop["panel_count"] == 1
    b = crop["panels"][0]["box"]
    assert b["x"] + b["w"] <= 1000 and b["y"] + b["h"] <= 1500  # clamped to page


@pytest.mark.asyncio
async def test_detect_handles_unreadable_page():
    det, err = await _handle_detect_page_panels(
        {"pages": [{"idx": 0, "name": "ghost.png", "mediaId": "00000000-0000-0000-0000-000000000000"}],
         "detector": "heuristic"}
    )
    assert err is None
    assert det["pages"][0]["error"] == "page_unreadable"
    assert det["pages"][0]["boxes"] == []


@pytest.mark.asyncio
async def test_error_cases(tmp_path):
    assert (await _handle_import_pages({}))[1] == "missing_folder"
    assert (await _handle_import_pages({"folder": str(tmp_path)}))[1].startswith("no page images")
    assert (await _handle_detect_page_panels({}))[1] == "missing_pages"
    assert (await _handle_detect_page_panels({"pages": [{}], "detector": "x"}))[1] == "invalid_detector:x"
    assert (await _handle_crop_panels({}))[1] == "missing_pages"
