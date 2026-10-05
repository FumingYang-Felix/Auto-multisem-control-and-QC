#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Whole-section overview builder: downsamples + stitches every mFOV's beam
tiles in a completed section's output folder into one low-resolution overview
image at a target pixel size (default 0.5 um/px).

Ported from a study of the MATLAB workflow manager at D:\\mSEM_workflow_manager
(CheckOverlap/Check_Overlap_For_One_Section.m and friends), adapted to this
project's real acquisition layout (focus_map.py / main_live_qc.py write
per-mFOV subfolders "mfov<idx>/" each holding beam bmps + a Zeiss-written
image_coordinates.txt of GLOBAL tile-center pixel coordinates -- the SAME
format auto_stage_calib.py and qc_overview.py already parse, and confirmed to
be one shared global coordinate frame across every mFOV in a section, not
reset per tile).

If the Zeiss API's own native thumbnails are available (DP_THUMBNAIL_LOCATION/
DP_THUMBNAIL_SCALE, wired into this project's real acquisitions in
focus_map.py's configure_output_for_tile()/apply_acq_scan_settings()), this
reads those directly instead of downsampling from the full-res bmps -- the
scope already downsamples via genuine area-averaging (per the API doc: "The
thumbnails obtained by averaging the full resolution scans"), in parallel with
the full-res acquisition at no extra scan time, so there's no reason to redo
that work in Python when a thumbnail is sitting right there. A thumbnail's
filename shares the exact same stem as its full-res counterpart (only the
extension differs -- see configure_output_for_tile()), so the two are matched
directly by stem, per beam; any beam missing its thumbnail (partial runs,
thumbnails disabled after the fact, etc.) falls back to downsampling that
beam's full-res image individually, so the tool works whether thumbnails are
present, absent, or only partially present.

Two things the MATLAB pipeline does that a naive "just cv2.resize everything"
port would miss, both kept here:

  1. Per-tile background subtraction BEFORE downsampling (a grayscale erosion
     estimates the slowly-varying local background/shading level and
     subtracts it). Doing this before the blur of downsampling is what lets
     fine local contrast survive -- do it after resizing and the erosion has
     nothing left to see. See remove_background_erode().

  2. Per-mFOV gain normalization AFTER downsampling: a robust (background-
     masked, percentile-based) brightness estimate computed PER MFOV, rescaled
     so every mFOV's percentile lands on a common target level. The MATLAB
     pipeline actually running in production only does a single GLOBAL
     percentile stretch across the whole section (no per-mFOV correction);
     a per-mFOV version was written (utils_normalize_thumbnail_stack_for_overview.m)
     but never wired into any caller. Per-mFOV is what's implemented here,
     since MultiSEM's 91 separate beam/detector chains are a real source of
     beam-to-beam brightness variation a single whole-section stretch can't
     correct -- exactly the "retain contrast for each mFOV" ask this was
     built for. See normalize_mfov_gain().

A final, mild whole-section percentile clip (not a full MATLAB-style rescale,
since per-mFOV normalization has already equalized levels) guards against any
single outlier tile before the mosaic is cast to uint8.

Usage:
    python section_overview_builder.py --section_dir <path to Region_.../S##R#> --out overview.png
"""
from __future__ import annotations

import argparse
import glob
import os
import sys
import time
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

import numpy as np
import cv2

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from qc_overview import parse_coords_file  # noqa: E402  (reuse the existing coords parser)


# =============================================================================
# Config
# =============================================================================

@dataclass
class OverviewConfig:
    target_um_per_px: float = 0.5
    raw_pixel_nm: float = 8.0          # must match the section's actual acquisition pixel size
    tile_w_full: int = 2000
    tile_h_full: int = 1748
    padding_full: int = 80

    # Background subtraction (grayscale erosion), applied per tile at FULL
    # resolution before downsampling -- the window must span a genuine
    # physical shading scale, not just a few raw pixels, or it does nothing.
    background_erode_window_full: int = 25

    # Per-mFOV gain normalization: pixels below this raw intensity are treated
    # as background/no-signal and excluded from the percentile estimate (mirrors
    # the MATLAB "<5" mask), same as background_pixel_floor below.
    background_pixel_floor: float = 5.0
    gain_percentile: float = 75.0
    gain_target_level: float = 100.0

    # Final whole-section safety clip (guards against one outlier tile after
    # per-mFOV normalization has already equalized levels -- not a full
    # MATLAB-style global stretch, just a percentile-based ceiling/floor).
    final_clip_low_percentile: float = 0.5
    final_clip_high_percentile: float = 99.5

    blend_mode: str = "min"   # how overlapping tiles combine ("min" or "overwrite")
    image_suffix_globs: Tuple[str, ...] = ("*.bmp", "*.BMP")
    verbose: bool = True

    # Prefer the scope's own native thumbnails (see module docstring) over
    # downsampling full-res images ourselves, when available.
    use_thumbnails_if_available: bool = True
    # Extensions to look for (matched against the full-res beam's own stem,
    # e.g. "mfov7_ts_001.bmp" -> try "mfov7_ts_001.png", ".tif", ... ), in the
    # same folder. Covers every STR_THUMBNAIL_FILETYPE this project's
    # focus_map.py config might reasonably be set to (default "png").
    thumbnail_extensions: Tuple[str, ...] = ("png", "tif", "tiff", "jpg", "jpeg", "bmp")
    # Thumbnails are already downsampled by the scope via area-averaging, so
    # the erosion-based background subtraction (designed to run BEFORE a
    # downsampling blur, on full-res data -- see remove_background_erode) has
    # little left to do at thumbnail resolution; off by default for the
    # thumbnail path. Full-res fallback tiles always get it regardless.
    thumbnail_background_subtract: bool = False

    # Resolved once per run in build_section_overview() (0 = "not yet
    # resolved"): either the actual downsample factor discovered from a
    # found thumbnail, or -- if none are available -- computed from
    # target_um_per_px/raw_pixel_nm exactly as before. Every tile in a run
    # (thumbnail-sourced or full-res-fallback) is stitched using this SAME
    # value, or their placements would be inconsistent with each other.
    ds: int = 0

    def default_ds_from_target(self) -> int:
        """The target_um_per_px/raw_pixel_nm-implied downsample factor --
        used when no thumbnail is available to discover the actual one from."""
        return max(1, int(round(self.target_um_per_px * 1000.0 / self.raw_pixel_nm)))


def _log(cfg: OverviewConfig, msg: str) -> None:
    if cfg.verbose:
        print(msg, flush=True)


# =============================================================================
# Per-tile processing
# =============================================================================

def remove_background_erode(img: np.ndarray, window: int) -> np.ndarray:
    """Grayscale-erosion background estimate, subtracted from img. Port of the
    MATLAB workflow manager's utils_remove_background_erode.m. Operates in
    float32 (result can go slightly negative at edges; left unclipped here --
    callers clip/rescale downstream) so it must run BEFORE any resize."""
    window = max(1, int(window))
    kernel = np.ones((window, window), np.uint8)
    background = cv2.erode(img, kernel)
    return img.astype(np.float32) - background.astype(np.float32)


def _downsample_area(img_float: np.ndarray, ds: int, tile_w_full: int, tile_h_full: int) -> np.ndarray:
    ds = max(1, int(ds))
    w_ds = max(1, int(round(tile_w_full / ds)))
    h_ds = max(1, int(round(tile_h_full / ds)))
    if ds == 1:
        return img_float
    return cv2.resize(img_float, (w_ds, h_ds), interpolation=cv2.INTER_AREA)


def discover_mfov_dirs(section_dir: str) -> List[str]:
    """Every immediate subfolder of section_dir that looks like an mFOV tile
    (name starts with "mfov", contains image_coordinates.txt), sorted for
    reproducible ordering."""
    out = []
    for entry in sorted(glob.glob(os.path.join(section_dir, "mfov*"))):
        if os.path.isdir(entry) and os.path.isfile(os.path.join(entry, "image_coordinates.txt")):
            out.append(entry)
    return out


def find_thumbnail_for_beam(beam_path: str, cfg: OverviewConfig) -> Optional[str]:
    """A thumbnail shares the full-res beam's exact stem (see
    configure_output_for_tile() in focus_map.py) -- only the extension
    differs. Returns the thumbnail path if one exists alongside beam_path.

    thumbnail_extensions can legitimately include the same extension the
    full-res images use (e.g. STR_THUMBNAIL_FILETYPE set to "bmp" to match),
    so a same-extension candidate must be checked against beam_path itself --
    otherwise a beam would be found as its own "thumbnail" and treated as
    already-downsampled when it's actually still full resolution."""
    beam_abspath = os.path.abspath(beam_path)
    stem, _ = os.path.splitext(beam_path)
    for ext in cfg.thumbnail_extensions:
        candidate = f"{stem}.{ext}"
        if os.path.abspath(candidate) == beam_abspath:
            continue
        if os.path.isfile(candidate):
            return candidate
    return None


