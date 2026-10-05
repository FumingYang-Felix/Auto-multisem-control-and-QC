"""
Compare a prior FocusMap.txt (the file that seeded a region's focus plane)
against the "*_HiMC_FocusMap.txt" this pipeline writes out after actually
measuring that region (see save_measured_focusmap_txt in focus_map.py) -
line up their support points by sp_id and report where they disagree.

Both files use Zeiss's own 10-column FocusMap.txt layout (see the column
notes in focus_map.load_prev_focusmap_fit_plane_and_candidates):

    0  region number         5  Objective tilt x (StigX)
    1  focus point (sp_id)   6  Objective tilt y (StigY)
    2  stage x               7  Autofocus confidence
    3  stage y               8  Autostig confidence x
    4  Objective lens (OL)   9  Autostig confidence y

-4 / -5 are Zeiss's own "this wasn't measured" sentinels; the HiMC file uses
the same convention for a field that's genuinely NaN (never measured), but
keeps the real number when AF/AS ran and just scored low - see
save_measured_focusmap_txt's docstring for why that distinction matters here.

This is a stdlib-only, off-hardware diagnostic: it never touches the scope,
only reads two text files.

CLI:
    python compare_focusmaps.py prev\S4_FocusMap.txt run\S4R1\S4R1_HiMC_FocusMap.txt
    python compare_focusmaps.py --dir-a prev\ --dir-b run\ --region S4R1
"""
from __future__ import annotations

import glob
import os
import re
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Tuple

_FIELDS = ["region_num", "sp_id", "x", "y", "ol", "stig_x", "stig_y", "af_conf", "as_conf_x", "as_conf_y"]
# OL/stig VALUE-field sentinels ("never measured") - Zeiss's own convention.
_SENTINELS = (-4.0, -5.0)
# af_conf/as_conf_x/as_conf_y FAILURE codes, per the MultiSEM API doc
# (AP_AUTOFOCUS_CONFIDENCE / AP_AUTOSTIG_CONFIDENCE_X/Y): >0 = successful
# (smaller is better), -1/-2/-3/-4 = documented failure reasons. -5 isn't
# actually a documented confidence code (it's the generic Set_PassedType*
# "out of range" return) but is kept as harmless insurance, matching
# AF_AS_CONFIDENCE_FAILURE_SENTINELS in focus_map.py.
_CONF_FAILURE_SENTINELS = (-1.0, -2.0, -3.0, -4.0, -5.0)


@dataclass
class FocusMapRow:
    region_num: float
    sp_id: int
    x: float
    y: float
    ol: float
    stig_x: float
    stig_y: float
    af_conf: float
    as_conf_x: float
    as_conf_y: float

    def is_failed(self) -> bool:
        if self.ol in _SENTINELS:
            return True
        if self.stig_x in _SENTINELS and self.stig_y in _SENTINELS:
            return True
        # af_conf is lower-is-better (0 = perfect, confirmed against ZEN) -
        # NaN-safe, and only the documented failure codes count as failed,
        # not "<= 0" (that would wrongly flag the best possible readings).
        if self.af_conf != self.af_conf or self.af_conf in _CONF_FAILURE_SENTINELS:
            return True
        return False


def parse_focusmap_txt(path: str) -> Dict[int, FocusMapRow]:
    """Read a FocusMap.txt (prior OR HiMC-measured) into {sp_id: row}. Lines
    that don't parse as a 10-field numeric row (blank, comment, header,
    truncated) are silently skipped - same tolerance focus_map.py's own
    readers use for these files."""
    rows: Dict[int, FocusMapRow] = {}
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = [p.strip() for p in line.split(",")]
            if len(parts) < 10:
                continue
            try:
                vals = [float(p) for p in parts[:10]]
            except ValueError:
                continue
            row = FocusMapRow(
                region_num=vals[0], sp_id=int(vals[1]), x=vals[2], y=vals[3],
                ol=vals[4], stig_x=vals[5], stig_y=vals[6],
                af_conf=vals[7], as_conf_x=vals[8], as_conf_y=vals[9],
            )
            rows[row.sp_id] = row  # last row for a repeated sp_id wins
    return rows


def _fmt(x: Optional[float]) -> str:
    if x is None:
        return "n/a"
    return f"{x:.4g}"


def _fmt_delta(a: Optional[float], b: Optional[float]) -> str:
    if a is None or b is None:
        return "n/a"
    return f"{b - a:+.4g}"


def compare_rows(a: Optional[FocusMapRow], b: Optional[FocusMapRow]) -> List[Tuple[str, str, str, str]]:
    """One (field, a_value, b_value, delta) tuple per compared field, for a
    single sp_id. Missing-in-one-file fields render as 'n/a'."""
    out = []
    for field in ("x", "y", "ol", "stig_x", "stig_y", "af_conf", "as_conf_x", "as_conf_y"):
        av = getattr(a, field) if a else None
        bv = getattr(b, field) if b else None
        out.append((field, _fmt(av), _fmt(bv), _fmt_delta(av, bv)))
    return out


