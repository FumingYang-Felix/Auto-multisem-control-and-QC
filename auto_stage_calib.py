#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
FAST Auto Stage Calibration with 3 L-Markers (MultiSEM 91-beam, beams 001..091)
Stitch-per-MFOV detection using image_coordinates.txt (coords are TILE CENTERS, GLOBAL pixel coords).

This version (clean + fast + stable):
- Marker default order: M3 -> M2 -> M1
- B2F policy:
    * For each marker, call CMD_BEAMS_TO_FIBERS ONCE.
    * If rc == 0: stop (do NOT run B2F again).
    * If rc != 0: reset projective to per-marker default, then run B2F ONCE more.
    * Done. (No converge loops.)
- Projective defaults (you provided):
    M1 (-585.156, 649.869)
    M2 (-556.728, 662.229)
    M3 (-693.381, 711.415)
  And we enforce that during reset, projective is clamped within default±100 (safety).
- Detection / offset uses downsampled stitching (default ds=8).
- Output: ONLY ONE PNG per successful marker: the ANNOTATED stitched mosaic.
  (No separate stitched grayscale PNG.)
- Writes affine json (new = A*old + t) via --affine_out (or default timestamped file in out_dir).
- Saves an optional projective cache json (proj used per marker) into out_dir/proj_cache.json.

