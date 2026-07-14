#!/usr/bin/env python3
"""
PROPAGATE_TILT_1PT — EXACT version (with robust acquisition scan settings)

Adds:
  - offset_method = AF_MINIMIZE_OL or DEEPFOCUS
  - DEEPFOCUS mode measures ONE support point, gets a new OL value,
    then computes delta_c exactly like the original 1-point offset pipeline.

Everything else (prev-plane fit, tile generation, route, acquisition chain)
remains the same.
"""

import sys, os, re, json, csv, time, math, glob, subprocess
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from typing import Optional, List, Tuple, Dict, Any
from statistics import mean, median

# ---------------------------------------------------------------------
# Project root import
# ---------------------------------------------------------------------
script_dir = os.path.dirname(os.path.abspath(__file__))
project_root = os.path.dirname(script_dir) if os.path.basename(script_dir) == "tests" else script_dir
if project_root not in sys.path:
    sys.path.insert(0, project_root)

from msem import MSEMConnection

# =============================================================================
# CONFIG
# =============================================================================

CONFIG: Dict[str, Any] = {
    "json_file": "w13_h25_slice1.json",
    "process_all_regions": False,
    "region_name": "S25R1",
    "region_name_filter": "R1",

    # prev slice log
    "prev_focusmap_by_region": {
        "S1R1": "w13_s1.txt",
        "S2R1": "w13_s2.txt",
        "S3R1": "w13_s3.txt",
        "S4R1": "w13_s4.txt",
        "S5R1": "w13_s5.txt",
        "S6R1": "w13_s6.txt",
        "S7R1": "w13_s7.txt",
        "S8R1": "w13_s8.txt",
        "S9R1": "w13_s9.txt",
        "S10R1": "w13_s10.txt",
        "S11R1": "w13_s11.txt",
        "S12R1": "w13_s12.txt",
        "S13R1": "w13_s13.txt",
        "S14R1": "w13_s14.txt",
        "S15R1": "w13_s15.txt",
        "S16R1": "w13_s16.txt",
        "S17R1": "w13_s17.txt",
        "S18R1": "w13_s18.txt",
        "S19R1": "w13_s19.txt",
        "S20R1": "w13_s20.txt",
        "S21R1": "w13_s21.txt",
        "S22R1": "w13_s22.txt",
        "S23R1": "w13_s23.txt",
        "S24R1": "w13_s24.txt",
        "S25R1": "w13_s25.txt",
        "S26R1": "w13_s26.txt",
        "S27R1": "w13_s27.txt",
    },

    # Alternative to the explicit dict above: point at a FOLDER of prev_focusmap
    # .txt files and let the software match each section by number (S25R1 -> a
    # file whose name contains 's25' as a token, e.g. w13_s25.txt). When set,
    # it's used for any region not found in prev_focusmap_by_region. Set the
    # dict to {} (or leave it) and just set this to use folder mode exclusively.
    "prev_focusmap_dir": None,

    # When process_all_regions + prev_focusmap_dir: a preflight resolves+validates
    # EVERY region's slice_<N>_FocusMap.txt up front (before touching the scope).
    #   False (default) -> abort the whole run if any region is missing its file
    #   True            -> warn + skip those regions, image the rest
    "preflight_skip_missing": False,


    # region ordering when process_all_regions=True
    "region_order_method": "SHORTEST_PATH",   # JSON / SHORTEST_PATH
    "region_order_start": "AUTO",             # AUTO / TOP_LEFT / TOP_RIGHT / BOTTOM_LEFT / BOTTOM_RIGHT / REGION_NAME
    "region_order_start_region_name": None,   # used only if region_order_start == REGION_NAME
    "region_coord_source": "CENTER_POSITION", # CENTER_POSITION / SUPPORTPOINT_CENTROID
    "region_path_two_opt": True,

    # BEST POINT SELECTION FROM prev_focusmap:
    #   "AF"     -> max autofocus confidence
    #   "AS_SUM" -> max (as_conf_x + as_conf_y)
    #   "AF_AS"  -> max (w_af*af + w_as*(asx+asy))
    "best_point_metric": "AF",
    "w_af": 1.0,
    "w_as": 0.1,

    # Manual override: set to an sp_id (int) from prev_focusmap to force that point
    # instead of auto-selecting the best. None = auto-select (default).
    "manual_sp_id": None,

    # gates for the ONE offset measurement (current slice)
    "offset_af_conf_min": 1e-9,
    "offset_dz_limit_um": 11.0,

    # -------------------------------------------------
    # Offset method
    #   "AF_MINIMIZE_OL" = original method
    #   "DEEPFOCUS"      = DeepFocus on ONE support point, then compute offset
    # -------------------------------------------------
    "offset_method": "AF_MINIMIZE_OL",
    # -------------------------------------------------
    # Plane update method
    #   "ONE_POINT_OFFSET"        = original current behavior
    #   "THREE_POINT_DELTA"       = measure a few current-slice points and fit delta plane on top of prev plane
    #   "CURRENT_ALL_POINTS_PLANE" = measure ALL current support points and fit a brand-new plane
    # -------------------------------------------------
    "plane_update_method": "ONE_POINT_OFFSET",


    # ONE_POINT_OFFSET robustness
    "one_point_offset_top_k": 5,                # measure this many top-AF candidates (so MAD has something to chew on)
    "one_point_offset_use_all_valid": False,
    "one_point_offset_min_valid": 2,            # raise if fewer than this many candidates passed _validate_focus_measurement
    "one_point_offset_min_inliers": 2,          # raise if fewer than this many points survive MAD filter
    "one_point_offset_mad_sigma": 2.5,          # |delta_c - median| > sigma * 1.4826 * MAD -> drop as outlier
    "one_point_offset_delta_spread_warn_uA": 80.0,  # now just a print-warning threshold, no longer raises
    "one_point_offset_save_all_candidates": True,

    # ZEN-compatible focus state protection
    "minimize_ol_only_first_point": True,
    "b2f_max_retries": 2,
    "b2f_hexfit_threshold": 100.0,
    "warmup_coarse_af_on_long_move": True,
    "warmup_distance_threshold_um": 5000.0,
    "restore_moves_stage_back": False,

    # Quadratic + residual IDW
    "residual_idw_power": 2.0,          # IDW power p
    "residual_idw_k": 5,                # use K nearest support points; 0 or None = use all
    "residual_clip_uA": 150.0,          # clip each residual to +/- this value
    "residual_idw_eps_um": 1e-3,        # avoid divide-by-zero

    # When plane_update_method == THREE_POINT_DELTA:
    # If provided, use these JSON support-point indices directly.
    # Otherwise auto-pick 3 well-spread points.
    "plane_update_sp_ids": [8,9,10],

    # minimum points required for delta plane fit
    "plane_update_min_points": 3,

    # plane-update tolerance:
    #   False (default) -> skip individual failed support points; raise only if
    #                      surviving valid points < plane_update_min_points
    #   True            -> original behavior: any single failure aborts the region
    "plane_update_strict": False,

    # DeepFocus paths
    "deepfocus_repo": r"D:\mSEMAPI\DeepFocus",
    "deepfocus_python_exe": r"C:\Users\Lichtman\miniconda3\envs\deepfocus\python.exe",
    "deepfocus_model_pts": r"D:\mSEMAPI\DeepFocus\data\StackedConv2D__21-07-27_16-21-40_normal_512.pts",
    "deepfocus_infer_script": r"D:\mSEMAPI\DeepFocus\infer_cli.py",
    "deepfocus_output_dir": r"V:\Fuming\DeepFocusOffset",

    # DeepFocus acquisition
    "deepfocus_beam_index": 1,
    "deepfocus_crop_edge": 512,
    "deepfocus_target_pert_um": 5.0,
    "deepfocus_n_iters": 2,

    # DeepFocus gains
    "deepfocus_focus_gain": 1.0,
    "deepfocus_stig_gain": 0.5,

    # DeepFocus safety
    "deepfocus_max_wd_sd_um": 2.0,
    "deepfocus_max_abs_dwd_um": 1.5,
    "deepfocus_max_abs_dol_uA": 200.0,
    "deepfocus_stig_clip": 0.10,

    # First version: use DeepFocus mainly for OL offset; keep global stig unchanged
    "deepfocus_apply_stig": False,

    # stage
    "stage_settling_time_ms": 50,
    "stage_speed_limit_pct": None,
    "move_z_to_json_supportpoint": False,

    # initial OL — what to set AP_OLENS_CURRENT to at the start of each region
    #   "PREV_PLANE_AT_FIRST_SP" (default) -> evaluate prev OL plane at sps[0] x,y;
    #                                          AF starts close to truth, way faster + more reliable
    #   "READ_CURRENT"                     -> don't reset; let it carry over from previous region
    #   "FIXED" / "DEFAULT"                -> use the hard-coded `initial_ol_uA` value below
    "initial_ol_mode": "PREV_PLANE_AT_FIRST_SP",
    "initial_ol_uA": 3000.0,        # only used when initial_ol_mode == "FIXED"

    # beam / scan readiness
    "beam_blanker_mode": 2,
    "ensure_beam_on": True,
    "ensure_continuous_scan": True,

    # beam-arc guard: real-time detection of "beam clearance failure" arcs from
    # the mSEM service log -> pause -> recover -> resume. See beam_arc_monitor.py.
    "beam_arc_enabled": True,
    "beam_arc_log_dir": r"C:\ProgramData\CarlZeiss\mSEM",
    "beam_arc_recovery_mode": "guided",   # "guided": operator runs Chamber reset EHT;
                                          # "auto": API recovery sequence (VALIDATE ON-SCOPE FIRST);
                                          # "flag_only": report but do not pause
    "beam_arc_min_outputs": 2,            # HV outputs tripped within ~1s to confirm an arc

    # reset AF/AS + beam restriction
    "reset_af_before_region": True,
    "reset_as_before_region": True,
    "restrict_af_beams": True,
    "restrict_as_beams": True,
    "beam_list_sparse_11": [1, 10, 19, 28, 37, 46, 55, 64, 73, 82, 91],

    # B2F policy
    "b2f_mode": "PER_FOCUS",  # ONCE / PER_FOCUS / PER_TILE / OFF

    # AF
    "af_enabled": True,
    "af_success_confidence_min": 1e-9,
    "af_fine_mode": 1,
    "af_fine_scan_rate": 4,
    "af_fine_support_points": 7,
    "af_fine_pixel_size_nm": 10.0,
    "af_fine_range_uA": 500,

    "af_coarse_mode": 2,
    "af_coarse_scan_rate": 4,
    "af_coarse_support_points": 20,
    "af_coarse_pixel_size_nm": 10.0,
    "af_coarse_range_uA": 10000,

    # minimize-OL
    "minimize_ol_enabled": True,
    "dz_limit_um": 11.0,
    "minimize_ol_max_iters": 6,
    "minimize_ol_try_both_signs": True,
    "minimize_ol_rerun_af_each_iter": True,

    # AS
    "as_enabled": True,
    "as_only_if_af_success": True,
    "as_scan_rate": 4,
    "as_support_points": 20,
    "as_pixel_size_nm": 10.0,
    "as_range": 10,

    # ROI tiles
    "roi_hex_overlap": 0.05,
    "use_region_contour_for_tiles": True,

    # route
    "route_source": "REBUILD",         # API_ORDER or REBUILD
    "route_mode": "SERPENTINE",          # used only when route_source == REBUILD
    "travel_direction": "VERTICAL_LEFT",
    "start_position": "TOP",
    "line_group_tol_um": 5.0,
    "vert_group_tol_um": 55.0,
    "vertical_column_slope_dx_per_um": 0.06,

    # backlash
    "backlash_enabled": False,
    "backlash_um": 20.0,
    "backlash_delay_s": 0.2,

    # output
    "output_root": r"V:\HI-MC\Pilot2\W15\slice0\API",
    "czi_name": "MyBlock",
    "region_idx": 1,
    "combo_format": "bmp",
    "prefix_base": "tile",

    # acquisition scan settings
    "acq_scan_speed": 4,
    "acq_pixel_size_nm": 8.0,
    "acq_width": 2000,
    "acq_height": 1748,

    "acq_scanrate_candidates": ["DP_SCANRATE", "DP_ACQUISITION_SCANRATE", "DP_IMAGE_SCANRATE", "DP_RECORD_SCANRATE"],
    "acq_pixelsize_candidates": ["AP_PIXELSIZE"],

    # Optional robustness knobs
    "force_averaging_1": True,
    "apply_scanparams_twice": True,

    "inner_sfoverlap_um": 1.0,
    "inner_overlap_param_candidates_x": ["AP_SFOV_OVERLAP_X_UM", "AP_SFOV_OVERLAP_X", "AP_INNER_OVERLAP_X"],
    "inner_overlap_param_candidates_y": ["AP_SFOV_OVERLAP_Y_UM", "AP_SFOV_OVERLAP_Y", "AP_INNER_OVERLAP_Y"],

    # Reuse a previous tile plan CSV instead of regenerating tiles + plane.
    "reuse_tile_plan_csv": None,

    # csv outputs
    "output_focus_points_csv": "focus_points_offset_measured.csv",
    "output_tiles_csv": "tiles.csv",
    "output_tile_plan_csv": "tile_plan_propagate_tilt_1pt.csv",

    # speed test
    "speed_test_max_tiles": 0,
    "speed_test_warmup_tiles": 10,

    # cmd _wait_stage_settled
    "wait_stage_settled": True,
    "no_wait_tiny_delay_ms": 200,

    # thumbnails
    "save_thumbnails": False,
    "thumbnail_scale": 0,

    # Speed experiments toggles
    "sanity_every_n_tiles": 0,
    "sanity_on_new_line": False,
    "use_wait_scan_complete": True,
    "use_wait_acq_complete": True,
    "defer_finish_write": True,
    "force_finish_every_n": 5,
    "pipeline_tiny_delay_ms": 0,
}


# =============================================================================
# External config override (GUI integration)
# =============================================================================
# When launched by the GUI (or any front-end), the env var MSEM_CONFIG_JSON
# points to a JSON file of the shape:
#     {"focus_map": { ...CONFIG overrides... },
#      "live_qc":   { ...LIVE_QC_CONFIG overrides (consumed by main_live_qc.py)... }}
# Only keys present in the JSON are overridden; everything else keeps the
# hardcoded defaults above. With no env var set, this is a no-op, so running
# `python focus_map.py` / `python main_live_qc.py` from a terminal is unchanged.

def apply_external_config_override(cfg: Dict[str, Any], section: str = "focus_map") -> Dict[str, Any]:
    """Merge overrides from $MSEM_CONFIG_JSON[<section>] into cfg, in place.
    Returns cfg. Safe + verbose: bad/missing file just prints and no-ops."""
    path = os.environ.get("MSEM_CONFIG_JSON", "").strip()
    if not path:
        return cfg
    if not os.path.isfile(path):
        print(f"[ExtConfig] MSEM_CONFIG_JSON set but file not found: {path} (using defaults)")
        return cfg
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as exc:
        print(f"[ExtConfig] failed to parse {path}: {exc} (using defaults)")
        return cfg
    overrides = data.get(section, {})
    if not isinstance(overrides, dict):
        print(f"[ExtConfig] {path}['{section}'] is not an object; ignoring")
        return cfg
    applied = []
    for k, v in overrides.items():
        old = cfg.get(k, "<unset>")
        cfg[k] = v
        applied.append(f"{k}: {old!r} -> {v!r}")
    if applied:
        print(f"[ExtConfig] applied {len(applied)} override(s) from {path} [{section}]:")
        for line in applied:
            print(f"    {line}")
    return cfg


# Apply focus_map overrides at import time so both `python focus_map.py` and
# `import focus_map` (from main_live_qc.py) pick them up.
apply_external_config_override(CONFIG, "focus_map")


# =============================================================================
# DATA
# =============================================================================

@dataclass
class SupportPoint:
    index: int
    x: float
    y: float
    z: float
    ol: float
    projective_x: float
    projective_y: float

@dataclass
class RegionInfo:
    name: str
    x: float
    y: float

@dataclass
class FocusMeasurement:
    sp_index: int
    x: float
    y: float
    z_stage: float
    ol_uA: float
    dz_um: float
    af_conf_fine: float
    af_conf_coarse: float
    af_success: bool
    minimize_ol_success: bool
    dz_after_minimize: float
    as_conf_x: float
    as_conf_y: float
    as_success: bool
    stig_x: float
    stig_y: float

@dataclass
class PrevPointCandidate:
    sp_id: int
    x: float
    y: float
    af: float
    asx: float
    asy: float
    score: float

@dataclass
class TilePlan:
    tile_idx: int
    x: float
    y: float
    z_pred: Optional[float]
    ol_pred: float
    stig_x_pred: float = 0.0
    stig_y_pred: float = 0.0

# =============================================================================
# Helpers
# =============================================================================

def _has_method(obj, name: str) -> bool:
    return hasattr(obj, name) and callable(getattr(obj, name))

def safe_get_last_error(conn: MSEMConnection) -> str:
    if _has_method(conn, "get_last_error"):
        try:
            return str(conn.get_last_error())
        except Exception:
            return ""
    if _has_method(conn, "get_string"):
        try:
            return str(conn.get_string("STR_LAST_ERROR"))
        except Exception:
            return ""
    return ""

def safe_set_string(conn: MSEMConnection, key: str, value: str) -> bool:
    if _has_method(conn, "set_string"):
        try:
            conn.set_string(key, value)
            return True
        except Exception:
            return False
    return False

def safe_set_double(conn: MSEMConnection, key: str, value: float) -> bool:
    try:
        conn.set_double(key, value)
        return True
    except Exception:
        return False

def safe_get_double(conn: MSEMConnection, key: str, default: float = float("nan")) -> float:
    try:
        return float(conn.get_double(key))
    except Exception:
        return default

def set_first_working_double(conn: MSEMConnection, candidates: List[str], value: float) -> Tuple[bool, Optional[str]]:
    for k in candidates:
        if safe_set_double(conn, k, value):
            return True, k
    return False, None

def beam_list_to_str(beams: List[int]) -> str:
    return ", ".join(str(b) for b in beams)

def is_success_conf(conf: Optional[float]) -> bool:
    return conf is not None and conf == conf and conf > CONFIG["af_success_confidence_min"]

def ensure_dir(path: str):
    os.makedirs(path, exist_ok=True)

# =============================================================================
# Region info loading - for imaging path planning
# =============================================================================

