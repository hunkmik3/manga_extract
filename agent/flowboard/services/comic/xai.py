"""xAI (Grok) image-edit service — the final "clean bubble" step of the
Bubble Extract branch.

Each detected speech-bubble crop is sent to Grok's **image *edit*** endpoint
(``POST /v1/images/edits``) — NOT ``/v1/images/generations``. The distinction
matters a lot here:

  * ``/v1/images/generations`` with an image input *reimagines* the scene — it
    rewrites the lettering into gibberish and changes the aspect ratio. That is
    what the Grok web UI's "create" mode does and it destroys the text.
  * ``/v1/images/edits`` performs a *faithful* edit — it keeps the exact text,
    composition and layout and only applies the instruction (clean the bubble,
    green background, remove the tail, sharpen). This matches the result the
    user gets from the Grok web UI's edit mode.

The image is passed inline as a ``data:`` URI in the ``image.url`` field
(``image.type == "image_url"``). Up to 3 source images are accepted; we send 1.

Auth: ``XAI_API_KEY`` from the environment (loaded from ``.env`` by
``flowboard.__init__``). No key → :class:`XAIUnavailable`.
"""
from __future__ import annotations

import base64
import logging
import os

import httpx

logger = logging.getLogger(__name__)

_API_URL = "https://api.x.ai/v1/images/edits"
# "quality" model — best text fidelity (~$0.50/img) vs the base grok-imagine-image
# (~$0.20/img). The user asked for whichever is best, so default to quality.
_MODEL = os.getenv("FLOWBOARD_XAI_IMAGE_MODEL", "grok-imagine-image-quality")
_TIMEOUT = 180.0

# The user's exact bubble-cleanup prompt. Kept verbatim — verified to reproduce
# the Grok web-UI result (text preserved, green background, closed bubble).
DEFAULT_PROMPT = (
    "only keep the text bubble, remove everything else, transparent background, "
    "enhance and upscale and sharpen all the text. keep the original composition "
    "and layout. solid green background. keep the original text style. remove the "
    "whisker of the text bubble, close the text bubble"
)

# Gemini-tuned variant. Gemini reads "solid green background" as "fill the bubble
# green" and floods the interior, which the chroma-key would then hollow out. So
# we spell out: WHITE inside, green ONLY outside. Verified on real bubbles to keep
# an opaque white fill that survives key_out_green. Used when engine == "gemini".
GEMINI_PROMPT = (
    "Keep ONLY the speech bubble and its text, remove everything else. "
    "Keep the inside of the speech bubble SOLID WHITE. "
    "Keep the text exactly as in the original — same wording, same lettering style — "
    "and enhance, upscale and sharpen the text. "
    "Draw one clean closed bubble outline and remove the bubble tail/whisker. "
    "Fill everything OUTSIDE the bubble with a flat uniform chroma-key green; "
    "do NOT put any green inside the bubble. Keep the original composition and layout."
)


class XAIUnavailable(RuntimeError):
    """xAI cannot be called (missing API key)."""


def _api_key() -> str:
    key = os.getenv("XAI_API_KEY")
    if not key or not key.strip():
        raise XAIUnavailable("XAI_API_KEY is not set (add it to .env)")
    return key.strip()


def is_available() -> bool:
    """True if an xAI API key is configured."""
    return bool(os.getenv("XAI_API_KEY", "").strip())


async def clean_bubble(
    image_png: bytes,
    prompt: str = DEFAULT_PROMPT,
    *,
    model: str | None = None,
    client: httpx.AsyncClient | None = None,
) -> bytes:
    """Send one bubble crop through Grok's faithful image-edit endpoint and
    return the cleaned PNG bytes. Raises :class:`XAIUnavailable` if no key,
    or ``RuntimeError`` on an API/transport error."""
    key = _api_key()
    data_uri = "data:image/png;base64," + base64.b64encode(image_png).decode("ascii")
    payload = {
        "model": model or _MODEL,
        "prompt": prompt,
        "image": {"url": data_uri, "type": "image_url"},
        "response_format": "b64_json",
    }
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}

    async def _post(c: httpx.AsyncClient) -> httpx.Response:
        return await c.post(_API_URL, json=payload, headers=headers)

    if client is not None:
        resp = await _post(client)
    else:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as c:
            resp = await _post(c)

    if resp.status_code != 200:
        raise RuntimeError(f"xAI edit failed: {resp.status_code} {resp.text[:300]}")
    body = resp.json()
    items = body.get("data") or []
    if not items or not isinstance(items, list):
        raise RuntimeError(f"xAI edit returned no image: {str(body)[:300]}")
    b64 = items[0].get("b64_json")
    if not b64:
        raise RuntimeError(f"xAI edit missing b64_json: {str(items[0])[:300]}")
    try:
        return base64.b64decode(b64)
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"xAI edit bad base64: {exc}") from exc
