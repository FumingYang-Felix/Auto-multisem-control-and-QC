"""Configuration for standalone QC.

Two groups of keys:

  - Keys consumed by the SHARED modules we import. These deliberately keep
    the exact names live_qc_support / qc_algorithm / qc_overview already use
    (dsamp, image_suffix_globs, outside_roi_sat_threshold, overview_ds, ...)
    so a config dict from here can be handed to them unchanged, and so an
    operator who already knows the LIVE_QC_CONFIG knobs doesn't have to learn
    a second set of names for the same thing.
  - Keys only this package understands (tile_sampling, tissue_probe_beam,
    max_workers, ...). Prefixed conceptually but not by name, for the same
    "one vocabulary" reason.

Override with a JSON file: `--config my.json`, containing any subset of these
keys at the top level.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Dict, Optional

QC_CONFIG: Dict[str, Any] = {
    # ---- file discovery (names shared with live_qc_support) --------------
    "image_suffix_globs": ["*.bmp", "*.BMP"],
    "beam_index_regexes": [
        r"beam[_\- ]?(\d{1,3})",
        r"mfov[_\- ]?\d+[_\- ]?beam[_\- ]?(\d{1,3})",
        r"[_\- ]b(\d{1,3})(?:\D|$)",
        r"[_\- ](\d{1,3})(?:\.[^.]+)$",
    ],

    # ---- per-image scoring (qc_algorithm.compute_single_image_quality) ---
    "dsamp": 200,
    "sig_thresh": 0.01,
    "top_fraction": 0.50,
    "jitter_smooth_size": 8,
    "jitter_top_band_fraction": 0.05,
    "jitter_band_position": "top",

    # ---- per-MFOV aggregation (qc_algorithm.aggregate_mfov_quality) ------
    "aggregate_mode": "median",
    "outside_roi_sat_threshold": 30.0,
    "outside_roi_std_threshold": 3.0,
    "jitter_warn_threshold": 1.5,
    "beam_outlier_mad_sigma": 3.0,
    "beam_outlier_min_beams": 5,

    # ---- quality tiers (qc_algorithm.quality_tier) -----------------------
    "low_quality_threshold": 10.0,
    "high_quality_threshold": 30.0,

    # ---- beam selection for the FULL scoring pass ------------------------
    # "ALL" = every beam found on disk for the tile (up to all 91). This is
    # the whole point of moving QC off the acquisition PC: we can now afford
    # the ~91 image reads per tile that catch a single beam going bad, which
    # a 5-beam sample could miss entirely. live_qc_support.choose_beams_for_tile
    # understands this key; the other modes there still work if you want them.
    "beam_selection_mode": "ALL",
    "include_beam1": True,
    "num_extra_beams": 4,
    "fixed_beams": [1, 10, 28, 55, 82],
    "random_seed": 20260316,

    # ---- pass A: the map pass (every mFoV) --------------------------------
    # Every mFoV gets scored with this many beams. This is what fills the
    # interactive dashboard's focus / stig / status HEX MAPS - one hex per
    # mFoV - so it has to cover the whole section, not a sample. 5 beams
    # matches what the acquisition side's live QC used to sample per tile, so
    # a hex here means what a hex there meant.
    #
    # An earlier version read only ONE beam per mFoV and used it purely to
    # classify tissue-vs-blank, throwing the quality number away. That made
    # the hex maps impossible to draw and was the reason the standalone
    # dashboard ended up a static scatter plot instead of the real thing.
    "map_beams_per_mfov": 5,
    "map_include_beam1": True,

    # ---- tile sampling (this package only) -------------------------------
    # All 91 beams is affordable per tile; all 91 beams on all ~236 tiles of
    # a region is ~75 GB of network reads. So we score every beam of only a
    # few representative tiles:
    #   "first_mid_last_tissue" - first / middle / last TISSUE-containing tile
    #                             (needs the map pass below)
    #   "every_n"               - every Nth tile, tissue or not
    #   "all"                   - every tile (the expensive, complete option)
    "tile_sampling": "first_mid_last_tissue",
    "tile_sampling_every_n": 25,

    # THE MAP PASS. The key name is historical: this began as a one-beam
    # tissue-vs-blank probe, and grew into the pass that produces a result
    # for EVERY mFoV (map_beams_per_mfov beams each). score.results_by_tile
    # is built from it, so it is what fills:
    #
    #   - the overview overlay's per-mFoV quality numbers and colours
    #   - IMQA (mean +/- std over every non-blank mFoV)
    #   - the scanfault and jitter columns
    #   - the retake lasso's per-tile data
    #   - tissue-based mFoV selection for the all-beam pass
    #
    # Measured on a 10-mFoV section: off, per-mFoV results drop from 10 to
    # 3 and scan-fault checks from 10 to 0. It is therefore NOT exposed as
    # a GUI opt-out, only as a command-line troubleshooting flag. To make
    # it cheaper, lower map_beams_per_mfov rather than turning it off - the
    # columns all still fill, just from fewer beams per mFoV.
    #
    # It still does the original job too: the acquisition side's
    # is_blank_outside_roi is a hardcoded placeholder in lightweight mode
    # (handoff section 3), so blank-vs-tissue is decided here.
    "tissue_probe_enabled": True,
    "tissue_probe_beam": 1,

    # ---- interactive per-section dashboard -------------------------------
    # Generated by live_qc_support._write_dashboard_html - the same function
    # the acquisition side's live QC used, so the panels, hex maps, pan/zoom,
    # tooltips and the retake-selection lasso are the real ones rather than a
    # reimplementation. Needs the map pass above to have covered every mFoV.
    "dashboard_enabled": True,

    # ---- report auto-refresh ---------------------------------------------
    # Seconds between automatic reloads of an open batch report, so a page
    # left open during a realtime run keeps showing current results instead
    # of a snapshot from whenever it was opened. 0 disables it.
    #
    # A reload rather than an in-place update because these pages are opened
    # over file://, where fetch()/XHR to local files is blocked - there is no
    # way to pull fresh data without re-reading the page itself. Scroll
    # position is preserved, and the reload holds off while a field is being
    # typed into.
    "report_auto_refresh_s": 30,

    # ---- section overview image (qc_overview) ----------------------------
    # ALWAYS ON. The section overview is panel ⓪ of the dashboard - the
    # thing that makes the hex maps interpretable, because it puts the actual
    # image next to them - so a dashboard without it answers much less. It is
    # not exposed as a GUI opt-out for that reason.
    #
    # It does read every beam of every mFoV, so cost scales with section
    # size: ~20s for a 9-mFoV section, ~15min for a 231-mFoV one. Turn the
    # downsample factor up to trade detail for time on big sections.
    "region_overview_enabled": True,
    # Per-MFOV overview PNGs (one per mfov folder) stay off: they would write
    # into acquisition's own tile folders, and the dashboard does not use
    # them - it places beams directly at their recorded coordinates.
    "mfov_overview_enabled": False,
    # Downsample factor for the overview. Higher = faster and smaller, less
    # detail. 16 gives ~125x109 px per beam image.
    "overview_ds": 16,
    # Caps on the STITCHED CANVAS, not on the downsample factor. overview_ds
    # is a per-image divisor, so canvas size scales with the section: a
    # 9-mFoV section is fine at ds=16 while a 397-mFoV one reaches 671 Mpx,
    # which Chrome refuses to decode (measured: 168 Mpx loads, 671 is
    # "broken"). When these would be exceeded, ds is raised for that section
    # only - see overview_plan.py. 0 disables a cap.
    # Build the overview from the per-beam thumbnails rather than the
    # full-resolution images. Same output resolution, ~9x faster,
    # ~20x less read off the share - see overview_fast.py. Falls back
    # automatically if a section has no thumbnails.
    "overview_use_thumbnails": True,
    "overview_max_megapixels": 64,
    "overview_max_dim_px": 12000,
    "overview_padding": 80,
    "overview_beam_layout": "auto",
    "overview_mfov_name": "overview_mfov.png",
    "overview_region_name": "standalone_qc_region_overview.png",

    # ---- mFoV overlap / gap check (mfov_overlap.py) ----------------------
    # Rasterises every beam rectangle of the section at its true stage
    # coordinates and asks two geometric questions: is any area enclosed by
    # imaged mFoVs left uncovered (a gap), and do adjacent mFoVs still share
    # a strip (enough to stitch). Cheap - ~0.15s per section, no image
    # decoding, it only reads image_coordinates.txt and one image header.
    "overlap_check_enabled": True,

    # ---- imaging JSON (retake selection) ---------------------------------
    # The Zeiss imaging JSON a batch was acquired from. The dashboard's
    # Retake selection panel clones the original region's XML out of it to
    # build a "<Section>_retake_<N>" sub-region; with no JSON that panel
    # cannot build anything.
    #
    # None = find it automatically by matching a candidate's region names
    # against the batch manifest's (see imaging_json.py). Set a path to pin
    # one file. NOT matched by filename: on this wafer the batch called
    # w10h31_dye_R3_8NM_... came from w10h31_dye_border.json, while
    # w10h31_dye_R3.json shares none of its regions.
    "imaging_json_path": None,
    "imaging_json_search_enabled": True,

    # ---- portable links --------------------------------------------------
    # Rewrite this machine's mapped-drive paths in the generated HTML to UNC
    # (Z:\... -> \nwl-vast...\multisem\...), so a dashboard opened on
    # another computer can still click through to the beam images. The
    # acquisition PC mounts the same share as V:, so drive-letter links are
    # dead there. Local paths are never rewritten. See portable_paths.py.
    "portable_unc_paths": True,

    # ---- execution -------------------------------------------------------
    # I/O bound (decoding ~3.5 MB BMPs off a network share), so more workers
    # than cores is fine and usually better.
    "max_workers": 16,
    # Subfolder created next to LiveQC/ inside each region folder. Keeping
    # our output under its own name makes it unambiguous which system owns
    # which files (handoff section 8).
    "output_subdir": "StandaloneQC",
}

# Version of the per-section OUTPUT SCHEMA - what a summary.json and a
# dashboard are expected to contain. It is folded into the policy fingerprint,
# so bumping it makes every previously-analyzed section read as not-done and
# therefore eligible for "Analyze All Pending" again.
#
# BUMP THIS whenever analysis starts producing a field or panel that older
# results lack. Without it, adding a check leaves a batch with mixed-vintage
# sections that the ledger insists are all finished: the batch report then
# shows the new column as "-" for every section analyzed before the change,
# and re-running does nothing because the config never changed. That is
# exactly how the mFoV-overlap column came up empty on 23 of 24 sections.
#
#   1 - quality + jitter + scan faults + section facts
#   2 - adds mFoV overlap (score.overlap) and the focus/stigmation plane
#       panels in the section dashboard
#   3 - adds per-FSP move/B2F/AF/AS timings and replaces the flat plane maps
#       with interactive 3D scatters
#   4 - OL plotted in microns of focus rather than raw OL microamps
#   5 - dashboards carry the imaging JSON, so the Retake selection panel can
#       build partial-retake sub-regions
ANALYSIS_VERSION = 5

# Keys that actually change the numbers in a result. The ledger fingerprints
# these (not the whole config) so that re-running after changing max_workers
# or an overview flag does NOT invalidate previous scoring, while changing
# dsamp or the sampling policy does.
_POLICY_KEYS = (
    "dsamp", "sig_thresh", "top_fraction",
    "jitter_smooth_size", "jitter_top_band_fraction", "jitter_band_position",
    "aggregate_mode", "outside_roi_sat_threshold", "outside_roi_std_threshold",
    "jitter_warn_threshold", "beam_outlier_mad_sigma", "beam_outlier_min_beams",
    "beam_selection_mode", "include_beam1", "num_extra_beams", "fixed_beams",
    "random_seed", "tile_sampling", "tile_sampling_every_n",
    "tissue_probe_enabled", "tissue_probe_beam",
    "map_beams_per_mfov", "map_include_beam1",
    # Not a config knob, but it belongs in the fingerprint for the same
    # reason the knobs do: it decides whether an existing result is still
    # what this code would produce.
    "overlap_check_enabled",
)


def load_config(path: Optional[str | Path] = None, **overrides: Any) -> Dict[str, Any]:
    """QC_CONFIG, optionally overridden by a JSON file and then by keyword
    arguments (CLI flags). Unknown keys are kept, not rejected - the shared
    modules read config with .get() and tolerate extras, and rejecting
    unknown keys would just make a future shared-module knob unusable from
    here until this file caught up."""
    cfg = dict(QC_CONFIG)
    if path:
        p = Path(path)
        # utf-8-sig, not utf-8: both PowerShell's `Out-File -Encoding utf8`
        # and Notepad write a BOM, so a hand-made config file would otherwise
        # fail with an unhelpful "Unexpected UTF-8 BOM". utf-8-sig reads
        # BOM-less files identically, so this is strictly more tolerant.
        data = json.loads(p.read_text(encoding="utf-8-sig"))
        if not isinstance(data, dict):
            raise ValueError(f"{p}: expected a JSON object at the top level")
        cfg.update(data)
    for k, v in overrides.items():
        if v is not None:
            cfg[k] = v
    return cfg


def policy_fingerprint(cfg: Dict[str, Any]) -> str:
    """Short stable hash of the result-affecting config, for the ledger."""
    keyed = {k: cfg.get(k) for k in _POLICY_KEYS}
    keyed["__analysis_version__"] = ANALYSIS_VERSION
    payload = json.dumps(keyed, sort_keys=True, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:12]
