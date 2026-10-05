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

import sys, os, re, json, csv, time, math, glob, subprocess, random
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
# OL current (uA) <-> focus (um) calibration
# =============================================================================
# Empirically measured 2026-09-02 by sweeping focus from -50 to +50 um and
# reading back AP_OLENS_CURRENT at each step (calibrate_ua_um.py) - linear to
# R^2=0.9999998 over that whole range: uA = 119.262*um + 0.047. Raw data and
# fit at D:\Users\Vlad\Section _detection-files\diagnostics\ua_um_calibration.csv
# / ua_um_calibration_fit.json. Lets AF's uA-native search range
# (AP_AUTOFOCUS_RANGE) be reasoned about/configured in the physically
# meaningful unit (um) instead of an opaque current value - see
# af_fine_range_um/af_coarse_range_um below. If this instrument's optics are
# ever recalibrated, update this constant from a fresh run of that script,
# not by guessing.
_OL_UA_PER_UM = 119.262

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

    # BEST POINT SELECTION FROM prev_focusmap. AF/AS confidence is
    # lower-is-better (0 = perfect, confirmed against ZEN directly):
    #   "AF"     -> min autofocus confidence
    #   "AS_SUM" -> min (as_conf_x + as_conf_y)
    #   "AF_AS"  -> min (w_af*af + w_as*(asx+asy))
    "best_point_metric": "AF",
    "w_af": 1.0,
    "w_as": 0.1,

    # Manual override: set to an sp_id (int) from prev_focusmap to force that point
    # instead of auto-selecting the best. None = auto-select (default).
    "manual_sp_id": None,

    # gates for the ONE offset measurement (current slice)
    # No longer used - AF confidence is lower-is-better (0 = perfect,
    # confirmed against ZEN), so there's no "too low to trust" threshold on
    # this scale; af_success (not-NaN, not a -4/-5 sentinel) already gates
    # validity. Left here (unused) so an old saved config that still sets
    # this doesn't error out.
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
    #   "ONE_POINT_OFFSET"        = original behavior (needs FocusMap.txt history)
    #   "THREE_POINT_DELTA"       = measure a few current-slice points and fit delta plane on top of prev plane
    #   "CURRENT_ALL_POINTS_PLANE" = measure ALL current support points and fit a brand-new plane (default -
    #                                doesn't depend on a possibly-marginal prev_focusmap history)
    #   "WAFER_FIDUCIAL_DIRECT"    = zero measurement, use the wafer-fiducial OL/StigX/StigY planes directly
    #   "SAMPLE_TOPOGRAPHY_CORRECTION" = zero per-region measurement; wafer-fiducial planes, re-leveled ONCE
    #                                    per wafer by a precheck that measures every fiducial point
    #   "FIDUCIAL_FOCUS_POINT_XY_REGION_OFFSET" = wafer-fiducial planes, re-leveled EVERY region by one fresh
    #                                    B2F -> AF -> AS measurement at that region's own (offset) support point
    #   "FOCUSMAP_TOPOGRAPHY"      = zero measurement, use this region's own FocusMap.txt plane directly
    # -------------------------------------------------
    "plane_update_method": "CURRENT_ALL_POINTS_PLANE",


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

    # -------------------------------------------------
    # MANUAL_POINTS plane update (issue #8)
    # When plane_update_method == "MANUAL_POINTS": ignore the JSON/auto support
    # points and use operator-placed points (with optional hand-set focus/stig)
    # from an external file (schema: see load_manual_focus_points). OFF by
    # default (file=None) so every other plane_update_method is untouched.
    #   manual_focus_mode:
    #     "SKIP_AF" -> apply the operator's OL (+stig) verbatim, no AF/AS
    #     "SEED_AF" -> seed OL with the operator's value, then AF/AS refine
    # A point with no operator ol_uA is placement-only (AF/AS measures it there)
    # regardless of mode. Contour/tiles still come from the imaging JSON; a prev
    # focus-map is still required (initial OL + stig fallback).
    # -------------------------------------------------
    "manual_focus_points_file": None,
    "manual_focus_mode": "SKIP_AF",
    "manual_focus_seed_range_uA": 300,

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
    "stage_settling_time_ms": 200,
    "stage_speed_limit_pct": None,
    "move_z_to_json_supportpoint": False,
    # After a commanded Z move, warn (not abort) if AP_STAGE_AT_Z ends up more
    # than this far from the target - a Z that didn't actually get there (stage
    # limit, homing/calibration issue, wrong value/unit) puts the sample outside
    # the beam's focus range, which otherwise just shows up later as an opaque
    # B2F/AF failure with no indication Z was the cause.
    "z_move_warn_tolerance_um": 5.0,

    # Mechanical Z backlash compensation, applied in _safe_goto_z (the single
    # choke point every Z move in this file routes through) - whenever
    # enabled, every commanded Z move first overshoots past the target by
    # z_backlash_um in the direction OPPOSITE the preferred approach, waits
    # for that to settle, then makes the final approach into the real target
    # from the preferred direction - so the last bit of travel always takes
    # up the mechanical slack the same way, regardless of which direction the
    # move came from. Most exposed by minimize_ol_lens_zen_style's
    # "try both signs" logic, which reverses Z direction repeatedly within
    # seconds. z_backlash_um is an UNVERIFIED PLACEHOLDER, not a measured
    # value - defaults to disabled until it's been characterized on real
    # hardware and tuned; enabling it with a wrong value could make
    # positioning worse, not better.
    "z_backlash_enabled": False,
    "z_backlash_um": 5.0,
    "z_backlash_approach_from_below": True,  # final move is +Z (upward) into the target

    # initial OL — what to set AP_OLENS_CURRENT to at the start of each region
    #   "PREV_PLANE_AT_FIRST_SP"           -> evaluate prev OL plane at sps[0] x,y;
    #                                          AF starts close to truth, way faster + more reliable
    #   "WAFER_FIDUCIAL_PLANE" (default)   -> evaluate the wafer-wide fiducial OL plane (see
    #                                          wafer_fiducial_ol_file below) at sps[0] x,y instead -
    #                                          works even for a region with no prev_focusmap history.
    #                                          Falls back to FIXED (initial_ol_uA below) if
    #                                          wafer_fiducial_ol_file isn't set/usable.
    #   "READ_CURRENT"                     -> don't reset; let it carry over from previous region
    #   "FIXED" / "DEFAULT"                -> use the hard-coded `initial_ol_uA` value below
    "initial_ol_mode": "WAFER_FIDUCIAL_PLANE",
    "initial_ol_uA": 3000.0,        # only used when initial_ol_mode == "FIXED"

    # Z-search: one-time per-region physical stage Z correction using a FRESH,
    # real AF measurement (not a stale calibration file) - see
    # run_z_search_correction(). Rationale: dz_too_large rejections mean the
    # CONVERGED OL sits far from the scope's OL~=0 reference; no amount of
    # better OL seeding changes that, since it's the true focal position, not
    # a starting guess. But AP_DZ_FROM_OLENS is itself already a Z-equivalent
    # (um) offset, so one scouting AF can estimate how far off the current
    # stage Z is and physically move Z to compensate, before the real
    # per-point measurements run. Off by default; only meaningful alongside
    # move_z_to_json_supportpoint=True (otherwise there's no per-region Z
    # baseline to correct in the first place).
    "z_search_enabled": False,
    "z_search_max_correction_um": 200.0,  # safety clip - refuse a bigger correction than this
    "z_search_min_correction_um": 2.0,    # skip applying a correction this small - not worth it
    # Sign of the applied correction relative to the measured dz. Unverified
    # against real hardware - if the verification AF logged right after
    # applying the correction shows dz got WORSE (not smaller), flip this.
    "z_search_dz_sign": -1.0,

    # Wafer-wide fiducial OL plane: a handful of (x, y, z, ol) points spanning the
    # WHOLE wafer (see fit_ol_plane.py), fit once into OL = a*x + b*y + c. Unlike
    # prev_focusmap (which only covers points a prior run already measured), this
    # gives an OL estimate for ANY (x, y) on the wafer - used as the per-point seed
    # in measure_plane_update_points() (CURRENT_ALL_POINTS_PLANE / THREE_POINT_DELTA,
    # which otherwise have no per-point seed at all) and as an initial_ol_mode option
    # above. It never overrides a prev_focusmap-derived seed where one exists (that's
    # real point-specific history, strictly more precise than a wafer-wide model).
    # If left unset (None), auto-discovered once at startup from
    # wafer_fiducial_default_dir - see resolve_wafer_fiducial_file(). Set this
    # explicitly to skip auto-discovery and use a specific file every time.
    "wafer_fiducial_ol_file": None,
    "wafer_fiducial_default_dir": r"D:\Users\Vlad\Section _detection-files",

    # Automatic end-of-run diagnostics (see focus_diagnostics.py): an
    # interactive HTML comparing expected-vs-measured OL and stage Z for
    # every tile actually imaged this run, written once after the region
    # loop finishes (success or partial failure) so an operator can open one
    # file and see whether anything looks wrong - built after the
    # w10h31_220526 Z-fault had to be reconstructed by hand from raw
    # metadata.txt/console.log files days after the fact. Best-effort: never
    # raises, never blocks (no interactive window - writes a file and
    # returns), so it's safe to leave on for every run.
    "focus_diagnostics_enabled": True,
    "focus_diagnostics_dir": r"D:\Users\Vlad\Section _detection-files\diagnostics",

    # Startup Z sanity check (see check_and_correct_z_from_fiducial): before
    # any focusing starts, compare the stage's CURRENT physical Z to the
    # wafer fiducial plane's Z estimate at this region's first support point.
    # If the stage is sitting more than z_fiducial_check_threshold_um BELOW
    # that estimate (e.g. left at Z=0 from a fresh connection/homing, nowhere
    # near the real working height), move up to the fiducial estimate first -
    # OL/AF/AS can only correct small (um-scale) defocus, not a stage at the
    # wrong ABSOLUTE height (mm-scale), which is what caused every AF/B2F
    # failure in a region to cascade once the stage was left at Z=0.
    # One-directional by design: does not correct an over-shoot (Z already
    # above the estimate) - this is a "move closer" safety guard, not a
    # calibration. No-ops if no wafer fiducial data is available.
    "z_fiducial_check_enabled": True,
    "z_fiducial_check_threshold_um": 800.0,  # 0.8 mm

    # _abort_if_stage_z_unsafe: how hard to try before actually sacrificing a
    # region for an unsafe (near-0) stage Z. Real runs show this reading is
    # sometimes a lone glitch, sometimes persists for a few seconds with no
    # Z command issued at all, and just as often clears up on its own a
    # moment later with nothing having corrected it - worth spending a few
    # retries (and, where support points are available, one corrective move
    # to the wafer-fiducial Z estimate) before giving up on a whole region.
    "stage_z_unsafe_check_retries": 3,
    "stage_z_unsafe_check_retry_delay_s": 0.5,

    # Separate, much lower retry budget for the checks that no longer GATE
    # anything (main_live_qc.py's per-tile pre-trigger check now images
    # anyway; run_af_once's Z-drift guard and measure_topography_check_point's
    # landing check now substitute the known-good value and continue - see
    # _z_looks_like_telemetry_glitch). A run-wide check (2026-09-04) found
    # retrying never actually distinguishes a real problem from this
    # instrument's chronic AP_STAGE_AT_Z glitch - even 3/3 retries can all
    # come back glitched - so for these three call sites the retries bought
    # nothing but wall-clock time: every tile that hit the glitch cost ~13s
    # on average vs ~2s for a clean tile (45% of tiles in one real run), and
    # each retry appears to cost several real seconds itself, not just the
    # deliberate retry_delay_s sleep (see the call-timing prints on
    # get_stage_z_stable). 0 retries = trust the first read's glitch/clean
    # determination immediately. stage_z_unsafe_check_retries above is
    # UNCHANGED and stays more cautious - _abort_if_stage_z_unsafe still
    # actually aborts a whole region on a confirmed-bad Z, a much bigger
    # consequence than these per-tile/per-point checks now carry.
    "stage_z_glitch_check_retries": 0,
    "stage_z_glitch_check_retry_delay_s": 0.15,

    # Per-point Z alignment before seeding OL from the wafer fiducial plane
    # (see _measure_plane_update_points_loop): the OL plane is only valid AT
    # the Z height its fiducial markers were measured at (every marker in
    # w10h31_fiducial_markers.txt shares one exact Z - it doesn't describe
    # wafer tilt, it's a single calibration height). If the stage has since
    # drifted (or was left) away from that height, the OL seed evaluated
    # there is wrong by roughly (actual Z - fiducial Z) * the instrument's
    # focus sensitivity - this was the actual cause of S4R1's point 0 landing
    # ~33um out of focus and needing an expensive coarse-AF + minimize-OL
    # rescue. Unlike z_fiducial_check_* above (a coarse, mm-scale, one-way,
    # once-per-region safety net), this moves Z to match the fiducial
    # estimate EVERY support point, both directions, right before seeding OL -
    # cheap (one stage move, reusing the XY move already needed for that
    # point) compared to the rescue it prevents.
    "wafer_fiducial_z_align_enabled": True,

    # SAMPLE_TOPOGRAPHY_CORRECTION-only, once per wafer before the first
    # region: re-measure a handful of real wafer-fiducial points fresh
    # (AF/AS/B2F) and shift the fiducial OL/StigX/StigY/ProjX/ProjY/Z planes
    # by the single (median, MAD-filtered) offset between what was measured
    # and what the as-fitted planes predicted there - see
    # precheck_and_correct_wafer_fiducial_planes(). The scope's calibration
    # drifts between whenever the fiducial file was built (from ZEN
    # screenshots) and whenever it's actually used, so this catches an
    # overall level shift without needing a fresh screenshot capture every
    # time. Intercept-only correction: slope/tilt from the original fit is
    # never touched, since 3-4 points is not a reliable basis for re-deriving
    # tilt, only for catching a level shift.
    "topography_precheck_enabled": True,
    # Measure every usable point in the wafer-fiducial file, not a small
    # subset - a freshly-measured OL plane fit from N points has the same
    # statistical basis as the original fiducial plane's own fit (also N
    # points), so it can safely carry a real slope/tilt instead of only an
    # intercept-level offset (which is all 3-4 points could support - see
    # precheck_and_correct_wafer_fiducial_planes). _select_topography_precheck_points
    # clamps this to however many complete rows the file actually has, so a
    # large sentinel here just means "all of them" regardless of file size.
    "topography_precheck_n_points": 9999,
    # Repeatedly running AF/AS/B2F at the exact same physical spot damages/
    # bleaches that tissue - a real concern once this precheck measures
    # every fiducial point (instead of 3-4) and gets run repeatedly across
    # many test runs against the same fiducial file. Each precheck point is
    # offset by this distance, in a random direction picked fresh per point
    # per run, so repeated runs (and repeated points within one run) don't
    # keep landing on the same already-damaged tissue. 110.0 um = 1 MFOV
    # diameter for this 91-beam instrument (see mfov_diameter elsewhere in
    # the codebase) - small enough that the wafer-fiducial planes (fit over
    # a much larger span) are still valid at the offset position, large
    # enough to land on genuinely different tissue. Set to 0.0 to disable.
    # Also reused by plane_update_method=FIDUCIAL_FOCUS_POINT_XY_REGION_OFFSET for the
    # exact same reason, to offset its one-point-per-region measurement.
    "topography_precheck_tissue_xy_offset_um": 110.0,
    # A properly fiducial-seeded AF should land close to real focus - per the
    # user: the fiducial values should already be close, so needing
    # minimize_ol_lens_zen_style's iterative Z-nudge loop at all is itself a
    # sign something is off, not routine. measure_topography_check_point no
    # longer calls it: if AF's own post-convergence AP_DZ_FROM_OLENS (um)
    # exceeds this, it does ONE direct stage Z move (same sign convention as
    # z_search_dz_sign) and re-checks AF once, instead of looping.
    "topography_focus_correction_threshold_um": 20.0,

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

    # B2F/AF "black image" triage: when B2F comes back with nothing usable
    # after its normal retries (the practical signal for a black/no-signal
    # image - CMD_BEAMS_TO_FIBERS itself needs a live beam to find fiber
    # positions, so a hard B2F failure IS what a black image looks like
    # through this API), run the same triage an operator would do at the
    # scope: is Z sane? -> is the beam actually on? -> does the service log
    # show a beam-clearance-failure (arc) in the last N seconds? If an arc is
    # confirmed, recover per beam_arc_recovery_mode and retry once more.
    "beam_issue_recovery_enabled": True,
    "beam_issue_log_lookback_s": 120.0,   # only react to an arc this recent

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
    # No longer used by is_success_conf() - AF/AS confidence is lower-is-better
    # (0 = perfect, confirmed against ZEN), so "success" is just "not NaN and
    # not one of the documented failure sentinels" (see
    # AF_AS_CONFIDENCE_FAILURE_SENTINELS), not a magnitude threshold. Left
    # here (unused) so an old saved config that still sets this doesn't error out.
    "af_success_confidence_min": 1e-9,
    "af_fine_mode": 1,
    "af_fine_scan_rate": 4,
    # Number of measurement steps AF samples across its search range (native
    # API name: DP_AUTOFOCUS_SUPPORTPOINTS) - called "sampling_points" here,
    # not "support_points", to avoid confusion with FocusMap.txt support
    # points (actual x,y stage positions - a completely different concept).
    # 3 matches ZEN's own stock AF default (confirmed against ZEN directly).
    "af_fine_sampling_points": 3,
    "af_fine_pixel_size_nm": 10.0,
    # af_fine_range_um is the physically meaningful view (see _OL_UA_PER_UM);
    # af_fine_range_uA is what AP_AUTOFOCUS_RANGE actually takes - and per the
    # Zeiss API doc itself (20200821_MultiSEM_API_17_V2-4.pdf, section 4.3.3):
    # "The main lens current Iol will be varied from Iol - range to Iol +
    # range" - i.e. this value already IS the +/- half-width, not a full span
    # to be halved. Both are written out as plain numbers (not one computed
    # from the other) because CONFIG is parsed as a literal dict by
    # tests/test_config_defaults.py (avoids importing focus_map's
    # Windows-only comtypes dependency) - if you change one, recompute and
    # update the other by hand.
    # +/-20 um = ~2385 uA at 119.262 uA/um.
    "af_fine_range_um": 20.0,
    "af_fine_range_uA": 2385,

    "af_coarse_mode": 2,
    "af_coarse_scan_rate": 4,
    "af_coarse_sampling_points": 20,
    "af_coarse_pixel_size_nm": 10.0,
    # Same "both written out, kept in sync by hand" reasoning as
    # af_fine_range_um/af_fine_range_uA above (also a +/- half-width, per the
    # same API doc section). +/-50 um = ~5963 uA at 119.262 uA/um - well
    # under the API's own 10000 uA ceiling (+/-83.85 um), so this is a
    # deliberate choice, not a hardware limit.
    "af_coarse_range_um": 50.0,
    "af_coarse_range_uA": 5963,

    # minimize-OL
    "minimize_ol_enabled": True,
    # Matches the requested working-distance tolerance of 1.4mm +/-10um.
    "dz_limit_um": 10.0,
    "minimize_ol_max_iters": 6,
    "minimize_ol_try_both_signs": True,
    "minimize_ol_rerun_af_each_iter": True,
    # Safety clip on a single Z-nudge computed from AP_DZ_FROM_OLENS - added
    # after a real incident where a coarse-AF-derived dz (fine AF had failed)
    # was wildly wrong and minimize_ol_lens_zen_style applied it uncapped,
    # driving the stage toward Z=0 (only _safe_goto_z's hard floor stopped it
    # from actually reaching 0). This function is meant for small (~dz_limit_um)
    # fine-tune nudges; needing a correction bigger than this in one step means
    # the seed OL/AF result upstream is wrong, not that the nudge is legitimate.
    # Same clip value as z_search_max_correction_um by default, but a separate
    # key since the two corrections serve different call sites.
    "minimize_ol_max_z_correction_um": 200.0,

    # AS
    "as_enabled": True,
    "as_only_if_af_success": True,
    "as_scan_rate": 4,
    # Same "sampling_points" naming as af_fine/af_coarse above (native API
    # name: DP_AUTOSTIG_SUPPORTPOINTS) - not "support_points", to avoid
    # confusion with FocusMap.txt support points (x,y stage positions).
    # 7 sampling points / range 2.5 match ZEN's own stock AS defaults
    # (confirmed against ZEN directly).
    "as_sampling_points": 7,
    "as_pixel_size_nm": 10.0,
    "as_range": 2.5,

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
    # Excludes the first N tiles of EVERY region from move_s/scan_s/storage_s/
    # tile_times (see main_live_qc.py:574 and focus_map.py's own acquire_tiles
    # loop) - meant to drop pipeline-startup skew from a THROUGHPUT benchmark
    # with many tiles per region. Found set to 10 (2026-09-04) while this
    # wafer's actual HI-MC regions have only 8-12 tiles each - with
    # `order_i > warmup_tiles` never true (or true for at most 1-2 tiles),
    # move_s/scan_s/storage_s in live_qc_timing.json came out ~0.0 for nearly
    # every region, making the timing reports look like stage move/scan cost
    # nothing at all when the raw per-tile [TileWall] lines showed real,
    # sometimes multi-second costs. 0 = keep every tile's data; only raise
    # this again for a real throughput benchmark with many tiles/region.
    "speed_test_warmup_tiles": 0,

    # cmd _wait_stage_settled
    "wait_stage_settled": True,
    "no_wait_tiny_delay_ms": 200,

    # Thumbnails: the Zeiss API can generate a downsampled thumbnail for every
    # beam alongside the full-res image in the SAME acquisition call (DP_THUMBNAIL_*
    # / STR_THUMBNAIL_*), via genuine area-averaging (not naive decimation), at
    # essentially no extra acquisition time. thumbnail_scale must be a power of 2
    # in [2,64]. Default is 4 (8nm * 4 = 32nm = 0.032um/px) for a much more
    # detailed thumbnail than the old default of 64 (0.512um/px); if you need
    # the coarser wafer-wide overview section_overview_builder.py was tuned
    # for instead, set this back to 64 for that run.
    "save_thumbnails": True,
    "thumbnail_scale": 4,
    "thumbnail_filetype": "png",   # compressed, lossless -- smaller than bmp for a 4x-downsampled image

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
    asx: float  # real StigX (Zeiss calls this "Objective tilt X" - see column-mapping note)
    asy: float  # real StigY ("Objective tilt Y")
    score: float
    ol: float = 0.0  # this point's own recorded OL from the prev_focusmap row
    # Real Autostig confidence (NOT the stig values themselves - see asx/asy
    # above). Parsed but not yet used for best_point_metric scoring pending
    # confirmation of which direction (higher vs lower) means "better" for
    # this instrument's confidence metric.
    as_conf_x: float = 0.0
    as_conf_y: float = 0.0

@dataclass
class TilePlan:
    tile_idx: int
    x: float
    y: float
    z_pred: Optional[float]
    ol_pred: float
    stig_x_pred: float = 0.0
    stig_y_pred: float = 0.0
    # Predicted/expected ProjX/ProjY, from a wafer-fiducial plane fit -
    # SAMPLE_TOPOGRAPHY_CORRECTION only (see the plane_update_method dispatch).
    # AP_PROJECTIVE_DEFLECTOR_X/Y is only ever set by the scope's own
    # CMD_BEAMS_TO_FIBERS auto-alignment, never by a script - these two
    # fields are recorded for comparison/QC against that real B2F reading,
    # never commanded to hardware. None for every other plane_update_method.
    proj_x_pred: Optional[float] = None
    proj_y_pred: Optional[float] = None

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


def get_stage_z_stable(conn: MSEMConnection, retries: int = 1, retry_delay_s: float = 0.15) -> float:
    """Debounced AP_STAGE_AT_Z read.

    Real logs show single readings of exactly 0.000 um sandwiched between
    otherwise-identical readings of the true position (e.g. 38873.164 um
    before AND after, unchanged to three decimals), during plain tile
    acquisition with NO Z stage command issued at all. A real ~38.9mm
    mechanical excursion and return within one tile's ~1.5s scan, with zero
    commanded move, no settling delay, and no effect on the captured image,
    is not physically plausible - this is a telemetry/readback glitch on
    this instrument (or the comms layer under it), not an actual physical
    stage position. Whenever a fresh read looks like that glitch (clearly
    below any real imaging height on this wafer), re-read once before
    trusting it. A SECOND consecutive bad reading is treated as real and
    returned as-is - this only filters a lone bad sample, it never masks a
    persistent problem.

    call_s (2026-09-04): wall-clock time of just THIS get_double() call is
    timed and printed on every retry - a real run showed every glitch-hit
    tile costing 15-20s of otherwise-unaccounted-for wall time (not visible
    in move_s/scan_s/storage_s at all), far more than retries*retry_delay_s
    (e.g. 3*0.5=1.5s) could explain. Hypothesis: the underlying COM/API call
    itself runs slow during a glitch episode, not just returning a wrong
    value quickly - this instrumentation exists to confirm that with real
    numbers instead of inferring it from the gap between print statements."""
    _t0 = time.perf_counter()
    z = safe_get_double(conn, "AP_STAGE_AT_Z", default=float("nan"))
    call_s = time.perf_counter() - _t0
    n = 0
    while n < retries and z == z and z < _STAGE_Z_ABORT_THRESHOLD_UM:  # NaN-safe
        time.sleep(retry_delay_s)
        _t0 = time.perf_counter()
        z_retry = safe_get_double(conn, "AP_STAGE_AT_Z", default=float("nan"))
        retry_call_s = time.perf_counter() - _t0
        print(f"[StageZ] first read {z:.3f} um looked like a telemetry glitch (below "
              f"{_STAGE_Z_ABORT_THRESHOLD_UM:.0f} um, call took {call_s:.3f}s) - "
              f"re-read: {z_retry:.3f} um (call took {retry_call_s:.3f}s)")
        z = z_retry
        call_s = retry_call_s
        n += 1
    return z

def set_first_working_double(conn: MSEMConnection, candidates: List[str], value: float) -> Tuple[bool, Optional[str]]:
    for k in candidates:
        if safe_set_double(conn, k, value):
            return True, k
    return False, None

def beam_list_to_str(beams: List[int]) -> str:
    return ", ".join(str(b) for b in beams)

