"""Regenerate the wafer-wide global QC report (w<N>_global_qc_report.html)
and resync retake_regions.txt from region_review_status.json, on demand,
without starting a new acquisition run.

Why this exists: live_qc_support.write_wafer_qc_report() currently only runs
as a side effect of focus_map.py's main() completing a region during a real
run. If you've just clicked Accept/Retake/Not-verified on the GLOBAL report
in a browser, retake_regions.txt won't reflect that until the next batch
run's _refresh_block_index() call - there was previously no way to force it
sooner. This script is that missing "regenerate now" step.

Note: the per-batch report (<batch>_qc_report.html, inside one run's own
batch folder) keeps its OWN separate region_review_status.json, independent
of the wafer-wide one this script reads. Only the GLOBAL report's Accept/
Retake clicks (region_review_status.json at --output-root itself, not inside
any one batch folder) feed retake_regions.txt / "skip already imaged".

Usage:
    python regenerate_qc_report.py --output-root V:\\...\\One_FSP --json path\\to\\imaging.json
"""
import argparse
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

_WAFER_RE = re.compile(r"[Ww]\d+[Hh]\d+")


def _guess_wafer_label(output_root: Path) -> str:
    """Match write_wafer_qc_report's own w<N>h<N> filename convention by
    reusing an existing BATCH FOLDER's name (which is what a real run passes
    as wafer_label - see focus_map.py's _refresh_block_index), so this
    script updates the SAME report/retake files a real run already created
    instead of writing a second, differently-named copy (e.g.
    'global_qc_report.html' alongside the real 'w10_global_qc_report.html').
    A report filename's own .stem ('w10_global_qc_report') does NOT work
    here - it only carries 'w10', not the 'h<N>' half the regex also needs."""
    for child in sorted(output_root.iterdir()):
        if child.is_dir() and _WAFER_RE.search(child.name):
            return child.name
    return output_root.name


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--output-root", required=True,
                     help="Wafer output root - where w<N>_global_qc_report.html and retake_regions.txt live")
    ap.add_argument("--json", default=None,
                     help="(optional) Zeiss imaging JSON - only needed so regions never yet attempted in any "
                          "batch show up as 'Not started' instead of being omitted; not needed just to sync "
                          "retake_regions.txt for regions that were already attempted.")
    ap.add_argument("--wafer-label", default="",
                     help="Optional report title/filename source - auto-detected from an existing "
                          "w<N>_global_qc_report.html or a wNNhNN-named batch folder under --output-root "
                          "if not given, so this updates the same report in place rather than creating a "
                          "second, differently-named copy.")
    args = ap.parse_args()

    from live_qc_support import write_wafer_qc_report

    output_root = Path(args.output_root)
    region_names = None
    if args.json:
        from focus_map import load_all_region_infos
        region_names = [r.name for r in load_all_region_infos(args.json)]
        print(f"[Regenerate] {len(region_names)} region(s) from {args.json}")

    wafer_label = args.wafer_label or _guess_wafer_label(output_root)
    out = write_wafer_qc_report(
        runs_root=args.output_root,
        canonical_region_names=region_names,
        wafer_label=wafer_label,
        imaging_json_path=args.json,
    )
    if out is None:
        print(f"[Regenerate] FAILED - '{args.output_root}' is not a directory, or nothing to report on.")
        sys.exit(1)
    print(f"[Regenerate] wrote {out}")

    retake_path = Path(args.output_root) / "retake_regions.txt"
    if retake_path.is_file():
        print(f"[Regenerate] retake list synced: {retake_path}")
        print(retake_path.read_text(encoding="utf-8"))
    else:
        print(f"[Regenerate] no regions are currently flagged for retake (no {retake_path.name} written yet) - "
              f"open {out.name} in a browser and use its Accept/Retake/Not-verified controls first.")


if __name__ == "__main__":
    main()
