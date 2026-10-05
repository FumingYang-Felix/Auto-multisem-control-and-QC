"""Per-section facts that come from acquisition's own artefacts, not from
scoring images.

These fill the Zen Report's non-quality columns: whether each expected file
actually landed, how long the section took, how its focus support points
went, and whether autofocus/autostig succeeded. Sources, all read-only:

  <batch>/timing_summary.txt   per-section Focus/setup, Image acq, Total
  <section>/console.log        [Region], [Timing], [PlaneUpdate], [AF]
  <section>/mfov*/             file-presence counts

Everything here is best-effort and returns partial data rather than raising:
a section from an older run, or one still being written, must degrade to an
em dash in a report column, never take the report down.
"""
from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import fsp_timing

# --- console.log -----------------------------------------------------------
_RE_REGION_DONE = re.compile(r"^\[Region\]\s+DONE\s+region_name=(\S+)")
_RE_REGION_FAIL = re.compile(r"^\[Region\]\s+(FAILED|SKIPPED)\s+region_name=(\S+)")
# "[Timing] Region 'S6R3' breakdown (231 tile(s), 1107.8s total):"
_RE_TIMING_REGION = re.compile(
    r"^\[Timing\]\s+Region\s+'([^']+)'\s+breakdown\s+\((\d+)\s+tile\(s\),\s*"
    r"([\d.]+)s\s+total\)"
)
# "[PlaneUpdate] <METHOD>: measuring one point for 'S6R3' at xy=(...)"
_RE_PLANE_MEASURING = re.compile(r"^\[PlaneUpdate\]\s+(\w+):.*\bmeasur(?:ing|e)\b")
# "[PlaneUpdate] <METHOD>: measured OL=2655.115 uA, StigX=0.070209, StigY=0.270078"
_RE_PLANE_MEASURED = re.compile(
    r"^\[PlaneUpdate\].*\bmeasured\b.*?OL=([-\d.]+)"
    r"(?:.*?StigX=([-\d.]+))?(?:.*?StigY=([-\d.]+))?"
)
_RE_PLANE_METHOD = re.compile(r"^\[PlaneUpdate\]\s+([A-Z_]+):")

# --- timing_summary.txt ----------------------------------------------------
# "S6R3                            26.9         1055.1      1107.8      20.1%"
_RE_TIMING_ROW = re.compile(
    r"^(\S+)\s+([\d.]+)\s+([\d.]+)\s+([\d.]+)\s+([\d.]+)%\s*$"
)
_RE_AGG = {
    "focus_stig_s": re.compile(r"focus/stigmation\s*:\s*([\d.]+)s"),
    "af_s": re.compile(r"-\s*focus \(AF\+minimize-OL\):\s*([\d.]+)s"),
    "as_s": re.compile(r"-\s*stig \(AS\)\s*:\s*([\d.]+)s"),
    "b2f_s": re.compile(r"-\s*stage/B2F within focus:\s*([\d.]+)s"),
    "move_s": re.compile(r"stage move\+settle\s*:\s*([\d.]+)s"),
    "scan_s": re.compile(r"beam scan\s*:\s*([\d.]+)s"),
    "storage_s": re.compile(r"storage overhead\s*:\s*([\d.]+)s"),
    "qc_catchup_s": re.compile(r"QC catch-up\s*:\s*([\d.]+)s"),
    "grand_total_s": re.compile(r"grand total\s*=\s*([\d.]+)s"),
}


@dataclass
class FileChecks:
    """Did every expected artefact actually land, per the Zen Report's
    file-presence checkmarks. Counts are over mFoV folders."""
    n_mfovs: int = 0
    modal_beams: int = 0
    full_res_ok: int = 0        # mFoVs with the modal number of .bmp
    thumb_ok: int = 0           # ... of .png
    coords_ok: int = 0          # image_coordinates.txt
    thumb_coords_ok: int = 0    # thumbnail_coordinates.txt
    metadata_ok: int = 0        # metadata.txt
    focus_map: bool = False     # <section>/FocusMap.txt

    def as_dict(self) -> Dict[str, Any]:
        return {
            "n_mfovs": self.n_mfovs,
            "modal_beams": self.modal_beams,
            "full_res_ok": self.full_res_ok,
            "thumb_ok": self.thumb_ok,
            "coords_ok": self.coords_ok,
            "thumb_coords_ok": self.thumb_coords_ok,
            "metadata_ok": self.metadata_ok,
            "focus_map": self.focus_map,
        }


