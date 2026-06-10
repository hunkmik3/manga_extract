"""ML panel detector (YOLO) — optional backend behind the same ``Box`` interface
as the heuristic ``panels.detect_panels``.

Uses a YOLO model trained for comic/manga frame detection (default:
``deepghs/manga109_yolo``, the "frame" class). It rescues the layouts the
projection-profile heuristic can't cut — dense pages where speech bubbles bridge
gutters, or borderless art — which is exactly the "leave a hook for a learned
detector" note in the brief.

Heavy deps (torch + ultralytics) are an **optional** install:

    uv pip install --python .venv/bin/python -e ".[ml]"

so the base agent stays light. Everything here is lazy: nothing is imported or
downloaded until the first ML detection. If the deps/weights are missing,
``MLUnavailable`` is raised with a clear message and callers fall back to the
heuristic (``auto`` mode) or surface the error (``ml`` mode).

Weights resolution:
  * ``FLOWBOARD_PANEL_MODEL`` env → a local .pt/.onnx path (all classes used), or
  * default → ``deepghs/manga109_yolo`` downloaded + cached via huggingface_hub.
"""
from __future__ import annotations

import logging
import os
from typing import Optional

import numpy as np

logger = logging.getLogger(__name__)

Box = tuple[int, int, int, int]

# Tested defaults on real manhwa pages (~900x1291). Larger imgsz than YOLO's 640
# matters for tall pages; conf kept low because frames are large, confident objs.
_HF_REPO = "deepghs/manga109_yolo"
_HF_FILE = "v2021.12.30_l_yv11/model.pt"
_CONF = 0.20
_IMGSZ = 1024

_model = None          # cached YOLO instance
_frame_id: Optional[int] = None  # class id of the "frame"/panel class, or None = all
_load_failed: Optional[str] = None


class MLUnavailable(RuntimeError):
    """The optional ML detector can't run (deps or weights missing)."""


def _load_model():
    """Lazy-load + cache the YOLO model. Raises MLUnavailable on any failure."""
    global _model, _frame_id, _load_failed
    if _model is not None:
        return _model
    if _load_failed is not None:
        raise MLUnavailable(_load_failed)
    try:
        from ultralytics import YOLO  # optional dep (pulls torch)
    except Exception as exc:  # noqa: BLE001
        _load_failed = f'ultralytics/torch not installed ({exc}); run: uv pip install --python .venv/bin/python -e ".[ml]"'
        raise MLUnavailable(_load_failed) from exc

    weights = os.getenv("FLOWBOARD_PANEL_MODEL")
    try:
        if not weights:
            from huggingface_hub import hf_hub_download
            weights = hf_hub_download(_HF_REPO, _HF_FILE)
        model = YOLO(weights)
    except Exception as exc:  # noqa: BLE001
        _load_failed = f"could not load panel model weights ({exc})"
        raise MLUnavailable(_load_failed) from exc

    # Find the panel/frame class; if the model has no such named class, accept all.
    names = getattr(model, "names", {}) or {}
    fid = next((i for i, n in names.items() if str(n).lower() in ("frame", "panel")), None)
    _model, _frame_id = model, fid
    logger.info("panel ML model loaded (%s); classes=%s frame_id=%s", weights, names, fid)
    return _model


def is_available() -> bool:
    """True if the ML detector can be loaded (deps + weights present)."""
    try:
        _load_model()
        return True
    except MLUnavailable:
        return False


def detect_panels_ml(bgr: np.ndarray, conf: float = _CONF, imgsz: int = _IMGSZ) -> list[Box]:
    """Detect panel boxes with the YOLO model. Returns [(x, y, w, h)] sorted
    top→bottom, left→right. Raises ``MLUnavailable`` if the backend is missing."""
    model = _load_model()
    res = model.predict(bgr, conf=conf, imgsz=imgsz, verbose=False)[0]
    boxes: list[Box] = []
    if res.boxes is not None and len(res.boxes) > 0:
        xyxy = res.boxes.xyxy.cpu().numpy()
        cls = res.boxes.cls.cpu().numpy().astype(int)
        for (x0, y0, x1, y1), c in zip(xyxy, cls):
            if _frame_id is None or int(c) == _frame_id:
                boxes.append((int(x0), int(y0), int(x1 - x0), int(y1 - y0)))
    H = bgr.shape[0]
    boxes.sort(key=lambda b: (round(b[1] / (0.08 * H)), b[0]))
    return boxes