# Per the MultiSEM API doc (AP_AUTOFOCUS_CONFIDENCE / AP_AUTOSTIG_CONFIDENCE_X/Y):
#   >0  successful, better confidence if smaller
#   -1  not successful, not enough features on sample
#ck   -2  not successful, upper limit of search range reached
#   -3  not successful, lower limit of search range reached
#   -4  not successful, inconsistent focus/stigmation fit
# -5 isn't actually a documented confidence code (it's the generic
# Set_PassedType* "value out of range" return, unrelated to this field) but
# is kept here too since a real confidence reading would never legitimately
# land on exactly -5.0, so including it is harmless insurance.
AF_AS_CONFIDENCE_FAILURE_SENTINELS = (-1.0, -2.0, -3.0, -4.0, -5.0)

# Human-readable meaning per failure code, for annotating saved FocusMap.txt
# rows (see save_measured_focusmap_txt) so a reported error code is
# self-explanatory without looking up the API doc.
AF_AS_CONFIDENCE_FAILURE_MEANINGS = {
    -1.0: "not enough features on sample",
    -2.0: "upper limit of search range reached",
    -3.0: "lower limit of search range reached",
    -4.0: "inconsistent fit",
    -5.0: "out-of-range (generic Set_PassedType* failure, not a documented AF/AS code)",
}


def is_success_conf(conf: Optional[float]) -> bool:
    """A real measurement was obtained. Zeiss AF/AS confidence is a
    lower-is-better error metric (0 = perfect focus/stig; confirmed against
    ZEN directly), NOT a probability-style score where higher is better - so
    success means "not NaN and not one of the documented failure sentinels",
    regardless of magnitude. A confidence of exactly 0 is the BEST possible
    result, not a failure - this used to require conf > af_success_confidence_min
    (1e-9), which rejected the best measurements and forced an unnecessary
    coarse-AF fallback whenever fine AF nailed it."""
    return conf is not None and conf == conf and conf not in AF_AS_CONFIDENCE_FAILURE_SENTINELS

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

def stage_move_xy_z(conn: MSEMConnection, x: float, y: float, z: Optional[float], verify_z: bool = True):
    conn.set_double("AP_STAGE_SETTLING_TIME", CONFIG["stage_settling_time_ms"])
    conn.set_double("AP_STAGE_GOTO_X", x)
    conn.set_double("AP_STAGE_GOTO_Y", y)
    if z is not None:
        _safe_goto_z(conn, z, context="stage_move_xy_z")

    if CONFIG.get("wait_stage_settled", True):
        rc = conn.execute("CMD_WAIT_STAGE_SETTLED")
        if rc != 0:
            raise RuntimeError(f"CMD_WAIT_STAGE_SETTLED rc={rc} err={safe_get_last_error(conn)}")
    else:
        tiny_ms = int(CONFIG.get("no_wait_tiny_delay_ms", 0) or 0)
        if tiny_ms > 0:
            time.sleep(tiny_ms / 1000.0)

    if not verify_z:
        # Caller (FIDUCIAL_FOCUS_POINT_XY_REGION_OFFSET, 2026-09-04) wants
        # ZERO Z reads for this method - running msem_control.main and ZEN
        # simultaneously is suspected of causing the AP_STAGE_AT_Z read
        # glitches characterized this session (Zeiss API doc 3.1,
        # "Simultaneous use with ZEN software": the two don't coordinate
        # access to the same hardware), and this method never has any real
        # need to verify Z once seeded - see measure_topography_check_point's
        # skip_z docstring.
        return

    # Read back the CURRENT stage Z after EVERY move, not just moves that
    # explicitly commanded a Z target - most moves in this pipeline pass
    # z=None (leave Z alone) and previously never re-checked it at all, so a
    # stage that drifts/settles back toward a low/parked Z between such moves
    # went completely unnoticed until whatever ran next (AF/B2F) failed with
    # no obvious cause. Printed as one parseable line so the GUI can watch it
    # live (see PipelineRunner's stage_z signal / HiMcPage's low-Z alert).
    #
    # retries=0 (stage_z_glitch_check_retries, 2026-09-04): this read is
    # purely informational/diagnostic here (a print + a WARNING log if it
    # disagrees with a commanded target - neither aborts or changes
    # anything), called after EVERY stage move in the whole pipeline
    # (including every inter-tile move during acquisition). A run showed the
    # underlying AP_STAGE_AT_Z read itself blocks for a hard, extremely
    # consistent ~4.002s when it's about to return the known telemetry-
    # glitch value (202/202 slow calls measured within 4.001-4.005s of each
    # other - looks like a fixed COM/driver-level timeout, not noise) - the
    # default retries=1 here was silently doubling that cost on top of the
    # per-tile checks that already dropped their own retries for the same
    # reason. Nothing here uses the retry to make a decision, so there's no
    # reason to pay for more than one read.
    z_actual = get_stage_z_stable(
        conn,
        retries=int(CONFIG.get("stage_z_glitch_check_retries", 0)),
        retry_delay_s=float(CONFIG.get("stage_z_glitch_check_retry_delay_s", 0.15)),
    )
    if z_actual == z_actual:  # NaN-safe
        print(f"[StageZ] current Z={z_actual:.3f} um")

    if z is not None:
        tol = float(CONFIG.get("z_move_warn_tolerance_um", 5.0))
        if z_actual == z_actual and abs(z_actual - z) > tol:  # NaN-safe (NaN != NaN)
            direction = "LOWER than" if z_actual < z else "HIGHER than"
            print(
                f"WARNING: [Stage] Z did not reach the commanded position - stage Z is "
                f"{direction} requested (target={z:.3f}, actual={z_actual:.3f}, "
                f"diff={z_actual - z:+.3f}). The sample is likely outside the beam's focus "
                f"range at this height, which is why B2F/AF calls fail right after this with "
                f"no other explanation - check for a stage limit, a homing/calibration issue, "
                f"or a wrong Z value/unit before retrying."
            )

def run_b2f(conn: MSEMConnection) -> Tuple[int, float, float]:
    rc = conn.execute("CMD_BEAMS_TO_FIBERS")
    proj_x = safe_get_double(conn, "AP_PROJECTIVE_DEFLECTOR_X")
    proj_y = safe_get_double(conn, "AP_PROJECTIVE_DEFLECTOR_Y")
    return rc, proj_x, proj_y

def diagnose_and_recover_beam_issue(conn: MSEMConnection, sp: "SupportPoint") -> bool:
    """Triage a B2F failure that survived its normal retries.

    This is the practical stand-in for "is the image all black?": there's no
    lightweight live-pixel readback in this API (the live image only renders
    in ZEN), but CMD_BEAMS_TO_FIBERS itself needs a live beam to find fiber
    positions, so a hard B2F failure after retries IS what a black/no-signal
    image looks like through the API we actually have.

    Runs the same triage an operator would do at the scope:
      1. Is the stage Z sane? (vs. the wafer-fiducial estimate at this point -
         if the sample is at the wrong absolute height, beam-on won't fix
         B2F/AF, so don't even guess "beam issue" here.)
      2. Is the beam actually on (DP_IS_BEAM_ON)?
      3. If it's off, does the live mSEM service log show a beam-clearance
         failure (arc) in the last beam_issue_log_lookback_s seconds?
      4. If one is found, recover per beam_arc_recovery_mode (same policy the
         per-tile BeamArcGuard uses) and report success so the caller retries.

    Returns True only when it found + handled a confirmed arc (safe to
    retry). Returns False for every other cause, with a log line explaining
    why - those need a human, not an automatic retry.
    """
    if not CONFIG.get("beam_issue_recovery_enabled", True):
        return False

    print(f"\n[BeamIssue] sp[{sp.index}]: B2F found nothing usable after retries - "
          f"running black-image triage (Z sanity -> beam-on -> service log)...")

    # 1. Z sanity - same wafer-fiducial estimate check_and_correct_z_from_fiducial uses.
    fiducial_z = estimate_z_from_wafer_fiducial(sp.x, sp.y)
    current_z = get_stage_z_stable(conn)
    threshold_um = float(CONFIG.get("z_fiducial_check_threshold_um", 800.0))
    if fiducial_z is not None and current_z == current_z:  # NaN-safe
        diff = current_z - fiducial_z
        print(f"[BeamIssue] Z check: current={current_z:.1f} um, fiducial={fiducial_z:.1f} um, "
              f"diff={diff:+.1f} um (threshold={threshold_um:.1f} um)")
        if abs(diff) > threshold_um:
            print("[BeamIssue] Z is off by more than the threshold - this looks like a stage/"
                  "height problem, not a beam issue. Not attempting beam recovery; check for "
                  "a stage limit, homing, or a wrong Z value before retrying.")
            return False
    else:
        print("[BeamIssue] Z check: no wafer-fiducial Z plane or unreadable current Z - "
              "skipping (can't rule out a stage/height problem this way).")

    # 2. Is the beam actually on?
    beam_on = safe_get_double(conn, "DP_IS_BEAM_ON", default=float("nan"))
    print(f"[BeamIssue] DP_IS_BEAM_ON={beam_on}")
    if beam_on >= 0.99:
        print("[BeamIssue] Beam reports ON - this isn't an arc/beam-off condition; "
              "not attempting recovery.")
        return False

    print("[BeamIssue] Beam is OFF - checking the mSEM service log for a beam-clearance "
          "failure (arc)...")

    # 3. Check the live service log for a recent arc.
    try:
        from beam_arc_monitor import find_active_log, scan_log_file
    except Exception as e:
        print(f"[BeamIssue] beam_arc_monitor unavailable ({e}) - can't check the service log.")
        return False

    log_dir = CONFIG.get("beam_arc_log_dir", r"C:\ProgramData\CarlZeiss\mSEM")
    log_path = find_active_log(log_dir)
    if not log_path:
        print(f"[BeamIssue] no mSEM service log found in {log_dir} - beam is off for an "
              f"unknown reason. This needs an operator, not an automatic retry.")
        return False

    lookback_s = float(CONFIG.get("beam_issue_log_lookback_s", 120.0))
    min_outputs = int(CONFIG.get("beam_arc_min_outputs", 2))
    try:
        arcs = scan_log_file(log_path, min_outputs=min_outputs)
    except Exception as e:
        print(f"[BeamIssue] failed to read {log_path}: {e}")
        return False

    now_ms = time.time() * 1000.0
    recent = [a for a in arcs if 0 <= (now_ms - a.ts_ms) <= lookback_s * 1000.0]
    if not recent:
        print(f"[BeamIssue] beam is OFF but no beam-clearance-failure event found in "
              f"{log_path} in the last {lookback_s:.0f}s - this may be a different issue "
              f"(operator beam-off, comms fault, etc.). Needs manual attention.")
        return False

    arc = recent[-1]
    print(f"[BeamIssue] {arc.describe()}")

    # 4. Confirmed arc - recover per the configured policy (same policy
    # BeamArcGuard.handle_if_arc uses for arcs caught between tiles).
    recovery_mode = str(CONFIG.get("beam_arc_recovery_mode", "guided"))
    if recovery_mode == "flag_only":
        print("[BeamIssue] recovery_mode=flag_only - reporting only, NOT attempting beam-on.")
        return False

    try:
        from beam_arc_recovery import recover_from_arc, wait_for_beam_restored
    except Exception as e:
        print(f"[BeamIssue] beam_arc_recovery unavailable ({e}) - can't recover automatically.")
        return False

    if recovery_mode == "auto":
        res = recover_from_arc(conn, log=print)
    else:  # "guided" (default)
        res = wait_for_beam_restored(conn, log=print)

    print(f"[BeamIssue] recovery {'OK' if res.ok else 'FAILED'} - {res.detail}.")
    return bool(res.ok)

def run_b2f_robust(conn: MSEMConnection, sp: Optional["SupportPoint"] = None) -> Tuple[bool, int, float, float]:
    max_retries = int(CONFIG.get("b2f_max_retries", 2))
    hexfit_thr = float(CONFIG.get("b2f_hexfit_threshold", 100.0))

    def _attempts() -> Tuple[bool, int, float, float]:
        rc, px, py = 0, float("nan"), float("nan")
        for attempt in range(1, max_retries + 1):
            rc, px, py = run_b2f(conn)
            if rc != 0:
                # CMD_BEAMS_TO_FIBERS rc is an opaque Zeiss API error code with no
                # meaning of its own - always pull STR_LAST_ERROR too, otherwise a
                # real hardware/optics fault (vs. e.g. a transient comms hiccup)
                # is indistinguishable from the log alone.
                err = safe_get_last_error(conn)
                print(f"  [B2F] attempt {attempt}/{max_retries} rc={rc} (failed)"
                      f"{f' err={err}' if err else ''}")
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
        return False, rc, px, py

    ok, rc, px, py = _attempts()
    if ok or sp is None:
        return ok, rc, px, py

    if diagnose_and_recover_beam_issue(conn, sp):
        print("  [B2F] beam recovered - retrying B2F...")
        ok, rc, px, py = _attempts()

    return ok, rc, px, py

def run_af_once(conn: MSEMConnection, mode: int, scan_rate: int, sampling_points: int, pixel_nm: float, rng_uA: float):
    # sampling_points -> DP_AUTOFOCUS_SUPPORTPOINTS (the native API's own
    # name for this parameter; ours is renamed to avoid confusion with
    # FocusMap.txt support points, which are x,y stage positions).
    conn.set_double("DP_AUTOFOCUS_MODE", mode)
    conn.set_double("DP_AUTOFOCUS_SCANRATE", scan_rate)
    conn.set_double("DP_AUTOFOCUS_SUPPORTPOINTS", sampling_points)
    conn.set_double("AP_AUTOFOCUS_PIXELSIZE", pixel_nm)
    conn.set_double("AP_AUTOFOCUS_RANGE", rng_uA)

    # Z-drift guard around CMD_AUTOFOCUS: per the Zeiss API doc (CMD_AUTOFOCUS
    # varies "main lens current" - the electronic OL - across sub-images; no
    # mention of stage movement), this command should never touch
    # AP_STAGE_AT_Z. An earlier version of this comment claimed real logs
    # had "confirmed" CMD_AUTOFOCUS actually does move the stage on this
    # instrument - a run-wide check (2026-09-04) undercut that: every single
    # occurrence found (across multiple runs, both here and in
    # measure_topography_check_point's landing check below) was EXACTLY
    # 0.000 um paired with EXACTLY the correct working Z, in either
    # direction, never any other magnitude. A real physical excursion would
    # vary run to run; bit-for-bit identical numbers every time is the
    # signature of the chronic AP_STAGE_AT_Z telemetry/readback glitch (see
    # _z_looks_like_telemetry_glitch/get_stage_z_stable above) - not a
    # genuine stage move. That glitch can persist through several retries in
    # a row (confirmed 3/3 in one real occurrence even with the stronger
    # debounce below), so retries alone don't fully solve it.
    #
    # Per the operator (2026-09-04): since we already know CMD_AUTOFOCUS
    # cannot move the stage, a glitched reading on ONE side of the pair
    # (before or after) doesn't need to invalidate an otherwise-good AF
    # result - just assume the glitched side actually equals the other,
    # clean side and carry on. Only a genuine disagreement between two
    # CLEAN (non-glitch-pattern) readings is treated as a real excursion -
    # something no run has ever actually produced, but the safety net (force
    # failure + restore Z) stays in place in case it ever does.
    _z_guard_retries = int(CONFIG.get("stage_z_glitch_check_retries", 0))
    _z_guard_delay = float(CONFIG.get("stage_z_glitch_check_retry_delay_s", 0.15))
    z_before_af = get_stage_z_stable(conn, retries=_z_guard_retries, retry_delay_s=_z_guard_delay)
    rc = conn.execute("CMD_AUTOFOCUS")
    z_after_af = get_stage_z_stable(conn, retries=_z_guard_retries, retry_delay_s=_z_guard_delay)
    conf = safe_get_double(conn, "AP_AUTOFOCUS_CONFIDENCE", default=float("nan"))

    before_glitched = _z_looks_like_telemetry_glitch(z_before_af)
    after_glitched = _z_looks_like_telemetry_glitch(z_after_af)
    if before_glitched != after_glitched:
        good_z, bad_z = (z_after_af, z_before_af) if before_glitched else (z_before_af, z_after_af)
        print(f"[AF] stage Z readback {bad_z:.3f} um on one side of CMD_AUTOFOCUS (mode={mode}) "
              f"looks like the known telemetry glitch (the other side read {good_z:.3f} um, and "
              f"CMD_AUTOFOCUS never moves the stage) - assuming Z stayed at {good_z:.3f} um and "
              f"trusting this AF result instead of forcing a failure.")
        z_before_af = z_after_af = good_z
    elif before_glitched and after_glitched:
        # Both sides glitched (e.g. 0.000 -> 0.000) - can't cross-check one
        # against the other, but every characterized instance of this glitch
        # has been a bogus reading, never a real position, so there's no more
        # reason to distrust AF here than in the single-side case above.
        print(f"[AF] both stage Z readbacks around CMD_AUTOFOCUS (mode={mode}) look like the "
              f"known telemetry glitch ({z_before_af:.3f} -> {z_after_af:.3f} um) - trusting this "
              f"AF result anyway (CMD_AUTOFOCUS never moves the stage).")
    elif z_before_af == z_before_af and z_after_af == z_after_af and abs(z_after_af - z_before_af) > 1.0:
        print(f"[AF] WARNING: stage Z changed DURING CMD_AUTOFOCUS (mode={mode}): "
              f"{z_before_af:.3f} um -> {z_after_af:.3f} um (delta={z_after_af - z_before_af:+.3f} um) - "
              f"undocumented behavior; CMD_AUTOFOCUS is only specified to vary the objective "
              f"lens current, never stage Z")
        # Whatever confidence/OL this AF pass reports was measured DURING the
        # excursion (the search ran while physically at the wrong Z at some
        # point) - restoring Z afterward fixes the POSITION for whatever runs
        # next, but doesn't make this pass's own result trustworthy. Force it
        # to the documented generic-failure sentinel so callers (is_success_conf)
        # correctly reject it and retry, instead of trusting a confidence
        # number that was computed at the wrong physical height.
        conf = -5.0
        if _STAGE_Z_ABORT_THRESHOLD_UM <= z_before_af <= _STAGE_Z_HARD_MAX_UM:
            print(f"[AF] restoring stage Z to its pre-AF value {z_before_af:.3f} um "
                  f"(CMD_AUTOFOCUS left it at {z_after_af:.3f} um) and treating this AF "
                  f"result as failed (conf forced to -5.0)")
            _safe_goto_z(conn, z_before_af, context="AF Z-drift restore")
            if CONFIG.get("wait_stage_settled", True):
                conn.execute("CMD_WAIT_STAGE_SETTLED")
        else:
            print(f"[AF] WARNING: pre-AF Z ({z_before_af:.3f} um) was not itself a safe value - "
                  f"cannot restore, leaving stage at {z_after_af:.3f} um; treating this AF "
                  f"result as failed (conf forced to -5.0)")

    return rc, conf

