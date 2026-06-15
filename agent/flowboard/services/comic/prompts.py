"""Prompts for the comic clean / 9:16 / anime-enhance steps (Phase 4/5).

Sent verbatim to Nano Banana Pro through the Flow bridge (services.comic.bridge
.edit_image). Kept here so they're versioned + tweakable in one place.
"""

# Phase 4 — remove all lettering and reconstruct the art underneath.
CLEAN_PROMPT = (
    "Remove all text, speech bubbles, captions, and sound-effect lettering from this "
    "comic panel. Where text or a bubble is removed, reconstruct the underlying artwork "
    "by seamlessly extending the surrounding background, characters, and textures, so the "
    "area looks like natural finished art with no trace that anything was ever there. "
    "Match the original art style, line work, colors, lighting, and shading exactly. Do "
    "NOT change, move, or redraw any character, object, or part of the background that is "
    "not covered by text — keep everything else identical to the input. Do NOT add any new "
    "text, watermark, or signature. Do NOT leave any white or light halo, glowing rim, "
    "outline, fringe, or cut-out seam around the characters or objects — the background must "
    "continue seamlessly right up to their edges, with natural contact and shading, so nothing "
    "looks pasted on. Output a clean, high-resolution image."
)

# Appended to CLEAN_PROMPT when the panel should fill a 9:16 vertical frame.
EXTEND_9_16 = (
    " Additionally, extend the scene to fill the ENTIRE 9:16 vertical frame, edge to edge: "
    "naturally continue the environment, the background, and (where natural) the subject's body "
    "into the areas above and below, keeping the original subject and framing intact and "
    "well-composed. If the input shows blurred, stretched, or smeared bands at the top/bottom, "
    "they are placeholder padding — PAINT OVER them completely with real, finished scene "
    "content. Solid black / dark hatched LETTERBOX BARS spanning the full width at the very top "
    "or bottom of the input are cinematic framing, NOT scene content: remove them and continue "
    "the artwork through that space instead. The final image must be 100% finished artwork "
    "across the whole 9:16 canvas: NO letterbox bars, NO black/white/empty bands, NO unfinished "
    "placeholder areas. STYLE LOCK for everything newly painted: match the original panel's "
    "exact art style — same line work, cel-shaded anime/manhwa rendering, same palette and "
    "level of detail. Never shift toward photorealism, western-comic rendering, or a different "
    "character design. Blend the extended areas seamlessly into the original art with no "
    "visible border, seam, or pale halo where the new and original regions meet."
)

# Phase 5 — re-render as a cinematic anime still while preserving the scene.
ENHANCE_PROMPT = (
    "Re-render this manhwa/webtoon panel as a high-quality, cinematic anime production "
    "still, while keeping the scene exactly the same.\n\n"
    "KEEP UNCHANGED: the characters and their identities, facial features, expressions, "
    "hairstyles, clothing and accessories, poses, body proportions, and the overall "
    "composition and camera angle. Keep the same action and the same story moment. Do not "
    "add, remove, or reposition any character or major object.\n\n"
    "ENHANCE: redraw with clean, crisp, confident line art; add rich layered cel shading "
    "with soft secondary shadows and subtle ambient occlusion; deepen and harmonize the "
    "colors with a cinematic color grade; add atmospheric lighting (rim light, soft key "
    "light, gentle bloom on highlights) that matches the existing light direction; sharpen "
    "detail in the eyes, hair strands, fabric folds, and skin; smooth gradients and remove "
    "any compression artifacts, jagged edges, or blur. Aim for the polished look of a modern "
    "theatrical anime film frame.\n\n"
    "BACKGROUND: if the background is empty, flat, or unfinished, generate a detailed, "
    "context-appropriate background that fits the scene's setting, mood, time of day, and "
    "perspective. Keep it consistent with what the characters are doing and where they are, "
    "and never let it overpower the subjects.\n\n"
    "DO NOT: add any text, speech bubbles, captions, sound effects, logos, watermarks, or "
    "signatures. Keep it 2D hand-drawn anime — no realism, no 3D, no photographic look. Do "
    "not alter the characters' identities or redesign them. Do not leave any white or light "
    "halo, glowing outline, fringe, or cut-out seam around characters or objects — blend them "
    "into the background seamlessly with natural contact shadows.\n\n"
    "Output a clean, high-resolution image."
)

# Appended when extra reference images are supplied (whole-story consistency).
REFERENCE_CLAUSE = (
    " Use the additional reference images to keep character designs, costumes, and the "
    "setting consistent."
)

