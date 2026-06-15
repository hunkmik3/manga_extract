"""Tests for character-sheet auto-segmentation (split a turnaround sheet into
face + full-body view crops)."""

import uuid

import cv2
import numpy as np
import pytest

from flowboard.services import media as media_service
from flowboard.services.comic import sheet as sheet_svc
from flowboard.worker.processor import _handle_segment_character_sheet


def _sheet() -> np.ndarray:
    """A synthetic turnaround sheet on white: one tall full-body region + two
    compact head regions, separated by wide white gutters."""
    img = np.full((800, 1000, 3), 255, np.uint8)
    cv2.rectangle(img, (100, 100), (220, 220), (40, 40, 40), -1)   # face 1 (≈1:1)
    cv2.rectangle(img, (320, 100), (440, 220), (40, 40, 40), -1)   # face 2 (≈1:1)
    cv2.rectangle(img, (700, 80), (820, 560), (40, 40, 40), -1)    # body (≈1:4 tall)
    return img


def _png(img: np.ndarray) -> bytes:
    return cv2.imencode(".png", img)[1].tobytes()


def test_segment_classifies_faces_and_body():
    seg = sheet_svc.segment_character_sheet(_sheet())
    assert len(seg["bodies"]) == 1            # the tall region
    assert len(seg["faces"]) == 2             # the two compact regions
    # body region is the tall one
    bx, by, bw, bh = seg["bodies"][0]
    assert bh / bw >= sheet_svc.BODY_MIN_ASPECT
    # faces returned left→right in reading order
    assert seg["faces"][0][0] < seg["faces"][1][0]


def test_segment_drops_specks():
    img = np.full((800, 1000, 3), 255, np.uint8)
    cv2.rectangle(img, (700, 80), (820, 560), (40, 40, 40), -1)    # body
    cv2.rectangle(img, (10, 10), (16, 16), (0, 0, 0), -1)          # tiny speck
    seg = sheet_svc.segment_character_sheet(img)
    assert len(seg["bodies"]) == 1
    assert seg["faces"] == []                 # speck below MIN_AREA_FRAC


def test_segment_drops_face_inside_body():
    """A head drawn on top of a full-body silhouette is the body view's own head,
    not a standalone face view → it must not be reported as a face."""
    img = np.full((600, 600, 3), 255, np.uint8)
    cv2.rectangle(img, (250, 50), (370, 520), (40, 40, 40), -1)   # tall body
    # (the head is part of the same connected component, so this mainly checks
    # the containment filter never turns a body sub-region into a face)
    seg = sheet_svc.segment_character_sheet(img)
    assert len(seg["bodies"]) == 1 and seg["faces"] == []


@pytest.mark.asyncio
async def test_handle_segment_sheet_crops_and_ingests_views():
    mid = str(uuid.uuid4())
    media_service.ingest_inline_bytes(mid, _png(_sheet()), kind="image", mime="image/png")
    result, err = await _handle_segment_character_sheet({"media_id": mid})
    assert err is None
    views = result["views"]
    assert len(views) == 3
    kinds = sorted(v["kind"] for v in views)
    assert kinds == ["body", "face", "face"]
    for v in views:
        assert media_service.status(v["mediaId"]).get("available") is True
        png = media_service.cached_path(v["mediaId"]).read_bytes()
        assert png[:8] == b"\x89PNG\r\n\x1a\n"           # real PNG crop
        assert v["box"]["w"] > 0 and v["box"]["h"] > 0


@pytest.mark.asyncio
async def test_handle_segment_sheet_errors():
    assert (await _handle_segment_character_sheet({}))[1] == "missing_media_id"
    assert (await _handle_segment_character_sheet({"media_id": "ghost"}))[1] == "no_source_image"
    blank = str(uuid.uuid4())
    media_service.ingest_inline_bytes(blank, _png(np.full((400, 400, 3), 255, np.uint8)),
                                      kind="image", mime="image/png")
    assert (await _handle_segment_character_sheet({"media_id": blank}))[1] == "no_views_found"