def run_af_fine_then_coarse(conn: MSEMConnection) -> Dict[str, Any]:
    ol_before = safe_get_double(conn, "AP_OLENS_CURRENT")

    rc_f, conf_f = run_af_once(
        conn,
        CONFIG["af_fine_mode"],
        CONFIG["af_fine_scan_rate"],
        CONFIG["af_fine_sampling_points"],
        CONFIG["af_fine_pixel_size_nm"],
        CONFIG["af_fine_range_uA"],
    )

    rc_c, conf_c = None, None
    if not is_success_conf(conf_f):
        rc_c, conf_c = run_af_once(
            conn,
            CONFIG["af_coarse_mode"],
            CONFIG["af_coarse_scan_rate"],
            CONFIG["af_coarse_sampling_points"],
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

# Hard safety floor, not a tunable CONFIG key: a real wafer's working Z on
# this instrument sits around 38000-39000 um (see the wafer fiducial file /
# msem/stage.py's DEFAULT_WORKING_Z), so anything this close to the stage's
# parked/home position at Z=0 is never a legitimate imaging height, no matter
# what AF/minimize-OL/AS report. Found via a real run where the stage sat at
# Z=0 for an entire region (either check_and_correct_z_from_fiducial's own
# corrective move never took, or Z drifted back to 0 sometime during the
# per-point measurement loop) while AF/OL/AS still reported a numerically
# clean convergence - the OL current alone can apparently compensate enough
# for the focus/stig confidence metrics to look fine even at the wrong
# physical height, so "AF succeeded" cannot be trusted as a proxy for "Z is
# actually sane" here.
_STAGE_Z_ABORT_THRESHOLD_UM = 1000.0


def _z_looks_like_telemetry_glitch(z: float) -> bool:
    """True if z is a NaN-safe number below the safety floor - the signature
    of the chronic AP_STAGE_AT_Z readback glitch documented on
    get_stage_z_stable() above, not a real stage position. Confirmed
    2026-09-04 across every occurrence found in a full run's logs (both here
    and in run_af_once's Z-drift guard): 100% were exactly 0.000 um paired
    with exactly the correct working Z, never any other magnitude - the
    stage never actually moves that far in the time available, this
    instrument's comms layer just occasionally hands back a stale/garbage
    zero instead of the truth."""
    return z == z and z < _STAGE_Z_ABORT_THRESHOLD_UM


# How far a corrective-move target (estimate_z_from_wafer_fiducial, which
# can carry precheck_and_correct_wafer_fiducial_planes's drift correction)
# is allowed to disagree with the known-good, never-corrected fiducial Z
# (estimate_known_good_z_from_wafer_fiducial) before it's rejected outright.
# Found via a real incident: a bad live-Z read during the once-per-wafer
# precheck baked a ~19424 um error into the drift-corrected Z plane, and
# the only check in place at the time ("is the target > 1000 um") happily
# accepted it - the corrective move then "succeeded" at the wrong height
# and 19 of 23 regions imaged completely out of focus as a result.
_FIDUCIAL_Z_SANITY_TOLERANCE_UM = 100.0


class RegionUnsafeStageZError(RuntimeError):
    """Raised when the stage Z is too close to 0 to be a real imaging height
    for this region - the caller must skip (not image) this region, however
    numerically clean AF/OL/AS looked."""


def _abort_if_stage_z_unsafe(
    conn: MSEMConnection, region_name: str, when: str,
    sps: Optional[List["SupportPoint"]] = None,
) -> None:
    """Read the live stage Z and raise RegionUnsafeStageZError if it's below
    _STAGE_Z_ABORT_THRESHOLD_UM. `when` is just a short label for the log line
    / exception message (e.g. "before focusing", "before tile acquisition")
    so it's clear which of the (possibly several) call sites caught it.

    Real runs have shown this Z=0 reading is sometimes a lone telemetry
    glitch (get_stage_z_stable's single re-read catches those) but sometimes
    persists across several seconds of re-reads with NO Z command issued by
    this code at all - and, just as often, the stage is found sitting at a
    perfectly normal height again moments later with nothing having
    corrected it. Given that, and given the cost of giving up here is an
    entire region never imaged, it's worth spending a few more seconds of
    retries - and, if `sps` is given, one deliberate corrective move to the
    wafer-fiducial Z estimate - before actually sacrificing the region."""
    retries = int(CONFIG.get("stage_z_unsafe_check_retries", 3))
    delay = float(CONFIG.get("stage_z_unsafe_check_retry_delay_s", 0.5))
    current_z = get_stage_z_stable(conn, retries=retries, retry_delay_s=delay)
    if current_z != current_z:  # NaN-safe (NaN != NaN) - can't verify, don't block on it
        return
    if current_z >= _STAGE_Z_ABORT_THRESHOLD_UM:
        return

    if sps:
        ref_sp = sps[0]
        fiducial_z = estimate_z_from_wafer_fiducial(ref_sp.x, ref_sp.y)
        known_good_z = estimate_known_good_z_from_wafer_fiducial(ref_sp.x, ref_sp.y)
        z_trustworthy = (
            fiducial_z is not None and known_good_z is not None
            and abs(fiducial_z - known_good_z) <= _FIDUCIAL_Z_SANITY_TOLERANCE_UM
        )
        if z_trustworthy:
            print(f"[ZSafetyCheck] region={region_name}: Z={current_z:.1f} um unsafe {when} "
                  f"after {retries} retr(ies) - attempting one corrective move to fiducial "
                  f"Z={fiducial_z:.1f} um (known-good {known_good_z:.1f} um) "
                  f"at xy=({ref_sp.x:.1f},{ref_sp.y:.1f}) before giving up")
            _safe_goto_z(conn, fiducial_z, context="_abort_if_stage_z_unsafe corrective retry")
            if CONFIG.get("wait_stage_settled", True):
                conn.execute("CMD_WAIT_STAGE_SETTLED")
            current_z = get_stage_z_stable(conn, retries=retries, retry_delay_s=delay)
            if current_z == current_z and current_z >= _STAGE_Z_ABORT_THRESHOLD_UM:
                print(f"[ZSafetyCheck] region={region_name}: corrective move succeeded "
                      f"(Z={current_z:.1f} um) - continuing")
                return
            if _z_looks_like_telemetry_glitch(current_z):
                # Per the operator (2026-09-04): we just moved here ourselves,
                # to a fiducial Z already validated as trustworthy above - and
                # every occurrence of this exact glitch pattern characterized
                # this session has been bogus, never a real position. Three
                # confirmed real incidents (2026-09-03/04) show this exact
                # "corrective move did not help (Z=0.0)" verification step
                # falsely sacrificing an entire region when the move almost
                # certainly succeeded. Fall back to trusting the fiducial Z
                # instead of aborting - only a genuinely different (non-
                # glitch-pattern) bad reading still gives up below.
                print(f"[ZSafetyCheck] region={region_name}: verification readback "
                      f"{current_z:.1f} um after the corrective move looks like the known "
                      f"telemetry glitch - assuming the move to Z={fiducial_z:.1f} um actually "
                      f"landed and continuing instead of aborting the region.")
                return
            print(f"[ZSafetyCheck] region={region_name}: corrective move did not help "
                  f"(Z={current_z}) - giving up")
        elif fiducial_z is None or known_good_z is None:
            print(f"[ZSafetyCheck] region={region_name}: no usable fiducial Z estimate to "
                  f"correct with - giving up")
        else:
            print(f"[ZSafetyCheck] region={region_name}: fiducial Z estimate {fiducial_z:.1f} um "
                  f"disagrees with the known-good (as-fitted, pre-drift-correction) fiducial Z "
                  f"{known_good_z:.1f} um by {abs(fiducial_z - known_good_z):.1f} um "
                  f"(> {_FIDUCIAL_Z_SANITY_TOLERANCE_UM:.0f} um tolerance) - refusing to trust "
                  f"it for a corrective move - giving up")

    raise RegionUnsafeStageZError(
        f"stage Z={current_z:.1f} um is below the {_STAGE_Z_ABORT_THRESHOLD_UM:.0f} um "
        f"safety floor {when} for region '{region_name}' (persisted through {retries} "
        f"retr(ies)" + (" and a corrective move attempt" if sps else "") + ") - this is not "
        f"a real imaging height on this wafer, regardless of what AF/OL/AS report; skipping "
        f"the region instead of imaging it at the wrong height."
    )


# Hard hardware-safety ceiling, not a tunable CONFIG key: a real wafer's
# working Z on this instrument tops out around 38000-39000 um (see
# _STAGE_Z_ABORT_THRESHOLD_UM above / the wafer fiducial file) - a commanded
# Z above this is not a legitimate imaging height and risks physically
# damaging the microscope. Unlike the low-Z floor above (only checked right
# after specific startup steps), this must be enforced on EVERY commanded Z
# move for the life of the process - see _safe_goto_z, the single choke
# point every Z-move call site in this file routes through.
_STAGE_Z_HARD_MAX_UM = 39100.0


class StageZHardLimitError(RuntimeError):
    """Raised instead of ever commanding the stage to a Z above
    _STAGE_Z_HARD_MAX_UM. A hard stop, not a clamp: a Z this high can damage
    the microscope, so this must abort the entire run - callers must NOT
    catch this the way RegionUnsafeStageZError is caught (skip one region,
    continue to the next) or a bad Z source would just get retried forever."""


def _safe_goto_z(conn: MSEMConnection, z: Optional[float], context: str = "") -> None:
    """The ONLY place in this file that should call
    conn.set_double("AP_STAGE_GOTO_Z", ...) - every Z-move call site routes
    through here so both hard bounds are actually observed for ALL stage Z
    movement (stage_move_xy_z, run_z_search_correction,
    minimize_ol_lens_zen_style), not just the common per-tile path.

    Checks BOTH directions, both hard stops (refuse before commanding the
    move, not just detect after it already happened):
      - above _STAGE_Z_HARD_MAX_UM: not a legitimate imaging height, risks
        damaging the microscope.
      - below _STAGE_Z_ABORT_THRESHOLD_UM: same floor _abort_if_stage_z_unsafe
        already treats as "not a real imaging height, whatever AF/OL/AS
        report" - added here after a real incident where
        minimize_ol_lens_zen_style applied an uncapped Z correction computed
        from a bogus/coarse-AF-derived AP_DZ_FROM_OLENS reading and drove the
        stage to Z=0 with nothing catching it until the operator noticed.
        _abort_if_stage_z_unsafe's post-move detection is now a second line
        of defense, not the only one."""
    if z is None:
        return
    if z != z:  # NaN-safe (NaN != NaN) - can't judge it, let it through as before
        conn.set_double("AP_STAGE_GOTO_Z", z)
        return

    def _assert_within_hard_bounds(zz: float) -> None:
        where = f" ({context})" if context else ""
        if zz > _STAGE_Z_HARD_MAX_UM:
            raise StageZHardLimitError(
                f"refusing to move stage Z to {zz:.1f} um{where} - exceeds the "
                f"{_STAGE_Z_HARD_MAX_UM:.0f} um hard safety ceiling. A Z this high can damage "
                f"the microscope; this is a hard stop, not a clamp - fix the underlying Z "
                f"source (wafer-fiducial data, AF/minimize-OL drift, or a manual override) "
                f"before retrying, don't just resume."
            )
        if zz < _STAGE_Z_ABORT_THRESHOLD_UM:
            raise StageZHardLimitError(
                f"refusing to move stage Z to {zz:.1f} um{where} - below the "
                f"{_STAGE_Z_ABORT_THRESHOLD_UM:.0f} um safety floor. This is not a real imaging "
                f"height on this wafer regardless of what AF/OL/minimize-OL computed; this is a "
                f"hard stop, not a clamp - fix the underlying Z source before retrying."
            )

    _assert_within_hard_bounds(z)

    # Backlash compensation (see z_backlash_enabled in CONFIG - off by
    # default, z_backlash_um is an unverified placeholder): overshoot past
    # the target in the direction OPPOSITE the preferred final approach,
    # wait for that to physically settle, THEN make the real move - so the
    # last leg of travel always takes up the mechanical slack from the same
    # direction, regardless of which way this move came from. Applied here
    # (not per-caller) so it's automatic for every Z move in the file.
    if CONFIG.get("z_backlash_enabled", False):
        backlash = abs(float(CONFIG.get("z_backlash_um", 5.0)))
        approach_from_below = bool(CONFIG.get("z_backlash_approach_from_below", True))
        overshoot = z - backlash if approach_from_below else z + backlash
        _assert_within_hard_bounds(overshoot)
        print(f"[StageZ] backlash compensation: overshoot to {overshoot:.3f} um, settle, "
              f"then approach {z:.3f} um from {'below' if approach_from_below else 'above'}")
        conn.set_double("AP_STAGE_GOTO_Z", overshoot)
        conn.execute("CMD_WAIT_STAGE_SETTLED")

    conn.set_double("AP_STAGE_GOTO_Z", z)


def check_and_correct_z_from_fiducial(
    conn: MSEMConnection, sps: List["SupportPoint"], region_name: str
) -> Optional[float]:
    """Coarse startup safety check, run BEFORE any focusing: compare the
    stage's CURRENT physical Z to the wafer-wide fiducial plane's Z estimate
    at this region's first support point. If the stage is sitting more than
    z_fiducial_check_threshold_um BELOW that estimate, move up to the
    fiducial Z (at that support point's x,y) before anything else runs.

    This exists because OL/AF/AS (including the wafer-fiducial OL seeding in
    measure_plane_update_points) only ever correct small (um-scale) defocus -
    they cannot recover from the stage sitting at the wrong ABSOLUTE height
    (e.g. left at Z=0 from a fresh connection/homing, mm-scale off), which is
    what causes AF/B2F to fail across an entire region with no other symptom.

    One-directional and coarse by design: only corrects an under-shoot low Z,
    never an over-shoot high one, and only ever once per region before the
    real per-point work starts - it is not a substitute for run_z_search_correction
    (which uses a real, fresh AF measurement) or the OL seeding that follows.

    Returns the new Z (um) if a correction was applied, else None (disabled,
    no wafer fiducial Z data, no support points, unreadable current Z, or
    already within threshold).
    """
    if not CONFIG.get("z_fiducial_check_enabled", True) or not sps:
        return None

    ref_sp = sps[0]
    fiducial_z = estimate_z_from_wafer_fiducial(ref_sp.x, ref_sp.y)
    if fiducial_z is None:
        print(f"[ZFiducialCheck] region={region_name}: no wafer-fiducial Z plane available, skipping")
        return None

    current_z = get_stage_z_stable(conn)
    if current_z != current_z:  # NaN-safe (NaN != NaN)
        print(f"[ZFiducialCheck] region={region_name}: could not read current stage Z, skipping")
        return None

    threshold_um = float(CONFIG.get("z_fiducial_check_threshold_um", 800.0))
    shortfall = fiducial_z - current_z
    print(
        f"[ZFiducialCheck] region={region_name}: current Z={current_z:.1f} um, "
        f"fiducial Z={fiducial_z:.1f} um at sp[{ref_sp.index}] "
        f"xy=({ref_sp.x:.1f},{ref_sp.y:.1f}), shortfall={shortfall:.1f} um "
        f"(threshold={threshold_um:.1f} um)"
    )

    if shortfall <= threshold_um:
        print("[ZFiducialCheck] within threshold, no correction needed")
        return None

    print(
        f"[ZFiducialCheck] current Z is {shortfall:.1f} um below the fiducial estimate "
        f"(> {threshold_um:.1f} um threshold) - moving to sp[{ref_sp.index}] "
        f"xy=({ref_sp.x:.1f},{ref_sp.y:.1f}) at fiducial Z={fiducial_z:.1f} um before focusing"
    )
    stage_move_xy_z(conn, ref_sp.x, ref_sp.y, fiducial_z)
    return fiducial_z


def run_z_search_correction(conn: MSEMConnection, sps: List["SupportPoint"], region_name: str) -> Optional[float]:
    """One-time per-region physical stage Z correction from a FRESH AF
    measurement, instead of trusting a Z value from the imaging JSON or a
    stale wafer-fiducial calibration to already be correct.

    dz_too_large rejections mean the region's true focus needs an OL far
    from the scope's OL~=0 reference - seeding a better starting OL guess
    can't fix that, since AF still converges to the same true answer either
    way. But AP_DZ_FROM_OLENS is itself already a Z-equivalent (um) offset:
    one scouting AF at this region's first support point tells us how far
    off the current stage Z is, so we can physically move Z to compensate
    and let OL land back near its nominal range for the real measurements
    that follow (see z_search_enabled in CONFIG).

    Returns the applied Z correction in um, or None if skipped (disabled,
    negligible, or the scouting measurement itself failed/was unusable).
    """
    if not CONFIG.get("z_search_enabled", False) or not sps:
        return None

    ref_sp = sps[0]
    print(f"[ZSearch] region={region_name}: scouting dz at sp[{ref_sp.index}] "
          f"xy=({ref_sp.x:.1f},{ref_sp.y:.1f})")

    z_target = ref_sp.z if CONFIG.get("move_z_to_json_supportpoint", False) else None
    stage_move_xy_z(conn, ref_sp.x, ref_sp.y, z_target)

    b2f_mode = str(CONFIG["b2f_mode"]).upper()
    if b2f_mode in ("PER_FOCUS", "ONCE"):
        run_b2f_robust(conn)

    af = run_af_fine_then_coarse(conn)
    dz = af["dz_um"]
    if dz != dz or not af["success"]:
        print(f"[ZSearch] WARNING: scouting AF failed/unusable (dz={dz}) - skipping Z correction")
        return None
    print(f"[ZSearch] scouting AF: dz_from_olens={dz:.3f} um")

    sign = float(CONFIG.get("z_search_dz_sign", -1.0))
    correction = sign * dz

    max_corr = float(CONFIG.get("z_search_max_correction_um", 200.0))
    if abs(correction) > max_corr:
        print(f"[ZSearch] WARNING: computed correction {correction:.3f} um exceeds "
              f"z_search_max_correction_um={max_corr:.1f} - clipping (if this keeps "
              f"happening, check z_search_dz_sign and whether the real offset here "
              f"is genuinely this large)")
        correction = math.copysign(max_corr, correction)

    min_corr = float(CONFIG.get("z_search_min_correction_um", 2.0))
    if abs(correction) < min_corr:
        print(f"[ZSearch] correction {correction:.3f} um below z_search_min_correction_um="
              f"{min_corr:.1f} - not worth applying")
        return None

    z_now = get_stage_z_stable(conn)
    z_new = z_now + correction
    print(f"[ZSearch] applying correction {correction:+.3f} um: Z {z_now:.3f} -> {z_new:.3f}")
    _safe_goto_z(conn, z_new, context="ZSearch correction")
    if CONFIG.get("wait_stage_settled", True):
        conn.execute("CMD_WAIT_STAGE_SETTLED")

    # Reset OL near the scope's nominal reference so the real per-point
    # measurements that follow start from a sane baseline again.
    safe_set_double(conn, "AP_OLENS_CURRENT", 0.0)

    # Diagnostic-only verification: a second scouting AF should now show a
    # much smaller dz. Doesn't gate/abort anything - just tells you from the
    # log whether z_search_dz_sign is right (dz shrinks) or backwards (dz
    # grows), so it's a one-line config flip to fix, not a code change.
    if b2f_mode in ("PER_FOCUS", "ONCE"):
        run_b2f_robust(conn)
    af2 = run_af_fine_then_coarse(conn)
    dz2 = af2["dz_um"]
    if dz2 == dz2:
        verdict = "improved" if abs(dz2) < abs(dz) else "WORSE - check z_search_dz_sign!"
        print(f"[ZSearch] verification AF: dz_from_olens={dz2:.3f} um ({verdict})")

    return correction

def run_as(conn: MSEMConnection) -> Tuple[int, float, float, bool]:
    conn.set_double("DP_AUTOSTIG_SCANRATE", CONFIG["as_scan_rate"])
    conn.set_double("DP_AUTOSTIG_SUPPORTPOINTS", CONFIG["as_sampling_points"])
    conn.set_double("AP_AUTOSTIG_PIXELSIZE", CONFIG["as_pixel_size_nm"])
    conn.set_double("AP_AUTOSTIG_RANGE", CONFIG["as_range"])
    rc = conn.execute("CMD_AUTOSTIG")
    conf_x = safe_get_double(conn, "AP_AUTOSTIG_CONFIDENCE_X", default=float("nan"))
    conf_y = safe_get_double(conn, "AP_AUTOSTIG_CONFIDENCE_Y", default=float("nan"))
    # Lower-is-better (0 = perfect, confirmed against ZEN) - see is_success_conf.
    ok = is_success_conf(conf_x) and is_success_conf(conf_y)
    return rc, conf_x, conf_y, ok

def run_warmup_coarse_af_as(conn: MSEMConnection):
    """ZEN-style warmup: coarse AF + AS after a long stage move."""
    af_px = float(CONFIG.get("af_fine_pixel_size_nm", 10.0))
    warmup_px = min(2.0 * af_px, 12.0)

    print("[Warmup] coarse AF (range=15000, sampling_points=20) + AS (range=10, sampling_points=17)")
    run_af_once(
        conn,
        mode=int(CONFIG["af_coarse_mode"]),
        scan_rate=int(CONFIG["af_fine_scan_rate"]),
        sampling_points=20,
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

def minimize_ol_lens_zen_style(conn: MSEMConnection, require_verified_dz: bool = False) -> Tuple[bool, float]:
    """require_verified_dz=True means the dz reading on entry came from a
    COARSE AF rescue, not a converged fine AF (fine failed - see the
    used_coarse_rescue check in measure_one_focus_point) - coarse's own
    resolution is much lower than fine's, so a coarse-derived dz that merely
    LOOKS within dz_limit_um is not actually a verified measurement at that
    precision. In that case, skip the iter-1 early-exit so the loop always
    does at least one real Z-nudge + AF rerun (which reruns fine AF, now
    hopefully within its capture range since coarse already got us close),
    getting a fine-AF-verified dz before anything is accepted as final."""
    dz_limit = float(CONFIG["dz_limit_um"])
    max_iters = int(CONFIG["minimize_ol_max_iters"])
    n_af_reruns = 0  # each is a full AF fine(+coarse) scan, not a cheap readback

    for _iter in range(1, max_iters + 1):
        dz = safe_get_double(conn, "AP_DZ_FROM_OLENS", default=float("nan"))
        skip_early_exit = require_verified_dz and _iter == 1
        if skip_early_exit:
            print(f"    [Minimize-OL] iter 1: dz={dz:.3f} um came from a coarse-AF rescue, "
                  f"not fine - forcing a verification pass instead of trusting it as-is")
        if not skip_early_exit and dz == dz and abs(dz) <= dz_limit:
            if _iter > 1 or n_af_reruns > 0:
                print(f"    [Minimize-OL] converged on iter {_iter}/{max_iters}, {n_af_reruns} AF rerun(s)")
            return True, dz

        z0 = get_stage_z_stable(conn)
        if z0 == z0 and z0 < _STAGE_Z_ABORT_THRESHOLD_UM:
            # A real incident: the topography precheck's live Z read came
            # back near 0 here (the same telemetry-glitch/fault pattern seen
            # elsewhere), and with no check on z0 this went on to compute a
            # "corrected" Z near 0 too - only the LAST-RESORT hard ceiling in
            # _safe_goto_z caught it, aborting the whole run with a
            # StageZHardLimitError instead of just failing this one
            # measurement. Treat an implausible starting Z as a failed
            # measurement (same bucket as AF failing to converge) so the
            # caller skips this point/candidate instead of the run dying.
            print(f"    [Minimize-OL] ABORT: current Z={z0:.1f} um is below the "
                  f"{_STAGE_Z_ABORT_THRESHOLD_UM:.0f} um safety floor - not a real imaging "
                  f"height, refusing to compute a Z correction from it")
            return False, float("nan")
        ol0 = safe_get_double(conn, "AP_OLENS_CURRENT")

        max_z_corr = float(CONFIG.get("minimize_ol_max_z_correction_um", 200.0))
        dz_for_move = dz
        if dz == dz and abs(dz) > max_z_corr:  # NaN-safe
            print(f"    [Minimize-OL] WARNING: dz={dz:.3f} um exceeds "
                  f"minimize_ol_max_z_correction_um={max_z_corr:.1f} - clipping the Z-nudge "
                  f"(a dz this large means the seed OL/AF result is wrong, not that this "
                  f"correction is legitimate)")
            dz_for_move = math.copysign(max_z_corr, dz)

        signs = [+1.0]
        if CONFIG["minimize_ol_try_both_signs"]:
            signs.append(-1.0)

        best = None
        for idx_s, sgn in enumerate(signs):
            z_target = z0 + sgn * dz_for_move
            conn.set_double("AP_STAGE_SETTLING_TIME", CONFIG["stage_settling_time_ms"])
            _safe_goto_z(conn, z_target, context="Minimize-OL")
            rc = conn.execute("CMD_WAIT_STAGE_SETTLED")
            if rc != 0:
                if idx_s != len(signs) - 1:
                    _safe_goto_z(conn, z0, context="Minimize-OL restore")
                    conn.execute("CMD_WAIT_STAGE_SETTLED")
                    conn.set_double("AP_OLENS_CURRENT", ol0)
                continue

            conn.set_double("AP_OLENS_CURRENT", 0.0)

            if CONFIG["minimize_ol_rerun_af_each_iter"]:
                run_af_fine_then_coarse(conn)
                n_af_reruns += 1

            dz_after = safe_get_double(conn, "AP_DZ_FROM_OLENS", default=float("nan"))
            score = abs(dz_after) if (dz_after == dz_after) else float("inf")
            cand = {"sign": sgn, "score": score, "dz_after": dz_after}
            if (best is None) or (cand["score"] < best["score"]):
                best = cand

            if idx_s != len(signs) - 1:
                _safe_goto_z(conn, z0, context="Minimize-OL restore")
                conn.execute("CMD_WAIT_STAGE_SETTLED")
                conn.set_double("AP_OLENS_CURRENT", ol0)

        if best is None:
            break

        dz_now = safe_get_double(conn, "AP_DZ_FROM_OLENS", default=float("nan"))
        z_now = get_stage_z_stable(conn)
        if dz_now != dz_now:
            break
        if z_now == z_now and z_now < _STAGE_Z_ABORT_THRESHOLD_UM:
            # Same reasoning as the z0 check above, for the post-move read
            # this iteration's commit target is computed from.
            print(f"    [Minimize-OL] ABORT: post-move Z={z_now:.1f} um is below the "
                  f"{_STAGE_Z_ABORT_THRESHOLD_UM:.0f} um safety floor - not a real imaging "
                  f"height, refusing to commit a Z correction from it")
            return False, float("nan")

        dz_now_for_commit = dz_now
        if abs(dz_now) > max_z_corr:
            print(f"    [Minimize-OL] WARNING: post-move dz={dz_now:.3f} um exceeds "
                  f"minimize_ol_max_z_correction_um={max_z_corr:.1f} - clipping the commit "
                  f"Z-nudge too (same reasoning as the tentative-move clip above: this large "
                  f"a dz means the measurement is unreliable here, not that the correction is "
                  f"legitimate)")
            dz_now_for_commit = math.copysign(max_z_corr, dz_now)

        z_commit = z_now + best["sign"] * dz_now_for_commit
        _safe_goto_z(conn, z_commit, context="Minimize-OL commit")
        conn.execute("CMD_WAIT_STAGE_SETTLED")
        conn.set_double("AP_OLENS_CURRENT", 0.0)
        if CONFIG["minimize_ol_rerun_af_each_iter"]:
            run_af_fine_then_coarse(conn)
            n_af_reruns += 1

    dz_final = safe_get_double(conn, "AP_DZ_FROM_OLENS", default=float("nan"))
    ok = (dz_final == dz_final) and (abs(dz_final) <= dz_limit)
    print(f"    [Minimize-OL] exhausted {max_iters} iter(s), {n_af_reruns} AF rerun(s), ok={ok}")
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
                            skip_minimize_ol: bool = False,
                            seed_stig: Optional[Tuple[float, float]] = None) -> FocusMeasurement:
    do_minimize = CONFIG["minimize_ol_enabled"] and (not skip_minimize_ol)
    phase_str = "AF -> Minimize-OL -> AS" if do_minimize else "AF -> AS (no minimize-OL)"
    print("\n" + "=" * 78)
    print(f"[Offset FocusPoint {sp.index}] Stage -> (B2F) -> {phase_str}")
    print("=" * 78)

    z_target = sp.z if CONFIG["move_z_to_json_supportpoint"] else None
    _t0 = time.time()
    stage_move_xy_z(conn, sp.x, sp.y, z_target)
    t_stage = time.time() - _t0

    t_b2f = 0.0
    b2f_mode = str(CONFIG["b2f_mode"]).upper()
    if b2f_mode == "PER_FOCUS":
        _t0 = time.time()
        b2f_ok, rc, px, py = run_b2f_robust(conn, sp=sp)
        t_b2f = time.time() - _t0
        if not b2f_ok:
            print(f"  [Timing] stage_move={t_stage:.2f}s b2f={t_b2f:.2f}s (failed here, no AF/minimize-OL/AS)")
            print(f"  B2F failed at sp[{sp.index}] after retries (incl. beam-issue triage), rc={rc}")
            _focus_point_timing_log.append({
                "stage_s": t_stage, "b2f_s": t_b2f, "af_s": 0.0, "minimize_s": 0.0, "as_s": 0.0,
            })
            return FocusMeasurement(
                sp_index=sp.index, x=sp.x, y=sp.y,
                z_stage=get_stage_z_stable(conn),
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

    _t0 = time.time()
    af = run_af_fine_then_coarse(conn)
    t_af = time.time() - _t0
    coarse_str = "None" if af["conf_coarse"] is None else f"{af['conf_coarse']:.4f}"
    print(f"  AF: fine={af['conf_fine']:.4f}, coarse={coarse_str}, success={af['success']}  ({t_af:.2f}s)")
    print(f"  OL after AF: {af['ol_after']:.3f} uA, dz_from_olens: {af['dz_um']:.3f} um")

    # Fine AF has a much narrower search range than coarse (see af_fine_range_uA
    # vs af_coarse_range_uA) - when fine fails and coarse rescues it, the dz
    # reading on entry to minimize-OL reflects coarse's own lower resolution,
    # not a verified fine-precision measurement. Used below to force at least
    # one real verification pass in that case, instead of trusting a
    # coarse-derived dz just because it happens to read within dz_limit_um.
    used_coarse_rescue = (not is_success_conf(af["conf_fine"])) and is_success_conf(af["conf_coarse"])
    if used_coarse_rescue:
        print("  [Focus] fine AF failed, coarse AF rescued it - dz above is coarse-precision, "
              "not yet verified at fine precision")

    minimize_ok = False
    dz_after_min = float("nan")
    t_minimize = 0.0
    if do_minimize:
        if not af["success"]:
            print("  Minimize-OL skipped: AF failed.")
            minimize_ok = False
            dz_after_min = af["dz_um"]
        else:
            _t0 = time.time()
            minimize_ok, dz_after_min = minimize_ol_lens_zen_style(conn, require_verified_dz=used_coarse_rescue)
            t_minimize = time.time() - _t0
            print(f"  Minimize-OL result: ok={minimize_ok}, dz_final={dz_after_min:.3f} um (limit={CONFIG['dz_limit_um']:.1f})  ({t_minimize:.2f}s)")
    else:
        minimize_ok = True
        dz_after_min = af["dz_um"]

    all_focus_ok = af["success"] and minimize_ok

    t_as = 0.0
    as_rc, as_cx, as_cy, as_ok = (None, float("nan"), float("nan"), False)
    if CONFIG["as_enabled"]:
        if not all_focus_ok:
            print("  AS skipped: AF or minimize-OL did not succeed.")
        else:
            if seed_stig is not None:
                # CMD_AUTOSTIG searches only within AP_AUTOSTIG_RANGE around
                # whatever AP_STIG_X/Y currently are - which by default is
                # just whatever was left over from the previous point/region,
                # not this point's own known-good value. Seed it first so AS
                # searches from somewhere real instead of an arbitrary spot.
                safe_set_double(conn, "AP_STIG_X", float(seed_stig[0]))
                safe_set_double(conn, "AP_STIG_Y", float(seed_stig[1]))
                print(f"  Seeded StigX={seed_stig[0]:.6f}, StigY={seed_stig[1]:.6f} before AS")
            _t0 = time.time()
            as_rc, as_cx, as_cy, as_ok = run_as(conn)
            t_as = time.time() - _t0
            print(f"  AS: rc={as_rc}, confX={as_cx:.4f}, confY={as_cy:.4f}, ok={as_ok}  ({t_as:.2f}s)")

    t_total = t_stage + t_b2f + t_af + t_minimize + t_as
    print(f"  [Timing] stage_move={t_stage:.2f}s b2f={t_b2f:.2f}s af={t_af:.2f}s "
          f"minimize_ol={t_minimize:.2f}s as={t_as:.2f}s total={t_total:.2f}s")
    _focus_point_timing_log.append({
        "stage_s": t_stage, "b2f_s": t_b2f, "af_s": t_af, "minimize_s": t_minimize, "as_s": t_as,
    })

    z_final = get_stage_z_stable(conn)
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

    z_final = get_stage_z_stable(conn)
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
                             skip_minimize_ol: bool = False,
                             seed_stig: Optional[Tuple[float, float]] = None) -> FocusMeasurement:
    method = str(CONFIG.get("offset_method", "AF_MINIMIZE_OL")).upper()
    if method == "AF_MINIMIZE_OL":
        return measure_one_focus_point(conn, sp, skip_minimize_ol=skip_minimize_ol, seed_stig=seed_stig)
    if method == "DEEPFOCUS":
        return measure_one_focus_point_deepfocus(conn, sp)
    raise RuntimeError(f"Unknown offset_method: {method}")

def measure_plane_update_points(
    conn: MSEMConnection,
    sps_for_update: List[SupportPoint],
    region_name: str = "",
    measured_focusmap_out: Optional[str] = None,
    focus_points_csv_out: Optional[str] = None,
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

    try:
        _measure_plane_update_points_loop(conn, sps_for_update, method, strict, ms, skipped_pairs, last_good)
    finally:
        # ALWAYS save what was actually measured - even if too few points
        # survived (about to raise below) or a strict-mode point aborted the
        # loop early (exception propagating through this finally). A region
        # that fails here otherwise leaves behind zero diagnostic data about
        # what was actually measured at each attempted point.
        if measured_focusmap_out:
            try:
                save_measured_focusmap_txt(measured_focusmap_out, region_name, ms, skipped_pairs)
                print(f"[PlaneUpdate] saved measured FocusMap.txt: {measured_focusmap_out}")
            except Exception as exc:
                print(f"[PlaneUpdate] WARNING: could not save measured FocusMap.txt: {exc}")
        if focus_points_csv_out:
            try:
                save_focus_points_csv(focus_points_csv_out, ms, skipped=skipped_pairs)
                print(f"[CSV] saved plane-update measurements: {focus_points_csv_out} "
                      f"({len(ms)} ok, {len(skipped_pairs)} skipped)")
            except Exception as exc:
                print(f"[PlaneUpdate] WARNING: could not save focus points CSV: {exc}")

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

def _measure_plane_update_points_loop(conn, sps_for_update, method, strict, ms, skipped_pairs, last_good):
    for sp in sps_for_update:
        # Unlike choose_robust_one_point_offset() (ONE_POINT_OFFSET), this path
        # (CURRENT_ALL_POINTS_PLANE / THREE_POINT_DELTA) has no per-point
        # prev_focusmap history to seed from - sp.ol here is always an unused
        # JSON placeholder. Seed from the wafer-wide fiducial plane instead, if
        # one is configured, so AF still gets a real head start.
        #
        # The fiducial OL plane is only valid AT the Z height it was
        # calibrated at (see wafer_fiducial_z_align_enabled above) - move
        # there FIRST (this doubles as this point's XY move) so the OL seed
        # is evaluated under the same condition it was measured under,
        # instead of seeding a value that assumes a Z the stage isn't
        # actually at.
        if CONFIG.get("wafer_fiducial_z_align_enabled", True):
            fiducial_z = estimate_z_from_wafer_fiducial(sp.x, sp.y)
            if fiducial_z is not None:
                current_z = get_stage_z_stable(conn)
                print(f"[PlaneUpdate] aligning to fiducial calibration height for sp[{sp.index}]: "
                      f"current Z={current_z:.1f} um -> fiducial Z={fiducial_z:.1f} um "
                      f"(delta={fiducial_z - current_z:+.1f} um)")
                stage_move_xy_z(conn, sp.x, sp.y, fiducial_z)

        seed_ol = estimate_ol_from_wafer_fiducial(sp.x, sp.y)
        if seed_ol is not None:
            safe_set_double(conn, "AP_OLENS_CURRENT", float(seed_ol))
            print(f"[PlaneUpdate] seeded OL={seed_ol:.3f} uA from wafer fiducial plane for sp[{sp.index}]")

        seed_stig = estimate_stig_from_wafer_fiducial(sp.x, sp.y)
        if seed_stig is not None:
            print(f"[PlaneUpdate] seeded StigX={seed_stig[0]:.6f}, StigY={seed_stig[1]:.6f} "
                  f"from wafer fiducial plane for sp[{sp.index}]")

        m = measure_one_offset_point(conn, sp, seed_stig=seed_stig)

        reason: Optional[str] = None
        if method == "AF_MINIMIZE_OL":
            if not m.af_success:
                reason = "AF_failed"
            elif CONFIG["minimize_ol_enabled"] and (not m.minimize_ol_success):
                reason = "minimize_ol_failed"
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

def _validate_focus_measurement(m: FocusMeasurement, require_minimize_ol: bool) -> bool:
    method = str(CONFIG.get("offset_method", "AF_MINIMIZE_OL")).upper()

    if method == "AF_MINIMIZE_OL":
        if not m.af_success:
            return False
        if require_minimize_ol and (not m.minimize_ol_success):
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

        # Seed OL with THIS point's own recorded value from the prev_focusmap
        # row (not the region-wide plane evaluated at some other point's xy,
        # and not whatever was left over from the previous candidate) - AF
        # then starts from what's actually known to be close to correct here,
        # before either the coarse warmup below or the fine AF in
        # measure_one_offset_point() runs.
        safe_set_double(conn, "AP_OLENS_CURRENT", float(cand.ol))
        print(f"[OnePointOffset] seeded OL={cand.ol:.3f} uA from prev_focusmap sp_id={cand.sp_id}")

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

        m = measure_one_offset_point(conn, sp, skip_minimize_ol=skip_min, seed_stig=(cand.asx, cand.asy))
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
        # Lower AF confidence = better (0 = perfect, confirmed against ZEN) -
        # a missing (NaN) reading falls back to +inf ("infinitely bad"), not
        # 0.0, so it can't masquerade as the best reading. min(inlier_pairs,
        # key=rep_key) wants this tiebreaker ascending, so no negation.
        conf_best = min(
            m.af_conf_fine if m.af_conf_fine == m.af_conf_fine else float("inf"),
            m.af_conf_coarse if m.af_conf_coarse == m.af_conf_coarse else float("inf"),
        )
        return (
            abs(dc - delta_med),
            abs(m.dz_um),
            conf_best,
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

def save_measured_focusmap_txt(
    path: str,
    region_name: str,
    ms: List[FocusMeasurement],
    skipped: Optional[List[Tuple[FocusMeasurement, str]]] = None,
) -> None:
    """Write this run's actually-measured support points in the SAME
    10-column format as Zeiss's own FocusMap.txt (see the column layout
    documented in load_prev_focusmap_fit_plane_and_candidates), so the two
    can be diffed line-for-line against the prev_focusmap file that seeded
    this region. That's the direct way to answer "is the prior data wrong,
    or is something in how we use it wrong" when focus keeps failing despite
    the prior file checking out independently.

    Written for EVERY attempted support point, ok or skipped - a region that
    fails with "only N valid measurements" still leaves behind exactly what
    was measured at each point, instead of nothing at all (see the finally
    block in measure_plane_update_points, which calls this even when the
    region is about to raise).

    A field only becomes -4 (Zeiss's own sentinel) when the underlying value
    is genuinely NaN/never measured - NOT just because af_success/as_success
    was False, since the real achieved number (even if the pipeline judged
    it too low-confidence to use) is exactly the diagnostic signal a
    focus-failure comparison needs to see."""
    skipped = skipped or []
    skip_reasons = {m.sp_index: reason for m, reason in skipped}
    all_points = sorted(list(ms) + [m for m, _ in skipped], key=lambda m: m.sp_index)

    m_sec = re.match(r"[Ss](\d+)", region_name)
    region_num = int(m_sec.group(1)) if m_sec else 0

    def _val(x: float) -> float:
        return x if (isinstance(x, (int, float)) and x == x) else -4.0

    def _fail_note(label: str, conf: Optional[float]) -> Optional[str]:
        if conf is None or conf != conf:  # NaN-safe: never measured, not a failure code
            return None
        meaning = AF_AS_CONFIDENCE_FAILURE_MEANINGS.get(conf)
        return f"{label}={conf:g} ({meaning})" if meaning else None

    def _explain(m: FocusMeasurement) -> str:
        """Trailing human-readable note for this row - empty when nothing
        noteworthy happened. Explains WHAT the af_conf/as_conf columns mean
        for this point even when fine AF failed but coarse silently rescued
        it (af_conf then shows coarse's real success value, not a failure
        code - see the fallback above), which otherwise looks unremarkable
        despite being exactly the kind of thing worth flagging."""
        notes = []
        fine_note = _fail_note("af_fine", m.af_conf_fine)
        coarse_note = _fail_note("af_coarse", m.af_conf_coarse)
        if fine_note and coarse_note:
            notes.append(f"AF FAILED - {fine_note}; {coarse_note}")
        elif fine_note:
            notes.append(f"fine AF failed ({fine_note}), coarse AF rescued it")
        as_notes = [n for n in (_fail_note("as_x", m.as_conf_x), _fail_note("as_y", m.as_conf_y)) if n]
        if as_notes:
            notes.append("AS FAILED - " + "; ".join(as_notes))
        reason = skip_reasons.get(m.sp_index)
        if reason:
            notes.append(f"point skipped from plane fit: {reason}")
        return "; ".join(notes)

    # Fixed-width, right-justified columns (one support point per row, CRLF
    # line endings so it reads correctly in Notepad/Excel too) purely for
    # human readability - every reader of this format (load_prev_focusmap_
    # fit_plane_and_candidates, compare_focusmaps.py) already strips each
    # comma-split field before parsing and only looks at the first 10 fields,
    # so the extra padding/header/trailing note changes nothing about how
    # the file is consumed - it's read for humans, ignored by every parser.
    header = (
        "#region, sp_id,            x,            y,           ol,     stig_x,"
        "     stig_y,    af_conf,  as_conf_x,  as_conf_y,  note"
    )
    legend = (
        "# AF/AS confidence: >0 success (smaller=better), 0=perfect. Failure codes per "
        "the MultiSEM API doc: -1=not enough features, -2=upper search-range limit, "
        "-3=lower search-range limit, -4=inconsistent fit, -5=out-of-range (generic "
        "Set_PassedType* failure, not a documented AF/AS code). af_conf below shows "
        "coarse's confidence when fine failed but coarse rescued it, not fine's raw "
        "failure code - see the trailing note on that row for what actually happened."
    )
    with open(path, "w", newline="") as f:
        f.write(header + "\r\n")
        f.write(legend + "\r\n")
        for m in all_points:
            af_conf = _val(m.af_conf_fine)
            if af_conf in AF_AS_CONFIDENCE_FAILURE_SENTINELS:
                # Fine failed for ANY documented reason (-1/-2/-3/-4), not just
                # -4 - show coarse's confidence (what was actually used to
                # recover) instead of fine's raw failure code, which by itself
                # looks like "the final answer failed" even when coarse (and
                # possibly a subsequent minimize-OL verification pass) already
                # rescued it.
                af_conf = _val(m.af_conf_coarse)
            note = _explain(m)
            f.write(
                f"{region_num:>7d}, {m.sp_index:>5d}, {_val(m.x):>12.6g}, {_val(m.y):>12.6g}, "
                f"{_val(m.ol_uA):>12.6g}, {_val(m.stig_x):>10.6g}, {_val(m.stig_y):>10.6g}, "
                f"{af_conf:>10.6g}, {_val(m.as_conf_x):>10.6g}, {_val(m.as_conf_y):>10.6g}"
                + (f",  {note}" if note else "")
                + "\r\n"
            )

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
                     "stig_x_pred", "stig_y_pred", "proj_x_pred", "proj_y_pred"])
        for p in plans:
            z_out = "" if p.z_pred is None else p.z_pred
            px_out = "" if p.proj_x_pred is None else p.proj_x_pred
            py_out = "" if p.proj_y_pred is None else p.proj_y_pred
            w.writerow([p.tile_idx, p.x, p.y, z_out, p.ol_pred,
                         p.stig_x_pred, p.stig_y_pred, px_out, py_out])

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
            proj_x_str = row.get("proj_x_pred", "").strip()
            proj_y_str = row.get("proj_y_pred", "").strip()
            proj_x_pred = None if not proj_x_str else float(proj_x_str)
            proj_y_pred = None if not proj_y_str else float(proj_y_str)
            tiles.append((x, y))
            plans.append(TilePlan(tile_idx=tile_idx, x=x, y=y, z_pred=z_pred,
                                  ol_pred=ol_pred,
                                  stig_x_pred=stig_x_pred,
                                  stig_y_pred=stig_y_pred,
                                  proj_x_pred=proj_x_pred,
                                  proj_y_pred=proj_y_pred))
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

# -----------------------------------------------------------------------------
# MANUAL_POINTS plane update (issue #8): operator-placed points + hand-set focus
# -----------------------------------------------------------------------------
def load_manual_focus_points(path: Optional[str], region_name: str) -> List[Dict[str, Any]]:
    """Load operator-placed focus points for ``region_name`` from a manual-focus
    JSON file. Schema:

        {"regions": {"<region_name>": {"points": [
            {"x": <um>, "y": <um>, "z": <um?>,
             "ol_uA": <float?>, "stig_x": <float?>, "stig_y": <float?>}, ...]}}}

    A point WITH ``ol_uA`` is a manual point (value used per manual_focus_mode);
    a point WITHOUT ``ol_uA`` is placement-only (AF/AS measures it there).
    Coordinates are stage um (same frame as AP_STAGE_GOTO_X/Y). Raises with a
    clear message on a missing file / region / empty points."""
    if not path:
        raise RuntimeError(
            "plane_update_method=MANUAL_POINTS but manual_focus_points_file is not set.")
    if not os.path.isfile(path):
        raise FileNotFoundError(f"manual_focus_points_file not found: {path}")
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    regions = data.get("regions", {})
    if not isinstance(regions, dict):
        raise RuntimeError(f"{path}: 'regions' must be an object mapping region name -> points.")
    entry = regions.get(region_name)
    if entry is None:
        raise RuntimeError(
            f"{path}: no manual points for region '{region_name}' "
            f"(available: {sorted(regions.keys())}).")
    pts = entry.get("points", entry) if isinstance(entry, dict) else entry
    if not pts:
        raise RuntimeError(f"{path}: region '{region_name}' has an empty points list.")
    out: List[Dict[str, Any]] = []
    for i, p in enumerate(pts):
        if "x" not in p or "y" not in p:
            raise RuntimeError(f"{path}: region '{region_name}' point {i} is missing x/y.")
        out.append({
            "x": float(p["x"]),
            "y": float(p["y"]),
            "z": (float(p["z"]) if p.get("z") is not None else None),
            "ol_uA": (float(p["ol_uA"]) if p.get("ol_uA") is not None else None),
            "stig_x": (float(p["stig_x"]) if p.get("stig_x") is not None else None),
            "stig_y": (float(p["stig_y"]) if p.get("stig_y") is not None else None),
        })
    return out


def build_manual_support_points(points: List[Dict[str, Any]]) -> List[SupportPoint]:
    """Turn manual-focus points into SupportPoint objects (index = order). The
    ol/projective fields carry no meaning here (measurement drives OL); 0.0."""
    return [
        SupportPoint(
            index=i,
            x=p["x"], y=p["y"],
            z=(p["z"] if p["z"] is not None else 0.0),
            ol=(p["ol_uA"] if p["ol_uA"] is not None else 0.0),
            projective_x=0.0, projective_y=0.0,
        )
        for i, p in enumerate(points)
    ]


def _fit_focus_plane_or_flat(rows: List[Tuple[float, float, float]], label: str) -> Tuple[float, float, float]:
    """Fit z = a*x + b*y + c from (x,y,z) rows. With <3 points or a degenerate
    (collinear) fit, fall back to a FLAT plane a=b=0, c=mean(z) — so a single
    manual point can set a constant OL/stig for the whole region."""
    zs = [r[2] for r in rows]
    if len(rows) >= 3:
        try:
            a, b, c = _fit_plane_from_xyz(rows)
            print(f"[ManualPlane/{label}] fitted a={a:.6e} b={b:.6e} c={c:.4f} from {len(rows)} pts")
            return a, b, c
        except RuntimeError as e:
            print(f"[ManualPlane/{label}] {e} -> flat fallback")
    c = mean(zs) if zs else 0.0
    print(f"[ManualPlane/{label}] flat plane c={c:.4f} from {len(rows)} pt(s)")
    return 0.0, 0.0, c


def measure_one_manual_point(conn: MSEMConnection, sp: SupportPoint, manual: Dict[str, Any]) -> FocusMeasurement:
    """Apply/measure focus + stig at one operator-placed point.

    SKIP_AF (+ operator ol_uA): move stage, set AP_OLENS_CURRENT (+AP_STIG_X/Y if
        given) to the operator's values verbatim; NO CMD_AUTOFOCUS/CMD_AUTOSTIG.
    SEED_AF (or a point with no operator ol_uA): move stage, seed OL with the
        operator's value if present, then run the normal AF (fine range =
        manual_focus_seed_range_uA) + minimize-OL + AS to refine."""
    mode = str(CONFIG.get("manual_focus_mode", "SKIP_AF")).upper()
    has_ol = manual.get("ol_uA") is not None
    z_target = sp.z if CONFIG["move_z_to_json_supportpoint"] else None
    stage_move_xy_z(conn, sp.x, sp.y, z_target)

    if mode == "SKIP_AF" and has_ol:
        ol = float(manual["ol_uA"])
        safe_set_double(conn, "AP_OLENS_CURRENT", ol)
        sx, sy = manual.get("stig_x"), manual.get("stig_y")
        if sx is not None:
            safe_set_double(conn, "AP_STIG_X", float(sx))
        if sy is not None:
            safe_set_double(conn, "AP_STIG_Y", float(sy))
        stig_x_final = safe_get_double(conn, "AP_STIG_X", default=float("nan"))
        stig_y_final = safe_get_double(conn, "AP_STIG_Y", default=float("nan"))
        print(f"[Manual/SKIP_AF] sp[{sp.index}] xy=({sp.x:.1f},{sp.y:.1f}) "
              f"OL={ol:.1f} uA stig=({stig_x_final:.4f},{stig_y_final:.4f}) (no AF/AS)")
        return FocusMeasurement(
            sp_index=sp.index, x=sp.x, y=sp.y,
            z_stage=get_stage_z_stable(conn),
            ol_uA=ol, dz_um=0.0,
            af_conf_fine=1.0, af_conf_coarse=float("nan"),
            af_success=True, minimize_ol_success=True, dz_after_minimize=0.0,
            as_conf_x=(1.0 if sx is not None else float("nan")),
            as_conf_y=(1.0 if sy is not None else float("nan")),
            as_success=(sx is not None and sy is not None),
            stig_x=stig_x_final, stig_y=stig_y_final,
        )

    # SEED_AF, or a placement-only point: seed OL (if given) then normal AF/AS.
    if has_ol:
        safe_set_double(conn, "AP_OLENS_CURRENT", float(manual["ol_uA"]))
        print(f"[Manual/SEED_AF] sp[{sp.index}] seeded OL={float(manual['ol_uA']):.1f} uA, refining with AF/AS")
    else:
        print(f"[Manual/measure] sp[{sp.index}] placement-only point, measuring with AF/AS")

    saved_range = CONFIG.get("af_fine_range_uA")
    seed_range = CONFIG.get("manual_focus_seed_range_uA")
    try:
        if has_ol and seed_range:
            CONFIG["af_fine_range_uA"] = seed_range
        return measure_one_focus_point(conn, sp)
    finally:
        CONFIG["af_fine_range_uA"] = saved_range


def measure_manual_focus_points(
    conn: MSEMConnection,
    sps: List[SupportPoint],
    points: List[Dict[str, Any]],
) -> Tuple[List[FocusMeasurement], List[Tuple[FocusMeasurement, str]]]:
    """Apply/measure every operator-placed point. Mirrors measure_plane_update_points'
    (ok, skipped) contract so it flows through the existing plane-fit + CSV path.
    SKIP_AF points bypass the AF/AS validation gate (they carry no confidence);
    SEED_AF / placement-only points must pass _validate_focus_measurement."""
    ms: List[FocusMeasurement] = []
    skipped: List[Tuple[FocusMeasurement, str]] = []
    strict = bool(CONFIG.get("plane_update_strict", False))
    mode = str(CONFIG.get("manual_focus_mode", "SKIP_AF")).upper()
    for sp, manual in zip(sps, points):
        m = measure_one_manual_point(conn, sp, manual)
        is_skip = (mode == "SKIP_AF" and manual.get("ol_uA") is not None)
        if is_skip or _validate_focus_measurement(m, require_minimize_ol=False):
            ms.append(m)
        else:
            skipped.append((m, "manual point failed AF/AS validation"))
            if strict:
                raise RuntimeError(f"Manual point sp[{sp.index}] failed measurement (strict mode).")
    return ms, skipped

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

    Zeiss writes -4 / -5 into the OL/stig VALUE fields when AF/AS didn't
    converge at that support point (a distinct convention from the confidence
    failure codes below - lens/stig_x/stig_y are real physical values the rest
    of the time, and -1/-2/-3 are not documented sentinels for them, so only
    -4/-5 apply here). stig_x/stig_y here are cols 5/6 ("Objective tilt x/y"
    per Zeiss's FocusMap.txt doc - see the column-mapping note in
    load_prev_focusmap_fit_plane_and_candidates()), so a failed AS legitimately
    drops the point.

    af_conf, by contrast, is the AP_AUTOFOCUS_CONFIDENCE / AP_AUTOSTIG_CONFIDENCE
    value itself, whose failure codes ARE fully documented (see
    AF_AS_CONFIDENCE_FAILURE_SENTINELS above: -1/-2/-3/-4, plus -5 kept as
    harmless insurance) - a lower-is-better error metric (0 = perfect focus,
    confirmed against ZEN directly), NOT a probability-style confidence where
    higher is better - so a row with af_conf == 0 is the BEST possible
    measurement, not a failure. This used to also treat af_conf <= 0 as failed,
    which silently dropped the best point in nearly every prev_focusmap file we
    looked at (see compare_focusmaps.py) and was very likely the actual cause of
    most "only N valid measurement(s) survived" preflight failures - real support
    points, not missing/bad data, were being thrown out here."""
    if lens in (-4.0, -5.0):
        return True
    if stig_x in (-4.0, -5.0) and stig_y in (-4.0, -5.0):
        return True
    if af_conf != af_conf or af_conf in AF_AS_CONFIDENCE_FAILURE_SENTINELS:
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


def _fit_plane_local_or_fiducial_anchored(
    rows: List[Tuple[float, float, float]],
    fiducial_plane: Optional[Tuple[float, float, float]],
    label: str,
) -> Tuple[float, float, float]:
    """Fit v = a*x + b*y + c for a region with FEWER than 3 local points
    (see load_prev_focusmap_fit_plane_and_candidates - the normal >=3-point
    case never calls this). A 2D plane fit is underdetermined with only 1-2
    points, so instead of guessing a slope from too little local data, this
    borrows the WAFER-WIDE fiducial plane's slope (a, b - the wafer's known
    tilt, fit from wafer_fiducial_ol_file's markers) and re-anchors its
    intercept to whatever this region's own local point(s) actually
    measured: c = mean(v_i - a*x_i - b*y_i). That keeps the wafer's overall
    tilt while correcting for THIS region's own local Z/mount offset from
    the fiducial calibration height - rather than trusting the fiducial's
    own intercept, which only reflects conditions at the calibration points
    and may not hold at this particular region.

    With zero local points, there's nothing to anchor to, so the fiducial
    plane is returned exactly as-is (logged clearly - this region gets NO
    region-specific correction at all, which is materially different from
    even a single anchored point)."""
    if fiducial_plane is None:
        raise RuntimeError(
            f"Not enough local {label} points ({len(rows)}, need >=3 for a normal "
            f"fit) and no wafer fiducial plane available to anchor to instead - "
            f"configure wafer_fiducial_ol_file (or fix the wafer fiducial markers "
            f"file, which needs >=3 usable points of its own) to proceed with "
            f"fewer than 3 local {label} points."
        )
    a, b, _c_fid = fiducial_plane
    if not rows:
        print(f"[PrevPlane] {label}: 0 local point(s) - using the wafer fiducial "
              f"plane exactly as-is (no local anchor for this region)")
        return fiducial_plane
    offsets = [v - (a * x + b * y) for x, y, v in rows]
    c = sum(offsets) / len(offsets)
    offsets_str = ", ".join(f"{o:+.4f}" for o in offsets)
    print(f"[PrevPlane] {label}: only {len(rows)} local point(s) (need >=3 for a "
          f"normal fit) - anchoring the wafer fiducial slope (a={a:.6e}, "
          f"b={b:.6e}) to the local measurement(s): c={c:.4f} (per-point "
          f"offset(s) from the fiducial plane: {offsets_str})")
    return (a, b, c)


# =============================================================================
# Wafer-wide fiducial planes (see wafer_fiducial_ol_file in CONFIG)
# =============================================================================

_wafer_fiducial_cache: Dict[str, Dict[str, Optional[Tuple[float, float, float]]]] = {}

# Snapshot of the Z plane exactly as fit from the fiducial file, taken once
# and never touched again - unlike _wafer_fiducial_cache["z"], which
# precheck_and_correct_wafer_fiducial_planes() mutates in place with a
# drift correction derived from live stage-Z reads. A real incident showed
# those live reads can themselves be wrong (a bad reading during the
# once-per-wafer precheck silently poisoned the cached Z plane for an
# entire run), so this untouched copy is the only thing left that can be
# trusted as "known good" when sanity-checking a corrective Z move.
_original_wafer_fiducial_z_plane: Dict[str, Tuple[float, float, float]] = {}


# Header column name -> canonical key, for _parse_wafer_fiducial_points'
# header-based column lookup (mirrors compare_ol_plane_fiducials.py's
# _normalize_header_token, kept as a separate copy since this module has no
# dependency on that script) - lets a header spell out units/use
# human-friendly names (e.g. "x(um)", "OL(um)", "Stigmatorx(%)") without
# breaking column lookup below.
_WAFER_FIDUCIAL_HEADER_ALIASES = {
    "stigmatorx": "stigx", "stigmatory": "stigy",
    "projectivex": "projx", "projectivey": "projy",
}

# Per the user: neither StigX nor StigY should ever legitimately exceed this
# on this instrument - a wafer-fiducial row with a bigger one is a fault
# (microscope or sample), not real data. See _parse_wafer_fiducial_points.
_STIG_FAULT_THRESHOLD_PCT = 2.0


def _normalize_wafer_fiducial_header_token(token: str) -> str:
    name = re.sub(r"\(.*?\)", "", token).strip().lower().replace("_", "")
    return _WAFER_FIDUCIAL_HEADER_ALIASES.get(name, name)


def _parse_wafer_fiducial_points(path: str):
    """Parse a wafer-fiducial points file: legacy 'x,y,z,ol' (4 columns, no
    header) or an extended format with a header row naming each column
    (any order/subset of x,y,z,ol,stigx,stigy,projx,projy - e.g.
    'x(um),y(um),Z(um),OL(um),Stigmatorx(%),Stigmatory(%),Projectivex,
    ProjectiveY'). Column lookup is by HEADER NAME, not position, whenever a
    header is present, so a file whose declared column count doesn't match
    its actual row length is caught explicitly and that row is skipped -
    NOT silently misread. This is the direct fix for a real incident: an
    8-column header once sat over 6-column rows in
    w10h31_region_fiducials_2.txt, and the old purely-positional parser
    silently read projective_x/y as stigx/stigy.

    Returns (x, y, z, ol, stigx, stigy, projx, projy) tuples - the last four
    are None for any column not present in the file."""
    with open(path, "r") as f:
        lines = [ln.strip() for ln in f if ln.strip()]
    if not lines:
        return []

    header_tokens = [_normalize_wafer_fiducial_header_token(t) for t in lines[0].split(",")]
    has_header = header_tokens[0] == "x"
    if has_header:
        col = {name: i for i, name in enumerate(header_tokens)}
        missing = [k for k in ("x", "y", "z", "ol") if k not in col]
        if missing:
            print(f"[WaferFiducial] {path}: header missing required column(s) "
                  f"{missing} ({lines[0]!r}) - no points parsed")
            return []
        data_lines = lines[1:]
        start_line_num = 2
    else:
        # No header - assume the legacy positional 'x,y,z,ol' layout.
        col = {"x": 0, "y": 1, "z": 2, "ol": 3}
        data_lines = lines
        start_line_num = 1

    rows = []
    for line_num, line in enumerate(data_lines, start=start_line_num):
        tokens = [t.strip() for t in line.split(",")]
        if len(tokens) == 5 and not has_header:
            # legacy decimal-comma typo, e.g. "11,2" meaning "11.2" in ol
            tokens = tokens[:3] + [f"{tokens[3]}.{tokens[4]}"]
        if len(tokens) <= max(col.values()):
            print(f"[WaferFiducial] {os.path.basename(path)} line {line_num}: "
                  f"only {len(tokens)} field(s), header declares a column at "
                  f"index {max(col.values())} - skipping: {line!r}")
            continue
        try:
            x = float(tokens[col["x"]]); y = float(tokens[col["y"]])
            z = float(tokens[col["z"]]); ol = float(tokens[col["ol"]])
            sx = float(tokens[col["stigx"]]) if "stigx" in col else None
            sy = float(tokens[col["stigy"]]) if "stigy" in col else None
            px = float(tokens[col["projx"]]) if "projx" in col else None
            py = float(tokens[col["projy"]]) if "projy" in col else None
        except ValueError:
            print(f"[WaferFiducial] {os.path.basename(path)} line {line_num}: "
                  f"could not parse floats -> skipping: {line!r}")
            continue
        # A |StigX| or |StigY| this large is never a real sample/optics tilt
        # on this instrument (per the user: never expect either to exceed 2%)
        # - it's a fault (microscope or sample), and the WHOLE point is
        # unreliable, not just its stig columns. Drop it entirely rather than
        # letting a bad stig reading poison the OL/Z/Proj plane fits too - a
        # real w10h31_dye_R3 fiducial point (StigY=4.6%) did exactly that,
        # dominating the OL plane's residuals before this filter existed.
        if (sx is not None and abs(sx) > _STIG_FAULT_THRESHOLD_PCT) or \
           (sy is not None and abs(sy) > _STIG_FAULT_THRESHOLD_PCT):
            print(f"[WaferFiducial] {os.path.basename(path)} line {line_num}: "
                  f"StigX={sx} StigY={sy} exceeds the {_STIG_FAULT_THRESHOLD_PCT:.0f}% "
                  f"fault threshold - treating as a bad point (microscope/sample fault) "
                  f"and dropping the whole row: {line!r}")
            continue
        rows.append((x, y, z, ol, sx, sy, px, py))
    return rows


def _get_wafer_fiducial_planes() -> Dict[str, Optional[Tuple[float, float, float]]]:
    """Parse + fit (and cache) every wafer-fiducial plane available from
    CONFIG['wafer_fiducial_ol_file']: 'ol' and 'z' always (if the file has
    >=3 usable rows -- both are present even in the legacy 4-column format),
    plus 'stigx'/'stigy' and 'projx'/'projy' if the file also has those
    columns (the extended 8-column format). Each entry is None if unavailable.

    'z' is the fiducial file's own physical stage Z (um) -- used only as a
    coarse startup sanity check (see check_and_correct_z_from_fiducial), NOT
    as a per-point focus seed the way 'ol'/'stigx'/'stigy' are: OL/AF/AS only
    ever correct small (um-scale) defocus, they can't fix the stage sitting
    at the wrong ABSOLUTE height (mm-scale), which is what this plane is for."""
    path = CONFIG.get("wafer_fiducial_ol_file")
    if not path:
        return {}
    if path in _wafer_fiducial_cache:
        return _wafer_fiducial_cache[path]

    planes: Dict[str, Optional[Tuple[float, float, float]]] = {
        "ol": None, "stigx": None, "stigy": None, "z": None, "projx": None, "projy": None,
    }
    try:
        rows = _parse_wafer_fiducial_points(path)
        if len(rows) >= 3:
            planes["ol"] = _fit_plane_from_xyz([(x, y, ol) for x, y, _z, ol, *_ in rows])
            a, b, c = planes["ol"]
            print(f"[WaferFiducial] fit OL = {a:.6e}*x + {b:.6e}*y + {c:.3f} "
                  f"from {len(rows)} point(s) in {path}")

            planes["z"] = _fit_plane_from_xyz([(x, y, z) for x, y, z, _ol, *_ in rows])
            az, bz, cz = planes["z"]
            print(f"[WaferFiducial] fit Z = {az:.6e}*x + {bz:.6e}*y + {cz:.3f} "
                  f"from {len(rows)} point(s) in {path}")
            _original_wafer_fiducial_z_plane.setdefault(path, planes["z"])

            stig_rows = [(x, y, sx, sy) for x, y, _z, _ol, sx, sy, _px, _py in rows
                         if sx is not None and sy is not None]
            if len(stig_rows) >= 3:
                planes["stigx"] = _fit_plane_from_xyz([(x, y, sx) for x, y, sx, _sy in stig_rows])
                planes["stigy"] = _fit_plane_from_xyz([(x, y, sy) for x, y, _sx, sy in stig_rows])
                asx, bsx, csx = planes["stigx"]
                asy, bsy, csy = planes["stigy"]
                print(f"[WaferFiducial] fit StigX = {asx:.6e}*x + {bsx:.6e}*y + {csx:.6f} "
                      f"from {len(stig_rows)} point(s)")
                print(f"[WaferFiducial] fit StigY = {asy:.6e}*x + {bsy:.6e}*y + {csy:.6f} "
                      f"from {len(stig_rows)} point(s)")
            else:
                print(f"[WaferFiducial] no stig columns (or <3 usable rows) in {path} "
                      f"- wafer-wide stig estimate unavailable")

            proj_rows = [(x, y, px, py) for x, y, _z, _ol, _sx, _sy, px, py in rows
                         if px is not None and py is not None]
            if len(proj_rows) >= 3:
                planes["projx"] = _fit_plane_from_xyz([(x, y, px) for x, y, px, _py in proj_rows])
                planes["projy"] = _fit_plane_from_xyz([(x, y, py) for x, y, _px, py in proj_rows])
                apx, bpx, cpx = planes["projx"]
                apy, bpy, cpy = planes["projy"]
                print(f"[WaferFiducial] fit ProjX = {apx:.6e}*x + {bpx:.6e}*y + {cpx:.3f} "
                      f"from {len(proj_rows)} point(s)")
                print(f"[WaferFiducial] fit ProjY = {apy:.6e}*x + {bpy:.6e}*y + {cpy:.3f} "
                      f"from {len(proj_rows)} point(s)")
            else:
                print(f"[WaferFiducial] no projective columns (or <3 usable rows) in {path} "
                      f"- wafer-wide projective estimate unavailable")
        else:
            print(f"[WaferFiducial] WARNING: only {len(rows)} valid point(s) in "
                  f"{path} (need >=3) - wafer-wide estimates unavailable")
    except Exception as exc:
        print(f"[WaferFiducial] WARNING: could not load {path}: {exc}")

    _wafer_fiducial_cache[path] = planes
    return planes


def get_wafer_fiducial_ol_plane() -> Optional[Tuple[float, float, float]]:
    """Fit (and cache) OL = a*x + b*y + c from CONFIG['wafer_fiducial_ol_file'].

    Unlike prev_focusmap (which only covers points a prior run already
    measured), this gives an OL estimate for ANY (x, y) on the wafer, fit
    from a small set of wafer-spanning fiducial points. Returns None if not
    configured, missing, or has fewer than 3 usable points."""
    return _get_wafer_fiducial_planes().get("ol")


def estimate_ol_from_wafer_fiducial(x: float, y: float) -> Optional[float]:
    """OL estimate at (x, y) from the wafer fiducial plane, or None if
    wafer_fiducial_ol_file isn't configured/usable."""
    plane = get_wafer_fiducial_ol_plane()
    if plane is None:
        return None
    a, b, c = plane
    return a * x + b * y + c


def estimate_stig_from_wafer_fiducial(x: float, y: float) -> Optional[Tuple[float, float]]:
    """(StigX, StigY) estimate at (x, y) from the wafer fiducial plane, or
    None if wafer_fiducial_ol_file isn't configured/usable, or is the legacy
    4-column (x,y,z,ol) format with no stig data to fit."""
    planes = _get_wafer_fiducial_planes()
    stigx_plane, stigy_plane = planes.get("stigx"), planes.get("stigy")
    if stigx_plane is None or stigy_plane is None:
        return None
    ax, bx, cx = stigx_plane
    ay, by, cy = stigy_plane
    return (ax * x + bx * y + cx, ay * x + by * y + cy)


def estimate_z_from_wafer_fiducial(x: float, y: float) -> Optional[float]:
    """Physical stage Z (um) estimate at (x, y) from the wafer fiducial
    plane, or None if wafer_fiducial_ol_file isn't configured/usable.

    Coarse-only: this is a startup sanity check against a stage sitting at
    the wrong ABSOLUTE height (see check_and_correct_z_from_fiducial), not a
    per-point fine-focus seed -- OL/AF/AS (seeded via
    estimate_ol_from_wafer_fiducial) already own that job."""
    plane = _get_wafer_fiducial_planes().get("z")
    if plane is None:
        return None
    a, b, c = plane
    return a * x + b * y + c


def estimate_known_good_z_from_wafer_fiducial(x: float, y: float) -> Optional[float]:
    """Z estimate at (x, y) from the wafer-fiducial plane exactly as fit from
    the file - NEVER shifted by precheck_and_correct_wafer_fiducial_planes's
    drift correction, unlike estimate_z_from_wafer_fiducial(). Used as the
    trusted reference _abort_if_stage_z_unsafe sanity-checks its corrective
    move against, precisely because that drift correction is itself computed
    from live stage-Z reads that can (and, in a real incident, did) come back
    wrong - see _FIDUCIAL_Z_SANITY_TOLERANCE_UM."""
    path = CONFIG.get("wafer_fiducial_ol_file")
    plane = _original_wafer_fiducial_z_plane.get(path) if path else None
    if plane is None:
        return None
    a, b, c = plane
    return a * x + b * y + c


# =============================================================================
# SAMPLE_TOPOGRAPHY_CORRECTION: once-per-wafer drift precheck
# =============================================================================
#
# The wafer-fiducial planes are fit once, from a ZEN screenshot capture that
# may be hours or days old by the time a run actually uses them - the wafer
# is also routinely removed and reloaded between sessions, which can shift
# both its height AND its tilt, not just its height. This re-measures EVERY
# point in the same fiducial file fresh (AF/AS/B2F) BEFORE the first region
# starts and fits brand-new OL/StigX/StigY planes directly from those
# measurements - full slope+intercept, not an offset applied to the old
# plane - since N points gives each fit the same statistical basis as the
# original fiducial fit (also N points, unlike the 3-4-point spot-check this
# used to be). ProjX/ProjY/Z are measured too but not corrected - see
# precheck_and_correct_wafer_fiducial_planes's docstring.

@dataclass
class TopographyCheckPoint:
    x: float
    y: float
    z_stage: float
    ol_uA: float
    stig_x: float
    stig_y: float
    proj_x: float
    proj_y: float
    af_success: bool
    minimize_ol_success: bool
    as_success: bool
    b2f_rc: int
    # AS is measured at every point purely to characterize its cost (see
    # measure_topography_check_point) - its result is never used to correct
    # any plane, so timing is the only thing worth keeping from it here.
    as_elapsed_s: float = 0.0


def _select_topography_precheck_points(path: str, n: int) -> List[Tuple[float, float]]:
    """Pick up to n (x, y) fiducial points that have every column
    SAMPLE_TOPOGRAPHY_CORRECTION needs (ol, stigx, stigy, projx, projy) -
    same completeness bar _get_wafer_fiducial_planes() applies to the planes
    themselves. Stepped through the file rather than just taking the first n,
    for a little spatial spread at no real cost."""
    rows = _parse_wafer_fiducial_points(path)
    complete = [r for r in rows if all(v is not None for v in (r[3], r[4], r[5], r[6], r[7]))]
    if not complete:
        return []
    n = max(1, min(n, len(complete)))
    step = max(1, len(complete) // n)
    chosen = complete[::step][:n]
    return [(x, y) for x, y, *_rest in chosen]


def measure_topography_check_point(
    conn: MSEMConnection, x: float, y: float, skip_z: bool = False,
) -> TopographyCheckPoint:
    """One fresh AF -> AS -> B2F measurement at a wafer-fiducial calibration
    point, for the drift precheck below - seeded from the CURRENT
    wafer-fiducial plane, and also captures B2F's projective reading.

    Does NOT call minimize_ol_lens_zen_style: a fiducial-seeded point should
    already put AF within a few um of real focus (see _OL_UA_PER_UM and
    af_fine_range_um), so needing minimize-OL's iterative Z-nudge loop here
    at all would itself be a sign something upstream is wrong, not a normal
    step to run every time. Instead (unless skip_z, see below), if AF's own
    post-convergence AP_DZ_FROM_OLENS exceeds
    topography_focus_correction_threshold_um, this does ONE direct stage Z
    move and re-checks AF once - see the af["success"] block below.

    AS runs at every point where AF succeeded, regardless of whether the
    focus correction converged - per the user, this is deliberately measured
    (for its own timing cost, to help balance speed vs. focus quality) but
    its result is never used to correct any plane; StigX/StigY stay exactly
    as fit from the fiducial file for actual imaging.

    skip_z=True (FIDUCIAL_FOCUS_POINT_XY_REGION_OFFSET, 2026-09-04): removes
    EVERY Z read and Z move in this function - no Z is commanded on the move
    to this point (XY only), no landing verification, and no post-AF
    dz-based corrective move; AF, AS, and B2F alone are trusted to do all
    the real work. Per the operator: running msem_control.main and ZEN
    simultaneously is suspected of causing the AP_STAGE_AT_Z read glitches
    characterized this session (Zeiss API doc 3.1, "Simultaneous use with
    ZEN software" - the API and ZEN don't coordinate access to the same
    hardware), and this wafer's initial Z is already known to be a good
    starting point that nothing here needs to touch unless the code itself
    commands a change - so just don't read or change it at all. Does NOT
    affect the precheck below (SAMPLE_TOPOGRAPHY_CORRECTION), which still
    calls this with skip_z=False (the default) and keeps its existing
    Z-landing/correction behavior unchanged."""
    seed_ol = estimate_ol_from_wafer_fiducial(x, y)
    seed_stig = estimate_stig_from_wafer_fiducial(x, y)
    fiducial_z = None if skip_z else estimate_z_from_wafer_fiducial(x, y)

    if skip_z:
        print(f"[TopographyPrecheck] point xy=({x:.1f},{y:.1f}): moving stage (XY only - "
              f"skip_z: Z is not read or changed at all)")
        stage_move_xy_z(conn, x, y, None, verify_z=False)
    else:
        print(f"[TopographyPrecheck] point xy=({x:.1f},{y:.1f}): moving stage"
              + (f" (z={fiducial_z:.1f} um from fiducial plane)" if fiducial_z is not None
                 else " (no fiducial Z estimate - leaving Z alone)"))
        stage_move_xy_z(conn, x, y, fiducial_z)

    if not skip_z:
        # stage_move_xy_z only WARNS (never aborts) if the commanded Z didn't
        # actually land within tolerance - fine for callers that already know
        # their starting position is sane, but this precheck runs AF and
        # Z-based minimize-OL immediately afterward on whatever the ACTUAL
        # landed Z turns out to be. Added after repeated incidents where
        # minimize-OL's dz-derived correction blew up mid-run - tracing those
        # back showed the Z-nudge clamps alone can't explain the numbers seen
        # unless the stage was ALREADY at a bad Z before minimize-OL even
        # started, i.e. this move silently didn't take. Verify explicitly here
        # and refuse to run AF/minimize-OL on a bad baseline instead of
        # inheriting whatever wrong position the stage happens to be at.
        # retries=0 (stage_z_glitch_check_retries): we just commanded this exact
        # move ourselves, and the chronic AP_STAGE_AT_Z telemetry glitch (see
        # _z_looks_like_telemetry_glitch above) has never once been a real
        # position in every occurrence checked - always a bogus near-zero
        # reading, never a genuine failed move, and retrying never distinguished
        # a real problem from it anyway (see CONFIG comment) while costing real
        # wall-clock time. So when the readback looks like that glitch, assume
        # the commanded Z actually took instead of aborting a perfectly good
        # measurement point over a flaky sensor read.
        actual_z = get_stage_z_stable(
            conn,
            retries=int(CONFIG.get("stage_z_glitch_check_retries", 0)),
            retry_delay_s=float(CONFIG.get("stage_z_glitch_check_retry_delay_s", 0.15)),
        )
        if _z_looks_like_telemetry_glitch(actual_z) and fiducial_z is not None:
            print(f"[TopographyPrecheck] stage Z readback {actual_z:.3f} um at xy=({x:.1f},{y:.1f}) "
                  f"looks like the known telemetry glitch - assuming the commanded move to "
                  f"Z={fiducial_z:.3f} um actually landed and continuing instead of aborting.")
            actual_z = fiducial_z
        z_landed_ok = actual_z == actual_z and _STAGE_Z_ABORT_THRESHOLD_UM <= actual_z <= _STAGE_Z_HARD_MAX_UM
        if z_landed_ok and fiducial_z is not None:
            tol = float(CONFIG.get("z_fiducial_check_threshold_um", 800.0))
            z_landed_ok = abs(actual_z - fiducial_z) <= tol
        if not z_landed_ok:
            print(f"[TopographyPrecheck] SKIP xy=({x:.1f},{y:.1f}): stage did not land at a safe/"
                  f"expected Z after the move (commanded={fiducial_z}, actual={actual_z}) - "
                  f"refusing to run AF/minimize-OL on a bad starting position")
            return TopographyCheckPoint(
                x=x, y=y, z_stage=actual_z, ol_uA=safe_get_double(conn, "AP_OLENS_CURRENT"),
                stig_x=float("nan"), stig_y=float("nan"), proj_x=float("nan"), proj_y=float("nan"),
                af_success=False, minimize_ol_success=False, as_success=False, b2f_rc=-1,
            )

    if seed_ol is not None:
        safe_set_double(conn, "AP_OLENS_CURRENT", float(seed_ol))
        print(f"[TopographyPrecheck] seeded OL={seed_ol:.3f} uA from current wafer-fiducial plane")
    if seed_stig is not None:
        safe_set_double(conn, "AP_STIG_X", float(seed_stig[0]))
        safe_set_double(conn, "AP_STIG_Y", float(seed_stig[1]))
        print(f"[TopographyPrecheck] seeded StigX={seed_stig[0]:.6f}, StigY={seed_stig[1]:.6f}")

    b2f_rc, proj_x, proj_y = run_b2f(conn)
    print(f"[TopographyPrecheck] B2F rc={b2f_rc}, projective=({proj_x:.3f}, {proj_y:.3f})")

    af = run_af_fine_then_coarse(conn)
    coarse_str = "None" if af["conf_coarse"] is None else f"{af['conf_coarse']:.4f}"
    print(f"[TopographyPrecheck] AF: fine={af['conf_fine']:.4f}, coarse={coarse_str}, "
          f"success={af['success']}")

    # Fine AF has a much narrower search range than coarse - when fine fails
    # and coarse rescues it, that's already a signal the fiducial seed was
    # further off than expected. Logged for visibility; no longer used to
    # force a minimize-OL verification pass (there isn't one anymore).
    used_coarse_rescue = (not is_success_conf(af["conf_fine"])) and is_success_conf(af["conf_coarse"])
    if used_coarse_rescue:
        print("[TopographyPrecheck] fine AF failed, coarse AF rescued it")

    as_rc, as_cx, as_cy, as_ok = (None, float("nan"), float("nan"), False)
    as_elapsed_s = 0.0
    minimize_ok = False
    if af["success"]:
        if skip_z:
            # No dz-based correction/gate anymore - trust AF's own success
            # flag outright, per the operator ("let AF, AS, and B2F do all
            # the work"). dz is still logged (informational only) since it
            # costs nothing (AP_DZ_FROM_OLENS is a lens-current readback,
            # not a stage-Z read) and is useful for the ongoing bias check.
            dz_final = safe_get_double(conn, "AP_DZ_FROM_OLENS", default=float("nan"))
            if dz_final == dz_final:
                print(f"[TopographyPrecheck] AF dz={dz_final:+.3f} um (AP_DZ_FROM_OLENS, "
                      f"post-AF residual, informational only - no correction applied)")
            minimize_ok = True
        else:
            dz_final = safe_get_double(conn, "AP_DZ_FROM_OLENS", default=float("nan"))
            # Logged unconditionally (not just when it exceeds the correction
            # threshold below) - added 2026-09-03 so dz has enough data points
            # across future runs to actually check whether AF's residual focus
            # error is biased positive/negative, instead of only the rare
            # over-threshold values that happened to get printed before.
            if dz_final == dz_final:
                print(f"[TopographyPrecheck] AF dz={dz_final:+.3f} um (AP_DZ_FROM_OLENS, post-AF residual)")
            correction_threshold = float(CONFIG.get("topography_focus_correction_threshold_um", 20.0))
            if dz_final == dz_final and abs(dz_final) > correction_threshold:
                z_now = get_stage_z_stable(conn)
                if z_now == z_now:
                    sign = float(CONFIG.get("z_search_dz_sign", -1.0))
                    z_new = z_now + sign * dz_final
                    print(f"[TopographyPrecheck] AF focus error {dz_final:+.3f} um exceeds "
                          f"{correction_threshold:.1f} um - moving stage Z {z_now:.3f} -> {z_new:.3f} um "
                          f"and re-checking AF once (no minimize-OL)")
                    _safe_goto_z(conn, z_new, context="TopographyPrecheck focus correction")
                    if CONFIG.get("wait_stage_settled", True):
                        conn.execute("CMD_WAIT_STAGE_SETTLED")
                    safe_set_double(conn, "AP_OLENS_CURRENT", 0.0)
                    af = run_af_fine_then_coarse(conn)
                    dz_final = safe_get_double(conn, "AP_DZ_FROM_OLENS", default=float("nan"))
                    print(f"[TopographyPrecheck] AF re-check after move: success={af['success']}, "
                          f"dz={dz_final:.3f} um")
                else:
                    print("[TopographyPrecheck] WARNING: could not read stage Z for a "
                          "focus correction move - leaving focus as AF found it")
            # A point whose focus is still unreliable after the corrective move
            # (or that failed AF entirely on the re-check) must NOT feed the
            # new OL plane fit below - af["success"] alone isn't a strong enough
            # bar for that. AS below is independent of this - it's run for
            # timing data regardless, per the user.
            minimize_ok = bool(af["success"]) and (dz_final != dz_final or abs(dz_final) <= correction_threshold)
        if CONFIG["as_enabled"]:
            _t0 = time.time()
            as_rc, as_cx, as_cy, as_ok = run_as(conn)
            as_elapsed_s = time.time() - _t0
            print(f"[TopographyPrecheck] AS: confX={as_cx:.4f} confY={as_cy:.4f} ok={as_ok} "
                  f"({as_elapsed_s:.2f}s, measured only - not applied to any plane)")
    else:
        print("[TopographyPrecheck] AS skipped: AF failed")

    return TopographyCheckPoint(
        x=x, y=y,
        z_stage=(float("nan") if skip_z else get_stage_z_stable(conn)),
        ol_uA=safe_get_double(conn, "AP_OLENS_CURRENT"),
        stig_x=safe_get_double(conn, "AP_STIG_X", default=float("nan")),
        stig_y=safe_get_double(conn, "AP_STIG_Y", default=float("nan")),
        proj_x=proj_x, proj_y=proj_y,
        af_success=af["success"], minimize_ol_success=minimize_ok,
        as_success=as_ok, b2f_rc=b2f_rc, as_elapsed_s=as_elapsed_s,
    )


def _robust_median_offset(vals: List[float]) -> float:
    """Median of vals, with a MAD-based outlier drop first when there are
    enough points for that to mean anything (same 2.5-sigma-equivalent
    convention as choose_robust_one_point_offset's ONE_POINT_OFFSET filter)."""
    med = median(vals)
    if len(vals) < 3:
        return med
    mad = median([abs(v - med) for v in vals])
    threshold = 2.5 * 1.4826 * max(mad, 1e-6)
    inliers = [v for v in vals if abs(v - med) <= threshold]
    return median(inliers) if inliers else med


def precheck_and_correct_wafer_fiducial_planes(
    conn: MSEMConnection, block_folder: Optional[str] = None,
) -> None:
    """SAMPLE_TOPOGRAPHY_CORRECTION-only, once per wafer, before the first
    region: see the module comment above this section. No-ops (with a log
    line explaining why) if disabled, unconfigured, or too few usable
    fiducial points/measurements survive to fit a trustworthy plane - in
    every no-op case, the fiducial planes are simply used as-fitted, same as
    before this feature existed.

    Re-measures EVERY usable point in the wafer-fiducial file (not a small
    subset) and fits brand-new OL(x,y)/StigX(x,y)/StigY(x,y) planes directly
    from those fresh measurements - full slope+intercept each, not an
    offset applied to the old plane - since N points gives each fit the
    same statistical basis as the original fiducial fit (also N points).
    This corrects for the wafer coming back with a genuinely different tilt
    after being reloaded, not just a uniform height shift, and now applies
    to stig the same way it already did to OL.

    ProjX/ProjY/Z are measured too (B2F runs at every point regardless)
    purely for comparison - they are not corrected; they stay exactly as
    fit from the fiducial file and are used unmodified for every mfov
    during imaging. This is a deliberate simplification while focus
    behavior is still being characterized - projective correction can be
    added back later."""
    if not CONFIG.get("topography_precheck_enabled", True):
        print("[TopographyPrecheck] disabled (topography_precheck_enabled=False) - skipping")
        return

    path = CONFIG.get("wafer_fiducial_ol_file")
    if not path:
        print("[TopographyPrecheck] no wafer_fiducial_ol_file configured - skipping")
        return

    planes = _get_wafer_fiducial_planes()
    required = ["ol", "stigx", "stigy", "projx", "projy"]
    missing = [k for k in required if planes.get(k) is None]
    if missing:
        print(f"[TopographyPrecheck] WARNING: {path} has no usable plane for {missing} - "
              f"skipping drift check (SAMPLE_TOPOGRAPHY_CORRECTION will fail its own "
              f"requirement check per-region if this isn't fixed)")
        return

    n_points = int(CONFIG.get("topography_precheck_n_points", 9999))
    points = _select_topography_precheck_points(path, n_points)
    if len(points) < 3:
        print(f"[TopographyPrecheck] only {len(points)} usable fiducial point(s) available "
              f"(need >=3 to fit a plane) - skipping drift check, using the fiducial "
              f"planes as-fitted")
        return

    tissue_offset_um = float(CONFIG.get("topography_precheck_tissue_xy_offset_um", 110.0))
    if tissue_offset_um > 0:
        offset_points = []
        for x, y in points:
            theta = random.uniform(0.0, 2.0 * math.pi)
            offset_points.append((x + tissue_offset_um * math.cos(theta),
                                   y + tissue_offset_um * math.sin(theta)))
        points = offset_points
        print(f"[TopographyPrecheck] offsetting every point by {tissue_offset_um:.1f} um "
              f"(random direction each) to avoid re-measuring already-bleached tissue")

    print("\n" + "=" * 78)
    print(f"[TopographyPrecheck] wafer-wide refit: {len(points)} point(s) from {path}")
    print("=" * 78)

    backup = backup_focus_state(conn)
    start_x = safe_get_double(conn, "AP_STAGE_AT_X")
    start_y = safe_get_double(conn, "AP_STAGE_AT_Y")
    start_z = get_stage_z_stable(conn)

    a_ol, b_ol, c_ol = planes["ol"]
    a_sx, b_sx, c_sx = planes["stigx"]
    a_sy, b_sy, c_sy = planes["stigy"]
    a_px, b_px, c_px = planes["projx"]
    a_py, b_py, c_py = planes["projy"]

    measured: List[Dict[str, float]] = []
    as_timings: List[float] = []

    for x, y in points:
        m = measure_topography_check_point(conn, x, y)
        as_timings.append(m.as_elapsed_s)
        if not m.af_success:
            print(f"[TopographyPrecheck] SKIP xy=({x:.1f},{y:.1f}): AF failed")
            continue
        if not m.minimize_ol_success:
            print(f"[TopographyPrecheck] SKIP xy=({x:.1f},{y:.1f}): focus not within "
                  f"tolerance after correction - excluding from the new OL plane fit")
            continue

        row = {
            "x": x, "y": y,
            "ol_pred": a_ol * x + b_ol * y + c_ol, "ol_meas": m.ol_uA,
            "stigx_pred": a_sx * x + b_sx * y + c_sx, "stigx_meas": m.stig_x,
            "stigy_pred": a_sy * x + b_sy * y + c_sy, "stigy_meas": m.stig_y,
            "projx_pred": a_px * x + b_px * y + c_px, "projx_meas": m.proj_x,
            "projy_pred": a_py * x + b_py * y + c_py, "projy_meas": m.proj_y,
        }
        measured.append(row)
        print(f"[TopographyPrecheck] xy=({x:.1f},{y:.1f}) OL meas={m.ol_uA:.3f} "
              f"pred={row['ol_pred']:.3f} delta={m.ol_uA - row['ol_pred']:+.3f} | "
              f"StigX meas={m.stig_x:.4f} pred={row['stigx_pred']:.4f} "
              f"delta={m.stig_x - row['stigx_pred']:+.4f} | "
              f"StigY meas={m.stig_y:.4f} pred={row['stigy_pred']:.4f} "
              f"delta={m.stig_y - row['stigy_pred']:+.4f}")

    print("[TopographyPrecheck] restoring pre-check focus state and stage position")
    restore_backup_state(conn, backup)
    z_restore = start_z if start_z == start_z else None
    stage_move_xy_z(conn, start_x, start_y, z_restore)

    if as_timings:
        as_sorted = sorted(as_timings)
        mid = as_sorted[len(as_sorted) // 2]
        print(f"[TopographyPrecheck] AS timing across {len(as_timings)} point(s): "
              f"mean={sum(as_timings)/len(as_timings):.2f}s median={mid:.2f}s "
              f"min={min(as_timings):.2f}s max={max(as_timings):.2f}s "
              f"total={sum(as_timings):.2f}s (measured only, not used to correct any plane)")

    run_name = os.path.basename(os.path.normpath(block_folder)) if block_folder else None
    try:
        from focus_diagnostics import generate_precheck_diagnostics
        generate_precheck_diagnostics(
            measured, run_name or "unknown_run",
            diagnostics_dir=CONFIG.get("focus_diagnostics_dir"),
        )
    except Exception as exc:
        print(f"[TopographyPrecheck] WARNING: could not generate precheck diagnostics: {exc}")

    if len(measured) < 3:
        print(f"[TopographyPrecheck] only {len(measured)} usable measurement(s) (need >=3) - "
              f"NOT refitting any plane, using the fiducial planes as-fitted")
        return

    def _refit_and_report(label, a_old, b_old, c_old, meas_key):
        a_new, b_new, c_new = _fit_plane_from_xyz(
            [(r["x"], r["y"], r[meas_key]) for r in measured]
        )
        print(f"[TopographyPrecheck] fiducial {label} plane: {a_old:.6e}*x + {b_old:.6e}*y + {c_old:.6f}")
        print(f"[TopographyPrecheck] fresh    {label} plane: {a_new:.6e}*x + {b_new:.6e}*y + {c_new:.6f}")
        print(f"[TopographyPrecheck] difference:            {a_new - a_old:+.6e}*x + {b_new - b_old:+.6e}*y "
              f"+ {c_new - c_old:+.6f}  (tilt change in slope, level change in intercept)")
        return a_new, b_new, c_new

    planes["ol"] = _refit_and_report("OL", a_ol, b_ol, c_ol, "ol_meas")
    planes["stigx"] = _refit_and_report("StigX", a_sx, b_sx, c_sx, "stigx_meas")
    planes["stigy"] = _refit_and_report("StigY", a_sy, b_sy, c_sy, "stigy_meas")
    print("[TopographyPrecheck] ProjX/ProjY/Z left exactly as fit from the fiducial file - "
          "not corrected in this mode yet")

    # _get_wafer_fiducial_planes() returns this same dict object straight
    # from cache on every later call this run (per-region, for every
    # SAMPLE_TOPOGRAPHY_CORRECTION region) - mutating it in place here is
    # exactly what makes the correction apply for the rest of this run
    # without touching the dispatch code that reads it.
    _wafer_fiducial_cache[path] = planes
    print("[TopographyPrecheck] wafer-fiducial planes corrected for this run")


_WAFER_HOLDER_RE = re.compile(r"[Ww](\d+)[Hh](\d+)")


def resolve_wafer_fiducial_file(json_file: str, default_dir: Optional[str] = None) -> Optional[str]:
    """Auto-discover the wafer fiducial file from the imaging JSON's own
    filename, when wafer_fiducial_ol_file isn't already set explicitly.

    Expects the JSON's basename to start with "w<wafer>h<holder>" (e.g.
    "w10h31_dye_roi_focus.json" -> wafer=10, holder=31 - the same convention
    every imaging JSON, FocusMap.txt collection, and fiducial file has used
    so far) and looks in `default_dir` for:
        1. the exact expected name "w<wafer>h<holder>_fiducial_markers.txt"
        2. failing that, any "w<wafer>h<holder>*fiducial*markers*.txt" match
           (tolerates a sample-name suffix, e.g. "..._dye_fiducial_markers.txt")

    Returns the resolved path, or None if the wafer/holder couldn't be
    parsed from the JSON name or no matching file exists - callers should
    treat None the same as "not configured" (WAFER_FIDUCIAL_PLANE modes
    already fall back gracefully to FIXED in that case).
    """
    default_dir = default_dir or CONFIG.get("wafer_fiducial_default_dir")
    if not default_dir or not os.path.isdir(default_dir):
        print(f"[WaferFiducial] wafer_fiducial_default_dir not found: {default_dir}")
        return None

    m = _WAFER_HOLDER_RE.search(os.path.basename(json_file))
    if not m:
        print(f"[WaferFiducial] could not parse w<N>h<N> from imaging JSON name "
              f"{os.path.basename(json_file)!r} - skipping auto-discovery")
        return None
    wafer, holder = m.group(1), m.group(2)

    exact_name = f"w{wafer}h{holder}_fiducial_markers.txt"
    exact_path = os.path.join(default_dir, exact_name)
    if os.path.isfile(exact_path):
        print(f"[WaferFiducial] auto-discovered {exact_path}")
        return exact_path

    pattern = re.compile(rf"^w{wafer}h{holder}.*fiducial.*markers.*\.txt$", re.IGNORECASE)
    matches = sorted(f for f in os.listdir(default_dir) if pattern.match(f))
    if not matches:
        print(f"[WaferFiducial] no fiducial file found for wafer {wafer} holder {holder} "
              f"in {default_dir} (expected {exact_name!r})")
        return None
    if len(matches) > 1:
        print(f"[WaferFiducial] multiple fiducial files match wafer {wafer} holder {holder}: "
              f"{matches} - using {matches[0]!r}")
    chosen = os.path.join(default_dir, matches[0])
    print(f"[WaferFiducial] auto-discovered {chosen}")
    return chosen


def load_prev_focusmap_fit_plane_and_candidates(
    path: str
) -> Tuple[float, float, float,
           float, float, float,
           float, float, float,
           List[PrevPointCandidate]]:
    # ZEN prev_focusmap column layout (10 comma-separated fields per row) -
    # per Zeiss's own FocusMap.txt documentation (Carl Zeiss Microscopy GmbH,
    # Friedhelm Panteleit, MAT-M, 2016-08-11):
    #   0  Number of region  (slice_id, constant per file e.g. "9")
    #   1  Number of focus point (sp_id)
    #   2  Stage coordinate x
    #   3  Stage coordinate y
    #   4  Objective lens value        (OL_uA)
    #   5  Objective tilt x            (the REAL StigX)
    #   6  Objective tilt y            (the REAL StigY)
    #   7  Autofocus confidence        (failure sentinels: -1/-2/-3/-4 per the
    #                                    MultiSEM API doc, plus -5 as harmless
    #                                    insurance - see AF_AS_CONFIDENCE_FAILURE_SENTINELS)
    #   8  Autostig confidence x       (a confidence score, NOT a stig value)
    #   9  Autostig confidence y       (a confidence score, NOT a stig value)
    #
    # This inverts an earlier (wrong) reading of this file: cols 5/6 were
    # previously treated as "not stig, possibly OL tilt" and cols 8/9 as the
    # real stig values, based on an empirical range match that turned out to
    # be a coincidence. The Zeiss doc is unambiguous that 5/6 ("Objective
    # tilt") are the stigmator values and 8/9 are confidence scores for the
    # autostig fit - column identity confirmed directly against the doc, not
    # re-derived here.
    rows_ol: List[Tuple[float, float, float]] = []
    rows_sx: List[Tuple[float, float, float]] = []
    rows_sy: List[Tuple[float, float, float]] = []
    candidates: List[PrevPointCandidate] = []
    all_valid: Dict[int, Tuple[float, float, float, float, float, float, float, float]] = {}

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
                stig_x_val = float(parts[5])
                stig_y_val = float(parts[6])
                af = float(parts[7])
                as_conf_x = float(parts[8])
                as_conf_y = float(parts[9])
            except Exception:
                continue

            if _is_failed_sentinel(lens, stig_x_val, stig_y_val, af):
                continue

            rows_ol.append((x, y, lens))
            rows_sx.append((x, y, stig_x_val))
            rows_sy.append((x, y, stig_y_val))

            # Lower is better for both AF and AS confidence (0 = perfect,
            # confirmed against ZEN directly) - all three metrics are
            # "smaller combined score wins", picked up by the ascending sort
            # below.
            if metric == "AF":
                score = af
            elif metric == "AS_SUM":
                score = as_conf_x + as_conf_y
            elif metric == "AF_AS":
                score = w_af * af + float(CONFIG.get("w_as", 0.1)) * (as_conf_x + as_conf_y)
            else:
                raise RuntimeError(f"Unknown best_point_metric: {metric!r}")

            cand = PrevPointCandidate(
                sp_id=spid,
                x=x,
                y=y,
                af=af,
                asx=stig_x_val,
                asy=stig_y_val,
                score=score,
                ol=lens,
                as_conf_x=as_conf_x,
                as_conf_y=as_conf_y,
            )
            candidates.append(cand)
            all_valid[spid] = (x, y, af, stig_x_val, stig_y_val, score, lens, as_conf_x, as_conf_y)

    if not rows_ol:
        raise RuntimeError(
            f"No valid points in {path} - even the wafer-fiducial fallback (see "
            f"_fit_plane_local_or_fiducial_anchored) needs at least one local point "
            f"to anchor to."
        )

    if len(rows_ol) < 3:
        # Not enough LOCAL points for this region's own plane fit (the normal
        # case needs >=3 non-collinear points) - anchor the WAFER FIDUCIAL
        # plane's slope to whatever local point(s) this region does have,
        # rather than failing outright. See collect_focus_maps.py /
        # _fit_plane_local_or_fiducial_anchored: this is exactly the reduced
        # single-focus-point-per-region workflow.
        fid_planes = _get_wafer_fiducial_planes()
        a_ol, b_ol, c_ol = _fit_plane_local_or_fiducial_anchored(rows_ol, fid_planes.get("ol"), "OL")
        a_sx, b_sx, c_sx = _fit_plane_local_or_fiducial_anchored(rows_sx, fid_planes.get("stigx"), "StigX")
        a_sy, b_sy, c_sy = _fit_plane_local_or_fiducial_anchored(rows_sy, fid_planes.get("stigy"), "StigY")
    else:
        a_ol, b_ol, c_ol = _fit_plane_from_xyz(rows_ol)
        a_sx, b_sx, c_sx = _fit_plane_from_xyz(rows_sx)
        a_sy, b_sy, c_sy = _fit_plane_from_xyz(rows_sy)

    # Lower score = better (0 = perfect focus/stig, confirmed against ZEN
    # directly) - ascending sort, NOT reverse=True. This was picking the
    # WORST point as "rank 1" and seeding every region from it.
    candidates.sort(key=lambda t: t.score)

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
        x, y, af, asx, asy, score, lens, as_conf_x, as_conf_y = all_valid[manual_id]
        candidates = [PrevPointCandidate(
            sp_id=manual_id, x=x, y=y, af=af, asx=asx, asy=asy, score=score, ol=lens,
            as_conf_x=as_conf_x, as_conf_y=as_conf_y,
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

    thumbnail_format = str(CONFIG.get("thumbnail_filetype", "png"))
    if CONFIG.get("save_thumbnails", True):
        safe_set_double(conn, "DP_THUMBNAIL_LOCATION", 1.0)
        safe_set_string(conn, "STR_REMOTE_THUMBNAIL_FOLDER", tile_folder)
        # SAME prefix as the full-res images (only the extension differs), so a
        # thumbnail and its full-res counterpart share the exact same
        # <prefix><beam:03d> stem and can be matched by beam number directly --
        # section_overview_builder.py relies on this correspondence.
        safe_set_string(conn, "STR_THUMBNAIL_FILENAME_PREFIX", prefix_value)
        safe_set_string(conn, "STR_THUMBNAIL_FILETYPE", thumbnail_format)
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
        "thumbnail_enabled": bool(CONFIG.get("save_thumbnails", True)),
        "thumbnail_format": thumbnail_format,
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

    # Thumbnails are a scan-wide setting (like pixel size/scan rate above), set
    # once per acquisition here; the per-tile folder/prefix/filetype are set
    # alongside the full-res image's own in configure_output_for_tile().
    if CONFIG.get("save_thumbnails", True):
        loc_ok = safe_set_double(conn, "DP_THUMBNAIL_LOCATION", 1.0)
        rb_loc = safe_get_double(conn, "DP_THUMBNAIL_LOCATION", default=float("nan"))
        print(f"[Thumb] DP_THUMBNAIL_LOCATION=1 ok={loc_ok} (readback {rb_loc})")

        scale = float(CONFIG.get("thumbnail_scale", 64))
        scale_ok = safe_set_double(conn, "DP_THUMBNAIL_SCALE", scale)
        rb_scale = safe_get_double(conn, "DP_THUMBNAIL_SCALE", default=float("nan"))
        print(f"[Thumb] DP_THUMBNAIL_SCALE={scale} ok={scale_ok} (readback {rb_scale})")
    else:
        loc_ok = safe_set_double(conn, "DP_THUMBNAIL_LOCATION", 0.0)
        rb_loc = safe_get_double(conn, "DP_THUMBNAIL_LOCATION", default=float("nan"))
        print(f"[Thumb] DP_THUMBNAIL_LOCATION=0 ok={loc_ok} (readback {rb_loc})")

    print("[AcqSettings] AFTER:")
    debug_dump_acq_state(conn, "after")
    return result

# =============================================================================
# Acquire tiles
# =============================================================================

# Beam-arc guard, lazily started inside the tile loop (idempotent). Watches the
# mSEM service log and pauses/recovers acquisition on a beam-clearance arc.
_ARC_GUARD = None

# Per-region timing breakdown (see _run_one_region_body / acquire_tiles),
# appended to as each region finishes so both the running "share of total so
# far" printed per-region and the final cross-run summary (print_final_timing_summary)
# have a single source of truth. Reset at the start of main().
_region_timing_log: List[Dict[str, Any]] = []

# Per-focus-point timing (see measure_one_focus_point), appended to on every
# call regardless of which region is being processed - the caller slices out
# just the entries added during its own region (by index range) rather than
# this being reset per-region, so a stray call from anywhere else never loses
# data. Gives the "focus" vs "stig" split within the combined focus_s phase
# total (AF search + minimize-OL convergence = focus; AS search = stig) -
# previously only visible as free-text in console.log's "[Timing] stage_move=
# ..." lines, not persisted anywhere structured.
_focus_point_timing_log: List[Dict[str, float]] = []


def print_final_timing_summary(block_folder: str):
    """Print (and write to <block_folder>/timing_summary.txt) a breakdown of
    where time went across every region processed so far in this run:
    focus/stigmation vs. stage move+settle vs. beam scan vs. storage overhead,
    and each region's share of the true grand total. Safe to call even after
    a partial/failed run (whatever regions completed are still reported)."""
    region_timings = _region_timing_log
    if not region_timings:
        print("[Timing] No completed regions to summarize.")
        return

    grand_total = sum(r["region_total_s"] for r in region_timings)
    total_focus = sum(r["focus_s"] for r in region_timings)
    total_focus_af = sum(r.get("focus_af_s", 0.0) for r in region_timings)
    total_focus_as = sum(r.get("focus_as_s", 0.0) for r in region_timings)
    total_focus_move = sum(r.get("focus_move_s", 0.0) for r in region_timings)
    total_stage = sum(r["stage_s"] for r in region_timings)
    total_scan = sum(r["scan_s"] for r in region_timings)
    total_storage = sum(r["storage_s"] for r in region_timings)
    total_qc_catchup = sum(r.get("qc_catchup_s", 0.0) for r in region_timings)
    total_qc_catchup_worker = sum(r.get("qc_catchup_worker_s", 0.0) for r in region_timings)
    total_qc_catchup_viewer = sum(r.get("qc_catchup_viewer_s", 0.0) for r in region_timings)

    def _pct(x: float) -> float:
        return (x / grand_total * 100.0) if grand_total > 0 else 0.0

    lines = []
    lines.append("=" * 78)
    lines.append(f"[Timing] FINAL SUMMARY across {len(region_timings)} region(s) - "
                 f"grand total = {grand_total:.1f}s ({grand_total / 60:.1f} min)")
    lines.append("-" * 78)
    # Focus/setup = the once-per-region plane-update measurement (B2F/AF/AS
    # at one point, wafer-fiducial seeding, etc.) - everything BEFORE the
    # tile-acquisition loop starts. Image acq = stage move+settle + beam scan
    # + storage overhead + QC catch-up - everything the tile loop itself
    # accounts for. The two don't always sum to EXACTLY Total (a small,
    # usually <1s gap from ROI/tile-plan generation between the two phases
    # isn't itemized anywhere) but this is the real region-by-region "how
    # long did actually acquiring the images take" breakdown that previously
    # only existed, in a DIFFERENT (and NOT reconciled) form, in the separate
    # qc_timing_summary.txt - which has its OWN, smaller grand total (sourced
    # from each region's live_qc_timing.json, written by the LiveQC viewer
    # subprocess) since it undercounts by whatever's spent on ROI/tile-plan
    # computation and worker/viewer subprocess startup that this file's
    # region_total_s (timed directly around the whole region) does capture.
    lines.append(f"{'Region':<20}{'Focus/setup (s)':>16}{'Image acq (s)':>15}"
                 f"{'Total (s)':>12}{'% of all regions':>20}")
    for r in region_timings:
        acq_s = r["stage_s"] + r["scan_s"] + r["storage_s"] + r.get("qc_catchup_s", 0.0)
        lines.append(
            f"{r['region_name']:<20}{r['focus_s']:>16.1f}{acq_s:>15.1f}"
            f"{r['region_total_s']:>12.1f}{_pct(r['region_total_s']):>19.1f}%"
        )
    lines.append("-" * 78)
    lines.append("Aggregate breakdown (all regions combined):")
    lines.append(f"  focus/stigmation  : {total_focus:>10.1f}s ({_pct(total_focus):5.1f}%)")
    lines.append(f"    - focus (AF+minimize-OL): {total_focus_af:>10.1f}s")
    lines.append(f"    - stig (AS)             : {total_focus_as:>10.1f}s")
    lines.append(f"    - stage/B2F within focus: {total_focus_move:>10.1f}s")
    lines.append(f"  stage move+settle : {total_stage:>10.1f}s ({_pct(total_stage):5.1f}%)")
    lines.append(f"  beam scan         : {total_scan:>10.1f}s ({_pct(total_scan):5.1f}%)")
    lines.append(f"  storage overhead  : {total_storage:>10.1f}s ({_pct(total_storage):5.1f}%)")
    # Time spent waiting for the QC worker (per-beam image quality scoring)
    # and viewer (region-overview stitch + dashboard render) subprocesses to
    # drain their queues and exit AFTER physical imaging (beam scan) already
    # finished - see stop_live_qc_runtime. Real region time, previously
    # invisible to every bucket above (not focus, not imaging).
    lines.append(f"  QC catch-up       : {total_qc_catchup:>10.1f}s ({_pct(total_qc_catchup):5.1f}%)")
    lines.append(f"    - worker (image quality scoring): {total_qc_catchup_worker:>10.1f}s")
    lines.append(f"    - viewer (overview + dashboard)  : {total_qc_catchup_viewer:>10.1f}s")
    lines.append("=" * 78)

    text = "\n".join(lines)
    print("\n" + text)

    try:
        summary_path = os.path.join(block_folder, "timing_summary.txt")
        with open(summary_path, "w") as f:
            f.write(text + "\n")
        print(f"[Timing] wrote {summary_path}")
    except Exception as exc:
        print(f"[Timing] WARNING: could not write timing_summary.txt: {exc}")


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
    focus_elapsed_s: float = 0.0,
    focus_af_s: float = 0.0,
    focus_as_s: float = 0.0,
    focus_move_s: float = 0.0,
) -> Dict[str, float]:
    # focus_elapsed_s/focus_af_s/focus_as_s/focus_move_s are unused here --
    # accepted only so this signature matches make_live_qc_acquire_tiles's
    # monkeypatched replacement (which reports them on the Live QC report),
    # so _run_one_region_body can pass them unconditionally regardless of
    # which implementation is currently bound to the module-level
    # acquire_tiles name.
    acq_tiles_t0 = time.perf_counter()
    route = get_tile_route(tiles)
    print(
        f"[Route] tiles={len(tiles)} route_len={len(route)} "
        f"source={CONFIG.get('route_source', 'REBUILD')} "
        f"mode={CONFIG['route_mode']} direction={CONFIG['travel_direction']} start={CONFIG['start_position']}"
    )
    tile_times = []
    move_times = []
    acq_times = []
    # Split acq_times (scan + storage-flush wait) into its two components, so
    # a region's timing breakdown can tell "the beam was actually scanning"
    # apart from "waiting on a file write to finish" - see print_final_timing_summary.
    scan_times = []
    storage_times = []

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
            scan_times.append(scan_chain)
            storage_times.append(t_flush_prev + t_finish_now + t_sanity + (tiny_ms / 1000.0))

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
    print("  move   :", _summ(move_times))
    print("  scan   :", _summ(scan_times))
    print("  storage:", _summ(storage_times))
    print("  acq    :", _summ(acq_times))
    print("  total  :", _summ(tile_times))
    print("=" * 78 + "\n")

    return {
        "wall_s": time.perf_counter() - acq_tiles_t0,
        "move_s": sum(move_times),
        "scan_s": sum(scan_times),
        "storage_s": sum(storage_times),
        "n_tiles": len(tile_times),
    }


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


def _region_folder_name(region_name: str, region_idx: int) -> str:
    """Sanitize a region name into the folder name it's actually stored
    under (shared by run_one_region's console-log setup and main()'s region
    manifest, so the manifest can point at the right folder without
    duplicating/drifting from this sanitization rule)."""
    safe_label = re.sub(r'[\\/:*?"<>|]+', "_", str(region_name)).strip().rstrip(".")
    return safe_label or f"Region_{region_idx}"


def run_one_region(
    conn: MSEMConnection,
    json_file: str,
    region_name: str,
    region_idx: int,
    block_folder: str,
):
    # ---- Per-section console log: tee stdout+stderr to <block>/<region_name>/console.log
    section_dir_name = _region_folder_name(region_name, region_idx)
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
    region_t0 = time.perf_counter()
    _focus_pt_log_start = len(_focus_point_timing_log)
    print("\n" + "=" * 78)
    print(f"[Region] START region_name={region_name} region_idx={region_idx}")
    print("=" * 78)

    sps, contour_pts = load_region_and_support_points(json_file, region_name)
    print(f"[JSON] region='{region_name}' support_points={len(sps)} contour_vertices={len(contour_pts)}")

    # First thing before any focusing: make sure the stage isn't sitting at
    # the wrong ABSOLUTE Z (e.g. left at Z=0 from a fresh connection/homing) -
    # OL/AF/AS below can only correct small defocus, not that.
    check_and_correct_z_from_fiducial(conn, sps, region_name)
    # The corrective move above doesn't always take (seen in real runs: the
    # readback still shows Z=0 right after commanding a move away from it) -
    # catch that HERE, before spending ~1 min/point on AF/minimize-OL/AS work
    # that (per real logs) can numerically "succeed" even at the wrong Z.
    _abort_if_stage_z_unsafe(conn, region_name, "right after the Z-fiducial startup check", sps=sps)

    # SAMPLE_TOPOGRAPHY_CORRECTION predicts OL/StigX/StigY entirely from the
    # wafer-fiducial planes (see the plane_update_method dispatch below) and
    # never reads a_prev/b_prev/c_prev/prev_candidates - so unlike every
    # other mode, it has no actual need for a region FocusMap.txt to exist.
    # Skip the requirement entirely for this one mode instead of forcing an
    # unused file to be present just to satisfy code shared with the other
    # modes.
    _plane_method_for_prev_focusmap = str(
        CONFIG.get("plane_update_method", "ONE_POINT_OFFSET")).upper()
    if _plane_method_for_prev_focusmap in ("SAMPLE_TOPOGRAPHY_CORRECTION", "FIDUCIAL_FOCUS_POINT_XY_REGION_OFFSET"):
        prev_path = None
        a_prev = b_prev = c_prev = 0.0
        a_sx_prev = b_sx_prev = c_sx_prev = 0.0
        a_sy_prev = b_sy_prev = c_sy_prev = 0.0
        prev_candidates = []
        print(f"[PrevFocusMap] {_plane_method_for_prev_focusmap}: skipping prev-focusmap "
              f"requirement for '{region_name}' - this mode predicts OL/StigX/StigY "
              "from the wafer-fiducial planes instead")
    else:
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

    run_z_search_correction(conn, sps, region_name)

    init_mode = str(CONFIG.get("initial_ol_mode", "PREV_PLANE_AT_FIRST_SP")).upper()
    # SAMPLE_TOPOGRAPHY_CORRECTION/FOCUSMAP_TOPOGRAPHY run B2F once below,
    # BEFORE their own plane_update_method dispatch (further down) ever
    # runs - so initial_ol_mode isn't an independent choice for them, it
    # must match whichever plane source that strategy predicts from, or the
    # focus B2F sees is inconsistent with the plane the rest of the region
    # runs on. The GUI auto-sets+locks this; only reachable here via a
    # hand-edited config or the Advanced overrides escape hatch.
    _early_plane_method = _plane_method_for_prev_focusmap
    _expected_init_mode = {
        "SAMPLE_TOPOGRAPHY_CORRECTION": "WAFER_FIDUCIAL_PLANE",
        "FIDUCIAL_FOCUS_POINT_XY_REGION_OFFSET": "WAFER_FIDUCIAL_PLANE",
        "FOCUSMAP_TOPOGRAPHY": "PREV_PLANE_AT_FIRST_SP",
    }.get(_early_plane_method)
    if _expected_init_mode is not None and init_mode != _expected_init_mode:
        print(f"[Init] WARNING: plane_update_method={_early_plane_method} expects "
              f"initial_ol_mode={_expected_init_mode} (its own plane source) - got "
              f"{init_mode!r} instead, which sets a Beam-to-Fiber-time focus inconsistent "
              f"with the plane this region will actually run on.")
    if init_mode == "PREV_PLANE_AT_FIRST_SP" and sps:
        first_sp = sps[0]
        init_ol = a_prev * first_sp.x + b_prev * first_sp.y + c_prev
        safe_set_double(conn, "AP_OLENS_CURRENT", float(init_ol))
        print(
            f"[Init] Set initial OL to {init_ol:.1f} uA "
            f"(from prev plane at sp[{first_sp.index}] "
            f"xy=({first_sp.x:.1f},{first_sp.y:.1f}))"
        )
    elif init_mode == "WAFER_FIDUCIAL_PLANE" and sps:
        first_sp = sps[0]
        init_ol = estimate_ol_from_wafer_fiducial(first_sp.x, first_sp.y)
        if init_ol is not None:
            safe_set_double(conn, "AP_OLENS_CURRENT", float(init_ol))
            print(
                f"[Init] Set initial OL to {init_ol:.1f} uA "
                f"(wafer fiducial plane at sp[{first_sp.index}] "
                f"xy=({first_sp.x:.1f},{first_sp.y:.1f}))"
            )
        elif CONFIG.get("initial_ol_uA") is not None:
            safe_set_double(conn, "AP_OLENS_CURRENT", float(CONFIG["initial_ol_uA"]))
            print(
                f"[Init] WAFER_FIDUCIAL_PLANE unavailable, falling back to FIXED "
                f"{CONFIG['initial_ol_uA']} uA"
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

    # Real B2F-measured projective reading for this region, if B2F ran once
    # up front - kept in scope for SAMPLE_TOPOGRAPHY_CORRECTION/
    # FOCUSMAP_TOPOGRAPHY below to log/record alongside their plane-predicted
    # ProjX/ProjY (comparison/QC only - AP_PROJECTIVE_DEFLECTOR_X/Y is never
    # set from a script, only ever an outcome of CMD_BEAMS_TO_FIBERS).
    # FIDUCIAL_FOCUS_POINT_XY_REGION_OFFSET runs its OWN B2F below (via
    # measure_topography_check_point, right at its measurement point) so it
    # is excluded here - firing B2F twice per region (once here at the old
    # position, once there at the measurement point) would be redundant.
    b2f_proj_x, b2f_proj_y = None, None
    if str(CONFIG["b2f_mode"]).upper() == "ONCE" and _plane_method_for_prev_focusmap != "FIDUCIAL_FOCUS_POINT_XY_REGION_OFFSET":
        rc, b2f_proj_x, b2f_proj_y = run_b2f(conn)
        print(f"[Region] B2F once rc={rc}, projective=({b2f_proj_x:.3f}, {b2f_proj_y:.3f})")

    a_sx_new, b_sx_new, c_sx_new = a_sx_prev, b_sx_prev, c_sx_prev
    a_sy_new, b_sy_new, c_sy_new = a_sy_prev, b_sy_prev, c_sy_prev
    # ProjX/ProjY plane coefficients for the tile-plan loop to evaluate per-
    # tile (~4769-4787 originally, now shifted by these edits) - only
    # SAMPLE_TOPOGRAPHY_CORRECTION below populates these; every other method
    # leaves both None, so TilePlan.proj_x_pred/proj_y_pred stay None too.
    proj_x_plane = None
    proj_y_plane = None

    plane_update_method = str(CONFIG.get("plane_update_method", "ONE_POINT_OFFSET")).upper()

    # This run's actually-measured support points, in the same 10-column
    # format as the prev_focusmap .txt that seeded this region - written for
    # EVERY region regardless of success/failure (see save_measured_focusmap_txt),
    # so a region that fails can still be diffed against the prior file to
    # tell "prior data is wrong" apart from "something else is wrong".
    measured_focusmap_path = os.path.join(
        block_folder, _region_folder_name(region_name, region_idx), f"{region_name}_HiMC_FocusMap.txt")

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
        try:
            save_measured_focusmap_txt(measured_focusmap_path, region_name, measured_all)
            print(f"[PlaneUpdate] saved measured FocusMap.txt: {measured_focusmap_path}")
        except Exception as exc:
            print(f"[PlaneUpdate] WARNING: could not save measured FocusMap.txt: {exc}")

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

        focus_csv = f"{os.path.splitext(CONFIG['output_focus_points_csv'])[0]}_{region_name}.csv"
        ms, skipped = measure_plane_update_points(
            conn, sps_for_update, region_name=region_name,
            measured_focusmap_out=measured_focusmap_path, focus_points_csv_out=focus_csv)

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

        focus_csv = f"{os.path.splitext(CONFIG['output_focus_points_csv'])[0]}_{region_name}.csv"
        ms, skipped = measure_plane_update_points(
            conn, sps_for_update, region_name=region_name,
            measured_focusmap_out=measured_focusmap_path, focus_points_csv_out=focus_csv)

        a_new, b_new, c_new = fit_new_plane_from_measurements(ms)

        print(f"[PrevPlane] old plane: ol = {a_prev:.6e}*x + {b_prev:.6e}*y + {c_prev:.3f}")
        print(f"[NewPlane]  new plane: ol = {a_new:.6e}*x + {b_new:.6e}*y + {c_new:.3f}")

    elif plane_update_method == "CURRENT_ALL_POINTS_QUADRATIC_RESIDUAL_IDW":
        sps_for_update = select_all_support_points_for_plane_update(sps)

        print("[PlaneUpdate] Using ALL support points:")
        for sp in sps_for_update:
            print(f"  sp[{sp.index}] xy=({sp.x:.3f}, {sp.y:.3f}) z={sp.z:.3f}")

        focus_csv = f"{os.path.splitext(CONFIG['output_focus_points_csv'])[0]}_{region_name}.csv"
        ms, skipped = measure_plane_update_points(
            conn, sps_for_update, region_name=region_name,
            measured_focusmap_out=measured_focusmap_path, focus_points_csv_out=focus_csv)

        quad_coef = fit_quadratic_from_measurements(ms)
        residual_pts = build_quadratic_residual_points(ms, quad_coef)

        print("[Quadratic+ResidualIDW] ready for tile prediction.")

    elif plane_update_method == "MANUAL_POINTS":
        manual_points = load_manual_focus_points(
            CONFIG.get("manual_focus_points_file"), region_name)
        sps_manual = build_manual_support_points(manual_points)
        mode = str(CONFIG.get("manual_focus_mode", "SKIP_AF")).upper()
        print(f"[PlaneUpdate] MANUAL_POINTS: {len(sps_manual)} operator-placed point(s), mode={mode}")
        for sp in sps_manual:
            print(f"  sp[{sp.index}] xy=({sp.x:.3f}, {sp.y:.3f})")

        ms, skipped = measure_manual_focus_points(conn, sps_manual, manual_points)
        focus_csv = f"{os.path.splitext(CONFIG['output_focus_points_csv'])[0]}_{region_name}.csv"
        save_focus_points_csv(focus_csv, ms, skipped=skipped)
        print(f"[CSV] saved manual measurements: {focus_csv} ({len(ms)} ok, {len(skipped)} skipped)")
        if not ms:
            raise RuntimeError(
                f"MANUAL_POINTS: no usable measurements for region '{region_name}' "
                f"({len(skipped)} skipped).")

        a_new, b_new, c_new = _fit_focus_plane_or_flat(
            [(m.x, m.y, m.ol_uA) for m in ms], "OL")
        # Stig planes: update ONLY from points where stig was actually determined
        # (operator hand-set under SKIP_AF, or AS succeeded under SEED_AF); those
        # carry as_success=True. If no point set stig, keep the prev-focusmap stig
        # planes (assigned above) so a focus-only manual run doesn't flatten tilt.
        stig_rows_x = [(m.x, m.y, m.stig_x) for m in ms if m.as_success and m.stig_x == m.stig_x]
        stig_rows_y = [(m.x, m.y, m.stig_y) for m in ms if m.as_success and m.stig_y == m.stig_y]
        if stig_rows_x and stig_rows_y:
            a_sx_new, b_sx_new, c_sx_new = _fit_focus_plane_or_flat(stig_rows_x, "StigX")
            a_sy_new, b_sy_new, c_sy_new = _fit_focus_plane_or_flat(stig_rows_y, "StigY")

        print(f"[NewPlane] OL    = {a_new:.6e}*x + {b_new:.6e}*y + {c_new:.3f}")
        print(f"[NewPlane] StigX = {a_sx_new:.6e}*x + {b_sx_new:.6e}*y + {c_sx_new:.6f}")
        print(f"[NewPlane] StigY = {a_sy_new:.6e}*x + {b_sy_new:.6e}*y + {c_sy_new:.6f}")

    elif plane_update_method == "WAFER_FIDUCIAL_DIRECT":
        if not CONFIG.get("wafer_fiducial_ol_file"):
            raise RuntimeError(
                "WAFER_FIDUCIAL_DIRECT requires CONFIG['wafer_fiducial_ol_file'] to be set.")

        wf_planes = _get_wafer_fiducial_planes()
        if wf_planes.get("ol") is None:
            raise RuntimeError(
                f"WAFER_FIDUCIAL_DIRECT: no usable OL plane fit from "
                f"{CONFIG['wafer_fiducial_ol_file']} (need >=3 valid rows) - "
                f"cannot set region '{region_name}' plane with zero measurement.")

        a_new, b_new, c_new = wf_planes["ol"]
        print(f"[PlaneUpdate] WAFER_FIDUCIAL_DIRECT: using wafer-fiducial plane directly "
              f"for '{region_name}' - zero per-region hardware measurement")
        print(f"[NewPlane] OL    = {a_new:.6e}*x + {b_new:.6e}*y + {c_new:.3f} (wafer fiducial)")

        if wf_planes.get("stigx") is not None and wf_planes.get("stigy") is not None:
            a_sx_new, b_sx_new, c_sx_new = wf_planes["stigx"]
            a_sy_new, b_sy_new, c_sy_new = wf_planes["stigy"]
            print(f"[NewPlane] StigX = {a_sx_new:.6e}*x + {b_sx_new:.6e}*y + {c_sx_new:.6f} (wafer fiducial)")
            print(f"[NewPlane] StigY = {a_sy_new:.6e}*x + {b_sy_new:.6e}*y + {c_sy_new:.6f} (wafer fiducial)")
        else:
            print("[PlaneUpdate] WAFER_FIDUCIAL_DIRECT: no wafer-fiducial stig plane available - "
                  f"keeping inherited prev-focusmap StigX/StigY plane for '{region_name}'")

    elif plane_update_method == "SAMPLE_TOPOGRAPHY_CORRECTION":
        # EXPERIMENTAL - see MODULE_OVERVIEW.md / the HI-MC "Plane update
        # method" tooltip. Zero AF/AS measurement: OL/StigX/StigY/ProjX/ProjY
        # for every mFOV come from planes fit to a wafer-fiducial CSV built
        # from ZEN screenshots (built externally, not by this codebase). Z is not
        # separately predicted per-tile - it's governed by the existing
        # one-time check_and_correct_z_from_fiducial/wafer_fiducial_z_align_
        # enabled mechanism (same wafer-fiducial Z-plane), since in practice
        # the fiducial data's Z is effectively constant across a wafer.
        if not CONFIG.get("wafer_fiducial_ol_file"):
            raise RuntimeError(
                "SAMPLE_TOPOGRAPHY_CORRECTION requires CONFIG['wafer_fiducial_ol_file'] to be set.")

        wf_planes = _get_wafer_fiducial_planes()
        required = ["ol", "stigx", "stigy", "projx", "projy"]
        missing = [k for k in required if wf_planes.get(k) is None]
        if missing:
            raise RuntimeError(
                f"SAMPLE_TOPOGRAPHY_CORRECTION: {CONFIG['wafer_fiducial_ol_file']} is missing "
                f"a usable plane for {missing} (need >=3 valid rows with that column) - cannot "
                f"predict every value this mode requires for region '{region_name}'.")

        if str(CONFIG["b2f_mode"]).upper() != "ONCE":
            print("[PlaneUpdate] WARNING: SAMPLE_TOPOGRAPHY_CORRECTION expects b2f_mode=ONCE "
                  "(ZEN's Beam-to-Fiber is what actually establishes the projective alignment "
                  f"- current b2f_mode={CONFIG['b2f_mode']!r} means it did not run for "
                  f"'{region_name}').")

        a_new, b_new, c_new = wf_planes["ol"]
        a_sx_new, b_sx_new, c_sx_new = wf_planes["stigx"]
        a_sy_new, b_sy_new, c_sy_new = wf_planes["stigy"]
        proj_x_plane = wf_planes["projx"]
        proj_y_plane = wf_planes["projy"]
        print(f"[PlaneUpdate] SAMPLE_TOPOGRAPHY_CORRECTION: predicting every value from the "
              f"wafer-fiducial planes for '{region_name}' - zero per-region hardware measurement")
        print(f"[NewPlane] OL    = {a_new:.6e}*x + {b_new:.6e}*y + {c_new:.3f} (wafer fiducial)")
        print(f"[NewPlane] StigX = {a_sx_new:.6e}*x + {b_sx_new:.6e}*y + {c_sx_new:.6f} (wafer fiducial)")
        print(f"[NewPlane] StigY = {a_sy_new:.6e}*x + {b_sy_new:.6e}*y + {c_sy_new:.6f} (wafer fiducial)")
        apx, bpx, cpx = proj_x_plane
        apy, bpy, cpy = proj_y_plane
        print(f"[NewPlane] ProjX = {apx:.6e}*x + {bpx:.6e}*y + {cpx:.3f} (wafer fiducial, recorded only)")
        print(f"[NewPlane] ProjY = {apy:.6e}*x + {bpy:.6e}*y + {cpy:.3f} (wafer fiducial, recorded only)")
        if b2f_proj_x is not None:
            region_x = sps[0].x if sps else 0.0
            region_y = sps[0].y if sps else 0.0
            pred_px = apx * region_x + bpx * region_y + cpx
            pred_py = apy * region_x + bpy * region_y + cpy
            print(f"[PlaneUpdate] real B2F reading=({b2f_proj_x:.3f}, {b2f_proj_y:.3f}) vs. "
                  f"plane-predicted at region's first support point=({pred_px:.3f}, {pred_py:.3f})")

    elif plane_update_method == "FOCUSMAP_TOPOGRAPHY":
        # EXPERIMENTAL - see MODULE_OVERVIEW.md / the HI-MC "Plane update
        # method" tooltip. Zero AF/AS measurement: OL/StigX/StigY for every
        # mFOV come from planes already fit to this region's OWN
        # FocusMap.txt (a_prev/b_prev/c_prev etc. above - loaded
        # unconditionally for every plane_update_method, not new here).
        # FocusMap.txt has no Z column, so Z is governed by the wafer-
        # fiducial Z-plane via the existing check_and_correct_z_from_fiducial
        # mechanism instead - same as SAMPLE_TOPOGRAPHY_CORRECTION.
        # ProjX/ProjY: only the real B2F-measured reading is recorded for
        # now (see b2f_proj_x/b2f_proj_y above) - fitting a cross-region
        # ProjX/ProjY plane from accumulated B2F readings ("we assume
        # regions all lie on a tilted flat plane") is a deferred follow-up,
        # not yet implemented (bootstrapping details still being worked out
        # with the user - see MODULE_OVERVIEW.md).
        if not CONFIG.get("wafer_fiducial_ol_file"):
            print("[PlaneUpdate] WARNING: FOCUSMAP_TOPOGRAPHY has no wafer_fiducial_ol_file set - "
                  "Z will only ever be whatever check_and_correct_z_from_fiducial's coarse "
                  f"safety check leaves it at for '{region_name}' (no Z-plane to align to).")
        elif _get_wafer_fiducial_planes().get("z") is None:
            print(f"[PlaneUpdate] WARNING: FOCUSMAP_TOPOGRAPHY: no usable Z plane in "
                  f"{CONFIG['wafer_fiducial_ol_file']} - same Z caveat as above for '{region_name}'.")

        if str(CONFIG["b2f_mode"]).upper() != "ONCE":
            print("[PlaneUpdate] WARNING: FOCUSMAP_TOPOGRAPHY expects b2f_mode=ONCE (ZEN's "
                  "Beam-to-Fiber is what actually establishes the projective alignment - "
                  f"current b2f_mode={CONFIG['b2f_mode']!r} means it did not run for "
                  f"'{region_name}').")

        a_new, b_new, c_new = a_prev, b_prev, c_prev
        print(f"[PlaneUpdate] FOCUSMAP_TOPOGRAPHY: using this region's own FocusMap.txt plane "
              f"directly for '{region_name}' - zero per-region hardware measurement")
        print(f"[NewPlane] OL    = {a_new:.6e}*x + {b_new:.6e}*y + {c_new:.3f} (from FocusMap.txt)")
        print(f"[NewPlane] StigX = {a_sx_new:.6e}*x + {b_sx_new:.6e}*y + {c_sx_new:.6f} (from FocusMap.txt)")
        print(f"[NewPlane] StigY = {a_sy_new:.6e}*x + {b_sy_new:.6e}*y + {c_sy_new:.6f} (from FocusMap.txt)")
        if b2f_proj_x is not None:
            print(f"[PlaneUpdate] real B2F reading for '{region_name}': "
                  f"({b2f_proj_x:.3f}, {b2f_proj_y:.3f}) - recorded, cross-region "
                  f"interpolation not yet implemented")

    elif plane_update_method == "FIDUCIAL_FOCUS_POINT_XY_REGION_OFFSET":
        # EXPERIMENTAL, 2026-09-03. Per-region variant of
        # SAMPLE_TOPOGRAPHY_CORRECTION's once-per-wafer precheck: the precheck
        # measures EVERY wafer-fiducial point once, up front, which surfaced a
        # real problem (many points failing z_landed_ok, timing out most of
        # the run's usefulness for very little payoff). Instead, this does
        # exactly ONE fresh B2F -> AF -> AS measurement per region (the same
        # measure_topography_check_point() routine the precheck uses, so the
        # move/land-check/seed/B2F/AF/AS sequence is identical and already
        # exercised), at a point offset from this region's own support point
        # by topography_precheck_tissue_xy_offset_um in a random direction each
        # time (same rationale as the precheck's tissue offset: don't keep
        # bleaching the exact same spot across repeated test runs).
        #
        # A single point cannot determine a new tilt, so the wafer-fiducial
        # plane's slope (a, b) is kept exactly as fit; only the intercept (c)
        # is re-leveled so the plane passes through what was actually
        # measured here, right now, for THIS region - i.e. c_new = measured -
        # (a*x + b*y). This is the same "offset a known plane by one fresh
        # point" idea as the legacy ONE_POINT_OFFSET method, just with a
        # simple deterministic single-point measurement instead of
        # ONE_POINT_OFFSET's multi-candidate MAD-filtered history.
        if not CONFIG.get("wafer_fiducial_ol_file"):
            raise RuntimeError(
                "FIDUCIAL_FOCUS_POINT_XY_REGION_OFFSET requires CONFIG['wafer_fiducial_ol_file'] to be set.")
        if not sps:
            raise RuntimeError(
                f"FIDUCIAL_FOCUS_POINT_XY_REGION_OFFSET: region '{region_name}' has no support points "
                "to measure from.")

        wf_planes = _get_wafer_fiducial_planes()
        required = ["ol", "stigx", "stigy"]
        missing = [k for k in required if wf_planes.get(k) is None]
        if missing:
            raise RuntimeError(
                f"FIDUCIAL_FOCUS_POINT_XY_REGION_OFFSET: {CONFIG['wafer_fiducial_ol_file']} is missing "
                f"a usable plane for {missing} (need >=3 valid rows with that column).")

        a_ol_fid, b_ol_fid, c_ol_fid = wf_planes["ol"]
        a_sx_fid, b_sx_fid, c_sx_fid = wf_planes["stigx"]
        a_sy_fid, b_sy_fid, c_sy_fid = wf_planes["stigy"]

        base_sp = sps[0]
        tissue_offset_um = float(CONFIG.get("topography_precheck_tissue_xy_offset_um", 110.0))
        if tissue_offset_um > 0:
            theta = random.uniform(0.0, 2.0 * math.pi)
            meas_x = base_sp.x + tissue_offset_um * math.cos(theta)
            meas_y = base_sp.y + tissue_offset_um * math.sin(theta)
        else:
            meas_x, meas_y = base_sp.x, base_sp.y

        print(f"[PlaneUpdate] FIDUCIAL_FOCUS_POINT_XY_REGION_OFFSET: measuring one point for "
              f"'{region_name}' at xy=({meas_x:.1f},{meas_y:.1f}) "
              f"(sp[0] offset by {tissue_offset_um:.1f} um) - B2F -> AF -> AS "
              f"(skip_z: no Z reads/moves, initial Z is trusted as-is)")
        pt = measure_topography_check_point(conn, meas_x, meas_y, skip_z=True)
        if b2f_proj_x is None:
            b2f_proj_x, b2f_proj_y = pt.proj_x, pt.proj_y

        if not pt.af_success:
            print(f"[PlaneUpdate] FIDUCIAL_FOCUS_POINT_XY_REGION_OFFSET: AF failed at the measurement "
                  f"point for '{region_name}' - keeping the wafer-fiducial plane unchanged "
                  "(no re-level this region)")
            a_new, b_new, c_new = a_ol_fid, b_ol_fid, c_ol_fid
            a_sx_new, b_sx_new, c_sx_new = a_sx_fid, b_sx_fid, c_sx_fid
            a_sy_new, b_sy_new, c_sy_new = a_sy_fid, b_sy_fid, c_sy_fid
        else:
            a_new, b_new = a_ol_fid, b_ol_fid
            c_new = pt.ol_uA - (a_new * meas_x + b_new * meas_y)
            a_sx_new, b_sx_new = a_sx_fid, b_sx_fid
            a_sy_new, b_sy_new = a_sy_fid, b_sy_fid
            c_sx_new = (pt.stig_x - (a_sx_new * meas_x + b_sx_new * meas_y)) if pt.stig_x == pt.stig_x else c_sx_fid
            c_sy_new = (pt.stig_y - (a_sy_new * meas_x + b_sy_new * meas_y)) if pt.stig_y == pt.stig_y else c_sy_fid
            print(f"[PlaneUpdate] FIDUCIAL_FOCUS_POINT_XY_REGION_OFFSET: measured OL={pt.ol_uA:.3f} uA, "
                  f"StigX={pt.stig_x:.6f}, StigY={pt.stig_y:.6f} at xy=({meas_x:.1f},{meas_y:.1f}) "
                  f"- re-leveling wafer-fiducial plane intercept(s) for '{region_name}'")

        print(f"[NewPlane] OL    = {a_new:.6e}*x + {b_new:.6e}*y + {c_new:.3f} (region offset)")
        print(f"[NewPlane] StigX = {a_sx_new:.6e}*x + {b_sx_new:.6e}*y + {c_sx_new:.6f} (region offset)")
        print(f"[NewPlane] StigY = {a_sy_new:.6e}*x + {b_sy_new:.6e}*y + {c_sy_new:.6f} (region offset)")
        if b2f_proj_x is not None:
            print(f"[PlaneUpdate] real B2F reading for '{region_name}': "
                  f"({b2f_proj_x:.3f}, {b2f_proj_y:.3f}) - recorded only, not applied "
                  "to any plane")

    else:
        raise RuntimeError(f"Unknown plane_update_method: {plane_update_method}")

    # Everything above this point (support-point/prev-focusmap loading,
    # Z-search, AF/AS plane-fitting) is the "focus/stigmation" phase; tile
    # acquisition below is timed separately in acquire_tiles() - see
    # print_final_timing_summary for how the two get combined.
    focus_elapsed_s = time.perf_counter() - region_t0

    # Split focus_elapsed_s into "focus" (AF search + minimize-OL convergence
    # - the two costs that balloon together whenever fine AF fails and falls
    # back to coarse, see used_coarse_rescue in measure_one_focus_point) vs
    # "stig" (AS search alone) vs pure per-point stage/B2F overhead - lets
    # the QC report show where the focus phase's time actually went instead
    # of just one opaque total.
    _focus_pts_this_region = _focus_point_timing_log[_focus_pt_log_start:]
    focus_af_s = sum(e["af_s"] + e["minimize_s"] for e in _focus_pts_this_region)
    focus_as_s = sum(e["as_s"] for e in _focus_pts_this_region)
    focus_move_s = sum(e["stage_s"] + e["b2f_s"] for e in _focus_pts_this_region)

    # Final gate before committing to actual tile acquisition: catches Z
    # having drifted back to 0 sometime during the per-point focus/plane-
    # update work above (seen in real runs even when the startup check right
    # after connecting found nothing wrong) - the last possible point to
    # refuse to image this region instead of doing so at the wrong height.
    _abort_if_stage_z_unsafe(conn, region_name, "right before tile acquisition", sps=sps)

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

        proj_x_pred = None
        proj_y_pred = None
        if proj_x_plane is not None and proj_y_plane is not None:
            apx, bpx, cpx = proj_x_plane
            apy, bpy, cpy = proj_y_plane
            proj_x_pred = float(apx * tx + bpx * ty + cpx)
            proj_y_pred = float(apy * tx + bpy * ty + cpy)

        plans.append(TilePlan(tile_idx=i, x=tx, y=ty, z_pred=None, ol_pred=float(ol),
                              stig_x_pred=float(sx), stig_y_pred=float(sy),
                              proj_x_pred=proj_x_pred, proj_y_pred=proj_y_pred))

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
        tile_timing = acquire_tiles(conn, tiles, plans, block_folder=block_folder,
                                     focus_elapsed_s=focus_elapsed_s,
                                     focus_af_s=focus_af_s, focus_as_s=focus_as_s,
                                     focus_move_s=focus_move_s)
    finally:
        CONFIG["region_idx"] = old_region_idx
        CONFIG["region_name"] = old_region_name
        CONFIG["imaging_json_path"] = old_imaging_json_path

    region_total_s = time.perf_counter() - region_t0
    breakdown = {
        "region_name": region_name,
        "region_idx": region_idx,
        "focus_s": focus_elapsed_s,
        "focus_af_s": focus_af_s,
        "focus_as_s": focus_as_s,
        "focus_move_s": focus_move_s,
        "stage_s": tile_timing["move_s"],
        "scan_s": tile_timing["scan_s"],
        "storage_s": tile_timing["storage_s"],
        # Only present under Live QC (see stop_live_qc_runtime / the wrapped
        # acquire_tiles in main_live_qc.py) - the base acquire_tiles here has
        # no worker/viewer subprocess to wait on, so nothing to catch up on.
        "qc_catchup_s": tile_timing.get("qc_catchup_s", 0.0),
        "qc_catchup_worker_s": tile_timing.get("qc_catchup_worker_s", 0.0),
        "qc_catchup_viewer_s": tile_timing.get("qc_catchup_viewer_s", 0.0),
        "n_tiles": tile_timing["n_tiles"],
        "region_total_s": region_total_s,
    }
    _region_timing_log.append(breakdown)

    running_total_s = sum(r["region_total_s"] for r in _region_timing_log)
    share_of_running_total = (region_total_s / running_total_s * 100.0) if running_total_s > 0 else 0.0

    def _share(x: float) -> float:
        return (x / region_total_s * 100.0) if region_total_s > 0 else 0.0

    print("\n" + "-" * 78)
    print(f"[Timing] Region '{region_name}' breakdown ({tile_timing['n_tiles']} tile(s), "
          f"{region_total_s:.1f}s total):")
    print(f"  focus/stigmation  : {focus_elapsed_s:>8.1f}s ({_share(focus_elapsed_s):5.1f}%)")
    print(f"  stage move+settle : {tile_timing['move_s']:>8.1f}s ({_share(tile_timing['move_s']):5.1f}%)")
    print(f"  beam scan         : {tile_timing['scan_s']:>8.1f}s ({_share(tile_timing['scan_s']):5.1f}%)")
    print(f"  storage overhead  : {tile_timing['storage_s']:>8.1f}s ({_share(tile_timing['storage_s']):5.1f}%)")
    print(f"[Timing] Region '{region_name}' is {share_of_running_total:.1f}% of the "
          f"{running_total_s:.1f}s processed across {len(_region_timing_log)} region(s) so far.")
    print("-" * 78)

    print(f"[Region] DONE region_name={region_name}")
    return breakdown

# =============================================================================
# MAIN
# =============================================================================

def _count_valid_focusmap_rows(path: str) -> int:
    """Count rows load_prev_focusmap_fit_plane_and_candidates() would actually
    accept: >=10 comma fields, a parseable x,y,OL,AF-confidence,StigX,StigY,
    AND not one of ZEN's -4/-5 failure sentinels (see _is_failed_sentinel).

    A shallower version of this used to only check "does it look like a data
    row" (right field count + parseable x,y), which let a file with real rows
    that all happened to be failed AF/AS sentinels pass preflight as OK, only
    to blow up later inside the plane fit ("Not enough valid points") after
    the beam was already on. Matching the same acceptance rule here catches
    that case up front instead."""
    n = 0
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                parts = [p.strip() for p in line.split(",")]
                if len(parts) < 10:
                    continue
                try:
                    float(parts[2]); float(parts[3])  # x, y
                    lens = float(parts[4])
                    stig_x_val = float(parts[5])
                    stig_y_val = float(parts[6])
                    af = float(parts[7])
                except (ValueError, IndexError):
                    continue
                if _is_failed_sentinel(lens, stig_x_val, stig_y_val, af):
                    continue
                n += 1
    except Exception:
        return 0
    return n


def preflight_resolve_prev_focusmaps(json_file: str, region_names: List[str]) -> Tuple[List[str], Dict[str, str]]:
    """Resolve + validate the prev_focusmap .txt for EVERY region up front.

    Reuses resolve_prev_focusmap_path() (so resolution semantics are identical to
    the per-region path) but runs once, before any hardware work, so a missing
    slice_<N>_FocusMap.txt aborts the whole run immediately instead of after hours
    of imaging. Honors CONFIG['preflight_skip_missing']:
        False (default) -> raise (abort) if any region has no usable file
        True            -> warn + drop those regions, return the survivors
    Prints a [Preflight] summary (keywords FAILED/WARNING/skip so the GUI colours
    the lines). Returns (region_names to actually process, {name: reason} for
    every region skipped) - the caller surfaces the reasons in the region
    manifest/QC report as status="skipped", instead of those regions just
    silently not appearing anywhere (which is indistinguishable from "never
    part of this run's plan at all" and was confusing enough on its own to
    prompt a bug investigation).
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
            n_rows = _count_valid_focusmap_rows(p)
            configured_min = max(1, int(CONFIG.get("plane_update_min_points", 3) or 3))
            # A region with 1-2 local points can still be resolved (see
            # _fit_plane_local_or_fiducial_anchored) by anchoring the
            # wafer-wide fiducial plane's slope to the local point(s) - but
            # only if a fiducial plane actually covers OL/StigX/StigY, or
            # that fallback fails too, just later (inside the plane fit,
            # mid-run, after the preflight already said "OK"). Preflight
            # needs to know that NOW, not discover it hours into a run.
            fid_planes = _get_wafer_fiducial_planes()
            fiducial_covers_all = all(
                fid_planes.get(k) is not None for k in ("ol", "stigx", "stigy")
            )
            min_rows = 1 if fiducial_covers_all else max(3, configured_min)
            if n_rows < min_rows:
                if n_rows > 0 and not fiducial_covers_all:
                    raise ValueError(
                        f"only {n_rows} usable data row(s) after dropping failed AF/AS "
                        f"sentinels (need >=3, or >=1 with a wafer fiducial plane covering "
                        f"OL/StigX/StigY - none configured/usable): {p}")
                raise ValueError(f"only {n_rows} usable data row(s) after dropping failed "
                                  f"AF/AS sentinels (need >={min_rows}): {p}")
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
        return region_names, {}

    if not CONFIG.get("preflight_skip_missing", False):
        bad = ", ".join(rn for rn, _ in failures)
        raise RuntimeError(
            f"[Preflight] FAILED: {len(failures)} region(s) have no usable prev_focusmap "
            f"({bad}). Aborting before acquisition. Set preflight_skip_missing=true to "
            f"skip them and image the rest instead.")

    ok_names = [rn for rn in region_names if rn in resolved]
    skip_reasons = dict(failures)
    for rn, reason in failures:
        print(f"[Preflight] WARNING: skipping region {rn} (no prev focusmap: {reason})")
    if not ok_names:
        raise RuntimeError(
            "[Preflight] FAILED: no region has a usable prev_focusmap; nothing to image.")
    print(f"[Preflight] proceeding with {len(ok_names)} region(s): {ok_names}")
    return ok_names, skip_reasons


def _write_region_manifest(block_folder: str, entries: List[Dict[str, Any]]) -> None:
    """Write <block_folder>/region_manifest.json: the authoritative list of
    every region this run intends to process, with live status
    ("pending"/"running"/"done"/"failed"). This is what lets the QC report
    (see live_qc_support._write_block_index_html) show what SHOULD be
    imaged and how many remain, even for a region that fails during focus -
    before acquire_tiles ever runs, so it would otherwise never get a Live
    QC dashboard, folder scan, or any other record at all.

    Written atomically (tmp + os.replace) so a concurrent reader (the QC
    report being rebuilt) never sees a half-written file."""
    path = os.path.join(block_folder, "region_manifest.json")
    payload = {"generated_at": time.strftime("%Y-%m-%d %H:%M:%S"), "regions": entries}
    try:
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)
        os.replace(tmp, path)
    except Exception as exc:
        print(f"[RegionManifest] WARNING: could not write {path}: {exc}")


def _refresh_block_index(
    block_folder: str,
    canonical_region_names: Optional[List[str]] = None,
    imaging_json_path: Optional[str] = None,
) -> None:
    """Best-effort refresh of <block_folder>/<wafer>_<timestamp>_qc_report.html
    (see live_qc_support._qc_report_filename) right after the
    manifest changes, so the QC report reflects a region's pending/running/
    done/failed status immediately - rather than only whenever some
    region's own Live QC viewer subprocess happens to write it, which never
    happens at all for a region that fails before acquire_tiles starts.

    Also refreshes the wafer-level global report - normally one directory up
    from block_folder, across every batch, EXCEPT for a partial-retake batch,
    whose block_folder lives a level deeper under output_root/partial_retakes
    (see main()'s auto-routing) - CONFIG["_wafer_output_root"] (set once at
    the top of main(), before any such redirect) is used instead so the
    global report always lands at the true wafer root, never nested inside
    partial_retakes/. canonical_region_names, when given, is every region
    defined in the imaging JSON regardless of what this batch was
    filtered/scoped to - it's both this batch's "N of TOTAL wafer regions"
    indicator and the global report's definitive region list (so a region no
    batch has ever attempted still shows up as "Not started" instead of
    silently missing)."""
    try:
        from live_qc_support import _write_block_index_html, write_wafer_qc_report
    except Exception as exc:
        print(f"[RegionManifest] live_qc_support unavailable, skipping index refresh: {exc}")
        return
    wafer_total = len(canonical_region_names) if canonical_region_names else None
    wafer_runs_root = CONFIG.get("_wafer_output_root") or os.path.dirname(block_folder)
    try:
        # review_root=block_folder (NOT wafer_runs_root): each batch report
        # is standalone with its own Accept/Retake state, per the operator
        # (2026-09-04) - sharing one wafer-wide region_review_status.json
        # meant a wafer routinely re-imaged across several independent
        # batches (same region names, different attempts) showed one
        # batch's review clicks bleeding into every other batch's report for
        # that same region name, which read as "opening batch 2 shows batch
        # 1's results". The global wafer report below is still the one place
        # that aggregates across batches and drives retake_regions.txt - it
        # keeps the wafer-wide review file; only the per-batch report's own
        # copy changed.
        _write_block_index_html(block_folder, block_label=os.path.basename(block_folder), wafer_total=wafer_total,
                                 review_root=block_folder)
    except Exception as exc:
        print(f"[RegionManifest] WARNING: index refresh failed: {exc}")
    try:
        write_wafer_qc_report(
            runs_root=wafer_runs_root,
            canonical_region_names=canonical_region_names,
            wafer_label=os.path.basename(block_folder),
            imaging_json_path=imaging_json_path,
        )
    except Exception as exc:
        print(f"[RegionManifest] WARNING: global wafer report refresh failed: {exc}")


def main():
    # A partial-region retake batch (imaging json_file = the wafer's own
    # "<wafer>_retake.json", written by a region dashboard's Retake selection
    # panel - see qc_overview.py/_write_dashboard_html) images only a handful
    # of "<region>_retake_N" sub-regions, not whole regions. Routing its
    # output into a dedicated "partial_retakes" subfolder (rather than
    # mixed in among ordinary whole-region batches under output_root) keeps
    # scan_wafer_regions()/the global QC report's normal region listing from
    # ever seeing these sub-region names at all - they're surfaced instead by
    # live_qc_support.scan_partial_retakes, which knows to look here.
    # CONFIG["_wafer_output_root"] preserves the ORIGINAL output_root so
    # retake_regions.txt (a few lines down) still resolves to one wafer-wide
    # file, not a copy nested inside partial_retakes/.
    CONFIG["_wafer_output_root"] = CONFIG["output_root"]
    json_file_basename = os.path.splitext(os.path.basename(str(CONFIG.get("json_file") or "")))[0]
    if CONFIG.get("partial_retake_subfolder_enabled", True) and json_file_basename.endswith("_retake"):
        partial_root = os.path.join(CONFIG["output_root"], "partial_retakes")
        os.makedirs(partial_root, exist_ok=True)
        print(f"[Regions] json_file '{CONFIG.get('json_file')}' looks like a partial-retake file "
              f"(basename ends with '_retake') - routing this batch's output to {partial_root} "
              f"instead of {CONFIG['output_root']}")
        CONFIG["output_root"] = partial_root

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

    # Every region defined in the imaging JSON, regardless of process_all_regions/
    # region_name_filter below - this batch's "N of TOTAL wafer regions" indicator
    # and the global (cross-batch) report's definitive region list.
    try:
        canonical_region_names = [r.name for r in load_all_region_infos(json_file)]
    except Exception as exc:
        print(f"[Regions] WARNING: could not read full region list from {json_file}: {exc}")
        canonical_region_names = None

    # Auto-discover the wafer fiducial file if the user hasn't already
    # pointed at one explicitly - see resolve_wafer_fiducial_file(). A miss
    # here just means WAFER_FIDUCIAL_PLANE modes fall back to FIXED, not a
    # hard failure, so this never blocks a run.
    if not CONFIG.get("wafer_fiducial_ol_file"):
        auto_path = resolve_wafer_fiducial_file(json_file)
        if auto_path:
            CONFIG["wafer_fiducial_ol_file"] = auto_path

    # Decide which regions to process (pure file/JSON work — no hardware) FIRST,
    # so the prev_focusmap preflight can fail fast BEFORE we touch the scope.
    process_all = bool(CONFIG.get("process_all_regions", False))
    region_name = CONFIG.get("region_name", None)

    if process_all or region_name is None:
        region_names = compute_region_processing_order(json_file)
    else:
        # Accept a comma-separated list of exact region names, not just one -
        # matches the natural expectation of typing several names into this
        # one field (2026-09-06: an operator tried "S2R3, S3R3, S4R3, S5R3,
        # S6R3" and got a single "region not found" for that whole literal
        # string, since it was passed through as one name unchanged).
        region_names = [n.strip() for n in region_name.split(",") if n.strip()]

    region_filter = CONFIG.get("region_name_filter", None)
    if region_filter:
        before_count = len(region_names)
        region_names = [r for r in region_names if region_filter in r]
        print(f"[RegionFilter] '{region_filter}' matched {len(region_names)}/{before_count} regions")

    # Skip regions already successfully imaged in an earlier batch under the
    # same output_root, so re-running with process_all_regions=True (e.g.
    # after regenerating some prev_focusmap files) doesn't re-image
    # everything - only what's still missing, plus anything explicitly
    # flagged in <output_root>/retake_regions.txt (see
    # live_qc_support.read_retake_list: one region name per line, optional
    # ": reason" - an operator's way to say "this looks done but redo it"
    # without needing to touch the imaging JSON or wait on a GUI feature).
    retake_reasons: Dict[str, str] = {}
    if bool(CONFIG.get("skip_already_imaged", False)):
        try:
            from live_qc_support import compute_regions_to_image
            retake_path = CONFIG.get("retake_regions_file") or os.path.join(
                CONFIG.get("_wafer_output_root", CONFIG["output_root"]), "retake_regions.txt")
            before_count = len(region_names)
            region_names, retake_reasons = compute_regions_to_image(CONFIG["output_root"], region_names, retake_path)
            print(f"[Regions] skip_already_imaged: {len(region_names)}/{before_count} region(s) still need "
                  f"imaging ({len(retake_reasons)} explicitly flagged for retake) - scanned {CONFIG['output_root']} "
                  f"(retake list: {retake_path})")
        except Exception as exc:
            print(f"[Regions] WARNING: skip_already_imaged failed ({exc}); processing the full list instead")

    wafer_total_str = f" (of {len(canonical_region_names)} total wafer region(s))" if canonical_region_names else ""
    print(f"[Regions] Will process {len(region_names)} region(s){wafer_total_str}: {region_names}")

    # Preflight: when processing all regions from a prev_focusmap FOLDER, resolve
    # + validate every region's slice_<N>_FocusMap.txt now so a missing section
    # aborts (or is skipped) BEFORE hours of imaging, not mid-run.
    preflight_skip_reasons: Dict[str, str] = {}
    if (process_all and CONFIG.get("prev_focusmap_dir")
            and str(CONFIG.get("plane_update_method", "ONE_POINT_OFFSET")).upper()
            not in ("SAMPLE_TOPOGRAPHY_CORRECTION", "FIDUCIAL_FOCUS_POINT_XY_REGION_OFFSET")):
        region_names, preflight_skip_reasons = preflight_resolve_prev_focusmaps(json_file, region_names)

    # Write the region manifest NOW - before touching hardware - so the QC
    # report immediately shows the full list of what this run intends to
    # image (especially useful with process_all_regions=True, where an
    # operator otherwise has no idea what's actually in the JSON) and how
    # many remain, updated as each region starts/finishes/fails below.
    #
    # manifest_entries[0:len(region_names)] map 1:1 by index to region_names -
    # the running loop below indexes into this list as manifest_entries[i-1],
    # so entries for regions the preflight dropped are appended AFTER (never
    # touched by that indexing) rather than surfaced as status="skipped" with
    # the reason - otherwise a preflight-skipped region just silently doesn't
    # appear anywhere in the QC report, indistinguishable from "never part of
    # this run's plan", which is exactly what made this need a bug hunt.
    def _initial_detail(rn: str) -> Optional[str]:
        # Carries the retake reason through pending -> running -> done/failed
        # (nothing below overwrites "detail" on status transitions), so the
        # QC report keeps showing WHY this region was re-imaged even after
        # it succeeds - not just while it's still pending.
        if rn in retake_reasons:
            reason = retake_reasons[rn]
            return f"retake requested: {reason}" if reason else "retake requested"
        return None

    manifest_entries = [
        {"idx": i, "name": rn, "folder": _region_folder_name(rn, i), "status": "pending",
         "detail": _initial_detail(rn)}
        for i, rn in enumerate(region_names, start=1)
    ]
    manifest_entries.extend(
        {"idx": len(region_names) + j, "name": rn, "folder": rn, "status": "skipped",
         "detail": f"preflight: {reason}"}
        for j, (rn, reason) in enumerate(preflight_skip_reasons.items(), start=1)
    )
    _write_region_manifest(block_folder, manifest_entries)
    _refresh_block_index(block_folder, canonical_region_names, imaging_json_path=json_file)

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

    if str(CONFIG.get("plane_update_method", "ONE_POINT_OFFSET")).upper() == "SAMPLE_TOPOGRAPHY_CORRECTION":
        precheck_and_correct_wafer_fiducial_planes(conn, block_folder=block_folder)

    _region_timing_log.clear()
    failed_regions: List[Tuple[str, str]] = []
    skipped_regions: List[Tuple[str, str]] = []
    try:
        for i, one_region_name in enumerate(region_names, start=1):
            manifest_entries[i - 1]["status"] = "running"
            _write_region_manifest(block_folder, manifest_entries)
            _refresh_block_index(block_folder, canonical_region_names, imaging_json_path=json_file)

            try:
                run_one_region(conn, json_file, one_region_name, i, block_folder=block_folder)
            except RegionUnsafeStageZError as exc:
                # Not a failure - the region was deliberately refused because
                # the stage Z was unsafe (see _abort_if_stage_z_unsafe), same
                # bucket as a preflight skip: doesn't count against the run,
                # doesn't trigger the end-of-run "N region(s) failed" abort
                # below, and the GUI shows it amber ("skipped"), not red
                # ("FAILED") - the log line's own wording drives that.
                detail = str(exc)[:500]
                print(f"[Region] SKIPPED region_name={one_region_name} - {detail}")
                manifest_entries[i - 1]["status"] = "skipped"
                manifest_entries[i - 1]["detail"] = detail
                skipped_regions.append((one_region_name, detail))
            except StageZHardLimitError:
                # Hard hardware-safety stop (see _safe_goto_z) - a Z this
                # high can damage the microscope. Unlike every other
                # exception here, this must NOT be treated as "this region
                # failed, try the next one" - re-raise so it aborts the
                # whole run immediately instead of risking the same bad Z
                # source getting retried on the next region.
                raise
            except Exception as exc:
                # One region's focus/imaging failure must NOT take the rest of
                # the batch down with it - with process_all_regions=True this
                # used to mean every region after the first failure was never
                # even attempted. The region's own console.log already has the
                # full traceback (see run_one_region); this is just the short
                # summary surfaced in the QC report.
                import traceback as _tb
                _tb.print_exc()
                detail = f"{type(exc).__name__}: {exc}"[:500]
                print(f"[Region] FAILED region_name={one_region_name} - {detail}")
                manifest_entries[i - 1]["status"] = "failed"
                manifest_entries[i - 1]["detail"] = detail
                failed_regions.append((one_region_name, detail))
            else:
                manifest_entries[i - 1]["status"] = "done"

            _write_region_manifest(block_folder, manifest_entries)
            _refresh_block_index(block_folder, canonical_region_names, imaging_json_path=json_file)
    finally:
        # Printed (and written to <block_folder>/timing_summary.txt) even if a
        # region raised partway through, so a failed run still reports timing
        # for whatever regions did complete.
        print_final_timing_summary(block_folder)

        # Same "even if a region raised partway through" reasoning as above -
        # a partial/failed run is exactly when this diagnostic is most
        # useful. Best-effort: generate_focus_diagnostics() never raises, but
        # the import itself could fail (e.g. plotly missing) so this is
        # wrapped too - diagnostics must never take a real run down with it.
        if CONFIG.get("focus_diagnostics_enabled", True):
            try:
                from focus_diagnostics import generate_focus_diagnostics
                generate_focus_diagnostics(
                    block_folder,
                    wafer_fiducial_file=CONFIG.get("wafer_fiducial_ol_file"),
                    diagnostics_dir=CONFIG.get("focus_diagnostics_dir"),
                )
            except Exception as exc:
                print(f"[FocusDiagnostics] WARNING: could not generate diagnostics: {exc}")

    n_done = sum(1 for e in manifest_entries if e["status"] == "done")
    print(f"\n[Regions] {n_done}/{len(region_names)} region(s) completed successfully.")
    if skipped_regions:
        print(f"[Regions] {len(skipped_regions)} region(s) SKIPPED (unsafe stage Z, not imaged):")
        for rn, detail in skipped_regions:
            print(f"  - {rn}: {detail}")
    if failed_regions:
        print(f"[Regions] {len(failed_regions)} region(s) FAILED:")
        for rn, detail in failed_regions:
            print(f"  - {rn}: {detail}")
        raise RuntimeError(
            f"{len(failed_regions)}/{len(region_names)} region(s) failed: "
            + ", ".join(rn for rn, _ in failed_regions)
        )

    print("\nDONE.")

