"""The two-pass scoring policy.

Pass 1 (THE MAP PASS) - score `map_beams_per_mfov` beams on EVERY mFoV.
This produces a result for every mFoV in the section, and `results_by_tile`
is built from it, so it is what fills the overview overlay's per-mFoV
quality numbers, IMQA, the scanfault and jitter columns, the retake lasso's
tile data, and the tissue-vs-blank classification used to choose pass 2's
mFoVs. At 5 of 91 beams it costs about 1/18 of a full pass.

It began as a one-beam tissue probe - hence `tissue_probe_enabled` - and the
blank classification is still one of its jobs: `is_blank_outside_roi` in
live_qc_results.csv is a hardcoded placeholder in lightweight mode (handoff
section 3), so it has to be decided here. But skipping this pass now costs
most of the report, not just tissue selection, which is why it has no GUI
opt-out. To make it cheaper, lower map_beams_per_mfov.

Pass 2 (full scoring) - score ALL beams found on disk, on a few
representative tiles. This is the point of having a dedicated machine: a
single beam going bad while the other 90 look fine is invisible to a 5-beam
sample, and `detect_outlier_beams` needs real beam coverage to call it.

Why not all beams on all tiles: at 2000x1748 8-bit, one beam image is ~3.5 MB,
so a 91-beam tile is ~320 MB and a 236-tile region is ~75 GB of network reads.
Pass 1 + pass 2 on three tiles is under 2 GB for the same region.

All scoring goes through the shared, acquisition-side-owned qc_algorithm -
never a reimplementation - so a number produced here means exactly what the
same number meant when live QC produced it.
"""
from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

import numpy as np

from live_qc_support import build_beam_map, choose_beams_for_tile
from qc_algorithm import (
    ImageQCResult,
    aggregate_mfov_quality,
    compute_single_image_quality,
    detect_outlier_beams,
)

from . import focus_planes, mfov_overlap, scan_checks, section_facts
from .discover import RegionCheck, RegionRef, verify_region_files
from .layout import (
    TileRef,
    detect_live_qc_mode,
    read_region_layout,
    read_region_timing,
)

# Acquisition should no longer be waiting on its live-QC subprocesses now that
# scoring lives here; anything above this is worth surfacing back to the
# acquisition side rather than leaving buried in a JSON file nobody opens.
QC_CATCHUP_WARN_S = 30.0

ProgressFn = Optional[Callable[[int, int, str], None]]


@dataclass
class RegionScore:
    """Everything one region's QC pass produced."""
    region_name: str
    region_folder: str
    block_label: str          # the Batch folder's name
    region_dir: Path
    run_label: str = ""       # the Run folder above the batch, e.g. "Try_1"
    sample_id: Optional[str] = None   # the wafer, e.g. "w10h31"
    layout_source: str = "none"
    tiles: List[TileRef] = field(default_factory=list)
    # Pass A: one result per mFoV, a few beams each (the hex maps).
    probe_by_tile: Dict[int, Dict[str, Any]] = field(default_factory=dict)
    tissue_tiles: List[int] = field(default_factory=list)
    # Pass B: all beams, on a few representative mFoVs.
    scored: List[Dict[str, Any]] = field(default_factory=list)
    # Pass A merged with pass B on top - what the dashboard's hex maps read.
    results_by_tile: Dict[int, Dict[str, Any]] = field(default_factory=dict)
    # beam index -> its quality across the all-beam mFoVs (dashboard panel ④).
    beam_quality_values: Dict[int, List[float]] = field(default_factory=dict)
    # tile_idx -> scan-integrity check on one sFoV of that mFoV.
    scan_checks: Dict[int, Dict[str, Any]] = field(default_factory=dict)
    # Section-wide mFoV coverage: gaps between mFoVs, adjacent-pair overlap.
    overlap: Dict[str, Any] = field(default_factory=dict)
    # One entry per fitted quantity (OL / StigX / StigY): plane coefficients,
    # RMS, max-min, and for OL the slope in um/mm and the two angles. The
    # plots themselves are drawn at render time from the raw points; these
    # NUMBERS are cached in the summary so the batch report can show a
    # per-section focus-plane slope without re-reading every metadata.txt on
    # each report rebuild.
    plane_fits: List[Dict[str, Any]] = field(default_factory=list)
    selection_reason: str = ""
    beam_summary: List[Dict[str, Any]] = field(default_factory=list)
    check: Optional[RegionCheck] = None
    overview: Optional[Dict[str, Any]] = None
    # The acquisition side's own timing for this region, passed through.
    acq_timing: Dict[str, Any] = field(default_factory=dict)
    # "lightweight" / "full" / "unknown" - which mode acquisition's own live
    # QC ran in, inferred from its CSV. See layout.detect_live_qc_mode.
    live_qc_mode: str = "unknown"
    # Non-quality facts from acquisition's own artefacts: file-presence
    # checks, duration, focus support points, AF/AS success. Gathered here
    # (where the folders are already being walked) and cached into the
    # summary JSON, so report rebuilds stay instant. See section_facts.py.
    facts: Dict[str, Any] = field(default_factory=dict)
    # Informational observations, as opposed to `errors` (things that look
    # wrong). Kept separate so the report can show both without a warning
    # banner crying wolf over expected behavior.
    notes: List[str] = field(default_factory=list)
    n_images_read: int = 0
    elapsed_s: float = 0.0
    errors: List[str] = field(default_factory=list)

    @property
    def n_tiles(self) -> int:
        return len(self.tiles)

    @property
    def n_blank(self) -> int:
        return sum(
            1 for r in self.probe_by_tile.values()
            if r.get("is_blank_outside_roi")
        )


