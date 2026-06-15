"""Chapter-wide character identification via magiv2 ("The Manga Whisperer" v2).

magiv2 reads whole pages in reading order with a *character bank* (named
reference images — here, the creator's uploaded character sheets) and returns,
per page, character boxes labelled with the bank names. Unlike the abandoned
CCIP crop-clustering (which could not separate this comic's characters), magiv2
uses page-level context and re-identifies characters across the chapter —
validated on this very comic: correct re-id of a banked character on full-body,
close-up, profile AND back-of-head views, ~2-3 s/page on Apple-Silicon CPU.

Lazy + optional: needs the ``[ml]`` extras plus ``transformers<5`` and ``timm``
(magiv2's remote code targets the transformers 4.x backbone API). Everything is
imported on first use; ``MLUnavailable`` is raised when deps/weights are
missing, mirroring ``panel_ml``.
"""
from __future__ import annotations

import logging
from typing import Optional

import numpy as np

from flowboard.services.comic.panel_ml import MLUnavailable

logger = logging.getLogger(__name__)

Box = tuple[int, int, int, int]  # x, y, w, h

_HF_REPO = "ragavsachdeva/magiv2"

_model = None
_load_failed: Optional[str] = None


def _load():
    global _model, _load_failed
    if _model is not None:
        return _model
    if _load_failed is not None:
        raise MLUnavailable(_load_failed)
    try:
        import torch  # noqa: F401
        from transformers import AutoModel
    except Exception as exc:  # noqa: BLE001
        _load_failed = (
            f"transformers/torch not installed ({exc}); run: "
            'uv pip install --python .venv/bin/python -e ".[ml]" "transformers<5" timm'
        )
        raise MLUnavailable(_load_failed) from exc
    try:
        model = AutoModel.from_pretrained(_HF_REPO, trust_remote_code=True).eval()
    except Exception as exc:  # noqa: BLE001
        _load_failed = f"could not load magiv2 weights ({exc})"
        raise MLUnavailable(_load_failed) from exc
    _model = model
    logger.info("magiv2 loaded")
    return _model


def is_available() -> bool:
    try:
        _load()
        return True
    except Exception:  # noqa: BLE001
        return False


def _to_magi_rgb(bgr: np.ndarray) -> np.ndarray:
    """magiv2 expects grayscale→RGB arrays (its training regime); colour pages
    are converted the same way — validated to work on this colour manhwa."""
    import cv2
    from PIL import Image

    rgb = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
    return np.array(Image.fromarray(rgb).convert("L").convert("RGB"))


def predict_page_characters(
    page_bgrs: list[np.ndarray],
    bank_images_bgr: list[np.ndarray],
    bank_names: list[str],
) -> list[list[tuple[Box, str]]]:
    """Run chapter-wide character identification.

    ``page_bgrs`` — pages in reading order; ``bank_images_bgr``/``bank_names`` —
    the character bank (several crops may share one name; magiv2 merges them).
    Returns, per page, ``[(character box (x,y,w,h), bank name or "Other"), …]``.
    """
    import torch

    model = _load()
    pages = [_to_magi_rgb(p) for p in page_bgrs]
    bank = {
        "images": [_to_magi_rgb(b) for b in bank_images_bgr],
        "names": list(bank_names),
    }
    with torch.no_grad():
        results = model.do_chapter_wide_prediction(pages, bank, use_tqdm=False, do_ocr=False)

    out: list[list[tuple[Box, str]]] = []
    for res in results:
        boxes = res.get("characters") or []
        names = res.get("character_names") or []
        page_out: list[tuple[Box, str]] = []
        for box, name in zip(boxes, names):
            x0, y0, x1, y1 = (int(v) for v in box)
            if x1 > x0 and y1 > y0:
                page_out.append(((x0, y0, x1 - x0, y1 - y0), str(name)))
        out.append(page_out)
    return out
