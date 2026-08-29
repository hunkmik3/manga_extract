"""PASS 1 — Bible Builder. A Gemini vision model reads the whole chapter and
proposes the bible (characters/outfits/scenes/per-page cast) as JSON.

Two backends (``READER_PROVIDER``, default ``atrium``):

  * **atrium** — Google Gemini via the Atrium passthrough
    (``/api/partner/llm/generate``, x-client-id/secret). Multimodal input is
    BY URL only, so pages are uploaded to R2 (downscaled) and passed as
    ``fileData.fileUri``, then deleted. Uses the ATRIUM credentials.
  * **gemini** — the direct Gemini API (``generateContent?key=``) with inline
    base64 images. Needs GEMINI_API_KEY with credits.

Model via ``READER_MODEL`` (default ``gemini-2.5-flash``).
"""
from __future__ import annotations

import asyncio
import base64
import io
import json
import logging
import os
from typing import Optional, Sequence

import httpx

from flowboard.services.colorize.bible import Bible, parse_bible

logger = logging.getLogger(__name__)

_GEMINI_BASE = "https://generativelanguage.googleapis.com/v1beta/models"
_DEFAULT_MODEL = "gemini-2.5-flash"
_READ_EDGE = 768
_TIMEOUT_S = 300.0
_MAX_ATTEMPTS = 3


def reader_provider() -> str:
    return os.getenv("READER_PROVIDER", "").strip().lower() or "atrium"


def reader_model() -> str:
    return os.getenv("READER_MODEL", "").strip() or _DEFAULT_MODEL


def is_configured() -> bool:
    if reader_provider() == "gemini":
        from flowboard.services.comic import gemini_api
        return bool(os.getenv("READER_API_KEY", "").strip() or gemini_api.api_key())
    from flowboard.services.comic import atrium_api
    return atrium_api.is_configured()


# ── shared prompt / parsing ──────────────────────────────────────────────────

