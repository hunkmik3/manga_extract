"""The colorization *bible* — the single source of truth for a chapter's colours.

Input B&W pages have no "true" colour, so whoever fixes the palette first makes
the law. The bible holds that law: fixed hex per character (hair/skin/eyes) and
per outfit (top/bottom/shoes), plus per-scene palette/lighting and a per-page
cast list. Every page's prompt is built from these fixed values, so all pages
look at ONE palette → consistency (instead of "remembering" the previous page,
which drifts).

Key split: ``characters`` (hair/skin — never change) vs ``outfits`` (clothes —
versioned, ``{char}__{version}``), so a wardrobe change opens a new outfit
version without disturbing the character's identity colours.
"""
from __future__ import annotations

import re
from typing import Optional

from pydantic import BaseModel, Field, field_validator

_HEX_RE = re.compile(r"^#[0-9a-fA-F]{6}$")


def _norm_hex(v: Optional[str], default: str) -> str:
    """Coerce a model-supplied colour to ``#RRGGBB``; fall back to ``default``.
    VLMs emit '#abc', 'abc123', 'rgb(...)', names — we keep only clean 6-digit
    hex and let the human fix the rest at the checkpoint."""
    if not isinstance(v, str):
        return default
    s = v.strip()
    if not s.startswith("#"):
        s = "#" + s
    if _HEX_RE.match(s):
        return s.upper()
    # '#abc' shorthand → expand
    short = s.lstrip("#")
    if len(short) == 3 and all(c in "0123456789abcdefABCDEF" for c in short):
        return ("#" + "".join(c * 2 for c in short)).upper()
    return default


def _norm_hex_list(v: object) -> list[str]:
    """Coerce a list of model-supplied colours to clean ``#RRGGBB``, dropping
    anything unrecoverable."""
    if not isinstance(v, list):
        return []
    out: list[str] = []
    for c in v:
        if isinstance(c, str):
            h = _norm_hex(c, "")
            if h:
                out.append(h)
    return out


# color_source tells the reviewer how much to trust a colour: "color_page" =
# read from an actual coloured page (cover/splash) → ground truth; "inferred" =
# guessed from a B&W page → eyeball it. Free string, lenient.
_COLOR_SOURCE_DEFAULT = "inferred"


class Character(BaseModel):
    name: str = "Unknown"
    role: str = ""  # who they are in the story (protagonist, love interest, maid…)
    desc: str = ""  # hair/eyes/build
    features: str = ""  # distinguishing marks (forehead mark, scar, braid, ears…)
    hair: str = "#333333"
    skin: str = "#E7C6A5"
    eyes: str = "#4A3A2A"
    color_source: str = _COLOR_SOURCE_DEFAULT

    @field_validator("hair", mode="before")
    @classmethod
    def _h(cls, v):  # noqa: N805
        return _norm_hex(v, "#333333")

    @field_validator("skin", mode="before")
    @classmethod
    def _s(cls, v):  # noqa: N805
        return _norm_hex(v, "#E7C6A5")

    @field_validator("eyes", mode="before")
    @classmethod
    def _e(cls, v):  # noqa: N805
        return _norm_hex(v, "#4A3A2A")


class Outfit(BaseModel):
    char: str  # character id this outfit belongs to
    desc: str = ""
    top: str = "#CCCCCC"
    bottom: str = "#888888"
    shoes: str = "#444444"
    accents: list[str] = Field(default_factory=list)  # trims/sashes/patterns
    color_source: str = _COLOR_SOURCE_DEFAULT

    @field_validator("top", mode="before")
    @classmethod
    def _t(cls, v):  # noqa: N805
        return _norm_hex(v, "#CCCCCC")

    @field_validator("bottom", mode="before")
    @classmethod
    def _b(cls, v):  # noqa: N805
        return _norm_hex(v, "#888888")

    @field_validator("shoes", mode="before")
    @classmethod
    def _sh(cls, v):  # noqa: N805
        return _norm_hex(v, "#444444")

    @field_validator("accents", mode="before")
    @classmethod
    def _ac(cls, v):  # noqa: N805
        return _norm_hex_list(v)


class Prop(BaseModel):
    """A recurring object/accessory that must stay one colour across pages —
    hairpin, jewelry, weapon, fan, banner, key furniture, etc."""

    name: str = ""
    desc: str = ""
    owner: Optional[str] = None  # character id it belongs to, or None (scenery)
    colors: list[str] = Field(default_factory=list)
    color_source: str = _COLOR_SOURCE_DEFAULT

    @field_validator("colors", mode="before")
    @classmethod
    def _c(cls, v):  # noqa: N805
        return _norm_hex_list(v)


