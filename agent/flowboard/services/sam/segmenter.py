"""MobileSAM cutout — the same encode-once / click-to-mask trick Grok uses.

Why this exists: ``detect.cutout`` (grabCut) re-segments from scratch for every
part — slow (seconds) and rough on hair/edges. MobileSAM instead runs the heavy
image encoder ONCE per image (~0.35s on an M1), caches the embedding, then each
selected box is just a decoder pass (~30ms) that returns a learned mask. That is
exactly why Grok's segment panel feels instant: pay the encode once, pick parts
for free.

Models (ONNX, ~45 MB total) live in ``models/`` beside this file:
  * ``mobilesam.encoder.onnx`` — takes a raw HWC float32 image, outputs a
    [1,256,64,64] embedding (bundles SAM's resize+normalize preprocessing).
  * ``mobilesam.decoder.onnx`` — standard SAM prompt decoder.

Coordinate convention (matches the reference browser demo, verified empirically):
resize the image so its longest side is 1024, feed that to the encoder, express
prompt points in that resized frame, and set ``orig_im_size`` to the resized
size so masks come back at that resolution. We then upscale the mask to the
original image for a full-res cutout.

CPU-only (onnxruntime). All entry points are synchronous and CPU-heavy — call
them from a thread (``asyncio.to_thread``).
"""
from __future__ import annotations

import logging
import threading
from collections import OrderedDict
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

_MODELS = Path(__file__).parent / "models"
_ENCODER = _MODELS / "mobilesam.encoder.onnx"
_DECODER = _MODELS / "mobilesam.decoder.onnx"
_LONG_SIDE = 1024

# Lazy singletons — building an ORT session is ~100ms, so do it once.
_lock = threading.Lock()
_enc = None  # type: ignore[var-annotated]
_dec = None  # type: ignore[var-annotated]

# media_id → (embedding ndarray, resized_h, resized_w). Small LRU so repeated
# part-picking on one image never re-encodes.
_EMB_CACHE_MAX = 24
_emb_cache: "OrderedDict[str, tuple]" = OrderedDict()


def available() -> bool:
    """True when both ONNX models are present on disk."""
    return _ENCODER.is_file() and _DECODER.is_file()


def _sessions():
    """Return (encoder, decoder) ORT sessions, building them once."""
    global _enc, _dec
    if _enc is not None and _dec is not None:
        return _enc, _dec
    with _lock:
        if _enc is None or _dec is None:
            import onnxruntime as ort

            opts = ort.SessionOptions()
            opts.intra_op_num_threads = 0  # let ORT pick (all M1 cores)
            _enc = ort.InferenceSession(str(_ENCODER), opts, providers=["CPUExecutionProvider"])
            _dec = ort.InferenceSession(str(_DECODER), opts, providers=["CPUExecutionProvider"])
    return _enc, _dec


def warm() -> None:
    """Build the ORT sessions ahead of first use (optional; ignored if models
    are missing)."""
    if available():
        try:
            _sessions()
        except Exception as exc:  # noqa: BLE001
            logger.warning("sam warm failed: %s", exc)


def _embed(img_bgr, media_id: Optional[str]):
    """Encode ``img_bgr`` (or return the cached embedding for ``media_id``).
    Returns (embedding, resized_h, resized_w, scale) where scale maps original
    pixels → resized frame."""
    import cv2
    import numpy as np

    h0, w0 = img_bgr.shape[:2]
    scale = _LONG_SIDE / max(h0, w0)
    hr, wr = max(1, round(h0 * scale)), max(1, round(w0 * scale))

    if media_id:
        hit = _emb_cache.get(media_id)
        if hit is not None:
            _emb_cache.move_to_end(media_id)
            emb, chr_, cwr = hit
            # Guard against a stale cache if the same id somehow re-sized.
            if (chr_, cwr) == (hr, wr):
                return emb, hr, wr, scale

    enc, _ = _sessions()
    resized = cv2.resize(img_bgr, (wr, hr), interpolation=cv2.INTER_AREA)
    rgb = cv2.cvtColor(resized, cv2.COLOR_BGR2RGB).astype(np.float32)
    emb = enc.run(None, {"input_image": rgb})[0]

    if media_id:
        _emb_cache[media_id] = (emb, hr, wr)
        _emb_cache.move_to_end(media_id)
        while len(_emb_cache) > _EMB_CACHE_MAX:
            _emb_cache.popitem(last=False)
    return emb, hr, wr, scale