def _instructions(chapter: str, page_names: Sequence[str]) -> str:
    order = json.dumps(list(page_names), ensure_ascii=False)
    return f"""You are a manga editor building a COLOR BIBLE so a black-and-white chapter can
be colorized CONSISTENTLY across every page. Pages are given IN ORDER, each
preceded by its filename. First READ and UNDERSTAND the chapter like a human
(who the characters are, what happens, how the mood shifts), THEN extract the
bible. A human reviews it after, so be honest about what you actually see.

COLOUR SOURCE — this is critical:
- Some pages are ALREADY IN COLOUR (covers, splash/title pages). Those are GROUND
  TRUTH: read the real colours from them and set "color_source": "color_page".
- The rest are black-and-white with no true colour. There you INFER sensible
  colours and set "color_source": "inferred".
- ALWAYS prefer a colour read from a colour page over a guess. If a character or
  item appears on any colour page, lock its colours from there.

OUTFIT CHANGES — avoid a classic trap:
- A garment drawn DARK with screentone in B&W is usually the SAME garment that
  looks lighter/coloured on a colour page — NOT a new outfit. Do NOT open a new
  outfit version just because shading got darker.
- Open a NEW outfit version ONLY when the garment is genuinely a DIFFERENT piece
  (different cut/type: e.g. school uniform → pajamas → formal robe).

Return ONE JSON object, no prose, matching EXACTLY this shape:

{{
  "meta": {{ "chapter": "{chapter}", "page_order": {order} }},
  "story": {{ "synopsis": "2-4 sentences: what this chapter is about", "genre": "...", "tone": "overall mood" }},
  "characters": {{
    "char_01": {{ "name": "...", "role": "protagonist / love interest / maid ...",
      "desc": "hair/eyes/build", "features": "forehead mark, braid, scar, ears...",
      "hair": "#RRGGBB", "skin": "#RRGGBB", "eyes": "#RRGGBB", "color_source": "color_page|inferred" }}
  }},
  "outfits": {{
    "char_01__A": {{ "char": "char_01", "desc": "what this specific outfit is",
      "top": "#RRGGBB", "bottom": "#RRGGBB", "shoes": "#RRGGBB",
      "accents": ["#RRGGBB"], "color_source": "color_page|inferred" }}
  }},
  "props": {{
    "prop_01": {{ "name": "gold hairpin", "desc": "...", "owner": "char_01 or null",
      "colors": ["#RRGGBB"], "color_source": "color_page|inferred" }}
  }},
  "scenes": {{
    "scene_01": {{ "desc": "place + time of day", "palette": ["#RRGGBB", "#RRGGBB"],
      "lighting": "...", "key_elements": ["<element>", "<element>"],
      "elements": {{ "<element>": "#RRGGBB", "<element>": "#RRGGBB" }} }}
  }},
  "pages": {{
    "page_xxx": {{ "scene": "scene_01",
      "present": [ {{ "char": "char_01", "outfit": "char_01__A" }} ],
      "props": ["prop_01"], "beat": "one line: what happens here",
      "mood": "tense|comedic|tender|dramatic|calm...", "lighting": "optional per-page light" }}
  }}
}}

Rules:
- Every recurring character gets a STABLE id (char_01, char_02, ...) with role + features + hair/skin/eyes.
- Each DISTINCT outfit gets a version key "{{char}}__A", "{{char}}__B", ...; keep old versions when clothes truly change.
- List recurring PROPS/accessories/objects that must stay one colour (hairpins, jewelry, weapons, fans, banners, key furniture). Give each a stable id and owner (or null for scenery). Skip one-off background clutter.
- Each SCENE (location) gets a palette + lighting. In "elements", bind each recurring
  background piece to ONE exact hex — this is what keeps a location the same colour
  across pages. USE ELEMENT NAMES THAT FIT THAT SPECIFIC SCENE, do not force a
  template: a bedroom → wall/bed/curtains/floor; a classroom → desk/blackboard/window/floor;
  outdoors → sky/ground/foliage/building; a temple → altar/pillars/floor. Only name
  elements that actually appear. Reuse the SAME element hex for the same location on every page.
- Map EVERY page (use the exact page names above) to its scene, the (char, outfit) present, any props visible, a one-line beat, and a mood.
- A character counts as PRESENT even if they appear ONLY as a small chibi /
  super-deformed reaction figure (big head, tiny body). Recognize them by hair
  colour/style, forehead/face marks and clothing, and still list them in that
  page's "present" (same char id + their current outfit) so their colours stay locked.
- COLOUR QUALITY (critical): every hex must be SPECIFIC and match the hue you
  describe. Do NOT output washed-out placeholders like #F0F0F0 / #FFFFFF / #EEEEEE
  or flat greys for a COLOURED item — if a robe is "light green" give a real light
  green (e.g. #C7D8C0), "light blue" → #C3D6E8, "pink" → #E8B9C4. Reserve pure
  white/black only for items that truly are white or black. A "top" and "bottom"
  of the same garment are usually DIFFERENT colours unless it is one solid piece —
  do not copy the same hex into both out of laziness.
- Distinct characters, outfits, and scene elements MUST get visibly DIFFERENT
  colours. The SAME character/outfit/scene keeps the SAME hex on every page it
  appears — one outfit = one fixed colour, never re-tinted page to page.
- All colours MUST be 6-digit hex like #1A2B3C. Output valid JSON only."""


def _extract_text(payload: dict) -> str:
    for cand in payload.get("candidates") or []:
        for part in (cand.get("content") or {}).get("parts") or []:
            t = part.get("text")
            if isinstance(t, str) and t.strip():
                return t
    return ""


def _downscale_b64(data: bytes) -> tuple[str, str]:
    try:
        from PIL import Image
        im = Image.open(io.BytesIO(data)).convert("RGB")
        w, h = im.size
        m = max(w, h)
        if m > _READ_EDGE:
            s = _READ_EDGE / m
            im = im.resize((max(1, round(w * s)), max(1, round(h * s))))
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=85)
        return base64.b64encode(buf.getvalue()).decode("ascii"), "image/jpeg"
    except Exception:  # noqa: BLE001
        return base64.b64encode(data).decode("ascii"), "image/png"


# ── backends ─────────────────────────────────────────────────────────────────

async def _build_atrium(pages: Sequence[tuple[str, str]], chapter: str, style_ref_id: Optional[str]) -> Bible:
    """Gemini via Atrium — images by R2 URL (inline is rejected)."""
    from flowboard.services.comic import atrium_api, r2

    creds = atrium_api.client_creds()
    if not creds:
        raise RuntimeError("reader(atrium): ATRIUM_CLIENT_ID/SECRET not set")
    if not r2.is_configured():
        raise RuntimeError("reader(atrium): R2 not configured (needed to host page images by URL)")
    headers = {"x-client-id": creds[0], "x-client-secret": creds[1]}

    uploaded: list[str] = []

    def _upload(mid: str) -> Optional[str]:
        url = r2.upload_media(mid)
        if url:
            uploaded.append(mid)
        return url

    try:
        parts: list[dict] = [{"text": _instructions(chapter, [n for n, _ in pages])}]
        if style_ref_id:
            su = await asyncio.to_thread(_upload, style_ref_id)
            if su:
                parts.append({"text": "STYLE REFERENCE (art style only, not colours):"})
                parts.append({"fileData": {"mimeType": "image/jpeg", "fileUri": su}})
        for name, mid in pages:
            u = await asyncio.to_thread(_upload, mid)
            if not u:
                continue
            parts.append({"text": f"=== {name} ==="})
            parts.append({"fileData": {"mimeType": "image/jpeg", "fileUri": u}})

        body = {
            "model": reader_model(),
            "contents": [{"role": "user", "parts": parts}],
            "config": {"responseMimeType": "application/json", "temperature": 0.3},
        }
        url = f"{atrium_api.base_url()}/api/partner/llm/generate"
        return await _post_and_parse(url, headers, None, body)
    finally:
        for mid in uploaded:
            try:
                await asyncio.to_thread(r2.delete_media, mid)
            except Exception:  # noqa: BLE001
                pass


