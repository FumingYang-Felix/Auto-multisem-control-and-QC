"""Small widgets matching the acquisition app's conventions.

Re-implemented rather than imported from msem_control/ui/widgets.py, for the
same reason theme.py copies the stylesheet: that file belongs to the other
clone. These are short enough that duplicating them costs less than the
dependency would - LogPanel in particular is a QGroupBox with a capped
QTextEdit and one HTML-formatting method.

Kept deliberately faithful to the originals:
  - LogPanel is titled "System Logs", caps the document at 5000 blocks (an
    unbounded log is a real source of UI lag over a long run), and colours
    each line as [time] [source] message with the same palette.
  - StatusIndicator is a "Label: value" pair whose value switches between the
    StatusValue / ErrorValue object names so the stylesheet colours it.
  - group() / file_row() mirror HiMcPage._group() / _file_row(): a QGroupBox
    with a right-aligned QFormLayout, and a QLineEdit followed by "Browse…".
"""
from __future__ import annotations

from datetime import datetime
from typing import Callable, Optional, Tuple

from PySide6.QtCore import QEvent, QObject, Qt
from PySide6.QtWidgets import (
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from .theme import ACCENT_LIGHT, AMBER, FG, ORANGE, RED


class LogPanel(QGroupBox):
    """The acquisition app's "System Logs" panel."""

    def __init__(self, title: str = "System Logs") -> None:
        super().__init__(title)
        layout = QVBoxLayout(self)
        self.text_edit = QTextEdit()
        self.text_edit.setReadOnly(True)
        self.text_edit.document().setMaximumBlockCount(5000)
        layout.addWidget(self.text_edit)

    def append_log(self, source: str, level: str, message: str) -> None:
        timestamp = datetime.now().strftime("%H:%M:%S")
        color = FG
        if level == "WARNING":
            color = AMBER
        elif level == "ERROR":
            color = RED
        safe = (
            str(message)
            .replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
        )
        self.text_edit.append(
            f'<span style="color:{ACCENT_LIGHT}">[{timestamp}]</span> '
            f'<span style="color:{ORANGE}">[{source}]</span> '
            f'<span style="color:{color}">{safe}</span>'
        )

    def clear_log(self) -> None:
        self.text_edit.clear()


class StatusIndicator(QWidget):
    """A "Label: value" row for the left status dock."""

    def __init__(self, label: str, initial: str = "—") -> None:
        super().__init__()
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.name_label = QLabel(f"{label}:")
        self.value_label = QLabel(initial)
        self.value_label.setObjectName("StatusValue")
        layout.addWidget(self.name_label)
        layout.addWidget(self.value_label)
        layout.addStretch()

    def set_value(self, value: object, state: str = "ok") -> None:
        """state: "ok" (green) / "warn" (amber) / "error" (red)."""
        self.value_label.setText(str(value))
        self.value_label.setObjectName(
            {"error": "ErrorValue", "warn": "WarnValue"}.get(state, "StatusValue")
        )
        # Qt caches the resolved style per object name; without this the new
        # name is set but the old colour keeps being painted.
        self.value_label.style().unpolish(self.value_label)
        self.value_label.style().polish(self.value_label)


class _NoWheelFilter(QObject):
    """Swallows mouse-wheel events.

    Qt's default behaviour is that scrolling the wheel while the cursor
    happens to be over a QSpinBox or QComboBox changes its value. On a form
    of parameters that is a standing trap: passing the mouse across the
    window on the way somewhere else silently alters a setting that then
    affects a run, with nothing on screen drawing attention to it.

    The event is consumed rather than forwarded. Nothing behind these
    widgets scrolls (the settings form is not inside a scroll area), so
    consuming costs nothing and guarantees the value cannot change.
    """

    def eventFilter(self, obj: QObject, event: QEvent) -> bool:
        if event.type() == QEvent.Type.Wheel:
            return True
        return super().eventFilter(obj, event)


# One shared instance, kept alive by this module reference. An event filter
# must outlive the widgets it is installed on.
_NO_WHEEL = _NoWheelFilter()


def no_wheel(widget: QWidget) -> QWidget:
    """Make a spin box / combo box ignore the scroll wheel. Returns it, so it
    can wrap a constructor inline."""
    widget.installEventFilter(_NO_WHEEL)
    return widget


def group(title: str) -> Tuple[QGroupBox, QFormLayout]:
    """QGroupBox + right-aligned QFormLayout, as HiMcPage._group() builds."""
    box = QGroupBox(title)
    form = QFormLayout(box)
    form.setLabelAlignment(Qt.AlignRight)
    form.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)
    return box, form


def file_row(
    edit: QLineEdit,
    handler: Callable[[], None],
    extra: Optional[QPushButton] = None,
) -> QWidget:
    """A path field followed by "Browse…" (and optionally one more button)."""
    row = QWidget()
    h = QHBoxLayout(row)
    h.setContentsMargins(0, 0, 0, 0)
    h.addWidget(edit, stretch=1)
    browse = QPushButton("Browse…")
    browse.clicked.connect(handler)
    h.addWidget(browse)
    if extra is not None:
        h.addWidget(extra)
    return row
