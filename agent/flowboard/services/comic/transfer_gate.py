"""Shared concurrency gate for large (multi-MB) media transfers.

Atrium generated-image downloads, R2 result uploads, and Avis generated-image
fetches all compete for the same physical uplink/downlink on this machine.
With many generation jobs in flight at once (20-30 concurrent users), letting
every one of them stream a multi-MB file at the same time saturates the link
and each transfer crawls — slow enough to blow past per-attempt timeouts
(e.g. Atrium's 90s download budget), which then get retried, making the
contention worse instead of better.

Job/orchestration concurrency (WORKER_CONCURRENCY — mostly waiting on a
remote API, not moving bytes) is intentionally NOT limited by this gate; only
the actual byte-streaming sections should acquire it.
"""
from __future__ import annotations

import os
import threading

MEDIA_TRANSFER_CONCURRENCY = max(1, int(os.getenv("FLOWBOARD_MEDIA_TRANSFER_CONCURRENCY", "8")))
transfer_gate = threading.Semaphore(MEDIA_TRANSFER_CONCURRENCY)