def load_all_region_infos(json_file: str) -> List[RegionInfo]:
    data = _read_imaging_json(json_file)

    infos: List[RegionInfo] = []
    coord_source = str(CONFIG.get("region_coord_source", "CENTER_POSITION")).upper()

    for region_str in data["Regions"]:
        root = ET.fromstring(region_str)
        name = root.get("Name")
        if not name:
            continue

        rx = ry = None

        if coord_source == "CENTER_POSITION":
            cp_node = root.find("CenterPosition")
            if cp_node is not None and cp_node.text:
                vals = [v.strip() for v in cp_node.text.split(",")]
                if len(vals) >= 2:
                    rx = float(vals[0])
                    ry = float(vals[1])

        elif coord_source == "SUPPORTPOINT_CENTROID":
            sp_root = root.find("SupportPoints")
            if sp_root is not None:
                sps = sp_root.findall("SupportPoint")
                xs, ys = [], []
                for sp in sps:
                    try:
                        xs.append(float(sp.find("X").text))
                        ys.append(float(sp.find("Y").text))
                    except Exception:
                        pass
                if xs and ys:
                    rx = sum(xs) / len(xs)
                    ry = sum(ys) / len(ys)

        else:
            raise RuntimeError(f"Unknown region_coord_source: {coord_source}")

        if rx is None or ry is None:
            # fallback: try support-point centroid
            sp_root = root.find("SupportPoints")
            if sp_root is not None:
                sps = sp_root.findall("SupportPoint")
                xs, ys = [], []
                for sp in sps:
                    try:
                        xs.append(float(sp.find("X").text))
                        ys.append(float(sp.find("Y").text))
                    except Exception:
                        pass
                if xs and ys:
                    rx = sum(xs) / len(xs)
                    ry = sum(ys) / len(ys)

        if rx is None or ry is None:
            raise RuntimeError(f"Cannot determine coordinates for region '{name}'")

        infos.append(RegionInfo(name=name, x=rx, y=ry))

    if not infos:
        raise RuntimeError(f"No regions found in JSON: {json_file}")

    return infos


def region_distance(a: RegionInfo, b: RegionInfo) -> float:
    return math.hypot(a.x - b.x, a.y - b.y)

def choose_region_start_index(regions: List[RegionInfo]) -> int:
    mode = str(CONFIG.get("region_order_start", "AUTO")).upper()

    if not regions:
        raise RuntimeError("No regions to choose start from.")

    if mode == "REGION_NAME":
        target = CONFIG.get("region_order_start_region_name", None)
        if not target:
            raise RuntimeError("region_order_start=REGION_NAME but region_order_start_region_name is None")
        for i, r in enumerate(regions):
            if r.name == target:
                return i
        raise RuntimeError(f"Start region '{target}' not found among {[r.name for r in regions]}")

    xs = [r.x for r in regions]
    ys = [r.y for r in regions]
    min_x, max_x = min(xs), max(xs)
    min_y, max_y = min(ys), max(ys)

    # score by distance to a corner
    def best_idx_for_corner(tx: float, ty: float) -> int:
        best_i = 0
        best_d = float("inf")
        for i, r in enumerate(regions):
            d = math.hypot(r.x - tx, r.y - ty)
            if d < best_d:
                best_d = d
                best_i = i
        return best_i

    if mode in ("AUTO", "TOP_LEFT"):
        return best_idx_for_corner(min_x, min_y)
    if mode == "TOP_RIGHT":
        return best_idx_for_corner(max_x, min_y)
    if mode == "BOTTOM_LEFT":
        return best_idx_for_corner(min_x, max_y)
    if mode == "BOTTOM_RIGHT":
        return best_idx_for_corner(max_x, max_y)

    raise RuntimeError(f"Unknown region_order_start: {mode}")

def build_region_order_nearest_neighbor(regions: List[RegionInfo]) -> List[int]:
    if not regions:
        return []

    n = len(regions)
    start_idx = choose_region_start_index(regions)

    unused = set(range(n))
    route = [start_idx]
    unused.remove(start_idx)

    while unused:
        cur = route[-1]
        nxt = min(unused, key=lambda j: region_distance(regions[cur], regions[j]))
        route.append(nxt)
        unused.remove(nxt)

    return route

def total_region_path_length(regions: List[RegionInfo], order: List[int]) -> float:
    if len(order) <= 1:
        return 0.0
    s = 0.0
    for i in range(len(order) - 1):
        s += region_distance(regions[order[i]], regions[order[i + 1]])
    return s

def optimize_region_order_two_opt(regions: List[RegionInfo], order: List[int]) -> List[int]:
    if len(order) < 4:
        return order[:]

    best = order[:]
    improved = True

    while improved:
        improved = False
        for i in range(1, len(best) - 2):
            for k in range(i + 1, len(best) - 1):
                a, b = best[i - 1], best[i]
                c, d = best[k], best[k + 1]

                old_cost = region_distance(regions[a], regions[b]) + region_distance(regions[c], regions[d])
                new_cost = region_distance(regions[a], regions[c]) + region_distance(regions[b], regions[d])

                if new_cost + 1e-9 < old_cost:
                    best[i:k + 1] = reversed(best[i:k + 1])
                    improved = True

    return best

def compute_region_processing_order(json_file: str) -> List[str]:
    method = str(CONFIG.get("region_order_method", "SHORTEST_PATH")).upper()

    regions = load_all_region_infos(json_file)

    if method == "JSON":
        names = [r.name for r in regions]
        print(f"[RegionOrder] JSON order: {names}")
        return names

    if method == "SHORTEST_PATH":
        order = build_region_order_nearest_neighbor(regions)

        if bool(CONFIG.get("region_path_two_opt", True)):
            order = optimize_region_order_two_opt(regions, order)

        ordered_regions = [regions[i] for i in order]
        names = [r.name for r in ordered_regions]
        path_len = total_region_path_length(regions, order)

        print("[RegionOrder] shortest-path order:")
        for i, r in enumerate(ordered_regions, start=1):
            print(f"  {i:02d}. {r.name} @ ({r.x:.3f}, {r.y:.3f})")
        print(f"[RegionOrder] total path length = {path_len:.3f} um")

        return names

    raise RuntimeError(f"Unknown region_order_method: {method}")


# =============================================================================
# Debug + strict pixel size setters
# =============================================================================

def debug_dump_acq_state(conn: MSEMConnection, tag: str = ""):
    keys = [
        "AP_PIXELSIZE",
        "DP_WIDTH", "DP_HEIGHT",
        "DP_SCANRATE",
        "DP_PREPARED_WIDTH", "DP_PREPARED_HEIGHT", "DP_PREPARED_SCANRATE",
        "DP_WIDTH_CP", "DP_HEIGHT_CP",
        "DP_LINE_AVERAGING", "DP_FRAME_AVERAGING",
        "DP_LINE_INTEGRATION", "DP_FRAME_INTEGRATION",
    ]
    print(f"[DebugAcq]{(' ' + tag) if tag else ''}")
    for k in keys:
        v = safe_get_double(conn, k, default=float("nan"))
        if v == v:
            print(f"   {k} = {v}")

def set_pixelsize_nm_strict(conn: MSEMConnection, px_nm: float, tol_nm: float = 1e-3) -> None:
    rc = None
    try:
        rc = conn.set_double("AP_PIXELSIZE", float(px_nm))
    except Exception as e:
        raise RuntimeError(f"Exception when setting AP_PIXELSIZE={px_nm}: {e} err={safe_get_last_error(conn)}")

    rb = safe_get_double(conn, "AP_PIXELSIZE", default=float("nan"))
    print(f"[PixelSize] set AP_PIXELSIZE={px_nm} nm -> rc={rc}, readback={rb}")

    if isinstance(rc, (int, float)) and rc != 0:
        raise RuntimeError(f"Failed to set AP_PIXELSIZE={px_nm} nm (rc={rc}) err={safe_get_last_error(conn)}")

    if not (rb == rb) or abs(rb - px_nm) > tol_nm:
        raise RuntimeError(f"AP_PIXELSIZE readback mismatch: want {px_nm}, got {rb} (rc={rc})")

# =============================================================================
# JSON parsing
# =============================================================================

def _parse_points_string(points_str: str) -> List[Tuple[float, float]]:
    pts = []
    for token in points_str.strip().split():
        if "," not in token:
            continue
        xs, ys = token.split(",", 1)
        try:
            pts.append((float(xs), float(ys)))
        except ValueError:
            pass
    if len(pts) >= 2 and (abs(pts[0][0] - pts[-1][0]) < 1e-6) and (abs(pts[0][1] - pts[-1][1]) < 1e-6):
        pts = pts[:-1]
    return pts

def _read_imaging_json(json_file: str) -> dict:
    """Robust JSON load for imaging / retake JSONs:

    * strips UTF-8 BOM (some Windows tools add it; ``json.load`` chokes
      on it as 'Expecting value: line 1 column 1 (char 0)')
    * empty / non-JSON files raise a clearer error than the bare
      ``JSONDecodeError`` which only mentions char 0
    """
    import os
    if not os.path.isfile(json_file):
        raise RuntimeError(f"JSON file not found: {json_file}")
    size = os.path.getsize(json_file)
    if size == 0:
        raise RuntimeError(
            f"JSON file is EMPTY (0 bytes): {json_file}\n"
            "  This usually means a previous save was interrupted. "
            "Re-export the retake JSON from the Live QC dashboard."
        )
    with open(json_file, "rb") as f:
        raw = f.read()
    # Strip UTF-8/16 BOMs.
    if raw.startswith(b"\xef\xbb\xbf"):
        raw = raw[3:]
    elif raw.startswith(b"\xff\xfe") or raw.startswith(b"\xfe\xff"):
        # UTF-16; decode then re-encode (rare on Windows but seen).
        text = raw.decode("utf-16")
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            head = text[:120].replace("\n", "\\n")
            raise RuntimeError(
                f"Could not parse JSON {json_file} (UTF-16): {exc}.\n"
                f"  size={size}B  head={head!r}"
            ) from exc
    try:
        return json.loads(raw.decode("utf-8"))
    except json.JSONDecodeError as exc:
        head = raw[:120].decode("utf-8", errors="replace").replace("\n", "\\n")
        raise RuntimeError(
            f"Could not parse JSON {json_file}: {exc}.\n"
            f"  size={size}B  head={head!r}\n"
            "  If the head shows '<?xml' or '<', this is XML, not JSON.\n"
            "  If the head is empty, the file is whitespace/blank.\n"
            "  Re-export the retake JSON from the Live QC dashboard."
        ) from exc


def load_all_region_names(json_file: str) -> List[str]:
    data = _read_imaging_json(json_file)

    names = []
    for region_str in data["Regions"]:
        root = ET.fromstring(region_str)
        name = root.get("Name")
        if name:
            names.append(name)

    if not names:
        raise RuntimeError(f"No regions found in JSON: {json_file}")
    return names

def _parse_support_points_from_xml(region_xml: str) -> List[SupportPoint]:
    """Pull all <SupportPoint> entries from a region XML string.
    Returns [] if the region has none / element missing."""
    out: List[SupportPoint] = []
    root = ET.fromstring(region_xml)
    sp_root = root.find("SupportPoints")
    if sp_root is None:
        return out
    for i, sp in enumerate(sp_root.findall("SupportPoint")):
        try:
            out.append(
                SupportPoint(
                    index=i,
                    x=float(sp.find("X").text),
                    y=float(sp.find("Y").text),
                    z=float(sp.find("Z").text),
                    ol=float(sp.find("OL").text),
                    projective_x=float(sp.find("ProjectiveX").text),
                    projective_y=float(sp.find("ProjectiveY").text),
                )
            )
        except (AttributeError, TypeError, ValueError):
            continue
    return out


def _parse_contour_from_xml(region_xml: str) -> List[Tuple[float, float]]:
    """Try Contour/Points first, then CenterPosition+ContourSize fallback."""
    root = ET.fromstring(region_xml)
    contour_pts: List[Tuple[float, float]] = []
    contour = root.find("Contour")
    if contour is not None:
        pts_node = contour.find("Points")
        if pts_node is not None and pts_node.text:
            contour_pts = _parse_points_string(pts_node.text)
    if not contour_pts:
        cp_node = root.find("CenterPosition")
        cs_node = root.find("ContourSize")
        if cp_node is not None and cp_node.text and cs_node is not None and cs_node.text:
            cx, cy = [float(v) for v in cp_node.text.split(",")]
            sw, sh = [float(v) for v in cs_node.text.split(",")]
            hw, hh = sw / 2.0, sh / 2.0
            contour_pts = [(cx - hw, cy - hh), (cx + hw, cy - hh),
                           (cx + hw, cy + hh), (cx - hw, cy + hh)]
    return contour_pts


def _region_center_xy(region_xml: str,
                      contour_pts: List[Tuple[float, float]]
                      ) -> Optional[Tuple[float, float]]:
    """Best-effort centroid of a region.  Prefer ``<CenterPosition>``,
    fall back to the centroid of the contour vertices."""
    try:
        root = ET.fromstring(region_xml)
        cp_node = root.find("CenterPosition")
        if cp_node is not None and cp_node.text:
            vals = [v.strip() for v in cp_node.text.split(",")]
            if len(vals) >= 2:
                return float(vals[0]), float(vals[1])
    except Exception:
        pass
    if contour_pts:
        cx = sum(p[0] for p in contour_pts) / len(contour_pts)
        cy = sum(p[1] for p in contour_pts) / len(contour_pts)
        return cx, cy
    return None


def _average_sp_z_in_json(data: Dict[str, Any]) -> Optional[float]:
    """Average Z of all SupportPoints across every region in the JSON.
    Used as a sane fallback Z for synthesized SPs when the requested
    region itself has no SPs.  Returns None if the JSON has none."""
    zs: List[float] = []
    for region_str in data.get("Regions", []):
        try:
            root = ET.fromstring(region_str)
        except Exception:
            continue
        sp_root = root.find("SupportPoints")
        if sp_root is None:
            continue
        for sp in sp_root.findall("SupportPoint"):
            try:
                zs.append(float(sp.find("Z").text))
            except (AttributeError, TypeError, ValueError):
                continue
    return (sum(zs) / len(zs)) if zs else None


def load_region_and_support_points(json_file: str, region_name: str) -> Tuple[List[SupportPoint], List[Tuple[float, float]]]:
    """Load (SupportPoints, contour_pts) for ``region_name``.

    When the requested region exists but has no SupportPoints (common
    for dashboard-saved retake regions), synthesize a single SP at
    the region's center (``CenterPosition`` if present, else the
    contour centroid).  This is a deliberately minimal "one-point
    offset" anchor — its only role downstream is to be the stage XY
    target where ``measure_one_offset_point`` re-measures focus.  The
    delta against the previous plane (loaded from
    ``prev_focusmap_by_region``) is computed against the measured OL,
    so a single synthetic point per retake sub-region is exactly
    enough to re-anchor the plane for that small area.

    Z for the synthetic SP defaults to the JSON-wide average Z when
    available, else 0; either is harmless because Z is only used when
    the (off-by-default) ``move_z_to_json_supportpoint`` config is on.
    """
    data = _read_imaging_json(json_file)

    by_name: Dict[str, str] = {}
    sp_count: Dict[str, int] = {}
    for region_str in data.get("Regions", []):
        try:
            root = ET.fromstring(region_str)
        except Exception:
            continue
        nm = root.get("Name")
        if not nm:
            continue
        by_name[nm] = region_str
        sp_root = root.find("SupportPoints")
        sp_count[nm] = (0 if sp_root is None
                        else len(sp_root.findall("SupportPoint")))

    if region_name not in by_name:
        raise RuntimeError(
            f"Region '{region_name}' not found in {json_file}.\n"
            f"  Region names in JSON (with SP count): "
            f"{ {n: sp_count[n] for n in sorted(by_name)} }"
        )

    primary_xml = by_name[region_name]
    contour_pts = _parse_contour_from_xml(primary_xml)
    support_points = _parse_support_points_from_xml(primary_xml)

    if not support_points:
        center = _region_center_xy(primary_xml, contour_pts)
        if center is None:
            raise RuntimeError(
                f"Region '{region_name}' has no SupportPoints in {json_file} "
                f"and no <CenterPosition>/contour available to synthesize one.\n"
                f"  Region names in JSON (with SP count): "
                f"{ {n: sp_count[n] for n in sorted(by_name)} }"
            )
        cx, cy = center
        z_default = _average_sp_z_in_json(data)
        synth_z = float(z_default) if z_default is not None else 0.0
        support_points = [
            SupportPoint(
                index=0, x=float(cx), y=float(cy),
                z=synth_z, ol=0.0,
                projective_x=0.0, projective_y=0.0,
            )
        ]
        z_note = (f"z={synth_z:.3f} (JSON avg)" if z_default is not None
                  else "z=0 (no JSON-wide Z available)")
        print(
            f"[JSON-synth] Region '{region_name}' has no SupportPoints — "
            f"placed 1 synthetic SP at region center "
            f"({cx:.3f}, {cy:.3f}) {z_note}.  This is the "
            f"one-point-offset anchor for re-measuring focus against "
            f"the previous plane."
        )

    return support_points, contour_pts

# =============================================================================
# ROI tiles
# =============================================================================

def get_roi_tile_positions(conn: MSEMConnection, contour_pts: List[Tuple[float, float]]) -> List[Tuple[float, float]]:
    if not contour_pts:
        raise RuntimeError("No contour points found. Cannot generate ROI tiles.")

    ok = safe_set_double(conn, "AP_HEXAGON_OVERLAP", float(CONFIG["roi_hex_overlap"]))
    print(f"[ROI] Set AP_HEXAGON_OVERLAP={CONFIG['roi_hex_overlap']} -> {ok}")

    rc = conn.execute("CMD_CLEAR_ROI_VERTICES")
    if rc != 0:
        raise RuntimeError(f"CMD_CLEAR_ROI_VERTICES failed rc={rc} err={safe_get_last_error(conn)}")

    for (x, y) in contour_pts:
        conn.set_double("AP_ROI_VERTEX_X", x)
        conn.set_double("AP_ROI_VERTEX_Y", y)

    rc = conn.execute("CMD_CALCULATE_ROI_VERTICES")
    if rc != 0:
        raise RuntimeError(f"CMD_CALCULATE_ROI_VERTICES failed rc={rc} err={safe_get_last_error(conn)}")

    n = int(conn.get_double("AP_ROI_NUMBEROFPOINTS"))
    if n <= 0:
        raise RuntimeError("AP_ROI_NUMBEROFPOINTS returned <=0")

    if _has_method(conn, "get_array"):
        try:
            xs = conn.get_array("AP_ROI_XPOSITIONS")
            ys = conn.get_array("AP_ROI_YPOSITIONS")

            if isinstance(xs, (list, tuple)) and isinstance(ys, (list, tuple)):
                if len(xs) == 1 and isinstance(xs[0], (list, tuple)):
                    xs = xs[0]
                if len(ys) == 1 and isinstance(ys[0], (list, tuple)):
                    ys = ys[0]
                if len(xs) == len(ys) and len(xs) > 0 and not isinstance(xs[0], (list, tuple)):
                    tiles = [(float(x), float(y)) for x, y in zip(xs, ys)]
                    print(f"[ROI] Read via get_array(): {len(tiles)} tiles")
                    return tiles

            print(f"[ROI] get_array returned unexpected shapes: type(xs)={type(xs)}, type(ys)={type(ys)} len(xs)={getattr(xs, '__len__', lambda: None)()} len(ys)={getattr(ys, '__len__', lambda: None)()}")
        except Exception as e:
            print(f"[ROI] get_array failed -> fallback: {e}")

        if _has_method(conn, "get_double_from_list"):
            xs = [conn.get_double_from_list("AP_ROI_XPOSITIONS", i) for i in range(n)]
            ys = [conn.get_double_from_list("AP_ROI_YPOSITIONS", i) for i in range(n)]
            tiles = list(zip(xs, ys))
            print(f"[ROI] Read via get_double_from_list(): {len(tiles)}")
            return tiles

    raise RuntimeError("Cannot read ROI arrays; need get_double_from_list or get_array wrapper.")

