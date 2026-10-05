"""Where a region's tile layout comes from, without any hardware state.

A live QC run got its layout (tile_idx + stage x/y + the commanded OL/stig
per tile) handed to it in memory from focus_map's TilePlan objects. We have
no such thing, so we recover it from disk. Two sources, in preference order:

  1. `<region>/LiveQC/live_qc_results.csv` - one row per tile. Even in the
     acquisition side's lightweight mode, `tile_idx, x_um, y_um, tile_folder,
     ol_pred, stig_x_pred, stig_y_pred` are REAL values (the commanded/planned
     ones); everything else in that file is a placeholder we must ignore.
     See STANDALONE_QC_HANDOFF.md section 3.
  2. `<region>/mfov<N>/metadata.txt` - the hardware readback per tile. Slower
     (one small file open per tile) but self-sufficient, and it's MEASURED
     rather than commanded. Used when the CSV is absent or unusable.

Deliberately NOT used: `tile_plan_propagate_tilt_1pt_<region>.csv`. focus_map
writes those relative to its own cwd, i.e. to the repo root on the
ACQUISITION computer's local disk (focus_map.py:6130) - they are not in the
output tree and this machine can't see them.
"""
from __future__ import annotations

import csv
import json
import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

_MFOV_DIR_RE = re.compile(r"^mfov(\d+)$", re.IGNORECASE)

# Same field names focus_diagnostics.py pulls out of metadata.txt. Duplicated
# rather than imported: focus_diagnostics is acquisition-side code that isn't
# on the shared-import list, and its parser is a private function.
_METADATA_KEYS = {
    "Stage pos. X": "x",
    "Stage pos. Y": "y",
    "Stage pos. Z": "z",
    "Lens": "ol",
    "Stigmator Tilt X": "stig_x",
    "Stigmator Tilt Y": "stig_y",
}
_METADATA_FIELD_RE = re.compile(r"^([^:]+):\s*(.+)$")
_LEADING_FLOAT_RE = re.compile(r"^\s*(-?[0-9.]+(?:[eE][-+]?[0-9]+)?)")


@dataclass
class TileRef:
    """One imaged MFOV: where it is, and what was commanded for it."""
    tile_idx: int
    x_um: float
    y_um: float
    tile_folder: Path
    ol_pred: Optional[float] = None
    stig_x_pred: Optional[float] = None
    stig_y_pred: Optional[float] = None

    @property
    def exists(self) -> bool:
        return self.tile_folder.is_dir()


def _to_float(raw: str) -> Optional[float]:
    m = _LEADING_FLOAT_RE.match(raw)
    if not m:
        return None
    try:
        val = float(m.group(1))
    except ValueError:
        return None
    return None if math.isnan(val) else val


def _opt_float(raw: Optional[str]) -> Optional[float]:
    """CSV cell -> float, treating '', 'nan' and unparseable as absent. The
    acquisition side writes literal NaN into placeholder columns, which would
    otherwise silently poison every downstream median."""
    if raw is None:
        return None
    s = str(raw).strip()
    if not s or s.lower() in ("nan", "none", "null"):
        return None
    try:
        val = float(s)
    except ValueError:
        return None
    return None if math.isnan(val) else val


def read_metadata(path: str | Path) -> Dict[str, float]:
    """Parse one tile's metadata.txt into {x, y, z, ol, stig_x, stig_y}.
    Returns whatever it found; missing keys are simply absent."""
    out: Dict[str, float] = {}
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                m = _METADATA_FIELD_RE.match(line.strip())
                if not m:
                    continue
                field = _METADATA_KEYS.get(m.group(1).strip())
                if field is None:
                    continue
                val = _to_float(m.group(2).strip())
                if val is not None:
                    out[field] = val
    except OSError:
        return {}
    return out


def read_region_timing(region_dir: str | Path) -> Dict[str, Any]:
    """`LiveQC/live_qc_timing.json` - the acquisition side's own timing for
    this region, or {} if absent.

    Read purely to pass through into our report. The interesting fields are
    `qc_catchup_worker_s` / `qc_catchup_viewer_s`: how long acquisition had to
    wait for its live-QC subprocesses to drain after imaging finished. Those
    used to hit their full 300 s timeout on real ~230-tile regions, and
    driving them to ~0 is precisely what moving QC to this machine was for -
    so a non-trivial value here is a regression on the acquisition side worth
    reporting back (see ../../ACQUISITION_REPLY.md section Q1). Confirmed
    written unconditionally, lightweight mode included.
    """
    path = Path(region_dir) / "LiveQC" / "live_qc_timing.json"
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def detect_live_qc_mode(region_dir: str | Path) -> str:
    """Was this region acquired with the acquisition side's live QC in
    "lightweight" mode or "full" mode? Returns "lightweight" / "full" /
    "unknown".

    Told apart by the `quality` column of live_qc_results.csv: lightweight
    mode writes a NaN placeholder for every tile, full mode writes real
    scores. This matters for interpreting `qc_catchup_s` - a pre-lightweight
    run legitimately spent minutes waiting for its own QC subprocesses (that
    is the baseline this whole split exists to remove), whereas the same
    number in a lightweight run would be a real regression.
    """
    csv_path = Path(region_dir) / "LiveQC" / "live_qc_results.csv"
    if not csv_path.is_file():
        return "unknown"
    try:
        with csv_path.open("r", newline="", encoding="utf-8-sig") as f:
            saw_row = False
            for row in csv.DictReader(f):
                saw_row = True
                if _opt_float(row.get("quality")) is not None:
                    return "full"
    except OSError:
        return "unknown"
    return "lightweight" if saw_row else "unknown"