def _no_progress(done: int, total: int, label: str) -> None:
    return None


def _beam_paths(tile: TileRef, cfg: Dict[str, Any]) -> Dict[int, Path]:
    """beam index -> image path for one tile folder.

    prefix=None: on a live run the prefix came from configure_output_for_tile,
    which we have no access to. build_beam_map's regexes resolve the real
    filenames anyway - `mfov42_<timestamp>__001.bmp` matches its
    `[_\\- ](\\d{1,3})(?:\\.[^.]+)$` rule on the trailing `_001.bmp`, and the
    13-digit timestamp can't match it because that rule anchors to the
    extension. Its sorted-order fallback covers anything unexpected.
    """
    return build_beam_map(tile.tile_folder, cfg, None)


def _aggregate_one(
    tile: TileRef,
    image_results: Sequence[ImageQCResult],
    selected_beams: Sequence[int],
    cfg: Dict[str, Any],
) -> Dict[str, Any]:
    """qc_algorithm aggregation for one tile + our commanded-value passthrough."""
    mfov = aggregate_mfov_quality(
        tile_idx=tile.tile_idx,
        x_um=tile.x_um,
        y_um=tile.y_um,
        tile_folder=tile.tile_folder,
        image_results=image_results,
        selected_beams=selected_beams,
        aggregate_mode=str(cfg.get("aggregate_mode", "median")),
        outside_roi_sat_threshold=float(cfg.get("outside_roi_sat_threshold", 30.0)),
        outside_roi_std_threshold=float(cfg.get("outside_roi_std_threshold", 3.0)),
        jitter_warn_threshold=float(cfg.get("jitter_warn_threshold", 1.5)),
        beam_outlier_mad_sigma=float(cfg.get("beam_outlier_mad_sigma", 3.0)),
        beam_outlier_min_beams=int(cfg.get("beam_outlier_min_beams", 5)),
    )
    result = mfov.to_dict()
    # Carried through, not derived: these are what the acquisition side
    # actually commanded for this tile (or, with the metadata layout source,
    # what the hardware read back). qc_algorithm has no notion of them.
    result["ol_pred"] = tile.ol_pred
    result["stig_x_pred"] = tile.stig_x_pred
    result["stig_y_pred"] = tile.stig_y_pred
    return result


def _score_image(path: Path, beam: int, cfg: Dict[str, Any]) -> ImageQCResult:
    return compute_single_image_quality(
        path,
        beam_index=beam,
        dsamp=int(cfg.get("dsamp", 200)),
        sig_thresh=float(cfg.get("sig_thresh", 0.01)),
        top_fraction=float(cfg.get("top_fraction", 0.50)),
        jitter_smooth_size=int(cfg.get("jitter_smooth_size", 8)),
        jitter_band_fraction=float(cfg.get("jitter_top_band_fraction", 0.05)),
        jitter_band_position=str(cfg.get("jitter_band_position", "top")),
    )