def discover_actual_thumbnail_ds(mfov_dirs: List[str], cfg: OverviewConfig) -> Optional[int]:
    """Scan mFOVs for the first available (beam, thumbnail) pair and compute
    the scope's ACTUAL thumbnail downsample factor from their relative pixel
    dimensions -- not assumed from config, since DP_THUMBNAIL_SCALE rounds
    down to the nearest power of 2 if a non-power-of-2 was ever requested, so
    what's actually on disk is the only reliable source of truth. Returns
    None if no thumbnail is found anywhere (caller falls back to
    cfg.default_ds_from_target())."""
    for mfov_dir in mfov_dirs:
        paths: List[str] = []
        for g in cfg.image_suffix_globs:
            paths.extend(glob.glob(os.path.join(mfov_dir, g)))
        for p in sorted(set(paths)):
            thumb_path = find_thumbnail_for_beam(p, cfg)
            if thumb_path is None:
                continue
            thumb = cv2.imread(thumb_path, cv2.IMREAD_GRAYSCALE)
            if thumb is None or thumb.shape[1] == 0:
                continue
            return max(1, int(round(cfg.tile_w_full / thumb.shape[1])))
    return None


def load_and_prepare_mfov_tiles(
    mfov_dir: str, cfg: OverviewConfig
) -> Tuple[Dict[str, np.ndarray], Dict[str, Tuple[float, float]], int, int]:
    """For one mFOV folder: for each beam matched in image_coordinates.txt,
    use its scope-generated thumbnail directly if one exists (cfg.ds must
    already be resolved to the thumbnail's actual scale by this point --
    see build_section_overview), else fall back to loading + background-
    subtracting + downsampling the full-res image. Returns
    {basename: float32 array}, {basename: (global_x, global_y)} (the latter
    filtered to only the beams actually loaded), n_from_thumbnail, n_from_fullres."""
    coords_path = os.path.join(mfov_dir, "image_coordinates.txt")
    coords = parse_coords_file(coords_path)
    if not coords:
        return {}, {}, 0, 0

    paths: List[str] = []
    for g in cfg.image_suffix_globs:
        paths.extend(glob.glob(os.path.join(mfov_dir, g)))
    # dedupe: on a case-insensitive filesystem (Windows), "*.bmp" and "*.BMP"
    # both match the same files, so without this every tile gets processed twice.
    paths = sorted(set(paths))

    tiles: Dict[str, np.ndarray] = {}
    used_coords: Dict[str, Tuple[float, float]] = {}
    n_thumb, n_fullres = 0, 0
    for p in paths:
        base = os.path.basename(p)
        if base not in coords:
            continue

        thumb_path = find_thumbnail_for_beam(p, cfg) if cfg.use_thumbnails_if_available else None
        if thumb_path is not None:
            img = cv2.imread(thumb_path, cv2.IMREAD_GRAYSCALE)
            if img is None:
                thumb_path = None  # corrupt/unreadable thumbnail -- fall through to full-res
            else:
                arr = img.astype(np.float32)
                if cfg.thumbnail_background_subtract:
                    arr = remove_background_erode(img, cfg.background_erode_window_full)
                tiles[base] = arr
                used_coords[base] = coords[base]
                n_thumb += 1
                continue

        img = cv2.imread(p, cv2.IMREAD_GRAYSCALE)
        if img is None:
            continue
        bg_removed = remove_background_erode(img, cfg.background_erode_window_full)
        tiles[base] = _downsample_area(bg_removed, cfg.ds, cfg.tile_w_full, cfg.tile_h_full)
        used_coords[base] = coords[base]
        n_fullres += 1

    return tiles, used_coords, n_thumb, n_fullres


