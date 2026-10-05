"""Command-line entry point.

--image-dir is a BATCH folder: the timestamped folder holding one
acquisition's section folders. The run folder above it also works, and then
covers every batch in that run.

    # what's there, and what has already been scored
    python -m standalone_qc --image-dir Z:\\...\\Try_1\\w10h31_..._125113 --list

    # score every finished section that hasn't been scored yet
    python -m standalone_qc --image-dir Z:\\...\\w10h31_..._125113

    # one section, re-scoring even if the ledger says it's done
    python -m standalone_qc --image-dir Z:\\...\\w10h31_..._125113 --section S2R3 --force

    # add the (expensive) section overview image
    python -m standalone_qc --image-dir Z:\\...\\w10h31_..._125113 --section S2R3 --overview

Only sections whose acquisition-side status is "done" are scored - see
discover.py for why region_manifest.json / region_complete.json are the right
triggers.

`--output-root` still works as an alias for `--image-dir`, and `--region` /
`--sample` for `--section` / `--batch`, so older commands keep running.
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import ledger, rollup
from .config import load_config, policy_fingerprint
from .discover import find_scorable_regions, list_regions, resolve_image_dir
from .runjob import analyze_one


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m standalone_qc",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--image-dir", "--output-root", dest="output_root",
                   required=True, metavar="PATH",
                   help="The batch folder to check — the timestamped folder "
                        "holding one acquisition's section folders. The run "
                        "folder above it is also accepted, and then covers "
                        "every batch in that run.")
    p.add_argument("--region", "--section", dest="region", default=None,
                   help="Only sections whose name/folder contains this string")
    p.add_argument("--block", "--batch", "--sample", dest="block", default=None,
                   help="Only batches whose name contains this string")
    p.add_argument("--scan", action="store_true",
                   help="Gather the acquisition-derived columns (duration, "
                        "FSP #, file checks, AF/AS) for every finished "
                        "section and cache them, then rebuild the reports. "
                        "Reads no image data. ~2s per section.")
    p.add_argument("--rescan", action="store_true",
                   help="Like --scan but re-reads sections already cached")
    p.add_argument("--reports-only", action="store_true",
                   help="Rebuild the batch and run-level reports from the "
                        "per-section results already on disk, and exit. Reads "
                        "no image data — use it to pick up Accept/Retake "
                        "clicks, or after copying results between machines.")
    p.add_argument("--no-reports", action="store_true",
                   help="Skip the batch/run-level report rebuild that "
                        "normally follows analysis")
    p.add_argument("--exact", action="store_true",
                   help="Treat --section/--sample as exact names rather than "
                        "substrings (what the GUI passes, so selecting one "
                        "section can never also match a longer name)")
    p.add_argument("--list", action="store_true", dest="do_list",
                   help="List sections, their acquisition status and whether "
                        "they have been analyzed; do nothing else")
    p.add_argument("--force", action="store_true",
                   help="Re-analyze sections the ledger already has under this policy")
    p.add_argument("--limit", type=int, default=0,
                   help="Analyze at most N sections this invocation (0 = no limit)")
    p.add_argument("--overview-ds", type=int, default=None, metavar="N",
                   help="Downsample factor for the section overview image "
                        "(default 16). Higher is faster and smaller with less "
                        "detail.")
    p.add_argument("--no-overview", action="store_true",
                   help="Skip building the section overview. Only for "
                        "troubleshooting — the overview is panel 0 of the "
                        "dashboard, so without it the hex maps have no image "
                        "to sit next to.")
    p.add_argument("--sampling", default=None,
                   choices=["first_mid_last_tissue", "every_n", "all"],
                   help="mFoV sampling policy (default: first_mid_last_tissue)")
    p.add_argument("--every-n", type=int, default=None,
                   help="With --sampling every_n: analyze every Nth mFoV")
    p.add_argument("--no-map-pass", "--no-probe", action="store_true",
                   dest="no_probe",
                   help="Skip the map pass. TROUBLESHOOTING ONLY: that pass "
                        "is what produces per-mFoV results, so without it "
                        "the overview quality numbers, IMQA, the scanfault "
                        "and jitter checks and the retake lasso all fall "
                        "back to the handful of fully-analyzed mFoVs. "
                        "(--no-probe is the old name for this flag.)")
    p.add_argument("--workers", type=int, default=None,
                   help="How many sFoVs (single beam images) to read and "
                        "measure at the same time; the GUI calls this "
                        "'# sFoVs Processed Concurrently' (default 16)")
    p.add_argument("--quality-threshold", type=float, default=None,
                   metavar="Q",
                   help="Image quality at or above which an mFoV counts as "
                        "good (default 10). Sets the colour scale and the "
                        "quality tiers in the reports; it does NOT change "
                        "any measurement, and the dashboard lets you move it "
                        "afterwards without re-analyzing.")
    p.add_argument("--high-quality-threshold", type=float, default=None,
                   metavar="Q",
                   help="Top of the colour scale (default 30). Quality at or "
                        "above this is fully green.")
    p.add_argument("--imaging-json", default=None, metavar="PATH",
                   help="The Zeiss imaging JSON this batch was acquired "
                        "from. The dashboard's Retake selection panel clones "
                        "the original region XML out of it to build a "
                        "partial retake. Found automatically by matching "
                        "region names if omitted; pass --no-imaging-json to "
                        "skip the search.")
    p.add_argument("--no-imaging-json", action="store_true",
                   help="Do not look for an imaging JSON (the Retake "
                        "selection panel then cannot build sub-regions)")
    p.add_argument("--config", default=None,
                   help="JSON file of config overrides")
    p.add_argument("--dry-run", action="store_true",
                   help="Show what would be analyzed, without reading images")
    p.add_argument("--quiet", action="store_true", help="Suppress progress lines")
    return p


def _make_progress(quiet: bool):
    """Throttled progress printer - one line at most every 2 s, plus the
    last. Per-image lines would be tens of thousands of lines over a region
    and would themselves slow a run down over a remote session."""
    state = {"last": 0.0, "phase": ""}

    def progress(done: int, total: int, label: str) -> None:
        if quiet:
            return
        phase = label.split(" tile")[0].split(" beam")[0]
        now = time.monotonic()
        if phase != state["phase"]:
            state["phase"] = phase
            state["last"] = 0.0
        if done and done < total and (now - state["last"]) < 2.0:
            return
        state["last"] = now
        pct = (100.0 * done / total) if total else 0.0
        print(f"    [{pct:5.1f}%] {done}/{total}  {label}", flush=True)

    return progress


def _print_listing(output_root: str, block: Optional[str],
                   entries: Dict[str, Dict[str, Any]], fingerprint: str,
                   exact: bool = False) -> int:
    regions = list_regions(output_root, block, exact)
    if not regions:
        print(f"No batch folder with a region_manifest.json in or under "
              f"{output_root}")
        return 1

    # Grouped by batch: the batch name is long and identical for every one of
    # its sections, so repeating it down a column buries the part that
    # actually varies.
    by_batch: Dict[Path, List[Any]] = {}
    for r in regions:
        by_batch.setdefault(r.block_dir, []).append(r)

    for batch_dir, rows in by_batch.items():
        first = rows[0]
        print(f"\nSample {first.sample_id or '—'}  ·  run {first.run_label}  "
              f"·  batch {batch_dir.name}")
        print(f"  {'SECTION':<16} {'IMAGED':<9} {'SIGNAL':<9} {'QC':<13} DETAIL")
        print("  " + "-" * 74)
        for r in rows:
            if ledger.is_done(entries, r.key, fingerprint):
                qc = "analyzed"
            elif r.key in entries:
                qc = str(entries[r.key].get("status", "?"))[:12]
            elif r.status == "done":
                qc = "awaiting analysis"
            else:
                qc = "—"
            # "trigger" = the per-section region_complete.json; "manifest" =
            # the batch-level file, all a pre-2026-09-07 run has.
            src = "trigger" if r.status_source == "region_complete" else "manifest"
            print(f"  {r.name[:15]:<16} {r.status:<9} {src:<9} {qc:<13} "
                  f"{(r.detail or '')[:26]}")
    n_pending = sum(
        1 for r in regions
        if r.status == "done" and not ledger.is_done(entries, r.key, fingerprint)
    )
    print(f"\n{len(regions)} section(s); {n_pending} finished and "
          f"awaiting analysis.")
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    args = _build_parser().parse_args(argv)

    overrides: Dict[str, Any] = {}
    if args.sampling:
        overrides["tile_sampling"] = args.sampling
    if args.every_n is not None:
        overrides["tile_sampling_every_n"] = args.every_n
    if args.workers is not None:
        overrides["max_workers"] = args.workers
    if args.no_probe:
        overrides["tissue_probe_enabled"] = False
    if args.overview_ds is not None:
        overrides["overview_ds"] = args.overview_ds
    if args.quality_threshold is not None:
        overrides["low_quality_threshold"] = args.quality_threshold
    if args.high_quality_threshold is not None:
        overrides["high_quality_threshold"] = args.high_quality_threshold
    if args.imaging_json:
        overrides["imaging_json_path"] = args.imaging_json
    if args.no_imaging_json:
        overrides["imaging_json_search_enabled"] = False
    if args.no_overview:
        overrides["region_overview_enabled"] = False

    try:
        cfg = load_config(args.config, **overrides)
    except (OSError, ValueError) as exc:
        print(f"[StandaloneQC] bad --config: {exc}", file=sys.stderr)
        return 2

    given = Path(args.output_root)
    if not given.is_dir():
        print(f"[StandaloneQC] --image-dir is not a directory: {given}",
              file=sys.stderr)
        return 2

    # A batch folder is the expected input; the run folder above it also
    # works. Resolving to the run folder keeps the ledger and the wafer-level
    # review files where the acquisition pipeline already puts them - see
    # discover.resolve_image_dir.
    root, batch_from_path = resolve_image_dir(given)
    if batch_from_path:
        print(f"[StandaloneQC] batch '{batch_from_path}' (run: {root.name})")
        if not args.block:
            args.block = batch_from_path
            args.exact = True

    fingerprint = policy_fingerprint(cfg)
    entries = ledger.load(root)

    if args.do_list:
        return _print_listing(str(root), args.block, entries, fingerprint,
                              exact=args.exact)

    if args.scan or args.rescan:
        def _tick(i: int, total: int, name: str) -> None:
            if not args.quiet:
                print(f"    [{100.0*i/max(1,total):5.1f}%] {i}/{total}  {name}",
                      flush=True)
        n = rollup.scan_sections(
            root, cfg, batch_filter=args.block,
            only_missing=not args.rescan, on_progress=_tick,
        )
        print(f"[StandaloneQC] scanned {n} section(s)")
        written = rollup.write_all_reports(root, cfg, batch_filter=args.block)
        for name, path in written.items():
            if name == "__global__":
                print(f"[StandaloneQC] retake manager: {path}")
        return 0

    if args.reports_only:
        written = rollup.write_all_reports(root, cfg, batch_filter=args.block)
        if not written:
            print("[StandaloneQC] nothing to report on - no section results "
                  "found under this path yet.")
            return 1
        for name, path in written.items():
            label = "run-level" if name == "__global__" else f"batch {name}"
            print(f"[StandaloneQC] {label}: {path}")
        return 0

    candidates = find_scorable_regions(str(root), args.block, args.region,
                                        exact=args.exact)
    n_matched = len(candidates)
    skipped: List[str] = []
    if not args.force:
        keep = []
        for r in candidates:
            if ledger.is_done(entries, r.key, fingerprint):
                skipped.append(r.key)
            else:
                keep.append(r)
        candidates = keep
    if args.limit > 0:
        candidates = candidates[: args.limit]

    if not candidates:
        if skipped:
            # A "!" line so the GUI runner surfaces it as a warning. Reporting
            # this as a bare "nothing to do" is how a re-analysis request can
            # look like it ran and changed nothing.
            print(f"! {len(skipped)} section(s) already analyzed under policy "
                  f"{fingerprint} and were skipped: "
                  f"{', '.join(skipped[:6])}"
                  f"{' ...' if len(skipped) > 6 else ''}")
            print("[StandaloneQC] nothing to do - every matching section is "
                  "already analyzed. Use --force (or the GUI's Re-analyze "
                  "checkbox) to run them again.")
        else:
            print("[StandaloneQC] nothing to do - no finished, un-analyzed "
                  "section matched. Try --list, or --force to re-analyze.")
        return 0
    if skipped:
        print(f"[StandaloneQC] skipping {len(skipped)} of {n_matched} matched "
              f"section(s): already analyzed under policy {fingerprint}.")

    print(f"[StandaloneQC] policy {fingerprint} | "
          f"sampling={cfg['tile_sampling']} beams={cfg['beam_selection_mode']} "
          f"workers={cfg['max_workers']}")
    print(f"[StandaloneQC] {len(candidates)} section(s) to analyze:")
    for r in candidates:
        print(f"    {r.key}")

    if args.dry_run:
        print("[StandaloneQC] --dry-run: stopping before reading any images.")
        return 0

    progress = _make_progress(args.quiet)
    n_ok = 0
    for r in candidates:
        # One implementation, shared with the agent - see runjob.py. The log
        # wording is load-bearing (the GUI's runner parses it), so it lives
        # there rather than being duplicated per caller.
        if analyze_one(r, root, cfg, fingerprint, on_progress=progress).get("ok"):
            n_ok += 1

    # Roll the per-section results up into the batch and run-level reports.
    # Cheap (summary JSONs only, no image data), so it runs after every batch
    # rather than needing a separate step an operator has to remember.
    if n_ok and not args.no_reports:
        written = rollup.write_all_reports(root, cfg)
        for name, path in written.items():
            if name == "__global__":
                print(f"[StandaloneQC] run-level report: {path}")
        for name, path in written.items():
            if name != "__global__":
                print(f"[StandaloneQC] batch report:     {path}")

    print(f"\n[StandaloneQC] done - {n_ok}/{len(candidates)} section(s) analyzed.")
    return 0 if n_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
