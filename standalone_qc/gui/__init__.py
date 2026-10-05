"""PySide6 desktop front end for standalone QC.

A SEPARATE window, deliberately not a tab in msem_control's main window:

  - This computer never talks to the microscope. A tab alongside Connect /
    Beam ON / stage controls would put hardware buttons in front of an
    operator on a machine that has no hardware to drive.
  - msem_control/ is acquisition-side code owned by the other clone. Adding a
    tab there means editing `main_window.py`, the highest-conflict surface
    between the two machines. Everything here stays in this subfolder.

The window does discovery in-process (reading small JSON marker files is
instant and gives structured data) but runs SCORING as a subprocess of the
same CLI an operator would type by hand - see gui/runner.py for why.

Launch:  python -m standalone_qc.gui
"""
from __future__ import annotations

__all__: list[str] = []