COMBINE_CHARACTER_REFERENCE_CLAUSE = (
    " Use the additional reference images only as character identity and costume references. "
    "Do not copy their backgrounds, camera angles, poses, layouts, or scene content into this "
    "panel. The source panel remains the ground truth."
)

# ── Axis-aware OVERRIDE clauses ───────────────────────────────────────────────
# The default combine/regen prompt orders the model to stay 100% faithful to the
# source panel, so an attached canon reference is only a gentle identity hint and
# the source wins every disagreement. That is correct for the common case (source
# already on-model). But on the panels canon exists to FIX — an off-model drawing,
# an outfit-change boundary, an art restyle — the source must NOT win. These
# clauses invert authority for a NAMED axis only: the source keeps pose / layout /
# camera / composition, while the reference becomes the design of record for that
# axis and the model must match it even where the source panel differs. Selected
# per panel via ``combine_reference_clause(override_axes=[...])``.
OVERRIDE_AXES = ("identity", "outfit", "style")

# The single most important guard: a strong reference (especially a full-body
# character sheet) tends to drag its OWN framing/zoom/pose into an empty or
# tightly-cropped source panel — e.g. a close-up of a face gets redrawn as the
# reference's full body. This locks the SHOT to the source so the reference only
# governs how the character LOOKS, never the camera. Appended whenever refs are
# attached, in both the gentle and the override paths.
_FRAMING_LOCK = (
    " CRITICAL — keep the SOURCE panel's exact shot scale, framing, crop, and the direction the "
    "character faces: if the source is a close-up the result stays that close-up; if the source "
    "shows the back of the head, a profile, or the character turned away, the result MUST keep that "
    "exact orientation. Do NOT zoom out, change the camera distance, add the rest of the body, "
    "rotate the character to reveal the face, or adopt the reference image's framing, pose, facing "
    "direction, or how much of the character is shown. The reference guides ONLY how the character "
    "looks (face, hair, design), never the shot, the orientation, or the amount of body visible."
)

_OVERRIDE_PREAMBLE = (
    " IMPORTANT: the source panel's drawing is OFF-MODEL and must be CORRECTED to match the "
    "reference image(s). Treat the source panel as ground truth ONLY for pose, body position, "
    "camera angle, framing, shot scale, the direction the character faces, layout, action, and "
    "overall composition — not for the attributes below."
)
_OVERRIDE_TAIL = (
    " Do not otherwise copy the reference images' backgrounds, poses, camera angles, framing, or "
    "scene content into this panel."
)


def _identity_override(char_name: str | None) -> str:
    who = f" the character {char_name}" if char_name else " the character"
    return (
        f" The reference image is the authoritative design for{who}: correct ONLY the parts of the "
        "character that are ACTUALLY VISIBLE in the source (face, facial features, eye shape, "
        "hairstyle, hair colour, build) so they match the reference's design. Do NOT add anything "
        "the source does not show: if the source shows the back of the head, a profile, or the "
        "character facing away, KEEP that orientation and do NOT rotate them to reveal the face — "
        "just render whatever IS visible on-model. Stay within the source's framing and shot scale."
    )


def _outfit_override(outfit: str | None) -> str:
    garment = f" ({outfit})" if outfit else ""
    return (
        f" If clothing is visible in the source panel, replace it with the outfit{garment} shown in "
        "the reference image, keeping the character's face, hair, pose, framing, and composition "
        "unchanged. Do not zoom out or add the body just to show the outfit."
    )


_STYLE_OVERRIDE = (
    " Match the line weight, shading, rendering technique, and colour palette of the style "
    "reference image."
)


def _char_sentence(char_name: str | None, char_desc: str | None) -> str:
    """Verbatim character anchor appended to every prompt that carries refs.
    Reusing the EXACT same descriptor wording across panels measurably improves
    cross-panel consistency (prompt-token consistency), so this is built from
    stored fields — never paraphrased per panel."""
    if not char_name and not char_desc:
        return ""
    if char_name and char_desc:
        return f" The character is {char_name}: {char_desc}"
    return f" The character is {char_name}." if char_name else f" The character: {char_desc}"