Notes:
- coords x/y are GLOBAL pixel coordinates of TILE CENTER (large constant offsets allowed).
- We compute elbow offset using ONLY global coords + small differences (robust to big offsets).
"""

from __future__ import annotations

import os
import json
import re
import shutil
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple, Callable

import numpy as np
from msem import MSEMConnection

try:
    from alerts import send_alert
except Exception:
    def send_alert(subject: str, body: str) -> bool:  # type: ignore[misc]
        print(f"[Alerts] alerts module unavailable -- would have sent: {subject}")
        return False


# =============================================================================
# Models
# =============================================================================

@dataclass
class LMarkerPosition:
    x: float
    y: float
    z: float
    name: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {"x": float(self.x), "y": float(self.y), "z": float(self.z), "name": self.name}

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "LMarkerPosition":
        return cls(
            x=float(d["x"]),
            y=float(d["y"]),
            z=float(d.get("z", 38500.0)),
            name=str(d.get("name", "")),
        )


@dataclass
class FoundMarker:
    idx: int
    expected: LMarkerPosition
    actual: LMarkerPosition
    stitched_annotated_path: str
    elbow_px_mosaic_ds: Tuple[int, int]
    success: bool
    error: str = ""
    b2f_rc: int = 0
    projective_x: float = float("nan")
    projective_y: float = float("nan")
    b2f_strategy: str = ""


@dataclass
class Transform2D:
    a11: float; a12: float
    a21: float; a22: float
    tx: float; ty: float

    def apply(self, x: float, y: float) -> Tuple[float, float]:
        return (self.a11 * x + self.a12 * y + self.tx,
                self.a21 * x + self.a22 * y + self.ty)


@dataclass
class AutoCalibConfig:
    # Scan parameters (sub-image size)
    scanrate: int = 1
    width: int = 2000
    height: int = 1748

    # Stage settle
    stage_settling_ms: int = 500
    stage_tol_um: float = 3.0
    stage_wait_max_s: float = 3.0

    # Output / acquisition
    remote_image_folder: str = r"V:\Fuming\msem_calib"
    image_filetype: str = "bmp"
    dp_image_location: int = 1
    bundle_timeout_s: float = 25.0
    beam_first: int = 1
    beam_last: int = 91

    # Pixel size & overlap
    pixel_nm: float = 8.0
    set_overlap_frac: Optional[float] = 0.05
    set_hexagon_overlap: Optional[float] = None

    # Downsample stitching
    ds: int = 8
    stitch_padding_px: int = 80   # in FULL-res pixels; will be scaled by ds inside stitch
    blend_mode: str = "min"
    y_flip: bool = False

    # Detection on mosaic
    lmarker_threshold: int = 70
    morph_kernel: int = 11        # in FULL-res thinking; will be scaled by ds inside detect
    min_contour_area: float = 150000.0  # full-res scale; will be scaled by ds^2

    # Auto brightness/contrast (CMD_AUTO_BRIGHTNESS_CONTRAST), run right before
    # every acquisition used for L-marker detection. Pushing contrast high
    # (near the 0-255 max) drives the image toward two grey-level clusters --
    # dark marker, bright background -- which makes the fixed-threshold
    # segmentation in detect_l_marker_elbow_on_mosaic_ds (lmarker_threshold,
    # above) much more reliable than at the API's own default target_contrast
    # of 40 (tuned for photographic images, not binary-style segmentation).
    run_auto_bc: bool = True
    target_brightness: int = 128   # DP_TARGET_BRIGHTNESS, range [0, 255], API default 128
    target_contrast: int = 230     # DP_TARGET_CONTRAST, range [0, 255], API default 40

    # Workflow
    run_b2f: bool = True
    run_autofocus: bool = True
    autofocus_hook: Optional[Callable[[], None]] = None
    post_af_sleep_s: float = 0.10

    # B2F reset defaults
    b2f_projective_bound: float = 100.0

    # Constrained XY re-search when the L-marker isn't found at the expected
    # position. Z is NEVER changed -- only X/Y are perturbed. Search points are
    # concentric rings (nearest first) out to search_radius_um, at intervals of
    # search_step_um.
    search_radius_um: float = 150.0
    search_step_um: float = 50.0
    search_points_per_ring: int = 8

    # If a candidate position sees only a PARTIAL L-marker (its contour
    # touches the mosaic edge, so the "corner" found there isn't trustworthy),
    # re-center the stage on that piece's estimated true position and
    # re-acquire, up to this many times, before giving up on the candidate.
    recenter_max_attempts: int = 2
    # Fraction of the mosaic's width/height, on each side, treated as
    # "periphery" -- a detection whose bounding box falls in that band counts
    # as incomplete/needing a recenter even if it doesn't literally touch the
    # true mosaic edge.
    edge_margin_frac: float = 0.25
    # Full-res px (scaled by ds internally): a stitching seam or threshold gap
    # can fragment the L-marker into disconnected contours; this dilation
    # bridges gaps up to roughly this size purely to find the marker's
    # overall bounding box across fragments (the elbow line-fit still uses
    # the original, un-dilated pixels). Was 800 (6.4um at 8nm/px) -- far
    # bigger than any real stitching seam needs. On a real run against a
    # textured wafer surface that merged the true L-marker together with an
    # unrelated dark scratch ~10um away into one bbox spanning nearly half
    # the mosaic, and the line-fit intersection landed in the blank space
    # between them. 100 (0.8um) still comfortably bridges genuine seam gaps
    # (a few px) without reaching distant, unrelated dark regions.
    gap_bridge_full: int = 100

    # End-of-run visual sanity check: a composite figure with a cropped view
    # (centered on the detected elbow) of each of the 3 markers' final
    # annotated mosaic, side by side. Half-width/height of that crop, in DS
    # mosaic pixels.
    summary_crop_half_px: int = 250

    # Troubleshooting: keep the 91 raw per-beam bmps (+ coords.txt) for every
    # stage-position attempt instead of deleting them, each in their own
    # subfolder, so a missed L-marker can be inspected beam-by-beam.
    keep_raw_images: bool = True

    verbose: bool = True


# Default order: M3 -> M2 -> M1
DEFAULT_MARKERS_JSON_EXAMPLE = {
    "l_markers": [
        {"x": -60768.047, "y": -48368.223, "z": 38726.947, "name": "M3"},
        {"x": -60770.781, "y":  49737.969, "z": 38726.947, "name": "M2"},
        {"x":  35215.684, "y":  49743.223, "z": 38726.947, "name": "M1"},
    ]
}

# Your per-marker default projective setpoints (used ONLY for reset when rc!=0)
DEFAULT_PROJ: Dict[str, Tuple[float, float]] = {
    "M1": (-585.156, 649.869),
    "M2": (-556.728, 662.229),
    "M3": (-693.381, 711.415),
}


# =============================================================================
# Small helpers
# =============================================================================

def ensure_dir(p: str) -> None:
    os.makedirs(p, exist_ok=True)

def clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))

def wait_file(path: str, timeout_s: float = 20.0, poll: float = 0.2) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if os.path.isfile(path):
            return True
        time.sleep(poll)
    return False


# Matches the raw per-beam bmp / coords filenames this script writes, e.g.
# "cal_M3_1_chk_1785080694649__084.bmp" or
# "cal_M3_1_srch03_1785080694649_image_coordinates.txt"
# (built from prefix = f"cal_{safe_name}_{idx+1}_{tag}_{ts}_").
_RAW_FILE_RE = re.compile(
    r"^cal_(?P<name>[^_]+)_(?P<idx>\d+)_(?P<tag>[^_]+)_(?P<ts>\d+)"
    r"(?:__\d+\.\w+|_image_coordinates\.txt)$"
)


def sweep_orphaned_raw_images(remote_image_folder: str, out_dir: str, verbose: bool = True) -> int:
    """
    Move any leftover cal_*.bmp / cal_*_image_coordinates.txt files sitting
    directly in remote_image_folder into their matching marker/tag subfolder
    under out_dir, instead of leaving them in the flat remote folder.

    Normally every attempt's raw images get organized into
    out_dir/marker_<idx>_<name>/raw_<tag>/ right after acquisition. But a Stop
    click can hard-kill this process on Windows with zero chance for that (or
    any other) cleanup code to run, leaving that attempt's files stranded in
    remote_image_folder. This sweep runs at startup of every new invocation so
    a previous interrupted run's leftovers get cleaned up before this run
    writes anything new. Returns the number of files moved.
    """
    if not os.path.isdir(remote_image_folder):
        return 0

    groups: Dict[Tuple[str, str, str], List[str]] = {}
    for fname in os.listdir(remote_image_folder):
        m = _RAW_FILE_RE.match(fname)
        if not m:
            continue
        key = (m.group("idx"), m.group("name"), m.group("tag"))
        groups.setdefault(key, []).append(os.path.join(remote_image_folder, fname))

    moved = 0
    for (idx, name, tag), paths in groups.items():
        dest_dir = os.path.join(out_dir, f"marker_{idx}_{name}", f"raw_{tag}_recovered")
        ensure_dir(dest_dir)
        for p in paths:
            try:
                shutil.move(p, os.path.join(dest_dir, os.path.basename(p)))
                moved += 1
            except Exception:
                pass
        if verbose and paths:
            print(f"[Sweep] recovered {len(paths)} leftover raw image(s) for marker_{idx}_{name} "
                  f"(tag={tag}, likely from a Stop-interrupted previous run) -> {dest_dir}")
    return moved


# =============================================================================
# JSON helpers
# =============================================================================

def load_markers_from_json(json_path: str) -> List[LMarkerPosition]:
    with open(json_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    markers = [LMarkerPosition.from_dict(m) for m in data.get("l_markers", [])]
    if len(markers) != 3:
        raise ValueError(f"Expected 3 markers in JSON, got {len(markers)}")
    return markers

def save_markers_to_json(markers: List[LMarkerPosition], json_path: str) -> None:
    data: Dict[str, Any] = {}
    if os.path.exists(json_path):
        try:
            with open(json_path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            data = {}
    data["l_markers"] = [m.to_dict() for m in markers]
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)

def save_affine_json(transform: Transform2D, path: str, extra: Optional[Dict[str, Any]] = None) -> None:
    payload: Dict[str, Any] = {
        "a11": transform.a11, "a12": transform.a12,
        "a21": transform.a21, "a22": transform.a22,
        "tx": transform.tx, "ty": transform.ty,
        "model": "Transform2D",
        "equation": "new = A*old + t",
    }
    if extra:
        payload.update(extra)
    ensure_dir(os.path.dirname(path) or ".")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)


# =============================================================================
# Output keys (MultiSEM API wrapper differences)
# =============================================================================

FOLDER_KEYS = ["STR_REMOTE_IMAGE_FOLDER", "STR_OUTPUT_FOLDER", "STR_RECORD_OUTPUT_FOLDER"]
PREFIX_KEYS = ["STR_IMAGE_FILENAME_PREFIX", "STR_OUTPUT_PREFIX", "STR_RECORD_OUTPUT_PREFIX"]
FORMAT_KEYS = ["STR_IMAGE_FILETYPE", "STR_OUTPUT_FORMAT", "STR_RECORD_OUTPUT_FORMAT"]

def set_output(conn: MSEMConnection, folder: str, prefix: str, fmt: str = "bmp") -> None:
    ensure_dir(folder)
    try:
        conn.set_double("DP_IMAGE_LOCATION", 1.0)
    except Exception:
        pass
    for k in FOLDER_KEYS:
        try:
            conn.set_string(k, folder)
        except Exception:
            pass
    for k in PREFIX_KEYS:
        try:
            conn.set_string(k, prefix)
        except Exception:
            pass
    for k in FORMAT_KEYS:
        try:
            conn.set_string(k, fmt)
        except Exception:
            pass

def _get_sem_output_folder(conn: MSEMConnection) -> Optional[str]:
    for k in FOLDER_KEYS:
        try:
            path = (conn.get_string(k) or "").strip()
            if path:
                return path
        except Exception:
            continue
    return None

def set_coordinates_filename(conn: MSEMConnection, coords_path: str) -> None:
    conn.set_string("STR_COORDINATES_FILENAME", coords_path)


# =============================================================================
# Coordinates parsing
# =============================================================================

def read_coords_table(coords_path: str) -> Dict[str, Tuple[float, float]]:
    """
    Tab-separated 4 columns: filename, x, y, z
    x,y are GLOBAL pixel coordinates of TILE CENTER.
    """
    coords: Dict[str, Tuple[float, float]] = {}
    with open(coords_path, "r", encoding="utf-8", errors="ignore") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            parts = line.split("\t")
            if len(parts) < 4:
                parts = line.split()
                if len(parts) < 4:
                    continue
            fn = os.path.basename(parts[0])
            try:
                x = float(parts[1])
                y = float(parts[2])
            except Exception:
                continue
            coords[fn] = (x, y)
    return coords


# =============================================================================
# Image IO + DS stitch
# =============================================================================

def load_gray_ds(path: str, ds: int) -> np.ndarray:
    import cv2
    img = cv2.imread(path, cv2.IMREAD_GRAYSCALE)
    if img is None:
        raise RuntimeError(f"cv2 failed reading {path}")
    if ds <= 1:
        return img
    h, w = img.shape[:2]
    h2 = max(1, int(round(h / ds)))
    w2 = max(1, int(round(w / ds)))
    return cv2.resize(img, (w2, h2), interpolation=cv2.INTER_AREA)

def stitch_mosaic_from_centers_ds(
    image_paths: List[str],
    coords_map: Dict[str, Tuple[float, float]],
    tile_w_full: int,
    tile_h_full: int,
    padding_full: int,
    ds: int,
    y_flip: bool = False,
    blend_mode: str = "min",
) -> Tuple[np.ndarray, float, float, int, int]:
    """
    Build a DS mosaic using GLOBAL tile center coords (full-res coords).
    Return:
      mosaic_ds (uint8),
      min_tl_x_full, min_tl_y_full (full-res top-left origin),
      tile_w_ds, tile_h_ds
    """
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
        x_tl = x - tile_w_full / 2.0
        y_tl = y - tile_h_full / 2.0
        items.append((p, x_tl, y_tl))

    if len(items) < 10:
        raise RuntimeError(f"Too few tiles matched coords: {len(items)}")

    xs = np.array([it[1] for it in items], dtype=np.float64)
    ys = np.array([it[2] for it in items], dtype=np.float64)

    min_x_full, max_x_full = float(xs.min()), float(xs.max())
    min_y_full, max_y_full = float(ys.min()), float(ys.max())

    # DS mosaic size
    mosaic_w = int(round((max_x_full - min_x_full) / ds + tile_w_ds + 2 * padding_s))
    mosaic_h = int(round((max_y_full - min_y_full) / ds + tile_h_ds + 2 * padding_s))
    mosaic_w = max(mosaic_w, tile_w_ds + 2 * padding_s)
    mosaic_h = max(mosaic_h, tile_h_ds + 2 * padding_s)

    mosaic = np.full((mosaic_h, mosaic_w), 255, dtype=np.uint8)

    for (p, x_tl_full, y_tl_full) in items:
        x0 = int(round((x_tl_full - min_x_full) / ds + padding_s))
        y0 = int(round((y_tl_full - min_y_full) / ds + padding_s))

        tile = load_gray_ds(p, ds=ds)

        th, tw = tile.shape
        if (tw, th) != (tile_w_ds, tile_h_ds):
            # center-crop/pad to DS tile size
            tile2 = np.full((tile_h_ds, tile_w_ds), 255, dtype=np.uint8)
            cx0 = max(0, (tw - tile_w_ds) // 2)
            cy0 = max(0, (th - tile_h_ds) // 2)
            crop = tile[cy0:cy0 + min(tile_h_ds, th), cx0:cx0 + min(tile_w_ds, tw)]
            oy = (tile_h_ds - crop.shape[0]) // 2
            ox = (tile_w_ds - crop.shape[1]) // 2
            tile2[oy:oy + crop.shape[0], ox:ox + crop.shape[1]] = crop
            tile = tile2

        y1 = min(mosaic_h, y0 + tile_h_ds)
        x1 = min(mosaic_w, x0 + tile_w_ds)
        if y0 < 0 or x0 < 0 or y0 >= mosaic_h or x0 >= mosaic_w:
            continue

        patch = mosaic[y0:y1, x0:x1]
        src = tile[:y1 - y0, :x1 - x0]

        if blend_mode == "min":
            mosaic[y0:y1, x0:x1] = np.minimum(patch, src)
        else:
            mosaic[y0:y1, x0:x1] = src

    return mosaic, min_x_full, min_y_full, tile_w_ds, tile_h_ds


# =============================================================================
# Global-coord elbow offset (DS-aware mapping)
# =============================================================================

def elbow_offset_um_from_global_coords_ds(
    elbow_px_ds: Tuple[int, int],
    coords_map: Dict[str, Tuple[float, float]],
    tile_w_full: int,
    tile_h_full: int,
    padding_full: int,
    ds: int,
    um_per_full_px: float,
    y_flip: bool = False,
    debug: bool = False,
) -> Tuple[float, float]:
    """
    coords_map: filename -> (x_center, y_center) in GLOBAL FULL pixel coords.
    elbow_px_ds: (u_ds, v_ds) in DS mosaic pixel coords.
    """
    # MFOV center in full global coords
    xs = []
    ys = []
    tl_xs_full = []
    tl_ys_full = []
    for (_fn, (x, y)) in coords_map.items():
        if y_flip:
            y = -y
        xs.append(x)
        ys.append(y)
        tl_xs_full.append(x - tile_w_full / 2.0)
        tl_ys_full.append(y - tile_h_full / 2.0)

    xs = np.array(xs, dtype=np.float64)
    ys = np.array(ys, dtype=np.float64)
    tl_xs_full = np.array(tl_xs_full, dtype=np.float64)
    tl_ys_full = np.array(tl_ys_full, dtype=np.float64)

    cx_full = float(np.median(xs))
    cy_full = float(np.median(ys))

    min_tl_x_full = float(np.min(tl_xs_full))
    min_tl_y_full = float(np.min(tl_ys_full))

    padding_s = max(1, int(round(padding_full / ds)))
    u_ds, v_ds = float(elbow_px_ds[0]), float(elbow_px_ds[1])

    # DS mosaic -> full global top-left coords
    elbow_tl_x_full = (u_ds - padding_s) * ds + min_tl_x_full
    elbow_tl_y_full = (v_ds - padding_s) * ds + min_tl_y_full

    # Convert to center-coord system (tile centers)
    elbow_x_full = elbow_tl_x_full + tile_w_full / 2.0
    elbow_y_full = elbow_tl_y_full + tile_h_full / 2.0

    dx_full_px = elbow_x_full - cx_full
    dy_full_px = elbow_y_full - cy_full
    dx_um = dx_full_px * um_per_full_px
    dy_um = dy_full_px * um_per_full_px

    if debug:
        print(f"    [DBG] mfov_center_global_px=({cx_full:.3f},{cy_full:.3f}) "
              f"elbow_global_px=({elbow_x_full:.3f},{elbow_y_full:.3f}) "
              f"delta_px=({dx_full_px:.3f},{dy_full_px:.3f}) "
              f"delta_um=({dx_um:.3f},{dy_um:.3f}) ds={ds}")

    return dx_um, dy_um


# =============================================================================
# Detection + annotation (COLOR) on DS mosaic
# =============================================================================

def _l_marker_corner_quadrant(roi: np.ndarray, bw: int, bh: int) -> str:
    """Which of the 4 bbox corners ("tl"/"tr"/"bl"/"br") the L-marker's elbow
    sits at: the quadrant DIAGONALLY OPPOSITE the emptiest one (that emptiest
    quadrant is, by construction, the one no arm reaches into)."""
    hy, hx = bh // 2, bw // 2
    quad_density = {
        "tl": roi[:hy, :hx].mean() if hy and hx else 0.0,
        "tr": roi[:hy, hx:].mean() if hy and (bw - hx) else 0.0,
        "bl": roi[hy:, :hx].mean() if (bh - hy) and hx else 0.0,
        "br": roi[hy:, hx:].mean() if (bh - hy) and (bw - hx) else 0.0,
    }
    empty_quad = min(quad_density, key=quad_density.get)
    return {"tl": "br", "tr": "bl", "bl": "tr", "br": "tl"}[empty_quad]


def _quadrant_emptiness_ratio(roi: np.ndarray, bw: int, bh: int) -> float:
    """
    How convincingly one bbox quadrant is emptier than the rest: (emptiest
    quadrant's density) / (mean density of the other three), in [0, 1] (0 =
    that quadrant is bit-for-bit empty). A genuine L-marker's bbox has one
    quadrant its two arms never reach, so this ratio is near 0; a solid or
    diffusely-noisy blob (e.g. real tissue/dust merged across a large area by
    the gap-bridging dilation) tends to have similar density in all four
    quadrants, so this ratio stays close to 1. Used to reject candidate
    regions that cleared the area check purely by being large, not by
    actually looking like an L -- confirmed against a real failed run where a
    massive, near-uniform dust-and-scratch blob (far below any single
    quadrant standing out) cleared the area threshold ahead of the true,
    compact L and was fit as if it were one.
    """
    hy, hx = bh // 2, bw // 2
    d = {
        "tl": roi[:hy, :hx].mean() if hy and hx else 0.0,
        "tr": roi[:hy, hx:].mean() if hy and (bw - hx) else 0.0,
        "bl": roi[hy:, :hx].mean() if (bh - hy) and hx else 0.0,
        "br": roi[hy:, hx:].mean() if (bh - hy) and (bw - hx) else 0.0,
    }
    values = list(d.values())
    emptiest = min(values)
    others_mean = (sum(values) - emptiest) / 3.0
    if others_mean <= 1e-9:
        return 1.0
    return float(emptiest / others_mean)


def _fit_l_marker_elbow_local(roi: np.ndarray, bw: int, bh: int) -> Optional[Tuple[float, float, int]]:
    """
    Finds the L-marker's elbow by fitting a line to each arm's centerline --
    per-column pixel centroids for the horizontal arm, per-row centroids for
    the vertical arm, each a least-squares fit over the arm's FULL length --
    and intersecting the two lines. Returns local (x, y, band) within roi
    (band is the arm-thickness-derived half-window later used to sanity-check
    that the fitted point actually sits near real foreground pixels), or None.

    This is deliberately NOT based on local corner curvature (convexity
    defects / corner detectors) at the joint: a single stitching seam or
    threshold gap right at the joint can fragment the marker into disconnected
    contours, at which point "the corner" found near/on whichever fragment
    happens to be largest is often just a point on a straight arm, not a real
    corner, and looks just as confident as a correct one. Fitting each arm's
    centerline over its whole length tolerates such gaps -- it only needs
    enough intact pixels somewhere along the arm, not an unbroken run.
    """
    area = int(np.count_nonzero(roi))
    s = bw + bh
    disc = max(0.0, s * s - 4.0 * area)
    thickness = (s - disc ** 0.5) / 2.0
    thickness = max(3.0, min(thickness, min(bw, bh) / 2.0))
    band = max(3, int(round(thickness * 1.5)))

    corner_quad = _l_marker_corner_quadrant(roi, bw, bh)
    top = corner_quad[0] == "t"
    left = corner_quad[1] == "l"

    # roi's own bbox can be padded well beyond the marker's real content (the
    # gap-bridging dilation used to FIND this bbox pads it outward on every
    # side), so the arm does NOT necessarily start at row/col 0 -- anchor the
    # band on where content actually starts near that edge, not the edge
    # itself, or the band would only catch part of the arm's thickness and
    # bias the fitted centerline toward the outer, empty side.
    row_has_fg = np.nonzero(roi.any(axis=1))[0]
    col_has_fg = np.nonzero(roi.any(axis=0))[0]
    if row_has_fg.size == 0 or col_has_fg.size == 0:
        return None

    if top:
        h_start = int(row_has_fg[0])
        h_rows, h_row_offset = roi[h_start:h_start + band, :], h_start
    else:
        h_end = int(row_has_fg[-1])
        h_start = max(0, h_end - band + 1)
        h_rows, h_row_offset = roi[h_start:h_end + 1, :], h_start
    if left:
        v_start = int(col_has_fg[0])
        v_cols, v_col_offset = roi[:, v_start:v_start + band], v_start
    else:
        v_end = int(col_has_fg[-1])
        v_start = max(0, v_end - band + 1)
        v_cols, v_col_offset = roi[:, v_start:v_end + 1], v_start

    xs, ys = [], []
    for x in range(bw):
        rows = np.nonzero(h_rows[:, x])[0]
        if rows.size:
            xs.append(x)
            ys.append(float(rows.mean()) + h_row_offset)
    if len(xs) < max(2, band):
        return None
    A = np.vstack([xs, np.ones(len(xs))]).T
    m1, b1 = np.linalg.lstsq(A, ys, rcond=None)[0]

    ys2, xs2 = [], []
    for y in range(bh):
        cols = np.nonzero(v_cols[y, :])[0]
        if cols.size:
            ys2.append(y)
            xs2.append(float(cols.mean()) + v_col_offset)
    if len(ys2) < max(2, band):
        return None
    A2 = np.vstack([ys2, np.ones(len(ys2))]).T
    m2, b2 = np.linalg.lstsq(A2, xs2, rcond=None)[0]

    # solve y = m1*x + b1  and  x = m2*y + b2
    denom = 1.0 - m1 * m2
    if abs(denom) < 1e-9:
        return None
    y_i = (m1 * b2 + b1) / denom
    x_i = m2 * y_i + b2
    if not (0 <= x_i <= bw and 0 <= y_i <= bh):
        return None
    return x_i, y_i, band


def detect_l_marker_elbow_on_mosaic_ds(
    mosaic_ds: np.ndarray,
    threshold: int,
    morph_kernel_full: int,
    min_area_full: float,
    ds: int,
    edge_margin_frac: float = 0.25,
    gap_bridge_full: int = 100,
) -> Optional[Tuple[Tuple[int, int], bool]]:
    """
    Returns None if no marker-sized region is found, else (corner, is_complete).

    The elbow itself comes from _fit_l_marker_elbow_local (see there for why
    a line-fit intersection replaces the old local-corner-curvature approach).
    A stitching seam or threshold gap can also fragment the marker into
    several disconnected contours in cv2.findContours; gap_bridge_full
    controls a generous dilation used ONLY to find the marker's overall
    bounding box across such fragments -- the line-fit itself still runs on
    the original (un-dilated) pixels within that box, so gaps are tolerated
    rather than papered over.

    is_complete is False when the corner sits within edge_margin_frac of the
    mosaic's periphery -- only the corner's own position is judged, not the
    arms' reach (an arm can be longer than the mosaic's field of view).
    """
    import cv2

    # scale params for DS
    k = max(3, int(round(morph_kernel_full / ds)))
    min_area_ds = float(min_area_full) / float(ds * ds)

    _, binary = cv2.threshold(mosaic_ds, int(threshold), 255, cv2.THRESH_BINARY_INV)

    kernel = np.ones((k, k), np.uint8)
    binary = cv2.morphologyEx(binary, cv2.MORPH_CLOSE, kernel)
    binary = cv2.morphologyEx(binary, cv2.MORPH_OPEN, kernel)

    gap_bridge_ds = max(k, int(round(gap_bridge_full / ds)))
    merge_kernel = np.ones((gap_bridge_ds, gap_bridge_ds), np.uint8)
    merged = cv2.dilate(binary, merge_kernel)

    contours, _ = cv2.findContours(merged, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not contours:
        return None
    contours = sorted(contours, key=cv2.contourArea, reverse=True)

    # A genuine L-marker's bbox has one quadrant its arms never reach; a
    # solid/diffuse blob (real tissue texture + dust merged across a large
    # area by the gap-bridging dilation above) doesn't -- its density is
    # closer to uniform across all four quadrants. 0.3 rejects that kind of
    # near-uniform blob (a real failed run's false-positive bboxes measured
    # 0.39-0.62 here) while leaving a real L's near-0 ratio a comfortable
    # margin of headroom. NOTE: this is a necessary check, not a sufficient
    # one -- a thin, off-center feature (e.g. a lone scratch) can also land a
    # low ratio despite not being an L at all, so it will not catch every
    # false positive on heavily textured real specimen surfaces; it is
    # deliberately paired with gap_bridge_full's now-much-smaller default so
    # fewer unrelated dark regions get merged into one bbox in the first
    # place (see _quadrant_emptiness_ratio).
    max_emptiness_ratio = 0.3

    corner = None
    for c in contours[:10]:
        bx, by, bw, bh = cv2.boundingRect(c)
        roi = binary[by:by + bh, bx:bx + bw] > 0
        real_area = int(np.count_nonzero(roi))
        if real_area < min_area_ds:
            continue
        if _quadrant_emptiness_ratio(roi, bw, bh) > max_emptiness_ratio:
            continue

        fit = _fit_l_marker_elbow_local(roi, bw, bh)
        if fit is None:
            continue
        cx_local, cy_local, band = fit

        # The fitted intersection is where two dark arms are claimed to MEET
        # -- genuine foreground pixels must actually be right there. Confirmed
        # against a real failed run where the fit landed in a blank stretch of
        # an over-large (dilation-merged) bbox, nowhere near any dark pixel,
        # yet still passed every other check this function used to perform.
        y0, y1 = max(0, int(round(cy_local)) - band), min(bh, int(round(cy_local)) + band + 1)
        x0, x1 = max(0, int(round(cx_local)) - band), min(bw, int(round(cx_local)) + band + 1)
        if not roi[y0:y1, x0:x1].any():
            continue

        corner = (int(round(cx_local + bx)), int(round(cy_local + by)))
        break

    if corner is None:
        return None

    # Completeness is judged by the CORNER POINT's own position, not the
    # whole shape's bounding box: an L-marker's arms can be longer than the
    # mosaic's field of view, so a far arm TIP can sit at the mosaic edge no
    # matter how well the corner itself is centered -- requiring the whole
    # shape to fit centrally made this basically unsatisfiable and caused
    # every search/recenter attempt to report "incomplete" even once the
    # corner had converged to a clean, trustworthy position. Only the corner
    # matters for the affine calc, so only its own proximity to the edge is
    # checked: a hard touch (definite risk the corner itself is clipped/
    # distorted by the boundary) or sitting within the outer edge_margin_frac
    # periphery band (found, but not confidently centered).
    cx, cy = corner
    mosaic_h, mosaic_w = mosaic_ds.shape[:2]

    strict_margin = max(2 * k, 10)
    corner_touches_true_edge = (
        cx <= strict_margin or cy <= strict_margin
        or cx >= mosaic_w - strict_margin
        or cy >= mosaic_h - strict_margin
    )

    frac = edge_margin_frac
    corner_near_periphery = (
        cx <= frac * mosaic_w or cy <= frac * mosaic_h
        or cx >= (1.0 - frac) * mosaic_w
        or cy >= (1.0 - frac) * mosaic_h
    )

    is_complete = not (corner_touches_true_edge or corner_near_periphery)

    return corner, is_complete


def annotate_point_color_ds(mosaic_gray_ds: np.ndarray, pt: Tuple[int, int], ds: int,
                            label: Optional[str] = None) -> np.ndarray:
    import cv2
    out = cv2.cvtColor(mosaic_gray_ds, cv2.COLOR_GRAY2BGR)
    x, y = int(pt[0]), int(pt[1])

    # scale marker sizes roughly with ds
    ms = int(max(40, 140 / max(1, ds)))
    th = int(max(2, 8 / max(1, ds)))
    rad1 = int(max(18, 70 / max(1, ds)))
    rad2 = int(max(6, 12 / max(1, ds)))

    cv2.drawMarker(out, (x, y), color=(0, 0, 255), markerType=cv2.MARKER_CROSS, markerSize=ms, thickness=th)
    cv2.circle(out, (x, y), rad1, color=(255, 255, 0), thickness=max(1, th // 2))
    cv2.circle(out, (x, y), rad2, color=(0, 0, 255), thickness=-1)
    cv2.putText(out, f"elbow_ds=({x},{y}) ds={ds}", (x + 10, max(20, y - 10)),
                cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 0, 255), 2, cv2.LINE_AA)
    if label:
        cv2.putText(out, label, (x + 10, max(45, y + 20)),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 128, 255), 2, cv2.LINE_AA)
    return out


# =============================================================================
# Affine transform
# =============================================================================

def generate_spiral_search_offsets(
    radius_um: float,
    step_um: float,
    points_per_ring: int = 8,
) -> List[Tuple[float, float]]:
    """
    Concentric-ring XY search pattern (nearest ring first) covering a disk of
    the given radius, e.g. radius=150, step=50 -> rings at 50/100/150 um, each
    with `points_per_ring` compass points. Excludes the (0,0) center point --
    callers should check the expected position first.
    """
    if radius_um <= 0 or step_um <= 0 or points_per_ring <= 0:
        return []
    offsets: List[Tuple[float, float]] = []
    n_rings = int(round(radius_um / step_um))
    for ring in range(1, n_rings + 1):
        r = min(ring * step_um, radius_um)
        for k in range(points_per_ring):
            theta = 2.0 * np.pi * k / points_per_ring
            offsets.append((r * np.cos(theta), r * np.sin(theta)))
    return offsets


def compute_affine_from_3_points(old_pts: List[Tuple[float, float]], new_pts: List[Tuple[float, float]]) -> Transform2D:
    old = np.array(old_pts, dtype=np.float64)
    new = np.array(new_pts, dtype=np.float64)
    old_h = np.hstack([old, np.ones((3, 1), dtype=np.float64)])
    T, _, _, _ = np.linalg.lstsq(old_h, new, rcond=None)
    return Transform2D(
        a11=float(T[0, 0]), a12=float(T[0, 1]),
        a21=float(T[1, 0]), a22=float(T[1, 1]),
        tx=float(T[2, 0]), ty=float(T[2, 1]),
    )


# =============================================================================
# Stage + beam helpers
# =============================================================================

def wait_stage_basic(conn: MSEMConnection, tx: float, ty: float, tz: float, tol_um: float = 3.0, max_s: float = 3.0) -> bool:
    conn.execute("CMD_WAIT_STAGE_SETTLED")
    t0 = time.monotonic()
    while time.monotonic() - t0 < max_s:
        x = conn.get_double("AP_STAGE_AT_X")
        y = conn.get_double("AP_STAGE_AT_Y")
        z = conn.get_double("AP_STAGE_AT_Z")
        if max(abs(x - tx), abs(y - ty), abs(z - tz)) <= tol_um:
            return True
        time.sleep(0.1)
    return False

def ensure_beams_on_and_scan(conn: MSEMConnection) -> None:
    # beam on
    try:
        if float(conn.get_double("DP_IS_BEAM_ON")) < 0.5:
            conn.execute("CMD_BEAM_ON")
            conn.execute("CMD_WAIT_BEAM_ON")
    except Exception:
        conn.execute("CMD_BEAM_ON")
        conn.execute("CMD_WAIT_BEAM_ON")

    # blanker auto
    try:
        conn.set_double("DP_BEAMBLANKER_MODE", 2.0)
    except Exception:
        pass

    # continuous scan on (best-effort)
    try:
        conn.execute("CMD_CONTINUOUS_SCAN_ON")
    except Exception:
        pass

def get_projective(conn: MSEMConnection) -> Tuple[float, float]:
    return (float(conn.get_double("AP_PROJECTIVE_DEFLECTOR_X")),
            float(conn.get_double("AP_PROJECTIVE_DEFLECTOR_Y")))

def set_projective(conn: MSEMConnection, px: float, py: float) -> None:
    conn.set_double("AP_PROJECTIVE_DEFLECTOR_X", float(px))
    conn.set_double("AP_PROJECTIVE_DEFLECTOR_Y", float(py))

def b2f_once_then_reset_default(
    conn: MSEMConnection,
    marker_name: str,
    bound: float,
    verbose: bool = True
) -> Tuple[int, float, float, str]:
    """
    Policy:
      - Run B2F once.
      - If rc==0 -> done (do NOT run again).
      - If rc!=0 -> set projective to marker default (clamped default±bound), then run B2F once more.
    Returns: (rc_final, proj_x, proj_y, strategy)
    """
    name = (marker_name or "").upper()

    rc1 = int(conn.execute("CMD_BEAMS_TO_FIBERS"))
    px1, py1 = get_projective(conn)
    if verbose:
        print(f"    [B2F] rc={rc1} projective=({px1:.3f},{py1:.3f})")

    if rc1 == 0:
        return 0, px1, py1, "once_ok"

    # reset to per-marker default
    strat = "reset_default"
    if name in DEFAULT_PROJ:
        dpx, dpy = DEFAULT_PROJ[name]
        # keep within default±bound (safety). (This doesn't change default, but enforces your rule.)
        dpx = clamp(dpx, dpx - bound, dpx + bound)
        dpy = clamp(dpy, dpy - bound, dpy + bound)
        set_projective(conn, dpx, dpy)
        if verbose:
            print(f"    [B2F] rc!=0 -> reset projective to default {name}=({dpx:.3f},{dpy:.3f}), retry once")
    else:
        if verbose:
            print(f"    [B2F] rc!=0 -> no default projective for {name}, retry once without reset")
        strat = "retry_no_default"

    rc2 = int(conn.execute("CMD_BEAMS_TO_FIBERS"))
    px2, py2 = get_projective(conn)
    if verbose:
        print(f"    [B2F] retry rc={rc2} projective=({px2:.3f},{py2:.3f})")
    return rc2, px2, py2, strat


# =============================================================================
# Core calibrator
# =============================================================================

class AutoStageCalibrator:
    def __init__(self, conn: MSEMConnection, cfg: AutoCalibConfig):
        self.conn = conn
        self.cfg = cfg

    def _log(self, msg: str) -> None:
        if self.cfg.verbose:
            print(msg)

    def configure_scan(self) -> None:
        # prepared width/height/scanrate
        self.conn.set_double("DP_PREPARED_WIDTH", float(self.cfg.width))
        self.conn.set_double("DP_PREPARED_HEIGHT", float(self.cfg.height))
        self.conn.set_double("DP_PREPARED_SCANRATE", float(self.cfg.scanrate))
        rc = self.conn.execute("CMD_APPLY_SCAN_PARAMETERS")
        if rc != 0:
            raise RuntimeError(f"CMD_APPLY_SCAN_PARAMETERS rc={rc}")

        # pixel size
        self.conn.set_double("AP_PIXELSIZE", float(self.cfg.pixel_nm))

        # overlap (optional)
        if self.cfg.set_overlap_frac is not None:
            f = float(self.cfg.set_overlap_frac)
            fov_x_um = (self.cfg.width * self.cfg.pixel_nm) / 1000.0
            fov_y_um = (self.cfg.height * self.cfg.pixel_nm) / 1000.0
            self.conn.set_double("AP_OVERLAP_X", float(f * fov_x_um))
            self.conn.set_double("AP_OVERLAP_Y", float(f * fov_y_um))

        if self.cfg.set_hexagon_overlap is not None:
            self.conn.set_double("AP_HEXAGON_OVERLAP", float(self.cfg.set_hexagon_overlap))

    def get_um_per_full_px(self) -> float:
        return float(self.cfg.pixel_nm) / 1000.0

    def move_xy_then_z(self, x_um: float, y_um: float, z_um: float) -> None:
        self.conn.set_double("AP_STAGE_SETTLING_TIME", float(self.cfg.stage_settling_ms))
        self.conn.set_double("AP_STAGE_GOTO_X", float(x_um))
        self.conn.set_double("AP_STAGE_GOTO_Y", float(y_um))
        self.conn.execute("CMD_WAIT_STAGE_SETTLED")
        self.conn.set_double("AP_STAGE_GOTO_Z", float(z_um))
        self.conn.execute("CMD_WAIT_STAGE_SETTLED")

        ok = wait_stage_basic(self.conn, x_um, y_um, z_um, tol_um=self.cfg.stage_tol_um, max_s=self.cfg.stage_wait_max_s)
        if not ok:
            self._log("  [WARN] Stage readback not within tolerance (continuing).")

    def run_autofocus(self) -> int:
        if self.cfg.autofocus_hook is not None:
            self.cfg.autofocus_hook()
            return 0
        return int(self.conn.execute("CMD_AUTOFOCUS"))

    def run_auto_brightness_contrast(self) -> int:
        """MultiSEM API CMD_AUTO_BRIGHTNESS_CONTRAST (sec. 4.3.12): adjusts the
        detector offset/sensitivity so the live image matches
        DP_TARGET_BRIGHTNESS / DP_TARGET_CONTRAST, on average. Called right
        before each acquisition used for L-marker detection so the marker
        segments as near-binary black/white (see cfg.target_contrast)."""
        if not self.cfg.run_auto_bc:
            return 0
        self.conn.set_double("DP_TARGET_BRIGHTNESS", float(self.cfg.target_brightness))
        self.conn.set_double("DP_TARGET_CONTRAST", float(self.cfg.target_contrast))
        rc = int(self.conn.execute("CMD_AUTO_BRIGHTNESS_CONTRAST"))
        if rc != 0:
            self._log(f"    [AutoBC] WARNING: CMD_AUTO_BRIGHTNESS_CONTRAST rc={rc} "
                      f"(target_brightness={self.cfg.target_brightness}, target_contrast={self.cfg.target_contrast}) "
                      f"-- continuing with current detector settings.")
        elif self.cfg.verbose:
            self._log(f"    [AutoBC] ok (target_brightness={self.cfg.target_brightness}, "
                      f"target_contrast={self.cfg.target_contrast})")
        return rc

    def _acquire_paths(self, prefix_norm: str) -> Tuple[str, str, List[str]]:
        lookup_folder = _get_sem_output_folder(self.conn) or self.cfg.remote_image_folder
        coords_path = os.path.join(self.cfg.remote_image_folder, f"{prefix_norm}_image_coordinates.txt")
        beams = list(range(self.cfg.beam_first, self.cfg.beam_last + 1))
        bmp_paths = [os.path.join(lookup_folder, f"{prefix_norm}__{b:03d}.{self.cfg.image_filetype}") for b in beams]
        return lookup_folder, coords_path, bmp_paths

    def cleanup_temp_files(self, paths: List[str]) -> None:
        for p in paths:
            try:
                if os.path.isfile(p):
                    os.remove(p)
            except Exception:
                pass

    def save_raw_images(self, paths: List[str], outputs_dir: str, tag: str) -> str:
        """
        Move the 91 raw per-beam bmps + coords.txt for one stage-position
        attempt into their own subfolder (instead of deleting them), so a
        missed L-marker can be inspected beam-by-beam. Returns the folder path
        (empty string if nothing was moved).
        """
        raw_dir = os.path.join(outputs_dir, f"raw_{tag}")
        moved_any = False
        for p in paths:
            try:
                if os.path.isfile(p):
                    if not moved_any:
                        ensure_dir(raw_dir)
                        moved_any = True
                    shutil.move(p, os.path.join(raw_dir, os.path.basename(p)))
            except Exception:
                pass
        return raw_dir if moved_any else ""

    def acquire_stitch_detect_annotated_once(
        self,
        marker_name: str,
        prefix: str,
        outputs_dir: str,
    ) -> Tuple[bool, bool, str, Tuple[int, int], Dict[str, Tuple[float, float]], List[str]]:
        """
        Acquire bundle, build DS mosaic, detect elbow, and SAVE ONLY ONE annotated PNG (if found).
        Returns:
          found, is_complete, annotated_path, elbow_ds, coords_map, temp_to_delete

        is_complete is False when the detected shape touches the mosaic edge
        (see detect_l_marker_elbow_on_mosaic_ds) -- found=True but
        is_complete=False means "a piece of the marker was seen, but not
        confidently enough to trust the corner," not "no marker present."
        """
        import cv2

        ensure_dir(outputs_dir)
        ensure_dir(self.cfg.remote_image_folder)

        set_output(self.conn, self.cfg.remote_image_folder, prefix, fmt=self.cfg.image_filetype)

        prefix_norm = prefix.rstrip("_")
        _, coords_path, bmp_paths = self._acquire_paths(prefix_norm)
        set_coordinates_filename(self.conn, coords_path)

        rc = int(self.conn.execute("CMD_TRIGGER_ACQUISITION"))
        if rc != 0:
            raise RuntimeError(f"CMD_TRIGGER_ACQUISITION rc={rc}")
        rc = int(self.conn.execute("CMD_WAIT_SCAN_COMPLETE"))
        if rc != 0:
            raise RuntimeError(f"CMD_WAIT_SCAN_COMPLETE rc={rc}")
        rc = int(self.conn.execute("CMD_WAIT_ACQUISITION_COMPLETE"))
        if rc != 0:
            raise RuntimeError(f"CMD_WAIT_ACQUISITION_COMPLETE rc={rc}")

        if not wait_file(coords_path, timeout_s=self.cfg.bundle_timeout_s):
            raise RuntimeError(f"Coordinates file not found: {coords_path}")

        # wait for bmps
        deadline = time.monotonic() + self.cfg.bundle_timeout_s
        while time.monotonic() < deadline:
            missing = [p for p in bmp_paths if not os.path.isfile(p)]
            if not missing:
                break
            time.sleep(0.2)
        missing = [p for p in bmp_paths if not os.path.isfile(p)]
        if missing:
            raise RuntimeError(f"Missing {len(missing)} bmp files. Example: {missing[0]}")

        temp_to_delete = bmp_paths + [coords_path]

        coords_map = read_coords_table(coords_path)

        mosaic_ds, _min_x_full, _min_y_full, _tw_ds, _th_ds = stitch_mosaic_from_centers_ds(
            image_paths=bmp_paths,
            coords_map=coords_map,
            tile_w_full=self.cfg.width,
            tile_h_full=self.cfg.height,
            padding_full=self.cfg.stitch_padding_px,
            ds=self.cfg.ds,
            y_flip=self.cfg.y_flip,
            blend_mode=self.cfg.blend_mode,
        )

        detection = detect_l_marker_elbow_on_mosaic_ds(
            mosaic_ds=mosaic_ds,
            threshold=self.cfg.lmarker_threshold,
            morph_kernel_full=self.cfg.morph_kernel,
            min_area_full=self.cfg.min_contour_area,
            ds=self.cfg.ds,
            edge_margin_frac=self.cfg.edge_margin_frac,
            gap_bridge_full=self.cfg.gap_bridge_full,
        )

        if detection is None:
            return False, False, "", (-1, -1), coords_map, temp_to_delete
        elbow_ds, is_complete = detection

        annotated = annotate_point_color_ds(mosaic_ds, elbow_ds, ds=self.cfg.ds,
                                            label="PARTIAL (touches edge)" if not is_complete else None)

        suffix = "annotated" if is_complete else "annotated_PARTIAL"
        annotated_path = os.path.join(outputs_dir, f"{prefix_norm}_stitched_ds{self.cfg.ds}_{suffix}.png")
        cv2.imwrite(annotated_path, annotated)
        return True, is_complete, annotated_path, elbow_ds, coords_map, temp_to_delete

    def _do_one_acquisition(
        self,
        idx: int,
        expected: LMarkerPosition,
        x: float,
        y: float,
        z: float,
        outputs_dir: str,
        tag: str,
        move_note: str = "",
    ) -> Dict[str, Any]:
        """
        Move to the given ABSOLUTE (x, y, z) -- Z is never altered -- then run
        B2F + autofocus + acquire/stitch/detect once. Every stage move needs a
        fresh B2F + autofocus pass, so this repeats both on every call,
        whether it's a search candidate or a within-candidate recenter.
        """
        self._log(f"  move stage=({x:.3f},{y:.3f},{z:.3f}){move_note}")
        self.move_xy_then_z(x, y, z)
        ensure_beams_on_and_scan(self.conn)

        # B2F (once; if not 0 -> reset default -> once)
        rc_b2f, px, py, strat = (0, float("nan"), float("nan"), "off")
        if self.cfg.run_b2f:
            rc_b2f, px, py, strat = b2f_once_then_reset_default(
                self.conn,
                marker_name=expected.name,
                bound=self.cfg.b2f_projective_bound,
                verbose=self.cfg.verbose,
            )
            if self.cfg.verbose:
                ok = (rc_b2f == 0)
                print(f"    [B2F] marker={expected.name} ok={ok} strategy={strat} rc={rc_b2f} proj=({px:.3f},{py:.3f})")

        # AF (optional)
        if self.cfg.run_autofocus:
            _ = self.run_autofocus()
            time.sleep(self.cfg.post_af_sleep_s)

        # Auto brightness/contrast -- push the live image toward near-binary
        # black/white before the acquisition that will be thresholded for
        # L-marker detection (see run_auto_brightness_contrast docstring).
        _ = self.run_auto_brightness_contrast()

        # Acquire + DS stitch + detect + save ONLY annotated PNG
        ts = int(time.time() * 1000)
        safe_name = expected.name.replace(" ", "_") if expected.name else f"M{idx+1}"
        prefix = f"cal_{safe_name}_{idx+1}_{tag}_{ts}_"

        base = {"rc_b2f": rc_b2f, "px": px, "py": py, "strat": strat, "x": x, "y": y}
        temp: List[str] = []
        found, is_complete, annotated_path, elbow_ds, coords_map = False, False, "", (0, 0), {}
        acquire_error = ""
        try:
            self.configure_scan()
            found, is_complete, annotated_path, elbow_ds, coords_map, temp = self.acquire_stitch_detect_annotated_once(
                marker_name=safe_name,
                prefix=prefix,
                outputs_dir=outputs_dir,
            )
        except Exception as e:
            acquire_error = f"acquisition/stitch failed: {e}"
        finally:
            if not temp:
                # acquire_stitch_detect_annotated_once computes bmp_paths/coords_path
                # internally but raises (e.g. beam off -> acquisition never
                # completed) BEFORE returning them, so `temp` comes back empty even
                # though some/all of those files may actually have been written.
                # Recompute the expected paths independently so anything that DID
                # get written isn't silently abandoned in the flat remote folder.
                try:
                    _, fallback_coords, fallback_bmps = self._acquire_paths(prefix.rstrip("_"))
                    temp = [p for p in (fallback_bmps + [fallback_coords]) if os.path.isfile(p)]
                except Exception:
                    pass

            if self.cfg.keep_raw_images:
                raw_dir = self.save_raw_images(temp, outputs_dir, tag)
                if raw_dir and self.cfg.verbose:
                    self._log(f"    raw images saved: {raw_dir}")
            else:
                raw_dir = ""
                try:
                    self.cleanup_temp_files(temp)
                except Exception:
                    pass

        if acquire_error:
            return {**base, "found": False, "complete": False, "error": acquire_error, "raw_images_dir": raw_dir}
        if not found:
            return {**base, "found": False, "complete": False,
                    "error": "elbow NOT found on stitched mosaic", "raw_images_dir": raw_dir}

        return {**base, "found": True, "complete": is_complete, "error": "", "annotated_path": annotated_path,
                "elbow_ds": elbow_ds, "coords_map": coords_map, "raw_images_dir": raw_dir}

    def _elbow_offset_um(self, result: Dict[str, Any]) -> Tuple[float, float]:
        return elbow_offset_um_from_global_coords_ds(
            elbow_px_ds=result["elbow_ds"],
            coords_map=result["coords_map"],
            tile_w_full=self.cfg.width,
            tile_h_full=self.cfg.height,
            padding_full=self.cfg.stitch_padding_px,
            ds=self.cfg.ds,
            um_per_full_px=self.get_um_per_full_px(),
            y_flip=self.cfg.y_flip,
            debug=self.cfg.verbose,
        )

    def _attempt_find_at_offset(
        self,
        idx: int,
        expected: LMarkerPosition,
        dx: float,
        dy: float,
        outputs_dir: str,
        tag: str,
    ) -> Dict[str, Any]:
        """
        Try the candidate position (expected.x+dx, expected.y+dy, expected.z).

        If only a PARTIAL view of the marker is found (its contour touches
        the mosaic edge -- see detect_l_marker_elbow_on_mosaic_ds), the piece
        we did see is used to estimate where the true corner actually is, the
        stage is re-centered on that estimate, and a fresh 91-beam
        acquisition is taken (up to cfg.recenter_max_attempts times) until a
        complete, trustworthy corner is captured. If the budget runs out
        still only partial, this candidate is reported as not-found so the
        outer search moves on rather than accepting a low-confidence corner.
        """
        x, y, z = expected.x + dx, expected.y + dy, expected.z
        move_note = f" [search dx={dx:+.1f} dy={dy:+.1f} r={float(np.hypot(dx, dy)):.1f}um]" if (dx or dy) else ""

        result = self._do_one_acquisition(idx, expected, x, y, z, outputs_dir, tag, move_note)

        recenter_i = 0
        while result["found"] and not result["complete"] and recenter_i < self.cfg.recenter_max_attempts:
            recenter_i += 1
            dx_um, dy_um = self._elbow_offset_um(result)
            new_x, new_y = result["x"] + dx_um, result["y"] + dy_um
            self._log(f"    partial L-marker (contour touches mosaic edge) -- recentering by "
                      f"({dx_um:+.2f},{dy_um:+.2f}) um and re-acquiring "
                      f"[recenter {recenter_i}/{self.cfg.recenter_max_attempts}]...")
            result = self._do_one_acquisition(idx, expected, new_x, new_y, z, outputs_dir,
                                              f"{tag}_recenter{recenter_i}",
                                              move_note=f" [recenter {recenter_i}]")

        if result["found"] and not result["complete"]:
            self._log(f"    still only a partial view after {self.cfg.recenter_max_attempts} recenter "
                      f"attempt(s) -- treating as not-found for this position.")
            result = {**result, "found": False,
                      "error": f"only a partial (edge-touching) L-marker view after "
                               f"{self.cfg.recenter_max_attempts} recenter attempt(s)"}

        return result

    def find_one_marker(self, idx: int, expected: LMarkerPosition, outputs_dir: str) -> FoundMarker:
        self._log(f"\n[Marker {idx+1}] expected=({expected.x:.3f},{expected.y:.3f},{expected.z:.3f}) um name={expected.name}")

        # Candidate offsets: expected position first, then a constrained XY
        # search (nearest ring first) if the marker isn't there. Z is fixed.
        search_offsets = generate_spiral_search_offsets(
            self.cfg.search_radius_um, self.cfg.search_step_um, self.cfg.search_points_per_ring)
        offsets = [(0.0, 0.0)] + search_offsets

        result: Optional[Dict[str, Any]] = None
        for n, (dx, dy) in enumerate(offsets):
            if n == 1:
                self._log(f"  [WARN] L-marker '{expected.name}' not found at expected position -- "
                          f"starting constrained XY search (radius={self.cfg.search_radius_um:.0f} um, "
                          f"step={self.cfg.search_step_um:.0f} um, {len(search_offsets)} candidate points)")
            if n > 0:
                self._log(f"    search[{n}/{len(search_offsets)}] dx={dx:+.1f} dy={dy:+.1f} um "
                          f"(r={float(np.hypot(dx, dy)):.1f} um)")
            result = self._attempt_find_at_offset(idx, expected, dx, dy, outputs_dir,
                                                   tag=("chk" if n == 0 else f"srch{n:02d}"))
            if result["found"]:
                if n > 0:
                    self._log(f"    L-marker FOUND during search at dx={dx:+.1f} dy={dy:+.1f} um")
                break

        assert result is not None
        if not result["found"]:
            err = (f"L-marker '{expected.name}' NOT FOUND within {self.cfg.search_radius_um:.0f} um search "
                   f"radius of expected position ({expected.x:.3f},{expected.y:.3f}).")
            if self.cfg.keep_raw_images:
                err += f" Raw images for every attempted stage position are under: {outputs_dir}"
            print(f"[ERROR] {err}")
            return FoundMarker(
                idx=idx,
                expected=expected,
                actual=expected,
                stitched_annotated_path="",
                elbow_px_mosaic_ds=(0, 0),
                success=False,
                error=err,
                b2f_rc=result["rc_b2f"],
                projective_x=result["px"],
                projective_y=result["py"],
                b2f_strategy=result["strat"],
            )

        # Compute offset in um (DS-aware mapping), relative to whichever
        # position (expected, or expected+search offset, or a recentered
        # position) actually produced the accepted, complete detection.
        dx_um, dy_um = self._elbow_offset_um(result)

        actual = LMarkerPosition(
            x=result["x"] + dx_um,
            y=result["y"] + dy_um,
            z=expected.z,
            name=expected.name,
        )

        self._log(f"    FOUND: elbow_ds={result['elbow_ds']} offset_um=({dx_um:+.3f},{dy_um:+.3f}) ds={self.cfg.ds}")
        self._log(f"    output:\n      {result['annotated_path']}")

        return FoundMarker(
            idx=idx,
            expected=expected,
            actual=actual,
            stitched_annotated_path=result["annotated_path"],
            elbow_px_mosaic_ds=result["elbow_ds"],
            success=True,
            error="",
            b2f_rc=result["rc_b2f"],
            projective_x=result["px"],
            projective_y=result["py"],
            b2f_strategy=result["strat"],
        )

    def _write_timing_report(
        self,
        outputs_dir: str,
        run_start_wall: str,
        timings: List[Tuple[str, float, bool]],
        total_s: float,
    ) -> None:
        ts = time.strftime("%Y%m%d_%H%M%S")
        path = os.path.join(outputs_dir, f"timing_report_{ts}.txt")
        try:
            ensure_dir(outputs_dir)
            with open(path, "w", encoding="utf-8") as f:
                f.write("Shuttle-and-Find Calibration Timing Report\n")
                f.write(f"Started: {run_start_wall}\n\n")
                for label, dt, ok in timings:
                    mm = int(dt // 60)
                    ss = dt - mm * 60
                    f.write(f"{label}: {dt:.1f} s ({mm}m {ss:.1f}s) [{'SUCCESS' if ok else 'FAILED'}]\n")
                f.write("\n" + "-" * 40 + "\n")
                tm = int(total_s // 60)
                ts_ = total_s - tm * 60
                f.write(f"Total: {total_s:.1f} s ({tm}m {ts_:.1f}s)\n")
            self._log(f"    [Timing] report written: {path}")
        except Exception as e:
            self._log(f"    [Timing] WARNING: failed to write timing report: {e}")

    def _build_summary_figure(self, results: List[FoundMarker], outputs_dir: str) -> str:
        """
        One composite figure for a quick visual sanity check: a crop centered
        on the detected elbow (or a "NOT FOUND" placeholder) from each
        marker's final annotated mosaic, side by side. Best-effort -- returns
        "" (and just logs a warning) rather than raising, since this is a
        convenience, not something that should ever fail the actual run.
        """
        import cv2

        half = max(20, int(self.cfg.summary_crop_half_px))
        size = 2 * half
        panels = []
        for r in results:
            label = r.expected.name or f"M{r.idx + 1}"
            panel = None
            if r.success and r.stitched_annotated_path and os.path.isfile(r.stitched_annotated_path):
                img = cv2.imread(r.stitched_annotated_path, cv2.IMREAD_COLOR)
                if img is not None:
                    h, w = img.shape[:2]
                    ex, ey = r.elbow_px_mosaic_ds
                    x0, x1 = max(0, ex - half), min(w, ex + half)
                    y0, y1 = max(0, ey - half), min(h, ey + half)
                    crop = img[y0:y1, x0:x1]
                    pad_top, pad_bottom = max(0, half - (ey - y0)), max(0, half - (y1 - ey))
                    pad_left, pad_right = max(0, half - (ex - x0)), max(0, half - (x1 - ex))
                    panel = cv2.copyMakeBorder(crop, pad_top, pad_bottom, pad_left, pad_right,
                                               cv2.BORDER_CONSTANT, value=(255, 255, 255))
                    panel = cv2.resize(panel, (size, size))
                    cv2.putText(panel, label, (10, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.9, (0, 128, 0), 2)
            if panel is None:
                panel = np.full((size, size, 3), 235, dtype=np.uint8)
                cv2.putText(panel, f"{label}: NOT FOUND", (10, half), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 0, 255), 2)
            panels.append(panel)

        if not panels:
            return ""

        sep = np.zeros((size, 4, 3), dtype=np.uint8)
        row = panels[0]
        for p in panels[1:]:
            row = np.hstack([row, sep, p])

        ts = time.strftime("%Y%m%d_%H%M%S")
        path = os.path.join(outputs_dir, f"summary_all_markers_{ts}.png")
        try:
            ensure_dir(outputs_dir)
            cv2.imwrite(path, row)
            self._log(f"    [Summary] all-markers figure written: {path}")
        except Exception as e:
            self._log(f"    [Summary] WARNING: failed to write summary figure: {e}")
            return ""
        return path

    def run_three_markers(self, markers: List[LMarkerPosition], outputs_dir: str) -> Tuple[List[FoundMarker], Optional[Transform2D]]:
        # continuous scan on once (best effort)
        try:
            self.conn.execute("CMD_CONTINUOUS_SCAN_ON")
        except Exception:
            pass

        run_start = time.monotonic()
        run_start_wall = time.strftime("%Y-%m-%d %H:%M:%S")
        timings: List[Tuple[str, float, bool]] = []

        results: List[FoundMarker] = []
        try:
            for i, m in enumerate(markers):
                marker_out = os.path.join(outputs_dir, f"marker_{i+1}_{m.name or 'M'}")
                ensure_dir(marker_out)
                t0 = time.monotonic()
                r = self.find_one_marker(i, m, outputs_dir=marker_out)
                dt = time.monotonic() - t0
                label = f"Marker {i+1} ({m.name or 'M'})"
                timings.append((label, dt, r.success))
                self._log(f"    [Timing] {label}: {dt:.1f}s ({'OK' if r.success else 'FAILED'})")
                results.append(r)
                if not r.success:
                    try:
                        send_alert(
                            f"Calibration FAILED -- marker {m.name or f'M{i+1}'} not found",
                            "Shuttle-and-Find calibration could not find L-marker "
                            f"'{m.name or f'M{i+1}'}' (marker {i+1}/{len(markers)}).\n\n"
                            f"Error: {r.error}\n\n"
                            "This often means the L-marker is dirty/obscured and no corner "
                            "(\"knee\") could be detected. Check the annotated mosaics and raw "
                            f"per-beam images under: {marker_out}"
                        )
                    except Exception as e:
                        self._log(f"    [Alerts] WARNING: failed to send failure alert: {e}")
                    return results, None

            transform = compute_affine_from_3_points(
                old_pts=[(r.expected.x, r.expected.y) for r in results],
                new_pts=[(r.actual.x, r.actual.y) for r in results],
            )
            return results, transform
        finally:
            self._write_timing_report(outputs_dir, run_start_wall, timings, time.monotonic() - run_start)
            if results:
                self._build_summary_figure(results, outputs_dir)
            try:
                self.conn.execute("CMD_CONTINUOUS_SCAN_OFF")
            except Exception:
                pass


# =============================================================================
# CLI
# =============================================================================

def main():
    import argparse

    ap = argparse.ArgumentParser()

    ap.add_argument("--markers", default=None, help="markers_prev.json (optional)")
    ap.add_argument("--save_markers", default=None, help="save updated markers json (default overwrite input)")
    ap.add_argument("--remote_folder", default=r"V:\Fuming\msem_calib")
    ap.add_argument("--out_dir", default=r"V:\Fuming\msem_calib\outputs")

    ap.add_argument("--scanrate", type=int, default=1)
    ap.add_argument("--width", type=int, default=2000)
    ap.add_argument("--height", type=int, default=1748)
    ap.add_argument("--pixel_nm", type=float, default=8.0)
    ap.add_argument("--overlap_frac", type=float, default=0.05, help="e.g. 0.05 = 5%%; set <0 to disable")
    ap.add_argument("--hexagon_overlap", type=float, default=-1.0, help="set 0.05 to enable AP_HEXAGON_OVERLAP")

    ap.add_argument("--ds", type=int, default=8, help="downsample factor for stitch/detect (e.g. 4/8/16)")
    ap.add_argument("--padding", type=int, default=80, help="stitch padding in FULL-res pixels")
    ap.add_argument("--blend", type=str, default="min", choices=["min", "overwrite"])
    ap.add_argument("--y_flip", action="store_true")

    ap.add_argument("--threshold", type=int, default=70)
    ap.add_argument("--morph_kernel", type=int, default=11)
    ap.add_argument("--min_area", type=float, default=150000.0)

    ap.add_argument("--no_auto_bc", action="store_true",
                    help="skip CMD_AUTO_BRIGHTNESS_CONTRAST before each acquisition")
    ap.add_argument("--target_brightness", type=int, default=128,
                    help="DP_TARGET_BRIGHTNESS for CMD_AUTO_BRIGHTNESS_CONTRAST, range [0,255]")
    ap.add_argument("--target_contrast", type=int, default=230,
                    help="DP_TARGET_CONTRAST for CMD_AUTO_BRIGHTNESS_CONTRAST, range [0,255] -- "
                         "kept high so the L-marker image segments as near-binary black/white")

    ap.add_argument("--no_b2f", action="store_true")
    ap.add_argument("--no_af", action="store_true")
    ap.add_argument("--b2f_bound", type=float, default=100.0, help="clamp rule for reset default±bound (safety)")

    ap.add_argument("--search_radius_um", type=float, default=150.0,
                    help="if the L-marker isn't found at the expected XY, search this radius around it (Z fixed)")
    ap.add_argument("--search_step_um", type=float, default=50.0,
                    help="ring spacing for the constrained XY search")
    ap.add_argument("--search_points_per_ring", type=int, default=8,
                    help="candidate points per search ring")
    ap.add_argument("--recenter_max_attempts", type=int, default=2,
                    help="if only a partial (edge-touching) L-marker is seen, re-center on it and "
                         "re-acquire up to this many times before giving up on that position")
    ap.add_argument("--edge_margin_frac", type=float, default=0.25,
                    help="fraction of mosaic width/height, per side, treated as periphery -- a "
                         "detection there is incomplete (needs a recenter) even if not literally clipped")
    ap.add_argument("--gap_bridge_full", type=int, default=100,
                    help="full-res px: dilation used to merge a stitching-seam/threshold-gap-"
                         "fragmented L-marker into one region for bounding-box purposes")
    ap.add_argument("--summary_crop_half_px", type=int, default=250,
                    help="half-width/height (DS mosaic px) of each marker's crop in the "
                         "end-of-run all-markers summary figure")

    ap.add_argument("--no_keep_raw_images", action="store_true",
                    help="delete the 91 raw per-beam bmps after stitching instead of keeping them "
                         "in a per-stage-position subfolder under out_dir")

    ap.add_argument("--affine_out", default=None,
                    help="Write affine json (new = A*old + t). Default: out_dir/affine_reload_YYYYmmdd_HHMMSS.json")
    ap.add_argument("--proj_cache_out", default=None,
                    help="Write projective cache json. Default: out_dir/proj_cache.json")

    # Per-marker default projective setpoints (used ONLY for the B2F reset when
    # rc!=0). Optional overrides of the hardcoded DEFAULT_PROJ so they can be
    # tuned from the GUI without editing this file.
    ap.add_argument("--proj_m1", type=float, nargs=2, default=None, metavar=("PX", "PY"))
    ap.add_argument("--proj_m2", type=float, nargs=2, default=None, metavar=("PX", "PY"))
    ap.add_argument("--proj_m3", type=float, nargs=2, default=None, metavar=("PX", "PY"))

    args = ap.parse_args()

    # apply projective overrides into the module-level DEFAULT_PROJ
    if args.proj_m1 is not None:
        DEFAULT_PROJ["M1"] = (float(args.proj_m1[0]), float(args.proj_m1[1]))
    if args.proj_m2 is not None:
        DEFAULT_PROJ["M2"] = (float(args.proj_m2[0]), float(args.proj_m2[1]))
    if args.proj_m3 is not None:
        DEFAULT_PROJ["M3"] = (float(args.proj_m3[0]), float(args.proj_m3[1]))
    print(f"[Init] DEFAULT_PROJ = {DEFAULT_PROJ}")

    ensure_dir(args.remote_folder)
    ensure_dir(args.out_dir)

    n_recovered = sweep_orphaned_raw_images(args.remote_folder, args.out_dir)
    if n_recovered:
        print(f"[Sweep] recovered {n_recovered} leftover raw image file(s) from a previous run "
              f"into per-position folders under {args.out_dir}")

    # markers json
    if args.markers is None:
        args.markers = os.path.join(args.out_dir, "markers_prev.json")
        if not os.path.exists(args.markers):
            with open(args.markers, "w", encoding="utf-8") as f:
                json.dump(DEFAULT_MARKERS_JSON_EXAMPLE, f, indent=2)
            print(f"[Init] Created default markers JSON at: {args.markers}")
        else:
            print(f"[Init] Using existing markers JSON: {args.markers}")

    markers = load_markers_from_json(args.markers)

    overlap_frac = None if args.overlap_frac < 0 else float(args.overlap_frac)
    hex_overlap = None if args.hexagon_overlap < 0 else float(args.hexagon_overlap)

    cfg = AutoCalibConfig(
        scanrate=args.scanrate,
        width=args.width,
        height=args.height,
        remote_image_folder=args.remote_folder,
        image_filetype="bmp",
        pixel_nm=float(args.pixel_nm),
        set_overlap_frac=overlap_frac,
        set_hexagon_overlap=hex_overlap,
        ds=int(args.ds),
        stitch_padding_px=int(args.padding),
        blend_mode=("min" if args.blend == "min" else "overwrite"),
        y_flip=bool(args.y_flip),
        lmarker_threshold=int(args.threshold),
        morph_kernel=int(args.morph_kernel),
        min_contour_area=float(args.min_area),
        run_auto_bc=(not args.no_auto_bc),
        target_brightness=int(args.target_brightness),
        target_contrast=int(args.target_contrast),
        run_b2f=(not args.no_b2f),
        run_autofocus=(not args.no_af),
        b2f_projective_bound=float(args.b2f_bound),
        search_radius_um=float(args.search_radius_um),
        search_step_um=float(args.search_step_um),
        search_points_per_ring=int(args.search_points_per_ring),
        recenter_max_attempts=int(args.recenter_max_attempts),
        edge_margin_frac=float(args.edge_margin_frac),
        gap_bridge_full=int(args.gap_bridge_full),
        summary_crop_half_px=int(args.summary_crop_half_px),
        keep_raw_images=(not args.no_keep_raw_images),
        verbose=True,
    )

    conn = MSEMConnection()
    conn.connect_and_initialize()

    print("[Stage] Current:",
          conn.get_double("AP_STAGE_AT_X"),
          conn.get_double("AP_STAGE_AT_Y"),
          conn.get_double("AP_STAGE_AT_Z"))

    calib = AutoStageCalibrator(conn, cfg)
    results, transform = calib.run_three_markers(markers, outputs_dir=args.out_dir)

    print("\n========== RESULTS ==========")
    for r in results:
        if r.success:
            print(f"Marker{r.idx+1} OK name={r.expected.name} expected=({r.expected.x:.3f},{r.expected.y:.3f}) "
                  f"actual=({r.actual.x:.3f},{r.actual.y:.3f}) elbow_ds={r.elbow_px_mosaic_ds} "
                  f"B2F(rc={r.b2f_rc}, strat={r.b2f_strategy}, proj=({r.projective_x:.3f},{r.projective_y:.3f}))")
            print(f"  annotated: {r.stitched_annotated_path}")
        else:
            print(f"Marker{r.idx+1} FAIL name={r.expected.name}: {r.error}")

    # save projective cache
    proj_cache_path = args.proj_cache_out or os.path.join(args.out_dir, "proj_cache.json")
    proj_payload = {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "defaults_used_for_reset": DEFAULT_PROJ,
        "b2f_bound": cfg.b2f_projective_bound,
        "markers": [
            {
                "name": r.expected.name,
                "expected_xy_um": [r.expected.x, r.expected.y],
                "b2f_rc": int(r.b2f_rc),
                "b2f_strategy": r.b2f_strategy,
                "projective_xy": [float(r.projective_x), float(r.projective_y)],
            } for r in results
        ],
    }
    try:
        with open(proj_cache_path, "w", encoding="utf-8") as f:
            json.dump(proj_payload, f, indent=2)
        print(f"[Saved] Projective cache: {proj_cache_path}")
    except Exception as e:
        print(f"[WARN] Failed saving projective cache: {e}")

    if transform is None:
        print("\n[Transform] Not computed (marker failure -> aborted).")
        return

    print("\n[Transform] Affine (new = A*old + t)")
    print(f"  A = [[{transform.a11:.8f}, {transform.a12:.8f}], [{transform.a21:.8f}, {transform.a22:.8f}]]")
    print(f"  t = ({transform.tx:.3f}, {transform.ty:.3f}) um")

    affine_path = args.affine_out
    if affine_path is None:
        ts = time.strftime("%Y%m%d_%H%M%S")
        affine_path = os.path.join(args.out_dir, f"affine_reload_{ts}.json")

    save_affine_json(
        transform,
        affine_path,
        extra={
            "from_frame": "prev_round_stage_xy",
            "to_frame": "this_reload_stage_xy",
            "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "markers_in": os.path.abspath(args.markers),
            "ds": cfg.ds,
            "threshold": cfg.lmarker_threshold,
            "morph_kernel": cfg.morph_kernel,
            "min_area": cfg.min_contour_area,
            "b2f_policy": "once_ok_else_reset_default_once",
            "b2f_default_projectives": DEFAULT_PROJ,
            "b2f_bound": cfg.b2f_projective_bound,
        },
    )
    print(f"[Saved] Affine written to: {affine_path}")

    # optionally update markers json (actual)
    out_path = args.save_markers or args.markers
    updated = [r.actual for r in results if r.success]
    if len(updated) == 3:
        save_markers_to_json(updated, out_path)
        print(f"[Saved] Updated markers written to: {out_path}")


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception as e:
        try:
            send_alert("Calibration CRASHED",
                       f"auto_stage_calib.py raised an unhandled exception:\n\n{e}\n\n"
                       "See the console/log output for the full traceback.")
        except Exception:
            pass
        raise