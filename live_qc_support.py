from __future__ import annotations

import csv
import html as _html
import json
import math
import os
import queue
import random
import re
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import numpy as np

from qc_algorithm import aggregate_mfov_quality, compute_single_image_quality, dumps_json, quality_tier

# OL-current-to-focus conversion for DISPLAY only (the live viewer's OL panel
# shows microns, a physically meaningful unit, instead of the raw commanded
# uA) - the underlying result/CSV/JSON values stay in uA (what was actually
# sent to hardware). Kept in sync by comment only with focus_map.py's
# _OL_UA_PER_UM (same empirical fit, see calibrate_ua_um.py /
# ua_um_calibration_fit.json) - this module deliberately never imports
# focus_map (comtypes/hardware deps), so the value is duplicated, not shared.
_OL_UA_PER_UM = 119.262

try:
    from alerts import send_alert
except Exception:
    def send_alert(subject: str, body: str) -> bool:  # type: ignore[misc]
        print(f"[Alerts] alerts module unavailable -- would have sent: {subject}")
        return False

# Downsampled overview images (per-MFOV + per-region). Best-effort: if the module
# or PIL stitch fails to import, QC still runs (overview just won't be produced).
try:
    from qc_overview import render_mfov_overview, RegionOverviewAccumulator
except Exception:  # pragma: no cover - overview is optional
    render_mfov_overview = None
    RegionOverviewAccumulator = None


DEFAULT_BEAM_REGEXES = [
    r"beam[_\- ]?(\d{1,3})",
    r"mfov[_\- ]?\d+[_\- ]?beam[_\- ]?(\d{1,3})",
    r"[_\- ]b(\d{1,3})(?:\D|$)",
    r"[_\- ](\d{1,3})(?:\.[^.]+)$",
]


@dataclass
class LiveQCRuntime:
    task_queue: Any
    viewer_queue: Any
    worker_process: Any
    viewer_process: Any
    results_dir: Path
    run_selected_beams: List[int]


def prepare_run_selected_beams(cfg: Dict[str, Any]) -> List[int]:
    mode = str(cfg.get("beam_selection_mode", "FIXED_PER_RUN_RANDOM")).upper()
    include_beam1 = bool(cfg.get("include_beam1", True))
    num_extra = int(cfg.get("num_extra_beams", 4))
    fixed_beams = [int(b) for b in cfg.get("fixed_beams", [])]
    beam_universe = [int(b) for b in cfg.get("beam_universe", list(range(1, 92)))]
    beam_universe = sorted(set(beam_universe))

    if mode == "FIXED_SET":
        chosen = fixed_beams[:] if fixed_beams else ([1] if include_beam1 else [])
        return sorted(set(chosen))

    rng = random.Random(int(cfg.get("random_seed", 20260316)))
    candidates = [b for b in beam_universe if (not include_beam1 or b != 1)]
    extras = rng.sample(candidates, k=min(num_extra, len(candidates)))
    chosen = extras[:]
    if include_beam1 and 1 in beam_universe:
        chosen = [1] + chosen
    return sorted(set(chosen))


def _iter_image_files(tile_folder: Path, cfg: Dict[str, Any], prefix: Optional[str]) -> List[Path]:
    patterns = cfg.get("image_suffix_globs", ["*.bmp"])
    paths: List[Path] = []
    for pat in patterns:
        paths.extend(tile_folder.glob(pat))
    paths = [p for p in paths if p.is_file()]
    paths = sorted(set(paths), key=lambda p: p.name.lower())
    if prefix:
        pref = [p for p in paths if p.name.startswith(prefix)]
        if pref:
            return pref
    return paths


def _extract_beam_index(name: str, prefix: Optional[str], regexes: Sequence[str]) -> Optional[int]:
    base = name
    if prefix and base.startswith(prefix):
        base = base[len(prefix):]
    for pat in regexes:
        m = re.search(pat, base, flags=re.IGNORECASE)
        if m:
            try:
                return int(m.group(1))
            except Exception:
                continue
    return None


def build_beam_map(tile_folder: str | Path, cfg: Dict[str, Any], prefix: Optional[str]) -> Dict[int, Path]:
    tile_folder = Path(tile_folder)
    regexes = cfg.get("beam_index_regexes", DEFAULT_BEAM_REGEXES)
    files = _iter_image_files(tile_folder, cfg, prefix)
    beam_map: Dict[int, Path] = {}
    extracted_any = False

    for p in files:
        beam_idx = _extract_beam_index(p.name, prefix, regexes)
        if beam_idx is not None:
            extracted_any = True
            beam_map.setdefault(int(beam_idx), p)

    if extracted_any:
        return dict(sorted(beam_map.items()))

    # Fallback: assume sorted files correspond to beams 1..N.
    for i, p in enumerate(files, start=1):
        beam_map[i] = p
    return beam_map


def choose_beams_for_tile(beam_map: Dict[int, Path], cfg: Dict[str, Any], tile_idx: int) -> List[int]:
    available = sorted(beam_map.keys())
    if not available:
        return []

    mode = str(cfg.get("beam_selection_mode", "FIXED_PER_RUN_RANDOM")).upper()
    include_beam1 = bool(cfg.get("include_beam1", True))
    num_extra = int(cfg.get("num_extra_beams", 4))

    if mode == "ALL":
        # Every beam actually found on disk for this tile (up to all 91) -
        # opt-in, since it's ~91 image reads+analyses per tile instead of the
        # usual ~5. Needed to catch a single beam going bad while the rest of
        # the mfov looks fine (see detect_outlier_beams below) - a subset
        # sample can miss that beam entirely.
        return available

    if mode == "FIXED_SET":
        target = [int(b) for b in cfg.get("fixed_beams", [])]
    elif mode == "PER_MFOV_RANDOM":
        seed = int(cfg.get("random_seed", 20260316)) + int(tile_idx)
        rng = random.Random(seed)
        candidates = [b for b in available if (not include_beam1 or b != 1)]
        extras = rng.sample(candidates, k=min(num_extra, len(candidates)))
        target = extras[:]
        if include_beam1 and 1 in available:
            target = [1] + target
    else:
        target = [int(b) for b in cfg.get("run_selected_beams", [])]
        if not target:
            target = prepare_run_selected_beams(cfg)

    picked = [b for b in target if b in available]
    if include_beam1 and 1 in available and 1 not in picked:
        picked = [1] + picked

    need = (1 if include_beam1 and 1 in available else 0) + num_extra
    if len(picked) < need:
        fill = [b for b in available if b not in picked]
        picked.extend(fill[: max(0, need - len(picked))])
    return picked[:need] if need > 0 else picked


CSV_COLUMNS = [
    "tile_idx",
    "x_um",
    "y_um",
    "quality",
    "ol_pred",
    "stig_x_pred",
    "stig_y_pred",
    "stig_x_med",
    "stig_y_med",
    "stig_anisotropy",
    "pct_saturation_med",
    "pct_dark_med",
    "pct_bright_med",
    "std_all_med",
    "jitter_score_med",
    "mean_intensity_med",
    "is_blank_outside_roi",
    "is_jittery",
    "n_images_used",
    "selected_beams",
    "status",
    "status_category",
    "failed_images",
    "tile_folder",
]


def write_result_files_header(results_csv: Path) -> None:
    if results_csv.exists():
        return
    with results_csv.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(CSV_COLUMNS)


def append_result_files(results_dir: Path, result: Dict[str, Any], low_quality_threshold: float) -> None:
    results_dir.mkdir(parents=True, exist_ok=True)
    results_csv = results_dir / "live_qc_results.csv"
    results_jsonl = results_dir / "live_qc_results.jsonl"
    low_txt = results_dir / "live_qc_low_quality.txt"
    outside_roi_txt = results_dir / "live_qc_outside_roi.txt"
    jittery_txt = results_dir / "live_qc_jittery.txt"

    write_result_files_header(results_csv)

    with results_csv.open("a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(
            [
                result.get("tile_idx"),
                result.get("x_um"),
                result.get("y_um"),
                result.get("quality"),
                result.get("ol_pred"),
                result.get("stig_x_pred"),
                result.get("stig_y_pred"),
                result.get("stig_x_med"),
                result.get("stig_y_med"),
                result.get("stig_anisotropy"),
                result.get("pct_saturation_med"),
                result.get("pct_dark_med"),
                result.get("pct_bright_med"),
                result.get("std_all_med"),
                result.get("jitter_score_med"),
                result.get("mean_intensity_med"),
                int(bool(result.get("is_blank_outside_roi", False))),
                int(bool(result.get("is_jittery", False))),
                result.get("n_images_used"),
                json.dumps(result.get("selected_beams", []), ensure_ascii=False),
                result.get("status"),
                result.get("status_category"),
                result.get("failed_images"),
                result.get("tile_folder"),
            ]
        )

    with results_jsonl.open("a", encoding="utf-8") as f:
        f.write(dumps_json(result) + "\n")

    q = float(result.get("quality", -100.0))
    if q < float(low_quality_threshold):
        with low_txt.open("a", encoding="utf-8") as f:
            f.write(f"tile {result.get('tile_idx')} quality={q:.3f} folder={result.get('tile_folder')}\n")

    if bool(result.get("is_blank_outside_roi", False)):
        with outside_roi_txt.open("a", encoding="utf-8") as f:
            f.write(
                f"tile {result.get('tile_idx')} sat_med={float(result.get('pct_saturation_med', 0.0)):.2f}% "
                f"std_med={float(result.get('std_all_med', 0.0)):.2f} folder={result.get('tile_folder')}\n"
            )

    if bool(result.get("is_jittery", False)):
        with jittery_txt.open("a", encoding="utf-8") as f:
            f.write(
                f"tile {result.get('tile_idx')} jitter_med={float(result.get('jitter_score_med', 0.0)):.4f} "
                f"folder={result.get('tile_folder')}\n"
            )


class _TeeStream:
    """File-like wrapper that writes to both the original stream AND a log file.
    Mirrors the same class in focus_map.py so QC subprocesses can append into
    the per-section console.log set up by run_one_region. Failures are swallowed
    silently — we never want logging to break QC."""

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


def _setup_subprocess_console_tee(cfg: Dict[str, Any], tag: str) -> None:
    """Tee this subprocess's stdout+stderr into the per-section console.log
    that the parent (focus_map.run_one_region) opened. No-op when the cfg
    key is missing or the file can't be opened — prints still go to the
    terminal in that case."""
    import sys as _sys
    log_path = cfg.get("section_console_log_path")
    if not log_path:
        return
    try:
        p = Path(log_path)
        p.parent.mkdir(parents=True, exist_ok=True)
        f = open(p, "a", buffering=1, encoding="utf-8", errors="replace")
        f.write(
            f"\n=== {tag} subprocess (pid={os.getpid()}) @ "
            f"{time.strftime('%Y-%m-%d %H:%M:%S')} ===\n"
        )
        f.flush()
        _sys.stdout = _TeeStream(_sys.stdout, f)
        _sys.stderr = _TeeStream(_sys.stderr, f)
    except Exception:
        pass  # tee is best-effort; never block QC on a log file error


def qc_worker_main(task_queue: Any, viewer_queue: Any, cfg: Dict[str, Any]) -> None:
    _setup_subprocess_console_tee(cfg, "qc_worker")
    results_dir = Path(cfg["results_dir"])
    low_quality_threshold = float(cfg.get("low_quality_threshold", 10.0))
    dsamp = int(cfg.get("dsamp", 200))
    sig_thresh = float(cfg.get("sig_thresh", 0.01))
    top_fraction = float(cfg.get("top_fraction", 0.50))
    aggregate_mode = str(cfg.get("aggregate_mode", "median"))
    outside_sat_thresh = float(cfg.get("outside_roi_sat_threshold", 30.0))
    outside_std_thresh = float(cfg.get("outside_roi_std_threshold", 3.0))
    jitter_warn_thresh = float(cfg.get("jitter_warn_threshold", 1.5))
    jitter_smooth_size = int(cfg.get("jitter_smooth_size", 8))
    jitter_band_fraction = float(cfg.get("jitter_top_band_fraction", 0.05))
    jitter_band_position = str(cfg.get("jitter_band_position", "top"))
    beam_outlier_mad_sigma = float(cfg.get("beam_outlier_mad_sigma", 3.0))
    beam_outlier_min_beams = int(cfg.get("beam_outlier_min_beams", 5))
    beam_outlier_alert_enabled = bool(cfg.get("beam_outlier_alert_enabled", True))

    import sys
    import traceback
    error_log = results_dir / "live_qc_worker_errors.txt"

    # One alert per (region, beam) per run - a berserk beam typically stays
    # berserk for many consecutive tiles, and emailing on every single one of
    # them would flood the inbox instead of informing it.
    _alerted_beam_outliers: set[Tuple[str, int]] = set()

    def _region_name_from_tile_folder(tile_folder: str) -> str:
        # Output layout is <block>/<Region_Name>/mfov<N>/ - see
        # configure_output_for_tile() in focus_map.py.
        try:
            return Path(tile_folder).parent.name
        except Exception:
            return "unknown_region"

    def _maybe_alert_beam_outliers(result: Dict[str, Any]) -> None:
        if not beam_outlier_alert_enabled:
            return
        outliers = result.get("outlier_beams") or []
        if not outliers:
            return
        region = _region_name_from_tile_folder(result.get("tile_folder", ""))
        new_outliers = [
            o for o in outliers
            if (region, int(o["beam_index"])) not in _alerted_beam_outliers
        ]
        if not new_outliers:
            return
        for o in new_outliers:
            _alerted_beam_outliers.add((region, int(o["beam_index"])))

        lines = [
            f"Region: {region}",
            f"Tile: {result.get('tile_idx')} @ ({result.get('x_um'):.1f}, {result.get('y_um'):.1f}) um",
            "",
            "Beam(s) with quality far from the rest of this mfov's beams:",
        ]
        for o in new_outliers:
            lines.append(
                f"  beam {o['beam_index']}: quality={o['quality']:.2f} "
                f"(mfov median={o['group_median']:.2f}, deviation={o['deviation']:+.2f})"
            )
        beam_list = ", ".join(str(o["beam_index"]) for o in new_outliers)
        subject = f"Beam outlier in {region} (beam {beam_list})"
        try:
            send_alert(subject, "\n".join(lines))
        except Exception as exc:
            print(f"[LiveQC worker] send_alert failed: {exc}")

    def _placeholder_failure_result(failed_task: Dict[str, Any], exc: BaseException) -> Dict[str, Any]:
        """Build a minimally-valid MFOV result so the dashboard can still
        render the failed tile (as status_category='error' / purple),
        instead of leaving it stuck on pending-grey forever."""
        return {
            "tile_idx": int(failed_task.get("tile_idx", -1)),
            "x_um": float(failed_task.get("x_um", 0.0)),
            "y_um": float(failed_task.get("y_um", 0.0)),
            "tile_folder": str(failed_task.get("tile_folder", "")),
            "quality": -100.0,
            "n_images_used": 0,
            "selected_beams": [],
            "status": f"error: {exc}",
            "status_category": "error",
            "failed_images": 0,
            "image_results": [],
            "stig_x_med": 0.0,
            "stig_y_med": 0.0,
            "stig_anisotropy": 0.0,
            "pct_saturation_med": 0.0,
            "pct_dark_med": 0.0,
            "pct_bright_med": 0.0,
            "std_all_med": 0.0,
            "jitter_score_med": 0.0,
            "mean_intensity_med": 0.0,
            "is_blank_outside_roi": False,
            "is_jittery": False,
            "outlier_beams": [],
            "has_beam_outlier": False,
            "ol_pred": failed_task.get("ol_pred"),
            "stig_x_pred": failed_task.get("stig_x_pred"),
            "stig_y_pred": failed_task.get("stig_y_pred"),
        }

    while True:
        task = task_queue.get()
        if task is None:
            try:
                viewer_queue.put({"type": "STOP"})
            except Exception:
                pass
            break

        # Wrap the entire per-task work in try/except. WITHOUT this, any
        # exception (corrupt BMP, missing beam file, numpy on empty array,
        # etc.) kills this spawned worker silently — after which no later
        # tile ever gets processed, so the dashboard shows them as pending
        # grey forever even though acquisition finished cleanly.
        try:
            tile_idx = int(task["tile_idx"])
            x_um = float(task["x_um"])
            y_um = float(task["y_um"])
            tile_folder = str(task["tile_folder"])
            prefix = task.get("prefix")

            beam_map = build_beam_map(tile_folder, cfg, prefix)
            selected_beams = choose_beams_for_tile(beam_map, cfg, tile_idx)
            image_results = []
            for beam in selected_beams:
                p = beam_map.get(int(beam))
                if p is None:
                    continue
                image_results.append(
                    compute_single_image_quality(
                        p,
                        beam_index=int(beam),
                        dsamp=dsamp,
                        sig_thresh=sig_thresh,
                        top_fraction=top_fraction,
                        jitter_smooth_size=jitter_smooth_size,
                        jitter_band_fraction=jitter_band_fraction,
                        jitter_band_position=jitter_band_position,
                    )
                )

            mfov = aggregate_mfov_quality(
                tile_idx=tile_idx,
                x_um=x_um,
                y_um=y_um,
                tile_folder=tile_folder,
                image_results=image_results,
                selected_beams=selected_beams,
                aggregate_mode=aggregate_mode,
                outside_roi_sat_threshold=outside_sat_thresh,
                outside_roi_std_threshold=outside_std_thresh,
                jitter_warn_threshold=jitter_warn_thresh,
                beam_outlier_mad_sigma=beam_outlier_mad_sigma,
                beam_outlier_min_beams=beam_outlier_min_beams,
            )
            result = mfov.to_dict()
            # The actually-commanded OL/StigX/StigY for this tile - passed
            # straight through from the task, not derived from the image
            # analysis above (mfov.to_dict() has no notion of these).
            result["ol_pred"] = task.get("ol_pred")
            result["stig_x_pred"] = task.get("stig_x_pred")
            result["stig_y_pred"] = task.get("stig_y_pred")
            _maybe_alert_beam_outliers(result)

            # Per-MFOV downsampled overview (best-effort; never breaks QC). Writes
            # overview_mfov.png into the tile folder and records its path on the
            # result so the viewer can build the region overview from it.
            if render_mfov_overview is not None and bool(cfg.get("overview_enabled", True)):
                try:
                    ov_png = render_mfov_overview(tile_folder, prefix, cfg)
                    if ov_png:
                        result["overview_mfov_png"] = ov_png
                except Exception:
                    pass
        except Exception as exc:
            tb = traceback.format_exc()
            try:
                sys.stderr.write(
                    f"[LiveQC worker] tile {task.get('tile_idx')} crashed:\n{tb}"
                )
                sys.stderr.flush()
            except Exception:
                pass
            try:
                results_dir.mkdir(parents=True, exist_ok=True)
                with error_log.open("a", encoding="utf-8") as f:
                    f.write(
                        f"--- tile {task.get('tile_idx')} @ "
                        f"{time.strftime('%Y-%m-%d %H:%M:%S')} ---\n"
                        f"task = {task}\n{tb}\n"
                    )
            except Exception:
                pass
            result = _placeholder_failure_result(task, exc)

        try:
            append_result_files(
                results_dir, result, low_quality_threshold=low_quality_threshold
            )
        except Exception as exc:
            try:
                sys.stderr.write(
                    f"[LiveQC worker] append_result_files failed "
                    f"for tile {task.get('tile_idx')}: {exc}\n"
                )
                sys.stderr.flush()
            except Exception:
                pass

        try:
            viewer_queue.put({"type": "RESULT", "result": result})
        except Exception:
            pass