def _resolve_tile_folder(region_dir: Path, tile_idx: int, csv_value: Optional[str]) -> Path:
    """Always prefer `<region_dir>/mfov<tile_idx>`.

    The CSV's own tile_folder column is an absolute path as seen by the
    ACQUISITION machine. Usually that's the same UNC/mapped share we're
    reading and would work verbatim - but it doesn't have to be (a run whose
    output_root was a local D:\\ path, a share mapped to a different drive
    letter here). Deriving the folder from region_dir keeps us correct on
    this machine regardless, and `mfov<tile_idx>` is a fixed convention from
    configure_output_for_tile(). The CSV value is only a fallback for a
    layout whose folder somehow isn't where we'd predict.
    """
    predicted = region_dir / f"mfov{int(tile_idx)}"
    if predicted.is_dir():
        return predicted
    if csv_value:
        candidate = Path(str(csv_value).strip())
        if candidate.is_dir():
            return candidate
        # Same-named leaf under our region_dir (share re-mapped to another
        # drive letter, most likely).
        relocated = region_dir / candidate.name
        if relocated.is_dir():
            return relocated
    return predicted


def from_live_qc_csv(region_dir: Path) -> List[TileRef]:
    """Layout from `LiveQC/live_qc_results.csv`. Reads ONLY the columns that
    stay real in lightweight mode - see this module's docstring."""
    csv_path = region_dir / "LiveQC" / "live_qc_results.csv"
    if not csv_path.is_file():
        return []

    tiles: Dict[int, TileRef] = {}
    try:
        with csv_path.open("r", newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                tile_idx = _opt_float(row.get("tile_idx"))
                x_um = _opt_float(row.get("x_um"))
                y_um = _opt_float(row.get("y_um"))
                if tile_idx is None or x_um is None or y_um is None:
                    continue
                idx = int(tile_idx)
                # A region re-imaged in place (or a retake) can append a
                # second row for the same tile. Last row wins: it's the most
                # recent acquisition of that tile.
                tiles[idx] = TileRef(
                    tile_idx=idx,
                    x_um=x_um,
                    y_um=y_um,
                    tile_folder=_resolve_tile_folder(region_dir, idx, row.get("tile_folder")),
                    ol_pred=_opt_float(row.get("ol_pred")),
                    stig_x_pred=_opt_float(row.get("stig_x_pred")),
                    stig_y_pred=_opt_float(row.get("stig_y_pred")),
                )
    except OSError:
        return []
    return [tiles[k] for k in sorted(tiles)]


def from_disk(region_dir: Path) -> List[TileRef]:
    """Layout by walking `mfov*/metadata.txt`. Self-sufficient fallback when
    there's no usable CSV. Values here are MEASURED readbacks, not the
    commanded plan - close enough for placement and reported as-is."""
    tiles: List[TileRef] = []
    try:
        entries = sorted(region_dir.iterdir())
    except OSError:
        return []

    for sub in entries:
        if not sub.is_dir():
            continue
        m = _MFOV_DIR_RE.match(sub.name)
        if not m:
            continue
        meta = read_metadata(sub / "metadata.txt")
        if "x" not in meta or "y" not in meta:
            continue
        tiles.append(
            TileRef(
                tile_idx=int(m.group(1)),
                x_um=meta["x"],
                y_um=meta["y"],
                tile_folder=sub,
                ol_pred=meta.get("ol"),
                stig_x_pred=meta.get("stig_x"),
                stig_y_pred=meta.get("stig_y"),
            )
        )
    tiles.sort(key=lambda t: t.tile_idx)
    return tiles


def read_region_layout(region_dir: str | Path) -> Tuple[List[TileRef], str]:
    """Best available layout for one region folder.

    Returns (tiles, source) where source is "live_qc_csv" or "metadata" or
    "none", so the report can state where the numbers came from - the two
    sources differ in meaning (commanded vs. measured) and that shouldn't be
    invisible to whoever reads the output.
    """
    region_dir = Path(region_dir)
    tiles = from_live_qc_csv(region_dir)
    if tiles:
        # A CSV that exists but describes tiles whose folders aren't on disk
        # is worse than no CSV (an aborted run, or a path we can't resolve).
        if sum(1 for t in tiles if t.exists) == 0:
            tiles = []
        else:
            return tiles, "live_qc_csv"

    tiles = from_disk(region_dir)
    if tiles:
        return tiles, "metadata"
    return [], "none"
