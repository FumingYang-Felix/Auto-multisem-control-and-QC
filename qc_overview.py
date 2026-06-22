"""
Downsampled OVERVIEW images for live QC — PIL + numpy only (no cv2, so it runs
wherever the QC worker runs).

Two products:
  - per-MFOV overview: one downsampled image of a single MFOV. If the tile folder
    has an image_coordinates.txt (global tile-center pixel coords, the Zeiss/ZEN
    export) we stitch the beams at their true positions; otherwise we fall back to
    an honest index-order grid montage of the beams (no fragile hex-geometry
    guess). Written next to the beams as overview_mfov.png.
  - per-region overview: the per-MFOV thumbnails placed at their MFOV layout
    positions (tile_idx -> x_um,y_um from the QC layout), min-blended into one
    downsampled image. Written to the LiveQC results dir.

Everything is best-effort: callers wrap in try/except and overview never breaks QC.
"""
from __future__ import annotations

import glob
import math
import os
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np
from PIL import Image


# ---------------------------------------------------------------------------
# low-level helpers
# ---------------------------------------------------------------------------
def load_gray_ds(path: str, ds: int) -> np.ndarray:
    """Load a grayscale image, downsampled by integer factor ds (>=1)."""
    with Image.open(path) as im0:   # context manager => file handle always closed
        im = im0.convert("L")
        if ds > 1:
            im = im.resize((max(1, im.width // ds), max(1, im.height // ds)), Image.BILINEAR)
        return np.asarray(im, dtype=np.uint8)


def parse_coords_file(path: str) -> Dict[str, Tuple[float, float]]:
    """Parse image_coordinates.txt rows: '<name>  <x>  <y>  <0>' -> {basename:(x,y)}."""
    out: Dict[str, Tuple[float, float]] = {}
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                parts = line.split()
                if len(parts) >= 3:
                    try:
                        out[os.path.basename(parts[0])] = (float(parts[1]), float(parts[2]))
                    except ValueError:
                        continue
    except Exception:
        return {}
    return out


def stitch_mosaic_from_centers_ds(
    image_paths: Sequence[str],
    coords_map: Dict[str, Tuple[float, float]],
    tile_w_full: int,
    tile_h_full: int,
    padding_full: int,
    ds: int,
    y_flip: bool = False,
    blend_mode: str = "min",
    min_tiles: int = 1,
) -> np.ndarray:
    """PIL port of auto_stage_calib.stitch_mosaic_from_centers_ds. coords are
    GLOBAL tile-CENTER pixel coords (full-res). Returns a uint8 mosaic."""
    tile_w_ds = max(1, int(round(tile_w_full / ds)))
    tile_h_ds = max(1, int(round(tile_h_full / ds)))
    padding_s = max(1, int(round(padding_full / ds)))

    items: List[Tuple[str, float, float]] = []
    for p in image_paths:
        base = os.path.basename(p)
        if base not in coords_map:
            continue
        x, y = coords_map[base]
        if y_flip:
            y = -y
        items.append((p, x - tile_w_full / 2.0, y - tile_h_full / 2.0))

    if len(items) < min_tiles:
        raise RuntimeError(f"too few tiles matched coords: {len(items)} < {min_tiles}")

    xs = np.array([it[1] for it in items], dtype=np.float64)
    ys = np.array([it[2] for it in items], dtype=np.float64)
    min_x, max_x = float(xs.min()), float(xs.max())
    min_y, max_y = float(ys.min()), float(ys.max())

    mosaic_w = max(int(round((max_x - min_x) / ds + tile_w_ds + 2 * padding_s)), tile_w_ds + 2 * padding_s)
    mosaic_h = max(int(round((max_y - min_y) / ds + tile_h_ds + 2 * padding_s)), tile_h_ds + 2 * padding_s)
    mosaic = np.full((mosaic_h, mosaic_w), 255, dtype=np.uint8)

    for (p, x_tl, y_tl) in items:
        x0 = int(round((x_tl - min_x) / ds + padding_s))
        y0 = int(round((y_tl - min_y) / ds + padding_s))
        tile = load_gray_ds(p, ds)
        th, tw = tile.shape
        if (tw, th) != (tile_w_ds, tile_h_ds):
            fitted = np.full((tile_h_ds, tile_w_ds), 255, dtype=np.uint8)
            crop = tile[: min(tile_h_ds, th), : min(tile_w_ds, tw)]
            fitted[: crop.shape[0], : crop.shape[1]] = crop
            tile = fitted
        if y0 < 0 or x0 < 0 or y0 >= mosaic_h or x0 >= mosaic_w:
            continue
        y1 = min(mosaic_h, y0 + tile_h_ds)
        x1 = min(mosaic_w, x0 + tile_w_ds)
        patch = mosaic[y0:y1, x0:x1]
        src = tile[: y1 - y0, : x1 - x0]
        mosaic[y0:y1, x0:x1] = np.minimum(patch, src) if blend_mode == "min" else src
    return mosaic


def grid_montage(image_paths: Sequence[str], ds: int, gap: int = 2) -> Optional[np.ndarray]:
    """Honest fallback when there are no coords: lay the beams out in a square-ish
    grid in filename (== beam-index) order. Useful for spotting dead/blurry beams."""
    paths = sorted(image_paths)
    tiles = []
    for p in paths:
        try:
            tiles.append(load_gray_ds(p, ds))
        except Exception:
            continue
    if not tiles:
        return None
    th = max(t.shape[0] for t in tiles)
    tw = max(t.shape[1] for t in tiles)
    n = len(tiles)
    cols = max(1, int(math.ceil(math.sqrt(n))))
    rows = int(math.ceil(n / cols))
    H = rows * th + (rows + 1) * gap
    W = cols * tw + (cols + 1) * gap
    canvas = np.full((H, W), 255, dtype=np.uint8)
    for i, t in enumerate(tiles):
        r, c = divmod(i, cols)
        y0 = gap + r * (th + gap)
        x0 = gap + c * (tw + gap)
        canvas[y0:y0 + t.shape[0], x0:x0 + t.shape[1]] = t
    return canvas


def _downsample_to_max(arr: np.ndarray, max_dim: int) -> np.ndarray:
    h, w = arr.shape[:2]
    if max(h, w) <= max_dim:
        return arr
    s = max_dim / float(max(h, w))
    im = Image.fromarray(arr).resize((max(1, int(w * s)), max(1, int(h * s))), Image.BILINEAR)
    return np.asarray(im, dtype=np.uint8)


# ---------------------------------------------------------------------------
# per-MFOV overview
# ---------------------------------------------------------------------------
def render_mfov_overview(tile_folder, prefix, cfg: Dict) -> Optional[str]:
    """Stitch/montage a single MFOV's beams into one downsampled PNG written into
    the tile folder. Returns the PNG path, or None if nothing could be rendered."""
    tile_folder = Path(tile_folder)
    ds = int(cfg.get("overview_ds", 16))
    globs = cfg.get("image_suffix_globs", ["*.bmp", "*.BMP"])
    paths: List[str] = []
    for g in globs:
        paths.extend(str(p) for p in tile_folder.glob(g))
    paths = sorted(set(paths))
    if not paths:
        return None

    mosaic = None
    layout = str(cfg.get("overview_beam_layout", "auto"))
    coords_path = tile_folder / "image_coordinates.txt"
    if layout in ("auto", "coords_file") and coords_path.is_file():
        coords = parse_coords_file(str(coords_path))
        if coords:
            try:
                with Image.open(paths[0]) as im0:
                    tw_full, th_full = im0.width, im0.height
                mosaic = stitch_mosaic_from_centers_ds(
                    paths, coords, tw_full, th_full,
                    padding_full=int(cfg.get("overview_padding", 80)), ds=ds,
                    blend_mode="min", min_tiles=1)
            except Exception:
                mosaic = None
    if mosaic is None:
        mosaic = grid_montage(paths, ds)
    if mosaic is None:
        return None

    out = tile_folder / str(cfg.get("overview_mfov_name", "overview_mfov.png"))
    Image.fromarray(mosaic).save(str(out))
    # absolute path: the viewer is a SEPARATE process and may have a different
    # cwd, so it must be able to re-open this regardless of relative roots.
    return os.path.abspath(str(out))


# ---------------------------------------------------------------------------
# per-region overview
# ---------------------------------------------------------------------------
class RegionOverviewAccumulator:
    """Caches per-MFOV thumbnails keyed by tile_idx and renders a region overview
    by placing each at its MFOV layout (x_um,y_um). Only re-renders cached
    thumbnails — never re-reads beam images."""

    def __init__(self, layout: Sequence[Dict], results_dir, cfg: Dict):
        self.cfg = cfg
        self.results_dir = Path(results_dir)
        self.thumb_px = int(cfg.get("overview_region_thumb_px", 64))
        self.canvas_px = int(cfg.get("overview_region_canvas_px", 2000))
        self.pos = {int(r["tile_idx"]): (float(r["x_um"]), float(r["y_um"])) for r in layout}
        self.thumbs: Dict[int, np.ndarray] = {}

    def add_png(self, tile_idx: int, png_path: str) -> None:
        try:
            with Image.open(png_path) as im:   # close handle (Windows file locks)
                arr = np.asarray(im.convert("L"), dtype=np.uint8)
            self.thumbs[int(tile_idx)] = _downsample_to_max(arr, self.thumb_px)
        except Exception:
            pass

    def render(self) -> Optional[str]:
        if not self.thumbs:
            return None
        try:
            pts = [self.pos[t] for t in self.thumbs if t in self.pos]
            if not pts:
                return None
            xs = [p[0] for p in pts]
            ys = [p[1] for p in pts]
            xmin, xmax = min(xs), max(xs)
            ymin, ymax = min(ys), max(ys)
            ext = max(xmax - xmin, ymax - ymin, 1e-6)
            scale = self.canvas_px / ext
            pad = self.thumb_px
            W = int((xmax - xmin) * scale) + 2 * pad
            H = int((ymax - ymin) * scale) + 2 * pad
            canvas = np.full((H, W), 255, dtype=np.uint8)
            for t, thumb in self.thumbs.items():
                if t not in self.pos:
                    continue
                x, y = self.pos[t]
                cx = int((x - xmin) * scale) + pad
                cy = int((ymax - y) * scale) + pad  # y up -> row down
                th, tw = thumb.shape
                yd = cy - th // 2
                xd = cx - tw // 2
                y0, x0 = max(0, yd), max(0, xd)
                y1, x1 = min(H, yd + th), min(W, xd + tw)
                if y1 <= y0 or x1 <= x0:
                    continue
                # offset the SOURCE crop by however much the dest was clamped, so
                # an edge thumbnail stays aligned instead of shifting.
                sy, sx = y0 - yd, x0 - xd
                src = thumb[sy:sy + (y1 - y0), sx:sx + (x1 - x0)]
                patch = canvas[y0:y1, x0:x1]
                canvas[y0:y1, x0:x1] = np.minimum(patch, src)
            out = self.results_dir / str(self.cfg.get("overview_region_name", "live_qc_region_overview.png"))
            self.results_dir.mkdir(parents=True, exist_ok=True)
            Image.fromarray(canvas).save(str(out))
            return str(out)
        except Exception:
            return None
