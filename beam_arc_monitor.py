"""
Real-time beam-arc detection from the Zeiss mSEM service log.

The mSEM service writes rolling log4j XML files on the Windows control PC:

    C:\\ProgramData\\CarlZeiss\\mSEM\\mSEMService.log.<N>.xml      (N = 1, 2, 3, ...)

A **beam arc** ("beam clearance failure") trips the high-voltage outputs off. In
the log it shows up as a tight burst of ERROR events (the operator's ids
4431-4434), all within ~10 ms:

    Received 3 on variable Tip_HV_BE_OutputStatusDetails: Output off (beam clearance failure)
    Stage_HV_BE_OutputStatusDetails:  Output off (beam clearance failure)
    MO_HV_MOLens_OutputStatusDetails: Output off (beam clearance failure)
    OL_HV_Extr_OutputStatusDetails:   Output off (beam clearance failure)

The recovery is the ZEN "Chamber reset EHT" action, then turn the beam back on
and resume imaging.

This module is **stdlib-only** (so it runs on the control PC) and is
**unit-testable off-hardware** via ``scan_log_file()`` against a saved log.

Discriminator note: the log contains many other ERRORs (AutoFocus / Matlab
failures). We match the literal ``beam clearance failure`` on the four HV
outputs only, so a focus miss never looks like an arc.

CLI:
    python beam_arc_monitor.py --scan docs/mSEMService.log.2.xml
    python beam_arc_monitor.py --watch "C:\\ProgramData\\CarlZeiss\\mSEM"
"""
from __future__ import annotations

import glob
import os
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, List, Optional, Tuple

# The four high-voltage outputs that trip together on a beam-clearance arc.
HV_OUTPUTS: Tuple[str, ...] = ("Tip_HV_BE", "Stage_HV_BE", "MO_HV_MOLens", "OL_HV_Extr")

# One clearance-failure line from any of the four HV outputs.
_CLEARANCE_RE = re.compile(
    r"(?P<output>Tip_HV_BE|Stage_HV_BE|MO_HV_MOLens|OL_HV_Extr)"
    r"_OutputStatusDetails:\s*Output off \(beam clearance failure\)"
)

# A complete <log4j:event ...>...</log4j:event> element (the file is a stream of
# these with no single root, so we can't use a real XML parser on the whole thing).
_EVENT_RE = re.compile(r"<log4j:event\b(?P<attrs>[^>]*)>(?P<body>.*?)</log4j:event>", re.DOTALL)
_TS_RE = re.compile(r'timestamp="(\d+)"')
_LVL_RE = re.compile(r'level="([A-Z]+)"')
_MSG_RE = re.compile(r"<log4j:message>(?P<msg>.*?)</log4j:message>", re.DOTALL)

DEFAULT_LOG_DIR = r"C:\ProgramData\CarlZeiss\mSEM"


@dataclass
class ArcEvent:
    """A detected beam-arc episode."""
    ts_ms: int                       # epoch-ms of the first clearance failure in the burst
    outputs: Tuple[str, ...]         # which HV outputs reported a clearance failure
    n_events: int                    # number of clearance-failure ERRORs coalesced
    first_message: str               # the first raw message (for the log/UI)

    @property
    def when(self) -> str:
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(self.ts_ms / 1000.0))

    def describe(self) -> str:
        return (f"BEAM ARC @ {self.when}: HV outputs tripped off "
                f"({', '.join(self.outputs)}) — 'beam clearance failure'. "
                f"Action: pause, run Chamber reset EHT, beam on, resume.")


def _iter_events(text: str):
    """Yield (ts_ms, level, message) for every complete <log4j:event> in *text*."""
    for m in _EVENT_RE.finditer(text):
        ts_m = _TS_RE.search(m.group("attrs"))
        lvl_m = _LVL_RE.search(m.group("attrs"))
        if not ts_m or not lvl_m:
            continue
        msg_m = _MSG_RE.search(m.group("body"))
        yield int(ts_m.group(1)), lvl_m.group(1), (msg_m.group("msg") if msg_m else "")


class ArcDetector:
    """
    Feed log4j events; fires ``on_arc(ArcEvent)`` when a beam-clearance-failure
    burst is seen.

    High-confidence rule: >= ``min_outputs`` distinct HV outputs report a
    clearance failure within ``window_ms``. ``cooldown_ms`` prevents the same
    physical arc from firing more than once.
    """

    def __init__(
        self,
        on_arc: Optional[Callable[[ArcEvent], None]] = None,
        window_ms: int = 2000,
        min_outputs: int = 2,
        cooldown_ms: int = 5000,
    ) -> None:
        self.on_arc = on_arc
        self.window_ms = window_ms
        self.min_outputs = min_outputs
        self.cooldown_ms = cooldown_ms
        self._burst: List[Tuple[int, str, str]] = []   # (ts, output, message)
        self._last_fire_ts: Optional[int] = None

    def feed_event(self, ts_ms: int, level: str, message: str) -> Optional[ArcEvent]:
        if level != "ERROR":
            return None
        m = _CLEARANCE_RE.search(message)
        if not m:
            return None
        output = m.group("output")

        # keep only clearance failures within the sliding window of this event
        self._burst = [(t, o, msg) for (t, o, msg) in self._burst if ts_ms - t <= self.window_ms]
        self._burst.append((ts_ms, output, message))

        distinct = {o for _, o, _ in self._burst}
        if len(distinct) < self.min_outputs:
            return None

        # same physical arc already reported?  suppress during cooldown.
        if self._last_fire_ts is not None and ts_ms - self._last_fire_ts < self.cooldown_ms:
            return None

        first_ts, _, first_msg = min(self._burst, key=lambda e: e[0])
        evt = ArcEvent(
            ts_ms=first_ts,
            outputs=tuple(o for o in HV_OUTPUTS if o in distinct),
            n_events=len(self._burst),
            first_message=first_msg,
        )
        self._last_fire_ts = ts_ms
        self._burst = []
        if self.on_arc:
            self.on_arc(evt)
        return evt


