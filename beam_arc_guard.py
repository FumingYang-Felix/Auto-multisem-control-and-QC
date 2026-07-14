"""
Beam-arc guard: ties the log monitor + recovery into one object the acquisition
loop can poll between tiles.

Usage (in focus_map.py's tile loop):

    guard = BeamArcGuard(log_dir=..., recovery_mode="guided")
    guard.start()
    ...
    for tile in route:
        guard.handle_if_arc(conn)   # blocks here if an arc happened, then returns
        acquire(tile)

On a detected arc it prints a ``[BeamArc] ...`` line (picked up by the GUI's log
panel / pipeline_runner) and, depending on ``recovery_mode``:

    "guided"    -> pause until the operator runs Chamber reset EHT + beam-on
                   (default; safe today, no HV commands issued by us)
    "auto"      -> run the API recovery sequence (VALIDATE ON-SCOPE FIRST)
    "flag_only" -> report only; do NOT pause acquisition
"""
from __future__ import annotations

import threading
from typing import Optional

from beam_arc_monitor import ArcEvent, BeamArcMonitor


def _emit(msg: str) -> None:
    print(f"[BeamArc] {msg}", flush=True)


class BeamArcGuard:
    def __init__(
        self,
        log_dir: str,
        recovery_mode: str = "guided",
        min_outputs: int = 2,
        window_ms: int = 2000,
    ) -> None:
        self.log_dir = log_dir
        self.recovery_mode = recovery_mode
        self._pending: Optional[ArcEvent] = None
        self._lock = threading.Lock()
        self.n_arcs = 0
        self._monitor = BeamArcMonitor(
            on_arc=self._on_arc,
            log_dir=log_dir,
            min_outputs=min_outputs,
            window_ms=window_ms,
        )

    # -- monitor callback (runs on the monitor thread) --
    def _on_arc(self, evt: ArcEvent) -> None:
        with self._lock:
            self._pending = evt          # newest wins; coalesced by the detector's cooldown

    def start(self) -> None:
        self._monitor.start()

    def stop(self) -> None:
        self._monitor.stop()

    def _take(self) -> Optional[ArcEvent]:
        with self._lock:
            evt, self._pending = self._pending, None
            return evt

    def handle_if_arc(self, conn) -> bool:
        """If an arc was flagged, report + recover (per recovery_mode). Blocks for
        guided/auto recovery. Returns True if an arc was handled this call."""
        evt = self._take()
        if evt is None:
            return False
        self.n_arcs += 1
        _emit(evt.describe())

        if self.recovery_mode == "flag_only":
            _emit("recovery_mode=flag_only — NOT pausing acquisition.")
            return True

        # import recovery lazily so detection works even if the recovery module
        # or the COM connection is unavailable (e.g. offline testing)
        try:
            from beam_arc_recovery import recover_from_arc, wait_for_beam_restored
        except Exception as e:
            _emit(f"recovery module unavailable ({e}); pausing is a no-op.")
            return True

        if self.recovery_mode == "auto":
            res = recover_from_arc(conn, log=_emit)
        else:  # "guided" (default)
            res = wait_for_beam_restored(conn, log=_emit)

        _emit(f"recovery {'OK' if res.ok else 'FAILED'} — {res.detail}. Resuming from current tile.")
        return True