@dataclass
class SectionFacts:
    duration_s: Optional[float] = None
    focus_setup_s: Optional[float] = None
    image_acq_s: Optional[float] = None
    n_tiles_logged: Optional[int] = None
    plane_method: Optional[str] = None
    fsp_attempted: int = 0
    fsp_succeeded: int = 0
    af_ok: Optional[bool] = None
    as_ok: Optional[bool] = None
    region_done: Optional[bool] = None
    files: FileChecks = field(default_factory=FileChecks)
    # Per-focus-support-point move / B2F / AF / AS timings. See fsp_timing.py;
    # older log formats time only AS, and report the rest as None.
    fsp_timings: List[Dict[str, Any]] = field(default_factory=list)
    # Multi-point focus maps: where each support point was measured, in the
    # same stage frame as the mFoV layout, so they can be located on the
    # stitched overview. {"method", "n_declared", "points", "plane"}.
    support_points: Dict[str, Any] = field(default_factory=dict)
    # The "focus/stigmation: 17.5s"-style section totals from console.log,
    # kept alongside the per-FSP rows so a chart can say what fraction of the
    # step it actually accounts for.
    aggregate: Dict[str, Any] = field(default_factory=dict)
    # <section>/LiveQC/live_qc_timing.json. Overlaps the aggregate on
    # focus/move/scan/storage (identical to one decimal across a 14-section
    # batch) and adds wall_s, n_tiles, the stage-Z glitch count and the QC
    # catch-up split. See fsp_timing.read_live_qc_timing.
    live_qc_timing: Dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "duration_s": self.duration_s,
            "focus_setup_s": self.focus_setup_s,
            "image_acq_s": self.image_acq_s,
            "n_tiles_logged": self.n_tiles_logged,
            "plane_method": self.plane_method,
            "fsp_attempted": self.fsp_attempted,
            "fsp_succeeded": self.fsp_succeeded,
            "af_ok": self.af_ok,
            "as_ok": self.as_ok,
            "region_done": self.region_done,
            "files": self.files.as_dict(),
            "fsp_timings": list(self.fsp_timings),
            "support_points": dict(self.support_points),
            "aggregate": dict(self.aggregate),
            "live_qc_timing": dict(self.live_qc_timing),
        }


def check_files(
    section_dir: str | Path,
    image_globs: tuple = ("*.bmp", "*.BMP"),
    thumb_globs: tuple = ("*.png", "*.PNG"),
) -> FileChecks:
    """Directory listings only - no image reads.

    Deliberately run at scoring time (when the folders are being walked
    anyway) and cached into the summary JSON, rather than on every report
    rebuild: a 29-section batch at 231 mFoVs each is ~6,700 listings, which
    would turn an instant Refresh into a half-minute stall on a share.
    """
    section_dir = Path(section_dir)
    out = FileChecks()
    out.focus_map = (section_dir / "FocusMap.txt").is_file()
    try:
        subs = [p for p in sorted(section_dir.iterdir())
                if p.is_dir() and p.name.lower().startswith("mfov")]
    except OSError:
        return out

    counts: List[int] = []
    per_mfov: List[Dict[str, Any]] = []
    for sub in subs:
        # Dedupe across globs - Path.glob is case-insensitive on Windows, so
        # "*.bmp" and "*.BMP" would otherwise each match every file.
        bmps = {p.name for g in image_globs for p in sub.glob(g)}
        pngs = {p.name for g in thumb_globs for p in sub.glob(g)}
        # overview_mfov.png is ours/live-QC's, not a beam thumbnail.
        pngs.discard("overview_mfov.png")
        counts.append(len(bmps))
        per_mfov.append({
            "bmp": len(bmps),
            "png": len(pngs),
            "coords": (sub / "image_coordinates.txt").is_file(),
            "tcoords": (sub / "thumbnail_coordinates.txt").is_file(),
            "meta": (sub / "metadata.txt").is_file(),
        })

    out.n_mfovs = len(subs)
    if not counts:
        return out
    # The section's own notion of "a full mFoV", rather than assuming 91 - a
    # run may legitimately use a different beam set, and we want deviation
    # from THIS section's norm.
    out.modal_beams = max(set(counts), key=counts.count)
    for m in per_mfov:
        if m["bmp"] >= out.modal_beams:
            out.full_res_ok += 1
        if m["png"] >= out.modal_beams:
            out.thumb_ok += 1
        out.coords_ok += bool(m["coords"])
        out.thumb_coords_ok += bool(m["tcoords"])
        out.metadata_ok += bool(m["meta"])
    return out


