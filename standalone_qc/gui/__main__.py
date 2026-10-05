"""`python -m standalone_qc.gui` - launch the standalone QC window."""
from __future__ import annotations

import sys


def main() -> int:
    from PySide6.QtWidgets import QApplication

    from .window import QCWindow

    app = QApplication(sys.argv)
    app.setApplicationName("MultiSEM Standalone QC")
    window = QCWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