# =============================================================================
# Beam / reset
# =============================================================================

def ensure_beam_and_scan(conn: MSEMConnection):
    conn.set_double("DP_BEAMBLANKER_MODE", CONFIG["beam_blanker_mode"])
    print(f"[Beam] DP_BEAMBLANKER_MODE={CONFIG['beam_blanker_mode']}")

    if CONFIG["ensure_beam_on"]:
        rc1 = conn.execute("CMD_BEAM_ON")
        rc2 = conn.execute("CMD_WAIT_BEAM_ON")
        is_on = safe_get_double(conn, "DP_IS_BEAM_ON", default=0.0)
        print(f"[Beam] CMD_BEAM_ON rc={rc1}, CMD_WAIT_BEAM_ON rc={rc2}, DP_IS_BEAM_ON={is_on}")
        if is_on < 0.5:
            raise RuntimeError("Beam is OFF after CMD_BEAM_ON/CMD_WAIT_BEAM_ON")

    if CONFIG["ensure_continuous_scan"]:
        rc = conn.execute("CMD_CONTINUOUS_SCAN_ON")
        is_scanning = safe_get_double(conn, "DP_IS_SCANNING", default=0.0)
        print(f"[Scan] CMD_CONTINUOUS_SCAN_ON rc={rc}, DP_IS_SCANNING={is_scanning}")

def reset_af_as_and_set_beams(conn: MSEMConnection):
    if CONFIG["reset_af_before_region"]:
        conn.execute("CMD_RESET_AUTOFOCUS")
    if CONFIG["reset_as_before_region"]:
        conn.execute("CMD_RESET_AUTOSTIG")

    if CONFIG["restrict_af_beams"]:
        ok = safe_set_string(conn, "STR_AUTOFOCUS_BEAMS", beam_list_to_str(CONFIG["beam_list_sparse_11"]))
        print(f"[AF] STR_AUTOFOCUS_BEAMS set -> {ok}")

    if CONFIG["restrict_as_beams"]:
        ok = safe_set_string(conn, "STR_AUTOSTIG_BEAMS", beam_list_to_str(CONFIG["beam_list_sparse_11"]))
        print(f"[AS] STR_AUTOSTIG_BEAMS set -> {ok}")

# =============================================================================
# Stage + AF/AS core ops
# =============================================================================

def stage_move_xy_z(conn: MSEMConnection, x: float, y: float, z: Optional[float]):
    conn.set_double("AP_STAGE_SETTLING_TIME", CONFIG["stage_settling_time_ms"])
    conn.set_double("AP_STAGE_GOTO_X", x)
    conn.set_double("AP_STAGE_GOTO_Y", y)
    if z is not None:
        conn.set_double("AP_STAGE_GOTO_Z", z)

    if CONFIG.get("wait_stage_settled", True):
        rc = conn.execute("CMD_WAIT_STAGE_SETTLED")
        if rc != 0:
            raise RuntimeError(f"CMD_WAIT_STAGE_SETTLED rc={rc} err={safe_get_last_error(conn)}")
    else:
        tiny_ms = int(CONFIG.get("no_wait_tiny_delay_ms", 0) or 0)
        if tiny_ms > 0:
            time.sleep(tiny_ms / 1000.0)

def run_b2f(conn: MSEMConnection) -> Tuple[int, float, float]:
    rc = conn.execute("CMD_BEAMS_TO_FIBERS")
    proj_x = safe_get_double(conn, "AP_PROJECTIVE_DEFLECTOR_X")
    proj_y = safe_get_double(conn, "AP_PROJECTIVE_DEFLECTOR_Y")
    return rc, proj_x, proj_y

def run_b2f_robust(conn: MSEMConnection) -> Tuple[bool, int, float, float]:
    max_retries = int(CONFIG.get("b2f_max_retries", 2))
    hexfit_thr = float(CONFIG.get("b2f_hexfit_threshold", 100.0))

    rc, px, py = 0, float("nan"), float("nan")
    for attempt in range(1, max_retries + 1):
        rc, px, py = run_b2f(conn)
        if rc != 0:
            print(f"  [B2F] attempt {attempt}/{max_retries} rc={rc} (failed)")
            continue

        hexfit = safe_get_double(conn, "AP_HEXAGON_FIT", default=float("nan"))
        if hexfit != hexfit:
            print(f"  [B2F] attempt {attempt}/{max_retries} rc=0 projective=({px:.3f},{py:.3f}) hexfit=N/A (ok)")
            return True, rc, px, py

        if hexfit < hexfit_thr:
            print(f"  [B2F] attempt {attempt}/{max_retries} rc=0 hexfit={hexfit:.1f} (ok)")
            return True, rc, px, py

        print(f"  [B2F] attempt {attempt}/{max_retries} rc=0 hexfit={hexfit:.1f} > {hexfit_thr:.1f} (retry)")

    print(f"  [B2F] all {max_retries} attempts exhausted, rc={rc}")
    return rc == 0, rc, px, py

def run_af_once(conn: MSEMConnection, mode: int, scan_rate: int, support_points: int, pixel_nm: float, rng_uA: float):
    conn.set_double("DP_AUTOFOCUS_MODE", mode)
    conn.set_double("DP_AUTOFOCUS_SCANRATE", scan_rate)
    conn.set_double("DP_AUTOFOCUS_SUPPORTPOINTS", support_points)
    conn.set_double("AP_AUTOFOCUS_PIXELSIZE", pixel_nm)
    conn.set_double("AP_AUTOFOCUS_RANGE", rng_uA)
    rc = conn.execute("CMD_AUTOFOCUS")
    conf = safe_get_double(conn, "AP_AUTOFOCUS_CONFIDENCE", default=float("nan"))
    return rc, conf

def run_af_fine_then_coarse(conn: MSEMConnection) -> Dict[str, Any]:
    ol_before = safe_get_double(conn, "AP_OLENS_CURRENT")

    rc_f, conf_f = run_af_once(
        conn,
        CONFIG["af_fine_mode"],
        CONFIG["af_fine_scan_rate"],
        CONFIG["af_fine_support_points"],
        CONFIG["af_fine_pixel_size_nm"],
        CONFIG["af_fine_range_uA"],
    )

    rc_c, conf_c = None, None
    if not is_success_conf(conf_f):
        rc_c, conf_c = run_af_once(
            conn,
            CONFIG["af_coarse_mode"],
            CONFIG["af_coarse_scan_rate"],
            CONFIG["af_coarse_support_points"],
            CONFIG["af_coarse_pixel_size_nm"],
            CONFIG["af_coarse_range_uA"],
        )

    ol_after = safe_get_double(conn, "AP_OLENS_CURRENT")
    dz_um = safe_get_double(conn, "AP_DZ_FROM_OLENS", default=float("nan"))
    ok = is_success_conf(conf_f) or is_success_conf(conf_c)

    return {
        "ol_before": ol_before,
        "ol_after": ol_after,
        "dz_um": dz_um,
        "rc_fine": rc_f,
        "conf_fine": conf_f,
        "rc_coarse": rc_c,
        "conf_coarse": conf_c,
        "success": ok,
    }

def run_as(conn: MSEMConnection) -> Tuple[int, float, float, bool]:
    conn.set_double("DP_AUTOSTIG_SCANRATE", CONFIG["as_scan_rate"])
    conn.set_double("DP_AUTOSTIG_SUPPORTPOINTS", CONFIG["as_support_points"])
    conn.set_double("AP_AUTOSTIG_PIXELSIZE", CONFIG["as_pixel_size_nm"])
    conn.set_double("AP_AUTOSTIG_RANGE", CONFIG["as_range"])
    rc = conn.execute("CMD_AUTOSTIG")
    conf_x = safe_get_double(conn, "AP_AUTOSTIG_CONFIDENCE_X", default=float("nan"))
    conf_y = safe_get_double(conn, "AP_AUTOSTIG_CONFIDENCE_Y", default=float("nan"))
    ok = (conf_x > 0) and (conf_y > 0)
    return rc, conf_x, conf_y, ok

def run_warmup_coarse_af_as(conn: MSEMConnection):
    """ZEN-style warmup: coarse AF + AS after a long stage move."""
    af_px = float(CONFIG.get("af_fine_pixel_size_nm", 10.0))
    warmup_px = min(2.0 * af_px, 12.0)

    print("[Warmup] coarse AF (range=15000, sp=20) + AS (range=10, sp=17)")
    run_af_once(
        conn,
        mode=int(CONFIG["af_coarse_mode"]),
        scan_rate=int(CONFIG["af_fine_scan_rate"]),
        support_points=20,
        pixel_nm=warmup_px,
        rng_uA=15000,
    )
    conn.set_double("DP_AUTOSTIG_SCANRATE", int(CONFIG["as_scan_rate"]))
    conn.set_double("DP_AUTOSTIG_SUPPORTPOINTS", 17)
    conn.set_double("AP_AUTOSTIG_PIXELSIZE", warmup_px)
    conn.set_double("AP_AUTOSTIG_RANGE", 10)
    conn.execute("CMD_AUTOSTIG")
    print("[Warmup] done")

# =============================================================================
# Minimize-OL
# =============================================================================

def minimize_ol_lens_zen_style(conn: MSEMConnection) -> Tuple[bool, float]:
    dz_limit = float(CONFIG["dz_limit_um"])
    max_iters = int(CONFIG["minimize_ol_max_iters"])

    for _iter in range(1, max_iters + 1):
        dz = safe_get_double(conn, "AP_DZ_FROM_OLENS", default=float("nan"))
        if dz == dz and abs(dz) <= dz_limit:
            return True, dz

        z0 = safe_get_double(conn, "AP_STAGE_AT_Z")
        ol0 = safe_get_double(conn, "AP_OLENS_CURRENT")

        signs = [+1.0]
        if CONFIG["minimize_ol_try_both_signs"]:
            signs.append(-1.0)

        best = None
        for idx_s, sgn in enumerate(signs):
            z_target = z0 + sgn * dz
            conn.set_double("AP_STAGE_SETTLING_TIME", CONFIG["stage_settling_time_ms"])
            conn.set_double("AP_STAGE_GOTO_Z", z_target)
            rc = conn.execute("CMD_WAIT_STAGE_SETTLED")
            if rc != 0:
                if idx_s != len(signs) - 1:
                    conn.set_double("AP_STAGE_GOTO_Z", z0)
                    conn.execute("CMD_WAIT_STAGE_SETTLED")
                    conn.set_double("AP_OLENS_CURRENT", ol0)
                continue

            conn.set_double("AP_OLENS_CURRENT", 0.0)

            if CONFIG["minimize_ol_rerun_af_each_iter"]:
                run_af_fine_then_coarse(conn)

            dz_after = safe_get_double(conn, "AP_DZ_FROM_OLENS", default=float("nan"))
            score = abs(dz_after) if (dz_after == dz_after) else float("inf")
            cand = {"sign": sgn, "score": score, "dz_after": dz_after}
            if (best is None) or (cand["score"] < best["score"]):
                best = cand

            if idx_s != len(signs) - 1:
                conn.set_double("AP_STAGE_GOTO_Z", z0)
                conn.execute("CMD_WAIT_STAGE_SETTLED")
                conn.set_double("AP_OLENS_CURRENT", ol0)

        if best is None:
            break

        dz_now = safe_get_double(conn, "AP_DZ_FROM_OLENS", default=float("nan"))
        z_now = safe_get_double(conn, "AP_STAGE_AT_Z")
        if dz_now != dz_now:
            break

        z_commit = z_now + best["sign"] * dz_now
        conn.set_double("AP_STAGE_GOTO_Z", z_commit)
        conn.execute("CMD_WAIT_STAGE_SETTLED")
        conn.set_double("AP_OLENS_CURRENT", 0.0)
        if CONFIG["minimize_ol_rerun_af_each_iter"]:
            run_af_fine_then_coarse(conn)

    dz_final = safe_get_double(conn, "AP_DZ_FROM_OLENS", default=float("nan"))
    ok = (dz_final == dz_final) and (abs(dz_final) <= dz_limit)
    return ok, dz_final

# =============================================================================
# DeepFocus helpers
# =============================================================================

DF_FOLDER_KEYS = ["STR_REMOTE_IMAGE_FOLDER", "STR_OUTPUT_FOLDER", "STR_RECORD_OUTPUT_FOLDER"]
DF_PREFIX_KEYS = ["STR_IMAGE_FILENAME_PREFIX", "STR_OUTPUT_PREFIX", "STR_RECORD_OUTPUT_PREFIX"]
DF_FORMAT_KEYS = ["STR_IMAGE_FILETYPE", "STR_OUTPUT_FORMAT", "STR_RECORD_OUTPUT_FORMAT"]

def deepfocus_set_output(conn: MSEMConnection, folder: str, prefix: str, fmt: str = "bmp"):
    ensure_dir(folder)
    try:
        conn.set_double("DP_IMAGE_LOCATION", 1.0)
    except Exception:
        pass

    for k in DF_FOLDER_KEYS:
        try:
            conn.set_string(k, folder)
        except Exception:
            pass

    for k in DF_PREFIX_KEYS:
        try:
            conn.set_string(k, prefix)
        except Exception:
            pass

    for k in DF_FORMAT_KEYS:
        try:
            conn.set_string(k, fmt)
        except Exception:
            pass

def deepfocus_get_sem_output_folder(conn: MSEMConnection) -> Optional[str]:
    for k in DF_FOLDER_KEYS:
        try:
            path = (conn.get_string(k) or "").strip()
            if path:
                return path
        except Exception:
            continue
    return None

def wait_for_beam_indexed_image(folder: str, prefix: str, beam_index: int, timeout_sec: float = 30.0, poll_interval: float = 0.3) -> Optional[str]:
    prefix_norm = prefix.rstrip("_")
    target = os.path.join(folder, f"{prefix_norm}__{beam_index:03d}.bmp")
    deadline = time.monotonic() + timeout_sec

    while time.monotonic() < deadline:
        if os.path.isfile(target):
            return target
        time.sleep(poll_interval)

    files = glob.glob(os.path.join(folder, f"{prefix_norm}__*.bmp"))
    if files:
        files.sort(key=lambda f: os.path.getmtime(f))
        return files[-1]
    return None

def acquire_one_beam_image(conn: MSEMConnection, folder: str, prefix: str, beam_index: int) -> str:
    deepfocus_set_output(conn, folder, prefix, fmt="bmp")
    lookup_folder = deepfocus_get_sem_output_folder(conn) or folder

    rc = conn.execute("CMD_TRIGGER_ACQUISITION")
    if rc != 0:
        raise RuntimeError(f"CMD_TRIGGER_ACQUISITION rc={rc} err={safe_get_last_error(conn)}")

    rc = conn.execute("CMD_WAIT_SCAN_COMPLETE")
    if rc != 0:
        raise RuntimeError(f"CMD_WAIT_SCAN_COMPLETE rc={rc} err={safe_get_last_error(conn)}")

    rc = conn.execute("CMD_WAIT_ACQUISITION_COMPLETE")
    if rc != 0:
        raise RuntimeError(f"CMD_WAIT_ACQUISITION_COMPLETE rc={rc} err={safe_get_last_error(conn)}")

    path = wait_for_beam_indexed_image(lookup_folder, prefix, beam_index)
    if not path:
        raise RuntimeError(f"No image found for prefix={prefix} in folder={lookup_folder}")
    return path

def calibrate_um_per_uA(conn: MSEMConnection, duA: float = 1000.0) -> float:
    ol0 = safe_get_double(conn, "AP_OLENS_CURRENT")
    dz0 = safe_get_double(conn, "AP_DZ_FROM_OLENS")

    conn.set_double("AP_OLENS_CURRENT", float(ol0 + duA))
    time.sleep(0.1)
    dz1 = safe_get_double(conn, "AP_DZ_FROM_OLENS")

    conn.set_double("AP_OLENS_CURRENT", float(ol0))
    time.sleep(0.1)

    k = (dz1 - dz0) / duA
    if abs(k) < 1e-12:
        raise RuntimeError("DeepFocus calibration failed: dz change too small.")
    return k

def run_deepfocus_infer(img_plus: str, img_minus: str, out_json: str):
    python_exe = CONFIG["deepfocus_python_exe"]
    infer_script = CONFIG["deepfocus_infer_script"]
    model_pts = CONFIG["deepfocus_model_pts"]

    if not os.path.isfile(python_exe):
        print(f"[DeepFocus] python not found: {python_exe}, fallback to current python={sys.executable}")
        python_exe = sys.executable

    if not os.path.isfile(infer_script):
        raise RuntimeError(f"DeepFocus infer script not found: {infer_script}")
    if not os.path.isfile(model_pts):
        raise RuntimeError(f"DeepFocus model not found: {model_pts}")

    cmd = [
        python_exe,
        infer_script,
        "--img_plus", img_plus,
        "--img_minus", img_minus,
        "--out_json", out_json,
        "--mpath", model_pts,
        "--pert_um", str(CONFIG["deepfocus_target_pert_um"]),
        "--crop", str(CONFIG["deepfocus_crop_edge"]),
        "--n_consensus", "5",
    ]
    print("[DeepFocus] Running:", " ".join(cmd))
    subprocess.check_call(cmd)

# =============================================================================
# Measure ONE focus point
# =============================================================================

