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
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple, Callable

import numpy as np
from msem import MSEMConnection


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

    # Workflow
    run_b2f: bool = True
    run_autofocus: bool = True
    autofocus_hook: Optional[Callable[[], None]] = None
    post_af_sleep_s: float = 0.10

    # B2F reset defaults
    b2f_projective_bound: float = 100.0

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

def detect_l_marker_elbow_on_mosaic_ds(
    mosaic_ds: np.ndarray,
    threshold: int,
    morph_kernel_full: int,
    min_area_full: float,
    ds: int,
) -> Optional[Tuple[int, int]]:
    import cv2

    # scale params for DS
    k = max(3, int(round(morph_kernel_full / ds)))
    min_area_ds = float(min_area_full) / float(ds * ds)

    _, binary = cv2.threshold(mosaic_ds, int(threshold), 255, cv2.THRESH_BINARY_INV)

    kernel = np.ones((k, k), np.uint8)
    binary = cv2.morphologyEx(binary, cv2.MORPH_CLOSE, kernel)
    binary = cv2.morphologyEx(binary, cv2.MORPH_OPEN, kernel)

    contours, _ = cv2.findContours(binary, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not contours:
        return None

    contours = sorted(contours, key=cv2.contourArea, reverse=True)

    best = None
    for c in contours[:10]:
        if float(cv2.contourArea(c)) >= min_area_ds:
            best = c
            break
    if best is None:
        return None

    # Try convexity defects (often finds the inner "elbow")
    hull = cv2.convexHull(best, returnPoints=False)
    corner = None
    try:
        defects = cv2.convexityDefects(best, hull)
    except cv2.error:
        defects = None

    if defects is not None and len(defects) > 0:
        max_depth = 0.0
        far_point = None
        for i in range(defects.shape[0]):
            _s, _e, f, d = defects[i, 0]
            depth = d / 256.0
            if depth > max_depth:
                max_depth = depth
                far_point = tuple(best[f][0])
        if far_point is not None and max_depth > 3:
            corner = (int(far_point[0]), int(far_point[1]))

    if corner is None:
        corners = cv2.goodFeaturesToTrack(binary, maxCorners=50, qualityLevel=0.02, minDistance=max(10, 80 // max(1, ds)))
        if corners is None or len(corners) == 0:
            return None
        x, y, bw, bh = cv2.boundingRect(best)
        box_center = (x + bw // 2, y + bh // 2)

        best_corner = None
        min_dist = float("inf")
        for c in corners:
            cx, cy = c.ravel()
            dist = (cx - box_center[0]) ** 2 + (cy - box_center[1]) ** 2
            if dist < min_dist:
                min_dist = dist
                best_corner = (int(cx), int(cy))
        corner = best_corner

    return corner


def annotate_point_color_ds(mosaic_gray_ds: np.ndarray, pt: Tuple[int, int], ds: int) -> np.ndarray:
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
    return out


# =============================================================================
# Affine transform
# =============================================================================

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

    def acquire_stitch_detect_annotated_once(
        self,
        marker_name: str,
        prefix: str,
        outputs_dir: str,
    ) -> Tuple[bool, str, Tuple[int, int], Dict[str, Tuple[float, float]], List[str]]:
        """
        Acquire bundle, build DS mosaic, detect elbow, and SAVE ONLY ONE annotated PNG (if found).
        Returns:
          found, annotated_path, elbow_ds, coords_map, temp_to_delete
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

        elbow_ds = detect_l_marker_elbow_on_mosaic_ds(
            mosaic_ds=mosaic_ds,
            threshold=self.cfg.lmarker_threshold,
            morph_kernel_full=self.cfg.morph_kernel,
            min_area_full=self.cfg.min_contour_area,
            ds=self.cfg.ds,
        )

        if elbow_ds is None:
            return False, "", (-1, -1), coords_map, temp_to_delete

        annotated = annotate_point_color_ds(mosaic_ds, elbow_ds, ds=self.cfg.ds)

        annotated_path = os.path.join(outputs_dir, f"{prefix_norm}_stitched_ds{self.cfg.ds}_annotated.png")
        cv2.imwrite(annotated_path, annotated)
        return True, annotated_path, elbow_ds, coords_map, temp_to_delete

    def find_one_marker(self, idx: int, expected: LMarkerPosition, outputs_dir: str) -> FoundMarker:
        self._log(f"\n[Marker {idx+1}] expected=({expected.x:.3f},{expected.y:.3f},{expected.z:.3f}) um name={expected.name}")

        # Move
        self._log(f"  move stage=({expected.x:.3f},{expected.y:.3f},{expected.z:.3f})")
        self.move_xy_then_z(expected.x, expected.y, expected.z)
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

        # Acquire + DS stitch + detect + save ONLY annotated PNG
        ts = int(time.time() * 1000)
        safe_name = expected.name.replace(" ", "_") if expected.name else f"M{idx+1}"
        prefix = f"cal_{safe_name}_{idx+1}_{ts}_"

        temp: List[str] = []
        try:
            self.configure_scan()
            found, annotated_path, elbow_ds, coords_map, temp = self.acquire_stitch_detect_annotated_once(
                marker_name=safe_name,
                prefix=prefix,
                outputs_dir=outputs_dir,
            )
        except Exception as e:
            return FoundMarker(
                idx=idx,
                expected=expected,
                actual=expected,
                stitched_annotated_path="",
                elbow_px_mosaic_ds=(0, 0),
                success=False,
                error=f"acquisition/stitch failed: {e}",
                b2f_rc=rc_b2f,
                projective_x=px,
                projective_y=py,
                b2f_strategy=strat,
            )
        finally:
            try:
                self.cleanup_temp_files(temp)
            except Exception:
                pass

        if not found:
            return FoundMarker(
                idx=idx,
                expected=expected,
                actual=expected,
                stitched_annotated_path="",
                elbow_px_mosaic_ds=(0, 0),
                success=False,
                error="elbow NOT found on stitched mosaic",
                b2f_rc=rc_b2f,
                projective_x=px,
                projective_y=py,
                b2f_strategy=strat,
            )

        # Compute offset in um (DS-aware mapping)
        dx_um, dy_um = elbow_offset_um_from_global_coords_ds(
            elbow_px_ds=elbow_ds,
            coords_map=coords_map,
            tile_w_full=self.cfg.width,
            tile_h_full=self.cfg.height,
            padding_full=self.cfg.stitch_padding_px,
            ds=self.cfg.ds,
            um_per_full_px=self.get_um_per_full_px(),
            y_flip=self.cfg.y_flip,
            debug=self.cfg.verbose,
        )

        actual = LMarkerPosition(
            x=expected.x + dx_um,
            y=expected.y + dy_um,
            z=expected.z,
            name=expected.name,
        )

        self._log(f"    FOUND: elbow_ds={elbow_ds} offset_um=({dx_um:+.3f},{dy_um:+.3f}) ds={self.cfg.ds}")
        self._log(f"    output:\n      {annotated_path}")

        return FoundMarker(
            idx=idx,
            expected=expected,
            actual=actual,
            stitched_annotated_path=annotated_path,
            elbow_px_mosaic_ds=elbow_ds,
            success=True,
            error="",
            b2f_rc=rc_b2f,
            projective_x=px,
            projective_y=py,
            b2f_strategy=strat,
        )

    def run_three_markers(self, markers: List[LMarkerPosition], outputs_dir: str) -> Tuple[List[FoundMarker], Optional[Transform2D]]:
        # continuous scan on once (best effort)
        try:
            self.conn.execute("CMD_CONTINUOUS_SCAN_ON")
        except Exception:
            pass

        results: List[FoundMarker] = []
        try:
            for i, m in enumerate(markers):
                marker_out = os.path.join(outputs_dir, f"marker_{i+1}_{m.name or 'M'}")
                ensure_dir(marker_out)
                r = self.find_one_marker(i, m, outputs_dir=marker_out)
                results.append(r)
                if not r.success:
                    return results, None

            transform = compute_affine_from_3_points(
                old_pts=[(r.expected.x, r.expected.y) for r in results],
                new_pts=[(r.actual.x, r.actual.y) for r in results],
            )
            return results, transform
        finally:
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

    ap.add_argument("--no_b2f", action="store_true")
    ap.add_argument("--no_af", action="store_true")
    ap.add_argument("--b2f_bound", type=float, default=100.0, help="clamp rule for reset default±bound (safety)")

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
        run_b2f=(not args.no_b2f),
        run_autofocus=(not args.no_af),
        b2f_projective_bound=float(args.b2f_bound),
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
    main()