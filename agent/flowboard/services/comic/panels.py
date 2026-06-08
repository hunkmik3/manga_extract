"""Node 1 — Import & Extract Panels (pure OpenCV XY-cut, no ML model).

Loads every page image in a folder (sorted by filename) and splits each page
into panels using a projection-profile / XY-cut: find horizontal content bands
separated by white gutters, then within each band find vertical content
columns. Works well on pages with clear white gutters; irregular / borderless
layouts are left as a future hook for a learned detector (YOLO) — see the
brief. The core ``content_runs`` / ``merge_close`` / ``detect_panels`` are the
exact algorithm validated on real comic pages.

This module is **pure** (no DB, no Flow, no network): it returns plain Python
data + encoded PNG bytes so it can be unit-tested in isolation. The worker
handler (``worker/processor._handle_extract_panels``) is what persists crops
into the media cache and shapes the node result.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import cv2
import numpy as np

logger = logging.getLogger(__name__)

# Read order matters → keep filename-sorted. Matches the brief's allowed set.
PAGE_EXTS = (".webp", ".png", ".jpg", ".jpeg", ".bmp")

Box = tuple[int, int, int, int]  # (x, y, w, h) in source-page pixels


# ── XY-cut core (verbatim algorithm from the brief) ──────────────────────────

def content_runs(empty_mask: np.ndarray, min_len: int) -> list[tuple[int, int]]:
    """Return [(start, end)] of runs that HAVE content, dropping runs shorter
    than ``min_len``."""
    idx = np.where(~empty_mask)[0]
    if len(idx) == 0:
        return []
    splits = np.where(np.diff(idx) > 1)[0]
    groups = np.split(idx, splits + 1)
    return [(int(g[0]), int(g[-1])) for g in groups if (g[-1] - g[0]) >= min_len]


def merge_close(runs: list[tuple[int, int]], min_gap: int) -> list[tuple[int, int]]:
    """Merge adjacent runs separated by less than ``min_gap`` (avoids a thin
    white streak splitting one panel in two)."""
    if not runs:
        return runs
    merged = [list(runs[0])]
    for s, e in runs[1:]:
        if s - merged[-1][1] < min_gap:
            merged[-1][1] = e
        else:
            merged.append([s, e])
    return [tuple(m) for m in merged]


_MAX_DEPTH = 12  # safety stop for the recursion

# A gutter line is a full-span row/col that is almost entirely ONE flat colour —
# near-white OR near-black. Detecting both makes the cut work whether the book
# uses white or black gutters, and (unlike a corner-sampled background) it keeps
# working when panels bleed into the page corners. Filled, coloured panel
# content is neither near-white nor near-black, so it reads as content.
GUTTER_WHITE = 230
GUTTER_BLACK = 25
GUTTER_UNIFORM_FRAC = 0.98


def _separator_lines(
    gray_region: np.ndarray, axis: int, white_thr: int, black_thr: int, uniform_frac: float
) -> np.ndarray:
    """Boolean per-line mask: True = a uniform near-white/near-black gutter line."""
    white = gray_region >= white_thr
    black = gray_region <= black_thr
    if axis == 0:
        wf = white.mean(axis=1)
        bf = black.mean(axis=1)
    else:
        wf = white.mean(axis=0)
        bf = black.mean(axis=0)
    return (wf >= uniform_frac) | (bf >= uniform_frac)


def _split_axis(
    gray_region: np.ndarray, axis: int, min_run: int, min_gap: int,
    white_thr: int, black_thr: int, uniform_frac: float,
) -> list[tuple[int, int]]:
    """Content runs along `axis` (0 = rows / vertical bands, 1 = cols), i.e. the
    spans *between* gutter lines."""
    sep = _separator_lines(gray_region, axis, white_thr, black_thr, uniform_frac)
    return merge_close(content_runs(sep, min_run), min_gap)


def _content_bbox(
    gray_region: np.ndarray, ox: int, oy: int, white_thr: int, black_thr: int, uniform_frac: float
) -> Optional[Box]:
    """Tighten a leaf region to the rows/cols that aren't pure gutter."""
    row_sep = _separator_lines(gray_region, 0, white_thr, black_thr, uniform_frac)
    col_sep = _separator_lines(gray_region, 1, white_thr, black_thr, uniform_frac)
    rows = np.where(~row_sep)[0]
    cols = np.where(~col_sep)[0]
    if len(rows) == 0 or len(cols) == 0:
        return None
    y0, y1 = int(rows[0]), int(rows[-1])
    x0, x1 = int(cols[0]), int(cols[-1])
    return (ox + x0, oy + y0, x1 - x0 + 1, y1 - y0 + 1)


