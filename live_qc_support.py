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
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import numpy as np

from qc_algorithm import aggregate_mfov_quality, compute_single_image_quality, dumps_json

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

    import sys
    import traceback
    error_log = results_dir / "live_qc_worker_errors.txt"

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
            )
            result = mfov.to_dict()

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


def _write_block_index_html(block_folder: Path, block_label: str = "") -> None:
    """Scan <block_folder>/*/LiveQC/live_qc_dashboard.html and emit <block_folder>/index.html."""
    block_folder = Path(block_folder)
    if not block_folder.is_dir():
        return
    sections: List[Dict[str, Any]] = []
    for region_dir in sorted(block_folder.iterdir()):
        if not region_dir.is_dir():
            continue
        live_dir = region_dir / "LiveQC"
        dash_html = live_dir / "live_qc_dashboard.html"
        if not dash_html.exists():
            continue
        stats = _read_section_stats_from_csv(live_dir / "live_qc_results.csv")
        sections.append({
            "name": region_dir.name,
            "rel_html": str(dash_html.relative_to(block_folder)).replace("\\", "/"),
            "rel_png": str((live_dir / "live_qc_dashboard.png").relative_to(block_folder)).replace("\\", "/")
                       if (live_dir / "live_qc_dashboard.png").exists() else None,
            **stats,
        })

    rows = []
    for s in sections:
        png_cell = (
            f'<a href="{_html.escape(s["rel_png"])}" target="_blank">'
            f'<img src="{_html.escape(s["rel_png"])}" alt="" style="height:60px;border:1px solid #ccc;"/></a>'
            if s["rel_png"] else "—"
        )
        med_q_str = "—" if (s["median_q"] != s["median_q"]) else f'{s["median_q"]:.2f}'
        rows.append(
            f'<tr>'
            f'<td><a href="{_html.escape(s["rel_html"])}" target="_blank"><strong>{_html.escape(s["name"])}</strong></a></td>'
            f'<td>{s["n_total"]}</td>'
            f'<td>{med_q_str}</td>'
            f'<td>{s["fails"]}</td>'
            f'<td>{s["outside"]}</td>'
            f'<td>{s["jittery"]}</td>'
            f'<td style="text-align:center;">{png_cell}</td>'
            f'</tr>'
        )

    doc = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>QC Index — {_html.escape(block_label)}</title>
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
</style>
</head>
<body>
<header>
  <h1>QC Index — {_html.escape(block_label or block_folder.name)}</h1>
  <div class="summary">{len(sections)} section(s) found in <code>{_html.escape(str(block_folder))}</code></div>
