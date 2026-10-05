"""
Real-time stage/calibration ERROR watcher for the Zeiss mSEM service logs.

Tails the same rolling log4j XML logs the beam-arc monitor watches
(beam_arc_monitor.py), on the microscope control PC (or a mapped/UNC path to
it):

    <log_dir>\\mSEMService.log.xml / .log.<N>.xml   -- hardware-level events;
        stage/calibration failures show up as level="ERROR" lines like
        "Failed to get stage position." or "Hardware process Configure_Stage
        stopped with status 1".
    <log_dir>\\ErrorHandler.log.xml / .log.<N>.xml  -- the actual user-facing
        error-dialog text, logged (at DEBUG!) as
        "create user message: <Category> | <code>: <text>".

Both are log4j:event streams with no single root element, same schema as
beam_arc_monitor.py, so they're parsed the same way here (kept as a small,
separate regex set rather than importing beam_arc_monitor's private helpers,
since that module is scoped to one specific failure signature).

Only ERROR-level mSEMService lines and ErrorHandler "create user message"
lines that mention "stage" or "calib" (case-insensitive) are surfaced -- WARN
is excluded because routine stage moves log constant "Failed to set variable
Stage_ZPosition_Monitor..." WARNs that aren't actual errors, and the log also
carries a lot of unrelated AutoFocus/vacuum/Matlab noise.

CLI:
    python stage_calib_error_monitor.py --scan "X:\\ProgramData\\Carl Zeiss\\mSEM"
    python stage_calib_error_monitor.py --watch "X:\\ProgramData\\Carl Zeiss\\mSEM"
"""
from __future__ import annotations

import glob
import os
import re
import threading
import time
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Tuple

_EVENT_RE = re.compile(r"<log4j:event\b(?P<attrs>[^>]*)>(?P<body>.*?)</log4j:event>", re.DOTALL)
_TS_RE = re.compile(r'timestamp="(\d+)"')
_LVL_RE = re.compile(r'level="([A-Z]+)"')
_MSG_RE = re.compile(r"<log4j:message>(?P<msg>.*?)</log4j:message>", re.DOTALL)

DEFAULT_LOG_DIR = r"C:\ProgramData\Carl Zeiss\mSEM"
KEYWORDS: Tuple[str, ...] = ("stage", "calib")

# base_name -> whether only "create user message" lines count (ErrorHandler),
# or the normal ERROR/WARN level filter applies (mSEMService).
SOURCES: Tuple[str, ...] = ("mSEMService", "ErrorHandler")


@dataclass
class StageErrorEvent:
    """A detected stage/calibration-related ERROR (or user-facing error dialog)."""
    ts_ms: int
    source: str       # "mSEMService" or "ErrorHandler"
    level: str
    message: str

    @property
    def when(self) -> str:
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(self.ts_ms / 1000.0))

    def describe(self) -> str:
        return f"[{self.when}] {self.source} {self.level}: {self.message}"


def _matches(source: str, level: str, message: str) -> bool:
    low = message.lower()
    if source == "ErrorHandler":
        if "create user message" not in low:
            return False
    else:
        # ERROR only: "WARN ... Failed to set variable Stage_ZPosition_Monitor
        # to value ..." fires constantly during ordinary stage moves and would
        # otherwise flood the console with non-errors.
        if level != "ERROR":
            return False
    return any(k in low for k in KEYWORDS)


def _iter_events(text: str):
    """Yield (ts_ms, level, message) for every complete <log4j:event> in *text*."""
    for m in _EVENT_RE.finditer(text):
        ts_m = _TS_RE.search(m.group("attrs"))
        lvl_m = _LVL_RE.search(m.group("attrs"))
        if not ts_m or not lvl_m:
            continue
        msg_m = _MSG_RE.search(m.group("body"))
        yield int(ts_m.group(1)), lvl_m.group(1), (msg_m.group("msg") if msg_m else "")


def _find_active_log(log_dir: str, base_name: str) -> Optional[str]:
    """Return the newest (by mtime) rotating log file for *base_name* in log_dir."""
    files = glob.glob(os.path.join(log_dir, f"{base_name}.log.*.xml"))
    base = os.path.join(log_dir, f"{base_name}.log.xml")
    if os.path.exists(base):
        files.append(base)
    return max(files, key=os.path.getmtime) if files else None


def scan_log_text(text: str, source: str) -> List[StageErrorEvent]:
    """Offline: find every matching event in a full log *text* from *source*."""
    found = []
    for ts, level, msg in _iter_events(text):
        if _matches(source, level, msg):
            found.append(StageErrorEvent(ts_ms=ts, source=source, level=level, message=msg))
    return found


def scan_log_file(path: str, source: str) -> List[StageErrorEvent]:
    """Offline: find every matching event in a log *file* (any encoding)."""
    with open(path, "rb") as f:
        text = f.read().decode("utf-8", errors="replace")
    return scan_log_text(text, source=source)



# A network share's cached file-size metadata can lag the true size by a
# couple of seconds (Windows SMB client-side attribute caching), which can
# make an already-existing (stale) tail of the file look "new" on the very
# first poll after picking an active log. Log timestamps are stamped by the
# microscope PC's own clock, which is not assumed to agree with the local
# machine's clock, so events are converted to "local-equivalent" time via a
# one-time clock-offset estimate (local now - remote mtime) before being
# compared against when this tail actually started watching. STALE_GRACE_MS
# absorbs the residual slop in that offset estimate.
STALE_GRACE_MS = 5_000


