"""Bring already-written HTML up to date, without re-reading any images.

Some improvements are pure HTML post-processing. Re-running the analysis
to pick them up would re-read every beam of every mFoV - about 36,000 images
for a 400-mFoV section - and that competes with acquisition for the share.
These passes read, replace and write instead. No image I/O.

Two passes, both idempotent, so running this twice or over a mix of old and
new pages changes nothing the second time:

  1. panel 2 renamed to "Stig Sharpness Anisotropy (horz - vert, %)", with
     the hover text explaining that it is not the stigmator setting.
  2. this machine's drive-letter paths rewritten to UNC, so the page works
     from any computer that can reach the share (see portable_paths.py).
  3. the focus / stig / status hex-map panels removed (see dashboard.py).

Pass 3 only touches dashboards. Note that the colour bar added to the 3D
plots is NOT retrofittable: it is drawn from data in each plot's payload,
which only a fresh analysis can write. Older pages simply draw no bar.

    python -m standalone_qc.retrofit <batch-or-run-folder>
"""
from __future__ import annotations

import sys
from pathlib import Path

from . import portable_paths
from .config import load_config
from .dashboard import (
    DASHBOARD_NAME,
    STIG_HEADING_NEW,
    STIG_HEADING_OLD,
    _strip_panels,
)
from .rollup import BATCH_REPORT_NAME, GLOBAL_REPORT_NAME, SECTION_REPORT_NAME


def patch_file(path: Path) -> bool:
    """Apply every pass to one file. True if anything changed."""
    try:
        html = path.read_text(encoding="utf-8")
    except OSError:
        return False
    before = html
    html = html.replace(STIG_HEADING_OLD, STIG_HEADING_NEW, 1)
    html, _ = portable_paths.make_portable(html)
    if path.name == DASHBOARD_NAME:
        html = _strip_panels(html, load_config(None))
    if html == before:
        return False
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(html, encoding="utf-8")
    tmp.replace(path)          # atomic, like everything else we write
    return True


def main(argv=None) -> int:
    args = list(argv if argv is not None else sys.argv[1:])
    if not args:
        print(__doc__)
        return 2
    root = Path(args[0])
    if not root.is_dir():
        print(f"not a folder: {root}")
        return 1
    done = skipped = 0
    # Every page we write, not just dashboards: the batch and run reports
    # carry drive-letter links too.
    names = (DASHBOARD_NAME, SECTION_REPORT_NAME,
             BATCH_REPORT_NAME, GLOBAL_REPORT_NAME)
    seen = set()
    for name in names:
        for path in sorted(root.rglob(name)):
            if path in seen:
                continue
            seen.add(path)
            if patch_file(path):
                done += 1
                print(f"  updated {path.relative_to(root)}")
            else:
                skipped += 1
    print(f"[StandaloneQC] {done} page(s) updated, "
          f"{skipped} already current.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
