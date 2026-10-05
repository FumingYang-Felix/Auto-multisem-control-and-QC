"""Choosing a downsample factor the browser can actually render.

`overview_ds` is a fixed divisor applied to every beam image, so the
canvas grows with the SECTION, not with the setting. A 9-mFoV section at
ds=16 lands around 3000 px; a 397-mFoV full section lands at
24355 x 27539 = 671 megapixels, a 251 MB PNG.

Chrome refuses that outright. Measured on this machine, loading the real
file into a page:

    671 Mpx (24355 x 27539)  ->  state "error", image in the "broken" state
    168 Mpx (12178 x 13770)  ->  loads, drawImage ok
     75 Mpx  (8119 x  9180)  ->  loads, drawImage ok
     42 Mpx  (6089 x  6885)  ->  loads, drawImage ok

which puts the cliff between 168 and 671 Mpx - consistent with Blink's
2^28-pixel (268 Mpx) decode limit. A broken image is what produced the
blurry, misaligned background an operator reported on S5R5: the hex overlay
is SVG and still drew correctly, so the two disagreed.

The fix is to raise ds for big sections so the canvas stays inside a budget.
It does NOT distort anything: the accumulator reports width/height/bboxes in
its own pixel space and the dashboard lays the image out in that same space,
so the hexes stay registered to the image whatever ds is.

The detail cost is real but small in context - at ds=52 one sFoV is ~38 px
across, and the whole section is still ~7500 px wide, far more than any
screen. An image the browser will not decode has no detail at all.
"""
from __future__ import annotations

import math
from pathlib import Path
from typing import Any, Dict, Optional, Sequence, Tuple

# Defaults if the config does not say. 64 Mpx sits well under the measured
# cliff and under Blink's limit, while staying above the 42 Mpx that was
# verified to render.
DEFAULT_MAX_MEGAPIXELS = 64.0
DEFAULT_MAX_DIM_PX = 12000


def full_pixel_extent(
    tiles: Sequence[Any],
) -> Optional[Tuple[float, float, int]]:
    """(width, height, n_mfovs_read) of the region in FULL-RES pixels.

    image_coordinates.txt holds GLOBAL image-pixel positions (values in the
    millions), not per-mFoV local ones, so the union of every beam's
    position is the region's footprint directly - no stage-to-pixel
    calibration needed here.
    """
    from qc_overview import parse_coords_file

    lo_x = lo_y = math.inf
    hi_x = hi_y = -math.inf
    n = 0
    for tile in tiles:
        folder = Path(getattr(tile, "tile_folder", "") or "")
        coords = parse_coords_file(str(folder / "image_coordinates.txt"))
        if not coords:
            continue
        n += 1
        for x, y in coords.values():
            lo_x = min(lo_x, x)
            hi_x = max(hi_x, x)
            lo_y = min(lo_y, y)
            hi_y = max(hi_y, y)
    if not n or lo_x is math.inf:
        return None
    return hi_x - lo_x, hi_y - lo_y, n


def _sfov_size(tiles: Sequence[Any], cfg: Dict[str, Any]) -> Tuple[int, int]:
    """One beam image's dimensions, from its header only (no decode)."""
    from PIL import Image

    globs = cfg.get("image_suffix_globs") or ["*.bmp"]
    for tile in tiles:
        folder = Path(getattr(tile, "tile_folder", "") or "")
        for g in globs:
            for p in sorted(folder.glob(g)):
                try:
                    with Image.open(p) as im:
                        return int(im.width), int(im.height)
                except Exception:
                    continue
    return 2000, 1748          # the known sFoV size, as a last resort


def plan_overview_ds(
    tiles: Sequence[Any], cfg: Dict[str, Any]
) -> Tuple[int, Dict[str, Any]]:
    """(ds, info) - the configured overview_ds, raised if the canvas would be
    too large for a browser to decode.

    Only ever raises it. A section small enough to be fine keeps exactly the
    factor the operator chose.
    """
    base = max(1, int(cfg.get("overview_ds", 16)))
    info: Dict[str, Any] = {"requested_ds": base, "ds": base}

    max_px = float(cfg.get("overview_max_megapixels",
                           DEFAULT_MAX_MEGAPIXELS)) * 1e6
    max_dim = int(cfg.get("overview_max_dim_px", DEFAULT_MAX_DIM_PX))
    if max_px <= 0 and max_dim <= 0:
        info["note"] = "no overview size cap configured"
        return base, info

    extent = full_pixel_extent(tiles)
    if extent is None:
        info["note"] = "no image_coordinates.txt readable; keeping requested ds"
        return base, info
    span_x, span_y, n = extent
    sw, sh = _sfov_size(tiles, cfg)
    full_w = span_x + sw
    full_h = span_y + sh
    info.update(mfovs=n, full_w=int(full_w), full_h=int(full_h))

    ds = base
    if max_px > 0:
        ds = max(ds, math.ceil(math.sqrt(full_w * full_h / max_px)))
    if max_dim > 0:
        ds = max(ds, math.ceil(max(full_w, full_h) / max_dim))

    info["ds"] = ds
    info["width"] = int(full_w // ds)
    info["height"] = int(full_h // ds)
    info["megapixels"] = round(info["width"] * info["height"] / 1e6, 1)
    if ds != base:
        info["note"] = (
            f"overview downsample raised {base} -> {ds}: at {base} this "
            f"{n}-mFoV section would come out "
            f"{int(full_w // base)}x{int(full_h // base)} px "
            f"({full_w * full_h / base / base / 1e6:.0f} Mpx), which a "
            f"browser will not decode. Now "
            f"{info['width']}x{info['height']} px "
            f"({info['megapixels']:.0f} Mpx)."
        )
    return ds, info