</header>
<main>
"""
    if sections:
        doc += (
            '<table>'
            '<thead><tr>'
            '<th>Section</th><th>Tiles</th><th>Median Q</th><th>Fails</th>'
            '<th>Outside-ROI</th><th>Jittery</th><th>Preview</th>'
            '</tr></thead><tbody>'
            + "".join(rows)
            + '</tbody></table>'
        )
    else:
        doc += '<div class="empty">No section dashboards found yet.</div>'
    doc += "\n</main>\n</body>\n</html>\n"

    (block_folder / "index.html").write_text(doc, encoding="utf-8")


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
) -> None:
    if not layout:
        return

    xs = [float(r["x_um"]) for r in layout]
    ys = [float(r["y_um"]) for r in layout]
    idxs = [int(r["tile_idx"]) for r in layout]

    # Pre-scan each tile's folder for BMPs so the click handler can
    # display them inverted (the raw acquisitions display black/white
    # reversed in normal viewers; this is a presentation-only flip,
    # the .bmp files on disk are NOT modified).
    tile_bmp_urls: Dict[int, List[str]] = {}
    for _ti in idxs:
        _r = results_by_tile.get(_ti)
        if not _r:
            continue
        _folder = _r.get("tile_folder")
        if not _folder:
            continue
        try:
            _bmps = sorted(str(p) for p in Path(_folder).glob("*.bmp"))
            if _bmps:
                tile_bmp_urls[int(_ti)] = [_path_to_file_url(b) for b in _bmps]
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

    panels = [
        ("focus",   "① Focus quality"),
        ("stig",    "② Stig anisotropy + arrows"),
        ("status",  "③ Status"),
        ("outside", "④ Outside-ROI / saturation %"),
        ("jitter",  "⑤ Jitter score"),
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

    panels_html = []
    for pid, title in panels:
        panels_html.append(
            f'<section class="panel"><h3>{_html.escape(title)}</h3>{_panel_svg(pid)}</section>'
        )
    stig_med_str = f"{stig_med_final:+.2f}" if len(stig_vals_all) >= 3 else "—"
    jitter_med_str = f"{jitter_med_final:.4f}" if len(jitter_vals_all) >= 3 else "—"
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
        f'</dl>'
        f'<div class="legend"><strong>Status legend</strong><br>{status_legend_html}</div>'
        f'<div class="legend" style="margin-top:8px;"><strong>Stig:</strong> hex color = (h−v) − region median; arrows = direction. <strong>Jitter:</strong> hex color = std(top {int(0.05*100)}% of rows) / std(rest), per-image self-referencing — 1.0 ≈ clean, &gt;1.5 mild, &gt;2.5 severe. Hatched border on either panel = MAD outlier (&gt;3σ).</div>'
        f'</section>'
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
                "blank": False,
                "jittery_abs": False,
                "stig_outlier": False,
                "jitter_outlier": False,
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
            "blank": bool(r.get("is_blank_outside_roi", False)),
            "jittery_abs": bool(r.get("is_jittery", False)),
            "stig_outlier": bool(is_baseline and abs(raw_stig - stig_med_final) > stig_outlier_threshold),
            "jitter_outlier": bool(is_baseline and abs(raw_jitter - jitter_med_final) > jitter_outlier_threshold),
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
  .retake-grid {{ display:grid; grid-template-columns: 3fr 1fr; gap:14px; }}
  .retake-grid svg {{ width:100%; height:auto; display:block; }}
  .retake-grid svg .hex {{ cursor:pointer; transition: filter 0.08s ease; }}
  .retake-grid svg .hex.selected {{ stroke:#0a7; stroke-width:3; filter: drop-shadow(0 0 3px rgba(0,128,80,0.4)); }}
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
    Fails {fail_count} &nbsp;|&nbsp; Outside-ROI {outside_count} &nbsp;|&nbsp; Jittery {jittery_count}
    <br><small>Click a hex to view its BMPs inverted (black/white-corrected, file unchanged). Cmd/Ctrl+click opens the tile folder. Shift+click copies the path.</small>
  </div>
</header>
<main class="grid">
{chr(10).join(panels_html)}
</main>
<section class="retake-section" id="retake-section">
  <h3>⑥ Retake selection</h3>
  <div class="retake-controls">
    <button id="btn-pick-file" class="primary">Pick wafer retake JSON file…</button>
    <button id="btn-save" disabled>Save</button>
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
    <span class="key">File:</span> <span id="retake-file" class="file">(not picked)</span>
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
    const imgs = urls.map(function(u) {{
      const safe = String(u).replace(/"/g, '&quot;');
      return '<figure><img src="' + safe + '" loading="lazy" '
           + 'style="max-width:100%;display:block;filter:invert(1);background:#000;">'
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
      + '</style></head><body>'
      + '<h1>Tile ' + tileIdx + ' — ' + urls.length + ' BMP(s), display-inverted</h1>'
      + '<div class="note">Files on disk are unchanged. The black↔white flip '
      +   'is applied via CSS <code>filter: invert(1)</code> for viewing only.</div>'
      + imgs + '</body></html>'
    );
    w.document.close();
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
    let baseFill = '#ebebeb';
    if (!t.pending) {{
      if (t.blank) baseFill = '#666';
      else if (t.low_q) baseFill = '#ffd980';
      else if (t.stig_outlier || t.jitter_outlier) baseFill = '#ffb0b0';
      else baseFill = '#dde6dd';
    }}
    el.setAttribute('fill', baseFill);
    el.setAttribute('stroke', '#555');
    el.setAttribute('stroke-width', '0.5');
    el.dataset.tile = t.tile_idx;
    if (t.pending) el.style.cursor = 'not-allowed';
    const title = document.createElementNS(svgNS, 'title');
    let lines = [
      `tile ${{t.tile_idx}}`,
      `xy=(${{t.x_um.toFixed(1)}}, ${{t.y_um.toFixed(1)}})`,
    ];
    if (t.pending) {{ lines.push('pending'); }}
    else {{
      if (t.quality !== null) lines.push(`Q=${{t.quality.toFixed(2)}}`);
      const flags = [];
      if (t.low_q) flags.push('low_q');
      if (t.blank) flags.push('outside_roi');
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
  function toggle(tile_idx) {{
    const t = tileById.get(tile_idx);
    if (!t || t.pending) return;
    if (selected.has(tile_idx)) selected.delete(tile_idx);
    else selected.add(tile_idx);
    refreshHex(tile_idx);
    updateStatus();
  }}
  function setMany(predicate, additive=false) {{
    if (!additive) selected.clear();
    for (const t of tiles) {{
      if (t.pending) continue;
      if (predicate(t)) selected.add(t.tile_idx);
    }}
    for (const t of tiles) refreshHex(t.tile_idx);
    updateStatus();
  }}

  document.getElementById('btn-flagged').onclick = () => setMany(t => t.low_q || t.blank || t.stig_outlier || t.jitter_outlier);
  document.getElementById('btn-low-q').onclick = () => setMany(t => t.low_q, true);
  document.getElementById('btn-blank').onclick = () => setMany(t => t.blank, true);
  document.getElementById('btn-jitter').onclick = () => setMany(t => t.jitter_outlier, true);
  document.getElementById('btn-stig').onclick = () => setMany(t => t.stig_outlier, true);
  document.getElementById('btn-all').onclick = () => setMany(t => !t.blank);
  document.getElementById('btn-clear').onclick = () => {{ selected.clear(); for (const t of tiles) refreshHex(t.tile_idx); updateStatus(); }};

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

  // ---------- File System Access API ----------
  let waferHandle = null;

  function setFileLabel(text) {{ document.getElementById('retake-file').textContent = text; }}

  async function pickFile() {{
    if (!window.showSaveFilePicker) {{
      alert('Your browser does not support File System Access API. Use Chrome or Edge to save retake JSON in-place.');
      return;
    }}
    try {{
      const handle = await window.showSaveFilePicker({{
        suggestedName: (RETAKE_DATA.wafer_metadata && RETAKE_DATA.wafer_metadata.Name ? RETAKE_DATA.wafer_metadata.Name : 'wafer') + '_retake.json',
        types: [{{description:'Imaging JSON', accept:{{'application/json':['.json']}} }}],
      }});
      waferHandle = handle;
      setFileLabel(handle.name);
      document.getElementById('btn-save').disabled = false;
      // try to load existing content
      try {{
        const file = await handle.getFile();
        if (file.size > 0) {{
          const data = JSON.parse(await file.text());
          loadExistingSelections(data);
        }}
      }} catch (e) {{}}
    }} catch (e) {{
      if (e && e.name !== 'AbortError') alert('File pick failed: ' + e.message);
    }}
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

  async function save() {{
    if (!waferHandle) {{ await pickFile(); if (!waferHandle) return; }}

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
          alert('Failed to build retake XML: ' + e.message);
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
    }} catch (e) {{
      alert('Save failed: ' + e.message);
    }}
  }}

  document.getElementById('btn-pick-file').onclick = pickFile;
  document.getElementById('btn-save').onclick = save;
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
    from matplotlib.colors import Normalize
    from matplotlib.patches import RegularPolygon, Patch

    def _get_cmap(name: str):
        if hasattr(matplotlib, "colormaps"):
            return matplotlib.colormaps[name]
        return cm.get_cmap(name)

    display_min = float(cfg.get("display_min", -100.0))
    display_max = float(cfg.get("display_max", 100.0))
    save_every = int(cfg.get("save_every", 5))
    results_dir = Path(cfg["results_dir"])
    results_dir.mkdir(parents=True, exist_ok=True)
    snapshot_path = results_dir / "live_qc_dashboard.png"

    # Per-region downsampled overview accumulator (best-effort). Caches each
    # MFOV's overview thumbnail at its layout position; rendered at save_every + STOP.
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
    fig = plt.figure(figsize=(18, 10))
    gs = fig.add_gridspec(2, 3, hspace=0.30, wspace=0.22, left=0.05, right=0.97, top=0.92, bottom=0.06)
    ax_focus = fig.add_subplot(gs[0, 0])
    ax_stig = fig.add_subplot(gs[0, 1])
    ax_status = fig.add_subplot(gs[0, 2])
    ax_outside = fig.add_subplot(gs[1, 0])
    ax_jitter = fig.add_subplot(gs[1, 1])
    ax_stats = fig.add_subplot(gs[1, 2])
    ax_stats.axis("off")

    cmap_focus = _get_cmap(str(cfg.get("colormap", "RdYlGn")))
    cmap_stig = _get_cmap("bwr")
    cmap_outside = _get_cmap("Reds")
    cmap_jitter = _get_cmap("Reds")

    norm_focus = Normalize(vmin=display_min, vmax=display_max)
    norm_stig = Normalize(vmin=-stig_centered_dmax, vmax=stig_centered_dmax)
    norm_outside = Normalize(vmin=0.0, vmax=100.0)
    norm_jitter = Normalize(vmin=jitter_dmin, vmax=jitter_dmax)

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
    p_status = _setup_layer(ax_status, "③ Status")
    p_outside = _setup_layer(ax_outside, "④ Outside-ROI / saturation %")
    p_jitter = _setup_layer(ax_jitter, "⑤ Jitter (top-band vs rest, per image)")

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

    fig.colorbar(cm.ScalarMappable(norm=norm_focus, cmap=cmap_focus), ax=ax_focus, shrink=0.8, label="quality")
    fig.colorbar(cm.ScalarMappable(norm=norm_stig, cmap=cmap_stig), ax=ax_stig, shrink=0.8, label="(h−v) − median")
    fig.colorbar(cm.ScalarMappable(norm=norm_outside, cmap=cmap_outside), ax=ax_outside, shrink=0.8, label="% sat")
    fig.colorbar(cm.ScalarMappable(norm=norm_jitter, cmap=cmap_jitter), ax=ax_jitter, shrink=0.8, label="top/rest ratio")

    status_legend = [
        Patch(facecolor=_STATUS_COLORS["ok"], label="ok"),
        Patch(facecolor=_STATUS_COLORS["insufficient_signal"], label="insufficient_signal"),
        Patch(facecolor=_STATUS_COLORS["acquisition_fail"], label="acquisition_fail"),
        Patch(facecolor=_STATUS_COLORS["no_valid_images"], label="no_valid_images"),
        Patch(facecolor=_STATUS_COLORS["error"], label="error"),
        Patch(facecolor=_STATUS_COLORS["outside_roi"], label="outside_roi"),
        Patch(facecolor=_PENDING_FILL, edgecolor="grey", label="pending"),
    ]
    ax_status.legend(handles=status_legend, loc="lower right", fontsize=7, framealpha=0.85)

    stats_text = ax_stats.text(
        0.02, 0.98, "", transform=ax_stats.transAxes,
        va="top", ha="left", fontsize=11, family="monospace",
    )

    suptitle = fig.suptitle("Live MFOV QC", fontsize=13)
    plt.show(block=False)

    done = 0
    total = len(layout)
    last_save_done = 0
    q_vals: List[float] = []
    fail_count = 0
    outside_count = 0
    jittery_count = 0
    stig_history: List[float] = []
    stig_outlier_count = 0
    jitter_history: List[float] = []
    jitter_outlier_count = 0
    results_by_tile: Dict[int, Dict[str, Any]] = {}
    cmap_arrow = _get_cmap("inferno")
    html_path = results_dir / "live_qc_dashboard.html"

    def _persist_html_now() -> None:
        """Write the per-section dashboard HTML + refresh the
        block-level index.html.  Called both periodically (so partial
        results survive a viewer crash) and at STOP."""
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
            )
        except Exception as exc:
            print(f"[Viewer] HTML dashboard write failed: {exc}")
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
            f"Median Q   : {med_q:.2f}" if q_vals else "Median Q   : —",
            f"Fails      : {fail_count}",
            f"Outside-ROI: {outside_count}",
            f"Jittery abs: {jittery_count}",
            f"Stig median: {stig_med:+.2f}" if len(stig_history) >= 3 else "Stig median: —",
            f"Stig outl. : {stig_outlier_count}",
            f"Jit median : {jitter_med:.4f}" if len(jitter_history) >= 3 else "Jit median : —",
            f"Jit outl.  : {jitter_outlier_count}",
        ]
        if last_tile is not None:
            tail = f"Last tile={last_tile}"
            if last_q is not None and not math.isnan(last_q):
                tail += f"  Q={last_q:.2f}"
            lines.append("")
            lines.append(tail)
        stats_text.set_text("\n".join(lines))
        suptitle.set_text(
            f"Live MFOV QC — {region_label or '—'}  ({done}/{total})  "
            f"Q_med={med_q:.2f}  fails={fail_count}  outside={outside_count}  jitter={jittery_count}"
        )

    _update_stats()

    while True:
        try:
            msg = viewer_queue.get(timeout=0.10)
        except queue.Empty:
            plt.pause(0.05)
            continue

        if msg is None or msg.get("type") == "STOP":
            _update_stats()
            fig.canvas.draw_idle()
            try:
                fig.savefig(snapshot_path, dpi=150)
            except Exception:
                pass
            _persist_html_now()
            if region_ov is not None:
                region_ov.render()
            plt.pause(1.0)
            plt.close(fig)
            break

        if msg.get("type") != "RESULT":
            plt.pause(0.01)
            continue

        result = msg["result"]
        tile_idx = int(result["tile_idx"])
        results_by_tile[tile_idx] = result
        if region_ov is not None and result.get("overview_mfov_png"):
            region_ov.add_png(tile_idx, result["overview_mfov_png"])
        q = float(result.get("quality", -100.0) or -100.0)
        stig_x = float(result.get("stig_x_med", 0.0) or 0.0)
        stig_y = float(result.get("stig_y_med", 0.0) or 0.0)
        stig_signed = float(result.get("stig_anisotropy", stig_x - stig_y) or 0.0)
        sat = float(result.get("pct_saturation_med", 0.0) or 0.0)
        jitter = float(result.get("jitter_score_med", 0.0) or 0.0)
        is_blank = bool(result.get("is_blank_outside_roi", False))
        is_jittery = bool(result.get("is_jittery", jitter >= jitter_warn))
        status_cat = str(result.get("status_category", result.get("status", "ok")) or "ok")

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

        if tile_idx in p_focus:
            p_focus[tile_idx].set_facecolor(cmap_focus(norm_focus(q)))
            p_stig[tile_idx].set_facecolor(cmap_stig(norm_stig(stig_dev)))
            p_stig[tile_idx].set_hatch("///" if is_stig_outlier else "")
            p_stig[tile_idx].set_edgecolor("black" if is_stig_outlier else (0.35, 0.35, 0.35, 1.0))
            p_stig[tile_idx].set_linewidth(0.9 if is_stig_outlier else 0.4)
            p_status[tile_idx].set_facecolor(_STATUS_COLORS.get(status_cat, _STATUS_COLORS["ok"]))

            p_outside[tile_idx].set_facecolor(cmap_outside(norm_outside(min(sat, 100.0))))
            p_outside[tile_idx].set_hatch("///" if is_blank else "")
            p_outside[tile_idx].set_edgecolor("black" if is_blank else (0.35, 0.35, 0.35, 1.0))
            p_outside[tile_idx].set_linewidth(0.9 if is_blank else 0.4)

            jitter_clamped = max(jitter_dmin, min(jitter_dmax, jitter))
            p_jitter[tile_idx].set_facecolor(cmap_jitter(norm_jitter(jitter_clamped)))
            p_jitter[tile_idx].set_hatch("///" if is_jitter_outlier else "")
            p_jitter[tile_idx].set_edgecolor("black" if is_jitter_outlier else (0.35, 0.35, 0.35, 1.0))
            p_jitter[tile_idx].set_linewidth(0.9 if is_jitter_outlier else 0.4)

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
            try:
                fig.savefig(snapshot_path, dpi=150)
            except Exception:
                pass
            # Also flush per-section HTML + refresh the block index now,
            # so the section's dashboard exists on disk even if the
            # viewer is killed before STOP arrives (the original failure
            # mode where only the last STOP-d section had any HTML).
            _persist_html_now()
            if region_ov is not None:
                region_ov.render()
            last_save_done = done


def emit_completed_tile(task_queue: Any, tile_meta: Dict[str, Any]) -> None:
    task_queue.put(
        {
            "tile_idx": int(tile_meta["tile_idx"]),
            "x_um": float(tile_meta["x_um"]),
            "y_um": float(tile_meta["y_um"]),
            "tile_folder": str(tile_meta["tile_folder"]),
            "prefix": tile_meta.get("prefix"),
        }
    )
