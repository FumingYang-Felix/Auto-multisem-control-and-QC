"""Finding regions that are finished imaging and safe to score.

Two signals, and we accept either - preferring the per-region one:

  1. `<region_folder>/region_complete.json` (preferred). A dedicated
     per-region "done" marker the acquisition side added on 2026-09-07:
     {"name", "status", "detail", "completed_at"}. Written atomically, right
     where the region's manifest status is finalized - so immediately BEFORE
     the manifest write, making it very slightly earlier as well as more
     specific. Preferred because it isn't rewritten every time some OTHER
     region in the batch changes state, so a watcher doesn't have to re-read
     and diff a shared file to notice one region finishing.
  2. `<batch_folder>/region_manifest.json` - a region's entry flipping to
     `"status": "done"`. Still required for ENUMERATION (it is the only
     record of what a batch intends to image, including regions that failed
     before their folder was ever created), and it is the only signal
     available for any region imaged before signal 1 existed - which is all
     historical data on the share.

Signal 2 is the one whose safety was established first, and it holds for
both (verified in focus_map.py; confirmed against the acquisition clone in
../../ACQUISITION_REPLY.md, including that neither is gated on
lightweight_mode):

  - It is written ATOMICALLY (tmp + os.replace, focus_map.py:6315-6334),
    explicitly so a concurrent reader never sees a half-written file. That is
    exactly what a watcher on another machine over SMB needs.
  - The flip to "done" (focus_map.py:6646) happens only after
    run_one_region() returns - i.e. after acquisition's final
    CMD_WAIT_ACQUISITION_COMPLETE and after both live-QC subprocesses have
    been joined. So when "done" is visible, ZEN has flushed every BMP.
  - It is always written, including for single-region runs, before hardware
    is touched (focus_map.py:6578), and it distinguishes failed / skipped /
    retake so we never wait forever on a region that died.

We still verify the region's tile folders look complete before scoring
(`verify_region_files`), purely to absorb SMB metadata lag. That's
belt-and-braces, not the primary mechanism.
"""
# NOTE ON CLONE SKEW: this machine's own focus_map.py does not write
# region_complete.json - the acquisition clone added it and the two clones
# have no shared remote yet. That is fine and is exactly why both signals
# are supported: we are the CONSUMER, so what matters is which files are
# present on the share, not what our local copy of the producer would write.
# Regions imaged before 2026-09-07 have only the manifest.
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

# Statuses that mean "acquisition is finished with this region". Only "done"
# is worth scoring; the rest are terminal but have nothing (or only partial
# data) to score, and are surfaced so `--list` can explain the absence.
STATUS_SCORABLE = ("done",)
STATUS_TERMINAL = ("done", "failed", "skipped")


REGION_COMPLETE_NAME = "region_complete.json"


@dataclass
class RegionRef:
    block_dir: Path
    name: str
    folder: str
    status: str
    detail: Optional[str] = None
    # Which signal decided `status`: "region_complete" (the per-region marker)
    # or "manifest". Surfaced in --list so an operator can tell at a glance
    # whether a run predates the per-region trigger file.
    status_source: str = "manifest"
    completed_at: Optional[str] = None

    @property
    def region_dir(self) -> Path:
        return self.block_dir / self.folder

    @property
    def block_label(self) -> str:
        """Internal name for the Batch folder, kept because RegionScore and
        the summary JSON already use it. `batch_label` is the operator-facing
        spelling of the same thing."""
        return self.block_dir.name

    @property
    def batch_label(self) -> str:
        """The specific image-acquisition folder for this wafer."""
        return self.block_dir.name

    @property
    def run_label(self) -> str:
        """The run the batch belongs to - the folder holding batch folders
        and the wafer-level QC report (e.g. "Try_1")."""
        return self.block_dir.parent.name

    @property
    def sample_id(self) -> Optional[str]:
        """The wafer this batch imaged, e.g. "w10h31"."""
        return parse_sample_id(self.block_dir.name)

    @property
    def key(self) -> str:
        """Ledger / CLI identity. A region name alone isn't unique across a
        wafer - the same region gets re-imaged in later batches."""
        return f"{self.block_dir.name}/{self.folder}"

    @property
    def is_scorable(self) -> bool:
        return self.status in STATUS_SCORABLE and self.region_dir.is_dir()


