"""
Interactive OL-current (uA) <-> Z-equivalent (um) calibration logger.

Run this from PowerShell on the machine connected to MultiSEM:

    python calibrate_ua_um.py

For each sample: type in the current focus value in um (e.g. read off ZEN's
own live display - WD, Focus Offset, or whatever manual reference you're
using), press Enter, and the script reads AP_OLENS_CURRENT (the lens current
in uA, per the Zeiss API doc's own units for this register) plus the current
stage Z at that same moment. Each (um, uA, stage Z) triple is appended to a
CSV in D:\\Users\\Vlad\\Section _detection-files\\diagnostics so you can build
a uA-to-um conversion table across as many points as you want, across
multiple runs of this script (existing rows are never overwritten).

Type 'q' (or just hit Enter on an empty line) to stop.
"""
import csv
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from msem import MSEMConnection, Optics, Stage  # noqa: E402

OUT_DIR = r"D:\Users\Vlad\Section _detection-files\diagnostics"
OUT_PATH = os.path.join(OUT_DIR, "ua_um_calibration.csv")


def main():
    print("Connecting to MultiSEM...")
    conn = MSEMConnection()
    ret = conn.connect_and_initialize()
    print(f"Initialize returned: {ret}")
    if ret != 0:
        print(f"ERROR: {conn.get_last_error()}")
        return 1

    optics = Optics(conn)
    stage = Stage(conn)

    os.makedirs(OUT_DIR, exist_ok=True)
    write_header = not os.path.isfile(OUT_PATH)

    print(f"\nLogging to: {OUT_PATH}")
    print("For each sample: enter the current focus value in um, then Enter.")
    print("Type 'q' or an empty line to stop.\n")

    n_saved = 0
    with open(OUT_PATH, "a", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        if write_header:
            writer.writerow(["timestamp", "um_entered", "ol_current_uA", "stage_z_um"])

        while True:
            raw = input(f"[{n_saved} saved] Focus value (um), or 'q' to quit: ").strip()
            if raw == "" or raw.lower() in ("q", "quit", "exit"):
                break
            try:
                um_value = float(raw)
            except ValueError:
                print(f"  Could not parse {raw!r} as a number - try again.")
                continue

            ol_current = optics.get_ol_current()
            pos = stage.get_position()
            z_now = pos.z if pos and pos.z is not None else float("nan")
            ts = time.strftime("%Y-%m-%d %H:%M:%S")

            writer.writerow([ts, um_value, ol_current, z_now])
            f.flush()
            n_saved += 1
            print(f"  -> saved: um={um_value}, OL current={ol_current:.3f} uA, stage Z={z_now:.3f} um")

    print(f"\nDone. {n_saved} pair(s) saved this session. File: {OUT_PATH}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
