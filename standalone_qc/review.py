"""Accept / Retake / Not-verified controls, shared with the acquisition side.

This is the one place where standalone QC deliberately reaches for PRIVATE
helpers in live_qc_support. The reasoning:

  - The review UI is ~270 lines of subtle browser JavaScript: File System
    Access API, an IndexedDB-remembered file handle, permission re-confirmation,
    debounced autosave, and a dirty-set reconcile so an unsaved local edit is
    never clobbered by what is on disk. Copying that would mean maintaining
    two versions of code whose failure modes are hard to see and harder to
    test, and any divergence would show up as "the buttons behave differently
    depending on which report you opened".
  - Both sides must write the SAME region_review_status.json at the run root -
    that file is the sanctioned two-way channel, and it feeds
    retake_regions.txt, which the acquisition pipeline reads to decide what to
    re-image. Sharing the generator is what guarantees they agree on format
    and on the IndexedDB key that identifies the file.

The cost is a dependency on three underscore-prefixed names in a file owned by
the other clone. That is guarded below: if any of them is renamed the reports
still build, minus the review column, with a visible note saying so - a loud,
diagnosable degradation rather than a traceback or a silently missing feature.
Claude 1 has been asked to add these three to the notify list.

Public helpers used here (already on the sanctioned list):
read_region_review_status, sync_retake_list, scan_partial_retakes_for_wafer.
"""
from __future__ import annotations

import html as _html
import json
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

REVIEW_STATUS_FILENAME = "region_review_status.json"
RETAKE_LIST_FILENAME = "retake_regions.txt"

try:
    from live_qc_support import (
        REGION_REVIEW_STATUS_FILENAME as _LQS_REVIEW_FILENAME,
        _review_cell_html,
        _review_controls_header_html,
        _review_controls_script_html,
        read_region_review_status,
    )
    REVIEW_STATUS_FILENAME = _LQS_REVIEW_FILENAME
    REVIEW_UI_AVAILABLE = True
    REVIEW_UI_ERROR = ""
except Exception as _exc:  # pragma: no cover - only on an upstream rename
    REVIEW_UI_AVAILABLE = False
    REVIEW_UI_ERROR = f"{type(_exc).__name__}: {_exc}"

    def read_region_review_status(path):  # type: ignore[misc]
        """Fallback reader - same format, same default of {} when absent."""
        p = Path(path)
        if not p.is_file():
            return {}
        try:
            data = json.loads(p.read_text(encoding="utf-8-sig"))
        except Exception:
            return {}
        if not isinstance(data, dict):
            return {}
        out: Dict[str, Dict[str, str]] = {}
        for name, entry in data.items():
            if isinstance(entry, dict):
                out[str(name)] = {
                    "status": str(entry.get("status", "not_verified")),
                    "reason": str(entry.get("reason", "") or ""),
                }
        return out


def review_status_path(run_root: str | Path) -> Path:
    """region_review_status.json lives at the RUN root - next to
    w<N>_global_qc_report.html - not inside any one batch folder. Each batch
    report keeps its own separate copy on the acquisition side; only the
    run-level one feeds retake_regions.txt."""
    return Path(run_root) / REVIEW_STATUS_FILENAME


def load_review_status(run_root: str | Path) -> Dict[str, Dict[str, str]]:
    return read_region_review_status(review_status_path(run_root))


def cell_html(
    section_name: str, review_status: Dict[str, Dict[str, str]]
) -> Tuple[str, Dict[str, str]]:
    """One section's review control group. Returns (html, current_entry)."""
    if REVIEW_UI_AVAILABLE:
        return _review_cell_html(section_name, review_status)
    entry = review_status.get(section_name, {"status": "not_verified", "reason": ""})
    label = {"accepted": "Accepted", "retake": "Retake"}.get(
        entry["status"], "Not verified"
    )
    return f'<span class="chip" style="background:#eee;color:#666;">{_html.escape(label)}</span>', entry


def controls_header_html(retake_sync_note: str) -> str:
    if REVIEW_UI_AVAILABLE:
        return _review_controls_header_html(retake_sync_note)
    return (
        '<div class="review-banner">The Accept/Retake controls are '
        'unavailable: standalone QC could not load them from '
        f'live_qc_support ({_html.escape(REVIEW_UI_ERROR)}). Review status is '
        'shown read-only below. Use the acquisition side\'s own QC report to '
        'change it, or tell the developer that those helpers were renamed.'
        "</div>"
    )


def controls_script_html(
    run_root: str | Path, review_defaults: Dict[str, Dict[str, str]]
) -> str:
    if REVIEW_UI_AVAILABLE:
        # The RUN root, so every report - ours and the acquisition side's -
        # reconnects to the same remembered file handle and the same JSON.
        return _review_controls_script_html(run_root, review_defaults)
    return ""


def retake_sync_note() -> str:
    return (
        f"<code>{RETAKE_LIST_FILENAME}</code> is kept in sync automatically "
        f"the next time a report is regenerated - it is not written directly "
        f"by the browser."
    )


def sync_retake_list_if_possible(
    run_root: str | Path, review: Dict[str, Dict[str, str]]
) -> Optional[bool]:
    """Push whole-section Retake decisions into retake_regions.txt, the plain
    text file the acquisition pipeline actually consumes for "skip already
    imaged".

    Returns True if written, False if already in sync, None if the upstream
    helpers were unavailable. Partial (per-mFoV) retakes are passed through
    from the acquisition side's own wafer retake JSON when readable, so
    syncing from here never drops a pending partial someone flagged in a
    region dashboard.
    """
    try:
        from live_qc_support import scan_partial_retakes_for_wafer, sync_retake_list
    except Exception:
        return None
    try:
        partials = scan_partial_retakes_for_wafer(run_root)
    except Exception:
        partials = {}
    try:
        return bool(
            sync_retake_list(review, partials, Path(run_root) / RETAKE_LIST_FILENAME)
        )
    except Exception as exc:
        print(f"[StandaloneQC] WARNING: retake sync failed: {exc}")
        return None
