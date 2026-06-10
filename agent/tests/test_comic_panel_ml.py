"""Tests for the optional YOLO panel detector + the detector-selection logic.

The real model (ultralytics/torch) is never loaded here — we stub
``panel_ml._load_model`` / ``detect_panels_ml`` so the wiring (box conversion,
sort order, auto-fallback, ml-unavailable error) is verified without heavy deps.
"""

from types import SimpleNamespace
from unittest.mock import patch

import numpy as np
import pytest

from flowboard.services.comic import panel_ml, panels as P


class _FakeBoxes:
    def __init__(self, xyxy, cls):
        self.xyxy = SimpleNamespace(cpu=lambda: SimpleNamespace(numpy=lambda: np.array(xyxy, float)))
        self.cls = SimpleNamespace(cpu=lambda: SimpleNamespace(numpy=lambda: np.array(cls, float)))

    def __len__(self):
        return len(self.cls.cpu().numpy())


class _FakeModel:
    """Minimal stand-in for ultralytics.YOLO."""
    names = {0: "body", 1: "face", 2: "frame", 3: "text"}

    def __init__(self, boxes_xyxy, classes):
        self._b = _FakeBoxes(boxes_xyxy, classes)

    def predict(self, *a, **k):
        return [SimpleNamespace(boxes=self._b)]


@pytest.fixture(autouse=True)
def _reset_ml():
    panel_ml._model = None
    panel_ml._frame_id = None
    panel_ml._load_failed = None
    yield
    panel_ml._model = None
    panel_ml._frame_id = None
    panel_ml._load_failed = None


def test_detect_panels_ml_filters_frame_class_and_sorts():
    # two frames (cls 2) + one text (cls 3, must be dropped); out of reading order
    fake = _FakeModel(
        boxes_xyxy=[[10, 800, 200, 1000], [10, 40, 200, 300], [50, 50, 90, 90]],
        classes=[2, 2, 3],
    )
    with patch.object(panel_ml, "_load_model", return_value=fake):
        panel_ml._frame_id = 2  # _load_model would set this; stubbed here
        boxes = panel_ml.detect_panels_ml(np.zeros((1100, 300, 3), np.uint8))
    assert len(boxes) == 2                 # text box dropped
    assert boxes[0][1] < boxes[1][1]        # sorted top→bottom
    assert boxes[0] == (10, 40, 190, 260)   # (x,y,w,h) conversion from xyxy


def test_ml_unavailable_when_ultralytics_missing():
    import builtins
    real_import = builtins.__import__

    def fake_import(name, *a, **k):
        if name == "ultralytics":
            raise ImportError("no ultralytics")
        return real_import(name, *a, **k)

    with patch("builtins.__import__", side_effect=fake_import):
        with pytest.raises(panel_ml.MLUnavailable):
            panel_ml._load_model()
    assert panel_ml.is_available() is False  # cached failure


def test_detect_boxes_auto_falls_back_to_heuristic_when_ml_unavailable():
    img = np.zeros((1500, 1000, 3), np.uint8)
    for (x, y) in [(40, 40), (520, 40), (40, 760), (520, 760)]:
        img[y:y + 500, x:x + 440] = 200  # 4 bright panels on black → heuristic finds 4
    with patch.object(panel_ml, "detect_panels_ml", side_effect=panel_ml.MLUnavailable("no deps")):
        boxes = P.detect_boxes(img, detector="auto")
    assert len(boxes) == 4  # heuristic result used


def test_detect_boxes_auto_prefers_ml_when_it_finds_more():
    img = np.zeros((1500, 1000, 3), np.uint8)
    img[100:1400, 100:900] = 180  # heuristic → 1 panel
    ml_boxes = [(0, 0, 500, 700), (0, 750, 500, 700), (520, 0, 480, 1450)]
    with patch.object(panel_ml, "detect_panels_ml", return_value=ml_boxes):
        boxes = P.detect_boxes(img, detector="auto")
    assert boxes == ml_boxes  # ML (3) > heuristic (1)


