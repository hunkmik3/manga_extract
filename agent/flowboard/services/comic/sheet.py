"""Character-sheet segmentation.

Split an uploaded turnaround / reference sheet (the clean, multi-view "model
sheet" a creator provides for a main character) into individual keyed views —
face close-ups + full-body views — that become the character's FROZEN canon
references.

Why: a single full-body sheet image is a poor reference for a tight close-up
panel (the model imports the sheet's framing — see the prompt framing-lock).
Cropping the sheet into separate face / body views lets a close-up pull the
face view and a wide shot pull the full-body view, at the right scale.

Approach (pure OpenCV — no heavy ML dep): turnaround sheets are conventionally
drawn on a near-white background with the views separated by white gutters. We
mask out the near-white background, seal line-art stroke gaps with a small
morphological close (small enough NOT to bridge separate views), find connected
components, drop specks, and classify each region by aspect ratio: tall regions
are full-body views, compact ones are face / head views.
"""
from __future__ import annotations

import logging

import numpy as np

logger = logging.getLogger(__name__)

Box = tuple[int, int, int, int]  # x, y, w, h

# A region taller than this (height / width) is a full-body view; otherwise a
# face / head view.
BODY_MIN_ASPECT = 1.7
# Ignore specks smaller than this fraction of the whole sheet area.
MIN_AREA_FRAC = 0.004
# Colour distance (BGR Euclidean, 0-441) beyond which a pixel differs enough
# from the detected background to count as foreground.
BG_TOLERANCE = 32.0


def _background_color(bgr: np.ndarray) -> np.ndarray:
    """Estimate the sheet background colour as the median of a thin border frame
    (robust to white, gray, or any uniform backdrop, and to a figure touching an
    edge)."""
    H, W = bgr.shape[:2]
    b = max(2, int(round(min(H, W) * 0.01)))
    border = np.concatenate([
        bgr[:b, :, :].reshape(-1, 3),
        bgr[-b:, :, :].reshape(-1, 3),
        bgr[:, :b, :].reshape(-1, 3),
        bgr[:, -b:, :].reshape(-1, 3),
    ], axis=0)
    return np.median(border, axis=0)


def _foreground_mask(bgr: np.ndarray) -> np.ndarray:
    """Binary mask (uint8 0/255) of non-background pixels (anything far enough
    from the detected background colour), with line-art stroke gaps sealed by a
    small close that won't bridge two separated views."""
    import cv2

    H, W = bgr.shape[:2]
    bg = _background_color(bgr).astype(np.float32)
    dist = np.linalg.norm(bgr.astype(np.float32) - bg, axis=2)
    fg = (dist > BG_TOLERANCE).astype(np.uint8) * 255
    # Kernel ~0.6% of the short side: seals anti-aliased stroke gaps inside one
    # figure, but the gutters between views are far wider so they survive.
    k = max(3, int(round(min(H, W) * 0.006)))
    if k % 2 == 0:
        k += 1
    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (k, k))
    return cv2.morphologyEx(fg, cv2.MORPH_CLOSE, kernel)


def _iou_contained(inner: Box, outer: Box) -> float:
    """Fraction of ``inner``'s area that lies inside ``outer``."""
    ix, iy, iw, ih = inner
    ox, oy, ow, oh = outer
    x0, y0 = max(ix, ox), max(iy, oy)
    x1, y1 = min(ix + iw, ox + ow), min(iy + ih, oy + oh)
    if x1 <= x0 or y1 <= y0:
        return 0.0
    return (x1 - x0) * (y1 - y0) / float(max(1, iw * ih))


def segment_character_sheet(bgr: np.ndarray, *, pad_frac: float = 0.02) -> dict[str, list[Box]]:
    """Segment a character sheet into ``{"faces": [box...], "bodies": [box...]}``.

    Boxes are ``(x, y, w, h)`` in pixels, padded slightly so a crop keeps a clean
    margin. Faces are returned in reading order (top→bottom, left→right); bodies
    left→right (front view is usually leftmost). A face fully inside a body box is
    dropped (it's the body view's own head, not a standalone face view)."""
    import cv2

    H, W = bgr.shape[:2]
    mask = _foreground_mask(bgr)
    n, _labels, stats, _cent = cv2.connectedComponentsWithStats(mask, connectivity=8)
    min_area = MIN_AREA_FRAC * H * W

    faces: list[Box] = []
    bodies: list[Box] = []
    for i in range(1, n):
        x, y, w, h, area = (int(v) for v in stats[i])
        if area < min_area or w < 8 or h < 8:
            continue
        pad = int(round(min(w, h) * pad_frac))
        box = (
            max(0, x - pad),
            max(0, y - pad),
            min(W - max(0, x - pad), w + 2 * pad),
            min(H - max(0, y - pad), h + 2 * pad),
        )
        if h / max(1, w) >= BODY_MIN_ASPECT:
            bodies.append(box)
        else:
            faces.append(box)

    # Drop any "face" that is really the head region of a full-body view.
    faces = [f for f in faces if all(_iou_contained(f, b) < 0.7 for b in bodies)]

    faces.sort(key=lambda b: (round(b[1] / (0.08 * H)), b[0]))  # reading order
    bodies.sort(key=lambda b: b[0])  # left → right
    return {"faces": faces, "bodies": bodies}
