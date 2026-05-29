#!/usr/bin/env python3
"""
One-click environment setup for the MultiSEM GUI + acquisition pipeline.

By default it installs the dependencies into the CURRENT Python interpreter —
the one running this script. On the control PC, run it with the SAME Python
that already runs focus_map.py + the Zeiss COM, so the GUI shares that
environment. Pass --venv to create + use an isolated .venv instead.

    python setup_env.py            # install into the current interpreter
    python setup_env.py --venv     # create ./.venv and install into it
    python setup_env.py --dry-run  # print what it would do, install nothing

On Windows it also installs comtypes (requirements-windows.txt) and prints the
COM-registration reminder needed for real (non-simulation) acquisition.
"""
from __future__ import annotations

import platform
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
IS_WINDOWS = platform.system() == "Windows"
DRY = "--dry-run" in sys.argv
USE_VENV = "--venv" in sys.argv


def run(cmd):
    print(">>", " ".join(str(c) for c in cmd))
    if not DRY:
        subprocess.check_call(cmd)


def main() -> int:
    vi = sys.version_info
    print(f"== MultiSEM env setup ==  Python {vi.major}.{vi.minor}.{vi.micro} on {platform.system()}")
    if vi < (3, 9):
        print("WARNING: PySide6 >= 6.5 generally needs Python 3.9+. If install fails,")
        print("         either use a newer Python or pin an older wheel "
              "(e.g. `pip install \"PySide6<6.8\"`).")

    py = sys.executable
    if USE_VENV:
        venv_dir = ROOT / ".venv"
        if not venv_dir.exists():
            run([sys.executable, "-m", "venv", str(venv_dir)])
        py = str(venv_dir / ("Scripts/python.exe" if IS_WINDOWS else "bin/python"))
        print(f"Using venv interpreter: {py}")

    req = ROOT / ("requirements-windows.txt" if IS_WINDOWS else "requirements.txt")
    run([py, "-m", "pip", "install", "--upgrade", "pip"])
    run([py, "-m", "pip", "install", "-r", str(req)])

    print("\n=== Done" + (" (dry run — nothing installed)" if DRY else "") + ". ===")
    print("Launch the GUI:")
    if USE_VENV:
        act = r".venv\Scripts\activate" if IS_WINDOWS else "source .venv/bin/activate"
        print(f"  {act}")
    print(f'  "{py}" -m msem_control.main')
    if IS_WINDOWS:
        print("\n--- Windows: real acquisition checklist ---")
        print("  1. Register the Zeiss COM object (once, as admin):")
        print(r'       regasm ZeissmSEMWrapper.dll /tlb:ZeissmSEMWrapper.tlb /codebase')
        print("  2. mSEMService must be running and reachable.")
        print("  3. In msem_control/main.py set WorkflowEngine(simulation_mode=False).")
        print("  4. Keep focus_map.py / main_live_qc.py / live_qc_support.py /")
        print("     qc_algorithm.py + the msem/ package in this same folder.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
