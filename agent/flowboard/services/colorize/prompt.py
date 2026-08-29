"""Build the per-page colorize prompt from the bible.

COLOR LOCK goes FIRST (models weight the head of the prompt most), then the
style / environment / lighting / cleanup / negative blocks that already work.
"""
from __future__ import annotations

from flowboard.services.colorize.bible import Bible

_STYLE_BLOCK = """Text & bubble removal: remove every speech/thought/dialogue/
narration bubble and box, all dialogue, captions, manga/comic lettering,
Japanese/English/other-language text, promotional and page-edge typography, and
all SFX lettering/symbols that function as type — keep only text that is an
essential physical part of the environment. After removing them, reconstruct the
hidden artwork naturally: any covered hair, face, body, hands, clothing, props,
furniture, architecture and background must follow the surrounding perspective,
proportions, line-art style and drawing density so it reads as if the page had
been drawn without any text from the start. Leave NO empty white bubble shapes,
erased patches, text remnants, ghost letters, white bubble outlines or retouch
artifacts.

Line-art lock: preserve the original black line art as accurately as possible —
sharp, clean, readable; keep original line weight, hierarchy and the artist's line
character; preserve facial line detail, hair strands, cloth folds, environment
linework and intentional inked black shadow shapes. Reconstructed lines must match
the original line quality. Do NOT thicken, thin, vectorize, blur, soften or
needlessly redraw the lines.

B&W to colour: interpret grayscale values, screentones, hatching and black shadow
shapes by their visual FUNCTION (material separation, depth, lighting, shadow
hierarchy) rather than copying grayscale literally. Replace manga screentones and
halftones with clean solid anime colour areas and larger simplified cel-shading
shapes; remove visible halftone dots from coloured surfaces; keep a screentone
only when it is clearly an intentional graphic effect. No screentone noise in the
final colours.

Anime colouring style: authentic high-quality Japanese hand-drawn 2D anime
colouring — clean solid LOCAL colours, flat graphic colour blocking, STRICT
two-tone cel shading (one clean flat base colour plus one clearly separated
hard-edged shadow tone per major material; a small solid highlight only when
necessary). Large deliberate shadow shapes, clean hard-edged cel boundaries.
Avoid micro-shading and detailed realistic surface rendering; keep skin and fabric
matte and graphic. Fully colour every region with a definite colour — no
white/blank/unpainted patches.

Lighting: infer the most believable primary light direction from the original and
respect its light/shadow logic and existing cast/inked black shadows; translate
the original shading into simplified anime lighting with a clear readable hierarchy
and large simple shadow groups. Lighting enhances the original composition, never
redesigns it.

Cinematic colour direction: a harmonious cinematic Japanese-anime palette suited
to the mood, time of day, location and narrative tone. Build controlled warm/cool
contrast to separate light and shadow. Use RESTRAINED saturation — controlled
medium or medium-low saturation for most surfaces, reserving stronger saturation
for meaningful focal points. Keep the palette cohesive across the whole page.
Avoid neon (unless the original scene demands it), rainbow palettes, muddy
mixtures, brown/grey dullness, and random or clashing saturated colours.

Consistency: give each character stable, believable local colours and keep them
IDENTICAL every time that character appears — including simplified chibi /
super-deformed / comedic versions (same established palette, preserve that drawing
style). Keep different characters distinguishable within one cohesive palette.
Colour the environment to match the actual scene shown and adapt the palette to
each location; keep clear foreground/midground/background separation. If a panel's
background is an abstract treatment (decorative pattern, screentone, sparkles/
bokeh, speed-lines or a plain field with no real setting), apply that abstract
treatment UNIFORMLY across the WHOLE panel edge-to-edge as one coherent tone — do
not leave part patterned/coloured and part blank white, and do not invent concrete
scenery that was not drawn.

Page structure: preserve all panel borders, gutters and page composition exactly;
keep margins clean and neutral; do not add, merge or split panels, and do not
expand beyond the original composition except where reconstruction behind removed
text requires it.

Target: an official Japanese TV-anime adaptation of this page — sharp original
line art, clean solid colour blocking, strict two-tone cel shading, large
hard-edged shadow shapes, matte surfaces, a harmonious cinematic palette, high
character and environment colour consistency, and clean reconstruction beneath all
removed text.

Negative: no visible speech/thought bubbles, dialogue/narration boxes, captions,
typography, dialogue/promotional text, SFX lettering, text remnants, ghost letters,
empty bubble shapes or white bubble outlines; no censorship patches, no broken
reconstruction; no redesign, no altered character identity/faces/anatomy/
proportions/poses/costumes, no changed camera angle or panel layout, no added
characters or objects; no 3D, CGI, photorealism, painterly/watercolour/oil/
airbrush rendering, soft gradient or complex multitone shading, ambient occlusion,
PBR materials, glossy CGI skin, bloom, glow, lens flare, blur, noise or grain; no
dirty colours, colour bleeding, uncontrolled saturation, colour spill outside the
line art, or white/blank/unpainted areas."""

