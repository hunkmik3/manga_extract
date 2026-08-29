"""Manga colorizer — read-first, color-locked chapter colorization.

Two passes over a folder of B&W pages, with a human checkpoint between:

  PASS 1  build_bible : a vision model reads the whole chapter → a "bible"
                        (characters, outfits, scenes, per-page cast) with fixed
                        hex colours. Human reviews/edits the bible.
  PASS 2  colorize    : per page, build a COLOR-LOCK prompt from the bible and
                        call Seedream (Ark) so every page reads the SAME fixed
                        palette → cross-page consistency.

The engine (Seedream 5.0 Pro via DanceSee B2B ``services.comic.dancesee_api``,
content-filter-disabled) and the LAB colour snap
(``worker.processor._match_reference_colors``) are shared with Flow. Override
with ``COLORIZE_PROVIDER=avis`` (moderated) or ``ark`` (BytePlus direct).
"""