def normalize_mfov_gain(tiles: Dict[str, np.ndarray], cfg: OverviewConfig) -> Dict[str, np.ndarray]:
    """Per-mFOV robust gain correction: one common scalar gain for every tile
    in this mFOV (not per-tile individually -- the point is to equalize this
    mFOV's overall brightness against the target level, not flatten its own
    internal contrast), computed from a background-masked percentile across
    ALL of the mFOV's (already background-subtracted, downsampled) pixels.
    Port of the MATLAB workflow manager's (unused-in-production)
    utils_normalize_thumbnail_stack_for_overview.m."""
    if not tiles:
        return tiles
    stacked = np.concatenate([t.reshape(-1) for t in tiles.values()])
    valid = stacked[stacked > cfg.background_pixel_floor]
    if valid.size == 0:
        return tiles
    level = float(np.percentile(valid, cfg.gain_percentile))
    if level <= 1e-6:
        return tiles
    gain = cfg.gain_target_level / level
    return {k: v * gain for k, v in tiles.items()}


# =============================================================================
# Combined stitch (all mFOVs' prepared tiles, one shared global coordinate frame)
# =============================================================================

def stitch_prepared_tiles(
    tiles: Dict[str, np.ndarray],
    coords_map: Dict[str, Tuple[float, float]],
    cfg: OverviewConfig,
) -> np.ndarray:
    """Coordinate-blit stitch of ALREADY-loaded/processed float tiles (no
    further disk I/O) -- same placement logic as
    auto_stage_calib.stitch_mosaic_from_centers_ds, generalized to however
    many tiles are handed in (a whole section's worth, not just one mFOV)."""
    ds = cfg.ds
    tile_w_ds = max(1, int(round(cfg.tile_w_full / ds)))
    tile_h_ds = max(1, int(round(cfg.tile_h_full / ds)))
    padding_ds = max(1, int(round(cfg.padding_full / ds)))

    items: List[Tuple[str, float, float]] = []
    for base, (x, y) in coords_map.items():
        if base not in tiles:
            continue
        items.append((base, x - cfg.tile_w_full / 2.0, y - cfg.tile_h_full / 2.0))
    if not items:
        raise RuntimeError("no tiles to stitch")

    xs = np.array([it[1] for it in items], dtype=np.float64)
    ys = np.array([it[2] for it in items], dtype=np.float64)
    min_x, max_x = float(xs.min()), float(xs.max())
    min_y, max_y = float(ys.min()), float(ys.max())

    mosaic_w = max(int(round((max_x - min_x) / ds + tile_w_ds + 2 * padding_ds)), tile_w_ds + 2 * padding_ds)
    mosaic_h = max(int(round((max_y - min_y) / ds + tile_h_ds + 2 * padding_ds)), tile_h_ds + 2 * padding_ds)
    mosaic = np.zeros((mosaic_h, mosaic_w), dtype=np.float32)
    written = np.zeros((mosaic_h, mosaic_w), dtype=bool)

    for base, x_tl, y_tl in items:
        tile = tiles[base]
        th, tw = tile.shape[:2]
        if (tw, th) != (tile_w_ds, tile_h_ds):
            fitted = np.zeros((tile_h_ds, tile_w_ds), dtype=np.float32)
            crop = tile[: min(tile_h_ds, th), : min(tile_w_ds, tw)]
            fitted[: crop.shape[0], : crop.shape[1]] = crop
            tile = fitted
        x0 = int(round((x_tl - min_x) / ds + padding_ds))
        y0 = int(round((y_tl - min_y) / ds + padding_ds))
        if x0 < 0 or y0 < 0 or x0 >= mosaic_w or y0 >= mosaic_h:
            continue
        y1 = min(mosaic_h, y0 + tile_h_ds)
        x1 = min(mosaic_w, x0 + tile_w_ds)
        src = tile[: y1 - y0, : x1 - x0]
        dst = mosaic[y0:y1, x0:x1]
        dst_written = written[y0:y1, x0:x1]
        if cfg.blend_mode == "min":
            combined = np.where(dst_written, np.minimum(dst, src), src)
        else:
            combined = src
        mosaic[y0:y1, x0:x1] = combined
        written[y0:y1, x0:x1] = True

    return mosaic


