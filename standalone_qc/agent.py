"""The QC-computer side of the control channel.

Watches a folder on the share for requests written by the acquisition
computer's msem_control GUI, runs the analysis HERE, and publishes progress
back into the same folder. See control.py for the protocol, and why it is
files on a share rather than a network service.

    python -m standalone_qc.agent --control-root //server/share/QcControl

Three things worth knowing about the implementation:

HEARTBEAT ON ITS OWN THREAD. A 400-mFoV section spends about 15 minutes
inside one overview build. If status.json were written only between
sections, the acquisition GUI would conclude the agent had died in the
middle of every large section and tell the operator so. The heartbeat thread
writes every few seconds regardless of what the worker is doing.

CLAIM BY RENAME. A request is taken by os.replace()-ing it to
request_<id>.json.claimed before any work starts. Rename is atomic on SMB,
so if two agents are ever pointed at one control folder, exactly one wins
and the other moves on instead of both analyzing the same section.

NEVER DIE ON ONE BAD REQUEST. Malformed JSON, a path that does not exist, a
section that fails to score: each is recorded in its response and the loop
carries on. An agent that exits on a bad request is an agent the operator
finds stopped hours later, with a run unanalyzed.
"""
from __future__ import annotations

import argparse
import os
import socket
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import control, ledger, rollup
from .config import load_config, policy_fingerprint
from .control import (
    CLAIMED_SUFFIX,
    HANDLED_DIR,
    HEARTBEAT_S,
    PROTOCOL_VERSION,
    REQUEST_PREFIX,
    RESPONSE_PREFIX,
    STATUS_NAME,
    read_json,
    write_json_atomic,
)
from .discover import find_scorable_regions, resolve_image_dir
from .runjob import analyze_one