def measure_one_focus_point(conn: MSEMConnection, sp: SupportPoint,
                            skip_minimize_ol: bool = False) -> FocusMeasurement:
    do_minimize = CONFIG["minimize_ol_enabled"] and (not skip_minimize_ol)
    phase_str = "AF -> Minimize-OL -> AS" if do_minimize else "AF -> AS (no minimize-OL)"
    print("\n" + "=" * 78)
    print(f"[Offset FocusPoint {sp.index}] Stage -> (B2F) -> {phase_str}")
    print("=" * 78)

    z_target = sp.z if CONFIG["move_z_to_json_supportpoint"] else None
    stage_move_xy_z(conn, sp.x, sp.y, z_target)

    b2f_mode = str(CONFIG["b2f_mode"]).upper()
    if b2f_mode == "PER_FOCUS":
        b2f_ok, rc, px, py = run_b2f_robust(conn)
        if not b2f_ok:
            print(f"  B2F failed at sp[{sp.index}] after retries, rc={rc}")
            return FocusMeasurement(
                sp_index=sp.index, x=sp.x, y=sp.y,
                z_stage=safe_get_double(conn, "AP_STAGE_AT_Z"),
                ol_uA=safe_get_double(conn, "AP_OLENS_CURRENT"),
                dz_um=float("nan"),
                af_conf_fine=float("nan"), af_conf_coarse=float("nan"),
                af_success=False, minimize_ol_success=False,
                dz_after_minimize=float("nan"),
                as_conf_x=float("nan"), as_conf_y=float("nan"),
                as_success=False,
                stig_x=safe_get_double(conn, "AP_STIG_X", default=float("nan")),
                stig_y=safe_get_double(conn, "AP_STIG_Y", default=float("nan")),
            )

    af = run_af_fine_then_coarse(conn)
    coarse_str = "None" if af["conf_coarse"] is None else f"{af['conf_coarse']:.4f}"
    print(f"  AF: fine={af['conf_fine']:.4f}, coarse={coarse_str}, success={af['success']}")
    print(f"  OL after AF: {af['ol_after']:.3f} uA, dz_from_olens: {af['dz_um']:.3f} um")

    minimize_ok = False
    dz_after_min = float("nan")
    if do_minimize:
        if not af["success"]:
            print("  Minimize-OL skipped: AF failed.")
            minimize_ok = False
            dz_after_min = af["dz_um"]
        else:
            minimize_ok, dz_after_min = minimize_ol_lens_zen_style(conn)
            print(f"  Minimize-OL result: ok={minimize_ok}, dz_final={dz_after_min:.3f} um (limit={CONFIG['dz_limit_um']:.1f})")
    else:
        minimize_ok = True
        dz_after_min = af["dz_um"]

    all_focus_ok = af["success"] and minimize_ok

    as_rc, as_cx, as_cy, as_ok = (None, float("nan"), float("nan"), False)
    if CONFIG["as_enabled"]:
        if not all_focus_ok:
            print("  AS skipped: AF or minimize-OL did not succeed.")
        else:
            as_rc, as_cx, as_cy, as_ok = run_as(conn)
            print(f"  AS: rc={as_rc}, confX={as_cx:.4f}, confY={as_cy:.4f}, ok={as_ok}")

    z_final = safe_get_double(conn, "AP_STAGE_AT_Z")
    ol_final = safe_get_double(conn, "AP_OLENS_CURRENT")
    dz_store = dz_after_min if (dz_after_min == dz_after_min) else af["dz_um"]
    stig_x_final = safe_get_double(conn, "AP_STIG_X", default=float("nan"))
    stig_y_final = safe_get_double(conn, "AP_STIG_Y", default=float("nan"))

    return FocusMeasurement(
        sp_index=sp.index,
        x=sp.x, y=sp.y,
        z_stage=z_final,
        ol_uA=ol_final,
        dz_um=dz_store,
        af_conf_fine=float(af["conf_fine"]) if af["conf_fine"] == af["conf_fine"] else float("nan"),
        af_conf_coarse=float(af["conf_coarse"]) if (af["conf_coarse"] is not None and af["conf_coarse"] == af["conf_coarse"]) else float("nan"),
        af_success=bool(af["success"]),
        minimize_ol_success=bool(minimize_ok),
        dz_after_minimize=dz_store,
        as_conf_x=as_cx,
        as_conf_y=as_cy,
        as_success=bool(as_ok),
        stig_x=stig_x_final,
        stig_y=stig_y_final,
    )

def measure_one_focus_point_deepfocus(conn: MSEMConnection, sp: SupportPoint) -> FocusMeasurement:
    print("\n" + "=" * 78)
    print(f"[Offset FocusPoint {sp.index}] Stage -> (B2F) -> DeepFocus")
    print("=" * 78)

    z_target = sp.z if CONFIG["move_z_to_json_supportpoint"] else None
    stage_move_xy_z(conn, sp.x, sp.y, z_target)

    b2f_mode = str(CONFIG["b2f_mode"]).upper()
    if b2f_mode == "PER_FOCUS":
        rc, px, py = run_b2f(conn)
        print(f"  B2F rc={rc}, projective=({px:.3f}, {py:.3f})")
        if rc != 0:
            raise RuntimeError(f"B2F failed at sp[{sp.index}] rc={rc}")

    beam_index = int(CONFIG["deepfocus_beam_index"])
    target_pert_um = float(CONFIG["deepfocus_target_pert_um"])
    n_iters = int(CONFIG["deepfocus_n_iters"])
    focus_gain = float(CONFIG["deepfocus_focus_gain"])
    stig_gain = float(CONFIG["deepfocus_stig_gain"])
    max_wd_sd_um = float(CONFIG["deepfocus_max_wd_sd_um"])
    max_abs_dwd_um = float(CONFIG["deepfocus_max_abs_dwd_um"])
    max_abs_dol_uA = float(CONFIG["deepfocus_max_abs_dol_uA"])
    stig_clip = float(CONFIG["deepfocus_stig_clip"])
    apply_stig = bool(CONFIG.get("deepfocus_apply_stig", False))

    out_root = CONFIG["deepfocus_output_dir"]
    ensure_dir(out_root)
    ts = time.strftime("%Y%m%d_%H%M%S")
    run_dir = os.path.join(out_root, f"sp_{sp.index:03d}_{ts}")
    ensure_dir(run_dir)

    k_um_per_uA = calibrate_um_per_uA(conn)
    duA_pert = target_pert_um / k_um_per_uA
    print(f"  [DeepFocus] k={k_um_per_uA:.6g} um/uA => ±{target_pert_um:.3f} um ~ ±{duA_pert:.1f} uA")

    ol = safe_get_double(conn, "AP_OLENS_CURRENT")
    sx = safe_get_double(conn, "AP_STIG_X", 0.0)
    sy = safe_get_double(conn, "AP_STIG_Y", 0.0)

    best_wd_sd_um = float("inf")
    success = False

    before_path = acquire_one_beam_image(conn, run_dir, "before_", beam_index)
    print(f"  [DeepFocus] before={before_path}")

    for it in range(n_iters):
        conn.set_double("AP_OLENS_CURRENT", float(ol + duA_pert))
        plus = acquire_one_beam_image(conn, run_dir, f"it{it}_plus_", beam_index)

        conn.set_double("AP_OLENS_CURRENT", float(ol - duA_pert))
        minus = acquire_one_beam_image(conn, run_dir, f"it{it}_minus_", beam_index)

        out_json = os.path.join(run_dir, f"it{it}_pred.json")
        run_deepfocus_infer(plus, minus, out_json)
        with open(out_json, "r") as f:
            pred = json.load(f)

        dwd_um = float(pred["wd_correction_m"]) * 1e6
        wd_sd_um = float(pred.get("wd_sd_m", 0.0)) * 1e6
        dstigx = float(pred.get("stigx", 0.0))
        dstigy = float(pred.get("stigy", 0.0))

        best_wd_sd_um = min(best_wd_sd_um, wd_sd_um)

        if wd_sd_um > max_wd_sd_um:
            dwd_used = 0.0
            dol_uA = 0.0
            print(f"  [DeepFocus][it{it}] wd_sd too large ({wd_sd_um:.3f} um), skip focus update")
        else:
            dwd_used = max(-max_abs_dwd_um, min(max_abs_dwd_um, dwd_um))
            dol_uA = -(dwd_used / k_um_per_uA) * focus_gain
            dol_uA = max(-max_abs_dol_uA, min(max_abs_dol_uA, dol_uA))
            success = True

        ol = ol + dol_uA

        dSX = stig_gain * dstigx
        dSY = stig_gain * dstigy
        dSX = max(-stig_clip, min(stig_clip, dSX))
        dSY = max(-stig_clip, min(stig_clip, dSY))

        if apply_stig:
            sx = sx + dSX
            sy = sy + dSY

        conn.set_double("AP_OLENS_CURRENT", float(ol))
        if apply_stig:
            conn.set_double("AP_STIG_X", float(sx))
            conn.set_double("AP_STIG_Y", float(sy))

        print(
            f"  [DeepFocus][it{it}] dWD={dwd_um:+.3f} um (sd={wd_sd_um:.3f}, used={dwd_used:+.3f}) "
            f"=> dOL={dol_uA:+.1f} uA | "
            f"modelStig=({dstigx:+.3f},{dstigy:+.3f}) "
            f"{'(applied)' if apply_stig else '(not applied)'} | "
            f"OL={ol:.3f}"
        )

    after_path = acquire_one_beam_image(conn, run_dir, "after_", beam_index)
    print(f"  [DeepFocus] after={after_path}")

    z_final = safe_get_double(conn, "AP_STAGE_AT_Z")
    ol_final = safe_get_double(conn, "AP_OLENS_CURRENT")
    dz_final = safe_get_double(conn, "AP_DZ_FROM_OLENS", default=float("nan"))
    stig_x_final = safe_get_double(conn, "AP_STIG_X", default=float("nan"))
    stig_y_final = safe_get_double(conn, "AP_STIG_Y", default=float("nan"))

    pseudo_conf = 1.0 / (1.0 + best_wd_sd_um) if best_wd_sd_um == best_wd_sd_um else 0.0

    return FocusMeasurement(
        sp_index=sp.index,
        x=sp.x,
        y=sp.y,
        z_stage=z_final,
        ol_uA=ol_final,
        dz_um=dz_final,
        af_conf_fine=pseudo_conf,
        af_conf_coarse=float("nan"),
        af_success=bool(success),
        minimize_ol_success=True,
        dz_after_minimize=dz_final,
        as_conf_x=float("nan"),
        as_conf_y=float("nan"),
        as_success=bool(apply_stig and success),
        stig_x=stig_x_final,
        stig_y=stig_y_final,
    )

def measure_one_offset_point(conn: MSEMConnection, sp: SupportPoint,
                             skip_minimize_ol: bool = False) -> FocusMeasurement:
    method = str(CONFIG.get("offset_method", "AF_MINIMIZE_OL")).upper()
    if method == "AF_MINIMIZE_OL":
        return measure_one_focus_point(conn, sp, skip_minimize_ol=skip_minimize_ol)
    if method == "DEEPFOCUS":
        return measure_one_focus_point_deepfocus(conn, sp)
    raise RuntimeError(f"Unknown offset_method: {method}")

def measure_plane_update_points(
    conn: MSEMConnection,
    sps_for_update: List[SupportPoint],
) -> Tuple[List[FocusMeasurement], List[Tuple[FocusMeasurement, str]]]:
    ms: List[FocusMeasurement] = []
    skipped_pairs: List[Tuple[FocusMeasurement, str]] = []

    method = str(CONFIG.get("offset_method", "AF_MINIMIZE_OL")).upper()
    strict = bool(CONFIG.get("plane_update_strict", False))
    min_points = int(CONFIG.get("plane_update_min_points", 3) or 3)

    print("\n" + "=" * 78)
    print(f"[PlaneUpdate] Measuring {len(sps_for_update)} support points using method={method}")
    print(f"[PlaneUpdate] strict={strict}  min_points={min_points}")
    print("=" * 78)

    last_good = backup_focus_state(conn)

    for sp in sps_for_update:
        m = measure_one_offset_point(conn, sp)

        reason: Optional[str] = None
        if method == "AF_MINIMIZE_OL":
            conf_best = max(
                m.af_conf_fine if m.af_conf_fine == m.af_conf_fine else 0.0,
                m.af_conf_coarse if m.af_conf_coarse == m.af_conf_coarse else 0.0,
            )
            if not m.af_success:
                reason = "AF_failed"
            elif CONFIG["minimize_ol_enabled"] and (not m.minimize_ol_success):
                reason = "minimize_ol_failed"
            elif conf_best <= float(CONFIG["offset_af_conf_min"]):
                reason = f"low_conf={conf_best:.3g}"
            elif abs(m.dz_um) > float(CONFIG["offset_dz_limit_um"]):
                reason = f"dz_too_large={m.dz_um:.2f}"

        elif method == "DEEPFOCUS":
            if not m.af_success:
                reason = "deepfocus_failed"
            elif abs(m.dz_um) > float(CONFIG["offset_dz_limit_um"]):
                reason = f"dz_too_large={m.dz_um:.2f}"

        else:
            raise RuntimeError(f"Unknown offset_method: {method}")

        if reason is None:
            ms.append(m)
            sx = m.stig_x if m.stig_x == m.stig_x else last_good.stig_x
            sy = m.stig_y if m.stig_y == m.stig_y else last_good.stig_y
            last_good = FocusBackupState(ol_uA=m.ol_uA, stig_x=sx, stig_y=sy)
        else:
            if strict:
                raise RuntimeError(f"[PlaneUpdate] {reason} at sp[{sp.index}] (strict mode)")
            print(f"[PlaneUpdate] SKIP sp[{sp.index}]: {reason}")
            skipped_pairs.append((m, reason))
            print(f"[PlaneUpdate] restoring last-good focus state before next point...")
            restore_backup_state(conn, last_good)

    if skipped_pairs:
        skipped_str = ", ".join(f"sp[{m.sp_index}]:{r}" for m, r in skipped_pairs)
        print(f"[PlaneUpdate] Skipped {len(skipped_pairs)}/{len(sps_for_update)} point(s): {skipped_str}")

    if len(ms) < min_points:
        raise RuntimeError(
            f"[PlaneUpdate] Only {len(ms)} valid measurement(s) survived "
            f"(need >= {min_points}). Skipped: "
            + ", ".join(f"sp[{m.sp_index}]:{r}" for m, r in skipped_pairs)
        )

    print(f"[PlaneUpdate] {len(ms)}/{len(sps_for_update)} point(s) usable for fit")
    return ms, skipped_pairs

def _validate_focus_measurement(m: FocusMeasurement, require_minimize_ol: bool) -> bool:
    method = str(CONFIG.get("offset_method", "AF_MINIMIZE_OL")).upper()

    if method == "AF_MINIMIZE_OL":
        conf_best = max(
            m.af_conf_fine if m.af_conf_fine == m.af_conf_fine else 0.0,
            m.af_conf_coarse if m.af_conf_coarse == m.af_conf_coarse else 0.0,
        )
        if not m.af_success:
            return False
        if require_minimize_ol and (not m.minimize_ol_success):
            return False
        if conf_best <= float(CONFIG["offset_af_conf_min"]):
            return False
        if abs(m.dz_um) > float(CONFIG["offset_dz_limit_um"]):
            return False
        if CONFIG["as_enabled"] and (not m.as_success):
            return False
        if abs(m.ol_uA) < 1e-6:
            return False
        return True

    if method == "DEEPFOCUS":
        if not m.af_success:
            return False
        if abs(m.dz_um) > float(CONFIG["offset_dz_limit_um"]):
            return False
        return True

    raise RuntimeError(f"Unknown offset_method: {method}")


