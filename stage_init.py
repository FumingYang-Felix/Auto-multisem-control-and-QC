"""
Standalone stage (re-)initialization: CMD_STAGE_INIT.

Zeiss MultiSEM API 4.1.44 CMD_STAGE_INIT:
  Re-initializes the stage: moves it to the lower limits, re-calibrates its
  coordinate system, then moves to (0, 0, 0). Return codes: 0 = success,
  -1 = failed to initialize stage / internal error, -2 = timeout while
  waiting for axes to initialize.

This is launched by the SFN page's "Calibrate Stage" button (as a subprocess,
same mechanism as auto_stage_calib.py) and is intended to be run standalone,
before the shuttle-and-find (3-marker) calibration.

CMD_STAGE_INIT occasionally fails for unrelated/transient reasons; the known
fix is simply to run it again, so this script retries once automatically
(MAX_ATTEMPTS = 2 total tries) before reporting failure.

Usage:
    python stage_init.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from msem import MSEMConnection, Stage

try:
    from alerts import send_alert
except Exception:
    def send_alert(subject: str, body: str) -> bool:  # type: ignore[misc]
        print(f"[Alerts] alerts module unavailable -- would have sent: {subject}")
        return False

MAX_ATTEMPTS = 2


def _run_stage_init(conn: MSEMConnection, attempt: int) -> int:
    """Execute CMD_STAGE_INIT once. Returns the raw API return code."""
    print(f"[StageInit] Executing CMD_STAGE_INIT (attempt {attempt}/{MAX_ATTEMPTS})...")
    rc = conn.execute("CMD_STAGE_INIT")
    is_final = attempt >= MAX_ATTEMPTS
    if rc == 0:
        print(f"[StageInit] CMD_STAGE_INIT succeeded on attempt {attempt}.")
    elif rc == -2:
        word = "timed out" if not is_final else "failed (timeout)"
        print(f"[StageInit] Attempt {attempt}/{MAX_ATTEMPTS}: CMD_STAGE_INIT {word} while waiting for axes to initialize (rc={rc}).")
    else:
        word = "did not succeed" if not is_final else "failed"
        print(f"[StageInit] Attempt {attempt}/{MAX_ATTEMPTS}: CMD_STAGE_INIT {word}, rc={rc}: {conn.get_last_error()}")
    return rc


def main() -> int:
    print("[StageInit] Connecting to MultiSEM...")
    conn = MSEMConnection()
    ret = conn.connect_and_initialize()
    if ret != 0:
        print(f"[StageInit] ERROR: connect_and_initialize failed rc={ret}: {conn.get_last_error()}")
        send_alert("Stage Init FAILED -- could not connect to scope",
                   f"stage_init.py: connect_and_initialize failed rc={ret}: {conn.get_last_error()}")
        return 1

    stage = Stage(conn)
    pos = stage.get_position()
    print(f"[StageInit] Stage position before init: X={pos.x:.3f} Y={pos.y:.3f} Z={pos.z}")

    # A previous run (e.g. Run Shuttle and Find Calibration, stopped mid-move)
    # can be force-killed by the GUI's Stop button with zero chance to leave
    # the stage settled -- CMD_STAGE_INIT can then fail because the
    # controller still considers itself mid-operation. Best-effort settle wait
    # before attempting re-init; harmless no-op if the stage was already idle.
    print("[StageInit] Waiting for stage to settle before re-init (in case a previous run was interrupted mid-move)...")
    try:
        conn.execute("CMD_WAIT_STAGE_SETTLED")
    except Exception as e:
        print(f"[StageInit] (settle wait raised {e}; continuing anyway)")

    rc = 0
    for attempt in range(1, MAX_ATTEMPTS + 1):
        rc = _run_stage_init(conn, attempt)
        if rc == 0:
            break
        if attempt < MAX_ATTEMPTS:
            print("[StageInit] Retrying CMD_STAGE_INIT (known transient failure mode)...")

    if rc != 0:
        err = f"CMD_STAGE_INIT failed after {MAX_ATTEMPTS} attempts, rc={rc}: {conn.get_last_error()}"
        print(f"[StageInit] ERROR: {err}")
        send_alert("Stage Init FAILED", f"stage_init.py: {err}")
        return 1

    pos_after = stage.get_position()
    print(f"[StageInit] Stage position after init: X={pos_after.x:.3f} Y={pos_after.y:.3f} Z={pos_after.z}")
    print("[StageInit] Done.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except Exception as e:
        try:
            send_alert("Stage Init CRASHED",
                       f"stage_init.py raised an unhandled exception:\n\n{e}\n\n"
                       "See the console/log output for the full traceback.")
        except Exception:
            pass
        raise