class AgentState:
    """What the heartbeat publishes. Locked because the worker writes it
    while the heartbeat thread reads it."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._d: Dict[str, Any] = {
            "state": "starting", "current": None, "queue": 0,
            "last_result": None, "requests_seen": 0, "errors": 0,
        }

    def update(self, **kw: Any) -> None:
        with self._lock:
            self._d.update(kw)

    def bump(self, key: str, by: int = 1) -> None:
        with self._lock:
            self._d[key] = int(self._d.get(key) or 0) + by

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            return dict(self._d)


class Agent:
    def __init__(self, control_root: Path, cfg: Dict[str, Any],
                 poll_s: float = 5.0, log=None) -> None:
        self.root = Path(control_root)
        self.cfg = cfg
        self.poll_s = max(1.0, float(poll_s))
        self.state = AgentState()
        self.log = log or (lambda m: print(m, flush=True))
        # Two flags, deliberately: _stop means "shut the agent down" and
        # is set by a stop request, while _hb_stop only parks the heartbeat
        # thread when run() returns. Using one event for both made run()
        # single-shot - a second call returned instantly having done
        # nothing, because the first call's cleanup had set it.
        self._stop = threading.Event()
        self._hb_stop = threading.Event()
        self._hb: Optional[threading.Thread] = None

    # --- status ----------------------------------------------------------
    def _write_status(self) -> None:
        snap = self.state.snapshot()
        snap.update({
            "protocol": PROTOCOL_VERSION,
            "agent": "standalone_qc.agent",
            "host": socket.gethostname(),
            "pid": os.getpid(),
            "control_root": str(self.root),
            "heartbeat_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "heartbeat_epoch": time.time(),
        })
        try:
            write_json_atomic(self.root / STATUS_NAME, snap)
        except OSError as exc:
            # The share blinking must not kill the agent. The acquisition
            # side sees a stale heartbeat and says so.
            self.log(f"[Agent] WARNING: could not write status: {exc}")

    def _heartbeat_loop(self) -> None:
        while not self._hb_stop.wait(HEARTBEAT_S):
            self._write_status()

    # --- requests --------------------------------------------------------
    def _pending_requests(self) -> List[Path]:
        try:
            return sorted(p for p in self.root.glob(REQUEST_PREFIX + "*.json")
                          if p.is_file())
        except OSError:
            return []

    def _claim(self, path: Path) -> Optional[Path]:
        """Take ownership atomically, or None if another agent got there."""
        target = path.with_name(path.name + CLAIMED_SUFFIX)
        try:
            os.replace(path, target)
            return target
        except OSError:
            return None

    @staticmethod
    def _rid_from_path(path: Path) -> str:
        """Fallback id for a request whose JSON we could not read: strip the
        request_ prefix and every suffix, so request_x.json.claimed -> x."""
        name = path.name
        if name.startswith(REQUEST_PREFIX):
            name = name[len(REQUEST_PREFIX):]
        for suffix in (CLAIMED_SUFFIX, ".json"):
            if name.endswith(suffix):
                name = name[: -len(suffix)]
        return name or "unknown"

    def _respond(self, rid: str, payload: Dict[str, Any]) -> None:
        payload.setdefault("protocol", PROTOCOL_VERSION)
        payload.setdefault("finished_at", time.strftime("%Y-%m-%d %H:%M:%S"))
        try:
            write_json_atomic(self.root / (RESPONSE_PREFIX + rid + ".json"),
                              payload)
        except OSError as exc:
            self.log(f"[Agent] WARNING: could not write response {rid}: {exc}")

    def _archive(self, claimed: Path) -> None:
        try:
            dest = self.root / HANDLED_DIR / claimed.name
            dest.parent.mkdir(parents=True, exist_ok=True)
            os.replace(claimed, dest)
        except OSError:
            pass        # leaving it claimed is harmless - it is never re-run

    # --- one request -----------------------------------------------------
    def _cfg_for(self, req: Dict[str, Any]) -> Dict[str, Any]:
        """Requests may override config, but only keys we recognise: an
        unknown key from a future acquisition-side page must not silently
        change scoring policy."""
        allowed = ("tile_sampling", "tile_sampling_every_n", "max_workers",
                   "overview_ds", "region_overview_enabled",
                   "tissue_probe_enabled", "dashboard_enabled",
                   "imaging_json_path")
        opts = {k: v for k, v in (req.get("options") or {}).items()
                if k in allowed and v is not None}
        return load_config(None, **opts) if opts else dict(self.cfg)

    def handle(self, req: Dict[str, Any]) -> Dict[str, Any]:
        rid = str(req.get("id") or control.new_id())
        action = str(req.get("action") or "analyze_pending")
        image_dir = str(req.get("image_dir") or "").strip()
        result: Dict[str, Any] = {
            "id": rid, "action": action, "image_dir": image_dir,
            "accepted_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "sections": [], "ok": False,
        }

        if action == "stop":
            self.log("[Agent] stop requested")
            self._stop.set()
            result.update(ok=True, note="agent stopping")
            return result

        if not image_dir or not Path(image_dir).is_dir():
            result["error"] = f"image_dir is not a folder: {image_dir!r}"
            self.log(f"[Agent] rejected {rid}: {result['error']}")
            return result

        cfg = self._cfg_for(req)
        fingerprint = policy_fingerprint(cfg)
        try:
            root, batch_filter = resolve_image_dir(image_dir)
        except Exception as exc:
            result["error"] = f"could not resolve {image_dir}: {exc}"
            return result

        if action == "reports_only":
            written = rollup.write_all_reports(root, cfg,
                                               batch_filter=batch_filter)
            result.update(ok=bool(written),
                          reports={k: str(v) for k, v in (written or {}).items()})
            return result

        wanted = [str(s) for s in (req.get("sections") or [])]
        entries = ledger.load(root)
        candidates = find_scorable_regions(str(root), batch_filter, None,
                                           exact=False)
        if wanted:
            # Named sections are analyzed whether the ledger has them or
            # not: naming a section IS the instruction, the same rule the
            # GUI's "Analyze Selected" follows.
            candidates = [r for r in candidates
                          if r.name in wanted or r.folder in wanted]
        else:
            candidates = [r for r in candidates
                          if not ledger.is_done(entries, r.key, fingerprint)]

        if not candidates:
            result.update(ok=True, note="nothing to analyze")
            self.log(f"[Agent] {rid}: nothing to analyze")
            return result

        self.log(f"[Agent] {rid}: {len(candidates)} section(s) to analyze")
        self.state.update(queue=len(candidates))
        done = 0
        for r in candidates:
            if self._stop.is_set():
                result["note"] = "stopped before finishing the queue"
                break

            def progress(d: int, t: int, label: str,
                         _key=r.key, _name=r.name) -> None:
                self.state.update(state="analyzing", current={
                    "key": _key, "section": _name, "image_dir": image_dir,
                    "done": d, "total": t, "phase": label,
                })

            self.state.update(state="analyzing", current={
                "key": r.key, "section": r.name, "image_dir": image_dir,
                "done": 0, "total": 0, "phase": "starting",
            })
            res = analyze_one(r, root, cfg, fingerprint,
                              log=self.log, on_progress=progress)
            result["sections"].append(res)
            done += 1
            self.state.update(last_result=res,
                              queue=max(0, len(candidates) - done))

        self.state.update(state="idle", current=None, queue=0)
        try:
            rollup.write_all_reports(root, cfg)
        except Exception as exc:
            self.log(f"[Agent] WARNING: report rebuild failed: {exc}")
        result["ok"] = any(s.get("ok") for s in result["sections"])
        return result

    # --- loop ------------------------------------------------------------
    def run(self, once: bool = False) -> int:
        self.root.mkdir(parents=True, exist_ok=True)
        self.log(f"[Agent] watching {self.root} every {self.poll_s:g}s "
                 f"(host {socket.gethostname()}, pid {os.getpid()})")
        self.state.update(state="idle")
        self._write_status()
        self._hb_stop.clear()
        self._hb = threading.Thread(target=self._heartbeat_loop, daemon=True)
        self._hb.start()
        try:
            while not self._stop.is_set():
                for path in self._pending_requests():
                    claimed = self._claim(path)
                    if claimed is None:
                        continue                    # another agent took it
                    self.state.bump("requests_seen")
                    req = read_json(claimed) or {}
                    rid = str(req.get("id") or self._rid_from_path(claimed))
                    if not req:
                        self._respond(rid, {"id": rid, "ok": False,
                                            "error": "unreadable request JSON"})
                        self.state.bump("errors")
                        self._archive(claimed)
                        continue
                    try:
                        res = self.handle(req)
                    except Exception as exc:
                        import traceback
                        traceback.print_exc()
                        res = {"id": rid, "ok": False,
                               "error": f"{type(exc).__name__}: {exc}"}
                        self.state.bump("errors")
                    self._respond(rid, res)
                    self._archive(claimed)
                    self._write_status()
                    if self._stop.is_set():
                        break
                if once:
                    break
                self._stop.wait(self.poll_s)
        except KeyboardInterrupt:
            self.log("[Agent] interrupted")
        finally:
            self._hb_stop.set()
            if self._hb is not None:
                self._hb.join(timeout=HEARTBEAT_S * 2)
            self.state.update(
                state="stopped" if self._stop.is_set() else "idle",
                current=None)
            self._write_status()
        return 0


def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(
        prog="python -m standalone_qc.agent",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--control-root", required=True, metavar="PATH",
                   help="Folder on the share both machines can see. Use a "
                        "UNC path - the two computers map the share to "
                        "different drive letters, so a Z: path written here "
                        "means nothing on the acquisition PC.")
    p.add_argument("--poll", type=float, default=5.0, metavar="SECONDS",
                   help="How often to look for new requests (default 5)")
    p.add_argument("--once", action="store_true",
                   help="Handle whatever is waiting, then exit (for testing)")
    p.add_argument("--config", default=None,
                   help="JSON file of config overrides")
    args = p.parse_args(argv)

    cfg = load_config(args.config)
    return Agent(Path(args.control_root), cfg, poll_s=args.poll).run(once=args.once)


if __name__ == "__main__":
    raise SystemExit(main())