# ---------------------------------------------------------------------------
# pass 1: the map pass (every mFoV)
# ---------------------------------------------------------------------------
def _map_beams(beam_map: Dict[int, Path], cfg: Dict[str, Any]) -> List[int]:
    """Which beams the map pass reads from one mFoV.

    Evenly spread across the beam numbering rather than random, so every
    mFoV's hex is built from the SAME beams - a hex map where each tile
    sampled a different random handful would show beam-to-beam variation as
    if it were spatial variation in focus.
    """
    available = sorted(beam_map)
    if not available:
        return []
    want = max(1, int(cfg.get("map_beams_per_mfov", 5)))
    if want >= len(available):
        return available
    picked: List[int] = []
    if bool(cfg.get("map_include_beam1", True)) and 1 in beam_map:
        picked.append(1)
        want -= 1
    rest = [b for b in available if b not in picked]
    if want > 0 and rest:
        step = len(rest) / float(want)
        picked += [rest[min(len(rest) - 1, int(i * step))] for i in range(want)]
    return sorted(set(picked))



def _map_pass_is_redundant(cfg: Dict[str, Any]) -> bool:
    """True when the all-beam pass will overwrite every map-pass result.

    The map pass exists to give the hex maps a reading for mFoVs the
    all-beam pass will not visit. When tile_sampling is "all" and
    beam_selection_mode is "ALL" there are no such mFoVs, so its only
    surviving output is the scan check.
    """
    return (str(cfg.get("tile_sampling", "")).lower() == "all"
            and str(cfg.get("beam_selection_mode", "")).upper() == "ALL")


def map_region(
    tiles: Sequence[TileRef],
    cfg: Dict[str, Any],
    on_progress: ProgressFn = None,
) -> Tuple[Dict[int, Dict[str, Any]], int, Dict[int, Dict[str, Any]]]:
    """Pass A - score EVERY mFoV with a few beams each.

    This is what makes the interactive dashboard possible: its focus, stig
    and status panels draw one hex per mFoV, so they need a result for every
    mFoV in the section, not for a sample of three. Tissue-vs-blank falls out
    of the same results (aggregate_mfov_quality's own saturation/flatness
    criteria), so no separate probe pass is needed.

    Returns ({tile_idx: mfov result}, n_images_read,
    {tile_idx: scan-check result}).
    """
    progress = on_progress or _no_progress
    workers = max(1, int(cfg.get("max_workers", 16)))
    out: Dict[int, Dict[str, Any]] = {}
    n_images = 0

    checks: Dict[int, Dict[str, Any]] = {}
    do_checks = bool(cfg.get("scan_checks_enabled", True))

    def _one(tile: TileRef):
        beam_map = _beam_paths(tile, cfg)
        beams = _map_beams(beam_map, cfg)
        results = [
            _score_image(beam_map[b], b, cfg) for b in beams if b in beam_map
        ]
        # Scan-integrity checks on ONE sFoV per mFoV, per the operator's
        # spec. The lowest-numbered beam, which this pass has already read -
        # so these cost no extra network I/O.
        chk = None
        if do_checks and beam_map:
            chk = scan_checks.check_image(beam_map[min(beam_map)], cfg).as_dict()
        return tile, results, beams, chk

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(_one, t): t for t in tiles}
        for i, fut in enumerate(as_completed(futures), start=1):
            tile, results, beams, chk = fut.result()
            n_images += len(results)
            # A tile with no images still gets a row - a hole in the section
            # must be visible on the hex map, not silently absent.
            out[tile.tile_idx] = _aggregate_one(tile, results, beams, cfg)
            if chk is not None:
                checks[tile.tile_idx] = chk
            progress(i, len(tiles), f"map mFoV {tile.tile_idx}")

    return out, n_images, checks


# Kept as an alias: older configs and callers say "probe".
probe_region = map_region