def parse_console_log(path: str | Path) -> Dict[str, Any]:
    """Whatever this section's console.log records about its focus phase."""
    path = Path(path)
    out: Dict[str, Any] = {
        "plane_method": None, "fsp_attempted": 0, "fsp_succeeded": 0,
        "af_ok": None, "as_ok": None, "region_done": None,
        "duration_s": None, "n_tiles_logged": None,
        # The "[Timing] Region '...' breakdown" block's own lines. The same
        # keys parse_timing_summary produces from timing_summary.txt, but
        # scoped to THIS section and available even before the batch-level
        # file exists (it is written once at the end of the batch).
        "aggregate": {},
    }
    if not path.is_file():
        return out
    try:
        with path.open("r", encoding="utf-8", errors="replace") as f:
            for raw in f:
                line = raw.strip()
                if not line.startswith("["):
                    # The breakdown's own rows are indented continuations
                    # ("  focus/stigmation  :  17.5s ( 19.1%)"), so they are
                    # matched before the "[" gate rejects them.
                    for key, rx in _RE_AGG.items():
                        m = rx.search(line)
                        if m:
                            out["aggregate"][key] = float(m.group(1))
                    continue

                m = _RE_TIMING_REGION.match(line)
                if m:
                    out["n_tiles_logged"] = int(m.group(2))
                    out["duration_s"] = float(m.group(3))
                    continue
                if _RE_REGION_DONE.match(line):
                    out["region_done"] = True
                    continue
                if _RE_REGION_FAIL.match(line):
                    out["region_done"] = False
                    continue

                m = _RE_PLANE_METHOD.match(line)
                if m and out["plane_method"] is None:
                    out["plane_method"] = m.group(1)

                m = _RE_PLANE_MEASURED.match(line)
                if m:
                    out["fsp_succeeded"] += 1
                    # An OL reading means autofocus produced a value; a StigX
                    # reading on the same line means autostig did too.
                    out["af_ok"] = True
                    if m.group(2) is not None:
                        out["as_ok"] = True
                    continue
                if _RE_PLANE_MEASURING.match(line):
                    out["fsp_attempted"] += 1
    except OSError:
        return out

    # A method that measured nothing still attempted whatever it measured.
    if out["fsp_succeeded"] > out["fsp_attempted"]:
        out["fsp_attempted"] = out["fsp_succeeded"]
    if out["fsp_attempted"] and not out["fsp_succeeded"]:
        out["af_ok"] = False
    return out


def parse_timing_summary(batch_dir: str | Path) -> Dict[str, Any]:
    """`<batch>/timing_summary.txt` -> per-section rows + the aggregate
    breakdown (AF / AS / B2F / move / scan / storage / QC catch-up).

    This is where the acquisition side's own AF-vs-AS split lives. A run
    predating that instrumentation reports 0.0 for both, which is why the
    Stig column can legitimately read zero.
    """
    path = Path(batch_dir) / "timing_summary.txt"
    out: Dict[str, Any] = {"per_section": {}, "aggregate": {}}
    if not path.is_file():
        return out
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return out

    for line in text.splitlines():
        m = _RE_TIMING_ROW.match(line.strip())
        if m:
            out["per_section"][m.group(1)] = {
                "focus_setup_s": float(m.group(2)),
                "image_acq_s": float(m.group(3)),
                "total_s": float(m.group(4)),
                "pct_of_run": float(m.group(5)),
            }
    for key, rx in _RE_AGG.items():
        m = rx.search(text)
        if m:
            out["aggregate"][key] = float(m.group(1))
    return out


CACHE_NAME = "section_facts.json"

# Schema version of the cached facts. BUMP whenever a field is added, or
# every section that was already scanned keeps serving a cache that predates
# it - the file is valid JSON and parses fine, it just silently lacks the new
# key, so the new column or chart reads as "no data" forever. (The ledger had
# the same gap; see config.ANALYSIS_VERSION.)
#
#   1 - duration / plane method / FSP counts / file checks
#   2 - adds per-FSP move+B2F+AF+AS timings and the console.log aggregate
#   3 - adds multi-point focus-map support points (xy + measured/predicted
#       OL + residual), so they can be drawn on the overview
#   5 - adds LiveQC's live_qc_timing.json (wall_s, n_tiles, z-glitch count,
#       QC catch-up split), so the region total can be broken down with an
#       explicit unattributed remainder instead of four bars that look like
#       they should add up
#   4 - the untagged "[Offset FocusPoint N] / AF: ... (6.18s)" log shape is
#       parsed. Sections cached at 3 have an EMPTY fsp_timings for that shape
#       - the steps were timed, the parser just did not see them - so the
#       cache has to be rebuilt rather than trusted.
CACHE_VERSION = 5


