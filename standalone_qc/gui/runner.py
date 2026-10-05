"""Drives `python -m standalone_qc` as a subprocess and turns its stdout into
Qt signals.

Why a subprocess rather than calling score_region() in this process:

  - Scoring saturates a thread pool on image decode. In the GUI process that
    would fight the Qt event loop, and the window would stop repainting for
    the duration - the exact "the app is frozen" experience this is meant to
    avoid. A QThread would work too, but see the next two points.
  - Stop becomes trivial and reliable: kill the process. Interrupting a
    ThreadPoolExecutor mid-flight is not.
  - The GUI then exercises the SAME code path an operator gets from the
    command line, so the two can never drift apart in behaviour. This is also
    the pattern msem_control/engine/pipeline_runner.py already uses to drive
    the acquisition pipeline, so it's the established shape in this codebase.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

from PySide6.QtCore import QObject, QProcess, Signal

from .. import REPO_ROOT

# `    [ 43.0%] 96/223  probe tile 94`
_RE_PROGRESS = re.compile(r"^\s*\[\s*([\d.]+)%\]\s+(\d+)/(\d+)\s+(.*)$")
# `[StandaloneQC] === w10h31_.../S2R3 ===`
_RE_SECTION = re.compile(r"^\[StandaloneQC\]\s+===\s+(.+?)\s+===\s*$")
# `    mfovs=223 blank=0 scored=3 images_read=496 median_q=19.33 in 12.2s`
_RE_RESULT = re.compile(
    r"^\s*mfovs=(\d+)\s+blank=(\d+)\s+analyzed=(\d+)\s+images_read=(\d+)\s+"
    r"median_q=(\S+)\s+in\s+([\d.]+)s"
)
_RE_OUTLIERS = re.compile(r"^\s*OUTLIER BEAM\(S\):\s*(.+)$")
_RE_REPORT = re.compile(r"^\s*report:\s*(.+)$")
_RE_WARN = re.compile(r"^\s*!\s*(.+)$")
# `[Phase] placing mFoVs into the section overview — reads every beam ...`
# and `[Phase] <name> done in 12.3s`
_RE_PHASE = re.compile(r"^\[Phase\]\s+(.+?)(?:\s+[—-]\s+(.*))?$")


class QCRunner(QObject):
    """One scoring subprocess at a time."""

    line = Signal(str)                 # every stdout line, for the log pane
    progress = Signal(int, int, str)   # done, total, label
    section_started = Signal(str)      # "<sample>/<section>"
    section_result = Signal(dict)      # parsed summary of one finished section
    warning = Signal(str)              # a `!` line from the CLI
    phase = Signal(str, str)           # phase name, detail ('' if none)
    finished = Signal(int)             # exit code (-1 if it failed to launch)

    def __init__(self, parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self._proc: Optional[QProcess] = None
        self._buf = ""
        self._current = ""
        self._pending: Dict[str, Any] = {}

    # -- lifecycle --------------------------------------------------------
    def is_running(self) -> bool:
        return self._proc is not None and self._proc.state() != QProcess.NotRunning

    def start(self, args: List[str], module: str = "standalone_qc") -> None:
        """Launch `python -m <module> <args>`.

        `module` so the same driver can run the agent
        (standalone_qc.agent) as well as a scoring run: both stream plain
        text we want in the log pane, and both must die with the window
        rather than outliving it.
        """
        if self.is_running():
            raise RuntimeError("a scoring run is already in progress")

        proc = QProcess(self)
        proc.setProcessChannelMode(QProcess.MergedChannels)
        proc.setWorkingDirectory(str(REPO_ROOT))
        proc.readyReadStandardOutput.connect(self._drain)
        proc.finished.connect(self._on_finished)
        proc.errorOccurred.connect(self._on_error)

        # -u is not optional: piped stdout is block-buffered otherwise, so the
        # log pane would sit empty and then dump everything at the end.
        # sys.executable keeps us in the same venv that launched the GUI.
        argv = ["-u", "-m", module] + args
        self._proc = proc
        self._buf = ""
        self.line.emit(f"$ {Path(sys.executable).name} {' '.join(argv)}")
        proc.start(sys.executable, argv)

    def stop(self) -> None:
        """Ask the run to stop. Scoring is read-only - it only writes its own
        report at the end of a section - so being killed mid-section leaves
        nothing half-written, at worst an unscored section."""
        if not self.is_running() or self._proc is None:
            return
        self.line.emit("[GUI] stopping...")
        self._proc.terminate()
        if not self._proc.waitForFinished(3000):
            # terminate() posts WM_CLOSE on Windows, which a console child
            # like ours simply ignores, so this is the normal path here -
            # not an exceptional one. kill() is waited on too: returning
            # while the child is still alive made is_running() keep saying
            # True right after Stop.
            self._proc.kill()
            self._proc.waitForFinished(3000)

    # -- output parsing ---------------------------------------------------
    def _drain(self) -> None:
        if self._proc is None:
            return
        self._buf += bytes(self._proc.readAllStandardOutput()).decode(
            "utf-8", errors="replace"
        )
        while "\n" in self._buf:
            raw, self._buf = self._buf.split("\n", 1)
            self._handle(raw.rstrip("\r"))

    def _handle(self, text: str) -> None:
        self.line.emit(text)

        m = _RE_PROGRESS.match(text)
        if m:
            self.progress.emit(int(m.group(2)), int(m.group(3)), m.group(4).strip())
            return

        m = _RE_SECTION.match(text)
        if m:
            self._flush_pending()
            self._current = m.group(1)
            self._pending = {"key": self._current}
            self.section_started.emit(self._current)
            return

        m = _RE_RESULT.match(text)
        if m:
            self._pending.update({
                "key": self._current,
                "n_mfovs": int(m.group(1)),
                "n_blank": int(m.group(2)),
                "n_scored": int(m.group(3)),
                "images_read": int(m.group(4)),
                "median_q": m.group(5),
                "elapsed_s": float(m.group(6)),
            })
            return

        m = _RE_OUTLIERS.match(text)
        if m:
            self._pending["outlier_beams"] = m.group(1).strip()
            return

        m = _RE_REPORT.match(text)
        if m:
            self._pending["report"] = m.group(1).strip()
            return

        m = _RE_PHASE.match(text)
        if m:
            name = m.group(1).strip()
            # "<name> done in 12.3s" closes a phase; it is not a new one.
            if not name.endswith("s") or " done in " not in name:
                self.phase.emit(name, (m.group(2) or "").strip())
            return

        m = _RE_WARN.match(text)
        if m:
            self._pending.setdefault("warnings", []).append(m.group(1).strip())
            self.warning.emit(m.group(1).strip())

    def _flush_pending(self) -> None:
        # Only emit sections that actually produced a result line; a section
        # that was skipped for lack of data has nothing to report.
        if self._pending.get("n_mfovs") is not None:
            self.section_result.emit(dict(self._pending))
        self._pending = {}

    # -- process events ---------------------------------------------------
    def _on_finished(self, code: int, _status: Any) -> None:
        self._drain()
        self._flush_pending()
        self._proc = None
        self._current = ""
        self.finished.emit(int(code))

    def _on_error(self, err: Any) -> None:
        if err == QProcess.FailedToStart:
            self.line.emit(
                f"[GUI] could not start {sys.executable} - is this the right venv?"
            )
            self._proc = None
            self.finished.emit(-1)