# ---------------------------------------------------------------------------
# tile selection
# ---------------------------------------------------------------------------
def select_tiles(
    tiles: Sequence[TileRef],
    probe_by_tile: Dict[int, Dict[str, Any]],
    cfg: Dict[str, Any],
) -> Tuple[List[TileRef], str]:
    """Which tiles get the full all-beams pass. Returns (tiles, reason)."""
    mode = str(cfg.get("tile_sampling", "first_mid_last_tissue")).lower()
    ordered = sorted(tiles, key=lambda t: t.tile_idx)

    if mode == "all":
        return list(ordered), "every mFoV (tile_sampling=all)"

    if mode == "every_n":
        n = max(1, int(cfg.get("tile_sampling_every_n", 25)))
        picked = list(ordered[::n])
        return picked, f"every {n}th mFoV ({len(picked)} of {len(ordered)})"

    # first_mid_last_tissue (default)
    if not probe_by_tile:
        # No probe ran, so we have no tissue information. Fall back to
        # position rather than guessing - first/middle/last of ALL tiles is
        # still a defensible sample, and saying so is better than silently
        # scoring blank corners as though they were representative.
        candidates = list(ordered)
        basis = "all mFoVs (map pass skipped)"
    else:
        candidates = [
            t for t in ordered
            if not probe_by_tile.get(t.tile_idx, {}).get("is_blank_outside_roi", False)
            and probe_by_tile.get(t.tile_idx, {}).get("status_category") != "error"
        ]
        basis = f"tissue-containing mFoVs ({len(candidates)} of {len(ordered)})"
        if not candidates:
            candidates = list(ordered)
            basis = f"all mFoVs - probe found no tissue in any of {len(ordered)}"

    if len(candidates) <= 3:
        return candidates, f"first/middle/last of {basis} - all {len(candidates)} taken"

    picked = [candidates[0], candidates[len(candidates) // 2], candidates[-1]]
    idxs = ", ".join(str(t.tile_idx) for t in picked)
    return picked, f"first/middle/last of {basis} - mFoVs {idxs}"


# ---------------------------------------------------------------------------
# pass 2: full scoring
# ---------------------------------------------------------------------------
def score_tiles_all_beams(
    tiles: Sequence[TileRef],
    cfg: Dict[str, Any],
    on_progress: ProgressFn = None,
) -> Tuple[List[Dict[str, Any]], int]:
    """Score every selected beam of every given tile.

    Parallelized across (tile, beam) pairs rather than across tiles: with
    only a handful of tiles but ~91 images each, per-tile threading would
    leave most workers idle. This is I/O bound on BMP decode off a share, so
    more workers than cores is normal.
    """
    progress = on_progress or _no_progress
    workers = max(1, int(cfg.get("max_workers", 16)))

    jobs: List[Tuple[TileRef, int, Path]] = []
    beams_by_tile: Dict[int, List[int]] = {}
    for tile in tiles:
        beam_map = _beam_paths(tile, cfg)
        selected = choose_beams_for_tile(beam_map, cfg, tile.tile_idx)
        beams_by_tile[tile.tile_idx] = list(selected)
        for beam in selected:
            path = beam_map.get(int(beam))
            if path is not None:
                jobs.append((tile, int(beam), path))

    per_tile: Dict[int, List[ImageQCResult]] = {t.tile_idx: [] for t in tiles}
    total = len(jobs)
    if total:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {
                pool.submit(_score_image, path, beam, cfg): (tile, beam)
                for tile, beam, path in jobs
            }
            for i, fut in enumerate(as_completed(futures), start=1):
                tile, beam = futures[fut]
                per_tile[tile.tile_idx].append(fut.result())
                progress(i, total, f"mFoV {tile.tile_idx} beam {beam}")

    results: List[Dict[str, Any]] = []
    for tile in tiles:
        images = sorted(
            per_tile[tile.tile_idx],
            key=lambda r: (r.beam_index if r.beam_index is not None else 0),
        )
        results.append(_aggregate_one(tile, images, beams_by_tile[tile.tile_idx], cfg))
    return results, total


# ---------------------------------------------------------------------------
# cross-tile beam summary
# ---------------------------------------------------------------------------
def summarize_beams(
    scored: Sequence[Dict[str, Any]],
    cfg: Dict[str, Any],
) -> List[Dict[str, Any]]:
    """Per-beam quality across every fully-scored tile, flagging beams whose
    median is an outlier vs. the other beams.

    This is the analysis the deleted "Image quality per beam" panel used to
    provide, and it is the main thing all-91-beam coverage buys: a beam that
    is consistently bad across several tiles is a hardware problem, not tile
    noise, and it is exactly what a 5-beam live sample could never see.

    Reuses qc_algorithm.detect_outlier_beams by feeding it one synthetic
    per-beam record carrying that beam's MEDIAN quality across tiles - the
    same median+MAD convention, applied across tiles instead of within one.
    """
    by_beam: Dict[int, List[float]] = {}
    for tile_result in scored:
        for img in tile_result.get("image_results", []):
            beam = img.get("beam_index")
            if beam is None or img.get("status") != "ok" or img.get("acquisition_fail"):
                continue
            by_beam.setdefault(int(beam), []).append(float(img.get("quality", 0.0)))

    if not by_beam:
        return []

    medians = {
        beam: float(np.median(np.asarray(vals, dtype=np.float64)))
        for beam, vals in by_beam.items()
    }
    synthetic = [
        ImageQCResult(
            image_path=f"beam_{beam}", beam_index=beam, quality=med,
            range_val=0.0, horz_qual=0.0, vert_qual=0.0, std_high=0.0,
            std_all=0.0, percent_saturation=0.0, acquisition_fail=False,
            used_signal_points=0, status="ok",
        )
        for beam, med in sorted(medians.items())
    ]
    outliers = {
        int(o["beam_index"]): o
        for o in detect_outlier_beams(
            synthetic,
            mad_sigma=float(cfg.get("beam_outlier_mad_sigma", 3.0)),
            min_beams=int(cfg.get("beam_outlier_min_beams", 5)),
        )
    }

    summary: List[Dict[str, Any]] = []
    for beam in sorted(by_beam):
        vals = np.asarray(by_beam[beam], dtype=np.float64)
        hit = outliers.get(beam)
        summary.append({
            "beam_index": beam,
            "n_tiles": int(vals.size),
            "quality_median": float(np.median(vals)),
            "quality_min": float(np.min(vals)),
            "quality_max": float(np.max(vals)),
            "is_outlier": hit is not None,
            "deviation": float(hit["deviation"]) if hit else 0.0,
            "group_median": float(hit["group_median"]) if hit else float("nan"),
        })
    return summary


# ---------------------------------------------------------------------------
# orchestration
# ---------------------------------------------------------------------------
def score_region(
    region: RegionRef,
    cfg: Dict[str, Any],
    on_progress: ProgressFn = None,
    skip_verify: bool = False,
) -> RegionScore:
    """Full QC pass over one finished region. Never raises for data problems -
    they land in RegionScore.errors so a batch loop keeps going."""
    progress = on_progress or _no_progress
    t0 = time.perf_counter()
    out = RegionScore(
        region_name=region.name,
        region_folder=region.folder,
        block_label=region.block_label,
        region_dir=region.region_dir,
        run_label=region.run_label,
        sample_id=region.sample_id,
    )

    if not skip_verify:
        out.check = verify_region_files(region.region_dir)
        for w in out.check.warnings:
            out.errors.append(f"verify: {w}")

    out.acq_timing = read_region_timing(region.region_dir)
    out.live_qc_mode = detect_live_qc_mode(region.region_dir)
    try:
        # load_or_collect, not collect: a prior --scan may already have paid
        # the ~2.3s of directory listings, and a section's files do not
        # change once it has finished imaging.
        out.facts = section_facts.load_or_collect(
            region.region_dir, region.name,
            section_facts.parse_timing_summary(region.block_dir),
            str(cfg.get("output_subdir", "StandaloneQC")),
        )
    except Exception as exc:
        out.errors.append(f"section facts unavailable: {exc}")
    catchup = out.acq_timing.get("qc_catchup_s")
    if isinstance(catchup, (int, float)) and float(catchup) > QC_CATCHUP_WARN_S:
        # Only a REGRESSION if the run was already in lightweight mode. A
        # pre-lightweight run spending minutes here is the old baseline -
        # exactly what moving scoring to this machine is meant to remove -
        # so flagging it as something to report back would be a false alarm.
        if out.live_qc_mode == "lightweight":
            out.errors.append(
                f"acquisition waited {float(catchup):.0f}s for its own live-QC "
                f"subprocesses to drain (qc_catchup_s) despite lightweight "
                f"mode, where it should be near zero - worth reporting back "
                f"to the acquisition side."
            )
        else:
            out.notes.append(
                f"acquisition waited {float(catchup):.0f}s for its own live-QC "
                f"subprocesses to drain (qc_catchup_s), as expected for a run "
                f"with live QC in {out.live_qc_mode} mode - this is the "
                f"baseline that moving scoring here removes, not a regression."
            )

    tiles, source = read_region_layout(region.region_dir)
    out.tiles = tiles
    out.layout_source = source
    if not tiles:
        out.errors.append(
            f"no tile layout recoverable from {region.region_dir} "
            f"(no usable LiveQC/live_qc_results.csv and no mfov*/metadata.txt)"
        )
        out.elapsed_s = time.perf_counter() - t0
        return out

    n_read = 0
    if bool(cfg.get("tissue_probe_enabled", True)):
        map_cfg = cfg
        if _map_pass_is_redundant(cfg):
            # Every mFoV is about to be re-read at all 91 beams, and the
            # all-beam result overwrites the map one, so four of the map
            # pass's five beams are read, scored and thrown away: 1,596
            # images and about 5.6 GB per section off the share, for
            # nothing. One beam is still read because the scanfault column
            # comes from this pass and nothing else produces it.
            map_cfg = dict(cfg, map_beams_per_mfov=1)
        progress(0, len(tiles), "map pass")
        out.probe_by_tile, mapped, out.scan_checks = map_region(
            tiles, map_cfg, on_progress
        )
        n_read += mapped

    selected, reason = select_tiles(tiles, out.probe_by_tile, cfg)
    out.selection_reason = reason
    progress(0, len(selected), f"all-beam pass: {reason}")

    out.scored, full_read = score_tiles_all_beams(selected, cfg, on_progress)
    n_read += full_read
    out.beam_summary = summarize_beams(out.scored, cfg)

    # One result per mFoV for the dashboard's hex maps: the map pass covers
    # every mFoV, and the all-beam pass overwrites the few it re-scored -
    # those readings are strictly better (91 beams vs. 5), so they win.
    out.results_by_tile = dict(out.probe_by_tile)
    for r in out.scored:
        idx = r.get("tile_idx")
        if idx is not None:
            out.results_by_tile[int(idx)] = r

    # Tissue/blank from the BEST reading available for each mFoV, not from
    # the map pass. It used to come from probe_by_tile, which on a run that
    # scored every mFoV at 91 beams meant the 5-beam guess outranked the
    # 91-beam measurement that had already replaced it everywhere else.
    out.tissue_tiles = [
        idx for idx, r in sorted(out.results_by_tile.items())
        if not r.get("is_blank_outside_roi")
    ]

    # Per-beam quality lists, keyed by beam - panel ④ of the dashboard. The
    # acquisition side stopped producing this when scoring moved here, so
    # this is data the live dashboard no longer had.
    beam_values: Dict[int, List[float]] = {}
    for tile_result in out.scored:
        for img in tile_result.get("image_results", []):
            beam = img.get("beam_index")
            if beam is None or img.get("status") != "ok" or img.get("acquisition_fail"):
                continue
            beam_values.setdefault(int(beam), []).append(float(img.get("quality", 0.0)))
    out.beam_quality_values = beam_values

    # mFoV coverage from image_coordinates.txt - geometry only, no image
    # reads, about 0.15s for a section.
    if bool(cfg.get("overlap_check_enabled", True)):
        try:
            out.overlap = mfov_overlap.analyze_section(
                region.region_dir, cfg
            ).as_dict()
        except Exception as exc:
            out.errors.append(f"overlap check failed: {exc}")

    # Focus / stigmation plane fits. Also geometry-only (metadata.txt and the
    # LiveQC CSV), no image reads.
    try:
        fits, _fid = focus_planes.analyze_section(
            region.region_dir, cfg, region.sample_id
        )
        out.plane_fits = [f.as_dict() for f in fits]
    except Exception as exc:
        out.errors.append(f"plane fit failed: {exc}")

    out.n_images_read = n_read
    out.elapsed_s = time.perf_counter() - t0
    return out