def scan_log_text(text: str, **detector_kwargs) -> List[ArcEvent]:
    """Offline: detect every arc episode in a full log *text*."""
    found: List[ArcEvent] = []
    det = ArcDetector(on_arc=found.append, **detector_kwargs)
    for ts, level, msg in _iter_events(text):
        det.feed_event(ts, level, msg)
    return found


def scan_log_file(path: str, **detector_kwargs) -> List[ArcEvent]:
    """Offline: detect every arc episode in a log *file* (any encoding)."""
    with open(path, "rb") as f:
        text = f.read().decode("utf-8", errors="replace")
    return scan_log_text(text, **detector_kwargs)


def find_active_log(log_dir: str = DEFAULT_LOG_DIR) -> Optional[str]:
    """Return the mSEMService log currently being written (newest by mtime)."""
    files = glob.glob(os.path.join(log_dir, "mSEMService.log.*.xml"))
    base = os.path.join(log_dir, "mSEMService.log.xml")
    if os.path.exists(base):
        files.append(base)
    return max(files, key=os.path.getmtime) if files else None


class BeamArcMonitor(threading.Thread):
    """
    Background thread that tails the newest mSEMService log and calls
    ``on_arc(ArcEvent)`` the moment a beam arc is detected.

    Robust to log rotation (a new ``.N.xml`` appearing) and to the file being
    truncated/replaced.  Start ``from_start=True`` to also scan the existing
    contents (useful for testing); default only watches new lines.
    """

    def __init__(
        self,
        on_arc: Callable[[ArcEvent], None],
        log_dir: str = DEFAULT_LOG_DIR,
        poll_interval: float = 1.0,
        from_start: bool = False,
        **detector_kwargs,
    ) -> None:
        super().__init__(daemon=True, name="BeamArcMonitor")
        self.log_dir = log_dir
        self.poll_interval = poll_interval
        self.from_start = from_start
        self.detector = ArcDetector(on_arc=on_arc, **detector_kwargs)
        # NOT named self._stop -- threading.Thread has an internal _stop()
        # method, and shadowing it with an Event breaks Thread.join().
        self._stop_evt = threading.Event()
        self._path: Optional[str] = None
        self._pos = 0
        self._buf = ""

    def stop(self) -> None:
        self._stop_evt.set()

    def _select_active(self) -> Optional[str]:
        path = find_active_log(self.log_dir)
        if path and path != self._path:
            self._path = path
            self._pos = 0 if self.from_start else (os.path.getsize(path) if os.path.exists(path) else 0)
            self._buf = ""
        return self._path

    def _read_new(self) -> None:
        if not self._path or not os.path.exists(self._path):
            return
        size = os.path.getsize(self._path)
        if size < self._pos:          # truncated / rotated in place
            self._pos, self._buf = 0, ""
        if size <= self._pos:
            return
        with open(self._path, "rb") as f:
            f.seek(self._pos)
            data = f.read()
            self._pos = f.tell()
        self._buf += data.decode("utf-8", errors="replace")

        last_end = 0
        for m in _EVENT_RE.finditer(self._buf):
            last_end = m.end()
            ts_m = _TS_RE.search(m.group("attrs"))
            lvl_m = _LVL_RE.search(m.group("attrs"))
            if not ts_m or not lvl_m:
                continue
            msg_m = _MSG_RE.search(m.group("body"))
            self.detector.feed_event(int(ts_m.group(1)), lvl_m.group(1),
                                     msg_m.group("msg") if msg_m else "")
        if last_end:
            self._buf = self._buf[last_end:]           # keep the partial tail
        if len(self._buf) > 5_000_000:                 # guard: never grow unbounded
            self._buf = self._buf[-1_000_000:]

    def run(self) -> None:
        while not self._stop_evt.is_set():
            try:
                self._select_active()
                self._read_new()
            except Exception:
                pass                                    # a transient FS error must not kill the monitor
            self._stop_evt.wait(self.poll_interval)


def _cli() -> None:
    import argparse

    ap = argparse.ArgumentParser(description="Detect Zeiss mSEM beam-arc (beam clearance failure) events.")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--scan", metavar="LOGFILE", help="offline: scan one log file and print arcs")
    g.add_argument("--watch", metavar="LOGDIR", nargs="?", const=DEFAULT_LOG_DIR,
                   help="live: tail the newest mSEMService log in LOGDIR")
    ap.add_argument("--min-outputs", type=int, default=2)
    ap.add_argument("--window-ms", type=int, default=2000)
    args = ap.parse_args()

    if args.scan:
        arcs = scan_log_file(args.scan, min_outputs=args.min_outputs, window_ms=args.window_ms)
        print(f"{len(arcs)} beam-arc episode(s) in {args.scan}:")
        for a in arcs:
            print("  " + a.describe())
        return

    print(f"Watching {args.watch} for beam arcs (Ctrl-C to stop)...")
    mon = BeamArcMonitor(on_arc=lambda a: print("\a" + a.describe(), flush=True),
                         log_dir=args.watch, min_outputs=args.min_outputs, window_ms=args.window_ms)
    mon.start()
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        mon.stop()


if __name__ == "__main__":
    _cli()