def combine_reference_clause(
    *,
    char_name: str | None = None,
    char_desc: str | None = None,
    outfit: str | None = None,
    override_axes: object = None,
) -> str:
    """Build the reference clause appended to a combine/regen prompt when refs are
    attached.

    Default (``override_axes`` empty/None): the gentle identity+costume hint —
    refs guide identity, the source panel stays ground truth.

    With ``override_axes`` (a subset of :data:`OVERRIDE_AXES`): authority is
    inverted for exactly those axes — the reference becomes the design of record
    for identity / outfit / style while the source keeps pose, layout, and
    composition. ``char_name`` / ``outfit`` are woven into the wording so the
    model knows who and what.
    """
    axes = [a for a in OVERRIDE_AXES if isinstance(override_axes, (list, tuple, set)) and a in override_axes]
    if not axes:
        return COMBINE_CHARACTER_REFERENCE_CLAUSE + _FRAMING_LOCK + _char_sentence(char_name, char_desc)
    parts = [_OVERRIDE_PREAMBLE]
    if "identity" in axes:
        parts.append(_identity_override(char_name))
    if "outfit" in axes:
        parts.append(_outfit_override(outfit))
    if "style" in axes:
        parts.append(_STYLE_OVERRIDE)
    parts.append(_FRAMING_LOCK)
    parts.append(_char_sentence(char_name, char_desc))
    parts.append(_OVERRIDE_TAIL)
    return "".join(parts)

# Combine 4 panels (pre-stitched into a rough 2×2) into one clean vertical 9:16
# storyboard image: remove text, keep characters 100% faithful, extend only
# backgrounds. Sent with the stitched composite as the single source image.
COMBINE_2X2_PROMPT = """The source image is ALREADY a 2×2 grid of four chosen comic/manga/manhwa panels in reading order (top-left → top-right → bottom-left → bottom-right), separated by plain margins.

YOUR TASK — do ONLY these two things:
1. Remove all text/bubbles/SFX and reconstruct the artwork hidden behind them.
2. Seamlessly extend each panel's own background into the surrounding plain/empty margin areas so the four panels merge into one clean, gap-free vertical 9:16 image.

DO NOT re-select, swap, reorder, move, resize, crop out, duplicate, or redraw any panel. DO NOT invent new panels, new characters, or extra sub-images. Keep exactly these four panels in these exact 2×2 positions. The plain margin areas are empty space to fill by extending the adjacent panel's background ONLY — never place new subjects there.

LAYOUT

[ Panel 1 ] [ Panel 2 ]
[ Panel 3 ] [ Panel 4 ]

The final composition is one vertical 9:16 canvas, four panels of roughly equal visual weight, in the original reading order above.

CRITICAL RULE: STAY 100% FAITHFUL TO THE ORIGINAL ARTWORK

Treat the source image as the ground truth.

Do NOT redesign, reinterpret, regenerate, or alter any character.

Preserve exactly:
- Character identity
- Facial features
- Face proportions
- Eye shape
- Hair shape
- Hair silhouette
- Hair color
- Anatomy
- Body proportions
- Clothing
- Accessories
- Expressions
- Poses
- Art style
- Linework
- Rendering style
- Color palette

The characters must remain identical to the source image.

No face changes.
No anatomy changes.
No costume changes.
No hairstyle changes.
No expression changes.

PANEL SELECTION

Analyze the page and identify the four most important narrative beats following the original reading flow.

Select the strongest moments based on:
- emotional impact
- character reactions
- action
- reveals
- dramatic dialogue moments
- important story progression

TEXT REMOVAL

Remove:
- Speech bubbles
- Dialogue balloons
- Narration boxes
- Captions
- Sound effects
- Onomatopoeia
- Watermarks
- Page numbers
- All text

Reconstruct the hidden artwork behind removed elements.

No traces of text or bubble outlines should remain.

REFRAMING RULES

The objective is reframing, not redrawing.

Prioritize:
1. Smart cropping
2. Recomposition
3. Canvas extension
4. Background outpainting

Avoid regenerating existing artwork.

Do not redraw faces.

Do not redraw bodies.

Do not modify poses.

Do not alter character proportions.

EXTENSION RULES

If additional space is required to fit the composition:

Only extend:
- Backgrounds
- Environment
- Architecture
- Walls
- Floors
- Ceilings
- Sky
- Atmospheric effects
- Empty surrounding space

Never modify the original character artwork.

All generated content should feel like a natural continuation of the original scene.

VISUAL STYLE

Maintain:
- Original comic/manhwa style
- Original rendering
- Original colors
- Original lighting
- Original storytelling intent

FINAL GOAL

Create a single mobile-friendly 9:16 image containing four story panels arranged in a clean 2×2 grid.

The final image should feel like an official storyboard adaptation of the original page, with all text removed, all characters preserved exactly, and the artwork expanded only where necessary to fit the new layout."""
