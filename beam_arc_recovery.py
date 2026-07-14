"""
Beam-arc recovery for the mSEM acquisition loop.

After ``beam_arc_monitor`` detects a beam-clearance arc, the HV outputs are off
and acquisition must stop, be recovered, and resume from the last completed tile.
Recovery on the microscope is the ZEN **"Chamber reset EHT"** action followed by
beam-on.

IMPORTANT: "Chamber reset EHT" is a ZEN UI action; it is **not** a single API
command (checked both API PDFs). Two recovery paths are provided:

  * ``wait_for_beam_restored`` — TIER 1 (guided, safe today): the app pauses and
    the operator runs Chamber reset EHT + beam-on in ZEN; this polls until the
    beam is back on, then acquisition resumes. No HV commands are issued by us.

  * ``recover_from_arc`` — TIER 2 (automated, **VALIDATE ON-SCOPE FIRST**):
    reproduces the reset from API building blocks (ramp HV down → wait for beam
    clearance → beam on → re-align). Only enable after confirming on the
    microscope that this sequence is equivalent to "Chamber reset EHT".

Both take the same ``conn`` object the rest of the codebase uses
(``conn.execute("CMD_...")`` -> rc, ``conn.get_double("DP_...")`` -> float),
so they slot straight into ``focus_map.py`` / ``auto_stage_calib.py``.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable, Optional

# API tokens (from 20200821_MultiSEM_API_17_V2.pdf)
CMD_BEAM_OFF = "CMD_BEAM_OFF"
CMD_WAIT_BEAM_OFF = "CMD_WAIT_BEAM_OFF"
CMD_BEAM_ON = "CMD_BEAM_ON"
CMD_WAIT_BEAM_ON = "CMD_WAIT_BEAM_ON"
CMD_OLANDSTAGEHV_OFF = "CMD_OLANDSTAGEHV_OFF"
CMD_WAIT_OLANDSTAGEHV_OFF = "CMD_WAIT_OLANDSTAGEHV_OFF"
CMD_BEAMS_TO_FIBERS = "CMD_BEAMS_TO_FIBERS"
DP_IS_BEAM_ON = "DP_IS_BEAM_ON"        # 1 on, 0 off, 0.5 in-progress
DP_VACSTATUS = "DP_VACSTATUS"          # 1 = beam clearance OK, 0 = no clearance


def _log(cb: Optional[Callable[[str], None]], msg: str) -> None:
    (cb or print)(f"[BeamArcRecovery] {msg}")


def _get(conn, var: str, default: float = 0.0) -> float:
    try:
        return float(conn.get_double(var))
    except Exception:
        return default


@dataclass
class RecoveryResult:
    ok: bool
    detail: str


def wait_for_beam_restored(conn, timeout_s: float = 900.0, poll_s: float = 2.0,
                           log: Optional[Callable[[str], None]] = None,
                           should_abort: Optional[Callable[[], bool]] = None) -> RecoveryResult:
    """TIER 1 — guided. Block until the operator has run Chamber reset EHT and the
    beam is back on (``DP_IS_BEAM_ON`` == 1) with vacuum clearance restored.

    Returns as soon as the beam is confirmed on, or fails on timeout/abort. Issues
    no HV commands — the operator performs the physical reset in ZEN.
    """
    _log(log, "waiting for operator to run 'Chamber reset EHT' and turn the beam on...")
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if should_abort and should_abort():
            return RecoveryResult(False, "aborted by user")
        if _get(conn, DP_IS_BEAM_ON) >= 0.99 and _get(conn, DP_VACSTATUS, 1.0) >= 0.99:
            _log(log, "beam is back ON with clearance — resuming.")
            return RecoveryResult(True, "beam restored by operator")
        time.sleep(poll_s)
    return RecoveryResult(False, f"timed out after {timeout_s:.0f}s waiting for beam-on")


def recover_from_arc(conn, clearance_timeout_s: float = 240.0, poll_s: float = 2.0,
                     log: Optional[Callable[[str], None]] = None) -> RecoveryResult:
    """TIER 2 — automated (VALIDATE ON-SCOPE FIRST). Reproduce 'Chamber reset EHT'
    from API building blocks:

        1. ramp HV / beams down cleanly     (CMD_BEAM_OFF, CMD_OLANDSTAGEHV_OFF)
        2. wait for beam clearance          (poll DP_VACSTATUS == 1)
        3. turn the beam back on            (CMD_BEAM_ON, verify DP_IS_BEAM_ON)
        4. re-align beams to fibers         (CMD_BEAMS_TO_FIBERS)

    Do NOT enable unattended until confirmed equivalent to the ZEN reset.
    """
    # 1. down to a known-off state
    _log(log, "ramping beam + OL/stage HV down...")
    conn.execute(CMD_BEAM_OFF)
    conn.execute(CMD_WAIT_BEAM_OFF)
    conn.execute(CMD_OLANDSTAGEHV_OFF)
    conn.execute(CMD_WAIT_OLANDSTAGEHV_OFF)

    # 2. wait for the vacuum/beam-clearance interlock to clear
    _log(log, "waiting for beam clearance (DP_VACSTATUS)...")
    deadline = time.time() + clearance_timeout_s
    while _get(conn, DP_VACSTATUS) < 0.99:
        if time.time() > deadline:
            return RecoveryResult(False, f"no beam clearance after {clearance_timeout_s:.0f}s")
        time.sleep(poll_s)

    # 3. beam back on
    _log(log, "clearance OK — turning beam on...")
    conn.execute(CMD_BEAM_ON)
    conn.execute(CMD_WAIT_BEAM_ON)
    if _get(conn, DP_IS_BEAM_ON) < 0.99:
        return RecoveryResult(False, "beam is still OFF after CMD_BEAM_ON")

    # 4. re-align (beam-on can shift beam positions on the scintillator)
    _log(log, "re-aligning beams to fibers...")
    conn.execute(CMD_BEAMS_TO_FIBERS)

    _log(log, "recovery complete — beam on, clearance OK.")
    return RecoveryResult(True, "auto-recovered (HV down -> clearance -> beam on -> align)")