def _estimate_hex_radius(layout: Sequence[Dict[str, Any]], fallback: float = 50.0) -> float:
    pts = [(float(r["x_um"]), float(r["y_um"])) for r in layout]
    if len(pts) < 2:
        return fallback
    nearest: List[float] = []
    for i, (x0, y0) in enumerate(pts):
        best = None
        for j, (x1, y1) in enumerate(pts):
            if i == j:
                continue
            d = math.hypot(x1 - x0, y1 - y0)
            if d <= 1e-9:
                continue
            if best is None or d < best:
                best = d
        if best is not None:
            nearest.append(best)
    if not nearest:
        return fallback
    return max(fallback, 0.45 * sorted(nearest)[len(nearest) // 2])


def qc_viewer_main(layout: List[Dict[str, Any]], viewer_queue: Any, cfg: Dict[str, Any]) -> None:
    import sys
    import traceback
    _setup_subprocess_console_tee(cfg, "qc_viewer")
    try:
        _qc_viewer_main_impl(layout, viewer_queue, cfg)
    except Exception:
        sys.stderr.write("[LiveQC viewer] CRASHED:\n")
        traceback.print_exc()
        sys.stderr.flush()
        try:
            (Path(cfg["results_dir"]) / "live_qc_viewer_error.txt").write_text(
                traceback.format_exc(), encoding="utf-8"
            )
        except Exception:
            pass
        raise


_STATUS_COLORS = {
    "ok": (0.30, 0.69, 0.31, 1.0),
    "insufficient_signal": (1.00, 0.85, 0.20, 1.0),
    "acquisition_fail": (0.85, 0.20, 0.20, 1.0),
    "no_valid_images": (0.55, 0.55, 0.55, 1.0),
    "error": (0.45, 0.10, 0.55, 1.0),
    "outside_roi": (0.40, 0.40, 0.40, 1.0),
    "mixed": (0.95, 0.55, 0.20, 1.0),
}
_PENDING_FILL = (0.92, 0.92, 0.92, 1.0)


def _path_to_file_url(p: str) -> str:
    """Convert a local filesystem path (Windows or POSIX) into a file:// URL."""
    if not p:
        return ""
    s = str(p).replace("\\", "/")
    if not s.startswith("/"):
        s = "/" + s
    return "file://" + urllib.parse.quote(s, safe="/:")


def _fmt_duration(seconds: float) -> str:
    """Human-readable duration for the timing lines in the QC report/stats
    panel, e.g. 125.4 -> '2m 05s'."""
    seconds = max(0.0, float(seconds))
    m, s = divmod(seconds, 60.0)
    h, m = divmod(int(m), 60)
    if h > 0:
        return f"{h}h {m:02d}m {s:04.1f}s"
    if m > 0:
        return f"{m}m {s:04.1f}s"
    return f"{s:.1f}s"


def _rgba_to_hex(rgba: Sequence[float]) -> str:
    r, g, b = rgba[0], rgba[1], rgba[2]
    return "#{:02x}{:02x}{:02x}".format(
        max(0, min(255, int(round(r * 255)))),
        max(0, min(255, int(round(g * 255)))),
        max(0, min(255, int(round(b * 255)))),
    )


def _read_section_stats_from_csv(csv_path: Path) -> Dict[str, Any]:
    out = {"n_total": 0, "fails": 0, "outside": 0, "jittery": 0, "median_q": float("nan")}
    if not csv_path.exists():
        return out
    qs: List[float] = []
    try:
        with csv_path.open("r", newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                out["n_total"] += 1
                status_cat = (row.get("status_category") or "").strip()
                if status_cat in ("acquisition_fail", "error", "no_valid_images"):
                    out["fails"] += 1
                if str(row.get("is_blank_outside_roi", "0")).strip() in ("1", "True", "true"):
                    out["outside"] += 1
                if str(row.get("is_jittery", "0")).strip() in ("1", "True", "true"):
                    out["jittery"] += 1
                try:
                    q = float(row.get("quality") or "nan")
                    if status_cat == "ok" and q == q:
                        qs.append(q)
                except Exception:
                    pass
    except Exception:
        return out
    if qs:
        out["median_q"] = float(np.median(np.asarray(qs, dtype=np.float64)))
    return out


def _read_section_timing_from_json(path: Path) -> Dict[str, Any]:
    """Read the small live_qc_timing.json a section's viewer writes when its
    TIMING message arrives (see _qc_viewer_main_impl). Missing/corrupt/not-
    yet-written (section still running, or ran before this file existed) all
    resolve to the same "no timing yet" defaults -- never raises."""
    out = {"focus_s": 0.0, "wall_s": 0.0, "n_tiles": 0}
    if not path.exists():
        return out
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        out["focus_s"] = float(data.get("focus_s", 0.0) or 0.0)
        out["wall_s"] = float(data.get("wall_s", 0.0) or 0.0)
        out["n_tiles"] = int(data.get("n_tiles", 0) or 0)
    except Exception:
        pass
    return out


def _read_region_manifest(block_folder: Path) -> Optional[List[Dict[str, Any]]]:
    """Read <block_folder>/region_manifest.json (written by focus_map.py's
    main() before/during a run) -- the authoritative list of every region
    this run intends to process, with live status ("pending"/"running"/
    "done"/"failed"). Returns None if the file doesn't exist or can't be
    parsed (older run, single-region run, or any other non-manifest-aware
    caller), so _write_block_index_html falls back to its old
    dashboard-scan-only behavior."""
    path = Path(block_folder) / "region_manifest.json"
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        regions = data.get("regions")
        return regions if isinstance(regions, list) else None
    except Exception:
        return None


# (label, fg color, bg color) per region_manifest.json status value.
_REGION_STATUS_LABELS: Dict[str, Tuple[str, str, str]] = {
    "pending": ("Pending", "#666", "#e6e6e6"),
    "running": ("Running…", "#fff", "#1d6fb8"),
    "done": ("Done", "#fff", "#2e8b3d"),
    "failed": ("Failed", "#fff", "#c0392b"),
    "skipped": ("Skipped", "#fff", "#c98a13"),
    "retake": ("Retake", "#fff", "#8e44ad"),
}

# (label, fg color, bg color) per human region-REVIEW decision - a SEPARATE
# axis from _REGION_STATUS_LABELS above. That dict is pipeline/acquisition
# status ("did imaging succeed"); this one is human judgment ("does the
# resulting image actually look in focus"), captured by the global QC
# report's Accept/Retake/Not-verified controls - a region can be "Done" and
# still "Not verified", or "Done" and "Retake" if a human looked and disagreed.
_REGION_REVIEW_LABELS: Dict[str, Tuple[str, str, str]] = {
    "not_verified": ("Not verified", "#555", "#e6e6e6"),
    "accepted": ("Accepted", "#fff", "#2e8b3d"),
    "retake": ("Retake", "#fff", "#8e44ad"),
}

REGION_REVIEW_STATUS_FILENAME = "region_review_status.json"


def read_region_review_status(path: str | Path) -> Dict[str, Dict[str, str]]:
    """Read the human region-review decisions saved by the global QC
    report's Accept/Retake/Not-verified controls:
    {region_name: {"status": "accepted"|"retake"|"not_verified", "reason": str}}.

    Returns {} if the file doesn't exist (nothing reviewed yet is the
    default, not an error) - same convention as read_retake_list."""
    path = Path(path)
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return {}
        out: Dict[str, Dict[str, str]] = {}
        for name, entry in data.items():
            if not isinstance(entry, dict):
                continue
            status = str(entry.get("status", "not_verified"))
            if status not in _REGION_REVIEW_LABELS:
                status = "not_verified"
            out[str(name)] = {"status": status, "reason": str(entry.get("reason", "") or "")}
        return out
    except Exception as exc:
        print(f"[Review] WARNING: could not read region review status {path}: {exc}")
        return {}


def _merged_region_review_status(
    runs_root: Path,
    best: Dict[str, Dict[str, Any]],
    wafer_review_status: Dict[str, Dict[str, str]],
) -> Dict[str, Dict[str, str]]:
    """Fold each region's WINNING batch's own region_review_status.json (see
    focus_map.py's _refresh_block_index: since 2026-09-04 each batch report
    saves an independent copy inside its own batch folder, so a stale
    attempt's review can never bleed into a different attempt's) into the
    wafer-level file the global report itself reads/writes - so the global
    report reflects Accept/Retake/Not-verified decisions made from a
    per-batch report, not only from the global report's own controls.

    The wafer-level entry wins whenever both exist and it's a real
    (non-default) decision - a click made directly on the global report is
    treated as the more deliberate, final word for that region. "Winning
    batch" is always best[name]["batch"] (the current best-status attempt),
    so a superseded earlier attempt's review can never override the current
    one - the same no-cross-batch-bleed guarantee the per-batch split was
    added for."""
    merged = dict(wafer_review_status)
    batch_cache: Dict[str, Dict[str, Dict[str, str]]] = {}
    for name, info in best.items():
        batch_name = info.get("batch")
        if not batch_name:
            continue
        existing = merged.get(name)
        if existing and existing.get("status", "not_verified") != "not_verified":
            continue
        if batch_name not in batch_cache:
            batch_cache[batch_name] = read_region_review_status(
                runs_root / batch_name / REGION_REVIEW_STATUS_FILENAME)
        batch_entry = batch_cache[batch_name].get(name)
        if batch_entry and batch_entry.get("status", "not_verified") != "not_verified":
            merged[name] = batch_entry
    return merged


# Shared by write_wafer_qc_report (where this originated) and
# _write_block_index_html (the per-batch report) - both use the exact same
# Accept/Retake/Not-verified controls/markup, but each SAVES into its own
# review_root's region_review_status.json (the global report's is at the
# wafer root; each batch report's is inside that batch's own folder, since
# 2026-09-04 - see focus_map.py's _refresh_block_index for why: sharing one
# wafer-wide file made one batch's review clicks bleed into every other
# batch's report for a region re-imaged across attempts). The global report
# still reflects a batch-level decision for display purposes - see
# _merged_region_review_status, which folds each region's WINNING batch's
# own file into the wafer-level one it reads/writes directly.
_REVIEW_CONTROLS_CSS = """
  .review-banner { margin-top:10px; padding:8px 12px; background:#fff3cd; color:#7a5b00; border:1px solid #ffe08a; border-radius:4px; font-size:12px; max-width:900px; }
  .review-banner.synced { background:#e6f4ea; color:#1e5b2e; border-color:#bfe3c9; }
  .review-controls { margin-top:8px; display:flex; align-items:center; gap:10px; flex-wrap:wrap; }
  .review-controls button { font:inherit; padding:5px 10px; border:1px solid #aaa; background:#f6f6f8; border-radius:4px; cursor:pointer; font-size:12px; }
  .review-controls button.primary { background:#2b66c2; color:#fff; border-color:#1d4f9e; }
  .review-controls button.primary:hover { background:#3a78d4; }
  .review-controls button:hover { background:#e8e8ec; }
  .review-status-line { font-size:11px; color:#555; }
  .review-status-line .file { color:#1d4f9e; word-break:break-all; }
  .review-status-line .dirty { color:#a33; font-weight:600; }
  .review-group { display:inline-flex; border:1px solid #ccc; border-radius:4px; overflow:hidden; }
  .rev-btn { font:inherit; font-size:11px; padding:4px 8px; border:none; border-right:1px solid #ccc; background:#f6f6f8; color:#444; cursor:pointer; }
  .rev-btn:last-child { border-right:none; }
  .rev-btn:hover { background:#e8e8ec; }
  .rev-btn.active[data-status="not_verified"] { background:#e6e6e6; color:#555; font-weight:600; }
  .rev-btn.active[data-status="accepted"] { background:#2e8b3d; color:#fff; font-weight:600; }
  .rev-btn.active[data-status="retake"] { background:#8e44ad; color:#fff; font-weight:600; }
  .rev-reason { display:block; margin-top:4px; font:inherit; font-size:11px; width:220px; padding:3px 6px; border:1px solid #ccc; border-radius:3px; }
  .toast { position:fixed; right:14px; bottom:14px; background:#222; color:#fff; padding:6px 10px; border-radius:4px; font-size:12px; opacity:0; transition:opacity 0.2s; pointer-events:none; }
  .toast.show { opacity:0.95; }
"""


def _review_cell_html(name: str, review_status: Dict[str, Dict[str, str]]) -> Tuple[str, Dict[str, str]]:
    """One region's Accept/Retake/Not-verified control group + reason input.
    Returns (html, this_region's_current_entry) - the caller collects the
    entries into review_defaults for _review_controls_script_html."""
    rv = review_status.get(name, {"status": "not_verified", "reason": ""})
    html = (
        f'<div class="review-group" data-region="{_html.escape(name)}">'
        f'<button type="button" class="rev-btn" data-status="not_verified">Not verified</button>'
        f'<button type="button" class="rev-btn" data-status="accepted">Accept</button>'
        f'<button type="button" class="rev-btn" data-status="retake">Retake</button>'
        f'</div>'
        f'<input type="text" class="rev-reason" data-region="{_html.escape(name)}" '
        f'placeholder="reason (optional)" value="{_html.escape(rv["reason"])}">'
    )
    return html, rv


def _review_controls_header_html(retake_sync_note: str) -> str:
    return f"""
  <div class="review-banner" id="review-sync-banner">
    ⚠ The Accept/Retake buttons below show this page's state as of when it was last generated -
    <strong>reopening this file does not automatically reload changes saved since then.</strong>
    Click <strong>"Sync review status"</strong> below once to load what's actually saved on disk right now.
  </div>
  <div class="review-controls">
    <button type="button" id="btn-pick-file" class="primary">Sync review status</button>
    <button type="button" id="btn-save-review" disabled>Save now</button>
    <span class="review-status-line">File: <span id="review-file-label" class="file">(not connected yet)</span>
      &nbsp;·&nbsp; <span id="review-autosave-status">not connected yet</span></span>
  </div>
  <div class="review-status-line" style="margin-top:4px;">
    Once connected (click "Sync review status" once per browser session, or per reopen of this exact file),
    every Accept/Retake click saves itself automatically - no separate save step. Your browser will only ask
    where to save <code>{REGION_REVIEW_STATUS_FILENAME}</code> the very first time ever; after that it silently
    reuses the same file. {retake_sync_note}
  </div>
"""


def _review_controls_script_html(review_root: str | Path, review_defaults: Dict[str, Dict[str, str]]) -> str:
    """review_root is the folder region_review_status.json lives in/gets
    created in - pass the SAME root (the wafer root, not a batch folder) from
    every caller so every report reads/writes one shared file. Includes the
    toast div the script's showToast() needs - callers don't add it separately."""
    return f"""
<div class="toast" id="toast">saved</div>
<script id="review-payload" type="application/json">{json.dumps(review_defaults, ensure_ascii=False)}</script>
<script>
(function() {{
  const REVIEW_FILENAME = {json.dumps(REGION_REVIEW_STATUS_FILENAME)};
  const IDB_NAME = 'himc_qc_review';
  // Scoped to THIS root folder, not just a fixed name - otherwise every
  // dataset's report would silently reconnect to whichever OTHER dataset's
  // region_review_status.json handle happened to be remembered last in this
  // browser profile (they all share one file:// IndexedDB).
  const IDB_KEY = 'reviewFileHandle:' + {json.dumps(str(review_root))};
  const AUTOSAVE_DEBOUNCE_MS = 400;

  let review = {{}};
  try {{
    review = JSON.parse(document.getElementById('review-payload').textContent || '{{}}');
  }} catch (e) {{ review = {{}}; }}

  const dirty = new Set();       // edited in this tab since load - never clobbered by reconcile
  let fileHandle = null;
  let saveTimer = null;
  let savingNow = false;

  const toast = document.getElementById('toast');
  function showToast(msg) {{
    toast.textContent = msg;
    toast.classList.add('show');
    clearTimeout(toast._t);
    toast._t = setTimeout(() => toast.classList.remove('show'), 2600);
  }}

  function setStatus(text) {{
    document.getElementById('review-autosave-status').textContent = text;
  }}

  function applyButtonState(group, status) {{
    group.querySelectorAll('.rev-btn').forEach(b => {{
      b.classList.toggle('active', b.dataset.status === status);
    }});
  }}

  function loadReviewIntoUI() {{
    document.querySelectorAll('.review-group').forEach(group => {{
      const region = group.dataset.region;
      const status = (review[region] && review[region].status) || 'not_verified';
      applyButtonState(group, status);
      const reasonInput = document.querySelector('.rev-reason[data-region="' + CSS.escape(region) + '"]');
      if (reasonInput) reasonInput.value = (review[region] && review[region].reason) || '';
    }});
  }}
  loadReviewIntoUI();

  // Never let a slow/stuck promise (a flaky IndexedDB open, in particular)
  // block the whole connect/save chain forever with no visible feedback -
  // resolves to `fallback` if `promise` hasn't settled within `ms`.
  function withTimeout(promise, ms, fallback) {{
    return new Promise((resolve) => {{
      let done = false;
      const timer = setTimeout(() => {{ if (!done) {{ done = true; resolve(fallback); }} }}, ms);
      promise.then(v => {{ if (!done) {{ done = true; clearTimeout(timer); resolve(v); }} }})
             .catch(() => {{ if (!done) {{ done = true; clearTimeout(timer); resolve(fallback); }} }});
    }});
  }}

  // ---------- tiny IndexedDB helper: remember the picked file handle so ----
  // ---------- reopening this same report doesn't need a re-pick every time ---
  function idbOpen() {{
    return new Promise((resolve, reject) => {{
      const req = indexedDB.open(IDB_NAME, 1);
      req.onupgradeneeded = () => req.result.createObjectStore('handles');
      req.onsuccess = () => resolve(req.result);
      req.onerror = () => reject(req.error);
    }});
  }}
  async function idbGet(key) {{
    try {{
      const db = await idbOpen();
      return await new Promise((resolve, reject) => {{
        const tx = db.transaction('handles', 'readonly');
        const req = tx.objectStore('handles').get(key);
        req.onsuccess = () => resolve(req.result || null);
        req.onerror = () => reject(req.error);
      }});
    }} catch (e) {{ return null; }}
  }}
  async function idbSet(key, value) {{
    try {{
      const db = await idbOpen();
      await new Promise((resolve, reject) => {{
        const tx = db.transaction('handles', 'readwrite');
        tx.objectStore('handles').put(value, key);
        tx.oncomplete = () => resolve();
        tx.onerror = () => reject(tx.error);
      }});
    }} catch (e) {{ /* best-effort only - manual re-pick still works */ }}
  }}

  async function reconcileFromDisk(handle) {{
    // Whatever is already on this file may be newer than what this report
    // was generated with (e.g. reviewed from another tab, or after this
    // page loaded) - on-disk wins for anything not already edited in THIS
    // tab since load (dirty), so an unsaved local edit is never clobbered.
    try {{
      const file = await handle.getFile();
      if (file.size === 0) return;
      const onDisk = JSON.parse(await file.text());
      for (const [region, entry] of Object.entries(onDisk)) {{
        if (!dirty.has(region)) review[region] = entry;
      }}
      loadReviewIntoUI();
    }} catch (e) {{ /* not created yet, or unreadable - fine, Save will create it */ }}
  }}

  function markSynced() {{
    const banner = document.getElementById('review-sync-banner');
    banner.classList.add('synced');
    banner.textContent = '✓ Synced with ' + (fileHandle ? fileHandle.name : REVIEW_FILENAME) +
      ' - the buttons below now reflect what is actually saved on disk. Every further click autosaves.';
  }}

  async function connectHandle(handle, persist) {{
    fileHandle = handle;
    document.getElementById('review-file-label').textContent = handle.name;
    document.getElementById('btn-save-review').disabled = false;
    await reconcileFromDisk(handle);
    if (persist) await withTimeout(idbSet(IDB_KEY, handle), 1500, undefined);
    setStatus('autosave ready');
    markSynced();
  }}

  async function pickFile() {{
    if (!window.showSaveFilePicker) {{
      alert('Your browser does not support the File System Access API. Use Chrome or Edge to save review status in-place.');
      return;
    }}
    try {{
      const handle = await window.showSaveFilePicker({{
        suggestedName: REVIEW_FILENAME,
        types: [{{description: 'Region review status', accept: {{'application/json': ['.json']}}}}],
      }});
      await connectHandle(handle, true);
    }} catch (e) {{
      if (e && e.name !== 'AbortError') alert('File pick failed: ' + e.message);
    }}
  }}

  // Called from inside a real click handler (button or reason-input change)
  // so it always has a valid user gesture available if one turns out to be
  // needed - either to silently re-confirm a remembered permission, or (only
  // the very first time ever, or if the remembered handle has gone stale) to
  // show the one-time "where should this save" dialog itself.
  async function ensureConnected() {{
    if (fileHandle) return true;
    const stored = await withTimeout(idbGet(IDB_KEY), 1500, null);
    if (stored) {{
      try {{
        let perm = await stored.queryPermission({{mode: 'readwrite'}});
        if (perm !== 'granted') perm = await stored.requestPermission({{mode: 'readwrite'}});
        if (perm === 'granted') {{
          await connectHandle(stored, false);
          return true;
        }}
      }} catch (e) {{ /* stale/invalid handle (e.g. file moved) - fall through to picking a new one */ }}
    }}
    await pickFile();
    return !!fileHandle;
  }}

  // Silent, gesture-free attempt on page load - only succeeds if the
  // browser already considers permission granted without asking again.
  async function tryQuietAutoReconnect() {{
    const stored = await withTimeout(idbGet(IDB_KEY), 1500, null);
    if (!stored) return;
    try {{
      const perm = await stored.queryPermission({{mode: 'readwrite'}});
      if (perm === 'granted') {{
        await connectHandle(stored, false);
        showToast('reconnected to ' + stored.name);
      }} else {{
        setStatus('not connected - click "Sync review status" above');
      }}
    }} catch (e) {{ /* stale/invalid handle - "Sync review status" will pick a new one */ }}
  }}

  async function doSave() {{
    if (!fileHandle || savingNow) return;
    savingNow = true;
    setStatus('saving…');
    try {{
      const writer = await fileHandle.createWritable();
      await writer.write(JSON.stringify(review, null, 2));
      await writer.close();
      dirty.clear();
      const now = new Date();
      setStatus('autosaved ' + now.toLocaleTimeString());
    }} catch (e) {{
      setStatus('autosave FAILED - click "Save now" (' + e.message + ')');
    }} finally {{
      savingNow = false;
    }}
  }}

  function scheduleAutoSave() {{
    clearTimeout(saveTimer);
    setStatus('saving…');
    saveTimer = setTimeout(doSave, AUTOSAVE_DEBOUNCE_MS);
  }}

  async function onReviewChanged(region, status, reason) {{
    review[region] = {{ status: status, reason: reason }};
    dirty.add(region);
    try {{
      const connected = await ensureConnected();
      if (connected) scheduleAutoSave();
      else setStatus('not connected - click "Sync review status" above');
    }} catch (e) {{
      setStatus('autosave error: ' + e.message + ' - try "Save now"');
    }}
  }}

  document.querySelectorAll('.review-group').forEach(group => {{
    const region = group.dataset.region;
    group.querySelectorAll('.rev-btn').forEach(btn => {{
      btn.addEventListener('click', () => {{
        const status = btn.dataset.status;
        applyButtonState(group, status);
        const reasonInput = document.querySelector('.rev-reason[data-region="' + CSS.escape(region) + '"]');
        onReviewChanged(region, status, reasonInput ? reasonInput.value : '');
      }});
    }});
  }});

  document.querySelectorAll('.rev-reason').forEach(input => {{
    input.addEventListener('change', () => {{
      const region = input.dataset.region;
      const current = (review[region] && review[region].status) || 'not_verified';
      onReviewChanged(region, current, input.value);
    }});
  }});

  // Best-effort final flush if the tab is closed/navigated away while a
  // debounced save is still pending - not guaranteed to complete (async
  // work during unload is inherently unreliable in every browser), but the
  // 400ms debounce means a save has almost always already landed well
  // before a human finishes closing a tab after their last click.
  document.addEventListener('visibilitychange', () => {{
    if (document.visibilityState === 'hidden' && fileHandle && saveTimer) {{
      clearTimeout(saveTimer);
      doSave();
    }}
  }});

  // "Sync review status" reuses the remembered handle (a lightweight
  // native re-confirm, not a fresh file dialog) whenever one exists - it
  // only falls back to picking a brand-new file if there truly isn't one
  // yet. Wired to ensureConnected() (not pickFile() directly) specifically
  // so reopening this exact report and clicking this button reconnects to
  // the SAME file instead of prompting to choose a new one each time.
  document.getElementById('btn-pick-file').onclick = () => {{
    ensureConnected().then(ok => {{
      if (ok) showToast('synced with ' + fileHandle.name);
    }});
  }};
  document.getElementById('btn-save-review').onclick = () => {{ ensureConnected().then(ok => {{ if (ok) doSave(); }}); }};

  tryQuietAutoReconnect();
}})();
</script>
"""


def sync_retake_list(
    review_status: Dict[str, Dict[str, str]],
    partial_retakes_pending: Optional[Dict[str, List[str]]],
    retake_path: str | Path,
) -> bool:
    """Keep retake_regions.txt (the plain-text file the acquisition pipeline
    actually consumes - see read_retake_list/compute_regions_to_image) in
    sync with two upstream sources:

    - region_review_status.json (what the global QC report's whole-region
      Accept/Retake/Not-verified UI saves) -> "full" retake lines.
    - the wafer retake JSON's pending "<region>_retake_N" sub-regions (see
      scan_partial_retakes, written by a region dashboard's per-mfov Retake
      selection panel) -> "partial(N)" retake lines.

    A region a human explicitly marked "retake" (whole-region) always wins
    over an auto-detected partial entry for that same region - re-imaging
    the whole thing subsumes whatever the partial flag was pointing at.
    Otherwise the two are independent: a region can be "accepted" overall
    and still carry a pending partial retake (a known, already-flagged
    trouble spot doesn't need re-litigating just because the rest of the
    region looks fine). Partial entries are fully re-derived from
    partial_retakes_pending every call - a region no longer listed there
    (its flagged mfovs got imaged, or removed from the retake JSON) has its
    partial line cleared automatically, never left stale. A region never
    touched by either source keeps whatever a human hand-edited directly
    into retake_regions.txt (the original, still-supported convention)
    untouched.

    Returns True if the file was written (changed), False if already in
    sync (nothing written, so a fresh generation doesn't touch the file's
    mtime for no reason)."""
    retake_path = Path(retake_path)
    existing = read_retake_list(retake_path)
    merged: Dict[str, Dict[str, Any]] = {name: dict(entry) for name, entry in existing.items()}

    # Partial entries are fully re-derived each call, never accumulated.
    partial_bases = set((partial_retakes_pending or {}).keys())
    for name in list(merged.keys()):
        if merged[name].get("kind") == "partial" and name not in partial_bases:
            del merged[name]
    for base_name, sub_names in (partial_retakes_pending or {}).items():
        if not sub_names:
            continue
        merged[base_name] = {
            "kind": "partial",
            "reason": f"{len(sub_names)} mfov area(s) flagged: {', '.join(sorted(sub_names))}",
            "count": len(sub_names),
        }

    # Whole-region human review decisions always win over auto-detected partial.
    for name, entry in review_status.items():
        if entry.get("status") == "retake":
            merged[name] = {"kind": "full", "reason": entry.get("reason", ""), "count": None}
        else:
            existing_entry = merged.get(name)
            if existing_entry and existing_entry.get("kind", "full") == "full":
                merged.pop(name, None)

    if merged == existing:
        return False
    lines = ["# Auto-generated from region_review_status.json + the wafer retake JSON - "
             "hand-edited entries for regions neither source touches are preserved."]
    for name, entry in merged.items():
        kind = entry.get("kind", "full")
        reason = entry.get("reason", "")
        count = entry.get("count")
        kind_str = f"partial({count})" if (kind == "partial" and count) else kind
        lines.append(f"{name}: {kind_str}: {reason}" if reason else f"{name}: {kind_str}")
    try:
        retake_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return True
    except Exception as exc:
        print(f"[Review] WARNING: could not write {retake_path}: {exc}")
        return False


_PARTIAL_RETAKE_SUBREGION_RE = re.compile(r"^(?P<base>.+)_retake_(?P<n>\d+)$")


def resolve_partial_retake_json_path(imaging_json_path: str | Path) -> Path:
    """Default location of a wafer's partial-retake JSON: a sibling of the
    main imaging JSON, named "<wafer>_retake.json" - the exact suggestedName
    a region dashboard's Retake selection panel offers when saving (see
    _write_dashboard_html), so accepting that default just works here with
    no extra configuration."""
    p = Path(imaging_json_path)
    return p.with_name(p.stem + "_retake.json")


def scan_partial_retakes(retake_json_path: str | Path) -> Dict[str, List[str]]:
    """Scan a wafer retake JSON (written by a region dashboard's per-mfov
    Retake selection panel - see _write_dashboard_html) for partial-region
    retake sub-regions, named "<region>_retake_<N>", grouped by the base
    region name they belong to.

    Returns {base_region_name: [sub_region_name, ...]} (sorted for
    determinism), or {} if the file doesn't exist / has no Regions / isn't
    readable - nothing flagged is the default, not an error."""
    path = Path(retake_json_path)
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        print(f"[Retake] WARNING: could not read partial-retake JSON {path}: {exc}")
        return {}
    regions = data.get("Regions")
    if not isinstance(regions, list):
        return {}
    out: Dict[str, List[str]] = {}
    for region_xml in regions:
        if not isinstance(region_xml, str):
            continue
        m = re.search(r'Name="([^"]+)"', region_xml)
        if not m:
            continue
        pm = _PARTIAL_RETAKE_SUBREGION_RE.match(m.group(1))
        if not pm:
            continue
        out.setdefault(pm.group("base"), []).append(m.group(1))
    return {base: sorted(names) for base, names in out.items()}


def scan_partial_retakes_for_wafer(runs_root: str | Path) -> Dict[str, List[str]]:
    """Merge every batch folder's OWN "<batch>_retake.json" (see
    _write_dashboard_html's per-batch Retake selection panel - each batch's
    dashboards grant access to and save directly into that batch's own
    folder, not one shared wafer-wide file) into a single
    {base_region_name: [sub_region_name, ...]} mapping for the whole wafer.

    A region's partial retakes only ever live in the ONE batch that actually
    imaged it, so there's no real merge conflict across batches in
    practice - this just saves callers from having to know which batch to
    look in."""
    runs_root = Path(runs_root)
    combined: Dict[str, List[str]] = {}
    if not runs_root.is_dir():
        return combined
    for batch_dir in sorted(p for p in runs_root.iterdir() if p.is_dir()):
        retake_path = batch_dir / f"{batch_dir.name}_retake.json"
        if not retake_path.is_file():
            continue
        for base, names in scan_partial_retakes(retake_path).items():
            bucket = combined.setdefault(base, [])
            for name in names:
                if name not in bucket:
                    bucket.append(name)
    return {base: sorted(names) for base, names in combined.items()}


# A bare "Name" or "Name: reason" line (no recognizable kind tag) is always
# "full" - this is the original, still-fully-supported format, so an
# operator's pre-existing hand-edited retake_regions.txt keeps working
# unchanged. "Name: partial(N): reason" / "Name: partial: reason" marks a
# PARTIAL-region retake (specific mfovs, tracked in a separate wafer retake
# JSON - see scan_partial_retakes) - N, when present, is just an informational
# sub-region count for humans reading the file; it is not re-derived from it.
_RETAKE_KIND_RE = re.compile(r"^(full|partial)(?:\((\d+)\))?$", re.IGNORECASE)


def read_retake_list(path: str | Path) -> Dict[str, Dict[str, Any]]:
    """Read an operator-maintained retake list: one region name per line,
    with an optional ": [full|partial[(N)]]: reason" after the name (e.g.
    "S5R1: full: outer beams look out of focus, redo" or "S20R1: partial(2):
    2 mfov areas flagged"). A plain "S5R1" or "S5R1: some text" with no
    recognizable kind tag defaults to "full" - the original format. Blank
    lines and lines starting with # are ignored.

    This is deliberately a plain text file, not a JSON/GUI mechanism: it's
    the same "operator edits a small file next to the data" convention this
    codebase already uses for prev_focusmap_dir/region_manifest.json, and it
    means flagging a retake never requires touching the imaging JSON or
    waiting on a GUI feature - just note it in the file and it's picked up
    by both the QC report and the next run's region selection.

    Returns {region_name: {"kind": "full"|"partial", "reason": str, "count":
    Optional[int]}}, or {} if the file doesn't exist (nothing flagged is the
    default, not an error)."""
    path = Path(path)
    if not path.exists():
        return {}
    entries: Dict[str, Dict[str, Any]] = {}
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if ":" not in line:
                entries[line] = {"kind": "full", "reason": "", "count": None}
                continue
            name, rest = line.split(":", 1)
            name = name.strip()
            rest = rest.strip()
            kind, count, reason = "full", None, rest
            if ":" in rest:
                maybe_kind, remainder = rest.split(":", 1)
                m = _RETAKE_KIND_RE.match(maybe_kind.strip())
                if m:
                    kind = m.group(1).lower()
                    count = int(m.group(2)) if m.group(2) else None
                    reason = remainder.strip()
            else:
                # No second colon at all - e.g. "S6R1: full" or "S6R1: partial(2)"
                # (an empty-reason auto-generated line, or an operator typing
                # just the kind by hand). Without this, a bare "full"/
                # "partial(N)" remainder would be misread as reason text
                # instead of the kind tag it actually is.
                m = _RETAKE_KIND_RE.match(rest)
                if m:
                    kind = m.group(1).lower()
                    count = int(m.group(2)) if m.group(2) else None
                    reason = ""
            entries[name] = {"kind": kind, "reason": reason, "count": count}
    except Exception as exc:
        print(f"[Retake] WARNING: could not read retake list {path}: {exc}")
    return entries


def _qc_report_filename(block_folder: Path, block_label: str) -> str:
    """<wafer>_<run timestamp>_qc_report.html instead of a bare "index.html"
    (which says nothing about which run/wafer it is once you have several
    open, or a link to one out of context). Parses the wafer number and the
    run's own trailing _YYYYMMDD_HHMMSS timestamp out of the block name (e.g.
    "w10h31_dye_roi_focus_8NM_20260822_134321" -> "w10_20260822_134321_qc_report.html").
    Falls back to "index.html" for a block name that doesn't match that
    convention, rather than guessing."""
    name = block_label or block_folder.name
    wafer_m = re.search(r"[Ww](\d+)[Hh]\d+", name)
    ts_m = re.search(r"(\d{8}_\d{6})$", name)
    if wafer_m and ts_m:
        return f"w{wafer_m.group(1)}_{ts_m.group(1)}_qc_report.html"
    return "index.html"


def _write_block_index_html(
    block_folder: Path, block_label: str = "", wafer_total: Optional[int] = None,
    review_root: Optional[str | Path] = None,
) -> None:
    """Emit <block_folder>/<wafer>_<timestamp>_qc_report.html (see
    _qc_report_filename).

    wafer_total, when given, is the count of ALL regions defined in the
    imaging JSON (regardless of what this particular batch was filtered/
    scoped to) - shown alongside this batch's own region count so "11
    region(s) in this batch" doesn't get mistaken for "the whole wafer" when
    it's really a partial/retry run of 11 out of e.g. 30 total.

    When region_manifest.json exists (written by focus_map.py's main()
    before/during a run), it drives the row list/order and adds a Status/Log
    column -- so a region that never got a Live QC dashboard at all (e.g. one
    that failed during focus, before acquire_tiles ever ran) still shows up
    instead of being silently absent, with a link straight to its
    console.log and the failure reason inline. The header summary line then
    answers "how many regions remain" (pending + running) at a glance.

    Falls back to the old dashboard-scan-only behavior (scan
    <block_folder>/*/LiveQC/live_qc_dashboard.html) when no manifest is
    present (older run, or a caller that never wrote one).

    review_root, when given, adds the same Accept/Retake/Not-verified
    controls the global wafer report has (see write_wafer_qc_report) - reads
    and writes region_review_status.json at review_root (pass the WAFER
    root, not block_folder, so a decision made from this batch report and
    from the global report stay in sync as one shared file). Omitted
    (None) when this batch report is generated standalone without a known
    wafer root - no review column in that case, same as before this existed."""
    block_folder = Path(block_folder)
    if not block_folder.is_dir():
        return

    review_status = read_region_review_status(Path(review_root) / REGION_REVIEW_STATUS_FILENAME) if review_root else {}

    manifest = _read_region_manifest(block_folder)
    sections: List[Dict[str, Any]] = []

    if manifest is not None:
        for entry in manifest:
            name = str(entry.get("name", "?"))
            folder = str(entry.get("folder") or name)
            status = str(entry.get("status", "pending"))
            detail = entry.get("detail")
            region_dir = block_folder / folder
            live_dir = region_dir / "LiveQC"
            dash_html = live_dir / "live_qc_dashboard.html"
            console_log = region_dir / "console.log"

            sec: Dict[str, Any] = {
                "name": name,
                "status": status,
                "detail": detail,
                "rel_log": str(console_log.relative_to(block_folder)).replace("\\", "/")
                           if console_log.exists() else None,
            }
            if dash_html.exists():
                stats = _read_section_stats_from_csv(live_dir / "live_qc_results.csv")
                timing = _read_section_timing_from_json(live_dir / "live_qc_timing.json")
                sec.update({
                    "rel_html": str(dash_html.relative_to(block_folder)).replace("\\", "/"),
                    "rel_png": str((live_dir / "live_qc_dashboard.png").relative_to(block_folder)).replace("\\", "/")
                               if (live_dir / "live_qc_dashboard.png").exists() else None,
                    **stats,
                    **timing,
                })
            else:
                sec.update({
                    "rel_html": None, "rel_png": None,
                    "n_total": 0, "fails": 0, "outside": 0, "jittery": 0,
                    "median_q": float("nan"), "focus_s": 0.0, "wall_s": 0.0, "n_tiles": 0,
                })
            sections.append(sec)
    else:
        for region_dir in sorted(block_folder.iterdir()):
            if not region_dir.is_dir():
                continue
            live_dir = region_dir / "LiveQC"
            dash_html = live_dir / "live_qc_dashboard.html"
            if not dash_html.exists():
                continue
            stats = _read_section_stats_from_csv(live_dir / "live_qc_results.csv")
            timing = _read_section_timing_from_json(live_dir / "live_qc_timing.json")
            console_log = region_dir / "console.log"
            sections.append({
                "name": region_dir.name,
                "status": "done",
                "detail": None,
                "rel_log": str(console_log.relative_to(block_folder)).replace("\\", "/")
                           if console_log.exists() else None,
                "rel_html": str(dash_html.relative_to(block_folder)).replace("\\", "/"),
                "rel_png": str((live_dir / "live_qc_dashboard.png").relative_to(block_folder)).replace("\\", "/")
                           if (live_dir / "live_qc_dashboard.png").exists() else None,
                **stats,
                **timing,
            })

    total_focus_s = sum(float(s.get("focus_s", 0.0) or 0.0) for s in sections)
    total_wall_s = sum(float(s.get("wall_s", 0.0) or 0.0) for s in sections)
    n_timed = sum(1 for s in sections if float(s.get("focus_s", 0.0) or 0.0) > 0.0
                  or float(s.get("wall_s", 0.0) or 0.0) > 0.0)
    timing_summary_str = (
        f", {n_timed} timed: focus {_fmt_duration(total_focus_s)}, "
        f"imaging {_fmt_duration(total_wall_s)}, total {_fmt_duration(total_focus_s + total_wall_s)}"
        if n_timed > 0 else ""
    )

    status_summary_str = ""
    if manifest is not None:
        counts: Dict[str, int] = {"pending": 0, "running": 0, "done": 0, "failed": 0, "skipped": 0}
        for s in sections:
            st = s.get("status", "pending")
            counts[st] = counts.get(st, 0) + 1
        remaining = counts["pending"] + counts["running"]
        skipped_str = f", {counts['skipped']} skipped (see detail)" if counts["skipped"] else ""
        status_summary_str = (
            f" — {counts['done']} done, {counts['failed']} failed, "
            f"{remaining} remaining ({counts['running']} running, {counts['pending']} pending)"
            f"{skipped_str}"
        )

    rows = []
    review_defaults: Dict[str, Dict[str, str]] = {}
    for s in sections:
        status = s.get("status", "pending")
        label, fg, bg = _REGION_STATUS_LABELS.get(status, (status.title(), "#000", "#eee"))
        detail = s.get("detail")
        status_title = f' title="{_html.escape(str(detail))}"' if detail else ""
        status_chip = f'<span class="chip" style="background:{bg};color:{fg};"{status_title}>{label}</span>'

        png_cell = (
            f'<a href="{_html.escape(s["rel_png"])}" target="_blank">'
            f'<img src="{_html.escape(s["rel_png"])}" alt="" style="height:60px;border:1px solid #ccc;"/></a>'
            if s.get("rel_png") else "—"
        )
        med_q = s.get("median_q", float("nan"))
        med_q_str = "—" if (med_q != med_q) else f'{med_q:.2f}'
        focus_s = float(s.get("focus_s", 0.0) or 0.0)
        wall_s = float(s.get("wall_s", 0.0) or 0.0)
        has_timing = focus_s > 0.0 or wall_s > 0.0
        focus_str = _fmt_duration(focus_s) if has_timing else "—"
        imaging_str = _fmt_duration(wall_s) if has_timing else "—"
        total_str = _fmt_duration(focus_s + wall_s) if has_timing else "—"

        name_cell = (
            f'<a href="{_html.escape(s["rel_html"])}" target="_blank"><strong>{_html.escape(s["name"])}</strong></a>'
            if s.get("rel_html") else f'<strong>{_html.escape(s["name"])}</strong>'
        )
        log_cell = (
            f'<a href="{_html.escape(s["rel_log"])}" target="_blank">console.log</a>'
            if s.get("rel_log") else "—"
        )
        # Shown for any status with a detail note set (failed/skipped reasons,
        # or a "retake requested: ..." note carried through from pending all
        # the way to done - see main()'s manifest_entries construction).
        detail_cell = f'<div class="detail">{_html.escape(str(detail))}</div>' if detail else ""

        review_cell = ""
        if review_root is not None:
            review_cell_html, review_defaults[s["name"]] = _review_cell_html(s["name"], review_status)
            review_cell = f'<td>{review_cell_html}</td>'

        rows.append(
            f'<tr>'
            f'<td>{status_chip}</td>'
            f'<td>{name_cell}{detail_cell}</td>'
            f'<td>{s.get("n_total", 0)}</td>'
            f'<td>{med_q_str}</td>'
            f'<td>{s.get("fails", 0)}</td>'
            f'<td>{focus_str}</td>'
            f'<td>{imaging_str}</td>'
            f'<td>{total_str}</td>'
            f'<td>{log_cell}</td>'
            f'<td style="text-align:center;">{png_cell}</td>'
            f'{review_cell}'
            f'</tr>'
        )

    doc = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>QC Report — {_html.escape(block_label)}</title>
<style>
  body {{ margin:0; font-family: ui-monospace, Menlo, Consolas, monospace; background:#f4f4f6; color:#1c1c1f; }}
  header {{ padding:14px 18px; background:#fff; border-bottom:1px solid #d8d8de; }}
  header h1 {{ margin:0 0 4px 0; font-size:16px; font-weight:600; }}
  header .summary {{ font-size:12px; color:#555; }}
  main {{ padding:14px; }}
  table {{ width:100%; border-collapse:collapse; background:#fff; border:1px solid #d8d8de; border-radius:6px; overflow:hidden; }}
  th, td {{ padding:8px 12px; text-align:left; border-bottom:1px solid #eee; font-size:13px; vertical-align:middle; }}
  th {{ background:#f8f8fa; color:#555; font-weight:600; font-size:12px; }}
  tr:last-child td {{ border-bottom:none; }}
  tr:hover {{ background:#fafafa; }}
  a {{ color:#1d4f9e; text-decoration:none; }}
  a:hover {{ text-decoration:underline; }}
  .empty {{ padding:24px; text-align:center; color:#888; }}
  .chip {{ display:inline-block; padding:2px 8px; border-radius:10px; font-size:11px; font-weight:600; white-space:nowrap; }}
  .detail {{ margin-top:4px; font-size:10px; color:#a33; max-width:340px; white-space:normal; word-break:break-word; }}
{_REVIEW_CONTROLS_CSS if review_root is not None else ""}</style>
</head>
<body>
<header>
  <h1>QC Report — {_html.escape(block_label or block_folder.name)}</h1>
  <div class="summary"><strong>This batch: {len(sections)} region(s)</strong>{
        f" (of {wafer_total} total wafer region(s))" if wafer_total else ""
    } in <code>{_html.escape(str(block_folder))}</code></div>
  {f'<div class="summary">{_html.escape(status_summary_str.lstrip(" —"))}</div>' if status_summary_str else ''}
  {f'<div class="summary">{_html.escape(timing_summary_str.lstrip(", "))}</div>' if timing_summary_str else ''}
{_review_controls_header_html('<code>retake_regions.txt</code> is kept in sync automatically the next time the global (wafer-wide) report is regenerated - it is not written directly by the browser.') if review_root is not None else ""}</header>
<main>
"""
    if sections:
        review_th = "<th>Review (does it actually look in focus?)</th>" if review_root is not None else ""
        doc += (
            '<table>'
            '<thead><tr>'
            '<th>Status</th><th>Section</th><th>mFoVs</th><th>Image Quality</th>'
            '<th title="mFoVs where acquisition/QC failed outright (no valid image data) '
            '- separate from mFoVs that acquired fine but scored poorly on quality">Fails</th>'
            '<th>Focus</th><th>Imaging</th><th>Total</th>'
            f'<th>Log</th><th>Preview</th>{review_th}'
            '</tr></thead><tbody>'
            + "".join(rows)
            + '</tbody></table>'
        )
    else:
        doc += '<div class="empty">No regions planned/found yet.</div>'
    doc += "\n</main>\n"
    if review_root is not None:
        doc += _review_controls_script_html(review_root, review_defaults)
    doc += "</body>\n</html>\n"

    (block_folder / _qc_report_filename(block_folder, block_label)).write_text(doc, encoding="utf-8")


def _find_batch_report_link(batch_folder: Path) -> Optional[str]:
    """Find whatever this batch's own per-batch report is actually named
    (w<N>_<timestamp>_qc_report.html per _qc_report_filename, or the older
    bare index.html for a run that predates that rename) - never assume a
    fixed name, since batches from before/after this convention changed
    coexist under the same wafer folder."""
    candidates = sorted(batch_folder.glob("*_qc_report.html"))
    if candidates:
        return candidates[0].name
    if (batch_folder / "index.html").exists():
        return "index.html"
    return None


_NATSORT_RE = re.compile(r"(\d+)")


def _natural_sort_key(name: str) -> Tuple:
    """'S2R2' < 'S10R2' < 'S27R2' - a plain sorted() on these strings puts
    'S10R2' before 'S2R2' (alphabetical), not what anyone scanning a region
    list expects. Splits into alternating text/number runs and compares
    number runs numerically, so it also naturally handles names with more
    than one embedded number (S<N>R<M>) or none at all."""
    parts = _NATSORT_RE.split(name)
    return tuple(int(p) if p.isdigit() else p for p in parts)


def _scan_one_batch_regions(
    batch_dir: Path, canonical_region_names: Optional[List[str]],
) -> List[Tuple[str, str, Optional[Path], Optional[str]]]:
    """One batch folder's own (name, status, region_dir, detail) rows, from
    its region_manifest.json (or a best-effort fallback for a batch that
    predates it). Shared by scan_wafer_regions (collapses to one best status
    per region) and scan_wafer_regions_all_attempts (keeps every attempt) so
    the two can never disagree about what any one batch actually contains."""
    manifest = _read_region_manifest(batch_dir)
    batch_regions: List[Tuple[str, str, Optional[Path], Optional[str]]] = []

    if manifest is not None:
        for entry in manifest:
            name = str(entry.get("name", "?"))
            folder = str(entry.get("folder") or name)
            status = str(entry.get("status", "pending"))
            detail = entry.get("detail")
            batch_regions.append((name, status, batch_dir / folder, detail))
    else:
        # Fallback for a batch that predates region_manifest.json: any
        # subfolder with a LiveQC dashboard counts as "done"; a plain
        # subfolder that isn't a manifest/report file is just "present"
        # (attempted, but we can't tell success from here). Only trust a
        # subfolder name that matches the canonical list (when we have
        # one) - a wafer's output directory can also contain runs from a
        # completely different tool/stage (e.g. a native ZEN "Auto Focus
        # Map" acquisition, or an older focus-map-only pass with its own
        # numbered-prefix naming) that don't correspond to these region
        # names at all; guessing at those would misattribute or inflate
        # counts rather than actually answer "is this region imaged".
        for sub in batch_dir.iterdir():
            if not sub.is_dir():
                continue
            if canonical_region_names is not None and sub.name not in canonical_region_names:
                continue
            dash = sub / "LiveQC" / "live_qc_dashboard.html"
            status = "done" if dash.exists() else "pending"
            batch_regions.append((sub.name, status, sub, None))

    return batch_regions


def _batch_summary_row(batch_dir: Path, batch_regions: List[Tuple[str, str, Optional[Path], Optional[str]]]) -> Dict[str, Any]:
    n_done = sum(1 for _, s, _, _ in batch_regions if s == "done")
    n_failed = sum(1 for _, s, _, _ in batch_regions if s == "failed")
    report_link = _find_batch_report_link(batch_dir)
    return {
        "name": batch_dir.name,
        "n_total": len(batch_regions),
        "n_done": n_done,
        "n_failed": n_failed,
        "report_link": f"{batch_dir.name}/{report_link}" if report_link else None,
    }


def scan_wafer_regions(
    runs_root: str | Path,
    canonical_region_names: Optional[List[str]] = None,
) -> Tuple[Dict[str, Dict[str, Any]], List[Dict[str, Any]]]:
    """Scan every batch (run) folder directly under runs_root and return the
    BEST status ever seen per region (done beats failed beats
    running/pending/skipped) plus per-batch summary rows.

    Shared by write_wafer_qc_report (the HTML view) and
    compute_regions_to_image (the "what should the next run image" logic) -
    one scan, two consumers, so they can never disagree about what's already
    done. Does a fresh filesystem scan every call, no persisted state.

    Returns (best, batch_rows) where best[region_name] = {"status", "batch",
    "detail", "rel_html", "rel_log"} (rel_* are relative to runs_root, or
    None if that file doesn't exist)."""
    runs_root = Path(runs_root)
    if not runs_root.is_dir():
        return {}, []

    batch_dirs = sorted(
        (p for p in runs_root.iterdir() if p.is_dir()),
        key=lambda p: p.name,
    )

    _RANK = {"done": 3, "failed": 2, "running": 1, "pending": 0, "skipped": 0}
    best: Dict[str, Dict[str, Any]] = {}
    batch_rows: List[Dict[str, Any]] = []

    for batch_dir in batch_dirs:
        batch_regions = _scan_one_batch_regions(batch_dir, canonical_region_names)
        if not batch_regions:
            continue  # not a batch folder at all, or none of its contents match a canonical region

        batch_rows.append(_batch_summary_row(batch_dir, batch_regions))

        for name, status, region_dir, detail in batch_regions:
            rank = _RANK.get(status, -1)
            cur = best.get(name)
            if cur is None or rank > cur["rank"]:
                dash = region_dir / "LiveQC" / "live_qc_dashboard.html" if region_dir else None
                log = region_dir / "console.log" if region_dir else None
                best[name] = {
                    "rank": rank,
                    "status": status,
                    "batch": batch_dir.name,
                    "detail": detail,
                    "rel_html": f"{batch_dir.name}/{dash.relative_to(batch_dir)}".replace("\\", "/")
                                if dash and dash.exists() else None,
                    "rel_log": f"{batch_dir.name}/{log.relative_to(batch_dir)}".replace("\\", "/")
                               if log and log.exists() else None,
                }

    return best, batch_rows


def scan_wafer_regions_all_attempts(
    runs_root: str | Path,
    canonical_region_names: Optional[List[str]] = None,
) -> Dict[str, List[Dict[str, Any]]]:
    """Like scan_wafer_regions, but keeps EVERY attempt per region instead of
    collapsing to the single best one - for a report that shows what
    actually happened batch by batch, not just the final outcome. Attempts
    for a given region are in the order their batches were run (batch
    folders are timestamp-suffixed, so a plain name sort is chronological).

    Returns {region_name: [{"status", "batch", "detail", "rel_html",
    "rel_log"}, ...]} - one dict per attempt, oldest first. A region with no
    attempts in any batch simply doesn't appear as a key."""
    runs_root = Path(runs_root)
    if not runs_root.is_dir():
        return {}

    batch_dirs = sorted(
        (p for p in runs_root.iterdir() if p.is_dir()),
        key=lambda p: p.name,
    )

    attempts: Dict[str, List[Dict[str, Any]]] = {}
    for batch_dir in batch_dirs:
        batch_regions = _scan_one_batch_regions(batch_dir, canonical_region_names)
        for name, status, region_dir, detail in batch_regions:
            dash = region_dir / "LiveQC" / "live_qc_dashboard.html" if region_dir else None
            log = region_dir / "console.log" if region_dir else None
            attempts.setdefault(name, []).append({
                "status": status,
                "batch": batch_dir.name,
                "detail": detail,
                "rel_html": f"{batch_dir.name}/{dash.relative_to(batch_dir)}".replace("\\", "/")
                            if dash and dash.exists() else None,
                "rel_log": f"{batch_dir.name}/{log.relative_to(batch_dir)}".replace("\\", "/")
                           if log and log.exists() else None,
            })

    return attempts


_TIMING_SUMMARY_REGION_ROW_RE = re.compile(r"^(\S+)\s+([\d.]+)\s+[\d.]+%\s*$")


def _read_timing_summary_totals(batch_dir: Path) -> Dict[str, float]:
    """Parse <batch_dir>/timing_summary.txt's per-region "Total (s)" column
    (see focus_map.py's print_final_timing_summary) - the only place the
    region's TRUE total wall-clock time lives. live_qc_timing.json's own
    focus_s + wall_s undercounts it (by however long is spent on ROI/tile-
    plan computation, QC worker/viewer subprocess startup, etc. between the
    two timers - a real, separate gap, not a rounding difference), so this
    is preferred whenever the file exists. Written once, in a `finally`, at
    the end of the whole batch (even a failed/interrupted one) - so it's
    simply absent for a batch that's still actively running; callers should
    fall back to the weaker focus_s+wall_s estimate in that case."""
    path = batch_dir / "timing_summary.txt"
    if not path.is_file():
        return {}
    out: Dict[str, float] = {}
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        m = _TIMING_SUMMARY_REGION_ROW_RE.match(line)
        if m:
            out[m.group(1)] = float(m.group(2))
    return out


def aggregate_batch_timing(batch_dir: str | Path) -> Dict[str, Any]:
    """Sum every region's LiveQC/live_qc_timing.json under one batch folder
    into a per-batch timing summary: focus (AF search + minimize-OL
    convergence), stig (AS search), move (stage move+settle BETWEEN mfov
    tiles during imaging - NOT the per-focus-point stage move, which is
    folded into "focus"), and total imaging time (focus phase + per-tile
    imaging phase, i.e. the region's full wall-clock time).

    focus_af_s/focus_as_s (see focus_map.py's measure_one_focus_point /
    _focus_point_timing_log) don't exist in live_qc_timing.json written
    before that instrumentation was added - a region missing them has its
    whole (unsplit) focus_s counted under "focus" instead, and counted in
    n_unsplit so callers can caveat the split's accuracy for that batch.

    qc_catchup_s is the time spent waiting for the QC worker (per-beam image
    quality scoring) and viewer (region-overview stitch + dashboard render)
    subprocesses to drain their queues and exit AFTER physical imaging
    already finished (see stop_live_qc_runtime in main_live_qc.py) - real
    region time that's otherwise invisible to both focus_s and wall_s, and
    the main reason total_s (from timing_summary.txt) is bigger than
    focus_s + wall_s alone would suggest. 0.0 for a region run without Live
    QC's worker/viewer subprocesses, or one that predates this
    instrumentation - not necessarily "no catch-up happened"."""
    batch_dir = Path(batch_dir)
    out = {
        "focus_s": 0.0, "stig_s": 0.0, "move_s": 0.0, "qc_catchup_s": 0.0, "total_s": 0.0,
        "n_regions": 0, "n_unsplit": 0, "n_total_estimated": 0,
    }
    if not batch_dir.is_dir():
        return out
    summary_totals = _read_timing_summary_totals(batch_dir)
    for region_dir in sorted(batch_dir.iterdir()):
        if not region_dir.is_dir():
            continue
        timing_path = region_dir / "LiveQC" / "live_qc_timing.json"
        if not timing_path.is_file():
            continue
        try:
            t = json.loads(timing_path.read_text(encoding="utf-8"))
        except Exception:
            continue
        out["n_regions"] += 1
        focus_s = float(t.get("focus_s", 0.0) or 0.0)
        af_s = float(t.get("focus_af_s", 0.0) or 0.0)
        as_s = float(t.get("focus_as_s", 0.0) or 0.0)
        wall_s = float(t.get("wall_s", 0.0) or 0.0)
        move_s = float(t.get("move_s", 0.0) or 0.0)
        qc_catchup_s = float(t.get("qc_catchup_s", 0.0) or 0.0)
        if af_s > 0.0 or as_s > 0.0:
            out["focus_s"] += af_s
            out["stig_s"] += as_s
        else:
            out["focus_s"] += focus_s  # pre-instrumentation: unsplit, attributed to focus
            out["n_unsplit"] += 1
        out["move_s"] += move_s
        out["qc_catchup_s"] += qc_catchup_s
        if region_dir.name in summary_totals:
            out["total_s"] += summary_totals[region_dir.name]
        else:
            out["total_s"] += focus_s + wall_s  # underestimate - see _read_timing_summary_totals
            out["n_total_estimated"] += 1
    return out


def write_batch_timing_summary_txt(batch_dir: str | Path) -> Optional[Path]:
    """Write <batch_dir>/qc_timing_summary.txt: a plain-text per-region AND
    aggregate breakdown of focus / stig / move (mfov-to-mfov) / QC catch-up /
    total time - the same figures as the global report's Batches table (see
    aggregate_batch_timing), just readable without a browser and archivable
    alongside the batch's own data.

    Deliberately a different file from focus_map.py's own timing_summary.txt
    (written live, during acquisition, from in-memory state) - this one is a
    read-back-from-disk report that can be (re)generated on demand for any
    batch, including one from before this breakdown existed, and is always
    safe to overwrite (never read by anything else)."""
    batch_dir = Path(batch_dir)
    if not batch_dir.is_dir():
        return None
    summary_totals = _read_timing_summary_totals(batch_dir)

    rows: List[Dict[str, Any]] = []
    for region_dir in sorted(batch_dir.iterdir()):
        if not region_dir.is_dir():
            continue
        timing_path = region_dir / "LiveQC" / "live_qc_timing.json"
        if not timing_path.is_file():
            continue
        try:
            t = json.loads(timing_path.read_text(encoding="utf-8"))
        except Exception:
            continue
        focus_s = float(t.get("focus_s", 0.0) or 0.0)
        af_s = float(t.get("focus_af_s", 0.0) or 0.0)
        as_s = float(t.get("focus_as_s", 0.0) or 0.0)
        wall_s = float(t.get("wall_s", 0.0) or 0.0)
        move_s = float(t.get("move_s", 0.0) or 0.0)
        qc_catchup_s = float(t.get("qc_catchup_s", 0.0) or 0.0)
        split = af_s > 0.0 or as_s > 0.0
        total_s = summary_totals.get(region_dir.name)
        estimated = total_s is None
        if total_s is None:
            total_s = focus_s + wall_s
        rows.append({
            "name": region_dir.name,
            "focus_s": af_s if split else focus_s,
            "stig_s": as_s if split else 0.0,
            "split": split,
            "move_s": move_s,
            "qc_catchup_s": qc_catchup_s,
            "total_s": total_s,
            "estimated": estimated,
        })

    if not rows:
        return None

    grand_total = sum(r["total_s"] for r in rows)

    def _pct(x: float) -> float:
        return (x / grand_total * 100.0) if grand_total > 0 else 0.0

    lines = []
    lines.append("=" * 88)
    lines.append(f"[QC Timing] {batch_dir.name} - {len(rows)} region(s) - "
                 f"grand total = {grand_total:.1f}s ({grand_total / 60:.1f} min)")
    lines.append("-" * 88)
    lines.append(f"{'Region':<12}{'Focus':>10}{'Stig':>10}{'Move':>10}{'QCcatchup':>12}{'Total':>10}{'% batch':>10}")
    for r in rows:
        flags = ""
        if not r["split"]:
            flags += "*"
        if r["estimated"]:
            flags += "^"
        lines.append(
            f"{r['name']:<12}{r['focus_s']:>10.1f}{r['stig_s']:>10.1f}{r['move_s']:>10.1f}"
            f"{r['qc_catchup_s']:>12.1f}{r['total_s']:>10.1f}{_pct(r['total_s']):>9.1f}%{flags}"
        )
    lines.append("-" * 88)
    total_focus = sum(r["focus_s"] for r in rows)
    total_stig = sum(r["stig_s"] for r in rows)
    total_move = sum(r["move_s"] for r in rows)
    total_catchup = sum(r["qc_catchup_s"] for r in rows)
    lines.append("Aggregate breakdown (all regions combined):")
    lines.append(f"  focus             : {total_focus:>10.1f}s ({_pct(total_focus):5.1f}%)")
    lines.append(f"  stig              : {total_stig:>10.1f}s ({_pct(total_stig):5.1f}%)")
    lines.append(f"  move (mfov→mfov)  : {total_move:>10.1f}s ({_pct(total_move):5.1f}%)")
    lines.append(f"  QC catch-up       : {total_catchup:>10.1f}s ({_pct(total_catchup):5.1f}%)")
    lines.append(f"  total imaging time: {grand_total:>10.1f}s")
    if any(not r["split"] for r in rows):
        lines.append("-" * 88)
        lines.append("* region predates the focus/stig timing split - its combined focus+stig time is shown under Focus")
    if any(r["estimated"] for r in rows):
        lines.append("^ region has no timing_summary.txt yet (batch still running, or predates it) - "
                      "its total is estimated as focus+imaging time only and may undercount real elapsed time")
    lines.append("=" * 88)

    out_path = batch_dir / "qc_timing_summary.txt"
    out_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return out_path


def compute_regions_to_image(
    runs_root: str | Path,
    canonical_region_names: List[str],
    retake_list_path: Optional[str | Path] = None,
) -> Tuple[List[str], Dict[str, str]]:
    """What should the NEXT run actually image: every canonical region that
    isn't already successfully done in any prior batch under runs_root,
    plus any region explicitly flagged FULL in retake_list_path regardless of
    prior "done" status. Preserves canonical_region_names' order.

    A "partial" retake entry (see read_retake_list) never forces a whole
    region back into this list - those handful of mfovs are re-imaged by
    pointing a separate run at the wafer retake JSON directly (see
    scan_partial_retakes), not by re-imaging the entire original region.

    Returns (region_names_to_image, retake_reasons) - retake_reasons is
    {name: reason} for just the FULL-retake names that were explicitly
    flagged (a region that's simply never-yet-imaged is in the first list
    but not this dict, since it wasn't a retake request)."""
    best, _ = scan_wafer_regions(runs_root, canonical_region_names)
    retake_entries = read_retake_list(retake_list_path) if retake_list_path else {}
    full_retake_reasons = {
        name: entry.get("reason", "") for name, entry in retake_entries.items()
        if entry.get("kind", "full") == "full"
    }

    to_image = []
    for name in canonical_region_names:
        is_done = best.get(name, {}).get("status") == "done"
        if name in full_retake_reasons or not is_done:
            to_image.append(name)
    return to_image, {n: r for n, r in full_retake_reasons.items() if n in canonical_region_names}


def write_wafer_qc_report(
    runs_root: str | Path,
    canonical_region_names: Optional[List[str]] = None,
    wafer_label: str = "",
    retake_list_path: Optional[str | Path] = None,
    imaging_json_path: Optional[str | Path] = None,
    partial_retake_json_path: Optional[str | Path] = None,
    partial_retake_output_root: Optional[str | Path] = None,
) -> Optional[Path]:
    """Emit <runs_root>/w<N>_global_qc_report.html (or global_qc_report.html
    if a wafer number can't be resolved): one report aggregating EVERY batch
    (run) folder directly under runs_root, since a wafer is routinely imaged
    across several separate runs (a full pass, then retries of whatever
    failed) rather than in one shot.

    Does a fresh filesystem scan every call - no persisted state - so it's
    safe to call standalone/on demand (not just from an active run) to check
    "have we imaged everything yet" at any time.

    For each region, picks the BEST status ever seen across all batches
    (done beats failed beats pending/running/never-attempted) and links to
    the batch that achieved it. When canonical_region_names is given (the
    full region list from the imaging JSON, regardless of what any one batch
    was filtered/scoped to), regions never attempted in ANY batch still show
    up as "Not started" instead of silently not appearing at all - that's
    the whole point versus just unioning what happened to run.

    Also reflects PARTIAL-region retakes (specific mfovs flagged via a
    region dashboard's Retake selection panel, tracked in a separate wafer
    retake JSON - see scan_partial_retakes) alongside whole-region
    Accept/Retake/Not-verified review. By default, partial retakes are
    aggregated across every batch's own "<batch>_retake.json" under
    runs_root (see scan_partial_retakes_for_wafer / _write_dashboard_html's
    per-batch Retake selection panel); pass partial_retake_json_path to
    instead read one single explicit file (e.g. the older
    resolve_partial_retake_json_path convention).
    partial_retake_output_root defaults to <runs_root>/partial_retakes (see
    focus_map.py's auto-routing for a partial-retake batch's own output).
    """
    runs_root = Path(runs_root)
    if not runs_root.is_dir():
        return None

    best, batch_rows = scan_wafer_regions(runs_root, canonical_region_names)
    wafer_review_status = read_region_review_status(runs_root / REGION_REVIEW_STATUS_FILENAME)
    review_status = _merged_region_review_status(runs_root, best, wafer_review_status)

    if partial_retake_json_path is not None:
        # Explicit override (or the old single-wafer-file convention, still
        # honored if a caller asks for it by name) - read just that one file.
        partial_retakes_pending = scan_partial_retakes(partial_retake_json_path)
    else:
        # Default: each batch's own dashboards grant access to and save
        # directly into THAT batch's folder (see _write_dashboard_html) -
        # so aggregate every batch's own "<batch>_retake.json" instead of
        # looking for one shared wafer-wide file.
        partial_retakes_pending = scan_partial_retakes_for_wafer(runs_root)

    partial_output_root = Path(partial_retake_output_root) if partial_retake_output_root else (runs_root / "partial_retakes")
    all_sub_names = sorted(n for names in partial_retakes_pending.values() for n in names)
    partial_best, _ = scan_wafer_regions(partial_output_root, all_sub_names) if all_sub_names else ({}, [])

    # Full retake overrides display status (even over "done") - the whole
    # point is that "done" isn't the end of the story for a region someone
    # flagged as having a problem. A partial retake does NOT override status
    # (the region's own imaging may well be "done" and fine except for the
    # specific flagged mfovs) - it's surfaced as its own column instead.
    retake_path = Path(retake_list_path) if retake_list_path else (runs_root / "retake_regions.txt")
    sync_retake_list(review_status, partial_retakes_pending, retake_path)
    retake_entries = read_retake_list(retake_path)
    for name, entry in retake_entries.items():
        if name in best and entry.get("kind", "full") == "full":
            best[name]["status"] = "retake"
            best[name]["detail"] = entry.get("reason") or best[name].get("detail") or "Retake requested"

    all_names = list(canonical_region_names) if canonical_region_names else list(best.keys())
    for name in best:
        if name not in all_names:
            all_names.append(name)  # a region that ran but isn't in the canonical list - still show it
    # Ascending numeric order (per the operator, 2026-09-04) regardless of
    # source - a plain string sort puts "S10R2" before "S2R2", and even the
    # canonical list from the imaging JSON isn't guaranteed to already be in
    # this order.
    all_names.sort(key=_natural_sort_key)

    all_attempts = scan_wafer_regions_all_attempts(runs_root, canonical_region_names)

    n_imaged = sum(1 for n in all_names if best.get(n, {}).get("status") == "done")
    pct = (100.0 * n_imaged / len(all_names)) if all_names else 0.0
    remaining_names = [n for n in all_names if best.get(n, {}).get("status") != "done"]
    remaining_html = (
        "".join(f'<span class="chip" style="background:#eee;color:#666;">{_html.escape(n)}</span> '
                for n in remaining_names)
        if remaining_names else '<span style="color:#2e8b3d;">none - every region imaged</span>'
    )

    wafer_m = re.search(r"[Ww](\d+)[Hh]\d+", wafer_label or runs_root.name)
    filename = f"w{wafer_m.group(1)}_global_qc_report.html" if wafer_m else "global_qc_report.html"

    review_defaults = {}
    region_rows = []
    for name in all_names:
        info = best.get(name)
        attempts = all_attempts.get(name) or []
        retake_badge = (
            ' <span class="chip" style="background:#8e44ad;color:#fff;">Retake requested</span>'
            if info and info.get("status") == "retake" else ""
        )
        name_cell = f'<strong>{_html.escape(name)}</strong>{retake_badge}'
        review_cell, review_defaults[name] = _review_cell_html(name, review_status)
        avgq_val = None
        if info and info.get("rel_html"):
            csv_path = runs_root / Path(info["rel_html"]).parent / "live_qc_results.csv"
            mq = _read_section_stats_from_csv(csv_path).get("median_q", float("nan"))
            if mq == mq:  # not NaN
                avgq_val = mq
        avgq_cell = f'{avgq_val:.2f}' if avgq_val is not None else "—"
        partial_names = partial_retakes_pending.get(name) or []
        if partial_names:
            n_done = sum(1 for sn in partial_names if partial_best.get(sn, {}).get("status") == "done")
            partial_cell = (
                f'<span class="chip" style="background:#8e44ad;color:#fff;">Partial retake</span>'
                f'<div class="detail" style="color:#555;">{n_done}/{len(partial_names)} mfov area(s) imaged: '
                f'{_html.escape(", ".join(partial_names))}</div>'
            )
        else:
            partial_cell = "—"

        if not attempts:
            # Never attempted in any batch - one placeholder row, no attempt detail to show.
            label, fg, bg = _REGION_STATUS_LABELS.get("not_started", ("Not started", "#666", "#eee"))
            region_rows.append(
                f'<tr><td>{name_cell}</td>'
                f'<td>—</td><td><span class="chip" style="background:{bg};color:{fg};">{label}</span></td>'
                f'<td>—</td><td>—</td>'
                f'<td class="avgq-col">{avgq_cell}</td>'
                f'<td>{partial_cell}</td><td>{review_cell}</td></tr>'
            )
            continue

        # One row per attempt (first region attempt first, most recent last -
        # batches are scanned in chronological/timestamp order), grouped
        # under the region via rowspan on the region/partial-retake/review
        # cells, which apply to the region as a whole, not any one attempt.
        n_attempts = len(attempts)
        for i, att in enumerate(attempts, start=1):
            a_label, a_fg, a_bg = _REGION_STATUS_LABELS.get(att["status"], (att["status"], "#666", "#eee"))
            detail = att.get("detail")
            detail_cell = f'<div class="detail">{_html.escape(str(detail))}</div>' if detail else ""
            view_cell = (
                f'<a href="{_html.escape(att["rel_html"])}" target="_blank">dashboard</a>'
                if att.get("rel_html") else "—"
            )
            log_cell = (
                f'<a href="{_html.escape(att["rel_log"])}" target="_blank">console.log</a>'
                if att.get("rel_log") else "—"
            )
            row = "<tr>"
            if i == 1:
                row += f'<td rowspan="{n_attempts}">{name_cell}</td>'
            row += (
                f'<td>attempt {i}/{n_attempts} — {_html.escape(att["batch"])}</td>'
                f'<td><span class="chip" style="background:{a_bg};color:{a_fg};">{a_label}</span>{detail_cell}</td>'
                f'<td>{view_cell}</td><td>{log_cell}</td>'
            )
            if i == 1:
                row += f'<td class="avgq-col" rowspan="{n_attempts}">{avgq_cell}</td>'
                row += f'<td rowspan="{n_attempts}">{partial_cell}</td><td rowspan="{n_attempts}">{review_cell}</td>'
            row += "</tr>"
            region_rows.append(row)

    batch_table_rows = []
    any_unsplit_timing = False
    for b in batch_rows:
        link = (
            f'<a href="{_html.escape(b["report_link"])}" target="_blank"><strong>{_html.escape(b["name"])}</strong></a>'
            if b["report_link"] else f'<strong>{_html.escape(b["name"])}</strong>'
        )
        timing = aggregate_batch_timing(runs_root / b["name"])
        try:
            write_batch_timing_summary_txt(runs_root / b["name"])
        except Exception as exc:
            print(f"[GlobalReport] WARNING: qc_timing_summary.txt write failed for {b['name']}: {exc}")
        if timing["n_regions"] > 0:
            focus_cell = _fmt_duration(timing["focus_s"])
            if timing["n_unsplit"] > 0:
                any_unsplit_timing = True
                focus_cell += f'<span title="{timing["n_unsplit"]} of {timing["n_regions"]} region(s) in this batch predate the focus/stig timing split - their combined focus+stig time is counted here under Focus">*</span>'
            stig_cell = _fmt_duration(timing["stig_s"])
            move_cell = _fmt_duration(timing["move_s"])
            catchup_cell = (
                _fmt_duration(timing["qc_catchup_s"]) if timing["qc_catchup_s"] > 0.0 else "—"
            )
            total_cell = _fmt_duration(timing["total_s"])
            if timing["n_total_estimated"] > 0:
                any_unsplit_timing = True
                total_cell += (
                    f'<span title="{timing["n_total_estimated"]} of {timing["n_regions"]} region(s) in this '
                    'batch have no timing_summary.txt yet (batch still running, or predates it) - their total '
                    'is estimated as focus+imaging time only and may undercount real elapsed time">*</span>'
                )
        else:
            focus_cell = stig_cell = move_cell = catchup_cell = total_cell = "—"
        batch_table_rows.append(
            f'<tr><td>{link}</td><td>{b["n_total"]}</td><td>{b["n_done"]}</td><td>{b["n_failed"]}</td>'
            f'<td>{focus_cell}</td><td>{stig_cell}</td><td>{move_cell}</td><td>{catchup_cell}</td><td>{total_cell}</td></tr>'
        )
    unsplit_note = (
        ' <span style="font-weight:normal;color:#777;">(* = see tooltip on the marked cell)</span>'
        if any_unsplit_timing else ""
    )

    doc = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Global QC Report — {_html.escape(wafer_label or runs_root.name)}</title>
<style>
  body {{ margin:0; font-family: ui-monospace, Menlo, Consolas, monospace; background:#f4f4f6; color:#1c1c1f; }}
  header {{ padding:14px 18px; background:#fff; border-bottom:1px solid #d8d8de; }}
  header h1 {{ margin:0 0 4px 0; font-size:16px; font-weight:600; }}
  header .summary {{ font-size:12px; color:#555; }}
  .progress {{ background:#e6e6e6; border-radius:6px; height:10px; width:100%; max-width:420px; overflow:hidden; margin-top:6px; }}
  .progress > div {{ background:#2e8b3d; height:100%; }}
  main {{ padding:14px; }}
  h2 {{ font-size:13px; color:#333; margin:18px 0 6px 0; }}
  table {{ width:100%; border-collapse:collapse; background:#fff; border:1px solid #d8d8de; border-radius:6px; overflow:hidden; margin-bottom:20px; }}
  th, td {{ padding:8px 12px; text-align:left; border-bottom:1px solid #eee; font-size:13px; vertical-align:middle; }}
  th {{ background:#f8f8fa; color:#555; font-weight:600; font-size:12px; }}
  tr:last-child td {{ border-bottom:none; }}
  tr:hover {{ background:#fafafa; }}
  a {{ color:#1d4f9e; text-decoration:none; }}
  a:hover {{ text-decoration:underline; }}
  .chip {{ display:inline-block; padding:2px 8px; border-radius:10px; font-size:11px; font-weight:600; white-space:nowrap; }}
  .detail {{ margin-top:4px; font-size:10px; color:#a33; max-width:340px; white-space:normal; word-break:break-word; }}
  body.hide-avgq .avgq-col {{ display:none; }}
{_REVIEW_CONTROLS_CSS}</style>
</head>
<body>
<header>
  <h1>Global QC Report — {_html.escape(wafer_label or runs_root.name)}</h1>
  <div class="summary"><strong>{n_imaged}/{len(all_names)} region(s) imaged ({pct:.0f}%)</strong> across {len(batch_rows)} batch(es) in <code>{_html.escape(str(runs_root))}</code></div>
  <div class="progress"><div style="width:{pct:.0f}%;"></div></div>
  <div class="summary" style="margin-top:8px;"><strong>Remaining ({len(remaining_names)}):</strong> {remaining_html}</div>
  <div class="summary" style="margin-top:8px;">
    <button type="button" id="btn-toggle-avgq">Hide avg quality</button>
  </div>
{_review_controls_header_html('<code>retake_regions.txt</code> is kept in sync automatically the next time this report is regenerated - it is not written directly by the browser.')}</header>
<main>
<h2>Regions ({len(all_names)})</h2>
<table><thead><tr><th>Region</th><th>Attempt</th><th>Status</th><th>View</th><th>Log</th><th class="avgq-col">Avg quality</th><th>Partial retake</th><th>Review (does it actually look in focus?)</th></tr></thead>
<tbody>{"".join(region_rows) if region_rows else '<tr><td colspan="8">No regions found.</td></tr>'}</tbody></table>
<h2>Batches ({len(batch_rows)}){unsplit_note}</h2>
<table><thead><tr><th>Batch</th><th>Regions</th><th>Done</th><th>Failed</th>
<th>Focus</th><th>Stig</th><th>Move (mfov→mfov)</th><th>QC catch-up</th><th>Total imaging time</th></tr></thead>
<tbody>{"".join(batch_table_rows) if batch_table_rows else '<tr><td colspan="9">No batches found.</td></tr>'}</tbody></table>
</main>
{_review_controls_script_html(runs_root, review_defaults)}
<script>
(function() {{
  const btn = document.getElementById('btn-toggle-avgq');
  if (!btn) return;
  btn.addEventListener('click', () => {{
    const hidden = document.body.classList.toggle('hide-avgq');
    btn.textContent = hidden ? 'Show avg quality' : 'Hide avg quality';
  }});
}})();
</script>
</body>
</html>
"""
    out_path = runs_root / filename
    out_path.write_text(doc, encoding="utf-8")
    return out_path


def _load_imaging_json_for_region(
    imaging_json_path: Optional[str],
    region_name: Optional[str],
) -> Tuple[Optional[str], Optional[Dict[str, Any]]]:
    """Returns (original_region_xml, wafer_metadata_without_regions). Both None on failure."""
    if not imaging_json_path or not region_name:
        return None, None
    try:
        with open(imaging_json_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return None, None
    region_xml = None
    needle = f'Name="{region_name}"'
    for s in data.get("Regions", []):
        if needle in s:
            region_xml = s
            break
    metadata = {k: v for k, v in data.items() if k != "Regions"}
    return region_xml, metadata


def _write_dashboard_html(
    out_path: Path,
    region_label: str,
    block_label: str,
    layout: List[Dict[str, Any]],
    results_by_tile: Dict[int, Dict[str, Any]],
    radius: float,
    cmap_focus: Any, norm_focus: Any,
    cmap_stig: Any, norm_stig: Any,
    cmap_outside: Any, norm_outside: Any,
    cmap_jitter: Any, norm_jitter: Any,
    cmap_arrow: Any,
    stig_dmax: float,
    jitter_dmax: float,
    counters: Dict[str, Any],
    region_name: Optional[str] = None,
    imaging_json_path: Optional[str] = None,
    low_quality_threshold: float = 10.0,
    high_quality_threshold: float = 30.0,
    region_overview: Optional[Dict[str, Any]] = None,
    focus_elapsed_s: float = 0.0,
    timing_info: Optional[Dict[str, float]] = None,
    beam_quality_values: Optional[Dict[int, List[float]]] = None,
    block_folder_path: Optional[str] = None,
    tile_bmp_urls_cache: Optional[Dict[int, List[str]]] = None,
) -> None:
    if not layout:
        return

    xs = [float(r["x_um"]) for r in layout]
    ys = [float(r["y_um"]) for r in layout]
    idxs = [int(r["tile_idx"]) for r in layout]

    # Pre-scan each COMPLETED tile's folder for BMPs so the click handler can
    # display them inverted (the raw acquisitions display black/white
    # reversed in normal viewers; this is a presentation-only flip, the .bmp
    # files on disk are NOT modified). This is called on every periodic save
    # (every save_every tiles) for the whole region, so re-globbing every
    # already-resolved tile's folder every time made this scale like
    # n_tiles^2/save_every on a real run (0.09s -> 2.48s across one region's
    # saves, on a network-drive share where each glob() has real latency) -
    # the same class of bug as RegionOverviewAccumulator's, just here via
    # directory listings instead of image compositing. tile_bmp_urls_cache
    # (owned by the caller, persisted across calls within one region) lets
    # this only glob tiles not already resolved.
    tile_bmp_urls: Dict[int, List[str]] = (
        tile_bmp_urls_cache if tile_bmp_urls_cache is not None else {}
    )
    for _ti in idxs:
        _ti = int(_ti)
        if _ti in tile_bmp_urls:
            continue
        _r = results_by_tile.get(_ti)
        if not _r:
            continue
        _folder = _r.get("tile_folder")
        if not _folder:
            continue
        try:
            _bmps = sorted(str(p) for p in Path(_folder).glob("*.bmp"))
            if _bmps:
                tile_bmp_urls[_ti] = [_path_to_file_url(b) for b in _bmps]
        except OSError:
            continue

    def _final_median_and_thresh(values: List[float]) -> Tuple[float, float]:
        if len(values) < 3:
            return 0.0, float("inf")
        arr = np.asarray(values, dtype=np.float64)
        med = float(np.median(arr))
        if len(values) < 5:
            return med, float("inf")
        mad = float(np.median(np.abs(arr - med)))
        return med, 3.0 * 1.4826 * max(mad, 1e-9)

    def _is_baseline_eligible(r: Dict[str, Any]) -> bool:
        return (
            str(r.get("status_category", "ok")) == "ok"
            and not bool(r.get("is_blank_outside_roi", False))
        )

    baseline_results = [r for r in results_by_tile.values() if _is_baseline_eligible(r)]

    stig_vals_all = [float(r.get("stig_anisotropy", 0.0) or 0.0) for r in baseline_results]
    stig_med_final, stig_outlier_threshold = _final_median_and_thresh(stig_vals_all)
    stig_outlier_total = sum(
        1 for v in stig_vals_all if abs(v - stig_med_final) > stig_outlier_threshold
    )

    jitter_vals_all = [float(r.get("jitter_score_med", 0.0) or 0.0) for r in baseline_results]
    jitter_med_final, jitter_outlier_threshold = _final_median_and_thresh(jitter_vals_all)
    jitter_outlier_total = sum(
        1 for v in jitter_vals_all if abs(v - jitter_med_final) > jitter_outlier_threshold
    )

    pad = 3.0 * radius
    minx = min(xs) - pad
    maxx = max(xs) + pad
    miny = min(ys) - pad
    maxy = max(ys) + pad
    width = maxx - minx
    height = maxy - miny

    angles = [math.radians(30.0 + 60.0 * i) for i in range(6)]
    hex_dx = [radius * math.cos(a) for a in angles]
    hex_dy = [radius * math.sin(a) for a in angles]

    def _y_svg(y: float) -> float:
        return (miny + maxy) - y

    def _hex_points(cx: float, cy: float) -> str:
        cy_s = _y_svg(cy)
        return " ".join(f"{cx + dx:.2f},{cy_s - dy:.2f}" for dx, dy in zip(hex_dx, hex_dy))

    def _tooltip(result: Dict[str, Any]) -> str:
        beams = result.get("selected_beams") or []
        if isinstance(beams, list):
            beams_str = ",".join(str(b) for b in beams)
        else:
            beams_str = str(beams)
        return (
            f"tile {result.get('tile_idx')}\n"
            f"xy=({float(result.get('x_um', 0)):.1f}, {float(result.get('y_um', 0)):.1f}) µm\n"
            f"quality={float(result.get('quality', 0)):.2f}\n"
            f"stig=({float(result.get('stig_x_med', 0)):.2f}, {float(result.get('stig_y_med', 0)):.2f})  "
            f"anis={float(result.get('stig_anisotropy', 0)):.2f}\n"
            f"sat_med={float(result.get('pct_saturation_med', 0)):.2f}%  "
            f"jitter={float(result.get('jitter_score_med', 0)):.4f}\n"
            f"status={result.get('status_category', '?')}  "
            f"blank={bool(result.get('is_blank_outside_roi', False))}  "
            f"jittery={bool(result.get('is_jittery', False))}\n"
            f"beams=[{beams_str}]  n_used={result.get('n_images_used', 0)}\n"
            f"folder: {result.get('tile_folder', '')}"
        )

    def _panel_svg(panel_id: str) -> str:
        out = [
            f'<svg viewBox="{minx:.2f} {miny:.2f} {width:.2f} {height:.2f}" '
            f'preserveAspectRatio="xMidYMid meet">'
        ]
        for tile_idx, x, y in zip(idxs, xs, ys):
            result = results_by_tile.get(tile_idx)
            stroke = "#555"
            stroke_width = 0.5
            stroke_dash = ""
            tooltip_text = f"tile {tile_idx}  pending"
            href = ""

            if result is None:
                fill = _rgba_to_hex(_PENDING_FILL)
            else:
                tooltip_text = _tooltip(result)
                href = str(result.get("tile_folder") or "")
                if panel_id == "focus":
                    fill = _rgba_to_hex(cmap_focus(norm_focus(float(result.get("quality", -100.0) or -100.0))))
                elif panel_id == "stig":
                    raw = float(result.get("stig_anisotropy", 0.0) or 0.0)
                    dev = raw - stig_med_final
                    fill = _rgba_to_hex(cmap_stig(norm_stig(dev)))
                    if abs(dev) > stig_outlier_threshold:
                        stroke = "#000"
                        stroke_width = 1.6
                        stroke_dash = ' stroke-dasharray="3,2"'
                elif panel_id == "status":
                    sc = str(result.get("status_category", "ok") or "ok")
                    fill = _rgba_to_hex(_STATUS_COLORS.get(sc, _STATUS_COLORS["ok"]))
                elif panel_id == "outside":
                    s = min(float(result.get("pct_saturation_med", 0.0) or 0.0), 100.0)
                    fill = _rgba_to_hex(cmap_outside(norm_outside(s)))
                    if bool(result.get("is_blank_outside_roi", False)):
                        stroke = "#000"
                        stroke_width = 1.6
                        stroke_dash = ' stroke-dasharray="3,2"'
                elif panel_id == "jitter":
                    raw_j = float(result.get("jitter_score_med", 1.0) or 1.0)
                    fill = _rgba_to_hex(cmap_jitter(norm_jitter(raw_j)))
                    if abs(raw_j - jitter_med_final) > jitter_outlier_threshold:
                        stroke = "#000"
                        stroke_width = 1.6
                        stroke_dash = ' stroke-dasharray="3,2"'
                else:
                    fill = _rgba_to_hex(_PENDING_FILL)

            pts = _hex_points(x, y)
            poly = (
                f'<polygon class="hex" points="{pts}" fill="{fill}" '
                f'stroke="{stroke}" stroke-width="{stroke_width}"{stroke_dash}>'
                f'<title>{_html.escape(tooltip_text)}</title>'
                f'</polygon>'
            )
            if href:
                href_url = _path_to_file_url(href)
                poly = (
                    f'<a href="{href_url}" target="_blank" '
                    f'data-path="{_html.escape(href)}" '
                    f'data-tile="{tile_idx}">{poly}</a>'
                )
            out.append(poly)

        if panel_id == "stig":
            arrow_data_len = max(radius * 0.9, 1e-6)
            scale = stig_dmax / arrow_data_len
            arrow_norm_max = math.hypot(stig_dmax, stig_dmax)
            for tile_idx, x, y in zip(idxs, xs, ys):
                result = results_by_tile.get(tile_idx)
                if result is None:
                    continue
                u = max(float(result.get("stig_x_med", 0.0) or 0.0), 0.0)
                v = max(float(result.get("stig_y_med", 0.0) or 0.0), 0.0)
                mag = math.hypot(u, v)
                if mag <= 1.0:
                    continue
                cy_s = _y_svg(y)
                ex = x + (u / scale)
                ey = cy_s - (v / scale)
                arrow_color = _rgba_to_hex(cmap_arrow(min(mag / arrow_norm_max, 1.0)))
                out.append(
                    f'<line x1="{x:.2f}" y1="{cy_s:.2f}" x2="{ex:.2f}" y2="{ey:.2f}" '
                    f'stroke="{arrow_color}" stroke-width="{radius * 0.07:.2f}" '
                    f'marker-end="url(#arrow)" opacity="0.9" pointer-events="none"/>'
                )

        out.append("</svg>")
        return "\n".join(out)

    def _overview_panel_html() -> str:
        """Real stitched-tile overview (see qc_overview.RegionOverviewAccumulator)
        with a translucent focus-quality tint + per-MFOV median-quality label +
        click-to-open-folder overlay, using the exact pixel footprint each
        MFOV's tiles actually landed at in that image (region_overview["bboxes"]).

        The saved overview PNG is in raw image_coordinates.txt pixel space,
        which runs opposite to the stage x_um/y_um convention the ①②③ hex
        panels use on BOTH axes (measured directly against this region's own
        CSV vs. image_coordinates.txt: corr(stage_x, img_x) = -1.0,
        corr(stage_y, img_y) = +1.0, and the hex panels flip y but not x) -
        so a plain unrotated placement shows the tissue mirrored top<->bottom
        AND left<->right relative to every other panel on this page. Rotating
        180deg about the image center corrects both axes at once. Only the
        <image> pixels are rotated via an SVG transform; the MFOV bbox
        overlay/labels are placed using their already-flipped coordinates
        (W-x, H-y) directly, so text stays upright and correctly aligned."""
        def _unavailable() -> str:
            # Distinguish "acquisition still running, nothing done yet" (routine)
            # from "tiles completed but the stitcher got nothing to work with"
            # (usually every saved beam image failed to decode - e.g. truncated/
            # corrupted BMPs from a hardware fault mid-acquisition) - the latter
            # is worth a loud warning since it otherwise looks identical to "not
            # ready yet" and can hide a real acquisition problem.
            if results_by_tile:
                return (
                    '<div class="warn-banner">&#9888; Region overview unavailable: no beam images could be '
                    f'stitched from {len(results_by_tile)} completed tile(s). This usually means the saved '
                    'images failed to decode (truncated/corrupted on disk) rather than the overview simply '
                    'not being ready yet - check console.log for a hardware/stage/focus fault around this '
                    'region\'s acquisition time.</div>'
                )
            return '<p style="font-size:12px;color:#777;margin:0;">Region overview not yet available.</p>'

        if not region_overview or not region_overview.get("path"):
            return _unavailable()
        img_name = Path(str(region_overview["path"])).name
        W = int(region_overview.get("width", 0) or 0)
        H = int(region_overview.get("height", 0) or 0)
        if W <= 0 or H <= 0:
            return _unavailable()
        bboxes = region_overview.get("bboxes") or {}
        stroke_w = max(1, int(round(0.0015 * max(W, H))))
        parts = [
            f'<svg viewBox="0 0 {W} {H}" preserveAspectRatio="xMidYMid meet">',
            f'<image href="{_html.escape(img_name)}" x="0" y="0" width="{W}" height="{H}" '
            f'transform="rotate(180 {W / 2.0:.1f} {H / 2.0:.1f})"/>',
        ]
        for tile_idx, bbox in bboxes.items():
            tile_idx = int(tile_idx)
            bx0, by0, bx1, by1 = (int(v) for v in bbox)
            # Same 180deg rotation applied to the bbox coordinates themselves.
            x0, x1 = W - bx1, W - bx0
            y0, y1 = H - by1, H - by0
            result = results_by_tile.get(tile_idx)
            if result is None:
                continue
            q = float(result.get("quality", -100.0) or -100.0)
            fill = _rgba_to_hex(cmap_focus(norm_focus(q)))
            href = str(result.get("tile_folder") or "")
            rect = (
                f'<rect class="qtile" data-opacity="0.35" x="{x0}" y="{y0}" '
                f'width="{max(1, x1 - x0)}" height="{max(1, y1 - y0)}" '
                f'fill="{fill}" fill-opacity="0.35" stroke="#000" stroke-opacity="0.25" '
                f'stroke-width="{stroke_w}">'
                f'<title>{_html.escape(_tooltip(result))}</title>'
                f'</rect>'
            )
            label = (
                f'<text class="qlabel" x="{(x0 + x1) / 2.0:.1f}" y="{(y0 + y1) / 2.0:.1f}" '
                f'text-anchor="middle" dominant-baseline="middle" '
                f'font-size="{max(10, int(round(0.22 * (x1 - x0)))):d}" '
                f'fill="#fff" stroke="#000" stroke-width="2" paint-order="stroke" '
                f'pointer-events="none">{q:.1f}</text>'
            )
            if href:
                href_url = _path_to_file_url(href)
                rect = (
                    f'<a href="{href_url}" target="_blank" '
                    f'data-path="{_html.escape(href)}" data-tile="{tile_idx}">{rect}</a>'
                )
            parts.append(rect)
            parts.append(label)
        parts.append("</svg>")
        return (
            '<label style="font-size:11px;display:block;margin-bottom:6px;">'
            '<input type="checkbox" id="toggle-overview-quality" checked> '
            'Show focus-quality overlay</label>'
            + "\n".join(parts)
        )

    def _beam_quality_panel_html() -> str:
        """SVG boxplot of image quality per beam number (1..N) - the
        per-region equivalent of the matplotlib ax_beam panel in the live
        viewer window, but rendered inline in this HTML report instead of
        only existing in the live_qc_dashboard.png snapshot, which most
        operators never open at full size."""
        if not beam_quality_values:
            return ('<div style="font-size:12px;color:#777;padding:24px;text-align:center;">'
                    'No per-beam quality data yet.</div>')
        beams_sorted = sorted(beam_quality_values.keys())
        stats = []
        for b in beams_sorted:
            vals = beam_quality_values.get(b) or []
            if not vals:
                continue
            arr = np.asarray(vals, dtype=np.float64)
            stats.append((
                int(b), float(np.min(arr)), float(np.percentile(arr, 25)),
                float(np.median(arr)), float(np.percentile(arr, 75)),
                float(np.max(arr)), int(arr.size),
            ))
        if not stats:
            return ('<div style="font-size:12px;color:#777;padding:24px;text-align:center;">'
                    'No per-beam quality data yet.</div>')

        all_lo = min(s[1] for s in stats)
        all_hi = max(s[5] for s in stats)
        y_pad = max(1.0, (all_hi - all_lo) * 0.08)
        y_min, y_max = all_lo - y_pad, all_hi + y_pad
        overall_med = float(np.median([s[3] for s in stats]))
        n_total_img = sum(s[6] for s in stats)

        n = len(stats)
        bar_w, gap = 14.0, 4.0
        plot_w = n * (bar_w + gap)
        plot_h = 220.0
        left_pad, top_pad, bottom_pad, right_pad = 40.0, 10.0, 24.0, 10.0
        width = plot_w + left_pad + right_pad
        height = plot_h + top_pad + bottom_pad

        def y_svg(v: float) -> float:
            return top_pad + (y_max - v) / (y_max - y_min) * plot_h

        parts = [f'<svg viewBox="0 0 {width:.1f} {height:.1f}" preserveAspectRatio="xMidYMid meet">']
        parts.append(f'<line x1="{left_pad:.1f}" y1="{top_pad:.1f}" x2="{left_pad:.1f}" '
                      f'y2="{top_pad + plot_h:.1f}" stroke="#999" stroke-width="1"/>')
        parts.append(f'<line x1="{left_pad:.1f}" y1="{top_pad + plot_h:.1f}" '
                      f'x2="{width - right_pad:.1f}" y2="{top_pad + plot_h:.1f}" stroke="#999" stroke-width="1"/>')
        for i in range(5):
            v = y_min + (y_max - y_min) * i / 4.0
            yy = y_svg(v)
            parts.append(f'<line x1="{left_pad - 4:.1f}" y1="{yy:.1f}" x2="{left_pad:.1f}" y2="{yy:.1f}" '
                          f'stroke="#999" stroke-width="1"/>')
            parts.append(f'<text x="{left_pad - 7:.1f}" y="{yy + 3:.1f}" font-size="9" '
                          f'text-anchor="end" fill="#555">{v:.0f}</text>')
        med_y = y_svg(overall_med)
        parts.append(f'<line x1="{left_pad:.1f}" y1="{med_y:.1f}" x2="{width - right_pad:.1f}" y2="{med_y:.1f}" '
                      f'stroke="#000" stroke-width="0.6" stroke-dasharray="4,3" opacity="0.4"/>')

        first_b, last_b = stats[0][0], stats[-1][0]
        for i, (b, lo, q1, med, q3, hi, n_img) in enumerate(stats):
            cx = left_pad + i * (bar_w + gap) + bar_w / 2.0 + gap
            color = _rgba_to_hex(cmap_focus(norm_focus(med)))
            y_lo, y_hi = y_svg(lo), y_svg(hi)
            y_q1, y_q3 = y_svg(q1), y_svg(q3)
            y_med = y_svg(med)
            box_x = cx - bar_w / 2.0
            parts.append(f'<line x1="{cx:.1f}" y1="{y_lo:.1f}" x2="{cx:.1f}" y2="{y_hi:.1f}" '
                          f'stroke="#666" stroke-width="0.6"/>')
            parts.append(
                f'<rect x="{box_x:.1f}" y="{min(y_q1, y_q3):.1f}" width="{bar_w:.1f}" '
                f'height="{max(abs(y_q1 - y_q3), 0.5):.1f}" fill="{color}" fill-opacity="0.9" '
                f'stroke="#555" stroke-width="0.6">'
                f'<title>beam {b}: median={med:.2f}  IQR=[{q1:.2f}, {q3:.2f}]  '
                f'range=[{lo:.2f}, {hi:.2f}]  n={n_img}</title>'
                f'</rect>'
            )
            parts.append(f'<line x1="{box_x:.1f}" y1="{y_med:.1f}" x2="{box_x + bar_w:.1f}" y2="{y_med:.1f}" '
                          f'stroke="#000" stroke-width="1"/>')
            if b % 5 == 0 or b == first_b or b == last_b:
                parts.append(f'<text x="{cx:.1f}" y="{top_pad + plot_h + 14:.1f}" font-size="8" '
                              f'text-anchor="middle" fill="#555">{b}</text>')

        parts.append(f'<text x="{left_pad + plot_w / 2.0:.1f}" y="{height - 2:.1f}" font-size="9" '
                      f'text-anchor="middle" fill="#333">Beam number  '
                      f'(overall median={overall_med:.2f}, n={n} beams, {n_total_img} images)</text>')
        parts.append('</svg>')
        return "".join(parts)

    # Outside-ROI/saturation and jitter panels were dropped from display (per
    # operator request - they weren't useful visually), but the underlying
    # outlier stats/flags (stig_outlier_total, jitter_outlier_total, the
    # retake "+ jitter/stig outliers only" buttons) still use those computed
    # values elsewhere in this function, so only the panel list shrinks.
    panels = [
        ("focus",   "① Focus quality"),
        ("stig",    "② Stig anisotropy + arrows"),
        ("status",  "③ Status"),
    ]

    status_legend_html = "".join(
        f'<span class="chip" style="background:{_rgba_to_hex(rgba)}">{_html.escape(name)}</span>'
        for name, rgba in _STATUS_COLORS.items()
    )

    q_vals = counters.get("q_vals") or []
    med_q = float(np.median(np.asarray(q_vals, dtype=np.float64))) if q_vals else float("nan")
    done = int(counters.get("done", 0))
    total = int(counters.get("total", len(layout)))
    fail_count = int(counters.get("fail", 0))
    outside_count = int(counters.get("outside", 0))
    jittery_count = int(counters.get("jittery", 0))

    panels_html = [
        f'<section class="panel overview"><h3>⓪ Region overview '
        f'(click an MFOV to open its folder)</h3>{_overview_panel_html()}</section>'
    ]
    for pid, title in panels:
        warn = ""
        if pid == "stig" and results_by_tile and len(stig_vals_all) < 3:
            # dev = raw - stig_med_final falls back to dev = raw (median 0.0,
            # threshold=inf) below this count - every hex reads as a near-white
            # "no deviation" rather than a real reading, indistinguishable from
            # a genuinely clean region unless flagged explicitly.
            warn = (
                '<div class="warn-banner">&#9888; Only '
                f'{len(stig_vals_all)}/{len(results_by_tile)} tile(s) have a usable (non-blank, non-failed) '
                'stig reading - too few for a meaningful baseline, so hex colors below are not reliable '
                '(they will look uniformly pale regardless of actual astigmatism).</div>'
            )
        panels_html.append(
            f'<section class="panel"><h3>{_html.escape(title)}</h3>{warn}{_panel_svg(pid)}</section>'
        )
    stig_med_str = f"{stig_med_final:+.2f}" if len(stig_vals_all) >= 3 else "—"
    jitter_med_str = f"{jitter_med_final:.4f}" if len(jitter_vals_all) >= 3 else "—"

    timing_dl = ""
    has_timing_header = focus_elapsed_s > 0.0 or timing_info is not None
    focus_str = total_str = "—"
    imaging_str = "in progress"
    if has_timing_header:
        focus_str = _fmt_duration(focus_elapsed_s) if focus_elapsed_s > 0.0 else "—"
        if timing_info is not None:
            imaging_s = float(timing_info.get("wall_s", 0.0))
            n_tiles = int(timing_info.get("n_tiles", 0))
            imaging_str = f"{_fmt_duration(imaging_s)} ({n_tiles} tiles)"
            total_str = _fmt_duration(focus_elapsed_s + imaging_s)
        timing_dl = (
            f'<dt>Focus time</dt><dd>{focus_str}</dd>'
            f'<dt>Imaging time</dt><dd>{imaging_str}</dd>'
            f'<dt>Total time</dt><dd>{total_str}</dd>'
        )

    panels_html.append(
        f'<section class="panel stats">'
        f'<h3>Stats</h3>'
        f'<dl>'
        f'<dt>Region</dt><dd>{_html.escape(region_label or "—")}</dd>'
        f'<dt>Block</dt><dd>{_html.escape(block_label or "—")}</dd>'
        f'<dt>Done</dt><dd>{done} / {total}</dd>'
        f'<dt>Median Q</dt><dd>{("—" if math.isnan(med_q) else f"{med_q:.2f}")}</dd>'
        f'<dt>Fails</dt><dd>{fail_count}</dd>'
        f'<dt>Outside-ROI</dt><dd>{outside_count}</dd>'
        f'<dt>Jittery (abs)</dt><dd>{jittery_count}</dd>'
        f'<dt>Stig median</dt><dd>{stig_med_str}</dd>'
        f'<dt>Stig outliers</dt><dd>{stig_outlier_total}</dd>'
        f'<dt>Jitter median</dt><dd>{jitter_med_str}</dd>'
        f'<dt>Jitter outliers</dt><dd>{jitter_outlier_total}</dd>'
        f'{timing_dl}'
        f'</dl>'
        f'<div class="legend"><strong>Status legend</strong><br>{status_legend_html}</div>'
        f'<div class="legend" style="margin-top:8px;"><strong>Stig:</strong> hex color = (h−v) − region median; arrows = direction. <strong>Jitter:</strong> hex color = std(top {int(0.05*100)}% of rows) / std(rest), per-image self-referencing — 1.0 ≈ clean, &gt;1.5 mild, &gt;2.5 severe. Hatched border on either panel = MAD outlier (&gt;3σ).</div>'
        f'</section>'
    )
    panels_html.append(
        f'<section class="panel beamplot"><h3>④ Image quality per beam '
        f'(look for a box shifted low or unusually wide/outlier-heavy vs. the rest)</h3>'
        f'{_beam_quality_panel_html()}</section>'
    )

    original_region_xml, wafer_metadata = _load_imaging_json_for_region(
        imaging_json_path, region_name
    )

    retake_tile_data: List[Dict[str, Any]] = []
    for tile_idx, x, y in zip(idxs, xs, ys):
        r = results_by_tile.get(tile_idx)
        if r is None:
            retake_tile_data.append({
                "tile_idx": int(tile_idx),
                "x_um": float(x),
                "y_um": float(y),
                "quality": None,
                "low_q": False,
                "quality_tier": "pending",
                "blank": False,
                "jittery_abs": False,
                "stig_outlier": False,
                "jitter_outlier": False,
                "has_beam_outlier": False,
                "tile_folder": "",
                "pending": True,
            })
            continue
        raw_q = float(r.get("quality", -100.0) or -100.0)
        raw_stig = float(r.get("stig_anisotropy", 0.0) or 0.0)
        raw_jitter = float(r.get("jitter_score_med", 0.0) or 0.0)
        is_baseline = (str(r.get("status_category", "ok")) == "ok"
                       and not bool(r.get("is_blank_outside_roi", False)))
        retake_tile_data.append({
            "tile_idx": int(tile_idx),
            "x_um": float(x),
            "y_um": float(y),
            "quality": raw_q,
            "low_q": bool(raw_q < float(low_quality_threshold)),
            # Three-tier quality bucket for the map fill color - "mid" sits
            # between the two thresholds (red/amber/green) - kept separate
            # from low_q (still used for the "+ low Q only" filter/button,
            # unchanged) and from the blank/stig/jitter override flags below.
            "quality_tier": quality_tier(raw_q, low_quality_threshold, high_quality_threshold),
            "blank": bool(r.get("is_blank_outside_roi", False)),
            "jittery_abs": bool(r.get("is_jittery", False)),
            "stig_outlier": bool(is_baseline and abs(raw_stig - stig_med_final) > stig_outlier_threshold),
            "jitter_outlier": bool(is_baseline and abs(raw_jitter - jitter_med_final) > jitter_outlier_threshold),
            "has_beam_outlier": bool(r.get("has_beam_outlier", False)),
            "tile_folder": str(r.get("tile_folder") or ""),
            "pending": False,
        })

    retake_payload = {
        "region_name": region_name or region_label,
        "region_label": region_label,
        "block_label": block_label,
        "hex_radius_um": float(radius),
        "tile_data": retake_tile_data,
        "original_region_xml": original_region_xml,
        "wafer_metadata": wafer_metadata,
        "imaging_json_path": imaging_json_path,
        "block_folder": block_folder_path,
    }
    retake_payload_json = json.dumps(retake_payload, ensure_ascii=False)
    tile_bmps_json = json.dumps(tile_bmp_urls, separators=(",", ":"))

    html_doc = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Live QC — {_html.escape(region_label)}</title>
<style>
  :root {{ --bg:#f4f4f6; --panel-bg:#fff; --border:#d8d8de; --text:#1c1c1f; }}
  * {{ box-sizing: border-box; }}
  body {{ margin:0; font-family: ui-monospace, Menlo, Consolas, monospace; background:var(--bg); color:var(--text); }}
  header {{ padding:14px 18px; background:#fff; border-bottom:1px solid var(--border); }}
  header h1 {{ margin:0 0 4px 0; font-size:16px; font-weight:600; }}
  header .summary {{ font-size:12px; color:#555; }}
  main.grid {{ display:grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap:12px; padding:12px; }}
  .panel {{ background:var(--panel-bg); border:1px solid var(--border); border-radius:6px; padding:10px; min-width:0; }}
  .panel h3 {{ margin:0 0 8px 0; font-size:13px; }}
  .panel svg {{ width:100%; height:auto; display:block; }}
  .panel .hex {{ cursor:pointer; transition: stroke-width 0.08s ease; }}
  .panel a:hover .hex, .panel .hex:hover {{ stroke:#000; stroke-width:1.6; }}
  .panel.overview {{ grid-column: 1 / -1; }}
  .panel.overview svg {{ width:auto; max-width:100%; height:auto; max-height:65vh; display:block; margin:0 auto; }}
  .panel.beamplot {{ grid-column: 1 / -1; }}
  .warn-banner {{ background:#fff3cd; color:#7a5b00; border:1px solid #ffe08a; border-radius:4px; padding:6px 10px; font-size:11px; margin-bottom:8px; }}
  .panel.overview .qtile {{ cursor:pointer; transition: fill-opacity 0.08s ease, stroke-width 0.08s ease; }}
  .panel.overview a:hover .qtile, .panel.overview .qtile:hover {{ stroke:#000; stroke-width:2; }}
  .panel.stats dl {{ display:grid; grid-template-columns: max-content 1fr; gap:2px 10px; margin:0; font-size:12px; }}
  .panel.stats dt {{ color:#555; }}
  .panel.stats dd {{ margin:0; font-weight:600; }}
  .legend {{ margin-top:10px; font-size:11px; }}
  .chip {{ display:inline-block; padding:2px 8px; margin:2px 4px 2px 0; border-radius:10px; color:#fff; font-size:10px; }}
  .toast {{ position:fixed; right:14px; bottom:14px; background:#222; color:#fff; padding:6px 10px; border-radius:4px; font-size:12px; opacity:0; transition:opacity 0.2s; pointer-events:none; }}
  .toast.show {{ opacity:0.95; }}
  .retake-section {{ margin:0 12px 16px 12px; background:var(--panel-bg); border:1px solid var(--border); border-radius:6px; padding:12px; }}
  .retake-section h3 {{ margin:0 0 8px 0; font-size:14px; }}
  .retake-controls {{ display:flex; flex-wrap:wrap; align-items:center; gap:8px; padding:8px 0 12px 0; border-bottom:1px solid var(--border); margin-bottom:12px; }}
  .retake-controls button {{ font:inherit; padding:6px 12px; border:1px solid #aaa; background:#f6f6f8; border-radius:4px; cursor:pointer; }}
  .retake-controls button.primary {{ background:#2b66c2; color:#fff; border-color:#1d4f9e; }}
  .retake-controls button.primary:hover {{ background:#3a78d4; }}
  .retake-controls button:hover {{ background:#e8e8ec; }}
  .retake-controls button.primary:hover {{ background:#3a78d4; }}
  .retake-status {{ font-size:12px; color:#444; }}
  .retake-status .key {{ color:#777; }}
  .retake-status .file {{ color:#1d4f9e; word-break:break-all; }}
  .retake-sync-banner {{ margin-bottom:10px; padding:7px 11px; background:#fff3cd; color:#7a5b00; border:1px solid #ffe08a; border-radius:4px; font-size:12px; }}
  .retake-sync-banner.synced {{ background:#e6f4ea; color:#1e5b2e; border-color:#bfe3c9; }}
  .retake-grid {{ display:grid; grid-template-columns: 3fr 1fr; gap:14px; }}
  .retake-grid svg {{ width:100%; height:auto; display:block; }}
  .retake-grid svg .hex {{ cursor:pointer; transition: filter 0.08s ease; }}
  /* stroke-dasharray:none is required here, not just cosmetic - a
     dashed "attention flag" hex (see hasAttentionFlag in the JS below)
     otherwise keeps its dashed outline even once selected, since this
     rule overrides stroke/stroke-width but a plain CSS class rule never
     touches an SVG presentation attribute it doesn't mention - leaving
     selection nearly invisible (dashed black -> dashed green) on exactly
     the tiles most worth selecting. */
  .retake-grid svg .hex.selected {{ stroke:#0a7; stroke-width:3; stroke-dasharray:none; filter: drop-shadow(0 0 3px rgba(0,128,80,0.4)); }}
  .retake-grid svg .hex:hover {{ stroke:#000; stroke-width:2; }}
  .retake-info {{ font-size:12px; }}
  .retake-info dl {{ display:grid; grid-template-columns: max-content 1fr; gap:4px 10px; margin:0; }}
  .retake-info dt {{ color:#666; }}
  .retake-info dd {{ margin:0; font-weight:600; }}
  .retake-info .hint {{ margin-top:10px; padding:8px; background:#f8f8fa; border-left:3px solid #2b66c2; font-size:11px; color:#444; }}
</style>
</head>
<body>
<header>
  <h1>Live MFOV QC — {_html.escape(region_label or "—")}</h1>
  <div class="summary">
    Block: {_html.escape(block_label or "—")} &nbsp;|&nbsp;
    Done {done}/{total} &nbsp;|&nbsp;
    Median Q = {("—" if math.isnan(med_q) else f"{med_q:.2f}")} &nbsp;|&nbsp;
    Fails {fail_count} &nbsp;|&nbsp; Outside-ROI {outside_count} &nbsp;|&nbsp; Jittery {jittery_count} &nbsp;|&nbsp;
    Focus {focus_str} &nbsp;|&nbsp; Imaging {imaging_str} &nbsp;|&nbsp; Total {total_str}
    <br><small>Click a hex to view its BMPs inverted (black/white-corrected, file unchanged). Cmd/Ctrl+click opens the tile folder. Shift+click copies the path.</small>
  </div>
</header>
<main class="grid">
{chr(10).join(panels_html)}
</main>
<section class="retake-section" id="retake-section">
  <h3>⑤ Retake selection</h3>
  <div class="retake-sync-banner" id="retake-sync-banner">
    ⚠ Not connected yet - click a hex, or "Connect batch folder…", and grant access to THIS batch's own
    folder ({_html.escape(block_label or "—")}). The retake list then saves itself there automatically as
    <code>{_html.escape(block_label or "batch")}_retake.json</code> - no save dialog, ever again, for any
    region in this batch (this asks once per batch, not once per region).
  </div>
  <div class="retake-controls">
    <button id="btn-pick-file" class="primary">Connect batch folder…</button>
    <button id="btn-save" disabled>Save now</button>
    <span style="width:10px"></span>
    <button id="btn-flagged">Select all flagged</button>
    <button id="btn-low-q">+ low Q only</button>
    <button id="btn-blank">+ outside-ROI only</button>
    <button id="btn-jitter">+ jitter outliers only</button>
    <button id="btn-stig">+ stig outliers only</button>
    <button id="btn-all">Select all in-ROI</button>
    <button id="btn-clear">Clear</button>
  </div>
  <div class="retake-status">
    <span class="key">File:</span> <span id="retake-file" class="file">(not connected yet)</span>
    &nbsp;·&nbsp;
    <span class="key">Autosave:</span> <span id="retake-sync-status">not connected</span>
    &nbsp;·&nbsp;
    <span class="key">Selected:</span> <span id="retake-count">0</span>
    &nbsp;·&nbsp;
    <span class="key">Clusters that will become retake regions:</span> <span id="retake-clusters">0</span>
  </div>
  <div class="retake-grid">
    <div id="retake-svg-host"></div>
    <div class="retake-info">
      <dl>
        <dt>Region</dt><dd id="info-region">—</dd>
        <dt>MFOVs total</dt><dd id="info-total">—</dd>
        <dt>Low-quality</dt><dd id="info-lowq">—</dd>
        <dt>Outside-ROI</dt><dd id="info-blank">—</dd>
        <dt>Stig outliers</dt><dd id="info-stig">—</dd>
        <dt>Jitter outliers</dt><dd id="info-jit">—</dd>
        <dt>Hex radius</dt><dd id="info-radius">—</dd>
      </dl>
      <div class="hint">
        <strong>How:</strong> click hexes to toggle. Selected MFOVs are clustered by adjacency; each connected cluster becomes a separate retake region with a convex-hull contour around its hexagons (option (b) per your spec). The wafer JSON keeps everything else from the original (SupportPoints, CenterPosition, ExperimentParameter, etc.) — only <code>Name</code> (suffixed <code>_retake_N</code>) and <code>Contour</code> change.
        <br><br>
        Save twice to the same file from different sections — selections for other sections are preserved (regions whose Name doesn't start with <code>{_html.escape((region_name or region_label) + "_retake")}</code> are kept untouched).
      </div>
    </div>
  </div>
</section>
<div class="toast" id="toast">copied</div>
<svg width="0" height="0" style="position:absolute">
  <defs>
    <marker id="arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="6" markerHeight="6" orient="auto-start-reverse">
      <path d="M0,0 L10,5 L0,10 z" fill="context-stroke"/>
    </marker>
  </defs>
</svg>
<script id="tile-bmps-payload" type="application/json">{tile_bmps_json}</script>
<script>
(function() {{
  const toast = document.getElementById('toast');
  function showToast(msg) {{
    toast.textContent = msg;
    toast.classList.add('show');
    clearTimeout(toast._t);
    toast._t = setTimeout(function() {{ toast.classList.remove('show'); }}, 1200);
  }}
  let TILE_BMPS = {{}};
  try {{
    TILE_BMPS = JSON.parse(document.getElementById('tile-bmps-payload').textContent || '{{}}');
  }} catch (_e) {{ TILE_BMPS = {{}}; }}

  function openInvertedView(tileIdx, urls) {{
    const w = window.open('about:blank', '_blank');
    if (!w) {{ showToast('popup blocked — allow popups for this page'); return; }}
    const imgs = urls.map(function(u, i) {{
      const safe = String(u).replace(/"/g, '&quot;');
      return '<figure class="zoomwrap">'
           + '<div class="zoombox" id="zb' + i + '">'
           + '<span class="zoom-badge" id="zbadge' + i + '">100%</span>'
           + '<img id="zimg' + i + '" src="' + safe + '" loading="lazy" draggable="false">'
           + '</div>'
           + '<figcaption>' + safe + '</figcaption></figure>';
    }}).join('');
    w.document.open();
    w.document.write(
      '<!doctype html><html><head><meta charset="utf-8">'
      + '<title>Tile ' + tileIdx + ' — inverted preview</title>'
      + '<style>'
      + 'body{{margin:0;padding:14px;background:#111;color:#ccc;'
      +    'font-family:ui-monospace,Menlo,Consolas,monospace;}}'
      + 'h1{{font-size:14px;margin:0 0 12px;color:#eee;}}'
      + '.note{{font-size:11px;color:#888;margin-bottom:14px;}}'
      + 'figure{{margin:0 0 18px;}}'
      + 'figcaption{{font-size:10px;color:#7aa;word-break:break-all;'
      +    'padding:4px 0;}}'
      + '.zoombox{{overflow:hidden;position:relative;max-height:88vh;'
      +    'border:1px solid #333;background:#000;cursor:grab;touch-action:none;}}'
      + '.zoombox.dragging{{cursor:grabbing;}}'
      + '.zoombox img{{display:block;max-width:100%;height:auto;'
      +    'transform-origin:0 0;filter:invert(1);user-select:none;'
      +    '-webkit-user-drag:none;}}'
      + '.zoom-badge{{position:absolute;top:6px;right:6px;'
      +    'background:rgba(0,0,0,0.65);color:#9fe;font-size:11px;'
      +    'padding:3px 7px;border-radius:3px;pointer-events:none;z-index:2;}}'
      + '</style></head><body>'
      + '<h1>Tile ' + tileIdx + ' — ' + urls.length + ' BMP(s), display-inverted</h1>'
      + '<div class="note">Files on disk are unchanged. The black↔white flip '
      +   'is applied via CSS <code>filter: invert(1)</code> for viewing only. '
      +   '<strong>Scroll/pinch to zoom</strong> (centered on cursor), '
      +   '<strong>drag to pan</strong>, <strong>double-click to reset</strong> — '
      +   'for checking pixel-level focus/stig quality.</div>'
      + imgs + '</body></html>'
    );
    w.document.close();

    // Zoom/pan per image, independent state per zoombox. Attached after
    // document.write() so the elements above already exist in w.document.
    urls.forEach(function(_u, i) {{
      const box = w.document.getElementById('zb' + i);
      const img = w.document.getElementById('zimg' + i);
      const badge = w.document.getElementById('zbadge' + i);
      if (!box || !img) return;
      const state = {{ scale: 1, tx: 0, ty: 0 }};
      const MIN_SCALE = 1, MAX_SCALE = 40;
      function apply() {{
        img.style.transform = 'translate(' + state.tx + 'px,' + state.ty + 'px) scale(' + state.scale + ')';
        badge.textContent = Math.round(state.scale * 100) + '%';
      }}
      function clampScale(s) {{ return Math.min(MAX_SCALE, Math.max(MIN_SCALE, s)); }}
      box.addEventListener('wheel', function(e) {{
        e.preventDefault();
        const rect = box.getBoundingClientRect();
        const mx = e.clientX - rect.left, my = e.clientY - rect.top;
        const factor = e.deltaY < 0 ? 1.2 : 1 / 1.2;
        const newScale = clampScale(state.scale * factor);
        if (newScale === state.scale) return;
        const ratio = newScale / state.scale;
        state.tx = mx - (mx - state.tx) * ratio;
        state.ty = my - (my - state.ty) * ratio;
        state.scale = newScale;
        if (state.scale === MIN_SCALE) {{ state.tx = 0; state.ty = 0; }}
        apply();
      }}, {{ passive: false }});
      let dragging = false, lastX = 0, lastY = 0;
      box.addEventListener('mousedown', function(e) {{
        if (state.scale <= MIN_SCALE) return;
        dragging = true; lastX = e.clientX; lastY = e.clientY;
        box.classList.add('dragging');
        e.preventDefault();
      }});
      w.document.addEventListener('mouseup', function() {{ dragging = false; box.classList.remove('dragging'); }});
      w.document.addEventListener('mousemove', function(e) {{
        if (!dragging) return;
        state.tx += e.clientX - lastX;
        state.ty += e.clientY - lastY;
        lastX = e.clientX; lastY = e.clientY;
        apply();
      }});
      box.addEventListener('dblclick', function() {{
        state.scale = 1; state.tx = 0; state.ty = 0;
        apply();
      }});
      apply();
    }});
  }}

  document.querySelectorAll('a[data-path]').forEach(function(a) {{
    a.addEventListener('click', function(ev) {{
      // Shift = copy path
      if (ev.shiftKey) {{
        ev.preventDefault();
        const path = a.getAttribute('data-path') || '';
        if (navigator.clipboard && navigator.clipboard.writeText) {{
          navigator.clipboard.writeText(path).then(function() {{ showToast('copied: ' + path); }});
        }} else {{
          const ta = document.createElement('textarea');
          ta.value = path;
          document.body.appendChild(ta);
          ta.select();
          try {{ document.execCommand('copy'); showToast('copied: ' + path); }} catch (e) {{}}
          document.body.removeChild(ta);
        }}
        return;
      }}
      // Cmd/Ctrl = legacy behavior (let the browser open the folder URL)
      if (ev.metaKey || ev.ctrlKey) {{ return; }}
      // Plain click: open inverted-BMP preview if we have the list
      const tileIdx = parseInt(a.getAttribute('data-tile') || '-1', 10);
      const urls = TILE_BMPS[tileIdx];
      if (urls && urls.length) {{
        ev.preventDefault();
        openInvertedView(tileIdx, urls);
      }}
      // else: fall through to <a href="...">'s default folder open
    }});
  }});
}})();
</script>
<script id="retake-payload" type="application/json">{retake_payload_json}</script>
<script>
(function() {{
  const RETAKE_DATA = JSON.parse(document.getElementById('retake-payload').textContent);
  const HEX_R = RETAKE_DATA.hex_radius_um || 50;
  const REGION_NAME = RETAKE_DATA.region_name;
  const RETAKE_NAME_PREFIX = REGION_NAME + '_retake';
  const tiles = RETAKE_DATA.tile_data;
  const tileById = new Map(tiles.map(t => [t.tile_idx, t]));

  // ---------- Stats info ----------
  const lowQ = tiles.filter(t => t.low_q && !t.pending).length;
  const blank = tiles.filter(t => t.blank && !t.pending).length;
  const stigOut = tiles.filter(t => t.stig_outlier && !t.pending).length;
  const jitOut = tiles.filter(t => t.jitter_outlier && !t.pending).length;
  document.getElementById('info-region').textContent = REGION_NAME;
  document.getElementById('info-total').textContent = tiles.length + ' (' + (tiles.length - tiles.filter(t => t.pending).length) + ' measured)';
  document.getElementById('info-lowq').textContent = lowQ;
  document.getElementById('info-blank').textContent = blank;
  document.getElementById('info-stig').textContent = stigOut;
  document.getElementById('info-jit').textContent = jitOut;
  document.getElementById('info-radius').textContent = HEX_R.toFixed(1) + ' µm';

  // ---------- SVG hex grid ----------
  const xs = tiles.map(t => t.x_um);
  const ys = tiles.map(t => t.y_um);
  const pad = 3 * HEX_R;
  const minX = Math.min(...xs) - pad, maxX = Math.max(...xs) + pad;
  const minY = Math.min(...ys) - pad, maxY = Math.max(...ys) + pad;
  const W = maxX - minX, H = maxY - minY;
  const ySvg = (y) => (minY + maxY) - y;

  function hexPoints(cx, cy) {{
    const pts = [];
    for (let i = 0; i < 6; i++) {{
      const a = Math.PI/6 + i*Math.PI/3;
      const px = cx + HEX_R * Math.cos(a);
      const py = ySvg(cy) - HEX_R * Math.sin(a);
      pts.push(px.toFixed(2) + ',' + py.toFixed(2));
    }}
    return pts.join(' ');
  }}

  const svgNS = 'http://www.w3.org/2000/svg';
  const svg = document.createElementNS(svgNS, 'svg');
  svg.setAttribute('viewBox', `${{minX.toFixed(2)}} ${{minY.toFixed(2)}} ${{W.toFixed(2)}} ${{H.toFixed(2)}}`);
  svg.setAttribute('preserveAspectRatio', 'xMidYMid meet');

  const hexEls = new Map();
  for (const t of tiles) {{
    const el = document.createElementNS(svgNS, 'polygon');
    el.setAttribute('class', 'hex');
    el.setAttribute('points', hexPoints(t.x_um, t.y_um));
    // Fill = quality tier (red/amber/green), or grey for outside-ROI/blank -
    // an aggregate (median) verdict for the mfov as a whole. Stroke is a
    // SEPARATE signal: dashed black border = "look closer" (a beam, stig, or
    // jitter outlier), independent of the fill - a single berserk beam can
    // hide inside an otherwise-green median, so it needs its own cue rather
    // than fighting the fill color for the same pixel.
    let baseFill = '#ebebeb';
    let hasAttentionFlag = false;
    if (!t.pending) {{
      if (t.blank) baseFill = '#666';
      else if (t.quality_tier === 'red') baseFill = '#e57373';
      else if (t.quality_tier === 'amber') baseFill = '#ffd980';
      else baseFill = '#dde6dd';
      hasAttentionFlag = t.has_beam_outlier || t.stig_outlier || t.jitter_outlier;
    }}
    el.setAttribute('fill', baseFill);
    el.setAttribute('stroke', hasAttentionFlag ? '#000' : '#555');
    el.setAttribute('stroke-width', hasAttentionFlag ? '1.5' : '0.5');
    if (hasAttentionFlag) el.setAttribute('stroke-dasharray', '3,2');
    el.dataset.tile = t.tile_idx;
    if (t.pending) el.style.cursor = 'not-allowed';
    const title = document.createElementNS(svgNS, 'title');
    let lines = [
      `tile ${{t.tile_idx}}`,
      `xy=(${{t.x_um.toFixed(1)}}, ${{t.y_um.toFixed(1)}})`,
    ];
    if (t.pending) {{ lines.push('pending'); }}
    else {{
      if (t.quality !== null) lines.push(`Q=${{t.quality.toFixed(2)}} (${{t.quality_tier}})`);
      const flags = [];
      if (t.low_q) flags.push('low_q');
      if (t.blank) flags.push('outside_roi');
      if (t.has_beam_outlier) flags.push('beam_outlier');
      if (t.stig_outlier) flags.push('stig_outlier');
      if (t.jitter_outlier) flags.push('jitter_outlier');
      if (t.jittery_abs) flags.push('jittery_abs');
      if (flags.length) lines.push('flags: ' + flags.join(', '));
    }}
    title.textContent = lines.join('\\n');
    el.appendChild(title);
    if (!t.pending) {{
      el.addEventListener('click', () => toggle(t.tile_idx));
    }}
    svg.appendChild(el);
    hexEls.set(t.tile_idx, el);
  }}
  document.getElementById('retake-svg-host').appendChild(svg);

  // ---------- Selection state ----------
  const selected = new Set();

  function refreshHex(tile_idx) {{
    const el = hexEls.get(tile_idx);
    if (!el) return;
    if (selected.has(tile_idx)) el.classList.add('selected');
    else el.classList.remove('selected');
  }}
  // Every selection change tries to connect (reusing a remembered file if
  // one exists - only a fresh, never-connected browser profile actually
  // sees a file dialog) and then autosaves - see ensureConnected/
  // scheduleAutoSave below. Called from a real click handler every time,
  // so it always has a valid user gesture if requestPermission ends up
  // needing one.
  function onSelectionChanged() {{
    ensureConnected().then(ok => {{ if (ok) scheduleAutoSave(); }});
  }}
  function toggle(tile_idx) {{
    const t = tileById.get(tile_idx);
    if (!t || t.pending) return;
    if (selected.has(tile_idx)) selected.delete(tile_idx);
    else selected.add(tile_idx);
    refreshHex(tile_idx);
    updateStatus();
    onSelectionChanged();
  }}
  function setMany(predicate, additive=false) {{
    if (!additive) selected.clear();
    for (const t of tiles) {{
      if (t.pending) continue;
      if (predicate(t)) selected.add(t.tile_idx);
    }}
    for (const t of tiles) refreshHex(t.tile_idx);
    updateStatus();
    onSelectionChanged();
  }}

  document.getElementById('btn-flagged').onclick = () => setMany(t => t.low_q || t.blank || t.has_beam_outlier || t.stig_outlier || t.jitter_outlier);
  document.getElementById('btn-low-q').onclick = () => setMany(t => t.low_q, true);
  document.getElementById('btn-blank').onclick = () => setMany(t => t.blank, true);
  document.getElementById('btn-jitter').onclick = () => setMany(t => t.jitter_outlier, true);
  document.getElementById('btn-stig').onclick = () => setMany(t => t.stig_outlier, true);
  document.getElementById('btn-all').onclick = () => setMany(t => !t.blank);
  document.getElementById('btn-clear').onclick = () => {{
    selected.clear();
    for (const t of tiles) refreshHex(t.tile_idx);
    updateStatus();
    onSelectionChanged();
  }};

  // ---------- Clustering + convex hull ----------
  function clusterSelected() {{
    const nodes = [...selected].map(idx => {{
      const t = tileById.get(idx);
      return {{ tile_idx: idx, x: t.x_um, y: t.y_um }};
    }});
    const edgeR2 = (2.5 * HEX_R) * (2.5 * HEX_R);
    const seen = new Set();
    const clusters = [];
    for (const start of nodes) {{
      if (seen.has(start.tile_idx)) continue;
      const stack = [start];
      seen.add(start.tile_idx);
      const group = [];
      while (stack.length) {{
        const cur = stack.pop();
        group.push(cur);
        for (const n of nodes) {{
          if (seen.has(n.tile_idx)) continue;
          const dx = cur.x - n.x, dy = cur.y - n.y;
          if (dx*dx + dy*dy < edgeR2) {{
            seen.add(n.tile_idx);
            stack.push(n);
          }}
        }}
      }}
      clusters.push(group);
    }}
    return clusters;
  }}

  function convexHull(points) {{
    if (points.length <= 1) return points.slice();
    const pts = points.slice().sort((a, b) => a[0] - b[0] || a[1] - b[1]);
    const cross = (O, A, B) => (A[0]-O[0])*(B[1]-O[1]) - (A[1]-O[1])*(B[0]-O[0]);
    const lo = [];
    for (const p of pts) {{
      while (lo.length >= 2 && cross(lo[lo.length-2], lo[lo.length-1], p) <= 0) lo.pop();
      lo.push(p);
    }}
    const up = [];
    for (let i = pts.length - 1; i >= 0; i--) {{
      const p = pts[i];
      while (up.length >= 2 && cross(up[up.length-2], up[up.length-1], p) <= 0) up.pop();
      up.push(p);
    }}
    up.pop(); lo.pop();
    return lo.concat(up);
  }}

  function updateStatus() {{
    document.getElementById('retake-count').textContent = selected.size;
    const c = selected.size === 0 ? 0 : clusterSelected().length;
    document.getElementById('retake-clusters').textContent = c;
  }}
  updateStatus();

  // Deterministically derive a retake _id from the original wafer _id.
  // Keep UUID shape (5 hex blocks separated by dashes). For each block,
  // shift the last hex digit by +7 (mod 16). Same original -> same retake id,
  // so the file's identity is stable across re-saves.
  function twistUuid(orig) {{
    if (!orig || typeof orig !== 'string') return null;
    const blocks = orig.split('-');
    if (blocks.length !== 5) return null;
    const shiftHex = (c) => {{
      const n = parseInt(c, 16);
      if (Number.isNaN(n)) return c;
      return ((n + 7) % 16).toString(16);
    }};
    return blocks.map(b => {{
      if (!b.length) return b;
      return b.slice(0, -1) + shiftHex(b.slice(-1));
    }}).join('-');
  }}

  // ---------- Build retake region XML ----------
  function inflate(c, r) {{ return r * 1.06; }}  // 6% safety margin so hexes lie strictly inside

  function buildRetakeRegionXml(cluster, retakeIdx) {{
    const orig = RETAKE_DATA.original_region_xml;
    if (!orig) {{
      throw new Error('No original region XML available; cannot build retake region. Was the imaging JSON path passed to the viewer?');
    }}
    const inflated = inflate(null, HEX_R);
    const verts = [];
    for (const node of cluster) {{
      for (let i = 0; i < 6; i++) {{
        const a = Math.PI/6 + i*Math.PI/3;
        verts.push([node.x + inflated * Math.cos(a), node.y + inflated * Math.sin(a)]);
      }}
    }}
    const hull = convexHull(verts);
    let minX = Infinity, maxX = -Infinity, minY = Infinity, maxY = -Infinity, cx = 0, cy = 0;
    for (const [x, y] of hull) {{
      minX = Math.min(minX, x); maxX = Math.max(maxX, x);
      minY = Math.min(minY, y); maxY = Math.max(maxY, y);
      cx += x; cy += y;
    }}
    cx /= hull.length; cy /= hull.length;
    const width = maxX - minX, height = maxY - minY;
    const ptsStr = hull.map(([x, y]) => x.toFixed(7) + ',' + y.toFixed(7)).join(' ');

    let xml = orig;
    const newName = REGION_NAME + '_retake_' + retakeIdx;
    xml = xml.replace(/Name="[^"]+"/, 'Name="' + newName + '"');
    xml = xml.replace(/<CenterPosition>[\\s\\S]*?<\\/CenterPosition>/,
                      '<CenterPosition>' + cx.toFixed(7) + ',' + cy.toFixed(7) + '</CenterPosition>');
    xml = xml.replace(/<ContourSize>[\\s\\S]*?<\\/ContourSize>/,
                      '<ContourSize>' + width.toFixed(7) + ',' + height.toFixed(7) + '</ContourSize>');
    xml = xml.replace(/(<Contour\\s+Type="Polygon">[\\s\\S]*?<Points>)[\\s\\S]*?(<\\/Points>)/,
                      function(_, p1, p2) {{ return p1 + ptsStr + p2; }});
    return xml;
  }}

  // ---------- File System Access API (connect once, autosave every change) ----------
  // A webpage cannot write ANY local file without at least one explicit user
  // gesture, by browser design - there is no way to skip that first grant
  // entirely, and no API lets a page pre-navigate a native folder dialog to
  // an arbitrary path either. What CAN be eliminated is the file dialog on
  // every save (and every future reconnect): granting access to THIS BATCH's
  // own folder once, up front, means every save afterwards - for this region
  // and every other region's dashboard in the same batch folder - writes
  // straight into "<batch>_retake.json" inside it with NO dialog at all,
  // not even a "where to save" one. The granted directory handle is
  // remembered in IndexedDB (shared across file:// pages in this browser
  // profile) so reopening any dashboard in this batch later reconnects
  // silently too, as long as the OS permission is still considered granted.
  let waferHandle = null;
  let batchDirHandle = null;
  let retakeSaveTimer = null;
  let retakeSavingNow = false;
  const RETAKE_IDB_NAME = 'himc_retake_json';
  // Scoped to THIS batch's own folder - otherwise a region dashboard from a
  // totally different batch (or dataset) opened earlier in this browser
  // profile would silently hand back ITS remembered folder handle instead
  // of asking fresh.
  const RETAKE_IDB_KEY = 'retakeDirHandle:' + (RETAKE_DATA.block_folder || '');
  const RETAKE_AUTOSAVE_DEBOUNCE_MS = 400;

  function setFileLabel(text) {{ document.getElementById('retake-file').textContent = text; }}
  function setSyncStatus(text) {{ document.getElementById('retake-sync-status').textContent = text; }}
  function markRetakeSynced() {{
    const banner = document.getElementById('retake-sync-banner');
    banner.classList.add('synced');
    banner.textContent = '✓ Connected to ' + (batchDirHandle ? batchDirHandle.name : 'this batch\\'s folder') +
      ' - every selection change here saves itself automatically, no dialog.';
  }}

  function retakeFileName() {{
    return (RETAKE_DATA.block_label || 'batch') + '_retake.json';
  }}

  // Never let a slow/stuck IndexedDB open hang the whole connect/save chain
  // forever with no feedback - resolves to `fallback` if not settled by `ms`.
  function withTimeout(promise, ms, fallback) {{
    return new Promise((resolve) => {{
      let done = false;
      const timer = setTimeout(() => {{ if (!done) {{ done = true; resolve(fallback); }} }}, ms);
      promise.then(v => {{ if (!done) {{ done = true; clearTimeout(timer); resolve(v); }} }})
             .catch(() => {{ if (!done) {{ done = true; clearTimeout(timer); resolve(fallback); }} }});
    }});
  }}
  function idbOpen() {{
    return new Promise((resolve, reject) => {{
      const req = indexedDB.open(RETAKE_IDB_NAME, 1);
      req.onupgradeneeded = () => req.result.createObjectStore('handles');
      req.onsuccess = () => resolve(req.result);
      req.onerror = () => reject(req.error);
    }});
  }}
  async function idbGet(key) {{
    try {{
      const db = await idbOpen();
      return await new Promise((resolve, reject) => {{
        const tx = db.transaction('handles', 'readonly');
        const req = tx.objectStore('handles').get(key);
        req.onsuccess = () => resolve(req.result || null);
        req.onerror = () => reject(req.error);
      }});
    }} catch (e) {{ return null; }}
  }}
  async function idbSet(key, value) {{
    try {{
      const db = await idbOpen();
      await new Promise((resolve, reject) => {{
        const tx = db.transaction('handles', 'readwrite');
        tx.objectStore('handles').put(value, key);
        tx.oncomplete = () => resolve();
        tx.onerror = () => reject(tx.error);
      }});
    }} catch (e) {{ /* best-effort only - manual re-pick still works */ }}
  }}

  async function reconcileFromDisk(handle) {{
    try {{
      const file = await handle.getFile();
      if (file.size === 0) return;
      const data = JSON.parse(await file.text());
      loadExistingSelections(data);
    }} catch (e) {{ /* not created yet, or unreadable - fine, Save will create it */ }}
  }}

  // dirHandle is a granted FileSystemDirectoryHandle for the batch folder -
  // the actual file we read/write (waferHandle) is derived from it fresh
  // each connect, but IndexedDB remembers the DIRECTORY handle, not the
  // file, since that's what carries the OS permission grant.
  async function connectDirHandle(dirHandle, persist) {{
    batchDirHandle = dirHandle;
    waferHandle = await dirHandle.getFileHandle(retakeFileName(), {{create: true}});
    setFileLabel(dirHandle.name + ' / ' + retakeFileName());
    document.getElementById('btn-save').disabled = false;
    await reconcileFromDisk(waferHandle);
    if (persist) await withTimeout(idbSet(RETAKE_IDB_KEY, dirHandle), 1500, undefined);
    setSyncStatus('autosave ready');
    markRetakeSynced();
  }}

  async function pickBatchFolder() {{
    if (!window.showDirectoryPicker) {{
      alert('Your browser does not support the File System Access API. Use Chrome or Edge to save retake JSON in-place.');
      return;
    }}
    try {{
      const dirHandle = await window.showDirectoryPicker({{mode: 'readwrite'}});
      await connectDirHandle(dirHandle, true);
    }} catch (e) {{
      if (e && e.name !== 'AbortError') alert('Folder pick failed: ' + e.message);
    }}
  }}

  // Called from inside a real click handler (a hex toggle, a quick-select
  // button, or the connect button itself) so it always has a valid user
  // gesture available if requestPermission needs one - either to silently
  // re-confirm a remembered permission, or (only the very first time ever
  // for this batch, or if the remembered handle has gone stale) to show the
  // one-time "pick this batch's folder" dialog itself.
  async function ensureConnected() {{
    if (waferHandle) return true;
    const stored = await withTimeout(idbGet(RETAKE_IDB_KEY), 1500, null);
    if (stored) {{
      try {{
        let perm = await stored.queryPermission({{mode: 'readwrite'}});
        if (perm !== 'granted') perm = await stored.requestPermission({{mode: 'readwrite'}});
        if (perm === 'granted') {{
          await connectDirHandle(stored, false);
          return true;
        }}
      }} catch (e) {{ /* stale/invalid handle (e.g. folder moved) - fall through to picking a new one */ }}
    }}
    await pickBatchFolder();
    return !!waferHandle;
  }}

  // Silent, gesture-free attempt on page load - only succeeds if the
  // browser already considers permission granted without asking again.
  async function tryQuietAutoReconnect() {{
    const stored = await withTimeout(idbGet(RETAKE_IDB_KEY), 1500, null);
    if (!stored) return;
    try {{
      const perm = await stored.queryPermission({{mode: 'readwrite'}});
      if (perm === 'granted') {{
        await connectDirHandle(stored, false);
      }} else {{
        setSyncStatus('not connected - click a hex or "Connect batch folder…" above');
      }}
    }} catch (e) {{ /* stale/invalid handle - a click will pick a new one */ }}
  }}

  function loadExistingSelections(data) {{
    if (!data || !Array.isArray(data.Regions)) return;
    let preselected = 0;
    for (const regStr of data.Regions) {{
      const m = regStr.match(/Name="([^"]+)"/);
      if (!m) continue;
      const name = m[1];
      if (!name.startsWith(RETAKE_NAME_PREFIX)) continue;
      // parse Points
      const pm = regStr.match(/<Contour\\s+Type="Polygon">[\\s\\S]*?<Points>([\\s\\S]*?)<\\/Points>/);
      if (!pm) continue;
      const polyPts = pm[1].trim().split(/\\s+/).map(s => s.split(',').map(Number));
      // For each measured tile, if its center is inside polygon, mark selected
      for (const t of tiles) {{
        if (t.pending || t.blank) continue;
        if (pointInPoly(t.x_um, t.y_um, polyPts)) {{
          if (!selected.has(t.tile_idx)) {{ selected.add(t.tile_idx); preselected++; }}
        }}
      }}
    }}
    for (const t of tiles) refreshHex(t.tile_idx);
    updateStatus();
    if (preselected > 0) {{
      console.log('[retake] Pre-selected ' + preselected + ' tile(s) from existing retake regions in file.');
    }}
  }}

  function pointInPoly(px, py, poly) {{
    let inside = false;
    for (let i = 0, j = poly.length - 1; i < poly.length; j = i++) {{
      const xi = poly[i][0], yi = poly[i][1], xj = poly[j][0], yj = poly[j][1];
      const intersect = ((yi > py) !== (yj > py)) && (px < (xj - xi) * (py - yi) / (yj - yi + 1e-12) + xi);
      if (intersect) inside = !inside;
    }}
    return inside;
  }}

  async function doSave() {{
    if (!waferHandle || retakeSavingNow) return;
    retakeSavingNow = true;
    setSyncStatus('saving…');

    let existing = null;
    try {{
      const file = await waferHandle.getFile();
      if (file.size > 0) existing = JSON.parse(await file.text());
    }} catch (e) {{}}

    const origId = (RETAKE_DATA.wafer_metadata && RETAKE_DATA.wafer_metadata._id) || null;
    const retakeId = twistUuid(origId) || crypto.randomUUID();
    const out = existing ? structuredClone(existing) : {{
      _id: retakeId,
      Name: ((RETAKE_DATA.wafer_metadata && RETAKE_DATA.wafer_metadata.Name) || 'wafer') + '_retake',
      ExperimentParameter: (RETAKE_DATA.wafer_metadata && RETAKE_DATA.wafer_metadata.ExperimentParameter) || {{}},
      Regions: [],
      DateTime: new Date().toISOString(),
    }};
    if (!Array.isArray(out.Regions)) out.Regions = [];

    // strip any existing retake regions for THIS section
    out.Regions = out.Regions.filter(s => {{
      const m = s.match(/Name="([^"]+)"/);
      return !(m && m[1].startsWith(RETAKE_NAME_PREFIX));
    }});

    // emit new ones
    if (selected.size > 0) {{
      const clusters = clusterSelected();
      let i = 1;
      for (const cluster of clusters) {{
        try {{
          out.Regions.push(buildRetakeRegionXml(cluster, i));
          i++;
        }} catch (e) {{
          setSyncStatus('autosave FAILED to build retake shape (' + e.message + ') - click "Save now" to retry');
          retakeSavingNow = false;
          return;
        }}
      }}
    }}

    try {{
      const writer = await waferHandle.createWritable();
      await writer.write(JSON.stringify(out, null, 2));
      await writer.close();
      const t = document.getElementById('toast');
      t.textContent = 'saved → ' + waferHandle.name + '  (' + out.Regions.length + ' total retake regions)';
      t.classList.add('show');
      setTimeout(() => t.classList.remove('show'), 1800);
      setSyncStatus('autosaved ' + new Date().toLocaleTimeString());
    }} catch (e) {{
      setSyncStatus('autosave FAILED (' + e.message + ') - click "Save now" to retry');
    }} finally {{
      retakeSavingNow = false;
    }}
  }}

  function scheduleAutoSave() {{
    clearTimeout(retakeSaveTimer);
    setSyncStatus('saving…');
    retakeSaveTimer = setTimeout(doSave, RETAKE_AUTOSAVE_DEBOUNCE_MS);
  }}

  // "Connect batch folder…" reuses a remembered directory handle (a
  // lightweight native re-confirm, not a fresh folder dialog) whenever one
  // exists - it only prompts for a brand-new folder the very first time
  // ever, for this batch (any region's dashboard within the same batch
  // folder reconnects silently once one of them has been granted).
  document.getElementById('btn-pick-file').onclick = () => {{ ensureConnected(); }};
  document.getElementById('btn-save').onclick = () => {{ ensureConnected().then(ok => {{ if (ok) doSave(); }}); }};

  // Best-effort final flush if the tab is closed/navigated away while a
  // debounced save is still pending (see the global QC report's identical
  // reasoning - not guaranteed to complete, but the short debounce means a
  // save has almost always already landed before a human closes the tab).
  document.addEventListener('visibilitychange', () => {{
    if (document.visibilityState === 'hidden' && waferHandle && retakeSaveTimer) {{
      clearTimeout(retakeSaveTimer);
      doSave();
    }}
  }});

  tryQuietAutoReconnect();

  const qtoggle = document.getElementById('toggle-overview-quality');
  if (qtoggle) {{
    qtoggle.addEventListener('change', (e) => {{
      const on = e.target.checked;
      document.querySelectorAll('.panel.overview .qtile').forEach((el) => {{
        el.setAttribute('fill-opacity', on ? el.getAttribute('data-opacity') : '0');
      }});
      // Numeric per-MFOV quality label - previously left always-on regardless
      // of this checkbox (2026-09-06 fix): "Show focus-quality overlay"
      // should remove the average-quality figure entirely when unchecked,
      // not just the color tint underneath it.
      document.querySelectorAll('.panel.overview .qlabel').forEach((el) => {{
        el.style.display = on ? '' : 'none';
      }});
    }});
  }}
}})();
</script>
</body>
</html>
"""

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(html_doc, encoding="utf-8")


def _qc_viewer_main_impl(layout: List[Dict[str, Any]], viewer_queue: Any, cfg: Dict[str, Any]) -> None:
    import matplotlib
    import matplotlib.pyplot as plt
    import matplotlib.cm as cm
    from matplotlib.colors import Normalize, BoundaryNorm, ListedColormap
    from matplotlib.patches import RegularPolygon, Patch
    from matplotlib.ticker import ScalarFormatter

    def _get_cmap(name: str):
        if hasattr(matplotlib, "colormaps"):
            return matplotlib.colormaps[name]
        return cm.get_cmap(name)

    save_every = int(cfg.get("save_every", 5))
    results_dir = Path(cfg["results_dir"])
    results_dir.mkdir(parents=True, exist_ok=True)
    snapshot_path = results_dir / "live_qc_dashboard.png"

    # Per-region downsampled overview accumulator (best-effort). Caches each
    # completed MFOV's individual beam tiles at their own true global
    # coordinates (image_coordinates.txt); rendered at save_every + STOP.
    region_ov = None
    if RegionOverviewAccumulator is not None and bool(cfg.get("overview_region_enabled", True)):
        try:
            region_ov = RegionOverviewAccumulator(layout, results_dir, cfg)
        except Exception as _ov_exc:
            print(f"[LiveQC] region overview disabled (init failed): {_ov_exc}")
            region_ov = None

    region_label = str(cfg.get("region_label", "") or "")
    block_label = str(cfg.get("block_label", "") or "")
    low_quality_threshold = float(cfg.get("low_quality_threshold", 10.0))
    high_quality_threshold = float(cfg.get("high_quality_threshold", 30.0))
    focus_color_mode = str(cfg.get("focus_color_mode", "continuous")).lower()
    # Focus/stigmation time is known up front (computed before acquire_tiles
    # even starts); the imaging-side breakdown (move/scan/storage) only
    # becomes known once all tiles are done, so it arrives later via a
    # "TIMING" queue message (see make_live_qc_acquire_tiles) instead of cfg.
    focus_elapsed_s = float(cfg.get("focus_elapsed_s", 0.0) or 0.0)
    timing_info: Optional[Dict[str, float]] = None

    jitter_warn = float(cfg.get("jitter_warn_threshold", 1.5))
    jitter_dmin = float(cfg.get("jitter_display_min", 1.0))
    jitter_dmax = float(cfg.get("jitter_display_max", 2.5))
    sat_warn = float(cfg.get("outside_roi_sat_threshold", 30.0))
    stig_dmax = float(cfg.get("stig_display_max", 50.0))
    stig_centered_dmax = float(cfg.get("stig_centered_display_max", 30.0))

    radius = float(cfg.get("hex_radius_um") or 0.0)
    if radius <= 0.0:
        radius = _estimate_hex_radius(layout)

    xs = [float(r["x_um"]) for r in layout]
    ys = [float(r["y_um"]) for r in layout]
    idxs = [int(r["tile_idx"]) for r in layout]
    n = len(idxs)
    idx_to_pos = {tid: i for i, tid in enumerate(idxs)}

    plt.ion()
    fig = plt.figure(figsize=(18, 9))
    # 2 rows now, not 3 - the "Image quality per beam" panel (previously
    # gs[2, :]) is gone: per the operator (2026-09-05), a separate computer
    # will own image-quality scoring going forward, so this module has no
    # business computing or plotting it. See qc_algorithm.compute_single_image_quality
    # / aggregate_mfov_quality for the underlying per-tile scoring that still
    # feeds the Focus quality panel and live_qc_results.csv - NOT removed
    # here, only this panel and the full-91-beam recompute that fed it.
    gs = fig.add_gridspec(2, 3, height_ratios=[1, 1],
                           hspace=0.35, wspace=0.22, left=0.05, right=0.97, top=0.92, bottom=0.07)
    ax_focus = fig.add_subplot(gs[0, 0])
    ax_stig = fig.add_subplot(gs[0, 1])
    # The two stig panels (image-derived proxy here, actual/commanded next to
    # it) are kept side by side so they're easy to compare at a glance (per
    # operator request, 2026-09-03) - OL moved to row 2 to make room.
    ax_stigact = fig.add_subplot(gs[0, 2])
    # Outside-ROI/saturation panel dropped from the live display (per operator
    # request - not useful visually); the underlying pct_saturation_med /
    # is_blank_outside_roi values still drive baseline eligibility and the
    # "outside" counter below, they just no longer get their own hex-map panel.
    # Status/jitter hex-maps (previously here) were dropped in favor of the
    # actual commanded OL/StigX/StigY values below (per operator request,
    # 2026-09) - status/jitter outlier counts are still tracked and shown
    # in the stats panel, and the static per-region HTML snapshot (written
    # by _write_dashboard_html) still has its own status/jitter panels.
    ax_ol = fig.add_subplot(gs[1, 0])
    ax_stats = fig.add_subplot(gs[1, 1:])
    ax_stats.axis("off")

    # The focus-quality color scale is centered on the actual low/high
    # thresholds, not the (much wider) -100..100 display_min/display_max
    # range - otherwise the meaningful part of the scale gets squeezed into
    # a sliver of the colorbar and most colors never show up in practice.
    if focus_color_mode == "tiered":
        # Discrete red / amber / green - below low_quality_threshold is red,
        # at/above high_quality_threshold is green, between is amber. A small
        # pad around [low, high] gives the colorbar some visible red/green
        # margin instead of the outer edges being zero-width. Any
        # cmap_focus(norm_focus(quality)) call downstream (colorbar, static
        # HTML snapshot, live hex fill) picks this up automatically since
        # they all go through these same two objects.
        pad = max(1.0, (high_quality_threshold - low_quality_threshold) * 0.15)
        cmap_focus = ListedColormap(["#c0392b", "#f0a83a", "#2e8b3d"])
        norm_focus = BoundaryNorm(
            [low_quality_threshold - pad, low_quality_threshold,
             high_quality_threshold, high_quality_threshold + pad],
            ncolors=cmap_focus.N, clip=True,
        )
    else:
        cmap_focus = _get_cmap(str(cfg.get("colormap", "RdYlGn")))
        norm_focus = Normalize(vmin=low_quality_threshold, vmax=high_quality_threshold)
    cmap_stig = _get_cmap("bwr")
    cmap_outside = _get_cmap("Reds")
    cmap_jitter = _get_cmap("Reds")

    norm_stig = Normalize(vmin=-stig_centered_dmax, vmax=stig_centered_dmax)
    norm_outside = Normalize(vmin=0.0, vmax=100.0)
    norm_jitter = Normalize(vmin=jitter_dmin, vmax=jitter_dmax)

    # OL and actual (hardware) StigX/StigY are absolute physical values whose
    # scale isn't known up front - unlike jitter/the stig image-proxy above,
    # which have fixed, roughly instrument-independent display ranges. Both
    # norms auto-expand to the min/max actually seen so far as RESULT
    # messages arrive (see _autoscale_norm below), so the color scale always
    # spans whatever range this run's values actually occupy.
    cmap_ol = _get_cmap(str(cfg.get("ol_colormap", "viridis")))
    cmap_stigact = _get_cmap("inferno")
    norm_ol = Normalize(vmin=0.0, vmax=1.0)
    norm_stigact = Normalize(vmin=0.0, vmax=1.0)
    _ol_seen: Dict[str, Optional[float]] = {"lo": None, "hi": None}
    _stigact_seen: Dict[str, Optional[float]] = {"lo": None, "hi": None}

    def _autoscale_norm(seen: Dict[str, Optional[float]], norm: Any, value: float, pad_frac: float = 0.08) -> None:
        if value != value:  # NaN guard
            return
        lo = value if seen["lo"] is None else min(seen["lo"], value)
        hi = value if seen["hi"] is None else max(seen["hi"], value)
        seen["lo"], seen["hi"] = lo, hi
        span = hi - lo
        pad = span * pad_frac if span > 1e-9 else max(abs(hi), 1.0) * 0.05 + 1e-9
        norm.vmin, norm.vmax = lo - pad, hi + pad

    def _setup_layer(ax, title, fill=_PENDING_FILL):
        patches: Dict[int, Any] = {}
        for tile_idx, x, y in zip(idxs, xs, ys):
            p = RegularPolygon(
                (x, y),
                numVertices=6,
                radius=radius,
                orientation=math.radians(30.0),
                facecolor=fill,
                edgecolor=(0.35, 0.35, 0.35, 1.0),
                linewidth=0.4,
            )
            ax.add_patch(p)
            patches[tile_idx] = p
        ax.set_aspect("equal", adjustable="box")
        ax.set_xlim(min(xs) - 3 * radius, max(xs) + 3 * radius)
        ax.set_ylim(min(ys) - 3 * radius, max(ys) + 3 * radius)
        ax.invert_yaxis()
        ax.set_title(title, fontsize=10)
        ax.set_xlabel("X (µm)", fontsize=8)
        ax.set_ylabel("Y (µm)", fontsize=8)
        ax.tick_params(labelsize=7)
        return patches

    p_focus = _setup_layer(ax_focus, "① Focus quality")
    p_stig = _setup_layer(ax_stig, "② Stig (value − running median) + arrows")
    p_stigact = _setup_layer(ax_stigact, "③ Stig X/Y (actual, commanded) + arrows")
    p_ol = _setup_layer(ax_ol, "④ OL value (commanded)")

    arr_xs = np.asarray(xs, dtype=np.float64)
    arr_ys = np.asarray(ys, dtype=np.float64)
    U = np.zeros(n, dtype=np.float64)
    V = np.zeros(n, dtype=np.float64)
    M = np.zeros(n, dtype=np.float64)
    arrow_data_len = radius * 0.9
    arrow_scale = max(stig_dmax / max(arrow_data_len, 1e-6), 1e-6)
    Q = ax_stig.quiver(
        arr_xs, arr_ys, U, V, M,
        cmap=_get_cmap("inferno"),
        scale=arrow_scale,
        scale_units="xy",
        angles="xy",
        width=0.004,
        headwidth=4.0,
        headlength=5.0,
        headaxislength=4.5,
        alpha=0.9,
        pivot="tail",
        clim=(0.0, math.hypot(stig_dmax, stig_dmax)),
    )

    # Actual commanded StigX/StigY are real signed hardware values (unlike
    # the image-derived proxy above, which is always >=0), so U2/V2 carry
    # the true signed components instead of being clipped to positive - the
    # arrow can point into any quadrant. Scale/clim start at a placeholder
    # and are refreshed by _autoscale_norm's tracked seen-range each tile
    # (see the RESULT handling below), since the real magnitude range isn't
    # known until data starts arriving.
    U2 = np.zeros(n, dtype=np.float64)
    V2 = np.zeros(n, dtype=np.float64)
    M2 = np.zeros(n, dtype=np.float64)
    Q2 = ax_stigact.quiver(
        arr_xs, arr_ys, U2, V2, M2,
        cmap=cmap_stigact,
        scale=1.0,
        scale_units="xy",
        angles="xy",
        width=0.004,
        headwidth=4.0,
        headlength=5.0,
        headaxislength=4.5,
        alpha=0.9,
        pivot="tail",
        clim=(0.0, 1.0),
    )

    fig.colorbar(cm.ScalarMappable(norm=norm_focus, cmap=cmap_focus), ax=ax_focus, shrink=0.8, label="quality")
    fig.colorbar(cm.ScalarMappable(norm=norm_stig, cmap=cmap_stig), ax=ax_stig, shrink=0.8, label="(h−v) − median")
    cbar_ol = fig.colorbar(cm.ScalarMappable(norm=norm_ol, cmap=cmap_ol), ax=ax_ol, shrink=0.8, label="OL (um)")
    # Plain decimal ticks - matplotlib's default ScalarFormatter switches to
    # scientific/offset notation ("+1.95e1"-style) once the autoscaled range
    # (see _autoscale_norm) drifts into values it considers "large", which
    # is exactly the kind of number OL commonly lands on.
    cbar_ol.formatter = ScalarFormatter(useOffset=False)
    cbar_ol.formatter.set_scientific(False)
    cbar_ol.update_ticks()
    cbar_stigact = fig.colorbar(cm.ScalarMappable(norm=norm_stigact, cmap=cmap_stigact), ax=ax_stigact, shrink=0.8, label="|StigX,StigY|")

    # Status is no longer its own hex-map panel (see ax_ol above); acquisition
    # failures/errors are still flagged visually on the OL panel itself via a
    # red dashed outline (set in the RESULT handling below) instead of a
    # separate color-coded panel + legend.
    fail_legend = [
        Patch(facecolor=(0, 0, 0, 0), edgecolor="red", linestyle="--", linewidth=1.4, label="acquisition_fail/error/no_valid_images"),
        Patch(facecolor=_PENDING_FILL, edgecolor="grey", label="pending"),
    ]
    ax_ol.legend(handles=fail_legend, loc="lower right", fontsize=6, framealpha=0.85)

    stats_text = ax_stats.text(
        0.02, 0.98, "", transform=ax_stats.transAxes,
        va="top", ha="left", fontsize=11, family="monospace",
    )

    suptitle = fig.suptitle("Live MFOV QC", fontsize=13)

    # Add/remove button for the average (median) image quality figure shown
    # in the stats panel + suptitle (per operator request, 2026-09-06) -
    # placed in the bottom-right corner of ax_stats' own footprint, clear of
    # stats_text (top-left within the same axes).
    from matplotlib.widgets import Button
    show_avg_quality = {"on": True}
    _stats_pos = ax_stats.get_position()
    _btn_w, _btn_h = 0.12, 0.045
    ax_avgq_btn = fig.add_axes([_stats_pos.x1 - _btn_w - 0.01, _stats_pos.y0 + 0.01, _btn_w, _btn_h])
    btn_avgq = Button(ax_avgq_btn, "Hide avg quality")

    def _on_toggle_avg_quality(_event) -> None:
        show_avg_quality["on"] = not show_avg_quality["on"]
        btn_avgq.label.set_text("Hide avg quality" if show_avg_quality["on"] else "Show avg quality")
        _update_stats()

    btn_avgq.on_clicked(_on_toggle_avg_quality)

    plt.show(block=False)

    region_overview_info: Optional[Dict[str, Any]] = None

    def _render_region_overview() -> None:
        nonlocal region_overview_info
        if region_ov is None:
            return
        try:
            info = region_ov.render()
            if info:
                region_overview_info = info
        except Exception as exc:
            print(f"[Viewer] region overview render failed: {exc}")

    done = 0
    total = len(layout)
    last_save_done = 0
    # Cumulative timing for the periodic (every save_every tiles) writes -
    # see the STOP-time print below (2026-09-06 investigation into why
    # worker/viewer joins keep hitting their full 300s timeout).
    periodic_save_timing = {"savefig_s": 0.0, "overview_s": 0.0, "html_s": 0.0, "n_saves": 0}
    q_vals: List[float] = []
    fail_count = 0
    outside_count = 0
    jittery_count = 0
    stig_history: List[float] = []
    stig_outlier_count = 0
    jitter_history: List[float] = []
    jitter_outlier_count = 0
    # Left permanently empty (2026-09-05, see gs above): the live "Image
    # quality per beam" panel that used to consume this is gone - a
    # separate computer now owns image-quality scoring, so this module no
    # longer accumulates it. Still passed through to _write_dashboard_html
    # below (its own per-region beam-quality panel already degrades
    # gracefully to "no data yet" when this is empty), so that function's
    # signature didn't need to change.
    beam_quality_values: Dict[int, List[float]] = {}
    results_by_tile: Dict[int, Dict[str, Any]] = {}
    cmap_arrow = _get_cmap("inferno")
    html_path = results_dir / "live_qc_dashboard.html"
    # Persists across every _persist_html_now() call this region (see
    # _write_dashboard_html's tile_bmp_urls_cache param) so each tile's
    # folder is only glob()'d once, not re-scanned on every periodic save.
    tile_bmp_urls_cache: Dict[int, List[str]] = {}

    def _persist_html_now(refresh_block_index: bool = True) -> None:
        """Write the per-section dashboard HTML + (usually) refresh the
        block-level index.html.  Called both periodically (so partial
        results survive a viewer crash) and at STOP.

        refresh_block_index=False at the STOP call site: that write races
        against focus_map.py's own _refresh_block_index(), called right
        after run_one_region() returns in main() - which happens strictly
        after this viewer subprocess has been joined, so it always runs
        AFTER this one. A real run showed the block-level report stuck
        showing one region short even after the whole batch finished: this
        viewer process's own STOP-time write lands here while
        region_manifest.json still says THIS region is "running" (main()
        doesn't flip it to "done" until run_one_region() returns control),
        writing a stale snapshot to the SAME shared file main() is about to
        correctly rewrite a moment later - for every region except the very
        last one, that's harmless (the next region's own refresh calls
        re-read the by-then-correct manifest and paper over it), but the
        last region has no "next iteration" to self-heal a lost or
        transiently-failed final write (e.g. a network-share write/lock
        conflict between the two processes touching the same path within
        milliseconds of each other). Skipping the shared-file write here
        removes the redundant, race-prone write entirely; the per-region
        dashboard HTML below is unaffected and still gets its accurate
        final flush."""
        try:
            _write_dashboard_html(
                html_path,
                region_label=region_label,
                block_label=block_label,
                layout=layout,
                results_by_tile=results_by_tile,
                radius=radius,
                cmap_focus=cmap_focus, norm_focus=norm_focus,
                cmap_stig=cmap_stig, norm_stig=norm_stig,
                cmap_outside=cmap_outside, norm_outside=norm_outside,
                cmap_jitter=cmap_jitter, norm_jitter=norm_jitter,
                cmap_arrow=cmap_arrow,
                stig_dmax=stig_dmax,
                jitter_dmax=jitter_dmax,
                counters={
                    "q_vals": q_vals,
                    "done": done,
                    "total": total,
                    "fail": fail_count,
                    "outside": outside_count,
                    "jittery": jittery_count,
                },
                region_name=str(cfg.get("region_name") or region_label),
                imaging_json_path=cfg.get("imaging_json_path"),
                low_quality_threshold=low_quality_threshold,
                high_quality_threshold=high_quality_threshold,
                region_overview=region_overview_info,
                focus_elapsed_s=focus_elapsed_s,
                timing_info=timing_info,
                beam_quality_values=beam_quality_values,
                block_folder_path=str(cfg.get("block_folder") or html_path.parent.parent.parent),
                tile_bmp_urls_cache=tile_bmp_urls_cache,
            )
        except Exception as exc:
            print(f"[Viewer] HTML dashboard write failed: {exc}")
        if refresh_block_index:
            try:
                _write_block_index_html(
                    Path(cfg.get("block_folder")
                         or html_path.parent.parent.parent),
                    block_label=block_label,
                )
            except Exception as exc:
                print(f"[Viewer] block index write failed: {exc}")

    def _running_median(values: List[float], min_n: int = 3) -> float:
        if len(values) < min_n:
            return 0.0
        return float(np.median(np.asarray(values, dtype=np.float64)))

    def _mad_outlier_threshold(values: List[float], min_n: int = 5, sigma: float = 3.0) -> float:
        if len(values) < min_n:
            return float("inf")
        arr = np.asarray(values, dtype=np.float64)
        med = float(np.median(arr))
        mad = float(np.median(np.abs(arr - med)))
        return sigma * 1.4826 * max(mad, 1e-9)

    def _stig_running_median() -> float:
        return _running_median(stig_history)

    def _stig_outlier_threshold() -> float:
        return _mad_outlier_threshold(stig_history)

    def _jitter_running_median() -> float:
        return _running_median(jitter_history)

    def _jitter_outlier_threshold() -> float:
        return _mad_outlier_threshold(jitter_history)

    def _update_stats(last_tile: Optional[int] = None, last_q: Optional[float] = None) -> None:
        med_q = float(np.median(np.asarray(q_vals, dtype=np.float64))) if q_vals else float("nan")
        stig_med = _stig_running_median()
        jitter_med = _jitter_running_median()
        lines = [
            f"Region : {region_label or '—'}",
            f"Block  : {block_label or '—'}",
            "",
            f"Done       : {done}/{total}",
        ]
        if show_avg_quality["on"]:
            lines.append(f"Median Q   : {med_q:.2f}" if q_vals else "Median Q   : —")
        lines += [
            f"Fails      : {fail_count}",
            f"Outside-ROI: {outside_count}",
            f"Jittery abs: {jittery_count}",
            f"Stig median: {stig_med:+.2f}" if len(stig_history) >= 3 else "Stig median: —",
            f"Stig outl. : {stig_outlier_count}",
            f"Jit median : {jitter_med:.4f}" if len(jitter_history) >= 3 else "Jit median : —",
            f"Jit outl.  : {jitter_outlier_count}",
        ]
        if focus_elapsed_s > 0.0 or timing_info is not None:
            lines.append("")
            lines.append(f"Focus time : {_fmt_duration(focus_elapsed_s)}" if focus_elapsed_s > 0.0
                          else "Focus time : —")
            if timing_info is not None:
                lines.append(f"Imaging time: {_fmt_duration(float(timing_info.get('wall_s', 0.0)))} "
                              f"({int(timing_info.get('n_tiles', 0))} tiles)")
                lines.append(f"Total time : {_fmt_duration(focus_elapsed_s + float(timing_info.get('wall_s', 0.0)))}")
            else:
                lines.append("Imaging time: (in progress)")
        if last_tile is not None:
            tail = f"Last tile={last_tile}"
            if last_q is not None and not math.isnan(last_q):
                tail += f"  Q={last_q:.2f}"
            lines.append("")
            lines.append(tail)
        stats_text.set_text("\n".join(lines))
        qmed_part = f"Q_med={med_q:.2f}  " if show_avg_quality["on"] else ""
        suptitle.set_text(
            f"Live MFOV QC — {region_label or '—'}  ({done}/{total})  "
            f"{qmed_part}fails={fail_count}  outside={outside_count}  jitter={jittery_count}"
        )

    _update_stats()

    while True:
        try:
            msg = viewer_queue.get(timeout=0.10)
        except queue.Empty:
            plt.pause(0.05)
            continue

        if msg is None or msg.get("type") == "STOP":
            # The full 91-beam recompute that used to run here (per operator
            # request, 2026-09-05) is gone for good, not just paused: a
            # separate computer now owns all image-quality scoring, so this
            # module has no business re-scoring every beam at region end
            # either. If that division of labor ever changes again, see git
            # history for select_representative_tiles_for_full_beam_qc /
            # compute_full_beam_quality_for_region (removed the same day).
            _update_stats()
            fig.canvas.draw_idle()
            # Timed (2026-09-06): the worker/viewer join in stop_live_qc_runtime
            # (main_live_qc.py) has repeatedly hit its full 300s timeout on
            # every region even after fixing RegionOverviewAccumulator's
            # quadratic recomposite - these prints exist to find out which of
            # the three STOP-time steps below (if any) is where that time
            # actually goes, instead of guessing again.
            _t_stop0 = time.perf_counter()
            try:
                fig.savefig(snapshot_path, dpi=150)
            except Exception:
                pass
            _t_savefig = time.perf_counter()
            _render_region_overview()
            _t_overview = time.perf_counter()
            # refresh_block_index=False: see _persist_html_now's docstring -
            # focus_map.py's own post-run_one_region() refresh is authoritative
            # and always runs after this viewer has been joined/exited.
            _persist_html_now(refresh_block_index=False)
            _t_html = time.perf_counter()
            print(f"[LiveQC] STOP-time write timing: savefig={_t_savefig - _t_stop0:.2f}s "
                  f"region_overview={_t_overview - _t_savefig:.2f}s "
                  f"dashboard_html={_t_html - _t_overview:.2f}s")
            print(f"[LiveQC] Periodic (every {save_every} tiles) write timing over "
                  f"{periodic_save_timing['n_saves']} save(s): "
                  f"savefig_total={periodic_save_timing['savefig_s']:.2f}s "
                  f"region_overview_total={periodic_save_timing['overview_s']:.2f}s "
                  f"dashboard_html_total={periodic_save_timing['html_s']:.2f}s")
            plt.pause(1.0)
            plt.close(fig)
            break

        if msg.get("type") == "TIMING":
            timing_info = {
                "wall_s": float(msg.get("wall_s", 0.0) or 0.0),
                "move_s": float(msg.get("move_s", 0.0) or 0.0),
                "scan_s": float(msg.get("scan_s", 0.0) or 0.0),
                "storage_s": float(msg.get("storage_s", 0.0) or 0.0),
                "n_tiles": int(msg.get("n_tiles", 0) or 0),
            }
            if msg.get("focus_s"):
                focus_elapsed_s = float(msg["focus_s"])
            # focus_af_s/focus_as_s/focus_move_s split the focus phase above
            # into AF+minimize-OL ("focus"), AS ("stig"), and per-point
            # stage/B2F overhead (see focus_map.py's measure_one_focus_point /
            # _focus_point_timing_log) - absent from older runs, so callers
            # should treat 0.0 here as "not available", not "took no time".
            focus_af_s = float(msg.get("focus_af_s", 0.0) or 0.0)
            focus_as_s = float(msg.get("focus_as_s", 0.0) or 0.0)
            focus_move_s = float(msg.get("focus_move_s", 0.0) or 0.0)
            # Persisted to disk (not just kept in memory) so the block-level
            # index page (_write_block_index_html) can show this section's
            # timing even though it's a separate process reading this section
            # after the fact, long after this viewer has exited.
            try:
                (results_dir / "live_qc_timing.json").write_text(
                    dumps_json({
                        "focus_s": focus_elapsed_s,
                        "focus_af_s": focus_af_s,
                        "focus_as_s": focus_as_s,
                        "focus_move_s": focus_move_s,
                        **timing_info,
                    }), encoding="utf-8"
                )
            except Exception as exc:
                print(f"[Viewer] timing.json write failed: {exc}")
            _update_stats()
            plt.pause(0.01)
            continue

        if msg.get("type") != "RESULT":
            plt.pause(0.01)
            continue

        result = msg["result"]
        tile_idx = int(result["tile_idx"])
        results_by_tile[tile_idx] = result
        if region_ov is not None and result.get("tile_folder"):
            region_ov.add_mfov(tile_idx, result["tile_folder"])
        q = float(result.get("quality", -100.0) or -100.0)
        stig_x = float(result.get("stig_x_med", 0.0) or 0.0)
        stig_y = float(result.get("stig_y_med", 0.0) or 0.0)
        stig_signed = float(result.get("stig_anisotropy", stig_x - stig_y) or 0.0)
        jitter = float(result.get("jitter_score_med", 0.0) or 0.0)
        is_blank = bool(result.get("is_blank_outside_roi", False))
        is_jittery = bool(result.get("is_jittery", jitter >= jitter_warn))
        status_cat = str(result.get("status_category", result.get("status", "ok")) or "ok")
        ol_pred = result.get("ol_pred")
        stig_x_pred = result.get("stig_x_pred")
        stig_y_pred = result.get("stig_y_pred")

        # Only feed valid in-ROI MFOVs into the medians/MAD; outside-ROI tiles
        # produce garbage horz/vert/jitter values that would poison the baseline,
        # especially when the smart-imaging path visits ROI edges first.
        contributes_to_baseline = (status_cat == "ok") and (not is_blank)

        if contributes_to_baseline:
            stig_history.append(stig_signed)
        stig_med_now = _stig_running_median()
        stig_dev = stig_signed - stig_med_now
        stig_thresh = _stig_outlier_threshold()
        is_stig_outlier = contributes_to_baseline and abs(stig_dev) > stig_thresh

        if contributes_to_baseline:
            jitter_history.append(jitter)
        jitter_med_now = _jitter_running_median()
        jitter_dev = jitter - jitter_med_now
        jitter_thresh = _jitter_outlier_threshold()
        is_jitter_outlier = contributes_to_baseline and abs(jitter_dev) > jitter_thresh

        is_fail_status = status_cat in ("acquisition_fail", "error", "no_valid_images")

        if tile_idx in p_focus:
            p_focus[tile_idx].set_facecolor(cmap_focus(norm_focus(q)))
            p_stig[tile_idx].set_facecolor(cmap_stig(norm_stig(stig_dev)))
            p_stig[tile_idx].set_hatch("///" if is_stig_outlier else "")
            p_stig[tile_idx].set_edgecolor("black" if is_stig_outlier else (0.35, 0.35, 0.35, 1.0))
            p_stig[tile_idx].set_linewidth(0.9 if is_stig_outlier else 0.4)

            # ③ OL value actually commanded for this tile (not an image-
            # derived quantity - passed straight through from the plan that
            # triggered acquisition, see emit_completed_tile/current_tile_meta
            # in main_live_qc.py). Failures still carry a real commanded OL
            # (the value was set before the failure happened), so they still
            # get a genuine color; a red dashed outline flags the failure
            # instead of a dedicated status color.
            if ol_pred is not None:
                # Displayed in microns (a physically meaningful focus unit),
                # converted from the raw commanded uA - the result dict/CSV
                # keep the original uA value unchanged, this conversion is
                # display-only.
                ol_val = float(ol_pred) / _OL_UA_PER_UM
                _autoscale_norm(_ol_seen, norm_ol, ol_val)
                p_ol[tile_idx].set_facecolor(cmap_ol(norm_ol(ol_val)))
            p_ol[tile_idx].set_hatch("xx" if is_fail_status else "")
            p_ol[tile_idx].set_edgecolor("red" if is_fail_status else (0.35, 0.35, 0.35, 1.0))
            p_ol[tile_idx].set_linewidth(1.4 if is_fail_status else 0.4)
            try:
                cbar_ol.update_normal(cbar_ol.mappable)
                # Re-assert plain-decimal ticks - defensive, in case
                # update_normal's redraw resets the formatter back to
                # matplotlib's scientific-notation default.
                cbar_ol.formatter.set_scientific(False)
                cbar_ol.update_ticks()
            except Exception:
                pass

            # ④ actual (hardware) commanded StigX/StigY - real signed values,
            # unlike the image-derived proxy in panel ②.
            if stig_x_pred is not None and stig_y_pred is not None:
                sx = float(stig_x_pred)
                sy = float(stig_y_pred)
                mag = math.hypot(sx, sy)
                _autoscale_norm(_stigact_seen, norm_stigact, mag)
                p_stigact[tile_idx].set_facecolor(cmap_stigact(norm_stigact(mag)))
                pos = idx_to_pos.get(tile_idx)
                if pos is not None:
                    U2[pos] = sx
                    V2[pos] = sy
                    M2[pos] = mag
                    arrow_span = max(_stigact_seen["hi"] or 1.0, 1e-9)
                    Q2.scale = max(arrow_span / max(radius * 0.9, 1e-6), 1e-6)
                    Q2.set_clim(0.0, arrow_span)
                    Q2.set_UVC(U2, V2, M2)
            p_stigact[tile_idx].set_hatch("xx" if is_fail_status else "")
            p_stigact[tile_idx].set_edgecolor("red" if is_fail_status else (0.35, 0.35, 0.35, 1.0))
            p_stigact[tile_idx].set_linewidth(1.4 if is_fail_status else 0.4)
            try:
                cbar_stigact.update_normal(cbar_stigact.mappable)
            except Exception:
                pass

        pos = idx_to_pos.get(tile_idx)
        if pos is not None:
            u = max(stig_x, 0.0)
            v = max(stig_y, 0.0)
            U[pos] = u
            V[pos] = v
            M[pos] = math.hypot(u, v)
            Q.set_UVC(U, V, M)

        done += 1
        if status_cat in ("acquisition_fail", "error", "no_valid_images"):
            fail_count += 1
        if is_blank:
            outside_count += 1
        if is_jittery:
            jittery_count += 1
        if is_stig_outlier:
            stig_outlier_count += 1
        if is_jitter_outlier:
            jitter_outlier_count += 1
        if status_cat == "ok":
            q_vals.append(q)

        _update_stats(last_tile=tile_idx, last_q=q)
        fig.canvas.draw_idle()
        plt.pause(0.001)

        if save_every > 0 and (done - last_save_done) >= save_every:
            _t_ps0 = time.perf_counter()
            try:
                fig.savefig(snapshot_path, dpi=150)
            except Exception:
                pass
            _t_ps1 = time.perf_counter()
            # Also flush per-section HTML + refresh the block index now,
            # so the section's dashboard exists on disk even if the
            # viewer is killed before STOP arrives (the original failure
            # mode where only the last STOP-d section had any HTML).
            _render_region_overview()
            _t_ps2 = time.perf_counter()
            _persist_html_now()
            _t_ps3 = time.perf_counter()
            periodic_save_timing["savefig_s"] += _t_ps1 - _t_ps0
            periodic_save_timing["overview_s"] += _t_ps2 - _t_ps1
            periodic_save_timing["html_s"] += _t_ps3 - _t_ps2
            periodic_save_timing["n_saves"] += 1
            # Printed immediately, not just accumulated for a STOP-time
            # summary (2026-09-06): stop_live_qc_runtime force-terminates
            # this process if it's still alive after a 300s join timeout, so
            # a viewer stuck mid-backlog never reaches STOP at all and an
            # end-of-run-only summary would report nothing. qsize() reveals
            # whether the viewer is genuinely falling behind acquisition (a
            # growing backlog) or keeping up but each write is just slow.
            try:
                _qsize = viewer_queue.qsize()
            except Exception:
                _qsize = -1
            print(f"[LiveQC] periodic save @ done={done}/{total}: "
                  f"savefig={_t_ps1 - _t_ps0:.2f}s region_overview={_t_ps2 - _t_ps1:.2f}s "
                  f"dashboard_html={_t_ps3 - _t_ps2:.2f}s viewer_queue_backlog={_qsize}")
            last_save_done = done


def emit_completed_tile(task_queue: Any, tile_meta: Dict[str, Any]) -> None:
    task_queue.put(
        {
            "tile_idx": int(tile_meta["tile_idx"]),
            "x_um": float(tile_meta["x_um"]),
            "y_um": float(tile_meta["y_um"]),
            "tile_folder": str(tile_meta["tile_folder"]),
            "prefix": tile_meta.get("prefix"),
            "ol_pred": tile_meta.get("ol_pred"),
            "stig_x_pred": tile_meta.get("stig_x_pred"),
            "stig_y_pred": tile_meta.get("stig_y_pred"),
        }
    )
