"""Standalone image-quality QC for MultiSEM acquisitions.

Runs on a SEPARATE computer from acquisition. Watches an acquisition output
root on a shared drive, and once a region has finished imaging, scores its
beam images and writes its own report - without ever touching the microscope,
importing focus_map, or needing comtypes.

Why this exists: real-time per-beam QC running inside the acquisition process
made acquisition itself unacceptably slow. Per the operator (2026-09-05),
image-quality scoring now belongs to a separate machine. The acquisition side
is deliberately "passive" - it just writes tiles to disk - and this package is
the consumer.

Design rules (see ../../STANDALONE_QC_HANDOFF.md and STANDALONE_QC_REPLY.md):

  1. NEVER import focus_map (comtypes / hardware deps). The three modules we
     do import - qc_algorithm, qc_overview, live_qc_support - are all
     hardware-free, and live_qc_support imports matplotlib only INSIDE its
     viewer function, so importing it here costs nothing.
  2. Those three files are shared, actively-used-by-acquisition code living in
     a DIFFERENT git clone with no shared remote. Import from them; never edit
     them. Everything new belongs in this subfolder.
  3. Read-only with respect to anything under a batch folder, except the
     explicit two-way channels (region_review_status.json / retake_regions.txt
     at the wafer root). Our own output goes to <region>/StandaloneQC/.
"""
from __future__ import annotations

import sys
from pathlib import Path

# The shared modules (qc_algorithm, qc_overview, live_qc_support) live one
# level up, at the himc_app repo root. Same bootstrap main_live_qc.py and
# regenerate_qc_report.py use, so `python -m standalone_qc` works from
# anywhere rather than only from the repo root.
REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

__all__ = ["REPO_ROOT"]
