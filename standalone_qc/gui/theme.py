"""The acquisition app's dark theme, replicated for the QC window.

Deliberately a COPY, not an import of msem_control's stylesheet. Two reasons:

  - msem_control/ is acquisition-side code owned by the other clone, and
    importing from it (even something as harmless as ui/widgets.py) would put
    a dependency on files we agreed not to rely on beyond their public API.
  - It is a stylesheet, not logic: nothing here can drift semantically. If the
    acquisition app restyles, this window keeping its current look is a
    cosmetic difference, not a bug.

Source: `MainWindow.apply_styles()` in msem_control/ui/main_window.py, merged
with msem_control/ui/style.qss (the QStatusBar / StatusValue / ErrorValue
rules live only in the .qss). Colour values are matched exactly so the two
windows look like the same application.

ADDED here, because the acquisition app has no table anywhere: QTableWidget /
QHeaderView rules. Without them Qt paints a table with its default light
palette, which on this dark background renders as a glaring white slab with
near-invisible header text.
"""
from __future__ import annotations

# VS Code-ish palette, same values the acquisition app uses.
BG = "#1e1e1e"
BG_RAISED = "#252526"
BG_PANEL = "#2d2d2d"
BG_FIELD = "#3c3c3c"
BORDER = "#3e3e3e"
FG = "#cccccc"
FG_DIM = "#666666"
ACCENT = "#007acc"
ACCENT_LIGHT = "#569cd6"
GREEN = "#4ec9b0"
AMBER = "#cca700"
RED = "#f44747"
ORANGE = "#ce9178"

