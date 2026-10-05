"""
HI-MC page — a lightweight launcher for the PROVEN acquisition + Live-QC
pipeline (focus_map.py via main_live_qc.py).

The full Acquisition page is "the whole workflow" (manual stage control + the
big interactive tile map). For day-to-day runs the user used to hand-edit
focus_map.CONFIG, then `python main_live_qc.py`. This page surfaces the knobs
they actually tune as a form, points at the imaging JSON + prev-focusmap folder,
and drives the SAME subprocess via the SAME PipelineRunner — no microscope code
runs in the GUI process, and there is deliberately no heavy map widget here, so
this page stays snappy.

Everything maps through pipeline_runner.build_run_config(); anything not exposed
as a widget can be set in the two "advanced overrides" boxes (key = value, one
per line, value JSON-decoded) which flow through fm_extra / qc_extra verbatim.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

from PySide6.QtCore import Qt, QTimer, QUrl
from PySide6.QtGui import QColor, QDesktopServices
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDoubleSpinBox, QFileDialog, QFormLayout,
    QFrame, QGroupBox, QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem,
    QMessageBox, QPlainTextEdit, QProgressBar, QPushButton, QScrollArea, QSpinBox,
    QVBoxLayout, QWidget,
)

from .base_page import BasePage
from ..widgets import LogPanel


# Mouse-wheel-safe variants of the three value-bearing widget types used
# throughout this page. A bare QSpinBox/QDoubleSpinBox/QComboBox changes its
# value on a wheel scroll even without being clicked into first - so simply
# scrolling the page with the cursor resting over a field (very easy in a
# long form like this one) silently edits a real acquisition parameter with
# no visual cue beyond the number itself changing. Ignoring the wheel event
# here means it falls through to the parent QScrollArea instead (the page
# just scrolls normally); every value can still be changed by typing,
# clicking the up/down arrows, or the keyboard arrow keys - just not by the
# wheel. Used in place of the bare Qt classes EVERYWHERE on this page.
class NoWheelSpinBox(QSpinBox):
    def wheelEvent(self, event):
        event.ignore()


class NoWheelDoubleSpinBox(QDoubleSpinBox):
    def wheelEvent(self, event):
        event.ignore()


class NoWheelComboBox(QComboBox):
    def wheelEvent(self, event):
        event.ignore()


# OL-current-to-focus conversion, so the AF fine/coarse range widgets below
# can be edited in microns (a practical, physically meaningful unit) while
# still sending focus_map.py the uA value AP_AUTOFOCUS_RANGE actually wants.
# Kept in sync by comment only with focus_map.py's _OL_UA_PER_UM (same
# empirical fit - see calibrate_ua_um.py / ua_um_calibration_fit.json); this
# page deliberately never imports focus_map (comtypes/hardware deps).
_OL_UA_PER_UM = 119.262

# Scan-rate -> dwell time per pixel, per the operator (2026-09-04). Only
# rates 1-5 are documented this way; higher rate values exist (the spinbox
# allows up to 30) but their ns/pixel isn't confirmed, so they're
# deliberately left unlabeled rather than guessed by extrapolating the x2
# pattern below.
_SCAN_RATE_NS_PER_PIXEL = {1: 50, 2: 100, 3: 200, 4: 400, 5: 800}


# Display name shown in the "Plane update method" dropdown -> the actual
# focus_map.py CONFIG string it maps to. Kept internally unchanged (focus_map.py's
# dispatch/print-logging/saved configs still use the original names) - this is
# purely a GUI-friendliness relabel, same pattern as pipeline_runner.py's
# _TRAVEL_MAP_REV/_DIRECTION_MAP_REV/_START_MAP_REV. THREE_POINT_DELTA,
# CURRENT_ALL_POINTS_QUADRATIC_RESIDUAL_IDW, and MANUAL_POINTS are hidden from
# the dropdown until further notice (still fully supported by focus_map.py -
# selectable via the "Acquisition pipeline settings" advanced override box).
_PLANE_METHOD_DISPLAY_TO_INTERNAL = {
    "ALL_POINTS_PLANE": "CURRENT_ALL_POINTS_PLANE",
    "ONE_REGION_FSP": "ONE_POINT_OFFSET",
    "WAFER_FIDUCIAL_DIRECT": "WAFER_FIDUCIAL_DIRECT",
    # Both EXPERIMENTAL - zero AF/AS/measurement, everything predicted from a
    # fitted plane. See focus_map.py's plane_update_method dispatch for the
    # full behavior/caveats (brand new CONFIG strings, no legacy name to map).
    "SAMPLE_TOPOGRAPHY_CORRECTION": "SAMPLE_TOPOGRAPHY_CORRECTION",
    "FOCUSMAP_TOPOGRAPHY": "FOCUSMAP_TOPOGRAPHY",
    # EXPERIMENTAL, 2026-09-03 - per-region variant of SAMPLE_TOPOGRAPHY_CORRECTION:
    # one fresh B2F -> AF -> AS measurement per region (instead of measuring
    # every wafer-fiducial point once per wafer) re-levels the wafer-fiducial
    # OL/StigX/StigY planes' intercept for that region only. Always runs its
    # own B2F regardless of b2f_mode (see focus_map.py dispatch).
    "FIDUCIAL_FOCUS_POINT_XY_REGION_OFFSET": "FIDUCIAL_FOCUS_POINT_XY_REGION_OFFSET",
}
_PLANE_METHOD_INTERNAL_TO_DISPLAY = {v: k for k, v in _PLANE_METHOD_DISPLAY_TO_INTERNAL.items()}

# Display names of strategies that use the wafer-wide fiducial file for their
# OWN plane (not just as a fallback via Initial OL value = WAFER_FIDUCIAL_PLANE,
# which is checked separately) - drives the "Wafer-wide fiducial source"
# section's visibility in _update_focus_source_visibility. ALL_POINTS_PLANE is
# deliberately excluded: it fits its own fresh plane from live measurement and
# only ever touches the wafer-fiducial file via Initial OL value.
_STRATEGIES_USING_WAFER_FIDUCIAL = {
    "WAFER_FIDUCIAL_DIRECT", "SAMPLE_TOPOGRAPHY_CORRECTION",
    "FOCUSMAP_TOPOGRAPHY", "ONE_REGION_FSP", "FIDUCIAL_FOCUS_POINT_XY_REGION_OFFSET",
}

# SAMPLE_TOPOGRAPHY_CORRECTION and FOCUSMAP_TOPOGRAPHY run Beam-to-Fiber once
# at region start, before their own per-mFOV plane predictions ever get
# evaluated - so "Initial OL value" (the coarse focus set before that B2F
# call) isn't an independent choice for them, it MUST match whichever plane
# source that strategy itself predicts from, or the initial focus used for
# B2F is inconsistent with the plane the whole region then runs on.
# WAFER_FIDUCIAL_PLANE for SAMPLE_TOPOGRAPHY_CORRECTION (its OL plane comes
# from the wafer-fiducial file); PREV_PLANE_AT_FIRST_SP for FOCUSMAP_TOPOGRAPHY
# (its OL plane comes from this region's own FocusMap.txt instead).
_TOPOGRAPHY_REQUIRED_INIT_OL = {
    "SAMPLE_TOPOGRAPHY_CORRECTION": "WAFER_FIDUCIAL_PLANE",
    "FOCUSMAP_TOPOGRAPHY": "PREV_PLANE_AT_FIRST_SP",
    "FIDUCIAL_FOCUS_POINT_XY_REGION_OFFSET": "WAFER_FIDUCIAL_PLANE",
}

# Extra focus_map CONFIG knobs surfaced as proper widgets (beyond the main
# scan/route/plane/QC groups). Data-driven so adding a param is one line; values
# flow through fm_extra verbatim. Defaults MUST match focus_map.CONFIG.
#   spec = (config_key, label, kind, default, opts)
#   kind: "i" int spinbox, "f" float spinbox, "b" checkbox
#   opts: may include "min"/"max"/"dec"/"suffix" and "tip" (hover tooltip)
_EXTRA_PARAM_GROUPS = [
    ("Z fiducial startup check", [
        # Coarse, one-time-per-region sanity check run BEFORE any focusing -
        # see check_and_correct_z_from_fiducial() in focus_map.py. Compares
        # the stage's CURRENT Z to the wafer fiducial plane's Z estimate at
        # the region's first support point; if the stage is sitting more than
        # the threshold below that estimate (e.g. left at Z=0 from a fresh
        # connection/homing), moves up to the fiducial Z first. OL/AF/AS can
        # only correct small (um-scale) defocus, not the stage being at the
        # wrong ABSOLUTE height (mm-scale) - this catches that before it
        # cascades into AF/B2F failures across a whole region.
        ("z_fiducial_check_enabled", "Enable Z fiducial startup check", "b", True,
         {"tip": "One-time-per-region coarse safety check, run before any focusing: if the "
                 "stage's current physical Z is far below the wafer fiducial plane's "
                 "estimate (e.g. left at Z=0 after a fresh connection/homing), move up "
                 "first. AF/AS/OL only ever correct micron-scale defocus, never a "
                 "millimeter-scale wrong absolute height - leave this ON."}),
        ("z_fiducial_check_threshold_um", "Shortfall threshold before correcting", "f", 800.0,
         {"max": 10000.0, "dec": 1, "suffix": " um",
          "tip": "How far below the fiducial plane's Z estimate the stage must be before "
                 "this check corrects it. Only ever corrects an under-shoot (too low), "
                 "never an over-shoot."}),
        ("wafer_fiducial_z_align_enabled", "Align Z to fiducial height before each OL seed", "b", True,
         {"tip": "Only applies to CURRENT_ALL_POINTS_PLANE / CURRENT_ALL_POINTS_"
                 "QUADRATIC_RESIDUAL_IDW / THREE_POINT_DELTA (not ONE_POINT_OFFSET, which "
                 "has its own seeding and never checks this). Moves to the fiducial "
                 "plane's own calibration Z before seeding each support point's OL from "
                 "that same plane, so the seed is evaluated at the height it was actually "
                 "measured at."}),
    ]),
    ("Z-search (dz_too_large fix)", [
        # One-time per-region physical stage Z correction from a fresh AF
        # measurement - see run_z_search_correction() in focus_map.py. Fixes
        # dz_too_large rejections caused by a genuinely-off stage Z, which no
        # amount of better OL seeding can fix on its own.
        ("z_search_enabled", "Enable Z-search correction", "b", False,
         {"tip": "One-time-per-region physical stage Z correction from a fresh AF "
                 "measurement - fixes dz_too_large rejections caused by a genuinely-off "
                 "stage Z that no amount of better OL seeding can recover from on its own."}),
        ("z_search_max_correction_um", "Max correction (safety clip)", "f", 200.0,
         {"max": 5000.0, "dec": 1, "suffix": " um",
          "tip": "Safety clip: never move the stage more than this much in Z during "
                 "correction, however large the measured error looks."}),
        ("z_search_min_correction_um", "Min correction worth applying", "f", 2.0,
         {"max": 1000.0, "dec": 1, "suffix": " um",
          "tip": "Skip the correction entirely if the measured error is smaller than "
                 "this - not worth a stage move for a sub-threshold offset."}),
        ("z_search_dz_sign", "dz sign (flip to -1/+1 if verification AF shows dz got worse)", "f", -1.0,
         {"min": -1.0, "max": 1.0, "dec": 0,
          "tip": "Sign convention between measured dz and the stage-Z move direction. If "
                 "a verification AF after correction shows dz got WORSE instead of "
                 "better, this is backwards for your setup - flip it."}),
    ]),
    ("Autofocus", [
        # af_enabled, minimize_ol_enabled/minimize_ol_max_iters/dz_limit_um
        # hidden from the GUI 2026-09-04 (per operator request - "cleaning up
        # the code"). af_enabled is genuinely dead: grepping focus_map.py
        # confirms nothing ever reads CONFIG["af_enabled"] - every
        # plane_update_method calls run_af_fine_then_coarse()/run_af_once()
        # directly, none of them gate on this flag, so toggling it in the
        # GUI never had any effect. minimize_ol_enabled (and its two
        # dependent knobs) IS still consulted by the older offset-based
        # methods (ONE_POINT_OFFSET/THREE_POINT_DELTA/CURRENT_ALL_POINTS_PLANE)
        # - but this operator's actual workflow (a fixed wafer imaged
        # repeatedly, only small Z/focus/stig drift run to run) uses the
        # topography family of methods, none of which ever call
        # minimize_ol_lens_zen_style at all. All four keys are still real
        # focus_map.CONFIG entries (defaults unchanged) - only their
        # dedicated widgets are gone; still reachable via the advanced
        # overrides box if a future workflow needs them back.
        ("af_fine_sampling_points", "AF fine sampling points", "i", 3,
         {"max": 500, "tip": "Number of image samples across the FAST/fine autofocus search range. Fewer = quicker but noisier."}),
        ("af_fine_range_um", "AF fine range", "f", 20.0,
         {"max": 1000.0, "dec": 1, "suffix": " um",
          "tip": "Search range for the fast/fine autofocus pass, in microns - the main lens "
                 "current is varied from Iol-range to Iol+range (Zeiss API doc 4.3.3), so this "
                 "is a +/- half-width, not a full span. Converted to uA (via _OL_UA_PER_UM) "
                 "before being sent as AP_AUTOFOCUS_RANGE - narrow by design, so it converges "
                 "quickly when the seed is already close, but fails and falls back to the "
                 "slower coarse search when it isn't."}),
        ("af_fine_scan_rate", "AF fine scan rate", "i", 4,
         {"max": 30, "tip": "Beam scan rate used for fine-AF sampling images. Dwell time per "
                            "pixel roughly doubles per rate step (documented for rates 1-5: "
                            "50/100/200/400/800 ns)."}),
        ("af_fine_pixel_size_nm", "AF fine pixel size", "f", 10.0,
         {"max": 1000.0, "dec": 1, "suffix": " nm", "tip": "Pixel size used for fine-AF sampling images."}),
        ("af_coarse_sampling_points", "AF coarse sampling points", "i", 20,
         {"max": 500, "tip": "Number of image samples across the SLOW/coarse autofocus search range (used only when fine AF fails)."}),
        ("af_coarse_range_um", "AF coarse range", "f", 50.0,
         {"max": 1000.0, "dec": 1, "suffix": " um",
          "tip": "Much wider (+/-) search range than fine AF, in microns - the fallback when "
                 "fine AF's narrow range can't find focus at all. Converted to uA (via "
                 "_OL_UA_PER_UM) before being sent as AP_AUTOFOCUS_RANGE, same as AF fine "
                 "range above."}),
        ("af_coarse_scan_rate", "AF coarse scan rate", "i", 4,
         {"max": 30, "tip": "Beam scan rate used for coarse-AF sampling images. Dwell time per "
                            "pixel roughly doubles per rate step (documented for rates 1-5: "
                            "50/100/200/400/800 ns)."}),
        ("af_coarse_pixel_size_nm", "AF coarse pixel size", "f", 10.0,
         {"max": 1000.0, "dec": 1, "suffix": " nm", "tip": "Pixel size used for coarse-AF sampling images."}),
    ]),
    ("Autostig", [
        ("as_enabled", "AS enabled", "b", True, {"tip": "Master switch for autostigmation. OFF skips AS entirely (stig stays whatever it was seeded to)."}),
        ("as_sampling_points", "AS sampling points", "i", 7, {"max": 500, "tip": "Number of image samples across the autostig search range."}),
        ("as_range", "AS range", "f", 2.5, {"max": 1000.0, "dec": 2, "tip": "Search range for autostigmation."}),
        ("as_scan_rate", "AS scan rate", "i", 4,
         {"max": 30, "tip": "Beam scan rate used for autostig sampling images. Dwell time per "
                            "pixel roughly doubles per rate step (documented for rates 1-5: "
                            "50/100/200/400/800 ns)."}),
        ("as_pixel_size_nm", "AS pixel size", "f", 10.0, {"max": 1000.0, "dec": 1, "suffix": " nm", "tip": "Pixel size used for autostig sampling images."}),
    ]),
    ("Stage / timing", [
        ("stage_settling_time_ms", "Stage settling time", "i", 200,
         {"max": 600000, "suffix": " ms",
          "tip": "Pause after every stage move before the next AF/AS/scan, letting "
                 "mechanical vibration/settling die out. Too low risks blurry/defocused "
                 "images from a still-moving stage."}),
        ("wait_stage_settled", "Wait for stage settled", "b", True,
         {"tip": "Poll the stage's own \"settled\" status flag after a move instead of "
                 "just sleeping a fixed time - more reliable, but only if the hardware "
                 "reports that flag correctly."}),
        ("move_z_to_json_supportpoint", "Move Z to JSON support point", "b", False,
         {"tip": "Move to the Z value stored in the imaging JSON's support-point "
                 "definition before focusing there, instead of leaving Z wherever the "
                 "stage currently sits. Usually OFF - the fiducial/prev-focusmap seeding "
                 "already handles Z."}),
        ("z_backlash_enabled", "Z backlash compensation", "b", False,
         {"tip": "Before every commanded Z move, overshoot past the target opposite the "
                 "preferred approach direction, settle, then approach the real target from "
                 "that direction - so the final bit of travel always takes up mechanical "
                 "slack the same way. OFF by default: z_backlash_um below is an UNVERIFIED "
                 "placeholder, not a measured value - only enable after characterizing the "
                 "real backlash on this stage."}),
        ("z_backlash_um", "Z backlash amount", "f", 5.0,
         {"max": 500.0, "dec": 2, "suffix": " um",
          "tip": "Overshoot distance for backlash compensation above - UNVERIFIED "
                 "placeholder, not a measured value. Only used if Z backlash compensation "
                 "is enabled."}),
        ("z_backlash_approach_from_below", "Z backlash: approach from below", "b", True,
         {"tip": "The final leg of every Z move is upward (+Z) into the target when ON, "
                 "downward (-Z) when OFF. Only used if Z backlash compensation is enabled."}),
        ("defer_finish_write", "Defer finish-write (pipeline)", "b", True,
         {"tip": "Overlap a tile's final disk write with the next tile's stage move/scan "
                 "instead of blocking on it - faster throughput, at the cost of a tile's "
                 "write finishing slightly later than its own acquisition."}),
        ("force_finish_every_n", "Force finish every N tiles", "i", 5,
         {"max": 100000,
          "tip": "With deferred finish-writes, force a full flush every N tiles anyway - "
                 "bounds how much unwritten data could be lost if the run is interrupted."}),
        ("sanity_every_n_tiles", "System-sanity every N tiles", "i", 0,
         {"max": 100000,
          "tip": "Run a hardware sanity check every N tiles (0 = never). Catches a "
                 "silently-degraded connection/scope state mid-run, at the cost of a "
                 "small pause each time it runs."}),
    ]),
    ("Thumbnails", [
        # On-scope thumbnail (DP_THUMBNAIL_*), generated alongside every full-res
        # tile at essentially no extra acquisition time. thumbnail_scale must be a
        # power of 2 in [2, 64]; default 4 for a detailed thumbnail - raise it
        # (e.g. back to 64) if you want section_overview_builder.py's coarser
        # ~0.5um/px wafer overview instead for a given run.
        ("save_thumbnails", "Save on-scope thumbnails", "b", True,
         {"tip": "Generate an on-scope thumbnail alongside every full-res tile, at "
                 "essentially no extra acquisition time."}),
        ("thumbnail_scale", "Thumbnail downsample factor", "i", 4,
         {"min": 2, "max": 64,
          "tip": "Must be a power of 2 (2-64). Lower = more detailed thumbnail; higher "
                 "(e.g. 64) matches section_overview_builder.py's coarser ~0.5um/px wafer "
                 "overview instead."}),
    ]),
]

# Rendered right after _build_plane_group() (not looped in with the other
# _EXTRA_PARAM_GROUPS further down the page) so these sit next to the
# "Plane update method" dropdown they directly qualify - previously buried
# several groups further down the page under a title that didn't obviously
# connect to "Focus Method", which is exactly where someone using
# ONE_POINT_OFFSET would look first.
_PLANE_ROBUSTNESS_SPECS = [
    ("plane_update_min_points", "Plane-update min points", "i", 3,
     {"max": 100, "tip": "Minimum support points required to fit a region's own plane "
                         "(CURRENT_ALL_POINTS_PLANE/_QUADRATIC_RESIDUAL_IDW/THREE_POINT_DELTA). "
                         "Not consulted by ONE_POINT_OFFSET."}),
    ("plane_update_strict", "Plane-update strict (abort on any fail)", "b", False,
     {"tip": "If ANY support-point measurement fails during plane update, abort the whole "
             "region instead of continuing with whatever succeeded."}),
    ("one_point_offset_top_k", "1-pt offset: top-K candidates", "i", 5,
     {"max": 100, "tip": "ONE_POINT_OFFSET only: remeasure up to this many of the best-ranked "
                        "prev-focusmap points (by AF/AS confidence) before computing the "
                        "robust median offset."}),
    ("one_point_offset_mad_sigma", "1-pt offset: MAD sigma", "f", 2.5,
     {"max": 10.0, "dec": 2, "tip": "ONE_POINT_OFFSET only: MAD-based outlier rejection "
                                   "threshold (in sigma-equivalents) applied across the "
                                   "remeasured candidates' offsets - only active with >=3 "
                                   "candidates."}),
    # Both default to 2 (matching focus_map.CONFIG) for the normal
    # multi-point workflow's robustness check. A prev_focusmap with only
    # ONE point per region (see the wafer-fiducial-anchored fallback in
    # load_prev_focusmap_fit_plane_and_candidates) only ever produces 1
    # candidate here too - set both to 1 for that workflow, or
    # ONE_POINT_OFFSET raises "not enough valid candidates" even though
    # the single point measured fine.
    ("one_point_offset_min_valid", "1-pt offset: min valid candidates", "i", 2,
     {"max": 100, "tip": "ONE_POINT_OFFSET only: minimum successfully-remeasured candidates "
                        "required, or the region fails with \"not enough valid candidates\". "
                        "A prev-focusmap with only ONE point per region can only ever "
                        "produce 1 candidate - set this to 1 for that workflow (default 2 "
                        "assumes the normal multi-point case)."}),
    ("one_point_offset_min_inliers", "1-pt offset: min inliers after MAD filter", "i", 2,
     {"max": 100, "tip": "ONE_POINT_OFFSET only: minimum candidates that must survive MAD "
                        "outlier rejection. Same 1-point caveat as min valid candidates "
                        "above - set to 1 for a true single-point-per-region prev-focusmap."}),
    ("topography_precheck_enabled", "Topography precheck: enabled", "b", True,
     {"tip": "SAMPLE_TOPOGRAPHY_CORRECTION only: before the first region, freshly remeasure "
             "wafer-fiducial points (AF/AS/B2F) and fit brand-new OL/StigX/StigY planes "
             "directly from those measurements, replacing the fiducial file's own OL/StigX/"
             "StigY planes for this run - corrects for both a height shift AND a tilt change "
             "since the fiducial file was built (e.g. from re-loading the wafer), not just a "
             "level shift. ProjX/ProjY are measured too (for comparison) but not corrected - "
             "they stay exactly as fit from the fiducial file. Runs once per wafer, not per "
             "region."}),
    ("topography_precheck_n_points", "Topography precheck: # points", "i", 9999,
     {"min": 3, "max": 99999, "tip": "SAMPLE_TOPOGRAPHY_CORRECTION only: how many wafer-fiducial "
                                 "points to freshly remeasure for the plane refit above. Clamped "
                                 "to however many complete rows the fiducial file actually has - "
                                 "the default (a large sentinel) means 'every point in the file', "
                                 "so the fresh OL plane has the same statistical basis (N points) "
                                 "as the original fiducial fit."}),
    ("topography_precheck_tissue_xy_offset_um", "Topography precheck: tissue xy offset", "f", 110.0,
     {"max": 1000.0, "dec": 1, "suffix": " um",
      "tip": "Offset each precheck measurement point, in the XY (stage) plane, by this "
             "distance in a random direction picked fresh per point per run, so repeatedly "
             "re-measuring the same fiducial point (across many runs) doesn't keep "
             "bleaching/damaging the same tissue spot. "
             "110 um = 1 MFOV diameter. Set to 0 to measure exactly at the fiducial points."}),
]

# Every internal plane_update_method string (see _PLANE_METHOD_DISPLAY_TO_INTERNAL).
_ALL_PLANE_METHODS = {
    "ONE_POINT_OFFSET", "THREE_POINT_DELTA", "CURRENT_ALL_POINTS_PLANE",
    "CURRENT_ALL_POINTS_QUADRATIC_RESIDUAL_IDW", "MANUAL_POINTS",
    "WAFER_FIDUCIAL_DIRECT", "SAMPLE_TOPOGRAPHY_CORRECTION",
    "FIDUCIAL_FOCUS_POINT_XY_REGION_OFFSET", "FOCUSMAP_TOPOGRAPHY",
}

# Which plane_update_method(s) each _PLANE_ROBUSTNESS_SPECS knob actually
# affects - traced by reading the real CONFIG-key consumers in focus_map.py,
# not guessed. Drives _update_plane_robustness_visibility so the section
# only ever shows knobs live for whatever strategy is currently selected.
_PLANE_ROBUSTNESS_RELEVANCE: Dict[str, set] = {
    # Consulted by measure_plane_update_points (THREE_POINT_DELTA /
    # CURRENT_ALL_POINTS_PLANE / CURRENT_ALL_POINTS_QUADRATIC_RESIDUAL_IDW's
    # own measurement loop) AND by preflight_resolve_prev_focusmaps, which
    # runs for every method that still needs a prev_focusmap file - i.e.
    # every method except the two that skip that preflight entirely
    # (SAMPLE_TOPOGRAPHY_CORRECTION, FIDUCIAL_FOCUS_POINT_XY_REGION_OFFSET -
    # see focus_map.py's _plane_method_for_prev_focusmap).
    "plane_update_min_points": _ALL_PLANE_METHODS - {
        "SAMPLE_TOPOGRAPHY_CORRECTION", "FIDUCIAL_FOCUS_POINT_XY_REGION_OFFSET"},
    # Consulted by measure_plane_update_points (same three as above) AND by
    # measure_manual_focus_points (MANUAL_POINTS).
    "plane_update_strict": {
        "THREE_POINT_DELTA", "CURRENT_ALL_POINTS_PLANE",
        "CURRENT_ALL_POINTS_QUADRATIC_RESIDUAL_IDW", "MANUAL_POINTS"},
    "one_point_offset_top_k": {"ONE_POINT_OFFSET"},
    "one_point_offset_mad_sigma": {"ONE_POINT_OFFSET"},
    "one_point_offset_min_valid": {"ONE_POINT_OFFSET"},
    "one_point_offset_min_inliers": {"ONE_POINT_OFFSET"},
    "topography_precheck_enabled": {"SAMPLE_TOPOGRAPHY_CORRECTION"},
    "topography_precheck_n_points": {"SAMPLE_TOPOGRAPHY_CORRECTION"},
    "topography_precheck_tissue_xy_offset_um": {
        "SAMPLE_TOPOGRAPHY_CORRECTION", "FIDUCIAL_FOCUS_POINT_XY_REGION_OFFSET"},
}


class HiMcPage(BasePage):
    """Form-driven launcher for the acquisition + Live-QC pipeline."""

    # -- construction -------------------------------------------------------
    def setup_ui(self):
        # Tooltips inherit the app's dark palette by default, which leaves
        # them low-contrast (light-grey-on-dark-grey) - this page's tooltips
        # carry a lot of real information (units, gotchas, which strategy a
        # knob applies to), so force plain black-on-white instead. Scoped to
        # this page (set on self, a QWidget) rather than the whole app.
        self.setStyleSheet(
            "QToolTip { color: #000000; background-color: #ffffff; "
            "border: 1px solid #888888; padding: 4px; }")

        # runtime state (BasePage calls setup_ui from __init__, so init here)
        self._pipeline_runner = None
        self._last_output_dir = None   # block folder / QC results dir for "Open"
        self._extra_widgets = {}       # config_key -> (widget, kind) for _EXTRA_PARAM_GROUPS
        self._extra_group_forms = {}   # group title -> its QFormLayout, for per-row show/hide
        self._region_rows = {}         # region_name -> QListWidgetItem, for the progress list
        self._run_failed_regions = []  # region names failed so far THIS run, for the multi-failure alert
        self._user_requested_stop = False  # set by _on_stop, so a graceful Stop never counts as "unexpected"
        self._alert_sent_this_run = False  # at most one failure alert per run, whichever condition trips first
        self._z_alert_active = False   # currently below the low-Z threshold (log the transition once, not every reading)

        title = QLabel("HI-MC — Acquisition + Live QC")
        title.setStyleSheet("font-size: 18px; font-weight: 600; padding: 0 0 6px 0;")
        self.layout.addWidget(title)

        hint = QLabel(
            "Load a Zeiss imaging JSON, point at the prev-focusmap folder, tune "
            "the parameters, then Start. Runs the same main_live_qc.py pipeline "
            "you run from the terminal.")
        hint.setWordWrap(True)
        hint.setStyleSheet("color: #9aa0a6; padding-bottom: 8px;")
        self.layout.addWidget(hint)

        # Informational only - Start's behavior does NOT depend on this
        # checkbox (main_live_qc.py always drives real hardware when
        # launched), same visual treatment as the SFN page's banner.
        self.lbl_sim_banner = QLabel()
        self.lbl_sim_banner.setWordWrap(True)
        self.lbl_sim_banner.setStyleSheet(
            "color: #ffd54a; background-color: rgba(204,167,0,0.18); font-weight: 600; "
            "padding: 6px 10px; border: 1px solid #cca700; border-radius: 4px;")
        self.lbl_sim_banner.setVisible(False)
        self.layout.addWidget(self.lbl_sim_banner)
        self._sim_banner_timer = QTimer(self)
        self._sim_banner_timer.timeout.connect(self._refresh_sim_banner)
        self._sim_banner_timer.start(1000)
        self._refresh_sim_banner()

        # --- scrollable parameter form ------------------------------------
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        form_host = QWidget()
        form_col = QVBoxLayout(form_host)
        form_col.setContentsMargins(0, 0, 0, 0)
        form_col.setSpacing(10)

        form_col.addWidget(self._build_io_group())
        form_col.addWidget(self._build_region_group())
        # Focusing strategy -> Autofocus -> Autostig -> Plane-update
        # robustness sit right after Region selection (which regions -> how
        # their focus/stig gets determined), ahead of Scan/Route (how each
        # tile within a region gets imaged) - per the operator (2026-09-04),
        # this groups every focus-related decision in the order it's
        # actually made: pick the strategy, tune AF, tune AS, then the
        # robustness knobs some strategies use. Autofocus/Autostig are
        # pulled out of the _EXTRA_PARAM_GROUPS loop below (they're excluded
        # there via the `if _title in (...)` skip) so they render here
        # instead of after Scan/Route with the rest of that list.
        _extra_by_title = dict(_EXTRA_PARAM_GROUPS)
        form_col.addWidget(self._build_plane_group())
        form_col.addWidget(self._build_extra_group("Autofocus", _extra_by_title["Autofocus"]))
        form_col.addWidget(self._build_extra_group("Autostig", _extra_by_title["Autostig"]))
        form_col.addWidget(self._build_extra_group("Plane-update robustness", _PLANE_ROBUSTNESS_SPECS))
        self._update_plane_robustness_visibility()
        form_col.addWidget(self._build_scan_group())
        form_col.addWidget(self._build_route_group())
        for _title, _specs in _EXTRA_PARAM_GROUPS:
            if _title in ("Autofocus", "Autostig"):
                continue  # already rendered above, right after Focusing strategy
            form_col.addWidget(self._build_extra_group(_title, _specs))
        form_col.addWidget(self._build_qc_group())
        form_col.addWidget(self._build_advanced_group())
        form_col.addStretch(1)

        scroll.setWidget(form_host)
        self.layout.addWidget(scroll, stretch=3)

        # --- action row ---------------------------------------------------
        self.layout.addLayout(self._build_action_row())

        # --- stage Z safety alert --------------------------------------------
        self.layout.addWidget(self._build_stage_z_group())

        # (failure-notification alerting has no GUI panel - see _NOTIFY_FAIL_THRESHOLD
        # / _NOTIFY_ON_UNEXPECTED_EXIT above; it just runs in the background)

        # --- region imaging order + progress -------------------------------
        self.layout.addWidget(self._build_region_progress_group())

        # --- log ----------------------------------------------------------
        self.log_panel = LogPanel()
        self.layout.addWidget(self.log_panel, stretch=2)

        # --- restore last run's settings, if any -----------------------------
        # Recovery, not convenience: a computer or microscope crash mid-run
        # should let the operator relaunch this page and hit Start again with
        # as little re-entry as possible. msem_run_config.json already holds
        # exactly the config the crashed run was launched with (PipelineRunner
        # writes it fresh at Start, before spawning main_live_qc.py) - nothing
        # separate to maintain, and it can never drift from what a real run
        # actually used, unlike hand-restoring an in-memory snapshot would.
        self._restore_last_config()
        self._ensure_notify_emails_file()

    # -- simulation-mode banner (informational only) -------------------------
    # Unlike the SFN page, HI-MC's Start does NOT check this flag - clicking
    # Start always launches main_live_qc.py against real hardware regardless
    # of the checkbox. This banner exists purely so that's never a silent
    # surprise while the checkbox happens to be left on.
    def _in_simulation(self) -> bool:
        return bool(self.engine is not None and getattr(self.engine, "simulation_mode", False))

    def _refresh_sim_banner(self):
        self.lbl_sim_banner.setVisible(self._in_simulation())
        if self._in_simulation():
            self.lbl_sim_banner.setText(
                "⚠ SIMULATION MODE — this has no effect on HI-MC: Start always drives the "
                "real microscope. Uncheck Simulation Mode in System Status once you no "
                "longer intend that.")

    # -- group builders -----------------------------------------------------
    def _group(self, title: str) -> tuple[QGroupBox, QFormLayout]:
        box = QGroupBox(title)
        form = QFormLayout(box)
        form.setLabelAlignment(Qt.AlignRight)
        form.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)
        return box, form

    def _sub_group(self, title: str) -> tuple[QGroupBox, QVBoxLayout]:
        """A visibly-bordered nested group (vs. a plain QLabel + QWidget) for
        a distinct data-source sub-section inside a bigger group - added
        after operator feedback that stacked labels with no frame read as
        one continuous section instead of separate ones."""
        box = QGroupBox(title)
        v = QVBoxLayout(box)
        return box, v

    def _file_row(self, edit: QLineEdit, handler) -> QWidget:
        row = QWidget()
        h = QHBoxLayout(row)
        h.setContentsMargins(0, 0, 0, 0)
        h.addWidget(edit, stretch=1)
        btn = QPushButton("Browse…")
        btn.clicked.connect(handler)
        h.addWidget(btn)
        return row

    def _build_io_group(self) -> QGroupBox:
        box, form = self._group("Define Imaging Regions and Image Storage")
        self.edit_json = QLineEdit()
        self.edit_json.setPlaceholderText("Zeiss imaging JSON (regions + support points)")
        self.edit_json.setToolTip(
            "The Zeiss imaging JSON defining every region and its focus support points - "
            "everything downstream (region list, tile plan, prev-focusmap matching) comes "
            "from this one file.")
        form.addRow("Imaging JSON:", self._file_row(self.edit_json, self._browse_json))

        self.edit_output = QLineEdit()
        self.edit_output.setPlaceholderText(r"output root, e.g. V:\HI-MC\...\API")
        self.edit_output.setToolTip(
            "Root folder this run's batch folder gets created under - also where the "
            "global QC report and retake_regions.txt live, shared across every batch run "
            "under it.")
        form.addRow("Image storage location:", self._file_row(self.edit_output, self._browse_output))
        return box

    def _build_focus_map_collect_group(self) -> QWidget:
        """Runs collect_focus_maps.py's logic (see
        msem_control/engine/focus_map_collector.py): pulls every region's
        FocusMap.txt out of a completed acquisition run's per-region
        subfolders (e.g. "016_S18R1") into one flat "S<N>_FocusMap.txt"
        folder. Writes directly into the "Raw focus-map dir" field above
        (edit_pfm) rather than a separate destination field - one field for
        both "where collected files land" and "what the pipeline reads"."""
        box, v = self._sub_group("Collect FocusMap.txt files from a completed acquisition run")

        self.edit_focus_map_source = QLineEdit()
        self.edit_focus_map_source.setPlaceholderText(
            "Completed run's output folder (contains e.g. 016_S18R1/FocusMap.txt)")
        source_row = self._file_row(self.edit_focus_map_source, self._browse_focus_map_source)
        v.addLayout(self._labeled_row("Source run folder:", source_row))

        self.btn_collect_focus_maps = QPushButton("Collect Focus Maps → Raw focus-map dir")
        self.btn_collect_focus_maps.setToolTip(
            "Writes into 'Raw focus-map dir' above - if that field already has a path, "
            "collected files land there; if it's blank, defaults to <source folder>_collected "
            "next to the source folder and fills the field in.")
        self.btn_collect_focus_maps.clicked.connect(self._on_collect_focus_maps)
        btn_row = QHBoxLayout()
        btn_row.addWidget(self.btn_collect_focus_maps)
        btn_row.addStretch(1)
        v.addLayout(btn_row)
        return box

    def _labeled_row(self, label: str, row_widget: QWidget) -> QHBoxLayout:
        h = QHBoxLayout()
        lbl = QLabel(label)
        lbl.setMinimumWidth(110)
        h.addWidget(lbl)
        h.addWidget(row_widget, stretch=1)
        return h

    def _build_region_group(self) -> QGroupBox:
        box, form = self._group("Region selection")
        self.chk_process_all = QCheckBox("Process ALL regions in the JSON")
        self.chk_process_all.setToolTip(
            "ON: image every region in the imaging JSON (optionally narrowed by Region "
            "filter below). OFF: image only the single region named in Region name.")
        form.addRow(self.chk_process_all)

        # When processing all regions from a raw focus-map folder, a preflight
        # checks every region's slice_<N>_FocusMap.txt up front. Default = abort
        # the run if any is missing; tick this to warn+skip and image the rest.
        self.chk_preflight_skip = QCheckBox("On missing focus-map: skip that region (else abort run)")
        self.chk_preflight_skip.setToolTip(
            "Before any hardware work starts, every region's prev-focusmap file is "
            "checked up front. OFF (default): abort the whole run if ANY region is "
            "missing one. ON: warn, skip just that region, and image the rest.")
        form.addRow(self.chk_preflight_skip)

        # Both fields stay editable regardless of the checkbox; focus_map uses
        # region_name when 'process all' is OFF, region_name_filter when it's ON.
        self.edit_region_name = QLineEdit()
        self.edit_region_name.setPlaceholderText(
            "e.g. S25R1, or S2R3, S3R3, S4R3 (used when 'process all' is OFF)")
        self.edit_region_name.setToolTip(
            "Exact region name(s) to image, when 'Process ALL regions' is unchecked. "
            "Comma-separate multiple names (e.g. \"S2R3, S3R3, S4R3\") to image just that subset.")
        form.addRow("Region name:", self.edit_region_name)

        self.edit_region_filter = QLineEdit()
        self.edit_region_filter.setPlaceholderText("e.g. R1 (used when 'process all' is ON; empty = every region)")
        self.edit_region_filter.setToolTip(
            "Only used when 'Process ALL regions' is checked - narrows the JSON's region "
            "list to names containing this substring (e.g. \"R1\" for every region's first "
            "repeat). Leave empty to process every region in the JSON.")
        form.addRow("Region filter:", self.edit_region_filter)

        # Skip regions already successfully imaged in an earlier batch under
        # the same output root - lets a re-run with new prev_focusmap files
        # (or "process ALL") only image what's actually missing, plus
        # anything listed in a retake file (one region name per line,
        # optional ": reason" - see the Global QC Report's "Retake" status).
        self.chk_skip_already_imaged = QCheckBox("Skip regions already imaged (scan prior batches)")
        self.chk_skip_already_imaged.setToolTip(
            "Before this run starts, scan every prior batch under Output root for regions "
            "already marked Done, and only image what's missing - plus anything listed in "
            "the retake file below, regardless of prior status.")
        form.addRow(self.chk_skip_already_imaged)

        self.edit_retake_file = QLineEdit()
        self.edit_retake_file.setPlaceholderText(
            "(optional) defaults to <output root>/retake_regions.txt")
        self.edit_retake_file.setToolTip(
            "One region name per line (optional \": reason\") - these are imaged "
            "regardless of prior Done status, alongside anything Skip-already-imaged "
            "found missing. Defaults to <output root>/retake_regions.txt if left empty.")
        form.addRow("Retake list file:", self._file_row(self.edit_retake_file, self._browse_retake_file))

        self.btn_retake_flagged = QPushButton("Retake flagged regions")
        self.btn_retake_flagged.setToolTip(
            "Reads the retake list above (or <output root>/retake_regions.txt if that field "
            "is empty) - kept in sync automatically from the global QC report's Accept/"
            "Retake decisions - and sets up 'Process ALL regions' + 'Skip regions already "
            "imaged' so the next run images only what's missing plus everything flagged. "
            "Shows what was found; does NOT start the run - Start is still a separate step.")
        self.btn_retake_flagged.clicked.connect(self._on_retake_flagged)
        form.addRow("", self.btn_retake_flagged)

        # "Block (czi) name" / "Tile prefix" widgets removed 2026-09-04 (dead
        # code, confirmed by reading the actual consumers): the batch folder
        # name comes entirely from build_block_name() parsing the imaging
        # JSON's filename (w#_h#_Slice# + pixel size + timestamp) -
        # CONFIG["czi_name"] is only ever consulted in a fallback branch that
        # fires if build_block_name() itself raises, which it never does (it
        # has its own internal fallback to the JSON's raw filename stem).
        # CONFIG["prefix_base"] is passed into configure_output_for_tile()
        # but never referenced anywhere in that function - every tile's real
        # filename prefix is the hardcoded f"mfov{tile_idx}_{tile_ts}_".
        # Both CONFIG keys still exist (focus_map.py defaults: czi_name=
        # "MyBlock", prefix_base="tile") so nothing downstream breaks; only
        # these two now-misleading widgets are gone.
        self.combo_format = NoWheelComboBox()
        self.combo_format.addItems(["bmp", "tif", "png"])
        self.combo_format.setToolTip("File format saved tile images are written in.")
        form.addRow("Image format:", self.combo_format)
        return box

    def _build_scan_group(self) -> QGroupBox:
        box, form = self._group("Scan settings")
        self.spin_pixel = NoWheelDoubleSpinBox()
        self.spin_pixel.setRange(0.1, 10000.0)
        self.spin_pixel.setDecimals(2)
        self.spin_pixel.setValue(8.0)
        self.spin_pixel.setSuffix(" nm")
        self.spin_pixel.setToolTip("Pixel size for full-resolution tile images - smaller = higher resolution, larger files, slower scan.")
        form.addRow("Pixel size:", self.spin_pixel)

        self.spin_scanrate = NoWheelSpinBox()
        self.spin_scanrate.setRange(1, 30)
        self.spin_scanrate.setValue(4)
        self.spin_scanrate.setToolTip(
            "Beam scan rate for full-resolution tile images - higher = faster scan, more "
            "noise per pixel. Dwell time per pixel roughly doubles per rate step "
            "(documented for rates 1-5: 50/100/200/400/800 ns).")
        form.addRow("Scan speed (rate):", self._scan_rate_row(self.spin_scanrate))

        self.spin_width = NoWheelSpinBox()
        self.spin_width.setRange(1, 100000)
        self.spin_width.setValue(2000)
        self.spin_width.setToolTip("Tile image width in pixels.")
        form.addRow("Tile width (px):", self.spin_width)

        self.spin_height = NoWheelSpinBox()
        self.spin_height.setRange(1, 100000)
        self.spin_height.setValue(1748)
        self.spin_height.setToolTip("Tile image height in pixels.")
        form.addRow("Tile height (px):", self.spin_height)

        self.spin_mfov_overlap = NoWheelDoubleSpinBox()
        self.spin_mfov_overlap.setRange(0.0, 50.0)
        self.spin_mfov_overlap.setDecimals(2)
        self.spin_mfov_overlap.setValue(5.0)
        self.spin_mfov_overlap.setSuffix(" %")
        self.spin_mfov_overlap.setToolTip("Overlap between adjacent mfov hexagons, as a percent of hexagon size - ensures no gap between them.")
        form.addRow("MFOV (hex) overlap:", self.spin_mfov_overlap)

        self.spin_sfov_overlap = NoWheelDoubleSpinBox()
        self.spin_sfov_overlap.setRange(0.0, 100.0)
        self.spin_sfov_overlap.setDecimals(2)
        self.spin_sfov_overlap.setValue(1.0)
        self.spin_sfov_overlap.setSuffix(" µm")
        self.spin_sfov_overlap.setToolTip("Overlap between the individual beam sub-fields within one mfov, in microns.")
        form.addRow("SFOV (inner) overlap:", self.spin_sfov_overlap)
        return box

    def _build_route_group(self) -> QGroupBox:
        box, form = self._group("Route")
        self.combo_travel = NoWheelComboBox()
        self.combo_travel.addItems(["Serpentine", "Comb"])
        self.combo_travel.setToolTip(
            "Tile visiting pattern within a region. Serpentine: alternates direction "
            "row-by-row (shorter total travel). Comb: always returns to the same edge "
            "before starting the next row.")
        form.addRow("Travel pattern:", self.combo_travel)

        self.combo_direction = NoWheelComboBox()
        self.combo_direction.addItems(
            ["Vertical Left", "Vertical Right", "Horizontal Up", "Horizontal Down"])
        self.combo_direction.setToolTip("Which edge/direction the tile route starts from and travels along.")
        form.addRow("Travel direction:", self.combo_direction)

        self.combo_start = NoWheelComboBox()
        self.combo_start.addItems(["Top", "Bottom"])
        self.combo_start.setToolTip("Which row of the region the tile route starts at.")
        form.addRow("Start position:", self.combo_start)
        return box

    def _build_plane_group(self) -> QGroupBox:
        box, form = self._group("Focusing strategy")
        self.combo_plane = NoWheelComboBox()
        self.combo_plane.addItems(list(_PLANE_METHOD_DISPLAY_TO_INTERNAL.keys()))
        self.combo_plane.setCurrentText("ALL_POINTS_PLANE")
        self.combo_plane.setToolTip(
            "How each region's focus/stig plane is built.\n"
            "ALL_POINTS_PLANE: live-measure ALL of the region's own JSON support points and "
            "fit a fresh plane (needs >=3). (internal name: CURRENT_ALL_POINTS_PLANE)\n"
            "ONE_REGION_FSP: remeasure a few of the best prev-focusmap points and apply their "
            "robust offset to the prior plane - the only mode that works with just 1-2 points "
            "per region (see the Plane-update robustness group below). "
            "(internal name: ONE_POINT_OFFSET)\n"
            "WAFER_FIDUCIAL_DIRECT: skip AF/AS measurement entirely for this region - every "
            "mFOV's OL/stig comes directly from evaluating the wafer fiducial file's fitted "
            "plane at that mFOV's (x,y). Fastest, but only as accurate as that file - see "
            "'Wafer fiducial OL file' below.\n"
            "SAMPLE_TOPOGRAPHY_CORRECTION (EXPERIMENTAL): zero AF/AS/B2F-per-tile - every "
            "mFOV's OL, StigX, StigY, AND predicted ProjX/ProjY come from planes fit to the "
            "wafer fiducial file. Z is not separately predicted; "
            "it's still governed by the one-time Z-fiducial startup check. One Beam-to-Fiber "
            "still runs at region start (ZEN's B2F is what actually establishes the real "
            "projective alignment - the predicted ProjX/ProjY are recorded for comparison "
            "only, never sent to hardware).\n"
            "FOCUSMAP_TOPOGRAPHY (EXPERIMENTAL): zero AF/AS - every mFOV's OL/StigX/StigY come "
            "from a plane fit to this region's OWN FocusMap.txt (prev-focusmap file) instead of "
            "the wafer fiducial file. FocusMap.txt has no Z column, so Z still comes from the "
            "wafer fiducial file's Z-plane - that file must still be set. One Beam-to-Fiber "
            "runs at region start; its real ProjX/ProjY reading is recorded (cross-region "
            "interpolation into a predicted value isn't implemented yet).\n"
            "FIDUCIAL_FOCUS_POINT_XY_REGION_OFFSET (EXPERIMENTAL, 2026-09-03): one fresh Beam-to-Fiber -> "
            "AutoFocus -> AutoStig measurement per region, at a point offset from that region's "
            "own support point by ~1 mFOV (random direction each time, so repeated test runs "
            "don't keep bleaching the same tissue). The wafer fiducial plane's tilt is kept as-"
            "fit; only its OL/StigX/StigY intercept is re-leveled per region to match this fresh "
            "measurement. Always runs its own B2F regardless of the b2f_mode override. Lighter-"
            "weight alternative to SAMPLE_TOPOGRAPHY_CORRECTION's once-per-wafer precheck (which "
            "measures every fiducial point and can have many fail their Z-landed check).\n"
            "(THREE_POINT_DELTA / CURRENT_ALL_POINTS_QUADRATIC_RESIDUAL_IDW / MANUAL_POINTS "
            "are hidden here until further notice, not removed - focus_map.py still supports "
            "them, they just need a config override to select for now.)")
        form.addRow("Plane Definition Method:", self.combo_plane)

        self.combo_init_ol = NoWheelComboBox()
        self.combo_init_ol.addItems(["WAFER_FIDUCIAL_PLANE", "PREV_PLANE_AT_FIRST_SP"])
        self.combo_init_ol.setCurrentText("WAFER_FIDUCIAL_PLANE")
        self.combo_init_ol.setToolTip(
            "Coarse starting OL value set once, before any measurement AND before the "
            "region-start Beam-to-Fiber call - a reasonably-focused image improves B2F's "
            "own alignment, even for a method that never runs AF. Matters little for "
            "ONE_REGION_FSP, which immediately overwrites it with each candidate's own "
            "recorded value anyway.\n"
            "WAFER_FIDUCIAL_PLANE: from the wafer fiducial plane (falls back to a fixed "
            "value if unavailable).\n"
            "PREV_PLANE_AT_FIRST_SP: from the prev-focusmap plane (already fiducial-anchored "
            "if that prev-focusmap only had <3 points).\n"
            "(READ_CURRENT / FIXED are hidden here until further notice, not removed.)\n"
            "Auto-set and locked for SAMPLE_TOPOGRAPHY_CORRECTION/FOCUSMAP_TOPOGRAPHY/"
            "FIDUCIAL_FOCUS_POINT_XY_REGION_OFFSET - see Plane Definition Method above - since for "
            "these, this MUST match the same plane source they use for every mFOV, not an "
            "independent choice.")
        form.addRow("Initial OL value:", self.combo_init_ol)

        form.addRow(self._build_focusmap_source_section())
        form.addRow(self._build_wafer_fiducial_section())

        self.combo_best_point = NoWheelComboBox()
        self.combo_best_point.addItems(["AF", "AS_SUM", "AF_AS"])
        self.combo_best_point.setToolTip(
            "Scoring metric used to rank prev-focusmap candidate points (lower/better "
            "confidence wins): AF = autofocus confidence alone, AS_SUM = sum of autostig "
            "X+Y confidence, AF_AS = weighted combination of both.")
        form.addRow("Best-point metric:", self.combo_best_point)

        # Both selections affect which data-source section below is actually
        # relevant - connected here (after every widget referenced by the
        # handler exists) rather than right after each combo is built.
        self.combo_plane.currentTextChanged.connect(self._update_focus_source_visibility)
        self.combo_init_ol.currentTextChanged.connect(self._update_focus_source_visibility)
        self._update_focus_source_visibility()
        # _update_plane_robustness_visibility itself is NOT called here (the
        # Plane-update robustness group is built later, after this method
        # returns - see setup_ui) - just wire the signal now, while
        # combo_plane exists, so it's ready the moment that group appears.
        self.combo_plane.currentTextChanged.connect(self._update_plane_robustness_visibility)
        return box

    def _build_focusmap_source_section(self) -> QWidget:
        """FocusMap.txt-based data source: the "Raw focus-map dir" field plus
        the tool that builds it from a completed run. Always visible,
        regardless of the chosen strategy - focus_map.py unconditionally
        requires a matching FocusMap.txt to exist per region for every
        plane_update_method EXCEPT SAMPLE_TOPOGRAPHY_CORRECTION (which
        predicts OL/StigX/StigY entirely from the wafer-fiducial planes and
        never reads a prev-focusmap file at all - genuinely standalone, not
        just "unused content")."""
        box, v = self._sub_group(
            "FocusMap.txt source (required for every strategy except SAMPLE_TOPOGRAPHY_CORRECTION)")

        self.edit_pfm = QLineEdit()
        self.edit_pfm.setPlaceholderText("raw data folder with slice_<N>_FocusMap.txt (matched per section)")
        self.edit_pfm.setToolTip(
            "prev_focusmap_dir: a folder of per-section FocusMap.txt files (e.g. "
            "S25_FocusMap.txt or slice_25_FocusMap.txt) - matched to each region "
            "automatically by its section number, so you point at the folder once instead "
            "of naming a file per region. A matching file must exist here for every "
            "strategy except SAMPLE_TOPOGRAPHY_CORRECTION (which never reads a prev-focusmap "
            "file at all). Of the rest, only ALL_POINTS_PLANE, ONE_REGION_FSP, and "
            "FOCUSMAP_TOPOGRAPHY actually use its content for their own plane - the others "
            "just need the file to exist to avoid aborting.")
        v.addLayout(self._labeled_row("Raw focus-map dir:", self._file_row(self.edit_pfm, self._browse_pfm)))

        v.addWidget(self._build_focus_map_collect_group())
        return box

    def _build_wafer_fiducial_section(self) -> QWidget:
        """Wafer-wide fiducial data source: just the "Wafer fiducial OL
        file" field - point it at an already-built file (Claude builds
        these from ZEN screenshots directly, on request; HI-MC no longer
        has its own OCR-based builder for this - it proved unreliable
        against real screenshots and Claude's own direct reading of them
        is more accurate anyway). Hidden when the current Plane update
        method + Initial OL value combination can't use it at all - see
        _update_focus_source_visibility."""
        self.grp_wafer_fiducial_section, v = self._sub_group(
            "Wafer-wide fiducial source (needed by WAFER_FIDUCIAL_DIRECT, "
            "SAMPLE_TOPOGRAPHY_CORRECTION, FOCUSMAP_TOPOGRAPHY, ONE_REGION_FSP, or "
            "whenever Initial OL value = WAFER_FIDUCIAL_PLANE)")

        self.edit_wafer_fiducial = QLineEdit()
        self.edit_wafer_fiducial.setPlaceholderText(
            "Wafer-wide fiducial (x,y,z,ol[,stigx,stigy,projx,projy]) points file")
        self.edit_wafer_fiducial.setToolTip(
            "Wafer-wide fiducial markers file (x,y,z,ol[,stigx,stigy,projx,projy]) - gives an "
            "OL/stig estimate at ANY (x,y) on the wafer, not just already-measured points.\n"
            "Required for ONE_REGION_FSP (internal name: ONE_POINT_OFFSET) (or any region with <3 local prev-focusmap "
            "points) to anchor a plane at all; auto-discovered from the imaging JSON's "
            "w<N>h<N> name if left blank. Also required for SAMPLE_TOPOGRAPHY_CORRECTION "
            "(needs OL/StigX/StigY/ProjX/ProjY planes from this file), FIDUCIAL_FOCUS_POINT_XY_REGION_OFFSET "
            "(needs OL/StigX/StigY planes as the tilt this method re-levels per region), and "
            "FOCUSMAP_TOPOGRAPHY (needs just this file's Z-plane, since FocusMap.txt itself has "
            "no Z column).")
        v.addLayout(self._labeled_row(
            "Wafer fiducial OL file:", self._file_row(self.edit_wafer_fiducial, self._browse_wafer_fiducial)))

        return self.grp_wafer_fiducial_section

    def _update_focus_source_visibility(self, *_args):
        plane_display = self.combo_plane.currentText()

        required_init_ol = _TOPOGRAPHY_REQUIRED_INIT_OL.get(plane_display)
        if required_init_ol is not None:
            self.combo_init_ol.setCurrentText(required_init_ol)
        self.combo_init_ol.setEnabled(required_init_ol is None)

        needs_wafer_fiducial = (
            plane_display in _STRATEGIES_USING_WAFER_FIDUCIAL
            or self.combo_init_ol.currentText() == "WAFER_FIDUCIAL_PLANE"
        )
        self.grp_wafer_fiducial_section.setVisible(needs_wafer_fiducial)

    def _build_qc_group(self) -> QGroupBox:
        box, form = self._group("Live QC")
        self.spin_lowq = NoWheelDoubleSpinBox()
        self.spin_lowq.setRange(0.0, 1000.0)
        self.spin_lowq.setDecimals(1)
        self.spin_lowq.setValue(10.0)
        self.spin_lowq.setToolTip("Focus-quality score at/below which a tile is flagged low-quality (red).")
        form.addRow("Low-quality threshold (red):", self.spin_lowq)

        self.spin_highq = NoWheelDoubleSpinBox()
        self.spin_highq.setRange(0.0, 1000.0)
        self.spin_highq.setDecimals(1)
        self.spin_highq.setValue(30.0)
        self.spin_highq.setToolTip("Only used when Focus color mode = tiered")
        form.addRow("High-quality threshold (green):", self.spin_highq)

        self.combo_focus_color = NoWheelComboBox()
        self.combo_focus_color.addItems(["continuous", "tiered"])
        self.combo_focus_color.setToolTip(
            "continuous: smooth colormap. tiered: discrete red/amber/green "
            "using the low/high thresholds above.")
        form.addRow("Focus color mode:", self.combo_focus_color)

        self.spin_extra_beams = NoWheelSpinBox()
        self.spin_extra_beams.setRange(0, 91)
        self.spin_extra_beams.setValue(4)
        self.spin_extra_beams.setToolTip(
            "Extra beams (beyond the fixed/random selection) sampled for QC per tile, on "
            "top of Beam selection mode's own choice.")
        form.addRow("Extra QC beams:", self.spin_extra_beams)

        self.combo_beam_mode = NoWheelComboBox()
        self.combo_beam_mode.addItems(
            ["FIXED_PER_RUN_RANDOM", "FIXED_SET", "PER_MFOV_RANDOM", "ALL"])
        self.combo_beam_mode.setToolTip(
            "ALL checks every beam found on disk per tile (up to 91) instead "
            "of a sampled subset - needed to reliably catch a single bad beam, "
            "at the cost of much more QC work per tile.")
        form.addRow("Beam selection:", self.combo_beam_mode)

        self.chk_beam_outlier_alert = QCheckBox("Alert on single-beam outlier")
        self.chk_beam_outlier_alert.setChecked(True)
        self.chk_beam_outlier_alert.setToolTip(
            "Email/SMS alert (via alerts.py) when one beam's quality is a "
            "statistical outlier vs. the rest of that mfov's beams - most "
            "reliable with Beam selection = ALL. Throttled to once per "
            "beam per region.")
        form.addRow(self.chk_beam_outlier_alert)

        self.spin_beam_outlier_sigma = NoWheelDoubleSpinBox()
        self.spin_beam_outlier_sigma.setRange(1.0, 10.0)
        self.spin_beam_outlier_sigma.setDecimals(1)
        self.spin_beam_outlier_sigma.setValue(3.0)
        self.spin_beam_outlier_sigma.setToolTip(
            "MAD-based outlier threshold (in sigma-equivalents) for flagging a single "
            "beam's quality as a statistical outlier vs. the rest of its mfov.")
        form.addRow("Beam-outlier MAD sigma:", self.spin_beam_outlier_sigma)

        self.spin_beam_outlier_min = NoWheelSpinBox()
        self.spin_beam_outlier_min.setRange(3, 91)
        self.spin_beam_outlier_min.setValue(5)
        self.spin_beam_outlier_min.setToolTip(
            "Minimum valid beams in an mfov needed to judge an outlier")
        form.addRow("Beam-outlier min beams:", self.spin_beam_outlier_min)

        self.spin_jitter = NoWheelDoubleSpinBox()
        self.spin_jitter.setRange(1.0, 10.0)
        self.spin_jitter.setDecimals(2)
        self.spin_jitter.setValue(1.5)
        self.spin_jitter.setToolTip(
            "Ratio of top-band vs. rest-of-image std dev above which a tile is flagged "
            "jittery. ~1.0 = clean, >1.5 = mild, >2.5 = severe.")
        form.addRow("Jitter warn threshold:", self.spin_jitter)

        self.spin_sat = NoWheelDoubleSpinBox()
        self.spin_sat.setRange(0.0, 100.0)
        self.spin_sat.setDecimals(1)
        self.spin_sat.setValue(30.0)
        self.spin_sat.setSuffix(" %")
        self.spin_sat.setToolTip(
            "Saturation percentage above which a tile is flagged as likely outside the "
            "sample ROI (imaging blank resin/substrate).")
        form.addRow("Outside-ROI saturation:", self.spin_sat)

        self.spin_save_every = NoWheelSpinBox()
        self.spin_save_every.setRange(1, 1000)
        self.spin_save_every.setValue(5)
        self.spin_save_every.setToolTip("Refresh the live QC dashboard HTML/PNG every N completed tiles.")
        form.addRow("Save dashboard every:", self.spin_save_every)
        return box

    # -- data-driven extra-param groups (flow through fm_extra) -------------
    def _build_extra_widget(self, kind: str, default, opts: dict):
        if kind == "b":
            w = QCheckBox()
            w.setChecked(bool(default))
            if opts.get("tip"):
                w.setToolTip(opts["tip"])
            return w
        if kind == "i":
            w = NoWheelSpinBox()
            w.setRange(int(opts.get("min", 0)), int(opts.get("max", 1_000_000)))
            w.setValue(int(default))
            if opts.get("suffix"):
                w.setSuffix(opts["suffix"])
            if opts.get("tip"):
                w.setToolTip(opts["tip"])
            return w
        # float
        w = NoWheelDoubleSpinBox()
        w.setDecimals(int(opts.get("dec", 2)))
        w.setRange(float(opts.get("min", 0.0)), float(opts.get("max", 1e9)))
        w.setValue(float(default))
        if opts.get("suffix"):
            w.setSuffix(opts["suffix"])
        if opts.get("tip"):
            w.setToolTip(opts["tip"])
        return w

    def _read_extra(self, w, kind: str):
        if kind == "b":
            return bool(w.isChecked())
        if kind == "i":
            return int(w.value())
        return float(w.value())

    def _scan_rate_row(self, spin) -> QWidget:
        """Wrap a scan-rate spinbox with a live '(X ns/px)' label next to it -
        see _SCAN_RATE_NS_PER_PIXEL above. Blank for any rate outside the
        documented 1-5 range rather than guessing."""
        row = QWidget()
        h = QHBoxLayout(row)
        h.setContentsMargins(0, 0, 0, 0)
        h.addWidget(spin)
        lbl = QLabel()
        lbl.setStyleSheet("color: #9aa0a6;")
        h.addWidget(lbl)
        h.addStretch(1)

        def _update(*_args):
            ns = _SCAN_RATE_NS_PER_PIXEL.get(int(spin.value()))
            lbl.setText(f"({ns} ns/px)" if ns is not None else "")

        spin.valueChanged.connect(_update)
        _update()
        return row

    def _build_extra_group(self, title: str, specs) -> QGroupBox:
        box, form = self._group(title)
        self._extra_group_forms[title] = form
        for key, label, kind, default, opts in specs:
            w = self._build_extra_widget(kind, default, opts)
            self._extra_widgets[key] = (w, kind)
            row = self._scan_rate_row(w) if key.endswith("scan_rate") else w
            form.addRow(label + ":", row)
        return box

    def _update_plane_robustness_visibility(self, *_args):
        """Show only the Plane-update robustness knobs that actually affect
        whichever plane_update_method is currently selected - see
        _PLANE_ROBUSTNESS_RELEVANCE. Most of this section is dormant for any
        one strategy; this makes that obvious instead of the operator having
        to already know which knobs apply to which method."""
        form = self._extra_group_forms.get("Plane-update robustness")
        if form is None:
            return  # group not built yet (combo_plane wires this signal before it exists)
        internal = _PLANE_METHOD_DISPLAY_TO_INTERNAL.get(
            self.combo_plane.currentText(), self.combo_plane.currentText())
        for key, _label, _kind, _default, _opts in _PLANE_ROBUSTNESS_SPECS:
            entry = self._extra_widgets.get(key)
            if entry is None:
                continue
            relevant_methods = _PLANE_ROBUSTNESS_RELEVANCE.get(key)
            visible = relevant_methods is None or internal in relevant_methods
            # The row's field widget may be wrapped (e.g. _scan_rate_row) -
            # setRowVisible accepts either the field widget or its wrapper,
            # both work since QFormLayout tracks rows by whatever widget was
            # passed to addRow.
            form.setRowVisible(entry[0], visible)

    def _build_advanced_group(self) -> QGroupBox:
        box = QGroupBox("Advanced overrides  (key = value, one per line; value is JSON)")
        box.setCheckable(True)
        box.setChecked(False)
        box.setToolTip(
            "When unchecked, whatever is typed in the two boxes below is ignored - neither "
            "gets merged into the run's config. Checked automatically if a restored config "
            "had any override in either box, so a previous run's overrides don't silently "
            "stop applying just because the page was reopened.")
        self.grp_advanced_overrides = box
        v = QVBoxLayout(box)
        v.addWidget(QLabel("Acquisition pipeline settings (focus_map.py) — any CONFIG key, e.g.  manual_sp_id = 5"))
        self.txt_fm_extra = QPlainTextEdit()
        self.txt_fm_extra.setPlaceholderText('manual_sp_id = 5\nb2f_mode = "OFF"\nplane_update_sp_ids = [8, 9, 10]')
        self.txt_fm_extra.setFixedHeight(80)
        self.txt_fm_extra.setToolTip(
            "Sets any focus_map.py CONFIG key that doesn't already have its own field "
            "elsewhere on this page - e.g. manual_sp_id, b2f_mode, plane_update_sp_ids, "
            "region_order_method. One 'key = value' per line; value is parsed as JSON "
            "(numbers, true/false, \"quoted strings\", [lists]).\n"
            "Only takes effect while 'Advanced overrides' above is checked.")
        v.addWidget(self.txt_fm_extra)
        v.addWidget(QLabel("Live QC settings (main_live_qc.py) — any LIVE_QC_CONFIG key, e.g.  stig_display_max = 40"))
        self.txt_qc_extra = QPlainTextEdit()
        self.txt_qc_extra.setPlaceholderText('fixed_beams = [1, 10, 28, 55, 82]\ntop_fraction = 0.5')
        self.txt_qc_extra.setFixedHeight(80)
        self.txt_qc_extra.setToolTip(
            "Sets any main_live_qc.py LIVE_QC_CONFIG key that doesn't already have its own "
            "field elsewhere on this page - e.g. fixed_beams, top_fraction. Same "
            "'key = value' / JSON-value format as the box above.\n"
            "Only takes effect while 'Advanced overrides' above is checked.")
        v.addWidget(self.txt_qc_extra)
        return box

    def _build_action_row(self) -> QHBoxLayout:
        row = QHBoxLayout()
        self.btn_start = QPushButton("▶  Start")
        self.btn_start.clicked.connect(self._on_start)
        self.btn_stop = QPushButton("■  Stop")
        self.btn_stop.clicked.connect(self._on_stop)
        self.btn_stop.setEnabled(False)
        self.btn_open = QPushButton("Open QC output…")
        self.btn_open.clicked.connect(self._open_output)
        self.btn_open.setEnabled(False)

        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.lbl_progress = QLabel("idle")
        self.lbl_progress.setMinimumWidth(220)

        row.addWidget(self.btn_start)
        row.addWidget(self.btn_stop)
        row.addWidget(self.btn_open)
        row.addSpacing(12)
        row.addWidget(self.progress_bar, stretch=1)
        row.addWidget(self.lbl_progress)
        return row

    # -- stage Z safety alert -------------------------------------------------
    # Live readback of AP_STAGE_AT_Z, printed by stage_move_xy_z() after EVERY
    # stage move (see focus_map.py / PipelineRunner.stage_z) - not just moves
    # that explicitly command a Z target. Found via a real run where the
    # stage drifted back toward Z=0 between moves that never touched Z at
    # all, with no other symptom until AF/B2F failed; this watches for that
    # drift continuously in the background. Deliberately NOT a visible/
    # configurable panel - the threshold below is a fixed internal safety net,
    # not a per-run setting, and the banner it drives only occupies space on
    # the page while an alert is actually active (hidden the rest of the time).
    _Z_ALERT_THRESHOLD_UM = 37000.0

    def _build_stage_z_group(self) -> QLabel:
        self.lbl_stage_z = QLabel("")
        self.lbl_stage_z.setStyleSheet(
            "font-family: monospace; font-size: 12px; font-weight: bold; color: #ffffff; "
            "background-color: #7a1f1f; padding: 2px 4px;")
        self.lbl_stage_z.setToolTip(
            "Appears only while the live AP_STAGE_AT_Z readback is below the "
            f"{self._Z_ALERT_THRESHOLD_UM:,.0f} um safety threshold - catches the stage "
            "drifting to a low/parked Z between moves that never explicitly command a Z "
            "target, which otherwise has no symptom until AF/B2F fails.")
        self.lbl_stage_z.setVisible(False)
        return self.lbl_stage_z

    def _on_stage_z(self, z: float) -> None:
        is_low = z < self._Z_ALERT_THRESHOLD_UM

        if is_low:
            self.lbl_stage_z.setText(
                f"⚠ Stage Z: {z:,.1f} um  (below {self._Z_ALERT_THRESHOLD_UM:,.0f} um safety threshold!)")
        self.lbl_stage_z.setVisible(is_low)

        if is_low and not self._z_alert_active:
            self._z_alert_active = True
            self.log_panel.append_log(
                "HI-MC", "ERROR",
                f"Stage Z dropped to {z:,.1f} um, below the "
                f"{self._Z_ALERT_THRESHOLD_UM:,.0f} um safety threshold.")
        elif not is_low and self._z_alert_active:
            self._z_alert_active = False
            self.log_panel.append_log("HI-MC", "INFO", f"Stage Z back above threshold: {z:,.1f} um.")

    # -- failure notifications -----------------------------------------------
    # Best-effort email alert (via alerts.py, same SMTP relay as the SFN
    # module's calibration/stage-init alerts) when THIS run looks like it's
    # going badly, so an operator who has stepped away finds out before
    # coming back to a wafer that silently sat idle/failing for hours.
    # Deliberately invisible to the user, same treatment as the stage-Z
    # safety alert above: no GUI panel, fixed thresholds, always running in
    # the background. Recipients come from a plain text file (see
    # _notify_emails_path/_notify_recipients) that this page just blindly
    # reads at send time. alert_config.json's own "enabled" flag and SMTP
    # settings still gate whether anything can be sent at all (one on/off
    # switch + one mail relay for the whole app).
    _NOTIFY_FAIL_THRESHOLD = 2         # region failures in one run before the multi-failure alert fires
    _NOTIFY_ON_UNEXPECTED_EXIT = True  # alert on a non-zero/crash pipeline exit that wasn't a user Stop

    # -- recipients: a plain text file this page blindly reads, one address
    # per line ("#"-prefixed lines and blank lines ignored) - deliberately
    # not a GUI field, so recipients are edited directly in the file, not
    # through the app. Seeded with a commented template the first time this
    # page loads if the file doesn't exist yet; never overwritten after that.
    def _notify_emails_path(self) -> Path:
        repo_root = Path(__file__).resolve().parents[3]
        return repo_root / "himc_notify_emails.txt"

    def _ensure_notify_emails_file(self) -> None:
        path = self._notify_emails_path()
        if path.is_file():
            return
        try:
            path.write_text(
                "# One email address per line. Lines starting with # are ignored.\n"
                "# These are notified on multi-region-failure / unexpected pipeline exit\n"
                "# during a HI-MC run (see alert_config.json for the SMTP settings that\n"
                "# actually deliver it - this file only says who to send to).\n"
                "#\n"
                "# example@yourlab.org\n",
                encoding="utf-8",
            )
        except Exception:
            pass  # best-effort; _notify_recipients() just reads [] if this never got created

    def _notify_recipients(self) -> list:
        try:
            text = self._notify_emails_path().read_text(encoding="utf-8")
        except Exception:
            return []
        out = []
        for line in text.splitlines():
            line = line.split("#", 1)[0].strip()
            if not line:
                continue
            for part in line.replace(";", ",").split(","):
                part = part.strip()
                if part:
                    out.append(part)
        return out

    def _maybe_send_failure_alert(self, subject: str, body: str) -> None:
        if self._alert_sent_this_run:
            return
        to_addrs = self._notify_recipients()
        if not to_addrs:
            return
        self._alert_sent_this_run = True
        try:
            import alerts
            sent = alerts.send_alert(subject, body, to_addrs=to_addrs)
            if sent:
                self.log_panel.append_log("HI-MC", "INFO", f"Failure alert sent to {', '.join(to_addrs)}: {subject}")
            else:
                self.log_panel.append_log(
                    "HI-MC", "WARNING",
                    f"Failure alert NOT sent (check alert_config.json's enabled/SMTP settings "
                    f"and the SMTP password env var it names): {subject}")
        except Exception as exc:
            self.log_panel.append_log("HI-MC", "WARNING", f"Failure alert failed to send: {exc}")

    # -- region imaging order + progress -------------------------------------
    # Nowhere else in the GUI shows the order regions will actually be
    # imaged in (NOT necessarily the imaging JSON's own listed order - by
    # default focus_map.py computes a shortest-path travel order - see
    # compute_region_processing_order), or which ones are done vs. still
    # pending. This list is that: populated from the SAME log lines the
    # pipeline already prints (see PipelineRunner's region_order/
    # region_started/region_done/region_failed/region_skipped signals), so
    # it can never disagree with what actually ran. Also restored from the
    # last batch's region_manifest.json on page load (see
    # _restore_region_progress_from_last_batch) so a relaunch after a crash
    # shows "where did we leave off" immediately, before Start is even
    # clicked again.
    _STATUS_COLORS = {
        "pending": None,                      # leave at the list's default background
        "running": QColor("#0e639c"),
        "done": QColor("#1e5b2e"),
        "failed": QColor("#7a1f1f"),
        "skipped": QColor("#7a5b00"),
    }
    _STATUS_LABELS = {
        "pending": "pending", "running": "running", "done": "done",
        "failed": "FAILED", "skipped": "skipped",
    }
    # "pending" rows have no background color (_STATUS_COLORS["pending"] is
    # None, so they stay at the list's default dark background) - explicit
    # light text is needed for them specifically, since the default/inherited
    # foreground read as unreadably dark against that background.
    _PENDING_FG = QColor("#cccccc")

    def _build_region_progress_group(self) -> QGroupBox:
        # Built directly (not via self._group(), which installs a QFormLayout
        # meant for label/field rows) - this panel is just one big list, so
        # it gets a QVBoxLayout of its own instead.
        box = QGroupBox("Region imaging order && progress")
        v = QVBoxLayout(box)

        self.lbl_region_progress_summary = QLabel(
            "No region order yet - shown here as soon as a run starts (before any hardware work begins).")
        self.lbl_region_progress_summary.setStyleSheet("color: #9aa0a6; font-size: 11px;")
        self.lbl_region_progress_summary.setWordWrap(True)
        v.addWidget(self.lbl_region_progress_summary)

        self.list_region_progress = QListWidget()
        self.list_region_progress.setToolTip(
            "Order this run will actually image regions in (may differ from the imaging "
            "JSON's own listed order - see Plane update method / region_order_method), "
            "with each region's live status.")
        self.list_region_progress.setMaximumHeight(160)
        self.list_region_progress.setUniformItemSizes(True)
        v.addWidget(self.list_region_progress)
        return box

    def _region_progress_reset(self, names: list) -> None:
        self.list_region_progress.clear()
        self._region_rows = {}
        for i, name in enumerate(names, start=1):
            item = QListWidgetItem(f"{i:02d}.  {name}  —  pending")
            item.setForeground(self._PENDING_FG)
            self.list_region_progress.addItem(item)
            self._region_rows[name] = item
        self._region_progress_update_summary()

    def _region_progress_set_status(self, name: str, status: str, detail: str = "") -> None:
        item = self._region_rows.get(name)
        if item is None:
            # A region reported outside any known order (e.g. single-region
            # mode, which never prints "[Regions] Will process...") - append
            # it on the fly instead of silently dropping the update.
            item = QListWidgetItem("")
            self.list_region_progress.addItem(item)
            self._region_rows[name] = item
        idx = self.list_region_progress.row(item)
        label = self._STATUS_LABELS.get(status, status)
        suffix = f"  ({detail})" if detail else ""
        item.setText(f"{idx + 1:02d}.  {name}  —  {label}{suffix}")
        color = self._STATUS_COLORS.get(status)
        item.setBackground(color if color else self.list_region_progress.palette().base())
        item.setForeground(QColor("#ffffff") if color else self._PENDING_FG)
        if status == "running":
            self.list_region_progress.scrollToItem(item)
        self._region_progress_update_summary()

    def _region_progress_update_summary(self) -> None:
        if not self._region_rows:
            return
        total = len(self._region_rows)
        done = sum(1 for it in self._region_rows.values() if "done" in it.text())
        failed = sum(1 for it in self._region_rows.values() if "FAILED" in it.text())
        skipped = sum(1 for it in self._region_rows.values() if "skipped" in it.text())
        self.lbl_region_progress_summary.setText(
            f"{done}/{total} done"
            + (f", {failed} failed" if failed else "")
            + (f", {skipped} skipped" if skipped else "")
            + f"  ({total - done - failed - skipped} remaining)")

    def _on_region_order(self, names: list) -> None:
        self._region_progress_reset(names)

    def _on_region_skipped(self, name: str) -> None:
        self._region_progress_set_status(name, "skipped")

    def _on_region_failed(self, name: str) -> None:
        self._region_progress_set_status(name, "failed")
        self._run_failed_regions.append(name)
        threshold = self._NOTIFY_FAIL_THRESHOLD
        if len(self._run_failed_regions) == threshold:
            self._maybe_send_failure_alert(
                f"HI-MC: {threshold} region(s) failed",
                f"{threshold} region(s) have failed in this HI-MC run so far:\n"
                + "\n".join(f"  - {n}" for n in self._run_failed_regions)
                + f"\n\nImaging JSON: {self.edit_json.text().strip()}"
                + f"\nOutput root: {self.edit_output.text().strip()}")

    # -- external wiring ----------------------------------------------------
    def set_imaging_json_path(self, path: str):
        """Auto-fill the imaging-JSON field when the shared Parameters dock
        loads a Zeiss experiment JSON (convenience; the field is still editable)."""
        if path:
            self.edit_json.setText(path)
            try:
                self.log_panel.append_log("HI-MC", "INFO", f"Imaging JSON set: {path}")
            except Exception:
                pass

    # -- small handlers -----------------------------------------------------
    def _browse_json(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Select imaging JSON", "", "JSON (*.json);;All files (*)")
        if path:
            self.edit_json.setText(path)

    def _browse_pfm(self):
        path = QFileDialog.getExistingDirectory(self, "Select prev-focusmap folder")
        if path:
            self.edit_pfm.setText(path)

    # -- collect FocusMap.txt files from a completed run --------------------
    def _browse_focus_map_source(self):
        path = QFileDialog.getExistingDirectory(self, "Select completed acquisition run folder")
        if path:
            self.edit_focus_map_source.setText(path)

    def _on_collect_focus_maps(self):
        source_root = self.edit_focus_map_source.text().strip()
        if not source_root:
            self.log_panel.append_log("HI-MC", "ERROR", "Pick a source run folder first.")
            return

        dest_dir = self.edit_pfm.text().strip()
        if not dest_dir:
            dest_dir = source_root.rstrip("\\/") + "_collected"
            self.log_panel.append_log(
                "HI-MC", "INFO", f"Raw focus-map dir left blank - using {dest_dir}")

        from msem_control.engine.focus_map_collector import collect_focus_maps

        try:
            result = collect_focus_maps(source_root, dest_dir)
        except (FileNotFoundError, RuntimeError) as exc:
            self.log_panel.append_log("HI-MC", "ERROR", str(exc))
            return
        except Exception as exc:
            self.log_panel.append_log("HI-MC", "ERROR", f"Collect Focus Maps failed: {exc}")
            return

        self.log_panel.append_log(
            "HI-MC", "INFO", f"Found {result.found_count} FocusMap.txt file(s) under {source_root}")
        for src, dest, note in result.copied:
            self.log_panel.append_log(
                "HI-MC", "INFO", f"  {os.path.basename(os.path.dirname(src))}/FocusMap.txt -> "
                f"{os.path.basename(dest)}  [{note}]")
        for w in result.warnings:
            self.log_panel.append_log("HI-MC", "WARNING", w)
        for path, reason in result.skipped:
            self.log_panel.append_log("HI-MC", "WARNING", f"Skipped {path}: {reason}")

        self.log_panel.append_log(
            "HI-MC", "INFO",
            f"Copied {len(result.copied)}/{result.found_count} file(s) to {dest_dir}")
        self.edit_pfm.setText(dest_dir)
        self.log_panel.append_log("HI-MC", "INFO", f"Set Raw focus-map dir to {dest_dir}")

    def _browse_output(self):
        path = QFileDialog.getExistingDirectory(self, "Select output root")
        if path:
            self.edit_output.setText(path)

    def _browse_wafer_fiducial(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Select wafer fiducial OL points file", "", "Text files (*.txt);;All files (*)")
        if path:
            self.edit_wafer_fiducial.setText(path)

    def _browse_retake_file(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Select retake list file", "", "Text files (*.txt);;All files (*)")
        if path:
            self.edit_retake_file.setText(path)

    def _on_retake_flagged(self):
        """Read the retake list (kept in sync from the global QC report's
        Accept/Retake decisions - see live_qc_support.sync_retake_list) and
        set up the two existing checkboxes that already make a run image
        only what's missing plus everything flagged, instead of requiring
        the operator to know that combination exists. Does NOT start the
        run - Start is still a separate, explicit step."""
        explicit_path = self.edit_retake_file.text().strip()
        if explicit_path:
            retake_path = Path(explicit_path)
        else:
            output_root = self.edit_output.text().strip()
            if not output_root:
                QMessageBox.warning(
                    self, "Retake flagged regions",
                    "Set 'Image storage location' (or 'Retake list file' directly) first - "
                    "the retake list defaults to <output root>/retake_regions.txt.")
                return
            retake_path = Path(output_root) / "retake_regions.txt"

        try:
            from live_qc_support import read_retake_list
            entries = read_retake_list(retake_path)
        except Exception as exc:
            QMessageBox.warning(
                self, "Retake flagged regions", f"Could not read {retake_path}:\n{exc}")
            return

        if not entries:
            QMessageBox.information(
                self, "Retake flagged regions",
                f"No regions are currently flagged for retake in:\n{retake_path}\n\n"
                "Flag some from the global QC report's Accept/Retake/Not-verified controls "
                "first (it writes this file automatically the next time it's regenerated).")
            return

        self.chk_process_all.setChecked(True)
        self.chk_skip_already_imaged.setChecked(True)
        self.edit_region_filter.clear()
        if not explicit_path:
            self.edit_retake_file.setText(str(retake_path))

        lines = []
        for name in sorted(entries.keys()):
            info = entries[name]
            kind = info.get("kind", "full")
            reason = info.get("reason") or ""
            lines.append(f"{name} ({kind})" + (f" - {reason}" if reason else ""))

        self.log_panel.append_log(
            "HI-MC", "INFO",
            f"Retake flagged regions: {len(entries)} region(s) from {retake_path} - "
            f"'Process ALL regions' and 'Skip regions already imaged' are now checked, "
            f"region filter cleared. Review below, then Start when ready.")
        QMessageBox.information(
            self, "Retake flagged regions",
            f"{len(entries)} region(s) flagged for retake (from {retake_path}):\n\n"
            + "\n".join(lines)
            + "\n\n'Process ALL regions' and 'Skip regions already imaged' are now checked, "
              "and the region filter was cleared, so the next run images only what's still "
              "missing plus everything listed above.\n\nThis did NOT start the run - click "
              "Start when ready.")

    # -- override parsing ---------------------------------------------------
    def _parse_overrides(self, text: str, where: str) -> dict:
        """Parse 'key = value' lines; value is JSON-decoded (fallback: string).
        Bad lines are logged + skipped (never block a run)."""
        out: dict = {}
        for raw in text.splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                self.log_panel.append_log("HI-MC", "WARNING",
                                          f"{where}: ignoring line without '=': {line}")
                continue
            key, _, val = line.partition("=")
            key = key.strip()
            val = val.strip()
            if not key:
                continue
            try:
                out[key] = json.loads(val)
            except Exception:
                out[key] = val  # treat as a plain string
        return out

    # -- restore from a previous run's config --------------------------------
    def _restore_last_config(self) -> None:
        """Load <repo_root>/msem_run_config.json (the exact config the LAST
        run was launched with - see _on_start/PipelineRunner.start) back into
        every widget, so relaunching this page after a crash needs as little
        re-entry as possible. Best-effort: no prior file, or one that fails
        to parse, just leaves every widget at its hardcoded default - never
        blocks the page from loading."""
        repo_root = Path(__file__).resolve().parents[3]
        config_path = repo_root / "msem_run_config.json"
        if not config_path.is_file():
            self.log_panel.append_log(
                "HI-MC", "INFO", "No previous run config found - starting from defaults.")
            return
        try:
            cfg = json.loads(config_path.read_text(encoding="utf-8"))
        except Exception as exc:
            self.log_panel.append_log(
                "HI-MC", "WARNING", f"Could not read {config_path.name}: {exc} - starting from defaults.")
            return

        from msem_control.engine.pipeline_runner import config_to_ui
        try:
            ui = config_to_ui(cfg)
        except Exception as exc:
            self.log_panel.append_log(
                "HI-MC", "WARNING", f"Could not apply {config_path.name}: {exc} - starting from defaults.")
            return

        self._apply_ui_dict(ui)
        region = ui.get("region_name") or ui.get("region_name_filter") or "(all regions)"
        self.log_panel.append_log(
            "HI-MC", "INFO",
            f"Restored settings from last run: {ui.get('imaging_json_path', '(none)')} "
            f"-> {ui.get('output_root', '(none)')}  [{region}]")

        self._restore_region_progress_from_last_batch(ui.get("output_root"))

    def _restore_region_progress_from_last_batch(self, output_root) -> None:
        """Best-effort: populate the region progress list from the LAST
        batch's region_manifest.json (see focus_map._write_region_manifest),
        so a relaunch after a crash shows "where did we leave off" before
        Start is even clicked again - not just once a new run starts
        printing its own order. A region still "pending" here may actually
        be the one that was running when the crash happened; the manifest
        alone can't tell the two apart, since it's only updated when a
        region finishes or fails."""
        if not output_root or not os.path.isdir(str(output_root)):
            return
        try:
            batch_dirs = [
                d for d in Path(output_root).iterdir()
                if d.is_dir() and (d / "region_manifest.json").is_file()
            ]
            if not batch_dirs:
                return
            latest = max(batch_dirs, key=lambda d: (d / "region_manifest.json").stat().st_mtime)
            data = json.loads((latest / "region_manifest.json").read_text(encoding="utf-8"))
            entries = data.get("regions") or []
            if not entries:
                return
        except Exception as exc:
            self.log_panel.append_log(
                "HI-MC", "WARNING", f"Could not read last batch's region_manifest.json: {exc}")
            return

        names = [str(e.get("name")) for e in entries if e.get("name")]
        self._region_progress_reset(names)
        for e in entries:
            name = e.get("name")
            status = str(e.get("status") or "pending")
            if not name or status == "pending":
                continue  # already shown as pending by _region_progress_reset
            detail = str(e.get("detail") or "") if status == "failed" else ""
            self._region_progress_set_status(name, status, detail)
        self.log_panel.append_log(
            "HI-MC", "INFO",
            f"Region progress restored from {latest.name}/region_manifest.json "
            f"({len(entries)} region(s) from the last run)")

    def _apply_ui_dict(self, ui: dict) -> None:
        """Inverse of _build_ui_dict(): push a UI-shaped dict (see
        config_to_ui) into every widget it names. Every `if key in ui:` guard
        below leaves the widget at its current value when the key is simply
        absent (an older config, or one that never set that field)."""
        def s(edit: QLineEdit, key: str):
            if ui.get(key) is not None:
                edit.setText(str(ui[key]))

        def chk(box: QCheckBox, key: str):
            if key in ui:
                box.setChecked(bool(ui[key]))

        def spin(w, key: str):
            if key in ui:
                try:
                    w.setValue(type(w.value())(ui[key]))
                except Exception:
                    pass

        def combo(w: QComboBox, key: str):
            if ui.get(key):
                w.setCurrentText(str(ui[key]))

        s(self.edit_json, "imaging_json_path")
        s(self.edit_output, "output_root")
        combo(self.combo_format, "combo_format")
        spin(self.spin_pixel, "acq_pixel_size_nm")
        spin(self.spin_scanrate, "acq_scan_speed")
        spin(self.spin_width, "acq_width")
        spin(self.spin_height, "acq_height")
        spin(self.spin_mfov_overlap, "mfov_overlap_pct")
        spin(self.spin_sfov_overlap, "sfov_overlap_um")
        combo(self.combo_travel, "travel_pattern")
        combo(self.combo_direction, "travel_direction")
        combo(self.combo_start, "start_position")
        if ui.get("plane_update_method"):
            display = _PLANE_METHOD_INTERNAL_TO_DISPLAY.get(
                ui["plane_update_method"], ui["plane_update_method"])
            self.combo_plane.setCurrentText(display)
        combo(self.combo_init_ol, "initial_ol_mode")
        s(self.edit_wafer_fiducial, "wafer_fiducial_ol_file")
        combo(self.combo_best_point, "best_point_metric")

        if "process_all_regions" in ui:
            self.chk_process_all.setChecked(bool(ui["process_all_regions"]))
        if "preflight_skip_missing" in ui:
            self.chk_preflight_skip.setChecked(bool(ui["preflight_skip_missing"]))
        s(self.edit_region_name, "region_name")
        s(self.edit_region_filter, "region_name_filter")
        if "skip_already_imaged" in ui:
            self.chk_skip_already_imaged.setChecked(bool(ui["skip_already_imaged"]))
        s(self.edit_retake_file, "retake_regions_file")
        s(self.edit_pfm, "prev_focusmap_dir")

        spin(self.spin_lowq, "qc_low_quality_threshold")
        spin(self.spin_highq, "qc_high_quality_threshold")
        combo(self.combo_focus_color, "qc_focus_color_mode")
        spin(self.spin_extra_beams, "qc_num_extra_beams")
        combo(self.combo_beam_mode, "qc_beam_selection_mode")
        if "qc_beam_outlier_alert_enabled" in ui:
            self.chk_beam_outlier_alert.setChecked(bool(ui["qc_beam_outlier_alert_enabled"]))
        spin(self.spin_beam_outlier_sigma, "qc_beam_outlier_mad_sigma")
        spin(self.spin_beam_outlier_min, "qc_beam_outlier_min_beams")
        spin(self.spin_jitter, "qc_jitter_warn_threshold")
        spin(self.spin_sat, "qc_outside_roi_sat_threshold")
        spin(self.spin_save_every, "qc_save_every")

        # data-driven extra-param widgets (Stage/timing, Z fiducial, Z-search,
        # Autofocus, Autostig, Plane-update robustness, Thumbnails) - anything
        # NOT claimed here falls through to the advanced-overrides text boxes
        # below, so a setting never silently disappears just because this
        # page doesn't have a dedicated widget for it (yet).
        fm_rest = dict(ui.get("fm_rest") or {})
        lq_rest = dict(ui.get("lq_rest") or {})
        for key, (w, kind) in self._extra_widgets.items():
            if key in fm_rest:
                self._write_extra(w, kind, fm_rest.pop(key))

        def _overrides_text(rest: dict) -> str:
            lines = []
            for k, v in sorted(rest.items()):
                if v is None or v == "" or v == {} or v == []:
                    continue
                lines.append(f"{k} = {json.dumps(v)}")
            return "\n".join(lines)

        restored_any_override = False
        if fm_rest:
            fm_text = _overrides_text(fm_rest)
            self.txt_fm_extra.setPlainText(fm_text)
            restored_any_override = restored_any_override or bool(fm_text)
        if lq_rest:
            lq_text = _overrides_text(lq_rest)
            self.txt_qc_extra.setPlainText(lq_text)
            restored_any_override = restored_any_override or bool(lq_text)
        if restored_any_override:
            # Otherwise a previous run's overrides would silently stop
            # applying just because the page was reopened - see the
            # unchecked-box-doesn't-actually-gate-anything note in
            # _build_advanced_group's tooltip.
            self.grp_advanced_overrides.setChecked(True)

    def _write_extra(self, w, kind: str, value) -> None:
        """Inverse of _read_extra() - see _build_extra_widget for the kinds."""
        try:
            if kind == "b":
                w.setChecked(bool(value))
            elif kind == "i":
                w.setValue(int(value))
            else:
                w.setValue(float(value))
        except Exception:
            pass  # stale/unparseable value in an old config - leave the default

    # -- build config from the form ----------------------------------------
    def _build_ui_dict(self, repo_root: Path) -> dict:
        ui = {
            "imaging_json_path": self.edit_json.text().strip(),
            "acquire_script_path": str(repo_root / "focus_map.py"),
            "output_root": self.edit_output.text().strip(),
            "combo_format": self.combo_format.currentText().strip(),
            "acq_pixel_size_nm": self.spin_pixel.value(),
            "acq_scan_speed": self.spin_scanrate.value(),
            "acq_width": self.spin_width.value(),
            "acq_height": self.spin_height.value(),
            "mfov_overlap_pct": self.spin_mfov_overlap.value(),
            "sfov_overlap_um": self.spin_sfov_overlap.value(),
            "travel_pattern": self.combo_travel.currentText(),
            "travel_direction": self.combo_direction.currentText(),
            "start_position": self.combo_start.currentText(),
            "plane_update_method": _PLANE_METHOD_DISPLAY_TO_INTERNAL.get(
                self.combo_plane.currentText(), self.combo_plane.currentText()),
            "initial_ol_mode": self.combo_init_ol.currentText(),
            "best_point_metric": self.combo_best_point.currentText(),
            # live_qc
            "qc_low_quality_threshold": self.spin_lowq.value(),
            "qc_high_quality_threshold": self.spin_highq.value(),
            "qc_focus_color_mode": self.combo_focus_color.currentText(),
            "qc_num_extra_beams": self.spin_extra_beams.value(),
            "qc_beam_selection_mode": self.combo_beam_mode.currentText(),
            "qc_beam_outlier_alert_enabled": self.chk_beam_outlier_alert.isChecked(),
            "qc_beam_outlier_mad_sigma": self.spin_beam_outlier_sigma.value(),
            "qc_beam_outlier_min_beams": self.spin_beam_outlier_min.value(),
            "qc_jitter_warn_threshold": self.spin_jitter.value(),
            "qc_outside_roi_sat_threshold": self.spin_sat.value(),
            "qc_save_every": self.spin_save_every.value(),
        }

        # Always pass process_all_regions + whatever the user typed; focus_map
        # picks region_name vs region_name_filter based on the flag.
        ui["process_all_regions"] = self.chk_process_all.isChecked()
        rn = self.edit_region_name.text().strip()
        if rn:
            ui["region_name"] = rn
        # Always set (even "") - focus_map.CONFIG's own hardcoded default for
        # this key is the leftover dev value "R1" (see focus_map.py's CONFIG
        # dict), and apply_external_config_override only overrides keys
        # actually PRESENT in the written config - so omitting this key when
        # the field is blank would silently leave "R1" active instead of
        # "every region", contradicting this field's own tooltip above.
        ui["region_name_filter"] = self.edit_region_filter.text().strip()

        pfm = self.edit_pfm.text().strip()
        if pfm:
            ui["prev_focusmap_dir"] = pfm
            ui["preflight_skip_missing"] = self.chk_preflight_skip.isChecked()

        ui["skip_already_imaged"] = self.chk_skip_already_imaged.isChecked()
        retake_file = self.edit_retake_file.text().strip()
        if retake_file:
            ui["retake_regions_file"] = retake_file

        # data-driven extra widgets (Stage/AF/AS/plane groups), then let the
        # advanced text box override any of them (explicit wins).
        fm_extra = {key: self._read_extra(w, kind) for key, (w, kind) in self._extra_widgets.items()}
        # The AF fine/coarse range widgets are edited in microns (see
        # _OL_UA_PER_UM above) but focus_map.py's AP_AUTOFOCUS_RANGE dispatch
        # reads the *_uA CONFIG keys, not *_um - derive and send both so the
        # um value the operator actually set takes effect.
        for _um_key, _ua_key in (("af_fine_range_um", "af_fine_range_uA"),
                                  ("af_coarse_range_um", "af_coarse_range_uA")):
            if _um_key in fm_extra:
                fm_extra[_ua_key] = round(float(fm_extra[_um_key]) * _OL_UA_PER_UM)
        wafer_fiducial = self.edit_wafer_fiducial.text().strip()
        if wafer_fiducial:
            fm_extra["wafer_fiducial_ol_file"] = wafer_fiducial
        # "Advanced overrides" checked -> merge the two free-text boxes in;
        # unchecked -> ignore them entirely (fm_extra still gets the
        # dedicated-widget values above either way - only the free-text
        # portion is gated).
        if self.grp_advanced_overrides.isChecked():
            fm_extra.update(self._parse_overrides(self.txt_fm_extra.toPlainText(), "focus_map override"))
            qc_extra = self._parse_overrides(self.txt_qc_extra.toPlainText(), "live_qc override")
        else:
            qc_extra = {}
        if fm_extra:
            ui["fm_extra"] = fm_extra
        if qc_extra:
            ui["qc_extra"] = qc_extra
        return ui

    # -- start / stop -------------------------------------------------------
    def _on_start(self):
        from msem_control.engine.pipeline_runner import PipelineRunner, build_run_config

        # don't double-run; the engine pause flag is shared with the Acquisition page
        if self._pipeline_runner is not None and self._pipeline_runner.is_running():
            self.log_panel.append_log("HI-MC", "WARNING", "A run is already in progress.")
            return
        if self.engine is not None and self.engine.is_paused():
            self.log_panel.append_log(
                "HI-MC", "WARNING",
                "Another acquisition already owns the scope (engine paused). Stop it first.")
            return

        json_path = self.edit_json.text().strip()
        if not json_path or not os.path.isfile(json_path):
            self.log_panel.append_log(
                "HI-MC", "ERROR", "Pick a valid imaging JSON first.")
            return
        if not self.chk_process_all.isChecked() and not self.edit_region_name.text().strip():
            self.log_panel.append_log(
                "HI-MC", "ERROR",
                "Single-region mode: enter a Region name (or tick 'Process ALL regions').")
            return

        repo_root = Path(__file__).resolve().parents[3]
        main_script = repo_root / "main_live_qc.py"
        if not main_script.is_file():
            self.log_panel.append_log(
                "HI-MC", "ERROR",
                f"main_live_qc.py not found at {main_script}. Deploy the pipeline "
                f"scripts alongside the GUI.")
            return

        ui = self._build_ui_dict(repo_root)
        config = build_run_config(ui)

        if self._pipeline_runner is None:
            self._pipeline_runner = PipelineRunner(self)
            r = self._pipeline_runner
            r.log_line.connect(self._on_log)
            r.region_total.connect(self._on_region_total)
            r.region_order.connect(self._on_region_order)
            r.region_started.connect(self._on_region_started)
            r.tile_progress.connect(self._on_tile_progress)
            r.region_done.connect(self._on_region_done)
            r.region_failed.connect(self._on_region_failed)
            r.region_skipped.connect(self._on_region_skipped)
            r.stage_z.connect(self._on_stage_z)
            r.finished.connect(self._on_finished)
            r.error.connect(self._on_error)
            r.beam_arc.connect(self._on_beam_arc)

        if self.engine is not None:
            try:
                self.engine.pause_hardware()
            except Exception:
                pass

        self._run_failed_regions = []
        self._user_requested_stop = False
        self._alert_sent_this_run = False
        self._z_alert_active = False
        self.lbl_stage_z.setVisible(False)

        config_path = str(repo_root / "msem_run_config.json")
        self.progress_bar.setValue(0)
        self.lbl_progress.setText("launching…")
        self.btn_start.setEnabled(False)
        self.btn_stop.setEnabled(True)
        self.log_panel.append_log("HI-MC", "INFO", f"Launching {main_script.name} (cwd={repo_root})")
        ok = self._pipeline_runner.start(
            config, workdir=str(repo_root), config_path=config_path,
            script="main_live_qc.py")
        if not ok:
            self._reset_after_run()

    def _on_stop(self):
        self._user_requested_stop = True
        if self._pipeline_runner is not None and self._pipeline_runner.is_running():
            self.log_panel.append_log("HI-MC", "INFO", "Stop requested (graceful)…")
            self._pipeline_runner.request_stop()
        else:
            self._reset_after_run()

    def _reset_after_run(self):
        if self.engine is not None:
            try:
                self.engine.resume_hardware()
            except Exception:
                pass
        self.btn_start.setEnabled(True)
        self.btn_stop.setEnabled(False)

    # -- pipeline signal handlers ------------------------------------------
    def _on_beam_arc(self, msg: str):
        # drive the top-level "Beam Arc" status indicator (the line itself is
        # already shown in the log via the normal log_line path)
        try:
            win = self.window()
            if hasattr(win, "status_panel"):
                win.status_panel.set_beam_arc(msg)
        except Exception:
            pass

    def _on_log(self, line: str):
        tag = "Pipeline"
        if line.startswith("[") and "]" in line:
            tag = line[1:line.index("]")]
        low = line.lower()
        if "error" in low or "traceback" in low or "failed" in low or "crashed" in low:
            level = "ERROR"
        elif "warn" in low or "skip" in low:
            level = "WARNING"
        else:
            level = "INFO"
        self.log_panel.append_log(tag, level, line)

        # capture output locations so "Open QC output" works
        marker = "QC outputs will be written to:"
        if marker in line:
            self._last_output_dir = line.split(marker, 1)[1].strip()
            self.btn_open.setEnabled(True)
        elif "[Output] Block folder:" in line and not self._last_output_dir:
            self._last_output_dir = line.split("Block folder:", 1)[1].strip()
            self.btn_open.setEnabled(True)

    def _on_region_total(self, n: int):
        self.lbl_progress.setText(f"0 / {n} region(s)")

    def _on_region_started(self, region_name: str, region_idx: int):
        self.progress_bar.setValue(0)
        self.lbl_progress.setText(f"Region {region_idx}: {region_name}")
        self.log_panel.append_log("HI-MC", "INFO", f"Region {region_idx} START: {region_name}")
        self._region_progress_set_status(region_name, "running")

    def _on_tile_progress(self, order: int, total: int):
        pct = int((order / total) * 100) if total > 0 else 0
        self.progress_bar.setValue(pct)
        self.lbl_progress.setText(f"tile {order}/{total}")

    def _on_region_done(self, region_name: str):
        self.log_panel.append_log("HI-MC", "SUCCESS", f"Region DONE: {region_name}")
        self._region_progress_set_status(region_name, "done")

    def _on_finished(self, code: int):
        if code == 0:
            self.progress_bar.setValue(100)
            self.lbl_progress.setText("done")
            self.log_panel.append_log("HI-MC", "SUCCESS", "Pipeline finished cleanly")
        else:
            self.lbl_progress.setText(f"exit {code}")
            self.log_panel.append_log("HI-MC", "ERROR", f"Pipeline exited with code {code}")
            if self._NOTIFY_ON_UNEXPECTED_EXIT and not self._user_requested_stop:
                self._maybe_send_failure_alert(
                    f"HI-MC: pipeline exited unexpectedly (code {code})",
                    f"The HI-MC pipeline exited with code {code} without a Stop being "
                    f"requested - this may mean a crash, hardware fault, or connection loss.\n\n"
                    f"Imaging JSON: {self.edit_json.text().strip()}"
                    f"\nOutput root: {self.edit_output.text().strip()}"
                    + (f"\nRegions failed so far: {', '.join(self._run_failed_regions)}"
                       if self._run_failed_regions else ""))

        # End-of-run summary: covers a run that finished (cleanly or not)
        # with SOME failures but never crossed the mid-run threshold above,
        # and a graceful Stop after failures already happened (which the
        # unexpected-exit branch above deliberately never alerts on). A
        # no-op if an alert already went out for this run (single-alert
        # guard in _maybe_send_failure_alert).
        if self._run_failed_regions:
            self._maybe_send_failure_alert(
                f"HI-MC: run finished with {len(self._run_failed_regions)} region failure(s)",
                f"The HI-MC run has finished (exit code {code}) with "
                f"{len(self._run_failed_regions)} region failure(s):\n"
                + "\n".join(f"  - {n}" for n in self._run_failed_regions)
                + f"\n\nImaging JSON: {self.edit_json.text().strip()}"
                + f"\nOutput root: {self.edit_output.text().strip()}")
        self._reset_after_run()

    def _on_error(self, msg: str):
        # QProcess error fires instead of finished -> resume hardware here too,
        # else the scope stays paused after a failed launch/crash.
        self._reset_after_run()
        self.log_panel.append_log("HI-MC", "ERROR", msg)
        if self._NOTIFY_ON_UNEXPECTED_EXIT and not self._user_requested_stop:
            self._maybe_send_failure_alert(
                "HI-MC: pipeline process error",
                f"The HI-MC pipeline process reported an error without a Stop being "
                f"requested:\n\n{msg}\n\n"
                f"Imaging JSON: {self.edit_json.text().strip()}"
                f"\nOutput root: {self.edit_output.text().strip()}")

    # -- misc ---------------------------------------------------------------
    def _open_output(self):
        if not self._last_output_dir:
            return
        path = self._last_output_dir
        # the QC results dir is <block>/<region>/LiveQC; open its parent if the
        # exact dir doesn't exist yet (e.g. only the block folder was captured)
        target = path if os.path.exists(path) else os.path.dirname(path)
        QDesktopServices.openUrl(QUrl.fromLocalFile(target))
