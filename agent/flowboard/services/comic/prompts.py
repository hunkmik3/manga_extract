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
    "text, watermark, or signature. Output a clean, high-resolution image."
)

# Appended to CLEAN_PROMPT when the panel should fill a 9:16 vertical frame.
EXTEND_9_16 = (
    " Additionally, extend the scene to fill a full 9:16 vertical frame: naturally continue "
    "the environment and background into the empty areas above and below, keeping the "
    "original subject and framing intact and well-composed."
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
    "not alter the characters' identities or redesign them.\n\n"
    "Output a clean, high-resolution image."
)

# Appended when extra reference images are supplied (whole-story consistency).
REFERENCE_CLAUSE = (
    " Use the additional reference images to keep character designs, costumes, and the "
    "setting consistent."
)

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