def cache_path(section_dir: str | Path, subdir: str = "StandaloneQC") -> Path:
    return Path(section_dir) / subdir / CACHE_NAME


def load_cached(
    section_dir: str | Path, subdir: str = "StandaloneQC"
) -> Optional[Dict[str, Any]]:
    p = cache_path(section_dir, subdir)
    if not p.is_file():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    if int(data.get("version") or 1) != CACHE_VERSION:
        return None                      # stale schema - re-collect
    facts = data.get("facts")
    return facts if isinstance(facts, dict) else None


def save_cached(
    section_dir: str | Path, facts: Dict[str, Any], subdir: str = "StandaloneQC"
) -> None:
    """Atomic, same tmp+replace discipline as everything else that writes
    into a tree the acquisition machine may be reading."""
    p = cache_path(section_dir, subdir)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(p.suffix + ".tmp")
        tmp.write_text(
            json.dumps(
                {"version": CACHE_VERSION,
                 "collected_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                 "facts": facts},
                indent=2,
            ),
            encoding="utf-8",
        )
        os.replace(tmp, p)
    except OSError as exc:
        print(f"[SectionFacts] WARNING: could not cache {p}: {exc}")


def load_or_collect(
    section_dir: str | Path,
    section_name: str,
    timing_summary: Optional[Dict[str, Any]] = None,
    subdir: str = "StandaloneQC",
    force: bool = False,
) -> Dict[str, Any]:
    """Facts for one section, from cache when available.

    Gathering costs ~2.3s per section on a share (mostly directory listings
    over ~230 mFoV folders), which is fine once but far too slow to repeat on
    every report rebuild - a 29-section batch would stall for a minute. Once
    a section has finished imaging its files do not change, so the cache is
    valid indefinitely; `force` re-reads anyway.
    """
    if not force:
        cached = load_cached(section_dir, subdir)
        if cached is not None:
            return cached
    facts = collect(section_dir, section_name, timing_summary).as_dict()
    save_cached(section_dir, facts, subdir)
    return facts


def collect(
    section_dir: str | Path,
    section_name: str,
    timing_summary: Optional[Dict[str, Any]] = None,
) -> SectionFacts:
    """Everything above for one section. `timing_summary` is the batch-level
    parse, passed in so a whole batch parses that file once, not per section."""
    section_dir = Path(section_dir)
    facts = SectionFacts()

    log = parse_console_log(section_dir / "console.log")
    facts.plane_method = log["plane_method"]
    facts.fsp_attempted = int(log["fsp_attempted"])
    facts.fsp_succeeded = int(log["fsp_succeeded"])
    facts.af_ok = log["af_ok"]
    facts.as_ok = log["as_ok"]
    facts.region_done = log["region_done"]
    facts.duration_s = log["duration_s"]
    facts.n_tiles_logged = log["n_tiles_logged"]

    # timing_summary.txt is the more authoritative duration: it is written
    # once at the end of the batch, whereas console.log's own line is
    # produced mid-run.
    row = (timing_summary or {}).get("per_section", {}).get(section_name)
    if row:
        facts.focus_setup_s = row.get("focus_setup_s")
        facts.image_acq_s = row.get("image_acq_s")
        facts.duration_s = row.get("total_s", facts.duration_s)

    facts.aggregate = dict(log.get("aggregate") or {})
    facts.live_qc_timing = fsp_timing.read_live_qc_timing(section_dir)
    facts.fsp_timings = fsp_timing.parse_console_log(section_dir / "console.log")
    facts.support_points = fsp_timing.parse_support_points(
        section_dir / "console.log")
    # A multi-point focus map names its method on a line the single-point
    # regex cannot see ("Measuring 7 support points using method=..."), so
    # without this those sections showed no focus strategy at all.
    if not facts.plane_method and facts.support_points.get("method"):
        facts.plane_method = str(facts.support_points["method"])
    facts.files = check_files(section_dir)
    return facts
