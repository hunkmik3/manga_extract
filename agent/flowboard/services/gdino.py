"""Grounding DINO — open-vocabulary object detector (the Grok-style part finder).

This replaces the VLM (Gemini-reads-coordinates) detector, whose boxes wandered
on small items (earrings, shoes). Grounding DINO is a real detector: give it a
list of concept words and it returns TIGHT, per-instance boxes for whichever are
present — left shoe and right shoe as two boxes, each earring separately — which
is exactly what a Grok-like segment panel needs. Paired with MobileSAM (masks),
this is the classic "Grounded-SAM" pipeline.

Runs locally on the machine's GPU (Apple MPS) via transformers + torch, ~1.3s
per image once the model is resident. The model (~700 MB, ``grounding-dino-tiny``)
downloads once to the HuggingFace cache on first use. No network, no API, no
per-image cost at inference time.

All entry points are synchronous and heavy — call from a thread.
"""
from __future__ import annotations

import logging
import threading
from typing import Optional

logger = logging.getLogger(__name__)

_MODEL_ID = "IDEA-Research/grounding-dino-tiny"

# Canonical concept list — one word per concept (synonyms bleed into merged
# labels in Grounding DINO, so we keep the prompt clean and map back). Broad
# enough for people/fashion/interiors/outdoors, not just fashion shots.
_VOCAB = [
    # people
    "person", "hair",
    # garments
    "shirt", "blouse", "sweater", "hoodie", "jacket", "blazer", "coat",
    "dress", "skirt", "trousers", "shorts", "jeans", "suit",
    # footwear
    "shoe", "boot", "sneaker", "sandal",
    # accessories
    "earring", "necklace", "bracelet", "ring", "watch", "handbag", "backpack",
    "belt", "sunglasses", "glasses", "hat", "scarf", "tie", "gloves",
    # scene / background
    "wall", "floor", "sky", "tree", "grass", "building", "window",
    "door", "chair", "table", "sofa", "bed", "car", "plant",
]
_PROMPT = " . ".join(_VOCAB) + " ."

# Overlapping garment concepts that describe the SAME physical item — when two
# different labels from one group cover the same region, keep the higher-scoring
# one (collapses "trousers"+"jeans" or "blazer"+"sweater" into a single part).
_GROUPS = {
    **{w: "top" for w in ("shirt", "blouse", "sweater", "hoodie", "jacket", "blazer", "coat")},
    **{w: "bottom" for w in ("trousers", "shorts", "jeans", "skirt")},
    **{w: "footwear" for w in ("shoe", "boot", "sneaker", "sandal")},
    **{w: "eyewear" for w in ("sunglasses", "glasses")},
}


def _group(label: str) -> str:
    return _GROUPS.get(label, label)

_BOX_THRESHOLD = 0.33
_TEXT_THRESHOLD = 0.25
_NMS_IOU = 0.6  # same-label boxes closer than this are duplicates

_lock = threading.Lock()
_proc = None  # type: ignore[var-annotated]
_model = None  # type: ignore[var-annotated]
_device = None  # type: ignore[var-annotated]
_import_ok: Optional[bool] = None


def available() -> bool:
    """True when torch + transformers import (the model itself downloads lazily
    on first detect)."""
    global _import_ok
    if _import_ok is None:
        try:
            import torch  # noqa: F401
            import transformers  # noqa: F401

            _import_ok = True
        except Exception as exc:  # noqa: BLE001
            logger.info("gdino unavailable: %s", exc)
            _import_ok = False
    return _import_ok


def _load():
    """Build the processor + model once, on the best available device."""
    global _proc, _model, _device
    if _model is not None:
        return _proc, _model, _device
    with _lock:
        if _model is None:
            import torch
            from transformers import (
                AutoModelForZeroShotObjectDetection,
                AutoProcessor,
            )

            _device = "mps" if torch.backends.mps.is_available() else "cpu"
            _proc = AutoProcessor.from_pretrained(_MODEL_ID)
            _model = (
                AutoModelForZeroShotObjectDetection.from_pretrained(_MODEL_ID)
                .to(_device)
                .eval()
            )
            logger.info("gdino loaded on %s", _device)
    return _proc, _model, _device


def warm() -> None:
    """Load the model ahead of first use (optional)."""
    if available():
        try:
            _load()
        except Exception as exc:  # noqa: BLE001
            logger.warning("gdino warm failed: %s", exc)


def _canon(phrase: str) -> str:
    """Map a (possibly merged) Grounding DINO phrase back to one canonical word."""
    words = phrase.split()
    ws = set(words)
    for v in _VOCAB:
        if v in ws:
            return v
    return words[0] if words else "object"


def _iou(a: list[int], b: list[int]) -> float:
    ix0, iy0 = max(a[0], b[0]), max(a[1], b[1])
    ix1, iy1 = min(a[2], b[2]), min(a[3], b[3])
    inter = max(0, ix1 - ix0) * max(0, iy1 - iy0)
    union = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    return inter / union if union > 0 else 0.0


def detect_parts(img_bytes: bytes) -> list[dict]:
    """Detect editable parts in ``img_bytes`` → ``[{label, box, score}]`` where
    box is ``[ymin, xmin, ymax, xmax]`` normalized 0-1000, sorted by confidence.
    Returns [] on failure. Heavy — call via ``asyncio.to_thread``."""
    if not available():
        return []
    try:
        import cv2
        import numpy as np
        import torch
        from PIL import Image

        img = cv2.imdecode(np.frombuffer(img_bytes, np.uint8), cv2.IMREAD_COLOR)
        if img is None:
            return []
        h, w = img.shape[:2]
        pil = Image.fromarray(cv2.cvtColor(img, cv2.COLOR_BGR2RGB))

        proc, model, device = _load()
        inp = proc(images=pil, text=_PROMPT, return_tensors="pt").to(device)
        with torch.no_grad():
            out = model(**inp)
        res = proc.post_process_grounded_object_detection(
            out,
            inp.input_ids,
            threshold=_BOX_THRESHOLD,
            text_threshold=_TEXT_THRESHOLD,
            target_sizes=[pil.size[::-1]],  # (h, w)
        )[0]

        dets: list[dict] = []
        for box_px, score, phrase in zip(
            res["boxes"].tolist(), res["scores"].tolist(), res["text_labels"]
        ):
            x0, y0, x1, y1 = box_px
            box = [
                max(0, min(1000, int(y0 / h * 1000))),
                max(0, min(1000, int(x0 / w * 1000))),
                max(0, min(1000, int(y1 / h * 1000))),
                max(0, min(1000, int(x1 / w * 1000))),
            ]
            if box[2] - box[0] < 3 or box[3] - box[1] < 3:
                continue
            dets.append({"label": _canon(phrase), "box": box, "score": round(float(score), 3)})

        dets.sort(key=lambda d: -d["score"])
        # Group-aware NMS — drop a box that overlaps a higher-scoring box in the
        # same concept GROUP (so "trousers"+"jeans" collapse to one), while still
        # keeping genuinely separate instances of a group (left shoe vs right
        # shoe have low overlap and both survive).
        kept: list[dict] = []
        for d in dets:
            g = _group(d["label"])
            if any(_group(k["label"]) == g and _iou(k["box"], d["box"]) > _NMS_IOU for k in kept):
                continue
            kept.append(d)
        return kept
    except Exception as exc:  # noqa: BLE001
        logger.warning("gdino detect failed: %s", exc)
        return []