def choose_robust_one_point_offset(
    conn: MSEMConnection,
    sps_json: List[SupportPoint],
    prev_candidates: List[PrevPointCandidate],
    a_prev: float,
    b_prev: float,
    c_prev: float,
    a_sx: float = 0.0, b_sx: float = 0.0, c_sx: float = 0.0,
    a_sy: float = 0.0, b_sy: float = 0.0, c_sy: float = 0.0,
) -> Tuple[float, float, float, List[FocusMeasurement], FocusMeasurement]:
    use_all = bool(CONFIG.get("one_point_offset_use_all_valid", False))
    top_k = int(CONFIG.get("one_point_offset_top_k", 5))
    min_valid = int(CONFIG.get("one_point_offset_min_valid", 2))
    min_inliers = int(CONFIG.get("one_point_offset_min_inliers", 2))
    mad_sigma = float(CONFIG.get("one_point_offset_mad_sigma", 2.5))
    spread_warn = float(CONFIG.get("one_point_offset_delta_spread_warn_uA", 80.0))
    minimize_only_first = bool(CONFIG.get("minimize_ol_only_first_point", True))
    do_warmup = bool(CONFIG.get("warmup_coarse_af_on_long_move", True))
    warmup_dist = float(CONFIG.get("warmup_distance_threshold_um", 5000.0))
    restore_moves_back = bool(CONFIG.get("restore_moves_stage_back", False))

    if use_all:
        selected_prev = prev_candidates[:]
    else:
        selected_prev = prev_candidates[:top_k]

    if not selected_prev:
        raise RuntimeError("No previous candidates available for ONE_POINT_OFFSET.")

    print("\n" + "=" * 78)
    print(f"[OnePointOffset] evaluating {len(selected_prev)} candidate points")
    print(f"[OnePointOffset] minimize_ol_only_first_point={minimize_only_first}")
    print("=" * 78)

    backup = backup_focus_state(conn)
    start_x = safe_get_double(conn, "AP_STAGE_AT_X")
    start_y = safe_get_double(conn, "AP_STAGE_AT_Y")

    measured_all: List[FocusMeasurement] = []
    valid_pairs: List[Tuple[FocusMeasurement, float]] = []

    used_json_indices = set()
    last_good_m: Optional[FocusMeasurement] = None
    ol_minimized = False
    is_first_candidate = True

    for cand in selected_prev:
        json_idx, match_dist = find_nearest_support_point(sps_json, cand.x, cand.y)
        sp = sps_json[json_idx]

        if json_idx in used_json_indices:
            print(
                f"[OnePointOffset] skip prev sp_id={cand.sp_id} "
                f"because nearest JSON sp[{json_idx}] already used"
            )
            continue
        used_json_indices.add(json_idx)

        print(
            f"[OnePointOffset] prev sp_id={cand.sp_id} xy=({cand.x:.3f},{cand.y:.3f}) "
            f"-> JSON sp[{json_idx}] xy=({sp.x:.3f},{sp.y:.3f}) "
            f"match_dist={match_dist:.3f} um"
        )

        skip_min = minimize_only_first and ol_minimized

        if is_first_candidate and do_warmup:
            move_dist = math.hypot(sp.x - start_x, sp.y - start_y)
            if move_dist > warmup_dist:
                print(f"[OnePointOffset] long move {move_dist:.0f} um > {warmup_dist:.0f} um, running warmup")
                z_target = sp.z if CONFIG["move_z_to_json_supportpoint"] else None
                stage_move_xy_z(conn, sp.x, sp.y, z_target)
                b2f_mode = str(CONFIG["b2f_mode"]).upper()
                if b2f_mode == "PER_FOCUS":
                    run_b2f_robust(conn)
                run_warmup_coarse_af_as(conn)

        m = measure_one_offset_point(conn, sp, skip_minimize_ol=skip_min)
        measured_all.append(m)
        is_first_candidate = False

        require_min = CONFIG["minimize_ol_enabled"] and (not skip_min)
        is_valid = _validate_focus_measurement(m, require_minimize_ol=require_min)

        if not is_valid:
            print(f"[OnePointOffset] candidate rejected: sp[{m.sp_index}]")

            if restore_moves_back and last_good_m is not None:
                print(
                    f"[OnePointOffset] restoring last good state from "
                    f"sp[{last_good_m.sp_index}] (legacy mode)"
                )
                restore_good_focus_state(conn, last_good_m)
            else:
                print(
                    f"[OnePointOffset] restoring backup OL/Stig at current position"
                )
                restore_backup_state(conn, backup)

            continue

        ol_pred = a_prev * m.x + b_prev * m.y + c_prev
        delta_c_i = m.ol_uA - ol_pred

        sx_pred = a_sx * m.x + b_sx * m.y + c_sx
        sy_pred = a_sy * m.x + b_sy * m.y + c_sy
        delta_sx_i = m.stig_x - sx_pred if (m.stig_x == m.stig_x) else 0.0
        delta_sy_i = m.stig_y - sy_pred if (m.stig_y == m.stig_y) else 0.0

        valid_pairs.append((m, delta_c_i, delta_sx_i, delta_sy_i))
        last_good_m = m

        if not ol_minimized and CONFIG["minimize_ol_enabled"] and m.minimize_ol_success:
            ol_minimized = True
            backup = backup_focus_state(conn)
            print(f"[OnePointOffset] OL minimized on sp[{m.sp_index}], backup updated")

        print(
            f"[OnePointOffset] accepted sp[{m.sp_index}] "
            f"ol_pred={ol_pred:.3f} ol_meas={m.ol_uA:.3f} delta_c={delta_c_i:.3f} | "
            f"sx_pred={sx_pred:.6f} sx_meas={m.stig_x:.6f} delta_sx={delta_sx_i:.6f} | "
            f"sy_pred={sy_pred:.6f} sy_meas={m.stig_y:.6f} delta_sy={delta_sy_i:.6f}"
        )

    if len(valid_pairs) < min_valid:
        raise RuntimeError(
            f"[OnePointOffset] not enough valid candidates: "
            f"{len(valid_pairs)} < min_valid={min_valid}"
        )

    deltas_ol_all = [dc for _, dc, _, _ in valid_pairs]

    # MAD-based outlier rejection on delta_c. Skip the filter when we only
    # have 1-2 measurements (MAD isn't meaningful there); just trust them.
    # 1.4826 makes MAD a consistent estimator of sigma under normality.
    if len(valid_pairs) >= 3:
        provisional_med = median(deltas_ol_all)
        mad = median([abs(d - provisional_med) for d in deltas_ol_all])
        threshold = mad_sigma * 1.4826 * max(mad, 1e-6)
        inlier_pairs = [
            tup for tup in valid_pairs
            if abs(tup[1] - provisional_med) <= threshold
        ]
        outlier_pairs = [
            tup for tup in valid_pairs
            if abs(tup[1] - provisional_med) > threshold
        ]
        if outlier_pairs:
            ol_str = ", ".join(
                f"sp[{m.sp_index}]:{dc:+.2f}" for m, dc, _, _ in outlier_pairs
            )
            print(
                f"[OnePointOffset] MAD outlier rejection: "
                f"median={provisional_med:.3f}, MAD={mad:.3f}, "
                f"thr=±{threshold:.3f} uA -> dropping {len(outlier_pairs)}: {ol_str}"
            )
        else:
            print(
                f"[OnePointOffset] MAD outlier check: "
                f"median={provisional_med:.3f}, MAD={mad:.3f}, "
                f"thr=±{threshold:.3f} uA -> all {len(valid_pairs)} inliers"
            )
    else:
        inlier_pairs = valid_pairs[:]
        print(
            f"[OnePointOffset] only {len(valid_pairs)} valid candidate(s) — "
            f"skipping MAD filter (need >=3)"
        )

    if len(inlier_pairs) < min_inliers:
        raise RuntimeError(
            f"[OnePointOffset] only {len(inlier_pairs)} inlier(s) after MAD "
            f"rejection (need >= {min_inliers}). Raw deltas: "
            + ", ".join(f"{d:+.2f}" for d in deltas_ol_all)
        )

    deltas_ol = [dc for _, dc, _, _ in inlier_pairs]
    deltas_sx = [dsx for _, _, dsx, _ in inlier_pairs]
    deltas_sy = [dsy for _, _, _, dsy in inlier_pairs]

    delta_med = median(deltas_ol)
    delta_sx_med = median(deltas_sx)
    delta_sy_med = median(deltas_sy)

    spread = max(deltas_ol) - min(deltas_ol)

    print(f"[OnePointOffset] inlier delta_c  median = {delta_med:.3f} uA (n={len(inlier_pairs)})")
    print(f"[OnePointOffset] inlier delta_sx median = {delta_sx_med:.6f}")
    print(f"[OnePointOffset] inlier delta_sy median = {delta_sy_med:.6f}")
    print(f"[OnePointOffset] inlier OL delta spread = {spread:.3f} uA")

    if spread > spread_warn:
        # Warn only; the MAD filter above is the actual safety net. Spread
        # being large *across surviving inliers* is informational — it means
        # the region has real OL non-planarity, not that the run is wrong.
        print(
            f"[OnePointOffset] WARN inlier spread {spread:.3f} uA > "
            f"warn threshold {spread_warn:.3f} uA — "
            f"region may have real curvature; consider QUADRATIC_RESIDUAL_IDW"
        )

    def rep_key(item):
        m, dc, _, _ = item
        conf_best = max(
            m.af_conf_fine if m.af_conf_fine == m.af_conf_fine else 0.0,
            m.af_conf_coarse if m.af_conf_coarse == m.af_conf_coarse else 0.0,
        )
        return (
            abs(dc - delta_med),
            abs(m.dz_um),
            -conf_best,
        )

    representative_m, representative_dc, _, _ = min(inlier_pairs, key=rep_key)

    print(
        f"[OnePointOffset] representative point = sp[{representative_m.sp_index}] "
        f"delta_c_i={representative_dc:.3f}, median_delta_c={delta_med:.3f}"
    )

    return delta_med, delta_sx_med, delta_sy_med, measured_all, representative_m


def restore_good_focus_state(conn: MSEMConnection, m: FocusMeasurement):
    print("\n" + "=" * 78)
    print(f"[RestoreState] restore to good state from sp[{m.sp_index}]")
    print("=" * 78)

    # move back to the successful support point
    stage_move_xy_z(conn, m.x, m.y, m.z_stage)

    # restore a known-good OL
    safe_set_double(conn, "AP_OLENS_CURRENT", float(m.ol_uA))

    if m.stig_x == m.stig_x:
        safe_set_double(conn, "AP_STIG_X", float(m.stig_x))
    if m.stig_y == m.stig_y:
        safe_set_double(conn, "AP_STIG_Y", float(m.stig_y))

    # optional: restore beam-to-fiber alignment after moving back
    b2f_mode = str(CONFIG.get("b2f_mode", "PER_FOCUS")).upper()
    if b2f_mode in ("PER_FOCUS", "ONCE"):
        try:
            rc, px, py = run_b2f(conn)
            print(f"[RestoreState] B2F rc={rc}, projective=({px:.3f}, {py:.3f})")
        except Exception as e:
            print(f"[RestoreState] B2F restore skipped/failed: {e}")

    dz_now = safe_get_double(conn, "AP_DZ_FROM_OLENS", default=float("nan"))
    print(
        f"[RestoreState] XY=({m.x:.3f},{m.y:.3f}) "
        f"Z={m.z_stage:.3f} OL={m.ol_uA:.3f} "
        f"STIG=({m.stig_x:.6f},{m.stig_y:.6f}) "
        f"DZ_now={dz_now:.3f}"
    )

# =============================================================================
# Backup / Restore (ZEN-style: OL+Stig only, no stage move)
# =============================================================================

@dataclass
class FocusBackupState:
    ol_uA: float
    stig_x: float
    stig_y: float

def backup_focus_state(conn: MSEMConnection) -> FocusBackupState:
    ol = safe_get_double(conn, "AP_OLENS_CURRENT")
    sx = safe_get_double(conn, "AP_STIG_X", default=0.0)
    sy = safe_get_double(conn, "AP_STIG_Y", default=0.0)
    print(f"[BackupState] OL={ol:.3f} StigX={sx:.6f} StigY={sy:.6f}")
    return FocusBackupState(ol_uA=ol, stig_x=sx, stig_y=sy)

def restore_backup_state(conn: MSEMConnection, bk: FocusBackupState):
    safe_set_double(conn, "AP_OLENS_CURRENT", float(bk.ol_uA))
    safe_set_double(conn, "AP_STIG_X", float(bk.stig_x))
    safe_set_double(conn, "AP_STIG_Y", float(bk.stig_y))
    dz_now = safe_get_double(conn, "AP_DZ_FROM_OLENS", default=float("nan"))
    print(
        f"[RestoreBackup] OL={bk.ol_uA:.3f} "
        f"StigX={bk.stig_x:.6f} StigY={bk.stig_y:.6f} "
        f"DZ_now={dz_now:.3f}"
    )

# =============================================================================
# CSV outputs
# =============================================================================

def save_focus_points_csv(
    path: str,
    ms: List[FocusMeasurement],
    skipped: Optional[List[Tuple[FocusMeasurement, str]]] = None,
):
    skipped = skipped or []
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow([
            "sp_index", "x_um", "y_um",
            "z_stage_um", "ol_uA",
            "dz_from_olens_um",
            "af_success", "af_conf_fine", "af_conf_coarse",
            "minimize_ol_success", "dz_after_minimize_um",
            "as_success", "as_conf_x", "as_conf_y",
            "stig_x", "stig_y",
            "sp_status", "skip_reason",
        ])
        for m in ms:
            w.writerow([
                m.sp_index, m.x, m.y,
                m.z_stage, m.ol_uA,
                m.dz_um,
                int(m.af_success), m.af_conf_fine, m.af_conf_coarse,
                int(m.minimize_ol_success), m.dz_after_minimize,
                int(m.as_success), m.as_conf_x, m.as_conf_y,
                m.stig_x, m.stig_y,
                "ok", "",
            ])
        for m, reason in skipped:
            w.writerow([
                m.sp_index, m.x, m.y,
                m.z_stage, m.ol_uA,
                m.dz_um,
                int(m.af_success), m.af_conf_fine, m.af_conf_coarse,
                int(m.minimize_ol_success), m.dz_after_minimize,
                int(m.as_success), m.as_conf_x, m.as_conf_y,
                m.stig_x, m.stig_y,
                "skipped", str(reason),
            ])

def save_tiles_xy_csv(path: str, tiles: List[Tuple[float, float]]):
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["tile_idx", "x_um", "y_um"])
        for i, (x, y) in enumerate(tiles, start=1):
            w.writerow([i, x, y])

def save_tile_plan_csv(path: str, plans: List[TilePlan]):
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["tile_idx", "x_um", "y_um", "z_pred_um", "ol_pred_uA",
                     "stig_x_pred", "stig_y_pred"])
        for p in plans:
            z_out = "" if p.z_pred is None else p.z_pred
            w.writerow([p.tile_idx, p.x, p.y, z_out, p.ol_pred,
                         p.stig_x_pred, p.stig_y_pred])

def load_tile_plan_csv(path: str) -> Tuple[List[Tuple[float, float]], List[TilePlan]]:
    tiles: List[Tuple[float, float]] = []
    plans: List[TilePlan] = []
    with open(path, "r") as f:
        reader = csv.DictReader(f)
        for row in reader:
            tile_idx = int(row["tile_idx"])
            x = float(row["x_um"])
            y = float(row["y_um"])
            z_str = row.get("z_pred_um", "").strip()
            z_pred = None if z_str == "" else float(z_str)
            ol_pred = float(row["ol_pred_uA"])
            stig_x_pred = float(row.get("stig_x_pred", 0.0) or 0.0)
            stig_y_pred = float(row.get("stig_y_pred", 0.0) or 0.0)
            tiles.append((x, y))
            plans.append(TilePlan(tile_idx=tile_idx, x=x, y=y, z_pred=z_pred,
                                  ol_pred=ol_pred,
                                  stig_x_pred=stig_x_pred,
                                  stig_y_pred=stig_y_pred))
    return tiles, plans

# =============================================================================
# Prev focusmap parsing: fit plane + choose best point
# =============================================================================
# =============================================================================
# Plane update helpers
# =============================================================================