STYLESHEET = f"""
QMainWindow {{ background-color: {BG}; }}
QWidget {{
    background-color: {BG};
    color: #e0e0e0;
    font-family: "Segoe UI", "Helvetica Neue", Arial, sans-serif;
    font-size: 13px;
}}

#TopBar {{
    background-color: {BG_RAISED};
    border-bottom: 1px solid {BORDER};
}}
#LogoLabel {{
    font-size: 16px;
    font-weight: bold;
    color: {ACCENT_LIGHT};
}}
#NavTab {{
    background-color: transparent;
    border: none;
    border-bottom: 2px solid transparent;
    color: #888888;
    padding: 0 20px;
    font-size: 14px;
    min-height: 40px;
}}
#NavTab:hover {{ color: {FG}; background-color: rgba(255,255,255,0.05); }}
#NavTab:checked {{
    color: #ffffff;
    background-color: rgba(0,122,204,0.15);
    border-bottom: 2px solid {ACCENT};
}}
#NavTab:checked:hover {{ background-color: rgba(0,122,204,0.25); }}

QDockWidget {{ color: {FG}; background-color: {BG}; }}
QDockWidget::title {{
    background-color: {BG_PANEL};
    padding: 8px;
    font-weight: bold;
    color: {FG};
}}
QDockWidget > QWidget {{ background-color: {BG}; }}

QGroupBox {{
    font-weight: bold;
    color: {ACCENT_LIGHT};
    border: 1px solid {BORDER};
    border-radius: 4px;
    margin-top: 10px;
    padding-top: 10px;
}}
QGroupBox::title {{
    subcontrol-origin: margin;
    left: 10px;
    padding: 0 5px;
}}

QPushButton {{
    background-color: #0e639c;
    color: white;
    border: none;
    padding: 6px 12px;
    border-radius: 4px;
    min-height: 24px;
}}
/* Qt's native Windows style drops the unqualified rule's background for the
   normal enabled state - the acquisition app hit this and fixed it the same
   way, by re-declaring it under an explicit :enabled selector. */
QPushButton:enabled {{ background-color: #0e639c; }}
QPushButton:hover {{ background-color: #1177bb; }}
QPushButton:pressed {{ background-color: #094771; }}
QPushButton:disabled {{ background-color: {BORDER}; color: {FG_DIM}; }}
QPushButton#PrimaryButton:enabled {{ background-color: #107c41; }}
QPushButton#PrimaryButton:hover {{ background-color: #149750; }}
QPushButton#StopButton:enabled {{ background-color: #a1260d; }}
QPushButton#StopButton:hover {{ background-color: #c42b0e; }}

QLabel {{ color: {FG}; }}
QLabel#StatusValue {{ font-weight: bold; color: {GREEN}; }}
QLabel#ErrorValue {{ font-weight: bold; color: {RED}; }}
QLabel#WarnValue {{ font-weight: bold; color: {AMBER}; }}
QLabel#HintLabel {{ color: #8a929b; }}

QLineEdit, QSpinBox, QDoubleSpinBox, QComboBox {{
    background-color: {BG_FIELD};
    color: {FG};
    border: 1px solid {BORDER};
    padding: 4px;
    border-radius: 3px;
}}
QLineEdit:focus, QSpinBox:focus, QDoubleSpinBox:focus {{ border: 1px solid {ACCENT}; }}
QLineEdit:disabled, QSpinBox:disabled, QDoubleSpinBox:disabled, QComboBox:disabled {{
    background-color: {BG_PANEL};
    color: #808080;
    border: 1px solid {BORDER};
}}
QComboBox QAbstractItemView {{
    background-color: {BG_PANEL};
    color: {FG};
    selection-background-color: {ACCENT};
    selection-color: #ffffff;
}}

QCheckBox {{ color: {FG}; spacing: 6px; }}
QCheckBox:disabled {{ color: {FG_DIM}; }}
QCheckBox::indicator, QGroupBox::indicator {{
    width: 14px; height: 14px;
    border: 1px solid #6e6e6e;
    border-radius: 3px;
    background-color: {BG_FIELD};
}}
QCheckBox::indicator:hover, QGroupBox::indicator:hover {{ border: 1px solid {ACCENT}; }}
QCheckBox::indicator:checked, QGroupBox::indicator:checked {{
    background-color: {ACCENT};
    border: 1px solid {ACCENT};
}}
QCheckBox::indicator:disabled {{ background-color: {BG_PANEL}; border: 1px solid {BORDER}; }}

QScrollArea {{ background-color: {BG}; border: none; }}
QScrollArea > QWidget > QWidget {{ background-color: {BG}; }}

QProgressBar {{
    border: 1px solid {BORDER};
    border-radius: 3px;
    text-align: center;
    color: {FG};
    background-color: {BG_PANEL};
}}
QProgressBar::chunk {{ background-color: {GREEN}; }}

QTextEdit, QPlainTextEdit {{
    background-color: {BG_FIELD};
    color: {FG};
    border: 1px solid {BORDER};
    border-radius: 3px;
    font-family: "Consolas", "Courier New", monospace;
    font-size: 12px;
}}

/* Not in the acquisition app's stylesheet - it has no tables. Without these,
   Qt falls back to its light default palette here. */
QTableWidget, QTableView {{
    background-color: {BG_RAISED};
    alternate-background-color: #2a2a2b;
    color: {FG};
    gridline-color: {BORDER};
    border: 1px solid {BORDER};
    border-radius: 3px;
    selection-background-color: {ACCENT};
    selection-color: #ffffff;
}}
QTableWidget::item {{ padding: 3px 6px; }}
QTableWidget::item:selected {{ background-color: {ACCENT}; color: #ffffff; }}
QHeaderView {{ background-color: {BG_PANEL}; }}
QHeaderView::section {{
    background-color: {BG_PANEL};
    color: {FG};
    padding: 6px;
    border: none;
    border-right: 1px solid {BORDER};
    border-bottom: 1px solid {BORDER};
    font-weight: bold;
}}
QTableCornerButton::section {{ background-color: {BG_PANEL}; border: none; }}

QStatusBar {{ background-color: {ACCENT}; color: white; }}
QStatusBar::item {{ border: none; }}

QSplitter::handle {{ background-color: {BORDER}; }}
QMenuBar {{ background-color: {BG_RAISED}; color: {FG}; }}
QMenuBar::item:selected {{ background-color: {ACCENT}; }}
QMenu {{ background-color: {BG_PANEL}; color: {FG}; border: 1px solid {BORDER}; }}
QMenu::item:selected {{ background-color: {ACCENT}; }}
QToolTip {{
    background-color: {BG_PANEL};
    color: {FG};
    border: 1px solid {BORDER};
    padding: 4px;
}}
"""

# Row tinting for the sections table. The light pastels a default-theme table
# would use are unreadable here, so these are dark tints paired with a
# coloured value in the QC column (same amber/green/red the acquisition app's
# log and status labels use).
ROW_PENDING = "#3d3320"
ROW_SCORED = "#1e3226"
ROW_UNAVAILABLE = "#262626"