def test_detect_boxes_ml_propagates_unavailable():
    with patch.object(panel_ml, "detect_panels_ml", side_effect=panel_ml.MLUnavailable("x")):
        with pytest.raises(panel_ml.MLUnavailable):
            P.detect_boxes(np.zeros((100, 100, 3), np.uint8), detector="ml")


def test_detect_boxes_unknown_detector():
    with pytest.raises(ValueError):
        P.detect_boxes(np.zeros((100, 100, 3), np.uint8), detector="nope")


@pytest.mark.asyncio
async def test_handler_ml_unavailable_surfaces_clear_error(tmp_path):
    import cv2
    from flowboard.worker.processor import _handle_extract_panels
    # one valid page so extraction reaches the detector
    img = np.full((600, 400, 3), 255, np.uint8)
    img[40:560, 40:360] = 90
    cv2.imwrite(str(tmp_path / "p01.png"), img)
    with patch.object(panel_ml, "detect_panels_ml", side_effect=panel_ml.MLUnavailable("install .[ml]")):
        result, err = await _handle_extract_panels({"folder": str(tmp_path), "detector": "ml"})
    assert err and err.startswith("ml_unavailable")


@pytest.mark.asyncio
async def test_handler_rejects_bad_detector(tmp_path):
    from flowboard.worker.processor import _handle_extract_panels
    result, err = await _handle_extract_panels({"folder": str(tmp_path), "detector": "bogus"})
    assert err == "invalid_detector:bogus"


def test_detect_panels_hybrid_backfills_panels_ml_missed():
    """ML gives the true (full) panel extents but MISSES a band; the heuristic
    band that ML left uncovered is backfilled, the one ML covers is dropped."""
    img = np.zeros((4000, 1280, 3), np.uint8)
    ml_boxes = [(0, 2000, 1280, 800)]                       # ML: creature panel (full width)
    wt_boxes = [(80, 0, 780, 1300), (280, 2100, 660, 700)]  # heuristic: missed top panel + cropped creature
    with patch.object(panel_ml, "detect_panels_ml", return_value=ml_boxes), \
         patch.object(P, "detect_panels_webtoon", return_value=wt_boxes):
        boxes = P.detect_panels_hybrid(img)
    assert (0, 2000, 1280, 800) in boxes        # ML's full extent kept
    assert (80, 0, 780, 1300) in boxes          # uncovered band backfilled
    assert (280, 2100, 660, 700) not in boxes   # already covered by ML → no duplicate
    assert len(boxes) == 2


def test_detect_panels_hybrid_falls_back_to_heuristic_without_ml():
    img = np.zeros((4000, 1280, 3), np.uint8)
    wt_boxes = [(0, 0, 1280, 1000)]
    with patch.object(panel_ml, "detect_panels_ml", side_effect=panel_ml.MLUnavailable("no deps")), \
         patch.object(P, "detect_panels_webtoon", return_value=wt_boxes):
        assert P.detect_panels_hybrid(img) == wt_boxes


def test_detect_boxes_hybrid_dispatch():
    img = np.zeros((4000, 1280, 3), np.uint8)
    with patch.object(P, "detect_panels_hybrid", return_value=[(1, 2, 3, 4)]) as h:
        boxes = P.detect_boxes(img, detector="hybrid")
    h.assert_called_once()
    assert boxes == [(1, 2, 3, 4)]


def test_detect_boxes_auto_tall_uses_hybrid():
    img = np.zeros((4000, 1000, 3), np.uint8)  # H/W = 4 ≥ WEBTOON_ASPECT → webtoon path
    with patch.object(P, "detect_panels_hybrid", return_value=[(0, 0, 10, 10)]) as h:
        boxes = P.detect_boxes(img, detector="auto")
    h.assert_called_once()
    assert boxes == [(0, 0, 10, 10)]
