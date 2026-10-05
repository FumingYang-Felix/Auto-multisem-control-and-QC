r"""Where each beam sits inside an mFoV, as a single shared table.

The dashboard needs per-beam positions for two things: laying out one mFoV's
beams in a viewer, and working out which beams are nearest a click. The
obvious way - ship every beam's absolute coordinate - is not affordable: a
400-mFoV section has ~36,000 of them, and the page is already 7.5 MB mostly
because of the per-beam URL list.

The 91 beams sit in the same arrangement in every mFoV, so one table of
offsets relative to the mFoV centre covers the whole section in about a
kilobyte. Read from one mFoV's image_coordinates.txt and reused for all.

Offsets are in FULL-RESOLUTION pixels, the same units image_coordinates.txt
uses, so a caller can scale them by whatever downsample it is displaying at.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Optional, Sequence

_BEAM_RE_KEYS = ("beam_index_regexes",)


def _beam_index(name: str, cfg: Dict[str, Any]) -> Optional[int]:
    """Beam number from a filename, using the same regexes as everything
    else that has to answer this question."""
    import re

    for pat in cfg.get("beam_index_regexes") or []:
        m = re.search(pat, name, re.IGNORECASE)
        if m:
            try:
                return int(m.group(1))
            except (TypeError, ValueError):
                continue
    return None


def read_beam_offsets(
    tiles: Sequence[Any], cfg: Dict[str, Any]
) -> Optional[Dict[str, Any]]:
    """{"beam_w", "beam_h", "offsets": {beam: [dx, dy]}} or None.

    Taken from the first mFoV whose coordinate file has enough beams to be
    worth it. Offsets are relative to the MEAN beam position, which is the
    mFoV centre for a symmetric arrangement and is well defined even if a
    beam is missing.
    """
    from qc_overview import parse_coords_file
    from PIL import Image

    for tile in tiles:
        folder = Path(getattr(tile, "tile_folder", "") or "")
        coords = parse_coords_file(str(folder / "image_coordinates.txt"))
        if len(coords) < 2:
            continue
        by_beam: Dict[int, tuple] = {}
        for name, (x, y) in coords.items():
            b = _beam_index(name, cfg)
            if b is not None:
                by_beam[b] = (float(x), float(y))
        if len(by_beam) < 2:
            continue
        cx = sum(p[0] for p in by_beam.values()) / len(by_beam)
        cy = sum(p[1] for p in by_beam.values()) / len(by_beam)

        beam_w = beam_h = None
        for g in cfg.get("image_suffix_globs") or ["*.bmp"]:
            for p in sorted(folder.glob(g)):
                try:
                    with Image.open(p) as im:
                        beam_w, beam_h = int(im.width), int(im.height)
                except Exception:
                    continue
                break
            if beam_w:
                break
        if not beam_w:
            beam_w, beam_h = 2000, 1748

        return {
            "beam_w": beam_w,
            "beam_h": beam_h,
            "offsets": {str(b): [round(p[0] - cx, 1), round(p[1] - cy, 1)]
                        for b, p in sorted(by_beam.items())},
        }
    return None