class _SourceTail:
    """Tails one rotating log source (mSEMService or ErrorHandler)."""

    def __init__(self, log_dir: str, base_name: str, from_start: bool):
        self.log_dir = log_dir
        self.base_name = base_name
        self.from_start = from_start
        self._path: Optional[str] = None
        self._pos = 0
        self._buf = ""
        self._run_start_ms = int(time.time() * 1000)
        self._clock_offset_ms: Optional[int] = None   # local_now_ms - remote_mtime_ms, set on first active file

    def poll(self) -> List[Tuple[int, str, str]]:
        """Return newly-appeared (ts_ms, level, message) events since the last poll."""
        path = _find_active_log(self.log_dir, self.base_name)
        if path and path != self._path:
            self._path = path
            self._pos = 0 if self.from_start else (os.path.getsize(path) if os.path.exists(path) else 0)
            self._buf = ""
            try:
                self._clock_offset_ms = int(time.time() * 1000) - int(os.path.getmtime(path) * 1000)
            except Exception:
                self._clock_offset_ms = 0
        if not self._path or not os.path.exists(self._path):
            return []

        size = os.path.getsize(self._path)
        if size < self._pos:                # truncated / rotated in place
            self._pos, self._buf = 0, ""
        if size <= self._pos:
            return []

        with open(self._path, "rb") as f:
            f.seek(self._pos)
            data = f.read()
            self._pos = f.tell()
        self._buf += data.decode("utf-8", errors="replace")

        out: List[Tuple[int, str, str]] = []
        last_end = 0
        for m in _EVENT_RE.finditer(self._buf):
            last_end = m.end()
            ts_m = _TS_RE.search(m.group("attrs"))
            lvl_m = _LVL_RE.search(m.group("attrs"))
            if not ts_m or not lvl_m:
                continue
            msg_m = _MSG_RE.search(m.group("body"))
            ts_ms = int(ts_m.group(1))
            # Reject anything that (once translated to local-equivalent time)
            # predates this tail's start -- a stale cached file size at
            # startup can otherwise surface pre-existing log content as if it
            # just happened.
            if not self.from_start and self._clock_offset_ms is not None:
                local_equivalent_ms = ts_ms + self._clock_offset_ms
                if local_equivalent_ms < self._run_start_ms - STALE_GRACE_MS:
                    continue
            out.append((ts_ms, lvl_m.group(1), msg_m.group("msg") if msg_m else ""))
        if last_end:
            self._buf = self._buf[last_end:]        # keep the partial tail
        if len(self._buf) > 5_000_000:               # guard: never grow unbounded
            self._buf = self._buf[-1_000_000:]
        return out


class StageCalibErrorMonitor(threading.Thread):
    """
    Background thread that tails mSEMService.log*.xml + ErrorHandler.log*.xml
    in *log_dir* and calls ``on_error(StageErrorEvent)`` the moment a
    stage/calibration-related ERROR (or user-facing error dialog) appears.

    Robust to log rotation and to a file being truncated/replaced. Start
    ``from_start=True`` to also scan existing contents (useful for testing);
    default only watches new lines from the moment it starts.
    """

    def __init__(
        self,
        on_error: Callable[[StageErrorEvent], None],
        log_dir: str = DEFAULT_LOG_DIR,
        poll_interval: float = 1.0,
        from_start: bool = False,
    ) -> None:
        super().__init__(daemon=True, name="StageCalibErrorMonitor")
        self.on_error = on_error
        self.poll_interval = poll_interval
        # NOT named self._stop -- threading.Thread has an internal _stop()
        # method, and shadowing it with an Event breaks Thread.join().
        self._stop_evt = threading.Event()
        self._tails: Dict[str, _SourceTail] = {
            src: _SourceTail(log_dir, src, from_start) for src in SOURCES
        }

    def stop(self) -> None:
        self._stop_evt.set()

    def run(self) -> None:
        while not self._stop_evt.is_set():
            for source, tail in self._tails.items():
                try:
                    for ts_ms, level, message in tail.poll():
                        if _matches(source, level, message):
                            self.on_error(StageErrorEvent(ts_ms=ts_ms, source=source,
                                                          level=level, message=message))
                except Exception:
                    pass                              # a transient FS/share hiccup must not kill the monitor
            self._stop_evt.wait(self.poll_interval)


def _cli() -> None:
    import argparse

    ap = argparse.ArgumentParser(description="Detect Zeiss mSEM stage/calibration ERROR events.")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--scan", metavar="LOGDIR", help="offline: scan the newest logs in LOGDIR and print matches")
    g.add_argument("--watch", metavar="LOGDIR", nargs="?", const=DEFAULT_LOG_DIR,
                   help="live: tail the newest logs in LOGDIR")
    args = ap.parse_args()

    if args.scan:
        found: List[StageErrorEvent] = []
        for src in SOURCES:
            path = _find_active_log(args.scan, src)
            if path:
                found += scan_log_file(path, source=src)
        found.sort(key=lambda e: e.ts_ms)
        print(f"{len(found)} stage/calibration error(s):")
        for e in found:
            print("  " + e.describe())
        return

    print(f"Watching {args.watch} for stage/calibration errors (Ctrl-C to stop)...")
    mon = StageCalibErrorMonitor(on_error=lambda e: print(e.describe(), flush=True), log_dir=args.watch)
    mon.start()
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        mon.stop()


if __name__ == "__main__":
    _cli()
