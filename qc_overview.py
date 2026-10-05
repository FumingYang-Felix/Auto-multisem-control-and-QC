"""
Downsampled OVERVIEW images for live QC — PIL + numpy only (no cv2, so it runs
wherever the QC worker runs).

Two products:
  - per-MFOV overview: one downsampled image of a single MFOV. If the tile folder
    has an image_coordinates.txt (global tile-center pixel coords, the Zeiss/ZEN
    export) we stitch the beams at their true positions; otherwise we fall back to
    an honest index-order grid montage of the beams (no fragile hex-geometry
    guess). Written next to the beams as overview_mfov.png.
  - per-region overview: every individual beam tile from every completed MFOV,
    placed at its own true global pixel coordinate (image_coordinates.txt --
    the same coordinate frame section_overview_builder.py stitches a whole
    section from), min-blended into one downsampled image. Written to the
    LiveQC results dir.

Everything is best-effort: callers wrap in try/except and overview never breaks QC.
"""
from __future__ import annotations

import math
import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

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
    """Parse image_coordinates.txt rows: '<path>\\t<x>\\t<y>\\t<0>' -> {basename:(x,y)}.

    Must split on tab specifically, not line.split() (any whitespace) - the
    path field can itself contain a space (e.g. an output folder named
    "New _test"), which otherwise splits the path into extra tokens and
    shifts the numeric fields out of position, silently failing every row.
    """
    out: Dict[str, Tuple[float, float]] = {}
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                parts = line.rstrip("\r\n").split("\t")
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
    """Builds a region overview from INDIVIDUAL beam tiles at their own true
    global pixel coordinates (image_coordinates.txt -- one shared coordinate
    frame across every MFOV in a section, same file section_overview_builder.py
    relies on), not from a per-MFOV pre-stitched thumbnail placed at an
    approximated MFOV-layout position. A per-MFOV thumbnail is a hexagon
    inscribed in a square bounding box with genuinely-unscanned (blank)
    corners; placing THAT as one blob at an approximate pitch-derived spot
    left either gaps or needed a fudge-factor to close them. Stitching every
    real tile at its own true coordinate sidesteps the whole problem the same
    way section_overview_builder.py's whole-section stitch does -- adjacent
    MFOVs' real tiles interlock wherever they actually overlap on the wafer,
    with no approximation involved.

    Beam tiles are cached downsampled (never re-read once cached) keyed by
    "<tile_idx>/<beam basename>"; render() is pure coordinate-blit over the
    cache, matching stitch_prepared_tiles() in section_overview_builder.py."""

    def __init__(self, layout: Sequence[Dict], results_dir, cfg: Dict):
        self.cfg = cfg
        self.results_dir = Path(results_dir)
        self.ds = max(1, int(cfg.get("overview_ds", 16)))
        self.padding_full = int(cfg.get("overview_padding", 80))
        self.image_suffix_globs = list(cfg.get("image_suffix_globs", ["*.bmp", "*.BMP"]))
        self.tiles: Dict[str, np.ndarray] = {}
        self.coords: Dict[str, Tuple[float, float]] = {}
        self.owner: Dict[str, int] = {}
        self._seen_mfovs: set = set()
        # All beam tiles share one fixed full-res size across the whole
        # acquisition; captured from the first tile read (cheap header-only
        # read, see add_mfov) since coords are tile-CENTER coords and need
        # half the tile's full-res size to get a top-left corner.
        self.tile_w_full: Optional[int] = None
        self.tile_h_full: Optional[int] = None
        # Persistent, GENEROUSLY PADDED backing buffer for incremental
        # render() (see its docstring). A first incremental version (2026-
        # 09-06) grew the buffer to the exact tight bound needed on every
        # resize - which sounds minimal but backfires badly for a scan order
        # whose bounding box keeps widening on nearly every periodic save
        # (observed in a real run: region_overview cost climbed linearly
        # from 0.18s to 4.9s across a single region's periodic saves,
        # instead of staying flat, because EVERY save re-seated every
        # previously-blitted tile). Padding the buffer well beyond the
        # tight bound on each resize (amortized-doubling, same idea as
        # Python list growth) bounds the number of resizes to
        # O(log(final_size)) instead of O(n_tiles/save_every), which is what
        # actually keeps total cost near-linear in tile count. The padding
        # is purely an internal buffer-management detail - render() always
        # crops back down to the tight bound before saving/returning, so the
        # saved PNG and reported bboxes are unaffected.
        self._buf: Optional[np.ndarray] = None
        self._buf_min_x: float = 0.0
        self._buf_max_x: float = 0.0
        self._buf_min_y: float = 0.0
        self._buf_max_y: float = 0.0
        self._buf_w_ds: int = 0
        self._buf_h_ds: int = 0
        self._blitted_keys: set = set()
        self._bboxes: Dict[int, List[int]] = {}
        # Every planned mfov's stage position is already known before
        # acquisition even starts (2026-09-07, per operator: "since we know
        # the number of mfovs per region... there is no reason to generate
        # the canvas on the fly"). This is used to precompute the region's
        # FINAL footprint from just the first couple of real tiles, instead
        # of discovering it (and resizing) tile by tile - see
        # _try_precompute_from_layout(). The stage-to-image-pixel transform
        # is CALIBRATED from real data each run (not assumed from a
        # hardware constant), since only the sign/no-rotation relationship
        # is documented (corr(stage_x,img_x)=-1.0, corr(stage_y,img_y)=+1.0
        # - see render()'s docstring), not an exact scale - so this stays
        # correct even if that assumption is ever imperfect: the amortized-
        # growth fallback in render() still covers any real position that
        # ends up outside the precomputed buffer.
        self._layout_stage: Dict[int, Tuple[float, float]] = {
            int(r["tile_idx"]): (float(r["x_um"]), float(r["y_um"])) for r in layout
        }
        self._calib_samples: List[Tuple[float, float, float, float]] = []  # (stage_x, stage_y, img_x, img_y)
        self._calibrated_tiles: set = set()
        self._precomputed = False

    def add_mfov(self, tile_idx: int, tile_folder) -> None:
        """Read one completed MFOV's individual beam images + its
        image_coordinates.txt directly from disk (once per MFOV) and cache
        each beam downsampled at its own true coordinate."""
        tile_idx = int(tile_idx)
        if tile_idx in self._seen_mfovs:
            return
        try:
            tile_folder = Path(tile_folder)
            coords = parse_coords_file(str(tile_folder / "image_coordinates.txt"))
            if not coords:
                return
            paths: List[str] = []
            for g in self.image_suffix_globs:
                paths.extend(str(p) for p in tile_folder.glob(g))
            paths = sorted(set(paths))
            added = False
            for p in paths:
                base = os.path.basename(p)
                if base not in coords:
                    continue
                if self.tile_w_full is None:
                    with Image.open(p) as im0:
                        self.tile_w_full, self.tile_h_full = im0.width, im0.height
                try:
                    tile = load_gray_ds(p, self.ds)
                except Exception:
                    continue
                # Match the Zeiss/production display convention (confirmed
                # against the MATLAB workflow manager, which inverts its final
                # overview the same way): raw beam images are dark-background/
                # bright-content, inverted here per-tile so unscanned canvas
                # (255, untouched by this) stays the correct white background
                # and real content reads light-background/dark-content like
                # the reference section overview.
                tile = (255 - tile).astype(np.uint8)
                key = f"{tile_idx}/{base}"
                self.tiles[key] = tile
                self.coords[key] = coords[base]
                self.owner[key] = tile_idx
                added = True
                # One (stage, image-pixel) correspondence per mfov is plenty
                # to calibrate the region-wide transform (see
                # _try_precompute_from_layout) - a few pixels of per-beam
                # offset within one mfov is negligible next to a whole
                # region's span.
                if tile_idx not in self._calibrated_tiles:
                    stage_pos = self._layout_stage.get(tile_idx)
                    if stage_pos is not None:
                        self._calib_samples.append((stage_pos[0], stage_pos[1], coords[base][0], coords[base][1]))
                        self._calibrated_tiles.add(tile_idx)
            if added:
                self._seen_mfovs.add(tile_idx)
        except Exception:
            pass

    def _blit_one(
        self, key: str, canvas: np.ndarray, min_x_ref: float, min_y_ref: float,
        w_ds: int, h_ds: int, tile_w_ds: int, tile_h_ds: int, padding_ds: int,
        ds: int, bboxes: Dict[int, List[int]],
    ) -> None:
        """Composite one cached (already-downsampled) tile onto `canvas` at
        the position its true coordinate maps to relative to
        (min_x_ref, min_y_ref) -- always computed fresh from self.coords, so
        calling this again for the same key against a different origin can
        never accumulate rounding drift versus a one-shot from-scratch
        render (see render())."""
        x, y = self.coords[key]
        x_tl = x - self.tile_w_full / 2.0
        y_tl = y - self.tile_h_full / 2.0
        tile = self.tiles[key]
        th, tw = tile.shape[:2]
        if (tw, th) != (tile_w_ds, tile_h_ds):
            fitted = np.full((tile_h_ds, tile_w_ds), 255, dtype=np.uint8)
            crop = tile[: min(tile_h_ds, th), : min(tile_w_ds, tw)]
            fitted[: crop.shape[0], : crop.shape[1]] = crop
            tile = fitted
        x0 = int(round((x_tl - min_x_ref) / ds + padding_ds))
        y0 = int(round((y_tl - min_y_ref) / ds + padding_ds))
        if x0 < 0 or y0 < 0 or x0 >= w_ds or y0 >= h_ds:
            return
        y1 = min(h_ds, y0 + tile_h_ds)
        x1 = min(w_ds, x0 + tile_w_ds)
        src = tile[: y1 - y0, : x1 - x0]
        patch = canvas[y0:y1, x0:x1]
        canvas[y0:y1, x0:x1] = np.minimum(patch, src)

        mfov_idx = self.owner.get(key)
        if mfov_idx is not None:
            box = bboxes.get(mfov_idx)
            if box is None:
                bboxes[mfov_idx] = [x0, y0, x1, y1]
            else:
                box[0] = min(box[0], x0)
                box[1] = min(box[1], y0)
                box[2] = max(box[2], x1)
                box[3] = max(box[3], y1)

    def _try_precompute_from_layout(self, ds: int, tile_w_ds: int, tile_h_ds: int, padding_ds: int) -> None:
        """Once >=2 real (stage, image-pixel) samples span both axes,
        solve for the region's stage-to-image-pixel transform (a pure
        per-axis scale, no rotation/skew - matches the documented
        corr(stage_x,img_x)=-1.0, corr(stage_y,img_y)=+1.0 relationship) and
        predict every OTHER mfov's image-pixel position directly from
        `layout` -- known in full before acquisition even starts -- so the
        buffer is allocated ONCE at (close to) its final size instead of
        being discovered, and resized, tile by tile. A generous safety
        margin absorbs a real position landing slightly outside the
        prediction; render()'s existing amortized-growth fallback still
        covers a position further outside than that (e.g. if the no-
        rotation assumption is ever imperfect for some setup), so a bad
        calibration degrades gracefully instead of corrupting anything."""
        if self._precomputed or len(self._calib_samples) < 2:
            return
        sx = np.array([s[0] for s in self._calib_samples], dtype=np.float64)
        sy = np.array([s[1] for s in self._calib_samples], dtype=np.float64)
        ix = np.array([s[2] for s in self._calib_samples], dtype=np.float64)
        iy = np.array([s[3] for s in self._calib_samples], dtype=np.float64)
        # Need real spread on BOTH axes to solve for both scales - a couple
        # of mfovs that happen to share almost the same x (or y) leave that
        # axis's scale unresolved for now; wait for more samples rather than
        # fit garbage.
        if (sx.max() - sx.min()) < 1.0 or (sy.max() - sy.min()) < 1.0:
            return
        try:
            scale_x = float(np.polyfit(sx, ix, 1)[0])
            scale_y = float(np.polyfit(sy, iy, 1)[0])
        except Exception:
            return

        ref_sx, ref_sy, ref_ix, ref_iy = self._calib_samples[0]
        stage_pts = list(self._layout_stage.values())
        pred_x = [ref_ix + (x - ref_sx) * scale_x for x, _y in stage_pts]
        pred_y = [ref_iy + (y - ref_sy) * scale_y for _x, y in stage_pts]
        min_x = min(pred_x) - self.tile_w_full / 2.0
        max_x = max(pred_x) + self.tile_w_full / 2.0
        min_y = min(pred_y) - self.tile_h_full / 2.0
        max_y = max(pred_y) + self.tile_h_full / 2.0
        # Generous safety margin - covers imperfect calibration (a few
        # samples, real-world noise) without ever needing render()'s
        # fallback resize path in the common case.
        margin_x = max((max_x - min_x) * 0.1, self.tile_w_full * 2.0)
        margin_y = max((max_y - min_y) * 0.1, self.tile_h_full * 2.0)
        min_x -= margin_x
        max_x += margin_x
        min_y -= margin_y
        max_y += margin_y

        buf_w = max(int(round((max_x - min_x) / ds + tile_w_ds + 2 * padding_ds)), tile_w_ds + 2 * padding_ds)
        buf_h = max(int(round((max_y - min_y) / ds + tile_h_ds + 2 * padding_ds)), tile_h_ds + 2 * padding_ds)
        new_buf = np.full((buf_h, buf_w), 255, dtype=np.uint8)
        new_bboxes: Dict[int, List[int]] = {}
        for key in self._blitted_keys:
            self._blit_one(key, new_buf, min_x, min_y, buf_w, buf_h,
                            tile_w_ds, tile_h_ds, padding_ds, ds, new_bboxes)
        self._buf = new_buf
        self._buf_min_x, self._buf_max_x = min_x, max_x
        self._buf_min_y, self._buf_max_y = min_y, max_y
        self._buf_w_ds, self._buf_h_ds = buf_w, buf_h
        self._bboxes = new_bboxes
        self._precomputed = True

    def render(self) -> Optional[Dict[str, Any]]:
        """Composite beam tiles onto a persistent, padded backing buffer
        (grown with amortized doubling as needed) and save the region
        overview PNG, cropped to the tight bound of the data seen so far.
        Returns {"path", "width", "height", "bboxes": {tile_idx:
        [x0,y0,x1,y1]}} (pixel-space bounding box of every MFOV that has
        contributed at least one tile so far, relative to the SAVED/cropped
        image) so a caller can overlay per-MFOV click targets / quality
        tints on the saved image at the exact same placement used here -- or
        None if nothing to render.

        Incremental, not a from-scratch recomposite: a tile already blitted
        into the current buffer is never touched again UNLESS the buffer
        itself needs to grow. Growing pads generously beyond the tight bound
        actually needed (at least doubling the data span in each direction)
        instead of growing to the exact tight bound - a real run showed that
        growing exactly-to-fit backfires when the bounding box widens on
        nearly every periodic save (a plausible scan order for a raster/
        serpentine region): region_overview cost climbed linearly across a
        single region's saves (0.18s -> 4.9s) because EVERY save re-seated
        every previously-blitted tile. Generous padding bounds the number of
        real resizes to O(log(final span)) regardless of scan order, the
        same amortized-doubling argument as Python list growth, so total
        cost across a whole region stays close to O(n_tiles) instead of
        O(n_tiles^2 / save_every) (the original always-from-scratch
        version's scaling). Each resize still re-seats every already-
        blitted tile fresh from its true coordinate (never shifts by a
        rounded delta, which would accumulate drift) - so a resize-only run
        is bit-identical to a one-shot from-scratch render, and the padding
        is purely an internal buffer-management detail invisible in the
        saved PNG/bboxes.

        When _try_precompute_from_layout has already fixed the buffer's
        origin (the common case - see its docstring), a tile's rounded
        pixel position can differ by <=1px from what a plain one-shot
        render would have produced: rounding a real coordinate against an
        origin picked from a PREDICTION isn't guaranteed to land on the
        same side of a 0.5 boundary as rounding it against the origin a
        one-shot render would derive from the FINAL exact data (confirmed
        bounded at exactly 1px, not growing, across a 227-tile test). In a
        16x-downsampled, explicitly best-effort QC overview, that's at most
        ~16 real pixels of seam wobble at one tile edge - cosmetically
        immaterial for what this image is for (spotting gross defects,
        selecting retake regions), not chased further."""
        if not self.tiles or self.tile_w_full is None:
            return None
        try:
            ds = self.ds
            tile_w_ds = max(1, int(round(self.tile_w_full / ds)))
            tile_h_ds = max(1, int(round(self.tile_h_full / ds)))
            padding_ds = max(1, int(round(self.padding_full / ds)))

            keys = [k for k in self.coords if k in self.tiles]
            if not keys:
                return None
            xs = np.array([self.coords[k][0] - self.tile_w_full / 2.0 for k in keys], dtype=np.float64)
            ys = np.array([self.coords[k][1] - self.tile_h_full / 2.0 for k in keys], dtype=np.float64)
            min_x, max_x = float(xs.min()), float(xs.max())
            min_y, max_y = float(ys.min()), float(ys.max())

            if not self._precomputed:
                self._try_precompute_from_layout(ds, tile_w_ds, tile_h_ds, padding_ds)

            needs_grow = (
                self._buf is None
                or min_x < self._buf_min_x or max_x > self._buf_max_x
                or min_y < self._buf_min_y or max_y > self._buf_max_y
            )
            if needs_grow:
                span_x = max(max_x - min_x, float(self.tile_w_full))
                span_y = max(max_y - min_y, float(self.tile_h_full))
                margin_x = max(span_x * 0.5, self.tile_w_full * 4.0)
                margin_y = max(span_y * 0.5, self.tile_h_full * 4.0)
                new_min_x, new_max_x = min_x - margin_x, max_x + margin_x
                new_min_y, new_max_y = min_y - margin_y, max_y + margin_y
                buf_w = max(int(round((new_max_x - new_min_x) / ds + tile_w_ds + 2 * padding_ds)), tile_w_ds + 2 * padding_ds)
                buf_h = max(int(round((new_max_y - new_min_y) / ds + tile_h_ds + 2 * padding_ds)), tile_h_ds + 2 * padding_ds)
                new_buf = np.full((buf_h, buf_w), 255, dtype=np.uint8)
                new_bboxes: Dict[int, List[int]] = {}
                for key in self._blitted_keys:
                    self._blit_one(key, new_buf, new_min_x, new_min_y, buf_w, buf_h,
                                    tile_w_ds, tile_h_ds, padding_ds, ds, new_bboxes)
                self._buf = new_buf
                self._buf_min_x, self._buf_max_x = new_min_x, new_max_x
                self._buf_min_y, self._buf_max_y = new_min_y, new_max_y
                self._buf_w_ds, self._buf_h_ds = buf_w, buf_h
                self._bboxes = new_bboxes

            for key in keys:
                if key in self._blitted_keys:
                    continue
                self._blit_one(key, self._buf, self._buf_min_x, self._buf_min_y,
                                self._buf_w_ds, self._buf_h_ds,
                                tile_w_ds, tile_h_ds, padding_ds, ds, self._bboxes)
                self._blitted_keys.add(key)

            # Crop the padded buffer down to the tight bound of the data
            # seen so far before saving/returning - the padding above is
            # purely an internal amortization trick, never part of the
            # reported image or bboxes. Derived from the MIN/MAX of the
            # already-rounded integer bboxes _blit_one just computed, not by
            # independently re-rounding (min_x - buf_min_x)/ds: two
            # separately-rounded quantities that are mathematically supposed
            # to cancel can still land on opposite sides of a 0.5 boundary
            # depending on the arbitrary buffer origin chosen (confirmed:
            # this shifted a handful of tile bboxes by exactly 1px between
            # two runs whose layout precompute happened to calibrate to
            # slightly different origins) - taking min/max over values that
            # went through the exact same rounding rule against the exact
            # same origin has no such gap.
            all_x0 = min(box[0] for box in self._bboxes.values())
            all_y0 = min(box[1] for box in self._bboxes.values())
            all_x1 = max(box[2] for box in self._bboxes.values())
            all_y1 = max(box[3] for box in self._bboxes.values())
            crop_x0 = max(0, all_x0 - padding_ds)
            crop_y0 = max(0, all_y0 - padding_ds)
            crop_x1 = min(self._buf_w_ds, all_x1 + padding_ds)
            crop_y1 = min(self._buf_h_ds, all_y1 + padding_ds)
            cropped = np.ascontiguousarray(self._buf[crop_y0:crop_y1, crop_x0:crop_x1])
            out_bboxes = {
                tile_idx: [box[0] - crop_x0, box[1] - crop_y0, box[2] - crop_x0, box[3] - crop_y0]
                for tile_idx, box in self._bboxes.items()
            }

            out = self.results_dir / str(self.cfg.get("overview_region_name", "live_qc_region_overview.png"))
            self.results_dir.mkdir(parents=True, exist_ok=True)
            Image.fromarray(cropped).save(str(out))
            return {
                "path": str(out),
                "width": int(cropped.shape[1]),
                "height": int(cropped.shape[0]),
                "bboxes": out_bboxes,
            }
        except Exception:
            return None
