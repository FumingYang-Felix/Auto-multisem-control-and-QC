"""Collect FocusMap.txt files from a completed acquisition run into a flat
folder, renamed by section number, for use as a prev_focusmap directory
(see focus_map.py's resolve_prev_focusmap_path folder-mode matching).

Each region folder under the run root looks like "016_S18R1" or, for a
retaken section, "017_S15R1 (2)" - the leading "NNN_" is acquisition order,
"S<section>R<repeat>" is the section/region, and a "(k)" suffix marks a
re-run. This pulls the section number out of that folder name and copies
(never moves - originals are left untouched) each FocusMap.txt to:

    <dest>/S<section>_FocusMap.txt

"S<N>_FocusMap.txt" is deliberately the naming convention
focus_map.resolve_prev_focusmap_path() already looks for in a
prev_focusmap_dir, so the output folder can be pointed at directly - this is
why HiMcPage's "Raw focus-map dir" field runs this first and fills itself in
with the result, rather than requiring collect_focus_maps.py to be run by
hand from a terminal first.

If more than one FocusMap.txt maps to the same section (e.g. a genuine
re-run), the LATEST acquisition (highest "NNN_" order prefix - i.e. the
second/retake run) wins the plain "S<N>_FocusMap.txt" name, on the working
assumption that a retake supersedes the run before it. Every earlier one for
that section is still kept, just under the source folder's "NNN_" order
prefix appended instead, with a warning returned so it can be surfaced to
the operator.
"""
from __future__ import annotations

import glob
import os
import re
import shutil
from dataclasses import dataclass, field
from typing import List, Tuple

SECTION_RE = re.compile(r"[Ss](\d+)R\d+")


@dataclass
class CollectResult:
    copied: List[Tuple[str, str, str]] = field(default_factory=list)  # (src, dest, note)
    skipped: List[Tuple[str, str]] = field(default_factory=list)  # (src, reason)
    warnings: List[str] = field(default_factory=list)
    dest_dir: str = ""

    @property
    def found_count(self) -> int:
        return len(self.copied) + len(self.skipped)


def find_focus_maps(source_root: str):
    """Yield (full_path, parent_folder_name) for every FocusMap.txt under
    source_root, walked recursively."""
    for dirpath, _dirnames, filenames in os.walk(source_root):
        for name in filenames:
            if name.lower() == "focusmap.txt":
                yield os.path.join(dirpath, name), os.path.basename(dirpath)


def collect_focus_maps(source_root: str, dest_dir: str) -> CollectResult:
    """Do the actual collection (see module docstring). Raises FileNotFoundError
    if source_root doesn't exist, or RuntimeError if no FocusMap.txt files are
    found under it - callers should catch and log these rather than letting
    them propagate raw into a GUI."""
    if not os.path.isdir(source_root):
        raise FileNotFoundError(f"Source folder not found: {source_root}")

    os.makedirs(dest_dir, exist_ok=True)

    found = list(find_focus_maps(source_root))
    if not found:
        raise RuntimeError(f"No FocusMap.txt files found under {source_root}")

    result = CollectResult(dest_dir=dest_dir)

    # Group by section number, in source (acquisition-order) order, so that
    # for a repeated section the LAST entry is the latest/retake run.
    by_section = {}
    for src_path, folder_name in sorted(found):
        m = SECTION_RE.search(folder_name)
        if not m:
            result.skipped.append((src_path, "couldn't find S<N>R<N> in folder name"))
            continue
        by_section.setdefault(int(m.group(1)), []).append((src_path, folder_name))

    # Clean previously-generated output so a re-run doesn't leave stale
    # duplicate-suffixed files around from an older collision resolution.
    for stale in glob.glob(os.path.join(dest_dir, "S*_FocusMap*.txt")):
        os.remove(stale)

    for section, entries in sorted(by_section.items()):
        # Latest (highest "NNN_" order prefix) run is canonical - a retake
        # supersedes the run before it.
        *earlier, (canonical_src, canonical_folder) = entries

        dest_path = os.path.join(dest_dir, f"S{section}_FocusMap.txt")
        shutil.copy2(canonical_src, dest_path)
        result.copied.append((canonical_src, dest_path, "canonical"))

        for src_path, folder_name in earlier:
            order_prefix = folder_name.split("_", 1)[0]
            dest_name = f"S{section}_FocusMap_{order_prefix}.txt"
            other_path = os.path.join(dest_dir, dest_name)
            shutil.copy2(src_path, other_path)
            result.copied.append((src_path, other_path, f"superseded by {canonical_folder}"))
            result.warnings.append(
                f"section {section} has {len(entries)} runs - using {canonical_folder!r} "
                f"as canonical, {folder_name!r} kept as {dest_name!r}")

    return result