@dataclass
class RegionCheck:
    """Result of verify_region_files()."""
    n_tile_dirs: int = 0
    n_with_coords: int = 0
    n_incomplete: int = 0
    beam_counts: Dict[int, int] = field(default_factory=dict)
    modal_beam_count: int = 0
    warnings: List[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.n_tile_dirs > 0 and self.n_incomplete == 0


def read_region_manifest(block_dir: str | Path) -> Optional[List[Dict[str, Any]]]:
    """`<block_dir>/region_manifest.json` -> its "regions" list, or None.

    Same contract as live_qc_support._read_region_manifest, reimplemented
    here rather than imported because that one is private and lives in a
    shared file we've agreed not to depend on beyond its public API.
    """
    path = Path(block_dir) / "region_manifest.json"
    if not path.is_file():
        return None
    try:
        # utf-8-sig tolerates a BOM and reads BOM-less files identically.
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return None
    regions = data.get("regions") if isinstance(data, dict) else None
    return regions if isinstance(regions, list) else None


def read_region_complete(region_dir: str | Path) -> Optional[Dict[str, Any]]:
    """`<region_dir>/region_complete.json` -> its dict, or None if absent.

    Written atomically by the acquisition side once a region reaches a
    terminal state. Absent for any region imaged before 2026-09-07, which is
    why every caller falls back to the batch manifest.
    """
    path = Path(region_dir) / REGION_COMPLETE_NAME
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        # A trigger file we can't parse must not mask the manifest's own
        # (still valid) answer - fall back rather than treat it as absent-
        # but-broken.
        return None
    return data if isinstance(data, dict) else None


# The wafer/holder identity carried in a batch folder's name. Both spellings
# occur: "w10h31_dye_roi_R2_focus_8NM_<ts>" and, per the README's own example
# of build_block_name's output, "w13_h25_Slice1_8NM_<ts>".
_SAMPLE_RE = re.compile(r"w\d+_?h\d+", re.IGNORECASE)


def parse_sample_id(batch_name: str) -> Optional[str]:
    """The Sample (wafer) this batch imaged, e.g. "w10h31" out of
    "w10h31_dye_roi_R2_focus_8NM_20260909_125113". None if the name carries
    no recognisable wafer/holder identity."""
    m = _SAMPLE_RE.search(str(batch_name))
    return m.group(0) if m else None


def resolve_image_dir(path: str | Path) -> Tuple[Path, Optional[str]]:
    """Accept an Image Directory (a Batch folder) or the Run folder above it.

    Returns (run_root, batch_name) - batch_name is None when the path was the
    Run folder, i.e. every batch under it is in scope.

    The two are indistinguishable by name, and the Batch level is what an
    operator naturally points at: a Batch folder holds section folders plus
    its own region_manifest.json, while the Run folder holds Batch folders
    plus the wafer-level w<N>_global_qc_report.html.

    Why this returns the RUN folder rather than the batch: the ledger and the
    wafer-level review files - retake_regions.txt, region_review_status.json,
    w<N>_global_qc_report.html - belong at the Run level, where the
    acquisition pipeline already puts them. Scoping to one batch is done with
    the returned batch_name instead, so pointing at a Batch folder does not
    strand a second ledger inside it.
    """
    p = Path(path)
    if (p / "region_manifest.json").is_file():
        return p.parent, p.name
    return p, None


def find_block_dirs(output_root: str | Path) -> List[Path]:
    """Batch folders directly under the wafer output root: any subdirectory
    carrying a region_manifest.json. Checking for the manifest rather than
    matching a name pattern means a wafer root holding output from some other
    tool (a native ZEN acquisition, an older focus-map-only pass) is skipped
    rather than misread as a batch."""
    root = Path(output_root)
    if not root.is_dir():
        return []
    blocks = [
        child for child in sorted(root.iterdir())
        if child.is_dir() and (child / "region_manifest.json").is_file()
    ]
    return blocks


def _matches(needle: str, name: str, folder: str, exact: bool) -> bool:
    """Filter one region by name or folder.

    exact=True is what the GUI uses: it selects concrete rows, and substring
    matching would silently widen that - picking "S2R1" would also match
    "S2R10", scoring a section the operator did not choose. Substring stays
    the CLI default, where "--section S2" meaning "all of S2*" is useful.
    """
    if exact:
        return needle == name or needle == folder
    return needle in name or needle in folder


def list_regions(
    output_root: str | Path,
    block_filter: Optional[str] = None,
    exact: bool = False,
) -> List[RegionRef]:
    """Every region of every batch under output_root, with its acquisition
    status. Newest batch last (directory names are timestamped, so the sort
    in find_block_dirs is chronological in practice)."""
    regions: List[RegionRef] = []
    for block_dir in find_block_dirs(output_root):
        if block_filter:
            if exact:
                if block_dir.name != block_filter:
                    continue
            elif block_filter not in block_dir.name:
                continue
        manifest = read_region_manifest(block_dir)
        if manifest is None:
            continue

        seen_folders = set()
        for entry in manifest:
            name = str(entry.get("name", "?"))
            folder = str(entry.get("folder") or name)
            seen_folders.add(folder)
            status = str(entry.get("status", "pending"))
            detail = entry.get("detail")
            source = "manifest"
            completed_at = None

            # The per-region marker wins where it exists: it is written
            # immediately before the manifest and describes only this region,
            # so it cannot be stale relative to the shared file.
            trigger = read_region_complete(block_dir / folder)
            if trigger is not None:
                status = str(trigger.get("status", status))
                detail = trigger.get("detail", detail)
                completed_at = trigger.get("completed_at")
                source = "region_complete"

            regions.append(
                RegionRef(
                    block_dir=block_dir, name=name, folder=folder,
                    status=status, detail=detail,
                    status_source=source, completed_at=completed_at,
                )
            )

        # A region folder carrying its own trigger file but missing from the
        # manifest: possible if the manifest's last write was lost (network
        # blip) or the batch was interrupted mid-rewrite. region_complete.json
        # is our own convention, never written by ZEN, so a folder that has
        # one is unambiguously a region of this batch and shouldn't be
        # invisible just because the shared file disagrees.
        try:
            subs = sorted(p for p in block_dir.iterdir() if p.is_dir())
        except OSError:
            subs = []
        for sub in subs:
            if sub.name in seen_folders:
                continue
            trigger = read_region_complete(sub)
            if trigger is None:
                continue
            regions.append(
                RegionRef(
                    block_dir=block_dir,
                    name=str(trigger.get("name") or sub.name),
                    folder=sub.name,
                    status=str(trigger.get("status", "done")),
                    detail=trigger.get("detail"),
                    status_source="region_complete",
                    completed_at=trigger.get("completed_at"),
                )
            )
    return regions


def find_scorable_regions(
    output_root: str | Path,
    block_filter: Optional[str] = None,
    region_filter: Optional[str] = None,
    exact: bool = False,
) -> List[RegionRef]:
    """Regions whose imaging is finished and whose folder is present."""
    out = [r for r in list_regions(output_root, block_filter, exact) if r.is_scorable]
    if region_filter:
        out = [r for r in out if _matches(region_filter, r.name, r.folder, exact)]
    return out


def verify_region_files(
    region_dir: str | Path,
    expected_beams: Optional[int] = None,
    image_globs: Sequence[str] = ("*.bmp", "*.BMP"),
) -> RegionCheck:
    """Cheap directory-listing sanity check over a region's tile folders.

    Guards against SMB metadata lag and against a region folder that is
    "done" per the manifest but whose files a mid-write share hasn't fully
    surfaced yet. Deliberately listing-only - no image reads - so it costs
    one round trip per tile folder and can be run before committing to a
    multi-GB scoring pass.

    "Complete" means: has an image_coordinates.txt (written once per tile
    AFTER all its beams are captured, per handoff section 4) and has the same
    beam count as the majority of the region's other tiles.
    """
    region_dir = Path(region_dir)
    check = RegionCheck()
    try:
        subs = sorted(p for p in region_dir.iterdir() if p.is_dir())
    except OSError as exc:
        check.warnings.append(f"cannot list {region_dir}: {exc}")
        return check

    for sub in subs:
        if not sub.name.lower().startswith("mfov"):
            continue
        check.n_tile_dirs += 1
        # Dedupe by name across the glob patterns. On Windows, Path.glob is
        # case-INSENSITIVE, so "*.bmp" and "*.BMP" each match every file and
        # a naive sum double-counts every beam - which would put the modal
        # beam count at 182 instead of 91 and make the completeness check
        # meaningless. Same reason live_qc_support._iter_image_files runs its
        # patterns through a set.
        names: set = set()
        for pat in image_globs:
            names.update(p.name for p in sub.glob(pat))
        n_beams = len(names)
        check.beam_counts[n_beams] = check.beam_counts.get(n_beams, 0) + 1
        if (sub / "image_coordinates.txt").is_file():
            check.n_with_coords += 1

    if check.n_tile_dirs == 0:
        check.warnings.append(f"no mfov* subfolders under {region_dir}")
        return check

    # Modal beam count is the region's own notion of "a full tile" - more
    # robust than assuming 91, since a run can legitimately use a different
    # beam set and we'd rather flag deviation from THIS region's norm.
    check.modal_beam_count = max(check.beam_counts.items(), key=lambda kv: kv[1])[0]
    if expected_beams and check.modal_beam_count != expected_beams:
        check.warnings.append(
            f"modal beam count is {check.modal_beam_count}, expected {expected_beams}"
        )

    short = sum(n for beams, n in check.beam_counts.items() if beams < check.modal_beam_count)
    check.n_incomplete = short
    if short:
        check.warnings.append(
            f"{short}/{check.n_tile_dirs} tile folder(s) have fewer than "
            f"{check.modal_beam_count} beam images"
        )
    missing_coords = check.n_tile_dirs - check.n_with_coords
    if missing_coords:
        check.warnings.append(
            f"{missing_coords}/{check.n_tile_dirs} tile folder(s) have no "
            f"image_coordinates.txt (tile may still be mid-write)"
        )
    return check
