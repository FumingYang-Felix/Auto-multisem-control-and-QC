r"""Building the section overview from the per-beam THUMBNAILS.

Acquisition writes two copies of every beam: the full-resolution .bmp and a
500x437 .png thumbnail, each with its own coordinate file. The overview was
reading the full-resolution set and throwing almost all of it away - at
overview_ds=52 a 2000x1748 beam becomes 38x33 px, so 99.96% of the pixels
read were discarded.

Measured on one real mFoV (91 beams, warm share):

    full-res .bmp, ds=52    318.2 MB   1.14 s   -> 38x33 px tiles
    thumbnail .png, ds=13    15.2 MB   0.13 s   -> 38x33 px tiles

Identical output, 8.8x faster, 20x less data off the share. Across a
400-mFoV section that is 7.6 minutes of reading against 0.9.

The thumbnails are exactly 1/4 scale, and thumbnail_coordinates.txt is the
same format as image_coordinates.txt with its numbers divided by 4 (checked
across three batches: ratio 4.0000, no mFoV missing thumbnails), so the
geometry is the same problem in smaller units.

This does not use qc_overview.RegionOverviewAccumulator: that reads
image_coordinates.txt by a hardcoded name and matches .bmp basenames, and it
belongs to the other clone. The placement rule here is deliberately the same
as its own - paste at the recorded coordinate, keep the darker pixel where
images overlap - so an overview built either way means the same thing.
"""
from __future__ import annotations

import math
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

THUMB_COORDS = "thumbnail_coordinates.txt"
FULL_COORDS = "image_coordinates.txt"


def _coords_for(folder: Path) -> Tuple[Dict[str, Tuple[float, float]], float]:
    """(basename -> (x, y), scale) in whatever units the file uses.

    `scale` is how many of THESE pixels make one full-resolution pixel, so
    a caller can still report sizes in the full-res frame.
    """
    from qc_overview import parse_coords_file

    thumb = parse_coords_file(str(folder / THUMB_COORDS))
    if thumb:
        full = parse_coords_file(str(folder / FULL_COORDS))
        scale = 4.0
        if full:
            # Measured, not assumed: match by stem, since the two files
            # name the same beam with different extensions.
            t = {Path(k).stem: v for k, v in thumb.items()}
            f = {Path(k).stem: v for k, v in full.items()}
            for k in set(t) & set(f):
                if abs(t[k][0]) > 1.0:
                    scale = f[k][0] / t[k][0]
                    break
        return thumb, scale
    return parse_coords_file(str(folder / FULL_COORDS)), 1.0