def _finalize_to_uint8(mosaic: np.ndarray, cfg: OverviewConfig) -> np.ndarray:
    valid = mosaic[mosaic > 0]
    if valid.size == 0:
        return np.zeros(mosaic.shape, dtype=np.uint8)
    lo = float(np.percentile(valid, cfg.final_clip_low_percentile))
    hi = float(np.percentile(valid, cfg.final_clip_high_percentile))
    if hi <= lo:
        hi = lo + 1.0
    scaled = (mosaic - lo) * (255.0 / (hi - lo))
    return np.clip(scaled, 0, 255).astype(np.uint8)


# =============================================================================
# Orchestration
# =============================================================================

def build_section_overview(section_dir: str, out_path: str, cfg: Optional[OverviewConfig] = None) -> str:
    """Discover every mFOV in section_dir, background-subtract + downsample +
    per-mFOV gain-normalize each independently, then stitch all of them
    (they share one global pixel coordinate frame) into a single overview
    image at cfg.target_um_per_px, and save it to out_path. Returns out_path."""
    cfg = cfg or OverviewConfig()
    t0 = time.monotonic()

    mfov_dirs = discover_mfov_dirs(section_dir)
    if not mfov_dirs:
        raise RuntimeError(f"no mfov* subfolders with image_coordinates.txt found under {section_dir}")

    # Resolve ONE ds for the whole run before touching any tile: every tile
    # (whether it ends up thumbnail-sourced or full-res-fallback) is stitched
    # using this same value, so thumbnail and fallback tiles must agree on it.
    thumb_ds = discover_actual_thumbnail_ds(mfov_dirs, cfg) if cfg.use_thumbnails_if_available else None
    if thumb_ds is not None:
        cfg.ds = thumb_ds
        _log(cfg, f"[Overview] {len(mfov_dirs)} mFOV folder(s) found; using scope-native thumbnails, "
                  f"discovered ds={cfg.ds} (~{cfg.ds * cfg.raw_pixel_nm / 1000.0:.3f} um/px)")
    else:
        cfg.ds = cfg.default_ds_from_target()
        _log(cfg, f"[Overview] {len(mfov_dirs)} mFOV folder(s) found; no thumbnails found, downsampling "
                  f"full-res -- ds={cfg.ds} ({cfg.raw_pixel_nm}nm/px raw -> "
                  f"~{cfg.ds * cfg.raw_pixel_nm / 1000.0:.3f} um/px)")

    all_tiles: Dict[str, np.ndarray] = {}
    all_coords: Dict[str, Tuple[float, float]] = {}
    total_thumb, total_fullres = 0, 0
    for i, mfov_dir in enumerate(mfov_dirs):
        t_mfov = time.monotonic()
        tiles, coords, n_thumb, n_fullres = load_and_prepare_mfov_tiles(mfov_dir, cfg)
        if not tiles:
            _log(cfg, f"  [{i+1}/{len(mfov_dirs)}] {os.path.basename(mfov_dir)}: no matching beam images, skipped")
            continue
        tiles = normalize_mfov_gain(tiles, cfg)
        # basenames are only unique WITHIN one mFOV folder (beam number), so
        # namespace them by mFOV to combine into one shared dict safely.
        prefix = os.path.basename(mfov_dir.rstrip("\\/"))
        for base, arr in tiles.items():
            key = f"{prefix}/{base}"
            all_tiles[key] = arr
            all_coords[key] = coords[base]
        total_thumb += n_thumb
        total_fullres += n_fullres
        _log(cfg, f"  [{i+1}/{len(mfov_dirs)}] {prefix}: {len(tiles)} beam(s) "
                  f"({n_thumb} thumbnail, {n_fullres} full-res fallback), "
                  f"{time.monotonic() - t_mfov:.1f}s")

    _log(cfg, f"[Overview] {total_thumb} beam(s) from scope thumbnails, "
              f"{total_fullres} from full-res fallback")
    mosaic = stitch_prepared_tiles(all_tiles, all_coords, cfg)
    final = _finalize_to_uint8(mosaic, cfg)

    out_dir = os.path.dirname(out_path)
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)
    cv2.imwrite(out_path, final)
    _log(cfg, f"[Overview] wrote {final.shape[1]}x{final.shape[0]} px -> {out_path} "
              f"({time.monotonic() - t0:.1f}s total)")
    return out_path


