"""MobileSAM segmentation — Grok-style encode-once, click-to-mask cutouts."""
from flowboard.services.sam.segmenter import available, cutout_box, segment_point_box, warm

__all__ = ["available", "cutout_box", "segment_point_box", "warm"]