async def _build_gemini(pages: Sequence[tuple[str, str]], chapter: str, style_ref_id: Optional[str]) -> Bible:
    """Direct Gemini — inline base64 images."""
    from flowboard.services import media as media_service
    from flowboard.services.comic import gemini_api

    key = os.getenv("READER_API_KEY", "").strip() or gemini_api.api_key()
    if not key:
        raise RuntimeError("reader(gemini): READER_API_KEY / GEMINI_API_KEY not set")

    def _bytes(mid: str) -> Optional[bytes]:
        p = media_service.cached_path(mid)
        try:
            return p.read_bytes() if p else None
        except OSError:
            return None

    parts: list[dict] = [{"text": _instructions(chapter, [n for n, _ in pages])}]
    if style_ref_id:
        b = await asyncio.to_thread(_bytes, style_ref_id)
        if b:
            d, m = _downscale_b64(b)
            parts.append({"text": "STYLE REFERENCE (art style only, not colours):"})
            parts.append({"inline_data": {"mime_type": m, "data": d}})
    for name, mid in pages:
        b = await asyncio.to_thread(_bytes, mid)
        if not b:
            continue
        d, m = _downscale_b64(b)
        parts.append({"text": f"=== {name} ==="})
        parts.append({"inline_data": {"mime_type": m, "data": d}})

    body = {
        "contents": [{"role": "user", "parts": parts}],
        "generationConfig": {"responseMimeType": "application/json", "temperature": 0.3},
    }
    url = f"{(os.getenv('READER_BASE_URL', '').strip() or _GEMINI_BASE).rstrip('/')}/{reader_model()}:generateContent"
    return await _post_and_parse(url, {}, {"key": key}, body)


async def _post_and_parse(url: str, headers: dict, params: Optional[dict], body: dict) -> Bible:
    """POST the generateContent body, parse candidate text → Bible, retry a few
    times on bad JSON (feeding the error back) or transient HTTP errors."""
    last = "unknown"
    async with httpx.AsyncClient(timeout=_TIMEOUT_S) as client:
        for attempt in range(1, _MAX_ATTEMPTS + 1):
            resp = await client.post(url, headers=headers or None, params=params, json=body)
            if resp.status_code != 200:
                last = f"http_{resp.status_code}: {resp.text[:200]}"
                logger.warning("reader attempt %d: %s", attempt, last)
                if resp.status_code in (400, 401, 403, 404):
                    raise RuntimeError(f"reader fatal {last}")
                await asyncio.sleep(3.0 * attempt)
                continue
            text = _extract_text(resp.json())
            try:
                return parse_bible(json.loads(text))
            except Exception as exc:  # noqa: BLE001
                last = f"{type(exc).__name__}: {exc}"[:200]
                logger.warning("reader attempt %d parse fail: %s", attempt, last)
                body.setdefault("contents", []).append({"role": "model", "parts": [{"text": text[:4000]}]})
                body["contents"].append({"role": "user", "parts": [{
                    "text": f"That was not valid per the schema ({last}). Return ONLY the corrected JSON object."}]})
    raise RuntimeError(f"reader failed after {_MAX_ATTEMPTS} attempts: {last}")


async def build_bible(
    pages: Sequence[tuple[str, str]],
    *,
    chapter: str = "",
    style_ref_id: Optional[str] = None,
) -> Bible:
    """Read the chapter → validated Bible. ``pages`` is [(page_name, media_id)]
    in reading order. Backend chosen by READER_PROVIDER (default 'atrium')."""
    if not pages:
        raise RuntimeError("no pages to read")
    if reader_provider() == "gemini":
        return await _build_gemini(pages, chapter, style_ref_id)
    return await _build_atrium(pages, chapter, style_ref_id)