def _xy_cut(
    gray_region: np.ndarray, ox: int, oy: int, prefer_axis: int,
    H0: int, W0: int, min_run_frac: float, min_gap_frac: float,
    white_thr: int, black_thr: int, uniform_frac: float,
    out: list[Box], depth: int = 0,
) -> None:
    """Recursive XY-cut. Try to split the region on the preferred axis, then the
    other; if either yields >1 content segment (separated by gutter lines),
    recurse each segment with the axes swapped. When neither axis splits, emit
    the region's tight content box as a leaf panel. Filled panels never
    over-split; full-bleed pages with no gutters stay a single panel."""
    h, w = gray_region.shape
    if depth <= _MAX_DEPTH and h > 1 and w > 1:
        for axis in (prefer_axis, 1 - prefer_axis):
            dim = H0 if axis == 0 else W0
            segs = _split_axis(
                gray_region, axis, int(min_run_frac * dim), max(1, int(min_gap_frac * dim)),
                white_thr, black_thr, uniform_frac,
            )
            if len(segs) > 1:
                for s, e in segs:
                    if axis == 0:
                        _xy_cut(gray_region[s:e + 1, :], ox, oy + s, 1 - axis, H0, W0,
                                min_run_frac, min_gap_frac, white_thr, black_thr, uniform_frac,
                                out, depth + 1)
                    else:
                        _xy_cut(gray_region[:, s:e + 1], ox + s, oy, 1 - axis, H0, W0,
                                min_run_frac, min_gap_frac, white_thr, black_thr, uniform_frac,
                                out, depth + 1)
                return
    box = _content_bbox(gray_region, ox, oy, white_thr, black_thr, uniform_frac)
    if box is not None:
        out.append(box)


def detect_panels(
    gray: np.ndarray,
    min_run_frac: float = 0.03,
    min_gap_frac: float = 0.004,
    white_thr: int = GUTTER_WHITE,
    black_thr: int = GUTTER_BLACK,
    uniform_frac: float = GUTTER_UNIFORM_FRAC,
) -> list[Box]:
    """Return [(x, y, w, h)] panel boxes, sorted top→bottom, left→right.

    Recursive XY-cut whose gutters are detected as uniform near-white/near-black
    full-span lines — handles white-gutter manga, black-gutter manhwa/webtoon,
    and staggered (non-grid) layouts. Full-bleed / borderless art has no gutters
    to cut on (stays one panel) and is the future YOLO hook (see brief)."""
    H, W = gray.shape
    out: list[Box] = []
    _xy_cut(gray, 0, 0, 0, H, W, min_run_frac, min_gap_frac, white_thr, black_thr, uniform_frac, out)
    out.sort(key=lambda b: (round(b[1] / (0.08 * H)), b[0]))
    return out


# ── Webtoon panel detection (band split + colour-aware crop) ─────────────────
# Tall vertical webtoons (Korean manhwa scrolls) don't have the gutter-grid
# structure XY-cut needs: panels are full-bleed/borderless, separated by large
# vertical whitespace, with B/W speech bubbles floating on that whitespace.
# XY-cut over-segments them (fragments a panel on its internal white, and emits
# each floating bubble as its own "panel"). Instead:
#   1. split into horizontal reading BANDS on full-width background rows, then
#   2. crop each band to its COLOURED art — so a bubble floating *beside* the
#      panel (in its own white space) is excluded, while a bubble drawn *over*
#      the art stays inside the art's bbox.
# Bands with no art (pure narration) are dropped; a dense grayscale band (B/W
# art) falls back to its full content extent so black-&-white pages still work.
WEBTOON_ASPECT = 2.0          # H/W ≥ this ⇒ treat as a webtoon (else XY-cut)
WEBTOON_BG_FRAC = 0.99        # a row/col is "background" if this fraction is flat
WEBTOON_MIN_BAND_FRAC = 0.02  # drop bands shorter than this fraction of height
WEBTOON_MERGE_GAP_FRAC = 0.02 # bridge gaps smaller than this (keep a panel whole)
WEBTOON_SAT_THR = 35          # HSV saturation above which a pixel counts as "art"
WEBTOON_MIN_COLOR_FRAC = 0.003  # colour ≥ this fraction of page ⇒ use the colour crop
WEBTOON_MIN_INK_FRAC = 0.04   # B/W fallback: keep band only if this dense (else text)
WEBTOON_PAD = 6               # px margin kept around the cropped art