def compare_focusmaps(path_a: str, path_b: str, ol_tol: float = 5.0, stig_tol: float = 0.01) -> int:
    """Print a full comparison; return the count of sp_ids with a
    "meaningfully different" OL or stig value (beyond the given tolerances)
    -- non-zero means something is actually worth investigating further,
    not just float noise."""
    rows_a = parse_focusmap_txt(path_a)
    rows_b = parse_focusmap_txt(path_b)
    all_ids = sorted(set(rows_a) | set(rows_b))

    print(f"A (prior):  {path_a}  ({len(rows_a)} point(s))")
    print(f"B (HiMC):   {path_b}  ({len(rows_b)} point(s))")
    print("=" * 100)

    n_flagged = 0
    for sp_id in all_ids:
        a, b = rows_a.get(sp_id), rows_b.get(sp_id)
        print(f"\nsp[{sp_id}]"
              + (f"  A.failed={a.is_failed()}" if a else "  A: MISSING")
              + (f"  B.failed={b.is_failed()}" if b else "  B: MISSING"))
        flagged_here = False
        for field, av, bv, dv in compare_rows(a, b):
            marker = ""
            if a and b:
                if field == "ol" and abs(b.ol - a.ol) > ol_tol:
                    marker = "  <-- OL differs beyond tolerance"
                    flagged_here = True
                elif field in ("stig_x", "stig_y") and abs(getattr(b, field) - getattr(a, field)) > stig_tol:
                    marker = "  <-- stig differs beyond tolerance"
                    flagged_here = True
            print(f"    {field:10s} A={av:>12s}  B={bv:>12s}  delta={dv:>10s}{marker}")
        if flagged_here:
            n_flagged += 1

    print("\n" + "=" * 100)
    print(f"{n_flagged}/{len(all_ids)} point(s) differ beyond tolerance "
          f"(OL > {ol_tol} uA or stig > {stig_tol})")
    return n_flagged


def find_himc_focusmap(run_dir: str, region_name: str) -> Optional[str]:
    """Locate <run_dir>/<region_folder>/<region_name>_HiMC_FocusMap.txt,
    tolerating the region folder name not being an exact match (e.g. a
    sanitized/duplicate-suffixed folder name)."""
    direct = os.path.join(run_dir, region_name, f"{region_name}_HiMC_FocusMap.txt")
    if os.path.exists(direct):
        return direct
    matches = glob.glob(os.path.join(run_dir, "**", f"{region_name}_HiMC_FocusMap.txt"), recursive=True)
    return matches[0] if matches else None


def find_prev_focusmap(prev_dir: str, region_name: str) -> Optional[str]:
    """Same section-number matching resolve_prev_focusmap_path() uses in
    focus_map.py, without needing to import that module (and its hardware
    dependencies) just for this."""
    m = re.match(r"[Ss](\d+)", region_name)
    if not m:
        return None
    sec = int(m.group(1))
    pat = re.compile(rf'(?:^|[_\-])(?:slice[_\-]?|[sS])0*{sec}(?=$|[_\-.])', re.I)
    matches = sorted(
        f for f in os.listdir(prev_dir)
        if f.lower().endswith(".txt") and pat.search(os.path.splitext(f)[0])
    )
    return os.path.join(prev_dir, matches[0]) if matches else None


def _cli() -> None:
    import argparse

    ap = argparse.ArgumentParser(
        description="Diff a prior FocusMap.txt against this pipeline's measured *_HiMC_FocusMap.txt.")
    ap.add_argument("path_a", nargs="?", help="prior FocusMap.txt (e.g. Focus_Map/S4_FocusMap.txt)")
    ap.add_argument("path_b", nargs="?", help="HiMC-measured FocusMap.txt (e.g. run/S4R1/S4R1_HiMC_FocusMap.txt)")
    ap.add_argument("--dir-a", help="prior focus-map folder (auto-match by section number)")
    ap.add_argument("--dir-b", help="run/batch output folder (auto-find <region>/<region>_HiMC_FocusMap.txt)")
    ap.add_argument("--region", help="region name, e.g. S4R1 (required with --dir-a/--dir-b)")
    ap.add_argument("--ol-tol", type=float, default=5.0, help="OL (uA) difference to flag (default 5.0)")
    ap.add_argument("--stig-tol", type=float, default=0.01, help="stig difference to flag (default 0.01)")
    args = ap.parse_args()

    path_a, path_b = args.path_a, args.path_b
    if args.dir_a or args.dir_b:
        if not args.region:
            ap.error("--region is required with --dir-a/--dir-b")
        if args.dir_a:
            path_a = find_prev_focusmap(args.dir_a, args.region)
            if not path_a:
                ap.error(f"no prior FocusMap.txt found for {args.region} in {args.dir_a}")
        if args.dir_b:
            path_b = find_himc_focusmap(args.dir_b, args.region)
            if not path_b:
                ap.error(f"no *_HiMC_FocusMap.txt found for {args.region} in {args.dir_b}")

    if not path_a or not path_b:
        ap.error("give path_a/path_b directly, or --dir-a/--dir-b + --region")

    n_flagged = compare_focusmaps(path_a, path_b, ol_tol=args.ol_tol, stig_tol=args.stig_tol)
    raise SystemExit(1 if n_flagged else 0)


if __name__ == "__main__":
    _cli()