_HEADER = (
    "Colorize this exact black-and-white manga/comic page into a polished, "
    "professional Japanese 2D anime colour illustration. The provided page is the "
    "ABSOLUTE source of truth for all structure, composition, panel layout, line "
    "art and storytelling — preserve the original drawing exactly and only add "
    "colour. Preserve the page dimensions, aspect ratio, crop, panel borders, "
    "framing, camera angles, perspective, shot scale, character placement, "
    "proportions, anatomy, poses, gestures, expressions, eye shapes, hairstyles, "
    "clothing, accessories, hands, props, architecture, furniture and every "
    "original non-text visual element. Do NOT redesign, reinterpret, simplify, "
    "move, resize, rotate, add or remove anything; do NOT change character identity."
)


def _clist(hexes) -> str:
    return ", ".join(h for h in (hexes or []) if h)


def build_color_lock(bible: Bible, page_filename: str) -> str:
    """The per-page COLOR LOCK: everything the model must colour consistently on
    THIS page — present characters (identity + current outfit), visible props,
    the setting palette + its recurring elements, and the page's mood/lighting so
    the grade follows the story. All values come from the ONE bible → every page
    references the same fixed colours (no drift, no per-page guessing)."""
    page = bible.pages.get(page_filename)
    scene = bible.scenes.get(page.scene) if (page and page.scene) else None

    lines: list[str] = []

    # Mood + lighting first so the model sets the overall grade for this page.
    mood = (page.mood if page else "") or ""
    lighting = ((page.lighting if page else "") or (scene.lighting if scene else "")).strip()
    if mood or lighting:
        lines.append(
            f"Page mood: {mood or 'natural'}. Lighting: {lighting or 'match the setting, balanced exposure'}."
        )

    lines.append("=== COLOR LOCK (non-negotiable — use these EXACT colours) ===")
    body_start = len(lines)

    if page:
        for pr in page.present:
            c = bible.characters.get(pr.char)
            if c:
                feat = f" [{c.features}]" if c.features else ""
                lines.append(f"- {c.name}{feat}: hair {c.hair}, skin {c.skin}, eyes {c.eyes}")
            o = bible.outfits.get(pr.outfit) if pr.outfit else None
            if o and c:
                acc = f", accents {_clist(o.accents)}" if o.accents else ""
                lines.append(
                    f'    · {c.name} wears "{o.desc}": top {o.top}, bottom {o.bottom}, shoes {o.shoes}{acc}'
                )
        # Recurring props visible on this page — keep each ONE colour everywhere.
        for pid in (page.props or []):
            p = bible.props.get(pid)
            if p and p.colors:
                own = f" ({bible.characters[p.owner].name}'s)" if (p.owner and p.owner in bible.characters) else ""
                lines.append(f"- {p.name}{own}: {_clist(p.colors)}")
        if scene:
            if scene.elements:
                # Per-element binding = the strong lock: this room's wall/floor/bed
                # etc. must be these EXACT colours on every page in this setting, so
                # the same location never drifts to a different cast page to page.
                lines.append(f'Setting "{scene.desc}" — background colours are FIXED, identical on every page of this location:')
                for name, hx in scene.elements.items():
                    lines.append(f"    · {name}: {hx}")
            else:
                pal = _clist(scene.palette) or "(setting palette)"
                ke = f" — keep {', '.join(scene.key_elements)} the same colour every appearance" if scene.key_elements else ""
                lines.append(f'Setting "{scene.desc}": palette {pal}{ke}')

    if len(lines) == body_start:
        lines.append("- (no cast recorded for this page — keep colours natural and consistent)")
    lines.append("=== END COLOR LOCK ===")
    return "\n".join(lines)