def detect_panels_webtoon(
    bgr: np.ndarray,
    sat_thr: int = WEBTOON_SAT_THR,
    white_thr: int = GUTTER_WHITE,
    black_thr: int = GUTTER_BLACK,
    bg_frac: float = WEBTOON_BG_FRAC,
    min_band_frac: float = WEBTOON_MIN_BAND_FRAC,
    merge_gap_frac: float = WEBTOON_MERGE_GAP_FRAC,
    min_color_frac: float = WEBTOON_MIN_COLOR_FRAC,
    min_ink_frac: float = WEBTOON_MIN_INK_FRAC,
    pad: int = WEBTOON_PAD,
) -> list[Box]:
    """Return [(x, y, w, h)] art-panel boxes for a tall webtoon page (BGR in).

    One box per horizontal reading band, cropped to the band's coloured art so a
    B/W bubble floating beside a panel is excluded (a bubble drawn over the art
    stays in its bbox). Grayscale-art bands fall back to their full content
    extent; pure-text bands are dropped. Accepts a gray array too (all bands then
    take the B/W path)."""
    if bgr.ndim == 2:  # tolerate a gray array
        bgr = cv2.cvtColor(bgr, cv2.COLOR_GRAY2BGR)
    H, W = bgr.shape[:2]
    gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
    row_bg = ((gray >= white_thr).mean(axis=1) >= bg_frac) | (
        (gray <= black_thr).mean(axis=1) >= bg_frac
    )
    bands = merge_close(
        content_runs(row_bg, int(min_band_frac * H)), max(1, int(merge_gap_frac * H))
    )
    hsv = cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV)
    colored = (((hsv[:, :, 1] > sat_thr) & (hsv[:, :, 2] > 30)).astype(np.uint8)) * 255
    # de-speckle so a few stray colour pixels can't inflate the crop
    colored = cv2.morphologyEx(
        colored, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_RECT, (9, 9))
    )
    not_bg = (gray < white_thr) & (gray > black_thr)
    out: list[Box] = []
    for y0, y1 in bands:
        cb = colored[y0:y1 + 1, :] > 0
        if cb.sum() >= min_color_frac * H * W:
            ys, xs = np.where(cb)                       # colour crop (drops side bubbles)
            x0, x1 = int(xs.min()), int(xs.max())
            yy0, yy1 = y0 + int(ys.min()), y0 + int(ys.max())
        else:                                           # no colour → B/W band
            ink = not_bg[y0:y1 + 1, :]
            if ink.sum() < min_ink_frac * (y1 - y0 + 1) * W:
                continue                                # sparse ⇒ pure text, drop
            cols = np.where(ink.any(axis=0))[0]         # dense B/W art ⇒ content extent
            if len(cols) == 0:
                continue
            x0, x1, yy0, yy1 = int(cols[0]), int(cols[-1]), y0, y1
        bx, by = max(0, x0 - pad), max(0, yy0 - pad)
        out.append((bx, by, min(W, x1 + pad) - bx, min(H, yy1 + pad) - by))
    out.sort(key=lambda b: (round(b[1] / (0.08 * H)), b[0]))
    return out


# ── Folder / page helpers ────────────────────────────────────────────────────

def iter_page_paths(folder: str | Path) -> list[Path]:
    """All supported page images in ``folder``, sorted by filename (the read
    order). Non-recursive — one flat folder of pages, as the brief specifies."""
    root = Path(folder)
    if not root.is_dir():
        raise NotADirectoryError(f"not a directory: {folder}")
    paths = [p for p in root.iterdir() if p.is_file() and p.suffix.lower() in PAGE_EXTS]
    return sorted(paths, key=lambda p: p.name)