# =============================================================================
# CLI
# =============================================================================

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--section_dir", required=True, help="section folder containing mfov*/ subfolders")
    ap.add_argument("--out", required=True, help="output overview PNG path")

    ap.add_argument("--target_um_per_px", type=float, default=0.5)
    ap.add_argument("--raw_pixel_nm", type=float, default=8.0)
    ap.add_argument("--tile_width", type=int, default=2000)
    ap.add_argument("--tile_height", type=int, default=1748)
    ap.add_argument("--padding_full", type=int, default=80)

    ap.add_argument("--background_erode_window_full", type=int, default=25,
                    help="full-res px erosion window for per-tile background subtraction")
    ap.add_argument("--background_pixel_floor", type=float, default=5.0,
                    help="raw intensities at/below this are treated as background/no-signal")
    ap.add_argument("--gain_percentile", type=float, default=75.0)
    ap.add_argument("--gain_target_level", type=float, default=100.0)

    ap.add_argument("--final_clip_low_percentile", type=float, default=0.5)
    ap.add_argument("--final_clip_high_percentile", type=float, default=99.5)
    ap.add_argument("--blend_mode", choices=["min", "overwrite"], default="min")

    ap.add_argument("--no_thumbnails", action="store_true",
                    help="always downsample from full-res, even if scope-native thumbnails are present")
    ap.add_argument("--thumbnail_extensions", nargs="+", default=["png", "tif", "tiff", "jpg", "jpeg", "bmp"],
                    help="extensions (no dot) to look for when matching a thumbnail to its full-res beam")
    ap.add_argument("--thumbnail_background_subtract", action="store_true",
                    help="also run erosion-based background subtraction on thumbnail-sourced tiles "
                         "(off by default -- thumbnails are already downsampled, so it has little to do)")

    args = ap.parse_args()

    cfg = OverviewConfig(
        target_um_per_px=args.target_um_per_px,
        raw_pixel_nm=args.raw_pixel_nm,
        tile_w_full=args.tile_width,
        tile_h_full=args.tile_height,
        padding_full=args.padding_full,
        background_erode_window_full=args.background_erode_window_full,
        background_pixel_floor=args.background_pixel_floor,
        gain_percentile=args.gain_percentile,
        gain_target_level=args.gain_target_level,
        final_clip_low_percentile=args.final_clip_low_percentile,
        final_clip_high_percentile=args.final_clip_high_percentile,
        blend_mode=args.blend_mode,
        use_thumbnails_if_available=(not args.no_thumbnails),
        thumbnail_extensions=tuple(args.thumbnail_extensions),
        thumbnail_background_subtract=args.thumbnail_background_subtract,
        verbose=True,
    )
    build_section_overview(args.section_dir, args.out, cfg)
    return 0


if __name__ == "__main__":
    sys.exit(main())
