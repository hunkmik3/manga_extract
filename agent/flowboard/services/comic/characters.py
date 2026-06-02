"""Character tracking — detect character bodies (manga109 YOLO) across the comic
and cluster them by identity (CCIP) so each panel can be enhanced with a
consistent set of reference images per character.

Optional ML backend (the ``[ml]`` extra: ultralytics + dghs-imgutils +
onnxruntime). Everything is lazy; ``MLUnavailable`` is raised if the deps/weights
are missing. Validated on real colored manhwa: manga109 finds character bodies
and CCIP groups main characters cleanly across pages.
"""
from __future__ import annotations

import logging
from typing import Optional

import numpy as np

from flowboard.services.comic import panels as panel_svc
from flowboard.services.comic.panel_ml import MLUnavailable

logger = logging.getLogger(__name__)

Box = tuple[int, int, int, int]

_HF_REPO = "deepghs/manga109_yolo"
_HF_FILE = "v2021.12.30_l_yv11/model.pt"
_BODY_CONF = 0.30
_IMGSZ = 1024
_MIN_BODY = 60          # ignore tiny detections (px)
_MAX_REFS = 4           # reference crops kept per character

_model = None
_body_id: Optional[int] = None
_load_failed: Optional[str] = None


def _load():
    global _model, _body_id, _load_failed
    if _model is not None:
        return _model
    if _load_failed is not None:
        raise MLUnavailable(_load_failed)
    try:
        from ultralytics import YOLO
    except Exception as exc:  # noqa: BLE001
        _load_failed = f"ultralytics/torch not installed ({exc}); run: pip install -e '.[ml]'"
        raise MLUnavailable(_load_failed) from exc
    try:
        from huggingface_hub import hf_hub_download
        model = YOLO(hf_hub_download(_HF_REPO, _HF_FILE))
    except Exception as exc:  # noqa: BLE001
        _load_failed = f"could not load manga109 weights ({exc})"
        raise MLUnavailable(_load_failed) from exc
    names = getattr(model, "names", {}) or {}
    _model = model
    _body_id = next((i for i, n in names.items() if str(n).lower() == "body"), None)
    logger.info("character model loaded; body class id=%s", _body_id)
    return _model


def is_available() -> bool:
    try:
        _load()
        from imgutils.metrics import ccip_clustering  # noqa: F401
        return True
    except Exception:  # noqa: BLE001
        return False


def detect_bodies(bgr: np.ndarray, conf: float = _BODY_CONF, min_size: int = _MIN_BODY) -> list[Box]:
    """Detect character body boxes (x, y, w, h) on a page."""
    model = _load()
    res = model.predict(bgr, conf=conf, imgsz=_IMGSZ, verbose=False)[0]
    out: list[Box] = []
    if res.boxes is None or len(res.boxes) == 0:
        return out
    xyxy = res.boxes.xyxy.cpu().numpy()
    cls = res.boxes.cls.cpu().numpy().astype(int)
    for (x0, y0, x1, y1), c in zip(xyxy, cls):
        if _body_id is not None and int(c) != _body_id:
            continue
        x0, y0, x1, y1 = int(x0), int(y0), int(x1), int(y1)
        if (x1 - x0) < min_size or (y1 - y0) < min_size:
            continue
        out.append((max(0, x0), max(0, y0), x1 - x0, y1 - y0))
    return out


def _cluster(crops_bgr: list[np.ndarray]) -> list[int]:
    """CCIP-cluster crops by character identity → labels (-1 = noise)."""
    if not crops_bgr:
        return []
    if len(crops_bgr) == 1:
        return [0]
    import cv2
    from PIL import Image
    from imgutils.metrics import ccip_clustering

    imgs = [Image.fromarray(cv2.cvtColor(c, cv2.COLOR_BGR2RGB)) for c in crops_bgr]
    try:
        # min_samples=2: a character appearing ≥2× still forms a cluster (the
        # optics default ~5 misses everything on shorter comics). CCIP's tight
        # eps keeps clusters single-identity.
        return list(ccip_clustering(imgs, method="optics", min_samples=2))
    except Exception as exc:  # noqa: BLE001
        logger.warning("ccip_clustering failed: %s", exc)
        return [-1] * len(crops_bgr)


def match_character(panel_bgr: np.ndarray, samples: list[tuple[str, np.ndarray]], threshold: float = 0.45) -> Optional[str]:
    """Pick which character (by id) appears in a panel by CCIP-comparing the
    panel's largest body crop to each character's sample crop. Returns the best
    char id if it's a confident match (difference below ``threshold``), else None."""
    if not samples:
        return None
    import cv2
    from PIL import Image
    from imgutils.metrics import ccip_difference

    try:
        boxes = detect_bodies(panel_bgr, min_size=40)
    except MLUnavailable:
        raise
    except Exception:  # noqa: BLE001
        boxes = []
    crop = panel_bgr
    if boxes:
        x, y, w, h = max(boxes, key=lambda b: b[2] * b[3])
        crop = panel_bgr[y:y + h, x:x + w]
    q = Image.fromarray(cv2.cvtColor(crop, cv2.COLOR_BGR2RGB))

    best_id: Optional[str] = None
    best_d = 1e9
    for cid, sample in samples:
        try:
            d = float(ccip_difference(q, Image.fromarray(cv2.cvtColor(sample, cv2.COLOR_BGR2RGB))))
        except Exception:  # noqa: BLE001
            continue
        if d < best_d:
            best_d, best_id = d, cid
    return best_id if best_d <= threshold else None


def build_clusters(
    pages: list[tuple[str, int, bytes]], conf: float = _BODY_CONF, max_refs: int = _MAX_REFS
) -> list[dict]:
    """Detect + cluster characters across pages. Pure (no DB / no network beyond
    the cached models). ``pages`` = [(page_name, page_idx, page_bytes)].

    Returns characters sorted by frequency:
      [{count, ref_png: [bytes, …] (top crops by area), pages: [idx, …]}]
    """
    crops: list[tuple[np.ndarray, int, int]] = []  # (bgr, area, page_idx)
    for _name, idx, data in pages:
        try:
            bgr = panel_svc.decode_bgr(data)
        except Exception:  # noqa: BLE001
            continue
        H, W = bgr.shape[0], bgr.shape[1]
        for (x, y, w, h) in detect_bodies(bgr, conf):
            x2, y2 = min(W, x + w), min(H, y + h)
            if x2 > x and y2 > y:
                crops.append((bgr[y:y2, x:x2], w * h, idx))

    labels = _cluster([c[0] for c in crops])
    groups: dict[int, list[tuple[np.ndarray, int, int]]] = {}
    for (crop, area, idx), lab in zip(crops, labels):
        if lab == -1:
            continue
        groups.setdefault(int(lab), []).append((crop, area, idx))

    out: list[dict] = []
    for members in groups.values():
        members.sort(key=lambda m: -m[1])  # largest (clearest) first
        ref_png = [panel_svc.encode_png(m[0]) for m in members[:max_refs]]
        out.append({
            "count": len(members),
            "ref_png": ref_png,
            "pages": sorted({m[2] for m in members}),
        })
    out.sort(key=lambda c: -c["count"])
    return out