def build_sheet_prompt(
    bible: Bible, char_id: str, outfit_id: Optional[str] = None, *, ref_is_colored: bool = False
) -> str:
    """Prompt to GENERATE a professional character model-sheet (turnaround) for one
    character×outfit. The provided image is the ABSOLUTE design reference (identity
    lock). When ``ref_is_colored`` (the reference is a real colour page / cover),
    COLOURS are read straight from that image — the bible's guessed hex is NOT
    injected, since it can be wrong and would pull the model off the true colours.
    Otherwise the bible hex fills the colour lock. Species-specific traits ride in
    via the character's ``features`` string, so this stays generic."""
    c = bible.characters.get(char_id)
    name = (c.name if c else char_id) or char_id
    design = ", ".join(x for x in [(c.desc if c else ""), (c.features if c else "")] if x) or "(see reference image)"
    o = bible.outfits.get(outfit_id) if outfit_id else None

    if ref_is_colored:
        colour_line = (
            "- COLOURS: read EVERY colour — hair, skin, eyes, and each clothing "
            "layer and accessory — DIRECTLY from the reference image; it is the "
            "authoritative colour source. Reproduce them EXACTLY; do NOT invent, "
            "brighten, desaturate or shift any colour. Keep identical across all views."
        )
        outfit_line = (
            f'- Outfit: "{o.desc}" — reproduce its colours from the reference EXACTLY, keep every layer/cut identical in every view.'
            if o else
            "- Outfit: exactly as worn in the reference image — reproduce its colours exactly, identical in every view."
        )
    else:
        colours = f"hair {c.hair}, skin {c.skin}, eyes {c.eyes}" if c else "(as in reference)"
        colour_line = f"- EXACT colours, identical in every view: {colours}."
        if o:
            acc = f", accents {_clist(o.accents)}" if o.accents else ""
            outfit_line = f'- Outfit: wearing "{o.desc}" (top {o.top}, bottom {o.bottom}, shoes {o.shoes}{acc}) — keep every layer, cut and colour identical in every view.'
        else:
            outfit_line = "- Outfit: as shown in the reference image — keep it identical in every view."

    return f"""Create a professional character MODEL SHEET (turnaround) for "{name}".

CHARACTER DESIGN LOCK — the provided image is the ABSOLUTE hard reference. Keep the
character 100% identical to it in every view:
- Same face, age, head shape, eye shape, hairstyle/hair, and all distinguishing
  features. Do NOT redesign, do NOT invent a new face.
- Character: {design}.
{colour_line}
{outfit_line}
It must clearly be the SAME single character in every view.
If the reference image contains more than one character, build the sheet for
"{name}" ONLY (identify them by the description/features above) and ignore any
other character. The reference is already in colour — read the real colours
(hair, skin, eyes, and every clothing layer) directly from it.

LAYOUT (arrangement only):
- LEFT — a 2x2 block of bust/portrait close-ups: (1) front, (2) 3/4, (3) side
  profile, (4) back. Clearly show the face, eyes, hair, head/facial features,
  neck, shoulders and upper torso.
- RIGHT — a horizontal FULL-BODY turnaround lineup: front, 3/4 front, side
  profile, back. All at the SAME scale, same height, same foot baseline, same
  neutral standing pose, arms relaxed. No action pose, no exaggerated perspective.

TURNAROUND CONSISTENCY: every view is the exact same character — identical face,
proportions, hairstyle, features, outfit and colours across ALL angles. Back views
keep consistent head/hair/outfit logic; do not invent new details.

STYLE: strict 2D Japanese anime model-sheet quality. Clean sharp anime line art,
flat colours, hard-edged cel shading (2-3 tones), matte rendering, very clean
readable shapes, professional presentation. No painterly, no realistic, no 3D.

BACKGROUND: clean plain light/white background. No environment, no props, no
lighting effects, no text, no logo, no watermark.

NEGATIVE: no redesign, no different face, no hairstyle change, no different outfit,
no different colours, no anatomy drift, no inconsistent turnaround, no action pose,
no perspective distortion, no 3D, no CGI, no photorealism, no painterly/airbrush
shading, no text, no logo, no watermark."""


_SHEET_CLAUSE = (
    "The extra reference image(s) provided are CHARACTER SHEETS. Use them ONLY to "
    "match each character's OUTFIT — its colours, patterns, layers and design. Take "
    "the FACE, expression, hairstyle drawing, pose, body and background straight "
    "from the black-and-white page itself. Do NOT copy the sheet's pose, do NOT "
    "redraw or replace faces with the sheet's face — only borrow the outfit colours."
)


_REMOVE_MANDATE = (
    "MANDATORY FIRST TASK — TEXT & BUBBLE REMOVAL (ZERO TOLERANCE, overrides "
    "everything else): carefully SCAN every panel and every corner of the page and "
    "COMPLETELY remove ALL of the following — every speech bubble, thought bubble, "
    "dialogue box and narration box; ALL text of any kind (dialogue, captions, "
    "furigana, Japanese/English/any-language lettering, numbers, vertical text, "
    "page-edge and promotional text); and EVERY sound-effect (SFX) letter or "
    "symbol that functions as typography — including any text that is small, faint, "
    "partial, tilted, stylised, overlapping artwork, or sitting in the background. "
    "Then reconstruct the artwork hidden underneath so it looks drawn without any "
    "text. The finished page MUST contain ZERO readable characters, ZERO leftover "
    "bubble shapes and ZERO white bubble outlines anywhere. If in doubt about "
    "whether something is text, remove it (unless it is an essential physical sign "
    "that is part of the environment)."
)


def build_prompt(bible: Bible, page_filename: str, *, sheet_refs: bool = False) -> str:
    """Full page prompt: REMOVE mandate → (story register) → (sheet role) → header
    → COLOR LOCK → style/cleanup. The text-removal mandate leads because models
    weight the head of the prompt most and leftover bubbles/text are the top
    complaint. When ``sheet_refs`` is set, character sheets are attached as
    references — use them for OUTFIT colours only, keeping faces/poses from B&W."""
    head = _HEADER
    tone = (bible.story.tone or "").strip()
    if tone:
        head = f"This chapter's overall tone: {tone}.\n{head}"
    if sheet_refs:
        head = f"{head}\n\n{_SHEET_CLAUSE}"
    return f"{_REMOVE_MANDATE}\n\n{head}\n\n{build_color_lock(bible, page_filename)}\n\n{_STYLE_BLOCK}"
