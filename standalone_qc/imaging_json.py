"""Finding the Zeiss imaging JSON that a batch was acquired from.

The dashboard's Retake selection panel (panel 5) builds a partial-retake
sub-region by CLONING the original region's XML out of this file and
replacing its contour with the hull of the lassoed mFoVs. Without the file
the panel cannot build anything: buildRetakeRegionXml throws "No original
region XML available". So the panel is dead in any dashboard written without
it, which is every standalone dashboard written before this module existed.

Nothing in a batch folder records which JSON it came from. The acquisition
side knows (it was handed the path), and its own LiveQC dashboards embed it,
but that path is a `V:\...` one belonging to the other machine and is not
written anywhere this side can read. So we find the file by CONTENT.

WHY CONTENT AND NOT THE NAME: on this wafer, batch
`w10h31_dye_R3_8NM_20260903_213951` was acquired from
`w10h31_dye_border.json` (24 of its 25 regions), NOT from the
identically-named-looking `w10h31_dye_R3.json`, which shares ZERO region
names with it. Matching on the name would have silently picked a file whose
regions are all different - and the failure would only surface later, as a
retake region built from the wrong geometry. Counting how many of the
batch's own region names a candidate actually contains cannot make that
mistake.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

_NAME_RE = re.compile(r'Name="([^"]+)"')

# How far above the batch folder to look. The wafer's JSONs sit beside the
# data root (…/w10h31_test_data/*.json) which is two levels above a run and
# three above a batch; one extra level of slack costs one directory listing.
_SEARCH_UP = 4


def region_names(path: str | Path) -> Set[str]:
    """Every region Name in an imaging JSON, or an empty set if it cannot be
    read as one. Unreadable is not an error here - we scan whatever .json
    files happen to be lying around, most of which are something else."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return set()
    if not isinstance(data, dict):
        return set()
    out: Set[str] = set()
    for s in data.get("Regions") or []:
        if isinstance(s, str):
            m = _NAME_RE.search(s)
            if m:
                out.add(m.group(1))
    return out


def region_xml(path: str | Path, name: str) -> Optional[str]:
    """The XML for one region, matched the way live_qc_support matches it."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None
    needle = f'Name="{name}"'
    for s in data.get("Regions") or []:
        if isinstance(s, str) and needle in s:
            return s
    return None


def _candidates(batch_dir: Path) -> List[Path]:
    """.json files in the batch folder and each parent, nearest first.

    Deliberately skips the two JSONs this pipeline writes into a batch
    itself - region_manifest.json and region_complete.json - which are not
    imaging JSONs and would only waste a read each.
    """
    seen: Set[Path] = set()
    out: List[Path] = []
    here = batch_dir
    for _ in range(_SEARCH_UP + 1):
        try:
            found = sorted(p for p in here.glob("*.json") if p.is_file())
        except OSError:
            found = []
        for p in found:
            if p.name in ("region_manifest.json", "region_complete.json"):
                continue
            rp = p.resolve()
            if rp not in seen:
                seen.add(rp)
                out.append(p)
        if here.parent == here:
            break
        here = here.parent
    return out


def find_for_batch(
    batch_dir: str | Path,
    wanted: Sequence[str],
    cfg: Optional[Dict[str, Any]] = None,
) -> Tuple[Optional[Path], Dict[str, Any]]:
    """Locate the imaging JSON a batch was acquired from.

    `wanted` is the batch's own region names (from its manifest). Returns
    (path_or_None, info) where info records what was considered, so a report
    can say why nothing was found rather than only that it wasn't.

    An explicit `imaging_json_path` in config wins outright and is not
    second-guessed; it is still checked for overlap so a wrong one is
    reported rather than silently producing retake regions with another
    wafer's geometry.
    """
    cfg = cfg or {}
    batch_dir = Path(batch_dir)
    want = {str(w) for w in wanted}
    info: Dict[str, Any] = {"searched": 0, "best_overlap": 0, "source": None}

    explicit = cfg.get("imaging_json_path")
    if explicit:
        p = Path(explicit)
        if not p.is_file():
            info["note"] = f"configured imaging_json_path does not exist: {p}"
            return None, info
        names = region_names(p)
        overlap = len(want & names) if want else len(names)
        info.update(source="configured", searched=1, best_overlap=overlap,
                    n_regions=len(names))
        if want and not overlap:
            info["note"] = (
                f"{p.name} contains {len(names)} region(s), none of which is "
                f"in this batch - check it is the right imaging JSON"
            )
        return p, info

    if not cfg.get("imaging_json_search_enabled", True):
        info["note"] = "automatic search disabled (imaging_json_search_enabled)"
        return None, info
    if not want:
        info["note"] = "no region names to match a candidate against"
        return None, info

    best: Optional[Path] = None
    best_n = 0
    cands = _candidates(batch_dir)
    info["searched"] = len(cands)
    # What each candidate actually held, kept so a failure can show the
    # mismatch instead of only asserting one. On the run that prompted this,
    # every JSON on the share held S*R3 regions while the batch was S*R5 -
    # one line of evidence that turns "not found" into "you are missing the
    # R5 file", but only if the names are carried out of here.
    seen: List[Dict[str, Any]] = []
    for p in cands:
        names = region_names(p)
        if names:
            seen.append({"file": p.name, "path": str(p),
                         "n_regions": len(names),
                         "sample": sorted(names)[:4]})
        n = len(want & names)
        # Strictly greater: _candidates is ordered nearest-first, so a tie
        # keeps the closer file, which is the one a retake JSON written into
        # the batch folder would be.
        if n > best_n:
            best, best_n = p, n
    info["best_overlap"] = best_n
    info["candidates"] = seen
    if best is None:
        detail = ""
        if seen:
            detail = "; the .json files found hold other regions (" + ", ".join(
                f"{c['file']}: {', '.join(c['sample'])}"
                + ("…" if c["n_regions"] > len(c["sample"]) else "")
                for c in seen[:3]
            ) + ")"
        info["note"] = (
            f"no .json in or above {batch_dir.name} lists any of this "
            f"batch's {len(want)} region(s) ({', '.join(sorted(want)[:3])}"
            + ("…" if len(want) > 3 else "")
            + f"); searched {len(cands)} file(s)" + detail
        )
        return None, info
    info["source"] = "auto"
    info["matched"] = f"{best_n}/{len(want)}"
    return best, info