def load_bgr(path: str | Path) -> np.ndarray:
    """Load an image as BGR (cv2 reads .webp/.png/.jpg/.bmp natively)."""
    img = cv2.imread(str(path), cv2.IMREAD_COLOR)
    if img is None:
        raise ValueError(f"could not read image: {path}")
    return img


def decode_bgr(data: bytes) -> np.ndarray:
    """Decode image bytes (from the media cache) to BGR."""
    img = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
    if img is None:
        raise ValueError("could not decode image bytes")
    return img


def stitch_2x2(
    panels_bgr: list[Optional[np.ndarray]], cell_w: int = 540, gutter: int = 16,
    fill: tuple[int, int, int] = (255, 255, 255),
) -> np.ndarray:
    """Arrange up to 4 panels into a tight 2×2 grid (reading order TL, TR, BL,
    BR) with thin white gutters. Cell height tracks the panels' median aspect so
    each panel fills its cell with minimal empty margin (a big improvement over
    fixed 9:16 cells, which left huge bands the model would fill with junk).
    Panels are contain-fit (no cropping → characters preserved). The model then
    cleans text + extends backgrounds into the gutters and reframes to 9:16."""
    present = [p for p in panels_bgr[:4] if p is not None and getattr(p, "size", 0)]
    aspects = sorted(p.shape[1] / p.shape[0] for p in present) if present else [1.4]
    cell_ar = aspects[len(aspects) // 2]  # median width/height
    cell_h = max(1, int(round(cell_w / cell_ar)))
    W = cell_w * 2 + gutter * 3
    H = cell_h * 2 + gutter * 3
    canvas = np.full((H, W, 3), fill, np.uint8)
    for i, pn in enumerate(panels_bgr[:4]):
        if pn is None or getattr(pn, "size", 0) == 0:
            continue
        ph, pw = pn.shape[:2]
        s = min(cell_w / pw, cell_h / ph)
        nw, nh = max(1, int(pw * s)), max(1, int(ph * s))
        resized = cv2.resize(pn, (nw, nh))
        r, c = divmod(i, 2)
        cx, cy = gutter + c * (cell_w + gutter), gutter + r * (cell_h + gutter)
        x0, y0 = cx + (cell_w - nw) // 2, cy + (cell_h - nh) // 2
        canvas[y0:y0 + nh, x0:x0 + nw] = resized
    return canvas


def pad_to_aspect(bgr: np.ndarray, w_ratio: int = 9, h_ratio: int = 16, *, mode: str = "constant") -> np.ndarray:
    """Pad the image to an exact w:h aspect (default 9:16).

    ``mode="constant"`` → black bars (a safety letterbox to guarantee the frame).
    ``mode="replicate"`` → stretch edge pixels into the new area, a better seed
    for the model to *outpaint* over than flat bars (used to pre-pad the canvas
    before the 9:16 generate-extend step)."""
    H, W = bgr.shape[:2]
    target = w_ratio / h_ratio
    cur = W / H
    if abs(cur - target) < 0.02 * target:
        return bgr
    border = cv2.BORDER_REPLICATE if mode == "replicate" else cv2.BORDER_CONSTANT
    kw = {} if mode == "replicate" else {"value": (0, 0, 0)}
    if cur > target:  # too wide → grow height (top/bottom)
        new_h = int(round(W / target))
        pad = max(0, new_h - H)
        return cv2.copyMakeBorder(bgr, pad // 2, pad - pad // 2, 0, 0, border, **kw)
    new_w = int(round(H * target))  # too tall → grow width (left/right)
    pad = max(0, new_w - W)
    return cv2.copyMakeBorder(bgr, 0, 0, pad // 2, pad - pad // 2, border, **kw)


def encode_png(bgr: np.ndarray) -> bytes:
    ok, buf = cv2.imencode(".png", bgr)
    if not ok:
        raise ValueError("PNG encode failed")
    return buf.tobytes()


def draw_overview(bgr: np.ndarray, boxes: list[Box]) -> np.ndarray:
    """Copy of the page with numbered panel boxes drawn — the QA debug flag."""
    out = bgr.copy()
    for i, (x, y, w, h) in enumerate(boxes):
        cv2.rectangle(out, (x, y), (x + w, y + h), (0, 0, 255), 3)
        cv2.putText(out, str(i + 1), (x + 6, y + 28), cv2.FONT_HERSHEY_SIMPLEX,
                    0.9, (0, 0, 255), 2, cv2.LINE_AA)
    return out


# ── Structured results ───────────────────────────────────────────────────────

@dataclass
class PanelCrop:
    page_index: int          # 0-based page order within the folder
    page_name: str           # source filename
    panel_index: int         # 0-based panel order within the page
    box: Box                 # (x, y, w, h) in source-page pixels
    png_bytes: bytes = field(repr=False)


@dataclass
class PageResult:
    page_index: int
    page_name: str
    panel_count: int
    panels: list[PanelCrop]
    overview_png: Optional[bytes] = field(default=None, repr=False)
    error: Optional[str] = None


def detect_boxes(bgr: np.ndarray, detector: str = "heuristic") -> list[Box]:
    """Detect panel boxes on a BGR page with the chosen backend:

      * ``"heuristic"`` — pure-OpenCV XY-cut (default; no heavy deps). Best for
                          gutter-separated grid manga.
      * ``"webtoon"``   — full-width band segmentation for tall vertical scrolls
                          (manhwa/webtoon): borderless panels on whitespace.
      * ``"ml"``        — YOLO frame detector (raises if the optional ML deps
                          aren't installed).
      * ``"auto"``      — pick by page shape: a tall page (H/W ≥ WEBTOON_ASPECT)
                          uses webtoon band segmentation; otherwise run heuristic
                          + YOLO and keep whichever finds more (falling back to
                          heuristic if the ML backend is unavailable).
    """
    gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
    H, W = gray.shape
    if detector == "heuristic":
        return detect_panels(gray)
    if detector == "webtoon":
        return detect_panels_webtoon(bgr)

    from flowboard.services.comic import panel_ml

    if detector == "ml":
        return panel_ml.detect_panels_ml(bgr)
    if detector == "auto":
        # Tall vertical webtoons: XY-cut/YOLO over-segment them — band+colour
        # crop keeps each scene whole. Fall through if it finds nothing.
        if H / max(1, W) >= WEBTOON_ASPECT:
            wt = detect_panels_webtoon(bgr)
            if wt:
                return wt
        heur = detect_panels(gray)
        try:
            ml = panel_ml.detect_panels_ml(bgr)
        except panel_ml.MLUnavailable as exc:
            logger.info("auto detector: ML unavailable, using heuristic (%s)", exc)
            return heur
        return ml if len(ml) > len(heur) else heur
    raise ValueError(f"unknown detector: {detector!r}")


def extract_page(
    path: str | Path, page_index: int, *, debug: bool = False, detector: str = "heuristic"
) -> PageResult:
    """Split one page into panel crops. Never raises on a bad page — records the
    error on the PageResult so a batch keeps going."""
    name = Path(path).name
    try:
        bgr = load_bgr(path)
    except Exception as exc:  # noqa: BLE001
        logger.warning("extract_page: cannot read %s: %s", name, exc)
        return PageResult(page_index, name, 0, [], error=str(exc)[:200])

    boxes = detect_boxes(bgr, detector)
    crops: list[PanelCrop] = []
    for j, (x, y, w, h) in enumerate(boxes):
        crop = bgr[y:y + h, x:x + w]
        crops.append(PanelCrop(page_index, name, j, (x, y, w, h), encode_png(crop)))
    overview = draw_overview(bgr, boxes) if debug else None
    return PageResult(
        page_index, name, len(crops), crops,
        overview_png=encode_png(overview) if overview is not None else None,
    )


def extract_folder(
    folder: str | Path, *, debug: bool = False, detector: str = "heuristic"
) -> list[PageResult]:
    """Extract panels from every page in ``folder`` (filename order)."""
    pages = iter_page_paths(folder)
    if not pages:
        raise FileNotFoundError(f"no page images ({', '.join(PAGE_EXTS)}) in {folder}")
    return [extract_page(p, i, debug=debug, detector=detector) for i, p in enumerate(pages)]
