"""Which regions this machine has already scored.

One JSON file at the wafer output root, so a restart (or a `--watch` loop
that gets interrupted) doesn't re-read tens of GB it has already processed.

Keyed by "<batch_folder>/<region_folder>" - a region name alone isn't unique
across a wafer, since the same region gets re-imaged in later batches and
each attempt deserves its own scoring record.

Entries record the config's policy fingerprint, so changing something that
affects the numbers (dsamp, the sampling policy) correctly invalidates prior
results, while changing something that doesn't (worker count, an overview
flag) leaves them alone.

Written atomically, same tmp + os.replace discipline focus_map.py uses for
region_manifest.json - the acquisition machine may be reading this root at
the same time, and a half-written JSON here would be indistinguishable from
a corrupt one.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Dict, Optional

LEDGER_NAME = "standalone_qc_state.json"


def ledger_path(output_root: str | Path) -> Path:
    return Path(output_root) / LEDGER_NAME


def load(output_root: str | Path) -> Dict[str, Dict[str, Any]]:
    path = ledger_path(output_root)
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        # A corrupt ledger must not stop QC - worst case we re-score, which
        # is wasteful but correct. Silently starting fresh beats refusing.
        return {}
    entries = data.get("regions") if isinstance(data, dict) else None
    return entries if isinstance(entries, dict) else {}


def save(output_root: str | Path, entries: Dict[str, Dict[str, Any]]) -> None:
    path = ledger_path(output_root)
    payload = {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "regions": entries,
    }
    tmp = path.with_suffix(path.suffix + ".tmp")
    try:
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        os.replace(tmp, path)
    except OSError as exc:
        print(f"[Ledger] WARNING: could not write {path}: {exc}")


def is_done(
    entries: Dict[str, Dict[str, Any]],
    key: str,
    fingerprint: str,
) -> bool:
    """True if this region was already scored under the SAME policy."""
    entry = entries.get(key)
    if not isinstance(entry, dict):
        return False
    return entry.get("policy") == fingerprint and entry.get("status") == "scored"


def attempt_status(
    entries: Dict[str, Dict[str, Any]],
    key: str,
    fingerprint: str,
) -> Optional[str]:
    """The recorded status for this region under the SAME policy, or None if
    it has never been attempted under these settings.

    Distinct from is_done(): a region that was attempted and failed has an
    entry ("error"/"no_data") but is not done. Automatic queueing needs to
    tell those apart, otherwise a section that fails once gets retried on
    every poll for the rest of the run.
    """
    entry = entries.get(key)
    if not isinstance(entry, dict) or entry.get("policy") != fingerprint:
        return None
    status = entry.get("status")
    return str(status) if status else None


def mark(
    output_root: str | Path,
    key: str,
    fingerprint: str,
    status: str = "scored",
    detail: Optional[Dict[str, Any]] = None,
) -> Dict[str, Dict[str, Any]]:
    """Record one region's outcome and persist. Re-reads before writing so
    two concurrent runs against the same root don't clobber each other's
    entries (they'd still race on the file itself, but the atomic replace
    means the loser's entry is simply absent, not corrupt)."""
    entries = load(output_root)
    entries[key] = {
        "status": status,
        "policy": fingerprint,
        "scored_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        **(detail or {}),
    }
    save(output_root, entries)
    return entries