class Scene(BaseModel):
    """A setting/location with a locked palette. ``elements`` binds each recurring
    background piece to ONE exact colour (wall→hex, floor→hex, bed→hex…) — this is
    what actually keeps a room the same colour across pages; a loose ``palette``
    list lets the model drift (same room, different cast each page)."""

    desc: str = ""
    palette: list[str] = Field(default_factory=list)
    lighting: str = ""
    key_elements: list[str] = Field(default_factory=list)
    elements: dict[str, str] = Field(default_factory=dict)  # element name → #RRGGBB

    @field_validator("palette", mode="before")
    @classmethod
    def _p(cls, v):  # noqa: N805
        if not isinstance(v, list):
            return []
        return [_norm_hex(c, "#808080") for c in v if isinstance(c, str)]

    @field_validator("key_elements", mode="before")
    @classmethod
    def _ke(cls, v):  # noqa: N805
        if not isinstance(v, list):
            return []
        return [str(x) for x in v if isinstance(x, (str, int, float))]

    @field_validator("elements", mode="before")
    @classmethod
    def _el(cls, v):  # noqa: N805
        if not isinstance(v, dict):
            return {}
        out: dict[str, str] = {}
        for k, c in v.items():
            if isinstance(k, str) and isinstance(c, str):
                h = _norm_hex(c, "")
                if h:
                    out[k.strip()] = h
        return out


class Story(BaseModel):
    """Chapter-level comprehension: what it is + the emotional tone, so colour
    grading can follow the story (a nightmare beat ≠ a bright school beat)."""

    synopsis: str = ""
    genre: str = ""
    tone: str = ""


class PagePresence(BaseModel):
    char: str
    outfit: Optional[str] = None  # outfit key; None → use character base only


class Page(BaseModel):
    scene: Optional[str] = None
    present: list[PagePresence] = Field(default_factory=list)
    props: list[str] = Field(default_factory=list)  # prop ids visible on the page
    beat: str = ""  # one-line: what happens on this page
    mood: str = ""  # emotional tone (tense, comedic, tender, dramatic…)
    lighting: str = ""  # per-page light overriding the scene's if needed

    @field_validator("props", mode="before")
    @classmethod
    def _pr(cls, v):  # noqa: N805
        if not isinstance(v, list):
            return []
        return [str(x) for x in v if isinstance(x, str)]


class BibleMeta(BaseModel):
    chapter: str = ""
    page_order: list[str] = Field(default_factory=list)


class Bible(BaseModel):
    """The full, validated bible. Lenient by design (extra fields ignored) since
    it comes from a VLM; the human fixes anything wrong at the checkpoint."""

    model_config = {"extra": "ignore"}

    meta: BibleMeta = Field(default_factory=BibleMeta)
    story: Story = Field(default_factory=Story)
    characters: dict[str, Character] = Field(default_factory=dict)
    outfits: dict[str, Outfit] = Field(default_factory=dict)
    props: dict[str, Prop] = Field(default_factory=dict)
    scenes: dict[str, Scene] = Field(default_factory=dict)
    pages: dict[str, Page] = Field(default_factory=dict)

    def summary(self) -> str:
        """Human-readable checkpoint summary (chars, outfit versions, wardrobe
        changes) so the reviewer knows what to eyeball before colorizing."""
        lines = [
            f"Chapter: {self.meta.chapter or '(unnamed)'}",
            f"Pages: {len(self.pages)}  |  Characters: {len(self.characters)}  "
            f"|  Outfits: {len(self.outfits)}  |  Props: {len(self.props)}  "
            f"|  Scenes: {len(self.scenes)}",
        ]
        if self.story.synopsis:
            lines.append(f"Story: {self.story.synopsis}")
        for cid, c in self.characters.items():
            versions = [k for k, o in self.outfits.items() if o.char == cid]
            lines.append(f"  • {cid} ({c.name}): hair {c.hair}, skin {c.skin}, eyes {c.eyes} "
                         f"— {len(versions)} outfit(s): {', '.join(versions) or '—'}")
        # wardrobe changes: a page whose outfit differs from the character's
        # first-seen outfit in page_order.
        seen: dict[str, str] = {}
        for pg in self.meta.page_order:
            page = self.pages.get(pg)
            if not page:
                continue
            for pr in page.present:
                if pr.outfit and pr.char in seen and seen[pr.char] != pr.outfit:
                    lines.append(f"  ↳ {pr.char} changes to {pr.outfit} at {pg}")
                if pr.outfit:
                    seen.setdefault(pr.char, pr.outfit)
        return "\n".join(lines)


def parse_bible(raw: object) -> Bible:
    """Validate arbitrary (VLM/user) data into a Bible. Raises pydantic
    ValidationError on unrecoverable shape problems (caller retries the VLM)."""
    if not isinstance(raw, dict):
        raise ValueError("bible must be a JSON object")
    return Bible.model_validate(raw)
