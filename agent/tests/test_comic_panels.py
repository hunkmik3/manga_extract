"""Tests for Node 1 — panel extraction (XY-cut) + the extract_panels worker
handler. Synthesizes comic-like pages (white background, dark filled panels
separated by white gutters) so the panel count is known exactly.
"""

import numpy as np
import cv2
import pytest

from flowboard.services.comic import panels as panel_svc
from flowboard.worker.processor import _handle_extract_panels


# ── synthetic page builders ──────────────────────────────────────────────────

def _blank(h=1500, w=1000):
    """White page (uint8 BGR)."""
    return np.full((h, w, 3), 255, np.uint8)


def _fill(img, x, y, w, h, val=90):
    """Paint a dark filled panel (plenty of 'ink') into the page."""
    img[y:y + h, x:x + w] = val


def _grid_2x2(path):
    """4 panels in a 2x2 grid with ~60px white gutters + margins."""
    img = _blank()
    # rows at y=40..680 and y=760..1400 ; cols at x=40..480 and x=520..960
    _fill(img, 40, 40, 440, 640)
    _fill(img, 520, 40, 440, 640)
    _fill(img, 40, 760, 440, 640)
    _fill(img, 520, 760, 440, 640)
    cv2.imwrite(str(path), img)


def _single(path):
    """1 full-bleed-ish panel."""
    img = _blank()
    _fill(img, 60, 60, 880, 1380)
    cv2.imwrite(str(path), img)


# ── pure algorithm ───────────────────────────────────────────────────────────

def test_detect_panels_counts_2x2_grid():
    img = _blank()
    _fill(img, 40, 40, 440, 640)
    _fill(img, 520, 40, 440, 640)
    _fill(img, 40, 760, 440, 640)
    _fill(img, 520, 760, 440, 640)
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    boxes = panel_svc.detect_panels(gray)
    assert len(boxes) == 4
    # reading order: top row first (both top boxes before either bottom box)
    ys = [b[1] for b in boxes]
    assert ys[0] < 760 and ys[1] < 760 and ys[2] >= 760 and ys[3] >= 760
    # left-before-right within a row
    assert boxes[0][0] < boxes[1][0]


def test_detect_panels_single():
    img = _blank()
    _fill(img, 60, 60, 880, 1380)
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    boxes = panel_svc.detect_panels(gray)
    assert len(boxes) == 1
    x, y, w, h = boxes[0]
    assert w > 800 and h > 1300


def test_detect_panels_dark_background_black_gutters():
    """Manhwa/webtoon: black background + black gutters, bright filled panels.
    The old white-gutter assumption returned 1 panel here — regression guard."""
    img = np.zeros((1500, 1000, 3), np.uint8)  # black page

    def bright(x, y, w, h):
        img[y:y + h, x:x + w] = 200  # a "lit" panel

    # top band: 2 panels side by side
    bright(40, 40, 440, 500)
    bright(520, 40, 440, 500)
    # bottom band: tall left panel + right column split into 2 stacked panels
    bright(40, 620, 440, 840)       # left, full band height
    bright(520, 620, 440, 380)      # right-top
    bright(520, 1080, 440, 380)     # right-bottom

    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    boxes = panel_svc.detect_panels(gray)
    assert len(boxes) == 5, boxes
    # reading order: 2 in the top band before the 3 in the bottom band
    assert sum(1 for b in boxes if b[1] < 600) == 2
    assert sum(1 for b in boxes if b[1] >= 600) == 3


# ── webtoon panel detection (band split + colour-aware crop) ─────────────────

def _webtoon_gray_page():
    """Tall B/W webtoon (1000x3000): 3 dense grayscale art bands + a tiny floating
    blob. No colour ⇒ the B/W fallback keeps each dense band as one box (a white
    caption inside band A must not split it); the tiny blob is dropped (below the
    min-band height)."""
    img = np.full((3000, 1000, 3), 255, np.uint8)
    _fill(img, 100, 200, 800, 700)        # band A (y 200..900)
    img[600:850, 150:450] = 255           # white caption inside band A
    _fill(img, 100, 1700, 800, 600)       # band B
    _fill(img, 100, 2500, 800, 400)       # band C
    img[1200:1250, 400:550] = 60          # tiny floating blob in the gap
    return img


def _webtoon_color_page():
    """Tall colour webtoon (1000x2200): one band with a COLOURED art panel on the
    left and a B/W speech bubble floating to its right (its own white space). The
    crop must keep the art and exclude the side bubble."""
    img = np.full((2200, 1000, 3), 255, np.uint8)
    img[300:1000, 100:500] = (0, 140, 255)   # saturated (orange) art block, left
    img[450:800, 620:900] = 255              # white bubble, right
    img[470:780, 640:880] = 60               # ... dark "text" inside the bubble
    return img