def plan(tiles: Sequence[Any], cfg: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Work out the canvas before reading a single image."""
    from PIL import Image

    lo_x = lo_y = math.inf
    hi_x = hi_y = -math.inf
    n = 0
    scale = 1.0
    tile_w = tile_h = None
    for tile in tiles:
        folder = Path(getattr(tile, "tile_folder", "") or "")
        coords, scale = _coords_for(folder)
        if not coords:
            continue
        n += 1
        for x, y in coords.values():
            lo_x = min(lo_x, x); hi_x = max(hi_x, x)
            lo_y = min(lo_y, y); hi_y = max(hi_y, y)
        if tile_w is None:
            p = folder / next(iter(coords))
            if p.is_file():
                try:
                    with Image.open(p) as im:
                        tile_w, tile_h = int(im.width), int(im.height)
                except Exception:
                    pass
    if not n or lo_x is math.inf:
        return None
    if tile_w is None:
        tile_w, tile_h = (500, 437) if scale > 1 else (2000, 1748)

    # Origin is the minimum TOP-LEFT, not the minimum centre. Using the
    # centre put every tile half a tile too far left and up - a constant
    # 62.5 x 54.6 px at ds=4 - which showed up as the whole overview being
    # offset against the bboxes the overlay and the FSP markers use.
    lo_x -= tile_w / 2.0
    lo_y -= tile_h / 2.0
    full_w = (hi_x - lo_x - tile_w / 2.0) + tile_w
    full_h = (hi_y - lo_y - tile_h / 2.0) + tile_h
    max_px = float(cfg.get("overview_max_megapixels", 64)) * 1e6
    max_dim = int(cfg.get("overview_max_dim_px", 12000))
    # overview_ds is expressed in FULL-RES pixels, so convert it into this
    # image's own units before using it as a floor.
    ds = max(1, int(round(float(cfg.get("overview_ds", 16)) / scale)))
    if max_px > 0:
        ds = max(ds, math.ceil(math.sqrt(full_w * full_h / max_px)))
    if max_dim > 0:
        ds = max(ds, math.ceil(max(full_w, full_h) / max_dim))
    # Same white margin qc_overview leaves, so an overview built either
    # way frames the section identically.
    pad = int(round(float(cfg.get("overview_padding", 80)) / scale / ds))
    return {
        "source": "thumbnails" if scale > 1 else "full resolution",
        "scale": scale, "ds": ds, "n_mfovs": n, "pad": pad,
        "tile_w": tile_w, "tile_h": tile_h,
        "min_x": lo_x, "min_y": lo_y,
        "width": int(full_w // ds) + 2 * pad,
        "height": int(full_h // ds) + 2 * pad,
    }


def build(
    tiles: Sequence[Any],
    out_dir: Path,
    cfg: Dict[str, Any],
    on_progress=None,
) -> Optional[Dict[str, Any]]:
    """Place every beam thumbnail and write the overview PNG.

    Returns the same shape qc_overview's accumulator returns - path, width,
    height, bboxes keyed by tile_idx - so the dashboard overlay and the FSP
    markers need no special case.
    """
    import numpy as np
    from PIL import Image

    pl = plan(tiles, cfg)
    if pl is None:
        return None
    ds = pl["ds"]
    W, H = pl["width"], pl["height"]
    if W <= 0 or H <= 0:
        return None
    progress = on_progress or (lambda d, t, l: None)

    canvas = np.full((H, W), 255, dtype=np.uint8)
    bboxes: Dict[int, List[int]] = {}
    tw = max(1, pl["tile_w"] // ds)
    th = max(1, pl["tile_h"] // ds)
    workers = max(1, int(cfg.get("max_workers", 16)))
    total = len(tiles)

    def _load(path: Path):
        try:
            with Image.open(path) as im:
                im = im.convert("L")
                if ds > 1:
                    im = im.resize((tw, th), Image.BILINEAR)
                return np.asarray(im, dtype=np.uint8)
        except Exception:
            return None

    for i, tile in enumerate(tiles, start=1):
        folder = Path(getattr(tile, "tile_folder", "") or "")
        coords, _ = _coords_for(folder)
        if not coords:
            continue
        progress(i, total, f"overview: reading mFoV {tile.tile_idx}")
        names = sorted(coords)
        # Decode in parallel - the share is the bottleneck - but composite
        # serially: overlapping writes into one array are not thread-safe.
        with ThreadPoolExecutor(max_workers=workers) as pool:
            arrays = list(pool.map(lambda n: _load(folder / n), names))
        for name, arr in zip(names, arrays):
            if arr is None:
                continue
            x, y = coords[name]
            pad = pl["pad"]
            x0 = int(round((x - pl["tile_w"] / 2.0 - pl["min_x"]) / ds)) + pad
            y0 = int(round((y - pl["tile_h"] / 2.0 - pl["min_y"]) / ds)) + pad
            if x0 >= W or y0 >= H:
                continue
            x0 = max(0, x0); y0 = max(0, y0)
            y1 = min(H, y0 + arr.shape[0])
            x1 = min(W, x0 + arr.shape[1])
            if y1 <= y0 or x1 <= x0:
                continue
            src = arr[: y1 - y0, : x1 - x0]
            # Darker pixel wins, exactly as qc_overview composites: the
            # canvas starts white so any real pixel beats blank, and a
            # genuine overlap keeps the darker sample rather than a blur
            # of two possibly misaligned ones.
            canvas[y0:y1, x0:x1] = np.minimum(canvas[y0:y1, x0:x1], src)
            b = bboxes.get(int(tile.tile_idx))
            if b is None:
                bboxes[int(tile.tile_idx)] = [x0, y0, x1, y1]
            else:
                b[0] = min(b[0], x0); b[1] = min(b[1], y0)
                b[2] = max(b[2], x1); b[3] = max(b[3], y1)

    progress(total, total, "overview: writing the image")
    out_dir.mkdir(parents=True, exist_ok=True)
    name = str(cfg.get("overview_region_name",
                       "standalone_qc_region_overview.png"))
    path = out_dir / name
    tmp = path.with_suffix(".tmp.png")
    # compress_level=1: these are 60 Mpx greyscale images written to a
    # share, where encode time dominates and the extra bytes cost less
    # than the CPU would.
    Image.fromarray(canvas).save(tmp, optimize=False, compress_level=1)
    os.replace(tmp, path)
    return {
        "path": str(path), "width": W, "height": H, "bboxes": bboxes,
        "source": pl["source"], "ds": ds, "scale": pl["scale"],
    }
