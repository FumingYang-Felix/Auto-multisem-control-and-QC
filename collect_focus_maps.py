#!/usr/bin/env python3
"""CLI wrapper around msem_control.engine.focus_map_collector - see that
module's docstring for why this exists. The HI-MC GUI page runs the same
logic via a "Collect Focus Maps" step ahead of its "Raw focus-map dir"
field (msem_control/ui/pages/himc_page.py).

Usage:
    python collect_focus_maps.py [source_root] [dest_dir]
Defaults:
    source_root = V:\\Richard\\Vlad\\w10h31_test_data\\R2_test2\\w10h31_dye_R2_20260830_01-44-38
    dest_dir    = D:\\Users\\Vlad\\Section _detection-files\\Focus_Map_R2_test2
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from msem_control.engine.focus_map_collector import collect_focus_maps

DEFAULT_SOURCE = r"V:\Richard\Vlad\w10h31_test_data\R2_test2\w10h31_dye_R2_20260830_01-44-38"
DEFAULT_DEST = r"D:\Users\Vlad\Section _detection-files\Focus_Map_R2_test2"


def main():
    source_root = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_SOURCE
    dest_dir = sys.argv[2] if len(sys.argv) > 2 else DEFAULT_DEST

    try:
        result = collect_focus_maps(source_root, dest_dir)
    except (FileNotFoundError, RuntimeError) as exc:
        print(exc)
        return 1

    print(f"Found {result.found_count} FocusMap.txt file(s) under {source_root}\n")
    for src, dest, note in result.copied:
        print(f"  {src}\n    -> {dest}  [{note}]")
    for w in result.warnings:
        print(f"WARNING: {w}")

    print(f"\nCopied {len(result.copied)}/{result.found_count} file(s) to {dest_dir}")
    if result.skipped:
        print(f"\nSkipped {len(result.skipped)} file(s):")
        for path, reason in result.skipped:
            print(f"  {path}: {reason}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