def test_detect_panels_webtoon_bw_fallback_three_bands():
    boxes = panel_svc.detect_panels_webtoon(_webtoon_gray_page())
    assert len(boxes) == 3, boxes  # 3 dense bands; tiny blob dropped
    assert [b[1] for b in boxes] == sorted(b[1] for b in boxes)  # top→bottom
    a = boxes[0]  # band A one box — the inner white caption doesn't split it
    assert a[1] <= 210 and a[1] + a[3] >= 890, a


def test_detect_panels_webtoon_excludes_side_bubble():
    boxes = panel_svc.detect_panels_webtoon(_webtoon_color_page())
    assert len(boxes) == 1, boxes
    x, y, w, h = boxes[0]
    assert x + w < 600, boxes        # cropped to the art; the side B/W bubble is out
    assert x <= 110 and w >= 380     # but it does cover the colour art block


def test_detect_boxes_webtoon_mode_matches_helper():
    img = _webtoon_color_page()
    assert panel_svc.detect_boxes(img, "webtoon") == panel_svc.detect_panels_webtoon(img)


def test_detect_boxes_auto_routes_tall_pages_to_webtoon():
    """A tall page (H/W ≥ WEBTOON_ASPECT) uses webtoon detection under 'auto' —
    and never touches the ML backend (early return)."""
    img = _webtoon_gray_page()  # 1000x3000 → H/W = 3.0
    assert panel_svc.detect_boxes(img, "auto") == panel_svc.detect_panels_webtoon(img)


def test_content_runs_and_merge_close():
    # empty mask: True = empty. content at indices 2..5 and 10..12
    mask = np.array([True, True, False, False, False, False, True, True, True, True,
                     False, False, False, True])
    runs = panel_svc.content_runs(mask, min_len=1)
    assert runs == [(2, 5), (10, 12)]
    # gap between runs is 10-5 = 5; merge if min_gap > 5
    assert panel_svc.merge_close(runs, min_gap=6) == [(2, 12)]
    assert panel_svc.merge_close(runs, min_gap=3) == [(2, 5), (10, 12)]


# ── folder loading (incl. .webp) ─────────────────────────────────────────────

def test_iter_page_paths_sorted_and_webp(tmp_path):
    _grid_2x2(tmp_path / "page_02.png")
    _single(tmp_path / "page_01.webp")  # webp must be readable
    (tmp_path / "notes.txt").write_text("ignore me")
    paths = panel_svc.iter_page_paths(tmp_path)
    assert [p.name for p in paths] == ["page_01.webp", "page_02.png"]


def test_extract_folder_counts(tmp_path):
    _single(tmp_path / "p01.webp")
    _grid_2x2(tmp_path / "p02.png")
    pages = panel_svc.extract_folder(tmp_path, debug=True)
    assert [pr.panel_count for pr in pages] == [1, 4]
    # debug overview produced + crops carry valid PNG bytes
    assert all(pr.overview_png for pr in pages)
    first_crop = pages[1].panels[0]
    assert first_crop.png_bytes[:8] == b"\x89PNG\r\n\x1a\n"
    assert first_crop.box[2] > 0 and first_crop.box[3] > 0


def test_extract_folder_empty_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        panel_svc.extract_folder(tmp_path)


def test_extract_folder_bad_dir():
    with pytest.raises(NotADirectoryError):
        panel_svc.extract_folder("/no/such/folder/xyz")


# ── worker handler (uses temp DB + storage from conftest) ─────────────────────

@pytest.mark.asyncio
async def test_handle_extract_panels_caches_crops(tmp_path):
    from flowboard.services import media as media_service

    _single(tmp_path / "p01.png")
    _grid_2x2(tmp_path / "p02.png")
    result, err = await _handle_extract_panels({"folder": str(tmp_path), "debug": True, "__node_id": 7})

    assert err is None
    assert result["page_count"] == 2
    assert result["panel_count"] == 5  # 1 + 4
    assert len(result["panels"]) == 5
    assert result["node_id"] == 7
    # global idx is contiguous and ordered
    assert [p["idx"] for p in result["panels"]] == [0, 1, 2, 3, 4]
    # every panel crop is fetchable from the local cache
    for p in result["panels"]:
        st = media_service.status(p["mediaId"])
        assert st.get("available") is True, st
    # debug overview persisted per page
    assert all(pg["debugMediaId"] for pg in result["pages"])


@pytest.mark.asyncio
async def test_handle_extract_panels_missing_folder():
    result, err = await _handle_extract_panels({})
    assert err == "missing_folder"


@pytest.mark.asyncio
async def test_handle_extract_panels_bad_dir():
    result, err = await _handle_extract_panels({"folder": "/no/such/dir/zzz"})
    assert err and "not a directory" in err.lower()