def select_three_support_points_for_plane_update(
    sps: List[SupportPoint],
    preferred_ids: Optional[List[int]] = None
) -> List[SupportPoint]:
    """
    Pick 3 well-spread support points.

    Priority:
      1) If preferred_ids is given, use those exact JSON support-point indices.
      2) Otherwise auto-pick:
         - left-most
         - right-most
         - farthest from the line connecting left-right
    """
    if preferred_ids is not None:
        picked = []
        seen = set()
        for idx in preferred_ids:
            idx_int = int(idx)
            if idx_int < 0 or idx_int >= len(sps):
                raise RuntimeError(f"plane_update_sp_ids contains invalid index: {idx_int}")
            if idx_int not in seen:
                picked.append(sps[idx_int])
                seen.add(idx_int)
        if len(picked) < 3:
            raise RuntimeError("plane_update_sp_ids must contain at least 3 unique support-point indices.")
        return picked[:3]

    if len(sps) < 3:
        raise RuntimeError("Need at least 3 support points for THREE_POINT_DELTA.")

    left = min(sps, key=lambda sp: sp.x)
    right = max(sps, key=lambda sp: sp.x)

    # if degenerate, fallback to spread by y
    if abs(right.x - left.x) < 1e-9 and abs(right.y - left.y) < 1e-9:
        sps_sorted_y = sorted(sps, key=lambda sp: sp.y)
        return [sps_sorted_y[0], sps_sorted_y[len(sps_sorted_y)//2], sps_sorted_y[-1]]

    # farthest from line(left, right)
    x1, y1 = left.x, left.y
    x2, y2 = right.x, right.y

    def point_line_distance(sp: SupportPoint) -> float:
        # distance from point to infinite line through left-right
        num = abs((y2 - y1) * sp.x - (x2 - x1) * sp.y + x2 * y1 - y2 * x1)
        den = math.sqrt((y2 - y1) ** 2 + (x2 - x1) ** 2)
        return num / den if den > 1e-12 else 0.0

    middle = max(
        [sp for sp in sps if sp.index not in (left.index, right.index)],
        key=point_line_distance
    )

    return [left, middle, right]

def select_all_support_points_for_plane_update(
    sps: List[SupportPoint]
) -> List[SupportPoint]:
    if len(sps) < 3:
        raise RuntimeError("Need at least 3 support points to fit a new plane.")
    return list(sps)

def fit_delta_plane_from_measurements(
    ms: List[FocusMeasurement],
    a_prev: float,
    b_prev: float,
    c_prev: float
) -> Tuple[float, float, float]:
    """
    Fit delta plane:
        delta(x,y) = alpha*x + beta*y + gamma

    where
        delta_i = ol_measured_i - (a_prev*x_i + b_prev*y_i + c_prev)

    Returns:
        alpha, beta, gamma
    """
    min_points = int(CONFIG.get("plane_update_min_points", 3) or 3)
    if len(ms) < min_points:
        raise RuntimeError(f"Need at least {min_points} measurements to fit delta plane, got {len(ms)}")

    A = []
    d = []
    for m in ms:
        ol_pred = a_prev * m.x + b_prev * m.y + c_prev
        delta = m.ol_uA - ol_pred
        A.append([m.x, m.y, 1.0])
        d.append(delta)

    # least-squares solve
    # works for 3 points exactly and also for >3 points
    import numpy as np
    A = np.asarray(A, dtype=float)
    d = np.asarray(d, dtype=float)

    sol, residuals, rank, s = np.linalg.lstsq(A, d, rcond=None)
    alpha, beta, gamma = float(sol[0]), float(sol[1]), float(sol[2])

    print(f"[DeltaPlaneFit] alpha={alpha:.6e}, beta={beta:.6e}, gamma={gamma:.3f}")
    if residuals is not None and len(residuals) > 0:
        print(f"[DeltaPlaneFit] residual_sum={float(residuals[0]):.6f}")

    return alpha, beta, gamma

def fit_new_plane_from_measurements(
    ms: List[FocusMeasurement]
) -> Tuple[float, float, float]:
    """
    Fit a brand-new plane directly from current measurements:
        ol(x,y) = a*x + b*y + c
    """
    if len(ms) < 3:
        raise RuntimeError(f"Need at least 3 measurements to fit a new plane, got {len(ms)}")

    import numpy as np

    A = []
    z = []
    for m in ms:
        A.append([m.x, m.y, 1.0])
        z.append(m.ol_uA)

    A = np.asarray(A, dtype=float)
    z = np.asarray(z, dtype=float)

    sol, residuals, rank, s = np.linalg.lstsq(A, z, rcond=None)
    a, b, c = float(sol[0]), float(sol[1]), float(sol[2])

    print(f"[NewPlaneFit] a={a:.6e}, b={b:.6e}, c={c:.3f}")
    if residuals is not None and len(residuals) > 0:
        print(f"[NewPlaneFit] residual_sum={float(residuals[0]):.6f}")

    for m in ms:
        ol_pred = a * m.x + b * m.y + c
        resid = m.ol_uA - ol_pred
        print(
            f"[NewPlanePt] sp[{m.sp_index}] "
            f"xy=({m.x:.1f},{m.y:.1f}) "
            f"ol_meas={m.ol_uA:.3f} ol_pred={ol_pred:.3f} resid={resid:.3f}"
        )

    return a, b, c

def fit_quadratic_from_measurements(
    ms: List[FocusMeasurement]
) -> Tuple[float, float, float, float, float, float]:
    """
    Fit quadratic surface directly from current measurements:

        ol(x,y) = ax + by + c*x^2 + d*x*y + e*y^2 + f
    """
    if len(ms) < 6:
        raise RuntimeError(f"Need at least 6 measurements to fit a quadratic surface, got {len(ms)}")

    import numpy as np

    A = []
    z = []
    for m in ms:
        A.append([m.x, m.y, m.x * m.x, m.x * m.y, m.y * m.y, 1.0])
        z.append(m.ol_uA)

    A = np.asarray(A, dtype=float)
    z = np.asarray(z, dtype=float)

    sol, residuals, rank, s = np.linalg.lstsq(A, z, rcond=None)
    a, b, c, d, e, f0 = [float(v) for v in sol]

    print(f"[QuadraticFit] a={a:.6e}, b={b:.6e}, c={c:.6e}, d={d:.6e}, e={e:.6e}, f={f0:.3f}")
    if residuals is not None and len(residuals) > 0:
        print(f"[QuadraticFit] residual_sum={float(residuals[0]):.6f}")

    for m in ms:
        ol_pred = a * m.x + b * m.y + c * m.x * m.x + d * m.x * m.y + e * m.y * m.y + f0
        resid = m.ol_uA - ol_pred
        print(
            f"[QuadraticPt] sp[{m.sp_index}] "
            f"xy=({m.x:.1f},{m.y:.1f}) "
            f"ol_meas={m.ol_uA:.3f} ol_pred={ol_pred:.3f} resid={resid:.3f}"
        )

    return a, b, c, d, e, f0

def predict_quadratic_ol(
    x: float,
    y: float,
    coef: Tuple[float, float, float, float, float, float]
) -> float:
    a, b, c, d, e, f0 = coef
    return a * x + b * y + c * x * x + d * x * y + e * y * y + f0

def build_quadratic_residual_points(
    ms: List[FocusMeasurement],
    coef: Tuple[float, float, float, float, float, float]
) -> List[Tuple[float, float, float, int]]:
    """
    Returns:
        [(x, y, residual_uA, sp_index), ...]
    """
    clip_uA = float(CONFIG.get("residual_clip_uA", 150.0))

    pts = []
    for m in ms:
        base = predict_quadratic_ol(m.x, m.y, coef)
        resid = m.ol_uA - base

        # clip residual for robustness
        resid = max(-clip_uA, min(clip_uA, resid))

        pts.append((m.x, m.y, resid, m.sp_index))

    print("[Residuals] clipped residual support points:")
    for x, y, r, spid in pts:
        print(f"  sp[{spid}] xy=({x:.1f},{y:.1f}) resid={r:.3f} uA")

    return pts

def predict_residual_idw(
    x: float,
    y: float,
    residual_pts: List[Tuple[float, float, float, int]]
) -> float:
    """
    IDW interpolation of residual field.

    residual_pts entries:
        (px, py, resid_uA, sp_index)
    """
    p = float(CONFIG.get("residual_idw_power", 2.0))
    k = CONFIG.get("residual_idw_k", 5)
    eps = float(CONFIG.get("residual_idw_eps_um", 1e-3))

    if not residual_pts:
        return 0.0

    items = []
    for px, py, rr, spid in residual_pts:
        d = math.hypot(x - px, y - py)
        items.append((d, px, py, rr, spid))

    items.sort(key=lambda t: t[0])

    if k is not None and int(k) > 0:
        items = items[:int(k)]

    # exact-hit protection
    if items[0][0] <= eps:
        return float(items[0][3])

    num = 0.0
    den = 0.0
    for d, px, py, rr, spid in items:
        w = 1.0 / ((d + eps) ** p)
        num += w * rr
        den += w

    if den <= 0.0:
        return 0.0
    return num / den

def predict_quadratic_plus_residual_idw(
    x: float,
    y: float,
    quad_coef: Tuple[float, float, float, float, float, float],
    residual_pts: List[Tuple[float, float, float, int]]
) -> float:
    base = predict_quadratic_ol(x, y, quad_coef)
    corr = predict_residual_idw(x, y, residual_pts)
    return base + corr


def _is_failed_sentinel(lens: float, stig_x: float, stig_y: float, af_conf: float) -> bool:
    """Detect ZEN's failure sentinels in a prev_focusmap row.

    Zeiss writes -4 / -5 into numeric fields when AF/AS didn't converge at
    that support point. Used to be checked against `tilt_x/tilt_y` here, but
    those columns are NOT the stig values — see the column-mapping note in
    load_prev_focusmap_fit_plane_and_candidates(). We now pass the real
    stig values (cols 8/9) so a failed AS legitimately drops the point."""
    if lens in (-4.0, -5.0):
        return True
    if stig_x in (-4.0, -5.0) and stig_y in (-4.0, -5.0):
        return True
    if af_conf != af_conf or af_conf <= 0:
        return True
    return False

def find_nearest_support_point(sps: List[SupportPoint], x: float, y: float) -> Tuple[int, float]:
    best_idx, best_dist = -1, float("inf")
    for i, sp in enumerate(sps):
        d = math.sqrt((sp.x - x) ** 2 + (sp.y - y) ** 2)
        if d < best_dist:
            best_dist = d
            best_idx = i
    return best_idx, best_dist

def _fit_plane_from_xyz(rows: List[Tuple[float, float, float]]) -> Tuple[float, float, float]:
    """Least-squares plane fit z = a*x + b*y + c using Cramer's rule."""
    Sx = Sy = Sz = Sxx = Syy = Sxy = Sxz = Syz = 0.0
    n = 0
    for x, y, z in rows:
        n += 1
        Sx += x; Sy += y; Sz += z
        Sxx += x * x; Syy += y * y; Sxy += x * y
        Sxz += x * z; Syz += y * z

    def det3(A):
        return (A[0][0] * (A[1][1] * A[2][2] - A[1][2] * A[2][1])
              - A[0][1] * (A[1][0] * A[2][2] - A[1][2] * A[2][0])
              + A[0][2] * (A[1][0] * A[2][1] - A[1][1] * A[2][0]))

    M = [[Sxx, Sxy, Sx],
         [Sxy, Syy, Sy],
         [Sx,  Sy,  float(n)]]
    D = det3(M)
    if abs(D) < 1e-12:
        raise RuntimeError("Degenerate plane fit (points nearly collinear).")

    Ma = [[Sxz, Sxy, Sx], [Syz, Syy, Sy], [Sz,  Sy,  float(n)]]
    Mb = [[Sxx, Sxz, Sx], [Sxy, Syz, Sy], [Sx,  Sz,  float(n)]]
    Mc = [[Sxx, Sxy, Sxz], [Sxy, Syy, Syz], [Sx,  Sy,  Sz]]

    return det3(Ma) / D, det3(Mb) / D, det3(Mc) / D


def load_prev_focusmap_fit_plane_and_candidates(
    path: str
) -> Tuple[float, float, float,
           float, float, float,
           float, float, float,
           List[PrevPointCandidate]]:
    # ZEN prev_focusmap column layout (10 comma-separated fields per row):
    #   0  slice_id (constant per file, e.g. "9")
    #   1  sp_id
    #   2  x_um
    #   3  y_um
    #   4  OL_uA                      (objective lens current)
    #   5  unused_a (OL tilt or similar — NOT stig; range ≈ ±0.2)
    #   6  unused_b (OL tilt — NOT stig)
    #   7  AF confidence              (0 - 1, with sentinels -4/-5 for failure)
    #   8  STIG_X value               (final AP_STIG_X after AS converged)
    #   9  STIG_Y value               (final AP_STIG_Y after AS converged)
    #
    # Previously this code mistakenly fit the stig planes on cols 5/6 and
    # treated cols 8/9 as AS confidences for scoring, which made every tile
    # get its stigmator set to the wrong value during acquisition. Verified
    # against a real prev_focusmap row + the live AP_STIG_X readback range:
    # cols 8/9 sit at ~0.13–0.18, matching live stig values, while cols 5/6
    # sit at ~-0.07 to -0.18 (different physical quantity, possibly OL tilt).
    rows_ol: List[Tuple[float, float, float]] = []
    rows_sx: List[Tuple[float, float, float]] = []
    rows_sy: List[Tuple[float, float, float]] = []
    candidates: List[PrevPointCandidate] = []
    all_valid: Dict[int, Tuple[float, float, float, float, float, float]] = {}

    metric = str(CONFIG["best_point_metric"]).upper()
    w_af = float(CONFIG.get("w_af", 1.0))
    manual_id = CONFIG.get("manual_sp_id", None)

    with open(path, "r") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = [p.strip() for p in line.split(",")]
            if len(parts) < 10:
                continue
            try:
                spid = int(float(parts[1]))
                x = float(parts[2]); y = float(parts[3])
                lens = float(parts[4])
                # cols 5, 6 are NOT stig — see header comment. Skip reading them.
                af = float(parts[7])
                stig_x_val = float(parts[8])
                stig_y_val = float(parts[9])
            except Exception:
                continue

            if _is_failed_sentinel(lens, stig_x_val, stig_y_val, af):
                continue

            rows_ol.append((x, y, lens))
            rows_sx.append((x, y, stig_x_val))
            rows_sy.append((x, y, stig_y_val))

            # AS_SUM / AF_AS modes used to mix stig values into the score as
            # if they were confidences — wrong, since this file does not store
            # AS confidence. Until that lands, only "AF" mode is meaningful;
            # anything else falls back to AF-only and prints a warning.
            if metric == "AF":
                score = af
            else:
                if metric in ("AS_SUM", "AF_AS"):
                    if not getattr(load_prev_focusmap_fit_plane_and_candidates,
                                   "_warned_scoring_fallback", False):
                        print(
                            f"[PrevPlane] WARN: best_point_metric={metric!r} relies "
                            f"on AS confidence which is not present in this "
                            f"prev_focusmap format; falling back to AF-only scoring."
                        )
                        load_prev_focusmap_fit_plane_and_candidates._warned_scoring_fallback = True
                score = af

            cand = PrevPointCandidate(
                sp_id=spid,
                x=x,
                y=y,
                af=af,
                asx=stig_x_val,  # field reused: now carries stig_x value, not AS conf
                asy=stig_y_val,
                score=score,
            )
            candidates.append(cand)
            all_valid[spid] = (x, y, af, stig_x_val, stig_y_val, score)

    if len(rows_ol) < 3:
        raise RuntimeError(f"Not enough valid points in {path}: valid={len(rows_ol)}")

    a_ol, b_ol, c_ol = _fit_plane_from_xyz(rows_ol)
    a_sx, b_sx, c_sx = _fit_plane_from_xyz(rows_sx)
    a_sy, b_sy, c_sy = _fit_plane_from_xyz(rows_sy)

    candidates.sort(key=lambda t: t.score, reverse=True)

    print(f"[PrevPlane] fit using {len(rows_ol)} points:")
    print(f"  OL    = {a_ol:.6e}*x + {b_ol:.6e}*y + {c_ol:.3f}")
    print(f"  StigX = {a_sx:.6e}*x + {b_sx:.6e}*y + {c_sx:.6f}")
    print(f"  StigY = {a_sy:.6e}*x + {b_sy:.6e}*y + {c_sy:.6f}")
    print(f"[PrevCandidates] valid candidates = {len(candidates)}")

    if manual_id is not None:
        manual_id = int(manual_id)
        if manual_id not in all_valid:
            valid_ids = sorted(all_valid.keys())
            raise RuntimeError(
                f"manual_sp_id={manual_id} not found among valid points in {path}. "
                f"Valid sp_ids: {valid_ids}"
            )
        x, y, af, asx, asy, score = all_valid[manual_id]
        candidates = [PrevPointCandidate(
            sp_id=manual_id, x=x, y=y, af=af, asx=asx, asy=asy, score=score
        )]
        print(f"[PrevCandidates] MANUAL override -> only sp_id={manual_id}, xy=({x:.3f},{y:.3f})")
    else:
        for i, cand in enumerate(candidates[:10], start=1):
            print(
                f"  rank={i:02d} sp_id={cand.sp_id} "
                f"xy=({cand.x:.3f},{cand.y:.3f}) "
                f"score={cand.score:.6g} af={cand.af:.6g} "
                f"stig=({cand.asx:.6g},{cand.asy:.6g})"
            )

    return (a_ol, b_ol, c_ol,
            a_sx, b_sx, c_sx,
            a_sy, b_sy, c_sy,
            candidates)



# =============================================================================
# Route building
# =============================================================================

def get_vertical_column_key(x: float, y: float) -> float:
    """
    For slanted vertical columns, use a transformed coordinate:
        col_key = x - slope * y

    If columns slant the opposite way, change slope sign in CONFIG.
    """
    slope = float(CONFIG.get("vertical_column_slope_dx_per_um", 0.0))
    return x - slope * y

def build_tile_route(tiles: List[Tuple[float, float]]) -> List[int]:
    if not tiles:
        return []

    mode = str(CONFIG["route_mode"]).upper()
    direction = str(CONFIG["travel_direction"]).upper()
    start = str(CONFIG["start_position"]).upper()
    horizontal = direction.startswith("HORIZONTAL")

    tol = float(CONFIG["line_group_tol_um"] if horizontal
                else CONFIG.get("vert_group_tol_um", 55.0))

    indexed = [(i, tiles[i][0], tiles[i][1]) for i in range(len(tiles))]

    if horizontal:
        # old behavior for horizontal grouping
        indexed.sort(key=lambda t: (t[2], t[1]))
        get_primary = lambda t: t[2]
    else:
        # NEW: use slanted-column key instead of pure x
        indexed.sort(key=lambda t: (get_vertical_column_key(t[1], t[2]), t[2]))
        get_primary = lambda t: get_vertical_column_key(t[1], t[2])

    lines = []
    cur = [indexed[0]]

    for item in indexed[1:]:
        val = get_primary(item)

        # Use current group mean, not first-point ref
        cur_vals = [get_primary(t) for t in cur]
        cur_mean = sum(cur_vals) / len(cur_vals)

        if abs(val - cur_mean) <= tol:
            cur.append(item)
        else:
            lines.append(cur)
            cur = [item]

    if cur:
        lines.append(cur)

    if direction in ("HORIZONTAL_DOWN", "VERTICAL_RIGHT"):
        lines.reverse()

    reverse_scan = ("RIGHT" in start) if horizontal else ("TOP" in start)

    route = []
    for li, line in enumerate(lines):
        # sort within each line
        line_sorted = sorted(line, key=lambda t: t[1] if horizontal else t[2])

        do_reverse = reverse_scan
        if mode == "SERPENTINE" and (li % 2 == 1):
            do_reverse = not do_reverse

        if do_reverse:
            line_sorted.reverse()

        route.extend([t[0] for t in line_sorted])

    return route


def get_tile_route(tiles: List[Tuple[float, float]]) -> List[int]:
    src = str(CONFIG.get("route_source", "REBUILD")).upper()

    if src == "API_ORDER":
        # Directly use the order returned by the ROI/API arrays
        return list(range(len(tiles)))

    if src == "REBUILD":
        return build_tile_route(tiles)

    raise RuntimeError(f"Unknown route_source: {src}")

def compute_line_id_for_tile(y: float, y_ref_list: List[float], tol: float) -> int:
    for i, yr in enumerate(y_ref_list):
        if abs(y - yr) <= tol:
            return i
    y_ref_list.append(y)
    return len(y_ref_list) - 1

# =============================================================================
# Output config per tile
# =============================================================================

def build_block_name(json_file: str, pixel_nm: float, ts: str) -> str:
    """Block folder name 'w#_h#_Slice#_##NM_<timestamp>' parsed from the imaging
    JSON filename (e.g. w13_h25_slice1.json -> w13_h25_Slice1) + pixel size + ts.
    Falls back to the raw stem if the w/h/slice tokens aren't all present."""
    stem = os.path.splitext(os.path.basename(str(json_file or "block")))[0]
    w = re.search(r"w(\d+)", stem, re.I)
    h = re.search(r"h(\d+)", stem, re.I)
    s = re.search(r"slice[_\-]?(\d+)", stem, re.I)
    base = f"w{w.group(1)}_h{h.group(1)}_Slice{s.group(1)}" if (w and h and s) else stem
    try:
        pn = float(pixel_nm)
        nm = f"{int(pn)}" if pn == int(pn) else f"{pn:g}"
    except Exception:
        nm = str(pixel_nm)
    return f"{base}_{nm}NM_{ts}"


def configure_output_for_tile(conn: MSEMConnection, output_root: str, czi_name: str, region_idx: int,
                              tile_idx: int, prefix_base: str, combo_format: str,
                              block_folder: Optional[str] = None,
                              region_label: Optional[str] = None) -> Dict[str, Any]:
    if block_folder is None:
        ts = time.strftime("%Y%m%d_%H%M%S")
        block_folder = os.path.join(
            output_root, build_block_name(CONFIG.get("json_file"), CONFIG.get("acq_pixel_size_nm"), ts))
    if region_label:
        safe_label = re.sub(r'[\\/:*?"<>|]+', "_", str(region_label)).strip().rstrip(".")
        region_dir_name = safe_label or f"Region_{region_idx}"
    else:
        region_dir_name = f"Region_{region_idx}"
    region_folder = os.path.join(block_folder, region_dir_name)
    # MFOV folder: root\<block>\S##R#\mfov#\
    tile_folder = os.path.join(region_folder, f"mfov{tile_idx}")
    os.makedirs(tile_folder, exist_ok=True)

    # Image filename: mfov#_<timestamp>_<beam:03d>.<ext>. The Zeiss API appends
    # the 3-digit beam number LAST (it can't go after a timestamp), so the ts is
    # baked into the prefix as a literal (computed here, NOT the API %ts token)
    # so focus_map + LiveQC can still predict the filename to find each beam.
    tile_ts = time.strftime("%Y%m%d_%H%M%S")
    prefix_value = f"mfov{tile_idx}_{tile_ts}_"

    safe_set_double(conn, "DP_IMAGE_LOCATION", 1.0)

    if CONFIG.get("save_thumbnails", True):
        safe_set_double(conn, "DP_THUMBNAIL_LOCATION", 1.0)
        safe_set_string(conn, "STR_REMOTE_THUMBNAIL_FOLDER", tile_folder)
    else:
        safe_set_double(conn, "DP_THUMBNAIL_LOCATION", 0.0)

    folder_keys = ["STR_REMOTE_IMAGE_FOLDER", "STR_OUTPUT_FOLDER", "STR_RECORD_OUTPUT_FOLDER"]
    prefix_keys = ["STR_IMAGE_FILENAME_PREFIX", "STR_OUTPUT_PREFIX", "STR_RECORD_OUTPUT_PREFIX"]
    format_keys = ["STR_IMAGE_FILETYPE", "STR_OUTPUT_FORMAT", "STR_RECORD_OUTPUT_FORMAT"]

    used = {}
    for k in folder_keys:
        if safe_set_string(conn, k, tile_folder):
            used["folder"] = k
            break
    for k in prefix_keys:
        if safe_set_string(conn, k, prefix_value):
            used["prefix"] = k
            break
    for k in format_keys:
        if safe_set_string(conn, k, combo_format):
            used["format"] = k
            break

    return {
        "tile_folder": tile_folder,
        "prefix_value": prefix_value,
        "combo_format": combo_format,
        "used_keys": used,
        "block_folder": block_folder,
    }

# =============================================================================
# Acquisition scan settings
# =============================================================================

def apply_acq_scan_settings(conn: MSEMConnection) -> Dict[str, Any]:
    result = {
        "scanrate": {"ok": False, "key": None},
        "pixelsize": {"ok": False, "key": "AP_PIXELSIZE"},
        "apply_rc": None,
        "apply_rc2": None,
    }

    print("\n[AcqSettings] BEFORE:")
    debug_dump_acq_state(conn, "before")

    safe_set_double(conn, "DP_PREPARED_WIDTH", float(CONFIG["acq_width"]))
    safe_set_double(conn, "DP_PREPARED_HEIGHT", float(CONFIG["acq_height"]))
    safe_set_double(conn, "DP_PREPARED_SCANRATE", float(CONFIG["acq_scan_speed"]))

    rc_apply = conn.execute("CMD_APPLY_SCAN_PARAMETERS")
    result["apply_rc"] = int(rc_apply)
    print(f"[AcqSettings] CMD_APPLY_SCAN_PARAMETERS rc={rc_apply} err={safe_get_last_error(conn)}")
    if rc_apply != 0:
        raise RuntimeError(f"CMD_APPLY_SCAN_PARAMETERS failed rc={rc_apply} err={safe_get_last_error(conn)}")

    ok_sr, key_sr = set_first_working_double(conn, CONFIG["acq_scanrate_candidates"], float(CONFIG["acq_scan_speed"]))
    result["scanrate"]["ok"] = ok_sr
    result["scanrate"]["key"] = key_sr

    if CONFIG.get("force_averaging_1", False):
        for k in ["DP_LINE_AVERAGING", "DP_FRAME_AVERAGING", "DP_LINE_INTEGRATION", "DP_FRAME_INTEGRATION"]:
            try:
                conn.set_double(k, 1.0)
                rb = safe_get_double(conn, k, default=float("nan"))
                if rb == rb:
                    print(f"[Averaging] set {k}=1 (readback {rb})")
            except Exception:
                pass

    set_pixelsize_nm_strict(conn, float(CONFIG["acq_pixel_size_nm"]))
    result["pixelsize"]["ok"] = True

    if CONFIG.get("apply_scanparams_twice", False):
        rc_apply2 = conn.execute("CMD_APPLY_SCAN_PARAMETERS")
        result["apply_rc2"] = int(rc_apply2)
        print(f"[AcqSettings] CMD_APPLY_SCAN_PARAMETERS (2) rc={rc_apply2} err={safe_get_last_error(conn)}")
        if rc_apply2 != 0:
            print(f"  WARNING: second apply failed rc={rc_apply2} err={safe_get_last_error(conn)}")
        set_pixelsize_nm_strict(conn, float(CONFIG["acq_pixel_size_nm"]))

    if not CONFIG.get("save_thumbnails", True):
        try:
            conn.set_double("DP_THUMBNAIL_LOCATION", 0.0)
            rb_loc = safe_get_double(conn, "DP_THUMBNAIL_LOCATION", default=float("nan"))
            print(f"[Thumb] DP_THUMBNAIL_LOCATION=0 (readback {rb_loc})")
        except Exception:
            print("[Thumb] DP_THUMBNAIL_LOCATION not supported")

        try:
            conn.set_double("DP_THUMBNAIL_SCALE", float(CONFIG.get("thumbnail_scale", 0)))
            rb_scale = safe_get_double(conn, "DP_THUMBNAIL_SCALE", default=float("nan"))
            print(f"[Thumb] DP_THUMBNAIL_SCALE={CONFIG.get('thumbnail_scale', 0)} (readback {rb_scale})")
        except Exception:
            print("[Thumb] DP_THUMBNAIL_SCALE not supported")

    print("[AcqSettings] AFTER:")
    debug_dump_acq_state(conn, "after")
    return result

# =============================================================================
# Acquire tiles
# =============================================================================

# Beam-arc guard, lazily started inside the tile loop (idempotent). Watches the
# mSEM service log and pauses/recovers acquisition on a beam-clearance arc.
_ARC_GUARD = None


def _beam_arc_guard():
    """Lazily create + start the beam-arc guard (idempotent). Returns the guard,
    or False if disabled / unavailable. Shared by focus_map.acquire_tiles (direct
    runs) AND main_live_qc's monkeypatched acquire_tiles (GUI / live-QC runs), so
    the arc monitor is active on every acquisition path."""
    global _ARC_GUARD
    if _ARC_GUARD is None:
        if not CONFIG.get("beam_arc_enabled", True):
            _ARC_GUARD = False
            return _ARC_GUARD
        try:
            from beam_arc_guard import BeamArcGuard
            _ARC_GUARD = BeamArcGuard(
                log_dir=CONFIG.get("beam_arc_log_dir", r"C:\ProgramData\CarlZeiss\mSEM"),
                recovery_mode=CONFIG.get("beam_arc_recovery_mode", "guided"),
                min_outputs=int(CONFIG.get("beam_arc_min_outputs", 2)),
            )
            _ARC_GUARD.start()
            print(f"[BeamArc] monitor started (dir={_ARC_GUARD.log_dir}, mode={_ARC_GUARD.recovery_mode})", flush=True)
        except Exception as e:
            print(f"[BeamArc] monitor NOT started: {e}", flush=True)
            _ARC_GUARD = False
    return _ARC_GUARD


def acquire_tiles(
    conn: MSEMConnection,
    tiles: List[Tuple[float, float]],
    plans: List[TilePlan],
    block_folder: str,
):
    route = get_tile_route(tiles)
    print(
        f"[Route] tiles={len(tiles)} route_len={len(route)} "
        f"source={CONFIG.get('route_source', 'REBUILD')} "
        f"mode={CONFIG['route_mode']} direction={CONFIG['travel_direction']} start={CONFIG['start_position']}"
    )
    tile_times = []
    move_times = []
    acq_times = []

    max_tiles = int(CONFIG.get("speed_test_max_tiles", 0) or 0)
    warmup_tiles = int(CONFIG.get("speed_test_warmup_tiles", 0) or 0)

    os.makedirs(block_folder, exist_ok=True)
    print(f"[Output] Block folder: {block_folder}")

    # --- beam-arc guard: start the shared log monitor (lazy, idempotent) ---
    _beam_arc_guard()

    rc_off = conn.execute("CMD_SCAN_OFF")
    print(f"[Scan] CMD_SCAN_OFF rc={rc_off}")

    safe_set_double(conn, "AP_SCAN_TIMEOUT", 60000)

    scan_set = apply_acq_scan_settings(conn)
    print(f"[AcqSettings] scanrate ok={scan_set['scanrate']['ok']} key={scan_set['scanrate']['key']} val={CONFIG['acq_scan_speed']}")
    print(f"[AcqSettings] pixelsize ok={scan_set['pixelsize']['ok']} key={scan_set['pixelsize']['key']} val={CONFIG['acq_pixel_size_nm']}nm")
    print(f"[AcqSettings] prepared readback: W={safe_get_double(conn, 'DP_PREPARED_WIDTH')} H={safe_get_double(conn, 'DP_PREPARED_HEIGHT')} SR={safe_get_double(conn, 'DP_PREPARED_SCANRATE')}")

    col_refs = []
    horizontal = str(CONFIG["travel_direction"]).upper().startswith("HORIZONTAL")
    tol = float(CONFIG["line_group_tol_um"] if horizontal else CONFIG.get("vert_group_tol_um", 55.0))
    last_col_id = None

    prev_need_wait2 = False

    first_i0 = route[0]
    first_plan = plans[first_i0]
    t0 = time.perf_counter()
    stage_move_xy_z(conn, first_plan.x, first_plan.y, first_plan.z_pred)
    t_premove = time.perf_counter() - t0
    safe_set_double(conn, "AP_OLENS_CURRENT", float(first_plan.ol_pred))
    safe_set_double(conn, "AP_STIG_X", float(first_plan.stig_x_pred))
    safe_set_double(conn, "AP_STIG_Y", float(first_plan.stig_y_pred))
    print(f"[Pipeline] pre-move to first tile: {t_premove:.3f}s")

    for order_i, tile_i0 in enumerate(route, start=1):
        t_tile0 = time.perf_counter()

        # beam-arc guard: if the log monitor flagged an arc, pause here (between
        # tiles), recover (guided/auto per CONFIG), then resume from this tile.
        if _ARC_GUARD:
            _ARC_GUARD.handle_if_arc(conn)

        plan = plans[tile_i0]
        tile_idx = plan.tile_idx
        x, y = plan.x, plan.y

        group_val = y if horizontal else get_vertical_column_key(x, y)
        line_id = compute_line_id_for_tile(group_val, col_refs, tol)
        new_line = (last_col_id is None) or (line_id != last_col_id)

        print("\n" + "-" * 78)
        print(f"[AcqTile] order={order_i}/{len(route)} tile_idx={tile_idx} line_id={line_id} new_line={new_line}")
        print(f"          XY=({x:.3f},{y:.3f}) Z_pred={plan.z_pred} OL_pred={plan.ol_pred:.3f} "
              f"StigX={plan.stig_x_pred:.6f} StigY={plan.stig_y_pred:.6f}")

        t_flush_prev = 0.0
        if prev_need_wait2:
            tF0 = time.perf_counter()
            rc_w2 = conn.execute("CMD_WAIT_ACQUISITION_COMPLETE")
            t_flush_prev = time.perf_counter() - tF0
            if rc_w2 != 0:
                raise RuntimeError(f"[Acq] wait prev acq failed rc={rc_w2} err={safe_get_last_error(conn)}")
            prev_need_wait2 = False

        t_sanity = 0.0
        do_sanity = False
        n = int(CONFIG.get("sanity_every_n_tiles", 0) or 0)
        if n > 0 and (order_i % n == 0):
            do_sanity = True
        if CONFIG.get("sanity_on_new_line", False) and new_line:
            do_sanity = True

        if do_sanity:
            tS = time.perf_counter()
            rc_s = conn.execute("CMD_CHECK_SYSTEM_SANITY")
            t_sanity = time.perf_counter() - tS
            if rc_s != 0:
                raise RuntimeError(f"[Sanity] failed rc={rc_s} err={safe_get_last_error(conn)}")

        rb_px = safe_get_double(conn, "AP_PIXELSIZE", default=float("nan"))
        print(f"[PreTrigger] AP_PIXELSIZE readback = {rb_px} nm")

        out = configure_output_for_tile(
            conn, CONFIG["output_root"], CONFIG["czi_name"], CONFIG["region_idx"],
            tile_idx, CONFIG["prefix_base"], CONFIG["combo_format"],
            block_folder=block_folder,
            region_label=CONFIG.get("region_name"),
        )
        print(f"[Output] tile_folder={out['tile_folder']} prefix={out['prefix_value']}")

        t_trig0 = time.perf_counter()
        rc_t = conn.execute("CMD_TRIGGER_ACQUISITION")
        if rc_t != 0:
            raise RuntimeError(f"[Acq] trigger failed rc={rc_t} err={safe_get_last_error(conn)}")
        t_trig = time.perf_counter() - t_trig0

        t_scan = 0.0
        if CONFIG.get("use_wait_scan_complete", True):
            t_scan0 = time.perf_counter()
            rc_w1 = conn.execute("CMD_WAIT_SCAN_COMPLETE")
            t_scan = time.perf_counter() - t_scan0
            if rc_w1 != 0:
                raise RuntimeError(f"[Acq] wait scan failed rc={rc_w1} err={safe_get_last_error(conn)}")

        t_move_next = 0.0
        if order_i < len(route):
            next_plan = plans[route[order_i]]
            tM0 = time.perf_counter()
            stage_move_xy_z(conn, next_plan.x, next_plan.y, next_plan.z_pred)
            t_move_next = time.perf_counter() - tM0
            safe_set_double(conn, "AP_OLENS_CURRENT", float(next_plan.ol_pred))
            safe_set_double(conn, "AP_STIG_X", float(next_plan.stig_x_pred))
            safe_set_double(conn, "AP_STIG_Y", float(next_plan.stig_y_pred))

        t_finish_now = 0.0
        defer = bool(CONFIG.get("defer_finish_write", False))
        use_wait2 = bool(CONFIG.get("use_wait_acq_complete", True))

        if defer:
            prev_need_wait2 = True
            force_n = int(CONFIG.get("force_finish_every_n", 0) or 0)
            if force_n > 0 and (order_i % force_n == 0):
                tF0 = time.perf_counter()
                rc_w2 = conn.execute("CMD_WAIT_ACQUISITION_COMPLETE")
                t_finish_now = time.perf_counter() - tF0
                if rc_w2 != 0:
                    raise RuntimeError(f"[Acq] wait acq failed rc={rc_w2} err={safe_get_last_error(conn)}")
                prev_need_wait2 = False
        else:
            if use_wait2:
                tF0 = time.perf_counter()
                rc_w2 = conn.execute("CMD_WAIT_ACQUISITION_COMPLETE")
                t_finish_now = time.perf_counter() - tF0
                if rc_w2 != 0:
                    raise RuntimeError(f"[Acq] wait acq failed rc={rc_w2} err={safe_get_last_error(conn)}")

        tiny_ms = int(CONFIG.get("pipeline_tiny_delay_ms", 0) or 0)
        if tiny_ms > 0:
            time.sleep(tiny_ms / 1000.0)

        t_tile_wall = time.perf_counter() - t_tile0

        scan_chain = t_trig + t_scan
        acq_wall = scan_chain + t_flush_prev
        move_wall = t_move_next
        total_wall = acq_wall + move_wall

        if order_i > warmup_tiles:
            move_times.append(move_wall)
            acq_times.append(acq_wall)
            tile_times.append(total_wall)

        print(
            f"[TileWall] acq_wall={acq_wall:.3f}s (scan={scan_chain:.3f}s + flush_prev={t_flush_prev:.3f}s) "
            f"move_next={move_wall:.3f}s total(move+acq)={total_wall:.3f}s  "
            f"(measured_loop_wall={t_tile_wall:.3f}s, sanity={t_sanity:.3f}s, finish_now={t_finish_now:.3f}s)"
        )

        if max_tiles and order_i >= max_tiles:
            print(f"[Timing] Reached max_tiles={max_tiles}, stopping early.")
            break

        last_col_id = line_id

    if prev_need_wait2:
        print("[Pipeline] final flush last tile...")
        rc_w2 = conn.execute("CMD_WAIT_ACQUISITION_COMPLETE")
        if rc_w2 != 0:
            raise RuntimeError(f"[Acq] final wait acq failed rc={rc_w2} err={safe_get_last_error(conn)}")

    def _summ(xs):
        if not xs:
            return "n/a"
        return f"n={len(xs)} mean={mean(xs):.3f}s med={median(xs):.3f}s min={min(xs):.3f}s max={max(xs):.3f}s"

    print("\n" + "=" * 78)
    print("[Timing] SUMMARY (excluding focus)")
    print("  move :", _summ(move_times))
    print("  acq  :", _summ(acq_times))
    print("  total:", _summ(tile_times))
    print("=" * 78 + "\n")


class _TeeStream:
    """File-like wrapper that writes to both the original stream AND a log file.

    Used per-section so every print() / traceback / Zeiss SDK chatter that
    you see in the terminal is also persisted under <block>/<region_name>/console.log.
    Falls back gracefully if writes fail (we never want logging to break acquisition).
    """

    def __init__(self, primary, log_file):
        self._primary = primary
        self._log = log_file

    def write(self, s):
        try:
            self._primary.write(s)
        except Exception:
            pass
        try:
            self._log.write(s)
            self._log.flush()
        except Exception:
            pass
        try:
            return len(s)
        except Exception:
            return 0

    def flush(self):
        try:
            self._primary.flush()
        except Exception:
            pass
        try:
            self._log.flush()
        except Exception:
            pass

    def isatty(self):
        try:
            return self._primary.isatty()
        except Exception:
            return False

    def __getattr__(self, name):
        return getattr(self._primary, name)


def resolve_prev_focusmap_path(region_name: str) -> str:
    """Find the prev_focusmap .txt for a region, in priority order:

      1. CONFIG['prev_focusmap_by_region']  — explicit per-region dict (if the
         region key is present, or a same-section S<N>* key as fallback)
      2. CONFIG['prev_focusmap_dir']        — a FOLDER of .txt files; the file
         is matched to the region by section number (S25R1 -> a file whose name
         contains 's25' as a token, e.g. w15_s25.txt). This is the "just point
         at a folder, software figures it out" mode.
      3. CONFIG['prev_focusmap_path']       — legacy single-file fallback

    Returns an absolute path (relative paths resolve against project_root).
    """
    def _abs(p: str) -> str:
        return p if os.path.isabs(p) else os.path.join(project_root, p)

    prev_map_dict = CONFIG.get("prev_focusmap_by_region", None)
    prev_dir = CONFIG.get("prev_focusmap_dir", None)

    # 1. explicit per-region dict
    if prev_map_dict:
        if region_name in prev_map_dict:
            return _abs(prev_map_dict[region_name])
        m = re.match(r"(S\d+)", region_name)
        if m:
            section_prefix = m.group(1)
            same_section = sorted(k for k in prev_map_dict if k.startswith(section_prefix))
            if same_section:
                print(f"[PrevFocusMap] '{region_name}' not in dict, using same-section "
                      f"key '{same_section[0]}'")
                return _abs(prev_map_dict[same_section[0]])

    # 2. folder auto-discovery by section number
    if prev_dir:
        prev_dir_abs = _abs(prev_dir)
        if not os.path.isdir(prev_dir_abs):
            raise FileNotFoundError(f"prev_focusmap_dir not found: {prev_dir_abs}")
        m = re.match(r"[Ss](\d+)", region_name)
        if not m:
            raise RuntimeError(
                f"Cannot extract a section number from region '{region_name}' "
                f"to match a prev_focusmap file in {prev_dir_abs}")
        sec = int(m.group(1))
        # match an 's<sec>' OR 'slice<sec>' token: 's25'/'slice_25' match
        # w15_s25.txt / slice_25_FocusMap.txt (the raw-data naming) but NOT
        # s250 / slice_250 (right boundary) and NOT slice_16 for sec=6.
        pat = re.compile(rf'(?:^|[_\-])(?:slice[_\-]?|[sS])0*{sec}(?=$|[_\-.])', re.I)
        matches = sorted(
            f for f in os.listdir(prev_dir_abs)
            if f.lower().endswith(".txt") and pat.search(os.path.splitext(f)[0])
        )
        if not matches:
            sample = sorted(x for x in os.listdir(prev_dir_abs) if x.lower().endswith(".txt"))[:20]
            raise FileNotFoundError(
                f"No prev_focusmap .txt for section S{sec} (region '{region_name}') "
                f"in {prev_dir_abs}.\n  .txt files there: {sample}")
        if len(matches) > 1:
            print(f"[PrevFocusMap] multiple .txt match section S{sec}: {matches} "
                  f"— using '{matches[0]}'")
        chosen = os.path.join(prev_dir_abs, matches[0])
        print(f"[PrevFocusMap] region '{region_name}' -> section S{sec} -> {matches[0]}")
        return chosen

    # 3. legacy single-file
    legacy = CONFIG.get("prev_focusmap_path")
    if legacy:
        return _abs(legacy)

    raise RuntimeError(
        f"No prev focusmap source for region '{region_name}': set one of "
        f"prev_focusmap_by_region, prev_focusmap_dir, or prev_focusmap_path in CONFIG.")


def run_one_region(
    conn: MSEMConnection,
    json_file: str,
    region_name: str,
    region_idx: int,
    block_folder: str,
):
    # ---- Per-section console log: tee stdout+stderr to <block>/<region_name>/console.log
    safe_label = re.sub(r'[\\/:*?"<>|]+', "_", str(region_name)).strip().rstrip(".")
    section_dir_name = safe_label or f"Region_{region_idx}"
    section_folder = os.path.join(block_folder, section_dir_name)
    os.makedirs(section_folder, exist_ok=True)
    log_path = os.path.join(section_folder, "console.log")
    try:
        log_file = open(log_path, "a", buffering=1, encoding="utf-8", errors="replace")
        log_file.write(
            f"\n=== focus_map process @ {time.strftime('%Y-%m-%d %H:%M:%S')} "
            f"(region {region_name}, idx {region_idx}) ===\n"
        )
        log_file.flush()
    except Exception as _exc:
        log_file = None
        print(f"[ConsoleLog] could not open {log_path}: {_exc} (running without tee)")

    orig_stdout, orig_stderr = sys.stdout, sys.stderr
    if log_file is not None:
        sys.stdout = _TeeStream(orig_stdout, log_file)
        sys.stderr = _TeeStream(orig_stderr, log_file)

    old_section_log = CONFIG.get("section_console_log_path")
    CONFIG["section_console_log_path"] = log_path

    try:
        return _run_one_region_body(
            conn=conn,
            json_file=json_file,
            region_name=region_name,
            region_idx=region_idx,
            block_folder=block_folder,
        )
    except BaseException:
        import traceback as _tb
        _tb.print_exc()  # captured by tee, then re-raised
        raise
    finally:
        CONFIG["section_console_log_path"] = old_section_log
        if log_file is not None:
            try:
                log_file.write(
                    f"=== focus_map process END @ {time.strftime('%Y-%m-%d %H:%M:%S')} ===\n"
                )
            except Exception:
                pass
            try:
                log_file.close()
            except Exception:
                pass
        sys.stdout, sys.stderr = orig_stdout, orig_stderr


def _run_one_region_body(
    conn: MSEMConnection,
    json_file: str,
    region_name: str,
    region_idx: int,
    block_folder: str,
):
    print("\n" + "=" * 78)
    print(f"[Region] START region_name={region_name} region_idx={region_idx}")
    print("=" * 78)

    sps, contour_pts = load_region_and_support_points(json_file, region_name)
    print(f"[JSON] region='{region_name}' support_points={len(sps)} contour_vertices={len(contour_pts)}")



    prev_path = resolve_prev_focusmap_path(region_name)

    if not os.path.exists(prev_path):
        raise FileNotFoundError(f"prev focusmap not found for region '{region_name}': {prev_path}")



    # Load the prev focusmap FIRST (it's pure file I/O — no hardware dependency)
    # so we can use the OL plane prediction at this region's first SP as a far
    # better initial OL than the hardcoded `initial_ol_uA` default. AF then
    # starts close to the right answer, faster convergence + less likely to fail.
    (a_prev, b_prev, c_prev,
     a_sx_prev, b_sx_prev, c_sx_prev,
     a_sy_prev, b_sy_prev, c_sy_prev,
     prev_candidates) = load_prev_focusmap_fit_plane_and_candidates(prev_path)

    init_mode = str(CONFIG.get("initial_ol_mode", "PREV_PLANE_AT_FIRST_SP")).upper()
    if init_mode == "PREV_PLANE_AT_FIRST_SP" and sps:
        first_sp = sps[0]
        init_ol = a_prev * first_sp.x + b_prev * first_sp.y + c_prev
        safe_set_double(conn, "AP_OLENS_CURRENT", float(init_ol))
        print(
            f"[Init] Set initial OL to {init_ol:.1f} uA "
            f"(from prev plane at sp[{first_sp.index}] "
            f"xy=({first_sp.x:.1f},{first_sp.y:.1f}))"
        )
    elif init_mode == "READ_CURRENT":
        cur_ol = safe_get_double(conn, "AP_OLENS_CURRENT")
        print(f"[Init] Keeping current OL = {cur_ol:.1f} uA (no reset)")
    elif init_mode in ("FIXED", "DEFAULT") and CONFIG.get("initial_ol_uA") is not None:
        safe_set_double(conn, "AP_OLENS_CURRENT", float(CONFIG["initial_ol_uA"]))
        print(f"[Init] Set initial OL to {CONFIG['initial_ol_uA']} uA (FIXED mode)")
    elif CONFIG.get("initial_ol_uA") is not None:
        # Unknown mode -> fall back to FIXED behavior for safety
        safe_set_double(conn, "AP_OLENS_CURRENT", float(CONFIG["initial_ol_uA"]))
        print(
            f"[Init] Unknown initial_ol_mode={init_mode!r}, "
            f"falling back to FIXED {CONFIG['initial_ol_uA']} uA"
        )

    if str(CONFIG["b2f_mode"]).upper() == "ONCE":
        rc, px, py = run_b2f(conn)
        print(f"[Region] B2F once rc={rc}, projective=({px:.3f}, {py:.3f})")

    a_sx_new, b_sx_new, c_sx_new = a_sx_prev, b_sx_prev, c_sx_prev
    a_sy_new, b_sy_new, c_sy_new = a_sy_prev, b_sy_prev, c_sy_prev

    plane_update_method = str(CONFIG.get("plane_update_method", "ONE_POINT_OFFSET")).upper()

    if plane_update_method == "ONE_POINT_OFFSET":
        delta_c, delta_sx, delta_sy, measured_all, representative_m = choose_robust_one_point_offset(
            conn=conn,
            sps_json=sps,
            prev_candidates=prev_candidates,
            a_prev=a_prev,
            b_prev=b_prev,
            c_prev=c_prev,
            a_sx=a_sx_prev, b_sx=b_sx_prev, c_sx=c_sx_prev,
            a_sy=a_sy_prev, b_sy=b_sy_prev, c_sy=c_sy_prev,
        )
        restore_good_focus_state(conn, representative_m)
        focus_csv = f"{os.path.splitext(CONFIG['output_focus_points_csv'])[0]}_{region_name}.csv"
        save_focus_points_csv(focus_csv, measured_all)
        print(f"[CSV] saved offset candidate measurements: {focus_csv}")

        a_new = a_prev
        b_new = b_prev
        c_new = c_prev + delta_c

        c_sx_new = c_sx_prev + delta_sx
        c_sy_new = c_sy_prev + delta_sy

        print(
            f"[DeltaC] representative sp={representative_m.sp_index}, "
            f"robust delta_c={delta_c:.3f} -> c_new={c_new:.3f}"
        )
        print(f"[NewPlane] OL    = {a_new:.6e}*x + {b_new:.6e}*y + {c_new:.3f}")
        print(f"[NewPlane] StigX = {a_sx_new:.6e}*x + {b_sx_new:.6e}*y + {c_sx_new:.6f}")
        print(f"[NewPlane] StigY = {a_sy_new:.6e}*x + {b_sy_new:.6e}*y + {c_sy_new:.6f}")

    elif plane_update_method == "THREE_POINT_DELTA":
        preferred_ids = CONFIG.get("plane_update_sp_ids", None)
        sps_for_update = select_three_support_points_for_plane_update(sps, preferred_ids)

        print("[PlaneUpdate] Selected support points:")
        for sp in sps_for_update:
            print(f"  sp[{sp.index}] xy=({sp.x:.3f}, {sp.y:.3f}) z={sp.z:.3f}")

        ms, skipped = measure_plane_update_points(conn, sps_for_update)
        focus_csv = f"{os.path.splitext(CONFIG['output_focus_points_csv'])[0]}_{region_name}.csv"
        save_focus_points_csv(focus_csv, ms, skipped=skipped)
        print(f"[CSV] saved plane-update measurements: {focus_csv} ({len(ms)} ok, {len(skipped)} skipped)")

        alpha, beta, gamma = fit_delta_plane_from_measurements(ms, a_prev, b_prev, c_prev)

        a_new = a_prev + alpha
        b_new = b_prev + beta
        c_new = c_prev + gamma

        print(f"[DeltaPlane] old plane: ol = {a_prev:.6e}*x + {b_prev:.6e}*y + {c_prev:.3f}")
        print(f"[DeltaPlane] delta    : d  = {alpha:.6e}*x + {beta:.6e}*y + {gamma:.3f}")
        print(f"[NewPlane]   new plane: ol = {a_new:.6e}*x + {b_new:.6e}*y + {c_new:.3f}")

    elif plane_update_method == "CURRENT_ALL_POINTS_PLANE":
        sps_for_update = select_all_support_points_for_plane_update(sps)

        print("[PlaneUpdate] Using ALL support points:")
        for sp in sps_for_update:
            print(f"  sp[{sp.index}] xy=({sp.x:.3f}, {sp.y:.3f}) z={sp.z:.3f}")

        ms, skipped = measure_plane_update_points(conn, sps_for_update)
        focus_csv = f"{os.path.splitext(CONFIG['output_focus_points_csv'])[0]}_{region_name}.csv"
        save_focus_points_csv(focus_csv, ms, skipped=skipped)
        print(f"[CSV] saved plane-update measurements: {focus_csv} ({len(ms)} ok, {len(skipped)} skipped)")

        a_new, b_new, c_new = fit_new_plane_from_measurements(ms)

        print(f"[PrevPlane] old plane: ol = {a_prev:.6e}*x + {b_prev:.6e}*y + {c_prev:.3f}")
        print(f"[NewPlane]  new plane: ol = {a_new:.6e}*x + {b_new:.6e}*y + {c_new:.3f}")

    elif plane_update_method == "CURRENT_ALL_POINTS_QUADRATIC_RESIDUAL_IDW":
        sps_for_update = select_all_support_points_for_plane_update(sps)

        print("[PlaneUpdate] Using ALL support points:")
        for sp in sps_for_update:
            print(f"  sp[{sp.index}] xy=({sp.x:.3f}, {sp.y:.3f}) z={sp.z:.3f}")

        ms, skipped = measure_plane_update_points(conn, sps_for_update)
        focus_csv = f"{os.path.splitext(CONFIG['output_focus_points_csv'])[0]}_{region_name}.csv"
        save_focus_points_csv(focus_csv, ms, skipped=skipped)
        print(f"[CSV] saved plane-update measurements: {focus_csv} ({len(ms)} ok, {len(skipped)} skipped)")

        quad_coef = fit_quadratic_from_measurements(ms)
        residual_pts = build_quadratic_residual_points(ms, quad_coef)

        print("[Quadratic+ResidualIDW] ready for tile prediction.")

    else:
        raise RuntimeError(f"Unknown plane_update_method: {plane_update_method}")

    if not CONFIG["use_region_contour_for_tiles"]:
        raise RuntimeError("use_region_contour_for_tiles=False; this script expects ROI tiles from contour.")

    tiles = get_roi_tile_positions(conn, contour_pts)
    print(f"[ROI] tiles generated: {len(tiles)}")

    tiles_csv = f"{os.path.splitext(CONFIG['output_tiles_csv'])[0]}_{region_name}.csv"
    save_tiles_xy_csv(tiles_csv, tiles)
    print(f"[CSV] saved tiles: {tiles_csv}")

    plans: List[TilePlan] = []
    for i, t in enumerate(tiles, start=1):
        tx = float(t[0])
        ty = float(t[1])

        if plane_update_method == "CURRENT_ALL_POINTS_QUADRATIC_RESIDUAL_IDW":
            ol = predict_quadratic_plus_residual_idw(tx, ty, quad_coef, residual_pts)
        else:
            ol = a_new * tx + b_new * ty + c_new

        sx = a_sx_new * tx + b_sx_new * ty + c_sx_new
        sy = a_sy_new * tx + b_sy_new * ty + c_sy_new

        plans.append(TilePlan(tile_idx=i, x=tx, y=ty, z_pred=None, ol_pred=float(ol),
                              stig_x_pred=float(sx), stig_y_pred=float(sy)))

    tileplan_csv = f"{os.path.splitext(CONFIG['output_tile_plan_csv'])[0]}_{region_name}.csv"
    save_tile_plan_csv(tileplan_csv, plans)
    print(f"[CSV] saved tile plan: {tileplan_csv}")

    old_region_idx = CONFIG["region_idx"]
    old_region_name = CONFIG.get("region_name")
    old_imaging_json_path = CONFIG.get("imaging_json_path")
    CONFIG["region_idx"] = region_idx
    CONFIG["region_name"] = region_name
    CONFIG["imaging_json_path"] = os.path.abspath(json_file)
    try:
        acquire_tiles(conn, tiles, plans, block_folder=block_folder)
    finally:
        CONFIG["region_idx"] = old_region_idx
        CONFIG["region_name"] = old_region_name
        CONFIG["imaging_json_path"] = old_imaging_json_path

    print(f"[Region] DONE region_name={region_name}")

# =============================================================================
# MAIN
# =============================================================================

def _count_focusmap_data_rows(path: str) -> int:
    """Count rows that look like real focus-map data (>=10 comma fields with a
    parseable x,y in cols 2,3). A present-but-empty/garbage file returns 0 so the
    preflight can reject it instead of failing mid-run inside the plane fit."""
    n = 0
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                parts = line.split(",")
                if len(parts) < 10:
                    continue
                try:
                    float(parts[2]); float(parts[3])
                    n += 1
                except (ValueError, IndexError):
                    continue
    except Exception:
        return 0
    return n


def preflight_resolve_prev_focusmaps(json_file: str, region_names: List[str]) -> List[str]:
    """Resolve + validate the prev_focusmap .txt for EVERY region up front.

    Reuses resolve_prev_focusmap_path() (so resolution semantics are identical to
    the per-region path) but runs once, before any hardware work, so a missing
    slice_<N>_FocusMap.txt aborts the whole run immediately instead of after hours
    of imaging. Honors CONFIG['preflight_skip_missing']:
        False (default) -> raise (abort) if any region has no usable file
        True            -> warn + drop those regions, return the survivors
    Prints a [Preflight] summary (keywords FAILED/WARNING/skip so the GUI colours
    the lines). Returns the region_names to actually process.
    """
    resolved: Dict[str, str] = {}
    failures: List[Tuple[str, str]] = []
    for rn in region_names:
        try:
            p = resolve_prev_focusmap_path(rn)
            if not os.path.exists(p):
                raise FileNotFoundError(f"resolved but not on disk: {p}")
            if os.path.getsize(p) <= 0:
                raise ValueError(f"file is empty: {p}")
            n_rows = _count_focusmap_data_rows(p)
            min_rows = max(3, int(CONFIG.get("plane_update_min_points", 3) or 3))
            if n_rows < min_rows:
                raise ValueError(f"only {n_rows} usable data row(s) (need >={min_rows}): {p}")
            resolved[rn] = p
        except Exception as exc:
            failures.append((rn, str(exc).splitlines()[0]))

    print("=" * 78)
    print(f"[Preflight] prev_focusmap check for {len(region_names)} region(s) "
          f"(dir={CONFIG.get('prev_focusmap_dir')}):")
    reason_by_region = dict(failures)
    for rn in region_names:
        if rn in resolved:
            print(f"[Preflight]   {rn} -> {os.path.basename(resolved[rn])} (OK)")
        else:
            print(f"[Preflight]   {rn} -> NO prev focusmap: {reason_by_region.get(rn, '?')}")
    print(f"[Preflight] {len(resolved)} OK, {len(failures)} missing.")
    print("=" * 78)

    if not failures:
        return region_names

    if not CONFIG.get("preflight_skip_missing", False):
        bad = ", ".join(rn for rn, _ in failures)
        raise RuntimeError(
            f"[Preflight] FAILED: {len(failures)} region(s) have no usable prev_focusmap "
            f"({bad}). Aborting before acquisition. Set preflight_skip_missing=true to "
            f"skip them and image the rest instead.")

    ok_names = [rn for rn in region_names if rn in resolved]
    for rn, reason in failures:
        print(f"[Preflight] WARNING: skipping region {rn} (no prev focusmap: {reason})")
    if not ok_names:
        raise RuntimeError(
            "[Preflight] FAILED: no region has a usable prev_focusmap; nothing to image.")
    print(f"[Preflight] proceeding with {len(ok_names)} region(s): {ok_names}")
    return ok_names


def main():
    run_ts = time.strftime("%Y%m%d_%H%M%S")
    block_folder = os.path.join(
        CONFIG["output_root"],
        build_block_name(CONFIG.get("json_file"), CONFIG.get("acq_pixel_size_nm"), run_ts))
    os.makedirs(block_folder, exist_ok=True)
    print(f"[Output] Global block folder: {block_folder}")

    reuse_csv = CONFIG.get("reuse_tile_plan_csv", None)

    if reuse_csv is not None:
        if not os.path.isabs(reuse_csv):
            reuse_csv = os.path.join(project_root, reuse_csv)
        if not os.path.exists(reuse_csv):
            raise FileNotFoundError(f"reuse_tile_plan_csv not found: {reuse_csv}")

        print("=" * 78)
        print(f"REUSE MODE: loading tile plan from {reuse_csv}")
        print(f"  acq_scan_speed={CONFIG['acq_scan_speed']}, stage_settling_time_ms={CONFIG['stage_settling_time_ms']}")
        print("=" * 78)

        tiles, plans = load_tile_plan_csv(reuse_csv)
        print(f"[Reuse] Loaded {len(plans)} tiles from {reuse_csv}")

        print("\nConnecting...")
        conn = MSEMConnection()
        conn.connect_and_initialize()
        print("Connected.\n")

        ensure_beam_and_scan(conn)
        reset_af_as_and_set_beams(conn)

        cur_speed = safe_get_double(conn, "AP_STAGE_SPEED_LIMIT")
        print(f"[Stage] Current AP_STAGE_SPEED_LIMIT = {cur_speed}%")
        if CONFIG["stage_speed_limit_pct"] is not None:
            target_speed = float(CONFIG["stage_speed_limit_pct"])
            safe_set_double(conn, "AP_STAGE_SPEED_LIMIT", target_speed)
            readback = safe_get_double(conn, "AP_STAGE_SPEED_LIMIT")
            print(f"[Stage] Set AP_STAGE_SPEED_LIMIT to {target_speed}% (readback: {readback}%)")

        acquire_tiles(conn, tiles, plans, block_folder=block_folder)
        print("\nDONE (reuse mode).")
        return

    print("=" * 78)
    print("EXACT PIPELINE: fit prev plane -> choose best point -> offset/update plane -> acquire all tiles")
    print(f"OFFSET METHOD = {CONFIG.get('offset_method', 'AF_MINIMIZE_OL')}")
    print("=" * 78)

    json_file = CONFIG["json_file"]
    if not os.path.isabs(json_file):
        json_file = os.path.join(project_root, json_file)
    if not os.path.exists(json_file):
        raise FileNotFoundError(f"JSON not found: {json_file}")

    # Decide which regions to process (pure file/JSON work — no hardware) FIRST,
    # so the prev_focusmap preflight can fail fast BEFORE we touch the scope.
    process_all = bool(CONFIG.get("process_all_regions", False))
    region_name = CONFIG.get("region_name", None)

    if process_all or region_name is None:
        region_names = compute_region_processing_order(json_file)
    else:
        region_names = [region_name]

    region_filter = CONFIG.get("region_name_filter", None)
    if region_filter:
        before_count = len(region_names)
        region_names = [r for r in region_names if region_filter in r]
        print(f"[RegionFilter] '{region_filter}' matched {len(region_names)}/{before_count} regions")

    print(f"[Regions] Will process {len(region_names)} region(s): {region_names}")

    # Preflight: when processing all regions from a prev_focusmap FOLDER, resolve
    # + validate every region's slice_<N>_FocusMap.txt now so a missing section
    # aborts (or is skipped) BEFORE hours of imaging, not mid-run.
    if process_all and CONFIG.get("prev_focusmap_dir"):
        region_names = preflight_resolve_prev_focusmaps(json_file, region_names)

    print("\nConnecting...")
    conn = MSEMConnection()
    conn.connect_and_initialize()
    print("Connected.\n")

    ensure_beam_and_scan(conn)
    reset_af_as_and_set_beams(conn)

    cur_speed = safe_get_double(conn, "AP_STAGE_SPEED_LIMIT")
    print(f"[Stage] Current AP_STAGE_SPEED_LIMIT = {cur_speed}%")
    if CONFIG["stage_speed_limit_pct"] is not None:
        target_speed = float(CONFIG["stage_speed_limit_pct"])
        safe_set_double(conn, "AP_STAGE_SPEED_LIMIT", target_speed)
        readback = safe_get_double(conn, "AP_STAGE_SPEED_LIMIT")
        print(f"[Stage] Set AP_STAGE_SPEED_LIMIT to {target_speed}% (readback: {readback}%)")

    for i, one_region_name in enumerate(region_names, start=1):
        run_one_region(conn, json_file, one_region_name, i, block_folder=block_folder)

    print("\nDONE.")