def cutout_box(
    img_bytes: bytes,
    box_2d: list[int],
    *,
    media_id: Optional[str] = None,
    feather: int = 1,
) -> Optional[bytes]:
    """Segment the object inside ``box_2d`` ([ymin,xmin,ymax,xmax] 0-1000) with
    MobileSAM and return a transparent PNG tight-cropped to the box. Returns None
    on any failure (caller can fall back to grabCut).

    ``media_id`` keys the embedding cache — pass it so repeated cutouts on the
    same image skip re-encoding.
    """
    if not available():
        return None
    try:
        import cv2
        import numpy as np

        img = cv2.imdecode(np.frombuffer(img_bytes, np.uint8), cv2.IMREAD_COLOR)
        if img is None:
            return None
        h0, w0 = img.shape[:2]
        ymin, xmin, ymax, xmax = box_2d

        emb, hr, wr, _ = _embed(img, media_id)

        # Box corners in the resized (encoder) frame; SAM box prompt = a
        # top-left point (label 2) + a bottom-right point (label 3).
        rx0 = max(0.0, xmin / 1000 * wr); ry0 = max(0.0, ymin / 1000 * hr)
        rx1 = min(float(wr), xmax / 1000 * wr); ry1 = min(float(hr), ymax / 1000 * hr)
        if rx1 - rx0 < 2 or ry1 - ry0 < 2:
            return None
        _, dec = _sessions()
        out = dec.run(
            None,
            {
                "image_embeddings": emb,
                "point_coords": np.array([[[rx0, ry0], [rx1, ry1]]], np.float32),
                "point_labels": np.array([[2, 3]], np.float32),
                "mask_input": np.zeros((1, 1, 256, 256), np.float32),
                "has_mask_input": np.zeros(1, np.float32),
                "orig_im_size": np.array([hr, wr], np.float32),
            },
        )
        mask_small = out[0][0, 0] > 0  # (hr, wr) bool
        if not mask_small.any():
            return None

        # Upscale the mask to the original resolution, then apply as alpha.
        alpha = cv2.resize(
            mask_small.astype(np.uint8) * 255, (w0, h0), interpolation=cv2.INTER_LINEAR
        )
        if feather > 0:
            alpha = cv2.GaussianBlur(alpha, (0, 0), feather)

        # Tight-crop to the box (in original pixels) so the cutout isn't a huge
        # mostly-empty canvas.
        bx0 = max(0, int(xmin / 1000 * w0)); bx1 = min(w0, int(xmax / 1000 * w0))
        by0 = max(0, int(ymin / 1000 * h0)); by1 = min(h0, int(ymax / 1000 * h0))
        if bx1 - bx0 < 2 or by1 - by0 < 2:
            return None
        bgra = cv2.cvtColor(img, cv2.COLOR_BGR2BGRA)
        bgra[:, :, 3] = alpha
        bgra = bgra[by0:by1, bx0:bx1]
        ok, buf = cv2.imencode(".png", bgra)
        return buf.tobytes() if ok else None
    except Exception as exc:  # noqa: BLE001
        logger.warning("sam cutout failed: %s", exc)
        return None


def segment_point_box(
    img_bytes: bytes, x_1000: float, y_1000: float, *, media_id: Optional[str] = None
) -> Optional[list[int]]:
    """Click-to-segment: given a click at (x,y) in 0-1000 normalized coords, run
    MobileSAM with a single positive point and return the bounding box of the
    segmented object as ``[ymin, xmin, ymax, xmax]`` (0-1000). None on failure.
    This is the 'SAM detect' entry point — click an object, get its region."""
    if not available():
        return None
    try:
        import cv2
        import numpy as np

        img = cv2.imdecode(np.frombuffer(img_bytes, np.uint8), cv2.IMREAD_COLOR)
        if img is None:
            return None
        h0, w0 = img.shape[:2]
        emb, hr, wr, _ = _embed(img, media_id)
        px = max(0.0, min(float(wr), x_1000 / 1000 * wr))
        py = max(0.0, min(float(hr), y_1000 / 1000 * hr))
        _, dec = _sessions()
        out = dec.run(
            None,
            {
                "image_embeddings": emb,
                "point_coords": np.array([[[px, py]]], np.float32),
                "point_labels": np.array([[1]], np.float32),  # single positive point
                "mask_input": np.zeros((1, 1, 256, 256), np.float32),
                "has_mask_input": np.zeros(1, np.float32),
                "orig_im_size": np.array([hr, wr], np.float32),
            },
        )
        mask = out[0][0, 0] > 0  # (hr, wr)
        ys, xs = np.where(mask)
        if ys.size == 0:
            return None
        ymin, ymax = int(ys.min()), int(ys.max())
        xmin, xmax = int(xs.min()), int(xs.max())
        return [
            max(0, min(1000, int(ymin / hr * 1000))),
            max(0, min(1000, int(xmin / wr * 1000))),
            max(0, min(1000, int(ymax / hr * 1000))),
            max(0, min(1000, int(xmax / wr * 1000))),
        ]
    except Exception as exc:  # noqa: BLE001
        logger.warning("sam segment_point failed: %s", exc)
        return None
