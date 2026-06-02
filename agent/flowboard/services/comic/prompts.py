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
