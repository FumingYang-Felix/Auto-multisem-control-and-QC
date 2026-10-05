"""The file-based control channel between the acquisition PC and this one.

The acquisition computer's msem_control GUI cannot run the analysis: putting
QC back on that machine is the exact problem this package exists to solve.
What it can do is ASK for it. A small folder on the share carries the
request, and the agent on the QC computer does the work:

    <control_root>/
        request_<id>.json      written by the acquisition GUI
        request_<id>.claimed   renamed by the agent, so two agents cannot
                               both take the same job
        status.json            the agent's heartbeat and progress
        response_<id>.json     the outcome
        handled/               finished requests, kept for the record

A folder on the share rather than a network service, because every
cross-machine handshake in this system already works that way
(region_manifest.json, region_complete.json, region_review_status.json,
retake_regions.txt): no ports, no firewall rules, no credentials, and it
survives either machine rebooting.

STDLIB ONLY, ON PURPOSE. The acquisition computer is a separate clone that
does not have this package, so its page needs to speak this protocol without
importing it. Keeping this module dependency-free means the file can simply
be copied over there, or its ~10 lines of JSON reimplemented, with nothing
to install.

Paths inside a request should be UNC. The two machines mount the same share
on different letters (Z: here, V: there), so a drive-letter path written by
one is meaningless to the other - the same reason the reports are rewritten
to UNC (see portable_paths.py).
"""
from __future__ import annotations

import json
import os
import random
import socket
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

PROTOCOL_VERSION = 1

STATUS_NAME = "status.json"
REQUEST_PREFIX = "request_"
RESPONSE_PREFIX = "response_"
CLAIMED_SUFFIX = ".claimed"
HANDLED_DIR = "handled"

# How often the agent refreshes status.json, and how stale it may be before
# a reader should call the agent offline. The multiple matters: a single
# missed write during a network hiccup must not read as "offline".
HEARTBEAT_S = 5.0
OFFLINE_AFTER_S = 30.0

ACTIONS = ("analyze", "analyze_pending", "reports_only", "stop")


def new_id() -> str:
    return f"{time.strftime('%Y%m%d_%H%M%S')}_{random.randint(0x1000, 0xffff):04x}"


def write_json_atomic(path: Path, payload: Dict[str, Any]) -> None:
    """tmp + os.replace, the same discipline as every other file this system
    writes into a tree the other machine is reading."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".tmp{os.getpid()}")
    tmp.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    os.replace(tmp, path)


def read_json(path: Path) -> Optional[Dict[str, Any]]:
    """None rather than raising: a reader polling a folder on a share will
    occasionally catch a file mid-write or not-yet-there, and that is normal
    rather than an error."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


# ---------------------------------------------------------------------------
# the acquisition side
# ---------------------------------------------------------------------------
def submit_request(
    control_root: str | Path,
    image_dir: str,
    action: str = "analyze_pending",
    sections: Optional[List[str]] = None,
    options: Optional[Dict[str, Any]] = None,
    requested_by: str = "",
) -> Dict[str, Any]:
    """Ask the QC computer to analyze something. Returns the request dict.

    Does NOT wait: the caller polls read_status() / read_response(). A GUI
    that blocked here would freeze for the length of a section.
    """
    if action not in ACTIONS:
        raise ValueError(f"action must be one of {ACTIONS}, got {action!r}")
    root = Path(control_root)
    rid = new_id()
    payload = {
        "protocol": PROTOCOL_VERSION,
        "id": rid,
        "action": action,
        "image_dir": str(image_dir),
        "sections": list(sections or []),
        "options": dict(options or {}),
        "requested_by": requested_by or f"{socket.gethostname()}",
        "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "created_epoch": time.time(),
    }
    write_json_atomic(root / f"{REQUEST_PREFIX}{rid}.json", payload)
    return payload


def read_status(control_root: str | Path) -> Optional[Dict[str, Any]]:
    return read_json(Path(control_root) / STATUS_NAME)


def read_response(control_root: str | Path, request_id: str) -> Optional[Dict[str, Any]]:
    return read_json(Path(control_root) / f"{RESPONSE_PREFIX}{request_id}.json")


def is_online(status: Optional[Dict[str, Any]],
              max_age_s: float = OFFLINE_AFTER_S) -> bool:
    """Is the agent alive? A stale heartbeat means the request nobody has
    read yet will never be read - which the GUI must say out loud rather
        than leaving a button that looks like it worked."""
    if not status:
        return False
    try:
        age = time.time() - float(status.get("heartbeat_epoch") or 0.0)
    except (TypeError, ValueError):
        return False
    return 0 <= age <= max_age_s


def status_line(status: Optional[Dict[str, Any]]) -> str:
    """One line an operator can read, for either machine's UI."""
    if not status:
        return "QC computer: no status file yet"
    if not is_online(status):
        seen = status.get("heartbeat_at") or "never"
        return f"QC computer: OFFLINE (last seen {seen})"
    state = str(status.get("state") or "?")
    cur = status.get("current") or {}
    if state == "analyzing" and cur:
        done, total = cur.get("done"), cur.get("total")
        pct = f" {100.0 * done / total:.0f}%" if done and total else ""
        return (f"QC computer: analyzing {cur.get('section') or '?'}"
                f"{pct} - {cur.get('phase') or ''}".rstrip(" -"))
    queued = int(status.get("queue") or 0)
    if queued:
        return f"QC computer: idle, {queued} request(s) queued"
    return f"QC computer: {state}"
