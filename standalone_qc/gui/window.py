"""The standalone QC window, laid out like the acquisition app's HI-MC page.

Shell (mirrors msem_control/ui/main_window.py):
  - dark VS Code-ish theme (see theme.py)
  - top bar: "MultiSEM" logo + a nav tab
  - left dock, "QC Status" - the analogue of the acquisition app's "System
    Status", but counts and current activity rather than vacuum and stage
    position, since this computer has no hardware to report on

Central page (mirrors HiMcPage's own top-to-bottom order):
  1. scrollable form of QGroupBoxes ......... stretch 3
  2. action row: Score / Stop / Open
  3. progress bar
  4. "System Logs" panel .................... stretch 2

Diagnostics about the Image Directory go to the STATUS BAR, not to a banner
above the form. There used to be an amber banner as well, which said the same
thing twice on the one path that mattered ("Not a folder: ..." appeared in
both) and took a permanent slot at the top of the page to do it.

Operator vocabulary throughout - Sample / Section / mFoV, never the code's
internal block / region / tile.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional

from PySide6.QtCore import QSettings, Qt, QTimer, QUrl
from PySide6.QtGui import QColor, QDesktopServices
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDockWidget,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMenu,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QDoubleSpinBox,
    QSizePolicy,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .. import ledger, rollup
from ..config import load_config, policy_fingerprint
from ..discover import (
    RegionRef,
    find_block_dirs,
    list_regions,
    resolve_image_dir,
)
from .runner import QCRunner
from .theme import (
    AMBER,
    FG_DIM,
    GREEN,
    ROW_PENDING,
    ROW_SCORED,
    ROW_UNAVAILABLE,
    STYLESHEET,
)
from .widgets import LogPanel, StatusIndicator, file_row, group, no_wheel

COL_BATCH, COL_SECTION, COL_ACQ, COL_SIGNAL, COL_QC, COL_DETAIL = range(6)
_HEADERS = ["Batch", "Section", "Imaged", "Signal", "QC", "Detail"]

# Back-compat alias: the column was called Sample before Sample came to mean
# the wafer rather than the acquisition folder.
COL_SAMPLE = COL_BATCH

_SAMPLING_LABELS = [
    ("First / middle / last tissue mFoV (recommended)", "first_mid_last_tissue"),
    ("Every Nth mFoV", "every_n"),
    ("Every mFoV (slow)", "all"),
]


def _form_group(title: str):
    """A form QGroupBox that takes exactly the height its rows need.

    The default Preferred policy makes every group share surplus space
    EQUALLY, regardless of stretch - so all three ended up the same height
    and the tallest form was handed less than its minimum, at which point
    Qt compresses each row and the inputs overlap. Minimum policy pins a
    form to its own sizeHint and lets the sections table (which scrolls)
    and the log panel absorb whatever is left.
    """
    box, form = group(title)
    box.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Minimum)
    return box, form


def _pair(first: QWidget, mid_label: str, second: QWidget) -> QWidget:
    """Two fields on one form row, with an optional label between them.

    Added because the Analysis settings form outgrew the window: its
    minimum height exceeded what the page could give it, so Qt compressed
    every row to 17px when the inputs need 28 and the rendered widgets
    overlapped. Fewer, wider rows is the fix that does not cost legibility.
    """
    row = QWidget()
    h = QHBoxLayout(row)
    h.setContentsMargins(0, 0, 0, 0)
    h.setSpacing(8)
    h.addWidget(first)
    if mid_label:
        lab = QLabel(mid_label)
        lab.setObjectName("HintLabel")
        h.addWidget(lab)
    h.addWidget(second)
    h.addStretch(1)
    return row


class QCWindow(QMainWindow):
    def __init__(self, settings: Optional[QSettings] = None) -> None:
        super().__init__()
        self.setWindowTitle("MultiSEM Standalone QC")
        self.resize(1500, 980)

        # Injectable so tests can hand in an isolated ini file. Redirecting
        # Qt's global QSettings path is not enough on Windows, where the
        # default backend is the registry - a test would then read, and on
        # close overwrite, the operator's real Image Directory, and refresh
        # against real data on the share.
        self._settings = settings or QSettings(
            "HarvardMCB", "MultiSEM-StandaloneQC"
        )
        self._regions: List[RegionRef] = []
        self._queue: List[Dict[str, Any]] = []
        # What the typed path resolved to: the real output root, plus which
        # single Sample was meant if the operator pointed at one directly.
        # Everything downstream (ledger, --output-root) uses these, never the
        # raw text, so pointing at a Sample folder still writes the ledger and
        # wafer-level files where a run pointed at the root would.
        self._resolved_root: Optional[Path] = None
        self._sample_filter: Optional[str] = None
        # Whether anything actually scored since the last report rebuild, so
        # draining an empty queue doesn't rewrite reports for no reason.
        self._ran_any = False
        # Last Image Directory diagnostic, kept so a successful
        # refresh can show it alongside the section count.
        self._notice = ""

        # Realtime mode: poll for sections that have just finished imaging.
        # A timer here rather than a long-lived subprocess, because each
        # discovery pass is only a few small JSON reads and the window
        # already knows how to queue and run sections one at a time.
        self._watching = False
        self._watch_timer = QTimer(self)
        self._watch_timer.timeout.connect(self._on_watch_tick)
        # Sections already reported as having failed, so a long watch does
        # not repeat the same warning on every poll.
        self._reported_failures: set = set()

        # A SEPARATE subprocess from the scoring runner. The agent is
        # long-lived, must not be killed by Stop, and must die with this
        # window - which is what "leaving the window open keeps it running"
        # means in practice: QProcess children are terminated when their
        # parent QObject goes away.
        self._agent = QCRunner(self)
        self._agent.finished.connect(self._on_agent_finished)

        self._runner = QCRunner(self)
        self._runner.line.connect(self._on_runner_line)
        self._runner.progress.connect(self._on_progress)
        self._runner.section_started.connect(self._on_section_started)
        self._runner.section_result.connect(self._on_section_result)
        self._runner.warning.connect(
            lambda msg: self.log_panel.append_log("StandaloneQC", "WARNING", msg)
        )
        self._runner.phase.connect(self._on_phase)
        self._runner.finished.connect(self._on_run_finished)

        # Ticks the "Doing now" elapsed counter, so a long silent phase
        # still visibly advances.
        self._phase_name = ""
        self._phase_started = 0.0
        self._phase_timer = QTimer(self)
        self._phase_timer.setInterval(1000)
        self._phase_timer.timeout.connect(self._tick_phase)

        self._build_ui()
        self.setStyleSheet(STYLESHEET)
        self._restore_settings()
        self._on_mode_changed()
        if self._root_edit.text().strip():
            self.refresh()

    # === shell ============================================================
    def _build_ui(self) -> None:
        central = QWidget()
        main = QVBoxLayout(central)
        main.setContentsMargins(0, 0, 0, 0)
        main.setSpacing(0)

        self._build_top_bar(main)

        page = QWidget()
        self.layout_page = QVBoxLayout(page)
        self.layout_page.setContentsMargins(16, 12, 16, 12)
        self.layout_page.setSpacing(8)
        main.addWidget(page, 1)

        # 1. form groups.
        #
        # NOT wrapped in a QScrollArea, which is where HiMcPage puts its own
        # form. That page needs one - it has ten-plus groups of fields and
        # genuinely cannot fit. Here there are three groups, one of which is a
        # table that scrolls internally, so a scroll area only nests one
        # scrollbar inside another and pushes "Scoring settings" below the
        # fold where an operator never sees it. Fixed groups top and bottom
        # with the table taking the stretch keeps everything on screen.
        self.layout_page.addWidget(self._build_io_group())
        self.layout_page.addWidget(self._build_sections_group(), 3)
        self.layout_page.addWidget(self._build_scoring_group())

        # 2. action row
        self.layout_page.addLayout(self._build_action_row())

        # 3. progress
        self._progress = QProgressBar()
        self._progress.setTextVisible(True)
        self._progress.setFormat("idle")
        self.layout_page.addWidget(self._progress)

        # 4. logs
        self.log_panel = LogPanel()
        # An explicit floor well below its natural minimum. The page's
        # minimum heights together exceeded the window, and Qt resolves
        # that by compressing form rows past their minimum until the
        # inputs overlap. The log has stretch 2, so it reclaims this space
        # whenever the window is tall enough - it only gives it up when
        # something has to.
        self.log_panel.setMinimumHeight(70)
        self.layout_page.addWidget(self.log_panel, 2)

        self.setCentralWidget(central)
        self._build_status_dock()
        self.statusBar().showMessage("Pick an image directory, then Refresh.")
        self._sync_buttons()

    def _build_top_bar(self, parent_layout: QVBoxLayout) -> None:
        bar = QWidget()
        bar.setFixedHeight(50)
        bar.setObjectName("TopBar")
        h = QHBoxLayout(bar)
        h.setContentsMargins(20, 0, 20, 0)
        h.setSpacing(0)

        logo = QLabel("MultiSEM")
        logo.setObjectName("LogoLabel")
        h.addWidget(logo)
        h.addSpacing(30)

        tab = QPushButton("Standalone QC")
        tab.setObjectName("NavTab")
        tab.setCheckable(True)
        tab.setChecked(True)
        # One page, so this is an identity marker rather than navigation -
        # clicking it must not be able to un-check it and look "off".
        tab.clicked.connect(lambda: tab.setChecked(True))
        h.addWidget(tab)
        h.addStretch()

        self.lbl_machine = QLabel("no microscope on this computer")
        self.lbl_machine.setObjectName("HintLabel")
        h.addWidget(self.lbl_machine)
        parent_layout.addWidget(bar)

    def _build_status_dock(self) -> None:
        dock = QDockWidget("QC Status", self)
        dock.setAllowedAreas(Qt.LeftDockWidgetArea | Qt.RightDockWidgetArea)
        dock.setFeatures(
            QDockWidget.DockWidgetMovable | QDockWidget.DockWidgetFloatable
        )
        dock.setMinimumWidth(240)
        dock.setMaximumWidth(320)

        host = QWidget()
        v = QVBoxLayout(host)
        v.setContentsMargins(10, 10, 10, 10)
        v.setSpacing(4)

        box, _ = group("Sections")
        inner = QVBoxLayout()
        self._ind_total = StatusIndicator("Total", "0")
        self._ind_pending = StatusIndicator("Awaiting analysis", "0")
        self._ind_scored = StatusIndicator("Analyzed", "0")
        self._ind_unavailable = StatusIndicator("Not imaged", "0")
        for w in (self._ind_total, self._ind_pending, self._ind_scored,
                  self._ind_unavailable):
            inner.addWidget(w)
        box.layout().addRow(self._wrap(inner))
        v.addWidget(box)

        box2, _ = group("Current run")
        inner2 = QVBoxLayout()
        self._ind_state = StatusIndicator("State", "idle")
        self._ind_current = StatusIndicator("Section", "—")
        # The question an operator actually has while watching a long run:
        # what is it doing right now, and for how long. Without this the
        # window could sit apparently frozen through a fifteen-minute
        # overview build with nothing on screen to say so.
        self._ind_phase = StatusIndicator("Doing now", "—")
        self._ind_queue = StatusIndicator("Queued", "0")
        self._ind_last_q = StatusIndicator("Last median Q", "—")
        self._ind_outliers = StatusIndicator("Outlier beams", "—")
        for w in (self._ind_state, self._ind_current, self._ind_phase,
                  self._ind_queue, self._ind_last_q, self._ind_outliers):
            inner2.addWidget(w)
        box2.layout().addRow(self._wrap(inner2))
        v.addWidget(box2)

        v.addStretch(1)
        note = QLabel(
            "Analysis reads images from the share only. It never writes into "
            "acquisition's own folders."
        )
        note.setObjectName("HintLabel")
        note.setWordWrap(True)
        v.addWidget(note)

        dock.setWidget(host)
        self.addDockWidget(Qt.LeftDockWidgetArea, dock)

    @staticmethod
    def _wrap(layout: QVBoxLayout) -> QWidget:
        w = QWidget()
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)
        w.setLayout(layout)
        return w

    # === form groups ======================================================
    def _build_io_group(self) -> QWidget:
        box, form = _form_group("Image data to check")

        self._mode = no_wheel(QComboBox())
        self._mode.addItem(
            "Post-acquisition — imaging is finished, analyze what is there", "post"
        )
        self._mode.addItem(
            "Realtime — imaging is still running, analyze sections as they finish",
            "realtime",
        )
        self._mode.addItem(
            "Service — the acquisition computer asks, this computer analyzes",
            "service",
        )
        self._mode.currentIndexChanged.connect(self._on_mode_changed)
        form.addRow("Mode:", self._mode)

        self._root_edit = QLineEdit()
        self._root_edit.setPlaceholderText(
            r"Z:\...\<run>\w10h31_..._<timestamp>   — the batch folder holding the section folders"
        )
        self._root_edit.returnPressed.connect(self.refresh)
        self._refresh_btn = QPushButton("Refresh")
        self._refresh_btn.clicked.connect(self.refresh)
        form.addRow("Image Directory:",
                    file_row(self._root_edit, self._on_browse, self._refresh_btn))
        hint = QLabel(
            "Point at one batch — the timestamped folder for a single "
            "acquisition of a wafer. The run folder above it also works, and "
            "then shows every batch in that run.\n"
            "On this computer the share is Z:\\ — the acquisition PC calls the "
            "same share V:\\, so swap the drive letter on a pasted path."
        )
        hint.setObjectName("HintLabel")
        hint.setWordWrap(True)
        form.addRow("", hint)
        return box

    def _build_sections_group(self) -> QWidget:
        box, form = group("Sections")
        self._table = QTableWidget(0, len(_HEADERS))
        self._table.setHorizontalHeaderLabels(_HEADERS)
        self._table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self._table.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self._table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self._table.verticalHeader().setVisible(False)
        # 120, not 160: the three groups plus the action row, progress bar
        # and log panel together asked for more vertical space than the
        # window has, and Qt resolves that by compressing every form row
        # below its minimum - which made the Analysis settings inputs
        # overlap. The table scrolls internally, so it is the one widget
        # here that loses nothing by being shorter.
        self._table.setMinimumHeight(120)
        hh = self._table.horizontalHeader()
        hh.setSectionResizeMode(QHeaderView.Interactive)
        hh.setSectionResizeMode(COL_DETAIL, QHeaderView.Stretch)
        self._table.itemSelectionChanged.connect(self._sync_buttons)
        self._table.doubleClicked.connect(self._on_open_report)
        form.addRow(self._table)
        hint = QLabel(
            "Amber = finished imaging, awaiting analysis.   Green = already analyzed.   "
            "Grey = not finished imaging.   Double-click a row to open its report."
        )
        hint.setObjectName("HintLabel")
        hint.setWordWrap(True)
        form.addRow("", hint)
        return box

    def _build_scoring_group(self) -> QWidget:
        box, form = _form_group("Analysis settings")

        self._sampling = no_wheel(QComboBox())
        for label, value in _SAMPLING_LABELS:
            self._sampling.addItem(label, value)
        self._sampling.currentIndexChanged.connect(self._sync_buttons)
        form.addRow("mFoVs to analyze:", self._sampling)

        self._every_n = no_wheel(QSpinBox())
        self._every_n.setRange(2, 500)
        self._every_n.setValue(25)
        self._every_n_row_label = "Analyze every Nth mFoV:"
        form.addRow(self._every_n_row_label, self._every_n)

        self._q_low = no_wheel(QDoubleSpinBox())
        self._q_low.setRange(-100.0, 1000.0)
        self._q_low.setDecimals(1)
        self._q_low.setSingleStep(1.0)
        self._q_low.setValue(10.0)
        self._q_low.setToolTip(
            "<p>Image quality at or above which an mFoV counts as good.</p>"
            "<p>It sets the overview colour scale and the quality tiers in "
            "the reports. It changes no measurement, so moving it does not "
            "invalidate anything already analyzed.</p>"
            "<p>You can also move it per-section in the dashboard, without "
            "re-analyzing: the overview overlay recolours in the browser.</p>"
        )

        self._q_high = no_wheel(QDoubleSpinBox())
        self._q_high.setRange(-100.0, 1000.0)
        self._q_high.setDecimals(1)
        self._q_high.setSingleStep(1.0)
        self._q_high.setValue(30.0)
        self._q_high.setToolTip(
            "<p>Top of the colour scale: quality at or above this is fully "
            "green, and the colours ramp between the two values.</p>"
            "<p>Set it EQUAL to the threshold on the left for a hard "
            "pass/fail instead: red below, green at or above, no ramp.</p>"
            "<p>Only affects how results are coloured - no measurement "
            "changes either way.</p>"
        )
        # Paired on one row: they are two ends of one scale, and the
        # form had grown tall enough that Qt was compressing every row
        # below its minimum height - which is what made the inputs overlap.
        self._q_row = _pair(self._q_low, "… fully good at:", self._q_high)
        form.addRow("Image quality threshold:", self._q_row)



        self._workers = no_wheel(QSpinBox())
        self._workers.setRange(1, 64)
        self._workers.setValue(24)
        self._workers.setToolTip(
            "<p>How many sFoVs (single beam images, ~3.3 MB each) are "
            "read off the share and measured at the same time.</p>"
            "<p>The work is waiting on the network, not on this PC, so "
            "a number well above the CPU core count is normal and "
            "usually faster. It changes speed only, never results - "
            "raising it does not invalidate sections already "
            "analyzed.</p>"
            "<p>Lower it if analysis is competing with acquisition for "
            "the share.</p>"
        )
        # "# sFoVs Processed Concurrently", not "Parallel reads": the old
        # label named the implementation (a thread pool) rather than the
        # thing the operator is choosing, and did not say what was being
        # read. One sFoV is one beam image - the unit these jobs come in.
        form.addRow("# sFoVs Processed Concurrently:", self._workers)

        self._imaging_json = QLineEdit()
        self._imaging_json.setPlaceholderText(
            "(optional) blank = find it automatically by matching region names"
        )
        self._imaging_json.setToolTip(
            "<p>The Zeiss imaging JSON this batch was acquired from.</p>"
            "<p>The section dashboard's <b>Retake selection</b> panel builds "
            "a partial retake by cloning the original region's XML out of "
            "this file and replacing its contour with the mFoVs you lasso. "
            "Without it that panel cannot build anything.</p>"
            "<p>Leave blank to find it automatically: candidates in and "
            "above the batch folder are scored by how many of the batch's "
            "own region names they contain. This is deliberately not a "
            "filename match - on w10h31 the batch named "
            "w10h31_dye_R3_8NM_... was acquired from w10h31_dye_border.json, "
            "while w10h31_dye_R3.json shares none of its regions.</p>"
        )
        form.addRow("Imaging JSON:",
                    file_row(self._imaging_json, self._on_browse_imaging_json))

        self._control_root = QLineEdit()
        self._control_root.setPlaceholderText(
            r"\server\share\StandaloneQC_control   — a folder BOTH computers can see"
        )
        self._control_root.setToolTip(
            "<p>Service mode only. The folder the acquisition computer "
            "writes its requests into, and where this computer publishes "
            "progress.</p>"
            "<p>Use a UNC path, not a drive letter: this computer mounts "
            "the share as Z: and the acquisition computer as V:, so a "
            "Z: path written here means nothing over there.</p>"
        )
        # Keep the ROW, not just the line edit: the field is wrapped in a
        # file_row with its own Browse button, so hiding the QLineEdit alone
        # would strand the button and the label beside it.
        self._control_row = file_row(self._control_root, self._on_browse_control)
        form.addRow("Control folder:", self._control_row)



        # No opt-out: the section overview is panel ⓪ of the dashboard, and
        # a dashboard without it makes the hex maps much harder to read.
        # Only its downsample factor is adjustable - that is the knob that
        # trades detail against time on a large section.
        self._overview_ds = no_wheel(QSpinBox())
        self._overview_ds.setRange(1, 64)
        self._overview_ds.setValue(16)
        self._overview_ds.setPrefix("÷ ")
        self._overview_ds.setToolTip(
            "<p>Downsample factor for the section overview. Higher is "
            "faster and smaller with less detail; 16 gives about "
            "125×109 px per beam image.</p>"
            "<p>The overview PLACES each beam at the coordinate "
            "acquisition recorded for it. Nothing is aligned or "
            "registered, so a beam recorded in the wrong place is drawn "
            "in the wrong place.</p>"
            "<p>It always runs: it is the image the overlay and the "
            "focus-support-point markers sit on.</p>"
        )
        form.addRow("Overview downsample:", self._overview_ds)

        # No GUI opt-out for the map pass (5 beams on every mFoV). It is
        # not a "tissue probe" any more - results_by_tile is built from it,
        # so it fills the overview quality numbers, IMQA, the scanfault and
        # jitter checks and the retake lasso. Measured on a 10-mFoV
        # section, turning it off took per-mFoV results from 10 to 3 and
        # scan-fault checks from 10 to 0. `--no-probe` still exists for
        # troubleshooting from the command line.
        self._rescore = QCheckBox("Re-analyze sections already analyzed")
        form.addRow("Re-analyze:", self._rescore)


        self._watch_interval = no_wheel(QSpinBox())
        self._watch_interval.setRange(15, 3600)
        self._watch_interval.setValue(60)
        self._watch_interval.setSuffix(" s")
        self._watch_interval.setToolTip(
            "How often realtime mode checks for sections that have finished "
            "imaging. A check is just a few small JSON reads."
        )
        self._watch_interval_label = QLabel("Check for new sections every:")
        form.addRow(self._watch_interval_label, self._watch_interval)
        return box

    def _build_action_row(self) -> QHBoxLayout:
        row = QHBoxLayout()

        # One horizontal line, in this order:
        #   Analyze All Pending  (or Start Watching in realtime mode)
        #   Analyze Selected
        #   Open Retake Manager
        #   Stop
        # then the per-section openers and the re-analyze toggle.
        self._score_all_btn = QPushButton("▶  Analyze All Pending")
        self._score_all_btn.setObjectName("PrimaryButton")
        self._score_all_btn.clicked.connect(self._on_score_all)
        row.addWidget(self._score_all_btn)

        # Occupies the same leftmost slot; only one of the two is ever shown,
        # because it is the primary action for whichever mode is selected.
        self._watch_btn = QPushButton("▶  Start Watching")
        self._watch_btn.setObjectName("PrimaryButton")
        self._watch_btn.setToolTip(
            "Poll for sections that have just finished imaging, analyze each "
            "one, and refresh the retake manager."
        )
        self._watch_btn.clicked.connect(self._on_toggle_watch)
        row.addWidget(self._watch_btn)

        self._service_btn = QPushButton("▶  Start Service")
        self._service_btn.setObjectName("PrimaryButton")
        self._service_btn.setToolTip(
            "Take analysis requests from the acquisition computer's QC tab "
            "and run them here. Leave this window open to keep the service "
            "running - closing it stops the service."
        )
        self._service_btn.clicked.connect(self._on_toggle_service)
        row.addWidget(self._service_btn)

        self._score_sel_btn = QPushButton("▶  Analyze Selected")
        self._score_sel_btn.setObjectName("PrimaryButton")
        self._score_sel_btn.clicked.connect(self._on_score_selected)
        row.addWidget(self._score_sel_btn)

        # A menu rather than a plain button: there are two retake managers -
        # the run-level one covering every batch, and one per batch - and
        # which you want depends on the question you are asking.
        self._retake_btn = QPushButton("Open Retake Manager  ▾")
        self._retake_btn.setToolTip(
            "Sections with Accept / Retake controls. Built from the "
            "acquisition manifests, so it works before any analysis; the "
            "quality columns fill in as sections are analyzed."
        )
        self._retake_menu = QMenu(self._retake_btn)
        self._retake_btn.setMenu(self._retake_menu)
        self._retake_menu.aboutToShow.connect(self._populate_retake_menu)
        row.addWidget(self._retake_btn)

        self._stop_btn = QPushButton("■  Stop")
        self._stop_btn.setObjectName("StopButton")
        self._stop_btn.clicked.connect(self._runner.stop)
        self._stop_btn.setEnabled(False)
        row.addWidget(self._stop_btn)

        self._open_report_btn = QPushButton("Section Report")
        self._open_report_btn.clicked.connect(self._on_open_report)
        row.addWidget(self._open_report_btn)

        self._open_folder_btn = QPushButton("Folder")
        self._open_folder_btn.clicked.connect(self._on_open_folder)
        row.addWidget(self._open_folder_btn)

        row.addStretch(1)
        return row

    def _populate_retake_menu(self) -> None:
        """Rebuilt on each open, listing EVERY batch under the run.

        Enumerated from the run root rather than from self._regions: that
        list is filtered to whichever batch the Image Directory points at,
        so pointing at one batch - the normal way to analyze - left the menu
        offering only that batch and no way to reach any other, including
        earlier attempts at the same sections.

        A batch with no report written yet is still listed, marked so, and
        opening it builds the report on the spot. Omitting it would hide the
        batch being analyzed right now, which is the one most likely to be
        wanted.
        """
        self._retake_menu.clear()
        act = self._retake_menu.addAction("Global — whole run")
        act.triggered.connect(self._on_open_run_report)

        root = self._resolved_root
        batches = find_block_dirs(root) if root else []
        if not batches:
            # Fall back to whatever the table knows, so the menu is never
            # emptier than the window itself.
            batches = sorted({r.block_dir for r in self._regions},
                             key=lambda p: p.name)
        if not batches:
            return

        current = self._sample_filter
        self._retake_menu.addSeparator()
        for b in batches:
            label = f"Batch — {b.name}"
            if b.name == current:
                label += "   (Image Directory)"
            if not (b / rollup.BATCH_REPORT_NAME).is_file():
                label += "   · no report yet"
            a = self._retake_menu.addAction(label)
            # Default argument, not a closure over b: a bare lambda would
            # capture the loop variable and every entry would open the last
            # batch.
            a.triggered.connect(lambda _checked=False, d=b: self._open_batch_report(d))

    # === settings =========================================================
    def _restore_settings(self) -> None:
        s = self._settings
        self._root_edit.setText(str(s.value("output_root", "")))
        want = str(s.value("sampling", "first_mid_last_tissue"))
        idx = self._sampling.findData(want)
        self._sampling.setCurrentIndex(idx if idx >= 0 else 0)
        self._every_n.setValue(int(s.value("every_n", 25)))
        self._workers.setValue(int(s.value("workers", 24)))
        self._q_low.setValue(float(s.value("q_low", 10.0)))
        self._q_high.setValue(float(s.value("q_high", 30.0)))
        self._imaging_json.setText(str(s.value("imaging_json", "")))
        self._control_root.setText(str(s.value("control_root", "")))
        self._overview_ds.setValue(int(s.value("overview_ds", 16)))
        self._watch_interval.setValue(int(s.value("watch_interval", 60)))
        want_mode = str(s.value("mode", "post"))
        idx = self._mode.findData(want_mode)
        self._mode.setCurrentIndex(idx if idx >= 0 else 0)

    def _save_settings(self) -> None:
        s = self._settings
        s.setValue("output_root", self._root_edit.text().strip())
        s.setValue("sampling", self._sampling.currentData())
        s.setValue("every_n", self._every_n.value())
        s.setValue("workers", self._workers.value())
        s.setValue("q_low", self._q_low.value())
        s.setValue("q_high", self._q_high.value())
        s.setValue("imaging_json", self._imaging_json.text().strip())
        s.setValue("control_root", self._control_root.text().strip())
        s.setValue("overview_ds", self._overview_ds.value())
        s.setValue("watch_interval", self._watch_interval.value())
        s.setValue("mode", self._mode.currentData())

    def closeEvent(self, event: Any) -> None:
        if self._watching:
            self._stop_watching()
        # The service MUST die with the window. "Leave the window open to
        # keep it running" is only a safe rule if closing it actually
        # stops it - an orphaned agent would go on taking requests from the
        # acquisition computer with nothing on screen to say so, which is
        # worse than either stopping or staying up honestly.
        if self._agent.is_running():
            self._agent.stop()
        if self._runner.is_running():
            answer = QMessageBox.question(
                self, "Analysis in progress",
                "An analysis run is still going. Stop it and close?",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
            )
            if answer != QMessageBox.Yes:
                event.ignore()
                return
            self._runner.stop()
        self._save_settings()
        super().closeEvent(event)

    # === discovery ========================================================
    def refresh(self) -> None:
        """Re-read the output root. Reads only the small JSON marker files —
        never image data — so it is instant and safe to call at any time, even
        while acquisition is still writing into the same tree."""
        root = self._root_edit.text().strip()
        if not root:
            self._set_notice("No image directory set yet. Browse to a batch "
                             "folder — the timestamped folder for one "
                             "acquisition of a wafer.")
            return
        if not Path(root).is_dir():
            self._set_notice(f"Not a folder: {root}")
            self._table.setRowCount(0)
            self._regions = []
            self._update_counts()
            self._sync_buttons()
            return

        # A batch folder is the expected input; the run folder above it is
        # also accepted - see discover.resolve_image_dir.
        resolved, batch_filter = resolve_image_dir(root)
        self._resolved_root = resolved
        self._sample_filter = batch_filter

        cfg = self._build_cfg()
        fingerprint = policy_fingerprint(cfg)
        entries = ledger.load(resolved)
        try:
            self._regions = list_regions(resolved, batch_filter)
        except OSError as exc:
            self._set_notice(f"Could not read {resolved}: {exc}")
            return

        if not self._regions:
            self._set_notice(self._diagnose_empty(Path(root)))
        elif batch_filter:
            # The normal, intended case: one batch. Nothing to warn about.
            self._set_notice(None)
        else:
            n_batches = len({r.block_dir for r in self._regions})
            self._set_notice(
                f"That path is the run folder, not one batch — showing all "
                f"{n_batches} batch(es) in “{resolved.name}”. Point at a "
                f"single timestamped batch folder to narrow it down."
                if n_batches > 1 else None
            )

        self._table.setRowCount(len(self._regions))
        for row, r in enumerate(self._regions):
            qc, state = self._qc_state(r, entries, fingerprint)
            values = [r.batch_label, r.name, r.status,
                      "trigger" if r.status_source == "region_complete" else "manifest",
                      qc, r.detail or ""]
            tint = {
                "pending": QColor(ROW_PENDING),
                "scored": QColor(ROW_SCORED),
            }.get(state, QColor(ROW_UNAVAILABLE) if r.status != "done" else None)

            for col, val in enumerate(values):
                item = QTableWidgetItem(str(val))
                if tint is not None:
                    item.setBackground(tint)
                if col == COL_QC:
                    if state == "pending":
                        item.setForeground(QColor(AMBER))
                    elif state == "scored":
                        item.setForeground(QColor(GREEN))
                    elif state == "none":
                        item.setForeground(QColor(FG_DIM))
                self._table.setItem(row, col, item)

        self._table.resizeColumnsToContents()
        self._table.setColumnWidth(
            COL_BATCH, min(300, self._table.columnWidth(COL_BATCH))
        )
        self._update_counts()
        # Build the retake manager here, not only after scoring: it comes
        # entirely from the acquisition manifests, so it is valid (and useful)
        # before anything has been scored - status, logs and the Accept/Retake
        # buttons all work, and the quality columns fill in later. Costs a few
        # small JSON reads, so it is fine on every Refresh.
        self._rebuild_reports(quiet=True)
        summary = (
            f"{len(self._regions)} section(s) — {self._n_pending} awaiting "
            f"analysis. Scoring policy {fingerprint}."
        )
        # Appended, not overwritten: a run folder holding several batches
        # still loads successfully, and that notice would otherwise be wiped
        # by this line the instant it was set.
        self.statusBar().showMessage(
            f"{summary}  {self._notice}" if self._notice else summary
        )
        self._sync_buttons()

    def _qc_state(self, r: RegionRef, entries: Dict[str, Any],
                  fingerprint: str) -> tuple[str, str]:
        if ledger.is_done(entries, r.key, fingerprint):
            return "analyzed", "scored"
        if r.key in entries:
            return str(entries[r.key].get("status", "?")), "other"
        if r.status == "done":
            return "awaiting analysis", "pending"
        return "—", "none"

    def _update_counts(self) -> None:
        cfg = self._build_cfg()
        fingerprint = policy_fingerprint(cfg)
        entries = ledger.load(self._resolved_root or ".")
        pending = scored = unavailable = 0
        for r in self._regions:
            _, state = self._qc_state(r, entries, fingerprint)
            if state == "pending":
                pending += 1
            elif state == "scored":
                scored += 1
            elif r.status != "done":
                unavailable += 1
        self._n_pending = pending
        self._ind_total.set_value(len(self._regions))
        self._ind_pending.set_value(pending, "warn" if pending else "ok")
        self._ind_scored.set_value(scored)
        self._ind_unavailable.set_value(unavailable)

    def _diagnose_empty(self, given: Path) -> str:
        """Say what is actually in the folder, rather than only that nothing
        matched. "Nothing found here" while the operator is looking at a
        screen full of data is not a useful thing to be told."""
        try:
            subdirs = [p.name for p in given.iterdir() if p.is_dir()]
        except OSError as exc:
            return f"Could not list {given}: {exc}"

        if not subdirs:
            return (
                f"{given} has no subfolders at all. The Image Directory is a "
                f"batch folder — the timestamped folder holding this "
                f"acquisition's section folders."
            )

        # A folder full of mfov* dirs is a section; the batch is one level up.
        if any(n.lower().startswith("mfov") for n in subdirs):
            return (
                f"That looks like a single Section folder (it contains mFoV "
                f"folders). The batch folder is one level up: {given.parent}"
            )

        deeper = [
            p for p in given.iterdir()
            if p.is_dir() and (p / "region_manifest.json").is_file()
        ]
        if deeper:
            return (
                f"Found {len(deeper)} batch folder(s) but none is readable — "
                f"check permissions on {given}."
            )
        return (
            f"No batch folder found in or under {given}. A batch folder is "
            f"the timestamped one containing region_manifest.json — this path "
            f"has {len(subdirs)} subfolder(s), none of which has one "
            f"(e.g. “{subdirs[0]}”). Try one level up, or one level down."
        )

    def _set_notice(self, text: Optional[str]) -> None:
        """Something the operator needs to know about the Image Directory.

        The status bar, not a banner: these are all answers to "why is the
        table not showing what I expected", which is a question asked once
        and then acted on, so a line at the bottom serves as well as a
        permanent amber block at the top. On the success path refresh()
        appends the notice to its own summary instead of overwriting it.
        """
        self._notice = text or ""
        if text:
            self.statusBar().showMessage(text)

    def _build_cfg(self) -> Dict[str, Any]:
        """Config for the currently-selected options. Used to compute the
        policy fingerprint, so the QC column reflects whether a section was
        scored under the settings that are selected right now."""
        return load_config(
            None,
            tile_sampling=self._sampling.currentData(),
            tile_sampling_every_n=self._every_n.value(),
            max_workers=self._workers.value(),
            low_quality_threshold=self._q_low.value(),
            high_quality_threshold=self._q_high.value(),
            imaging_json_path=self._imaging_json.text().strip() or None,
        )

    # === selection ========================================================
    def _selected_regions(self) -> List[RegionRef]:
        rows = sorted({i.row() for i in self._table.selectedIndexes()})
        return [self._regions[r] for r in rows if 0 <= r < len(self._regions)]

    def _pending_regions(self, skip_failed: bool = False) -> List[RegionRef]:
        """Sections finished imaging and not yet scored under these settings.

        skip_failed=True for automatic (realtime) queueing: a section that was
        already attempted and failed must NOT be retried on every poll, or a
        single bad section turns into hundreds of re-reads over a long
        unattended run. Manual "Score Selected" still retries it, and the
        re-score checkbox still forces it.
        """
        fingerprint = policy_fingerprint(self._build_cfg())
        entries = ledger.load(self._resolved_root or ".")
        out: List[RegionRef] = []
        for r in self._regions:
            if not r.is_scorable:
                continue
            status = ledger.attempt_status(entries, r.key, fingerprint)
            if status == "scored":
                continue
            if skip_failed and status is not None:
                continue
            out.append(r)
        return out

    def _failed_keys(self) -> List[str]:
        fingerprint = policy_fingerprint(self._build_cfg())
        entries = ledger.load(self._resolved_root or ".")
        return [
            r.key for r in self._regions
            if r.is_scorable
            and (ledger.attempt_status(entries, r.key, fingerprint)
                 not in (None, "scored"))
        ]

    def _why_disabled(self, running: bool, any_sel: bool,
                      any_scorable: bool, selected: bool) -> str:
        """Tooltip for an analysis button, saying why it is unavailable."""
        if running:
            return ("A section is being analyzed right now. This becomes "
                    "available when it finishes.")
        if not self._regions:
            return "No sections found under this Image Directory yet."
        if selected and not any_sel:
            return "Select one or more sections in the table first."
        if selected and not any_scorable:
            return ("The selected section has not finished imaging yet, so "
                    "there is nothing complete to analyze. Its images are "
                    "still being written.")
        if not selected and not self._pending_regions(skip_failed=False):
            return ("Every section that has finished imaging has already "
                    "been analyzed. Tick 'Re-analyze' to run them again.")
        if self._watching:
            return ("Analyze now, without waiting for the next watch poll. "
                    "Watching continues.")
        return ""

    def _sync_buttons(self) -> None:
        running = self._runner.is_running()
        sel = self._selected_regions()
        scorable_sel = [r for r in sel if r.is_scorable]
        every_n = self._sampling.currentData() == "every_n"

        # QFormLayout hides a row's label with its field in Qt 6.
        self._every_n.setVisible(every_n)
        label = self.layout_label_for(self._every_n)
        if label is not None:
            label.setVisible(every_n)

        self._score_sel_btn.setEnabled(bool(scorable_sel) and not running)
        self._score_all_btn.setEnabled(not running and bool(self._regions))
        self._stop_btn.setEnabled(running)
        self._refresh_btn.setEnabled(not running)
        self._open_report_btn.setEnabled(len(sel) == 1)
        self._open_folder_btn.setEnabled(len(sel) == 1)

        realtime = self._mode.currentData() == "realtime"
        service = self._mode.currentData() == "service"
        self._retake_btn.setEnabled(bool(self._regions))
        self._watch_btn.setVisible(realtime)
        serving = self._agent.is_running()
        self._service_btn.setVisible(service)
        self._service_btn.setText(
            "■  Stop Service" if serving else "▶  Start Service"
        )
        self._service_btn.setObjectName(
            "StopButton" if serving else "PrimaryButton"
        )
        self._service_btn.style().unpolish(self._service_btn)
        self._service_btn.style().polish(self._service_btn)
        self._watch_btn.setText(
            "■  Stop Watching" if self._watching else "▶  Start Watching"
        )
        self._watch_btn.setObjectName(
            "StopButton" if self._watching else "PrimaryButton"
        )
        self._watch_btn.style().unpolish(self._watch_btn)
        self._watch_btn.style().polish(self._watch_btn)
        # Manual analysis stays available WHILE WATCHING. It used to be
        # disabled ("the watcher owns the queue"), which left an operator
        # mid-run with every analysis button greyed out and only "Stop
        # Watching" live - so the only way to analyze a section by hand was
        # to stop watching first, which is exactly when you least want to.
        #
        # There is no conflict to protect against: _on_watch_tick already
        # returns early while a run is in flight or a queue is outstanding,
        # so a manual run simply makes the next poll a no-op, and the poll
        # after that picks up wherever acquisition has got to. Both buttons
        # are still disabled while a run is actually executing, which is what
        # stops two runs overlapping.
        self._score_sel_btn.setEnabled(bool(scorable_sel) and not running)
        self._score_all_btn.setEnabled(not running and bool(self._regions))
        # Visible in realtime too: the watcher polls on an interval, and an
        # operator who can see a section has just finished should not have to
        # wait out the rest of that interval to start it.
        self._score_all_btn.setVisible(True)

        # Say WHY a button is off, rather than leaving a row of dead grey
        # buttons to be interpreted.
        self._score_sel_btn.setToolTip(self._why_disabled(
            running, bool(sel), bool(scorable_sel), selected=True))
        self._score_all_btn.setToolTip(self._why_disabled(
            running, bool(sel), bool(scorable_sel), selected=False))

        if sel and not scorable_sel and not running:
            self.statusBar().showMessage(
                f"{sel[0].name} is '{sel[0].status}', not finished imaging — "
                f"nothing to analyze."
            )

    def layout_label_for(self, field: QWidget) -> Optional[QWidget]:
        parent_form = field.parentWidget().layout() if field.parentWidget() else None
        if parent_form is None or not hasattr(parent_form, "labelForField"):
            return None
        return parent_form.labelForField(field)

    # === running ==========================================================
    def _base_args(self, force: bool = False) -> List[str]:
        args = [
            # The RESOLVED root, not the typed text - so a path that pointed
            # at a Sample folder still hands the CLI the real output root.
            "--output-root", str(self._resolved_root or self._root_edit.text().strip()),
            "--workers", str(self._workers.value()),
            "--sampling", str(self._sampling.currentData()),
            # Exact matching: we name concrete rows the operator picked, and
            # substring matching would let "S2R1" also catch "S2R10".
            "--exact",
        ]
        if self._sampling.currentData() == "every_n":
            args += ["--every-n", str(self._every_n.value())]
        args += ["--overview-ds", str(self._overview_ds.value())]
        args += ["--quality-threshold", str(self._q_low.value()),
                 "--high-quality-threshold", str(self._q_high.value())]
        imaging_json = self._imaging_json.text().strip()
        if imaging_json:
            args += ["--imaging-json", imaging_json]
        if force or self._rescore.isChecked():
            args.append("--force")
        # The window rebuilds the rollup reports itself once the queue drains
        # (see _rebuild_reports), so each subprocess doing it too would repeat
        # the same work once per section for no benefit.
        args.append("--no-reports")
        return args

    def _start_queue(
        self, regions: List[RegionRef], announce: bool = True,
        force: bool = False,
    ) -> None:
        if not regions:
            if announce:
                QMessageBox.information(
                    self, "Nothing to do",
                    "No section is finished imaging and un-analyzed. Tick "
                    "“Re-analyze sections that have already been analyzed” to run "
                    "them again.",
                )
            return
        args = self._base_args(force=force)
        # EXTEND, never replace: a manual "Analyze Selected" pressed while
        # the watcher already has sections queued would otherwise silently
        # drop them on the floor.
        self._queue += [
            {"key": r.key,
             "args": args + ["--sample", r.block_dir.name, "--section", r.folder]}
            for r in regions
        ]
        self.log_panel.append_log(
            "GUI", "INFO", f"queued {len(self._queue)} section(s)"
        )
        self._ind_state.set_value("running")
        self._sync_buttons()
        # Only kick the queue when nothing is executing. A run already in
        # flight will call _next_job itself when it finishes, and starting
        # one here on top of it would raise straight into _next_job's
        # error path.
        if not self._runner.is_running():
            self._next_job()

    def _next_job(self) -> None:
        self._ind_queue.set_value(len(self._queue))
        if not self._queue:
            self._progress.setFormat("done")
            self._progress.setValue(0)
            self._ind_state.set_value("idle")
            self._ind_current.set_value("—")
            # refresh() rebuilds the reports itself, so no explicit call here.
            self._ran_any = False
            self.refresh()
            if self._watching:
                self._ind_state.set_value("watching", "warn")
                self.statusBar().showMessage("Watching for the next section…")
            else:
                self.statusBar().showMessage("Finished.")
            return
        if self._runner.is_running():
            return                      # it will call back in when it ends
        job = self._queue[0]
        self._progress.setFormat(f"{job['key']} — starting")
        self._progress.setValue(0)
        try:
            self._runner.start(job["args"])
        except RuntimeError as exc:
            # Left ON the queue deliberately: popping first and then failing
            # to launch silently dropped that section.
            self.log_panel.append_log("GUI", "ERROR", str(exc))
            return
        self._queue.pop(0)
        self._ind_queue.set_value(len(self._queue))
        self._sync_buttons()

    # There is no "Scan Sections" button. Analysis populates the
    # acquisition-derived columns itself (score_region calls
    # section_facts.load_or_collect), so a separate scan only ever helped for
    # sections nobody intended to analyze - not worth a button nobody could
    # interpret. `python -m standalone_qc --scan` still exists for headless
    # use.

    def _on_score_selected(self) -> None:
        # force=True: naming specific rows IS the instruction to analyze them.
        # Without this the CLI's ledger filter silently dropped every section
        # that had been analyzed before, so selecting all 24 rows and pressing
        # this button re-analyzed only the ones that happened to be new - and
        # the operator's only clue was that the batch report still showed old
        # numbers. "Analyze All Pending" keeps the filter; that is what
        # "pending" means there.
        self._start_queue(
            [r for r in self._selected_regions() if r.is_scorable], force=True
        )

    def _on_score_all(self) -> None:
        self._start_queue(self._pending_regions())

    # === modes ============================================================
    def _on_mode_changed(self) -> None:
        mode = self._mode.currentData()
        if mode != "realtime" and self._watching:
            self._stop_watching()
        # Leaving service mode stops the service: a running agent while the
        # window says "Post-acquisition" would take requests nobody at this
        # machine can see being served.
        if mode != "service" and self._agent.is_running():
            self._stop_service()
        realtime = mode == "realtime"
        service = mode == "service"
        self._watch_interval_label.setVisible(realtime)
        self._watch_interval.setVisible(realtime)
        self._control_row.setVisible(service)
        label = self.layout_label_for(self._control_row)
        if label is not None:
            label.setVisible(service)
        self._set_mode_hint()
        self._sync_buttons()

    def _set_mode_hint(self) -> None:
        if self._mode.currentData() == "realtime":
            self.statusBar().showMessage(
                "Realtime: Start Watching, and each section is analyzed as it "
                "finishes imaging."
            )
        else:
            self.statusBar().showMessage(
                "Post-acquisition: the retake manager is ready now; analyze "
                "sections to fill in its quality columns."
            )

    def _on_toggle_watch(self) -> None:
        if self._watching:
            self._stop_watching()
        else:
            self._start_watching()

    # === service mode =====================================================
    def _on_toggle_service(self) -> None:
        if self._agent.is_running():
            self._stop_service()
        else:
            self._start_service()

    def _start_service(self) -> None:
        root = self._control_root.text().strip()
        if not root:
            QMessageBox.information(
                self, "No control folder",
                "Set the Control folder first: the folder on the share "
                "that the acquisition computer writes its requests "
                "into. Use a UNC path (\\\\server\\share\\...), not a "
                "drive letter - this computer sees the share as Z: and "
                "the acquisition computer as V:, so a Z: path written "
                "here means nothing over there.",
            )
            return
        args = ["--control-root", root]
        try:
            self._agent.start(args, module="standalone_qc.agent")
        except RuntimeError as exc:
            self.log_panel.append_log("Agent", "ERROR", str(exc))
            return
        self.log_panel.append_log(
            "Agent", "INFO",
            f"service started - taking requests from {root}. Leave this "
            f"window open to keep it running.")
        self._ind_state.set_value("service", "ok")
        self._sync_buttons()

    def _stop_service(self) -> None:
        if not self._agent.is_running():
            return
        self._agent.stop()
        self.log_panel.append_log("Agent", "INFO", "service stopped")
        self._sync_buttons()

    def _on_agent_finished(self, code: int) -> None:
        if code not in (0, 62097, 15):        # 15/terminate is a normal stop
            self.log_panel.append_log(
                "Agent", "WARNING",
                f"service exited with code {code} - it is no longer taking "
                f"requests from the acquisition computer.")
        self._ind_state.set_value("idle")
        self._sync_buttons()

    def _start_watching(self) -> None:
        if self._resolved_root is None:
            QMessageBox.information(
                self, "No image directory",
                "Set the Image Directory first so there is something to watch.",
            )
            return
        self._watching = True
        self._watch_timer.start(max(15, self._watch_interval.value()) * 1000)
        self._ind_state.set_value("watching", "warn")
        self.log_panel.append_log(
            "GUI", "INFO",
            f"watching {self._resolved_root} every "
            f"{self._watch_interval.value()}s for sections that finish imaging",
        )
        self._sync_buttons()
        self._on_watch_tick()

    def _stop_watching(self) -> None:
        self._watching = False
        self._watch_timer.stop()
        self._ind_state.set_value("idle")
        self.log_panel.append_log("GUI", "INFO", "stopped watching")
        self._sync_buttons()

    def _on_watch_tick(self) -> None:
        """One poll: re-read the manifests, and queue anything that has newly
        finished imaging. Skipped while a scoring run is still going, so a
        slow section can never pile up a backlog of overlapping runs."""
        if not self._watching or self._runner.is_running() or self._queue:
            return
        self.refresh()
        pending = self._pending_regions(skip_failed=True)
        if not pending:
            # Report a section that failed once, so it is visible rather than
            # silently skipped - but only the first time we notice it.
            for key in self._failed_keys():
                if key not in self._reported_failures:
                    self._reported_failures.add(key)
                    self.log_panel.append_log(
                        "GUI", "WARNING",
                        f"{key} was attempted and did not analyze - not "
                        f"retrying automatically. Select it and use "
                        f"'Analyze Selected' to try again.",
                    )
            return
        self.log_panel.append_log(
            "GUI", "INFO",
            f"{len(pending)} section(s) finished imaging - queueing",
        )
        self._start_queue(pending, announce=False)

    # === runner signals ===================================================
    def _on_runner_line(self, text: str) -> None:
        if not text.strip():
            return
        level = "INFO"
        low = text.lower()
        if "error" in low or "failed" in low or "traceback" in low:
            level = "ERROR"
        elif "warning" in low:
            level = "WARNING"
        self.log_panel.append_log("StandaloneQC", level, text)

    def _on_phase(self, name: str, detail: str) -> None:
        import time

        self._phase_name = name
        self._phase_started = time.monotonic()
        self._ind_phase.set_value(name, "warn")
        self._phase_timer.start()
        self.log_panel.append_log(
            "GUI", "INFO", f"{name}" + (f" — {detail}" if detail else ""))

    def _tick_phase(self) -> None:
        import time

        if not self._phase_name or not self._runner.is_running():
            self._phase_timer.stop()
            return
        secs = int(time.monotonic() - self._phase_started)
        mins, rem = divmod(secs, 60)
        elapsed = f"{mins}m {rem:02d}s" if mins else f"{rem}s"
        self._ind_phase.set_value(f"{self._phase_name}  ({elapsed})", "warn")

    def _on_progress(self, done: int, total: int, label: str) -> None:
        self._progress.setMaximum(max(1, total))
        self._progress.setValue(done)
        self._progress.setFormat(f"{label}  —  %v/%m (%p%)")

    def _on_section_started(self, key: str) -> None:
        self._ind_current.set_value(key.split("/")[-1])
        self._ind_state.set_value("analyzing")
        self.statusBar().showMessage(f"Analyzing {key}…")

    def _on_section_result(self, result: Dict[str, Any]) -> None:
        self._ran_any = True
        self._ind_last_q.set_value(result.get("median_q", "—"))
        outliers = result.get("outlier_beams")
        self._ind_outliers.set_value(outliers or "none", "warn" if outliers else "ok")
        bits = [
            f"{result.get('n_mfovs')} mFoVs",
            f"{result.get('n_scored')} scored",
            f"median quality {result.get('median_q')}",
            f"{result.get('elapsed_s')}s",
        ]
        if outliers:
            bits.append(f"outlier beam(s) {outliers}")
        self.log_panel.append_log(
            "GUI", "WARNING" if outliers else "INFO",
            f"{result.get('key')}: " + ", ".join(bits),
        )

    def _on_run_finished(self, code: int) -> None:
        self._phase_timer.stop()
        self._phase_name = ""
        self._ind_phase.set_value("—")
        if code != 0:
            self.log_panel.append_log(
                "GUI", "ERROR", f"run exited with code {code}"
            )
            if code == -1:
                self._queue.clear()
        self._sync_buttons()
        self._next_job()

    # === opening things ===================================================
    def _report_path(self, region: RegionRef) -> Optional[Path]:
        subdir = str(load_config(None).get("output_subdir", "StandaloneQC"))
        path = region.region_dir / subdir / "standalone_qc_report.html"
        return path if path.is_file() else None

    def _on_open_report(self) -> None:
        sel = self._selected_regions()
        if len(sel) != 1:
            return
        path = self._report_path(sel[0])
        if path is None:
            QMessageBox.information(
                self, "No report yet",
                f"{sel[0].name} has no standalone QC report yet. Analyze it first.",
            )
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))

    def _rebuild_reports(self, quiet: bool = False) -> Optional[Path]:
        """Roll the per-section results up into the batch and run-level
        reports. Done in-process rather than by the subprocess: it reads only
        the small summary JSONs (no image data), so it costs milliseconds, and
        doing it here means the reports are refreshed exactly once per queue
        — and still get refreshed if the operator hits Stop partway.

        quiet=True on the Refresh path: rebuilding is routine there and
        logging two lines every time would drown the log an operator actually
        needs to read during a run.
        """
        if self._resolved_root is None or not self._regions:
            return None
        try:
            written = rollup.write_all_reports(self._resolved_root, self._build_cfg())
        except Exception as exc:
            self.log_panel.append_log("GUI", "ERROR", f"report rebuild failed: {exc}")
            return None
        if not quiet:
            for name, path in written.items():
                label = (
                    "retake manager" if name == "__global__"
                    else f"batch report ({name})"
                )
                self.log_panel.append_log("GUI", "INFO", f"{label}: {path}")
        return written.get("__global__")

    def _on_open_run_report(self) -> None:
        if self._resolved_root is None:
            return
        path = self._resolved_root / rollup.GLOBAL_REPORT_NAME
        if not path.is_file():
            path = self._rebuild_reports() or path
        if not path.is_file():
            QMessageBox.information(
                self, "No run report yet",
                "Nothing has been analyzed under this run yet, so there is "
                "nothing to report on.",
            )
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))

    def _open_batch_report(self, batch_dir: Path) -> None:
        """Open one batch's retake manager. Reached from the Open Retake
        Manager menu, which lists every batch present."""
        path = batch_dir / rollup.BATCH_REPORT_NAME
        if not path.is_file():
            self._rebuild_reports()
        if not path.is_file():
            QMessageBox.information(
                self, "No batch report yet",
                f"Nothing under {batch_dir.name} has been analyzed yet, so "
                f"there is nothing to report on.",
            )
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))

    def _on_open_folder(self) -> None:
        sel = self._selected_regions()
        if len(sel) != 1:
            return
        target = sel[0].region_dir
        if not target.is_dir():
            QMessageBox.information(
                self, "Folder missing",
                f"{target} does not exist — this section was never imaged.",
            )
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(target)))

    def _on_browse_control(self) -> None:
        chosen = QFileDialog.getExistingDirectory(
            self, "Select the shared control folder",
            self._control_root.text().strip() or self._root_edit.text().strip(),
        )
        if chosen:
            self._control_root.setText(chosen)

    def _on_browse_imaging_json(self) -> None:
        start = self._imaging_json.text().strip() or self._root_edit.text().strip()
        path, _ = QFileDialog.getOpenFileName(
            self, "Select the imaging JSON this batch was acquired from",
            start, "JSON files (*.json);;All files (*)",
        )
        if path:
            self._imaging_json.setText(path)

    def _on_browse(self) -> None:
        chosen = QFileDialog.getExistingDirectory(
            self, "Select the image directory (a batch folder)",
            self._root_edit.text().strip() or "",
        )
        if chosen:
            self._root_edit.setText(chosen)
            self.refresh()
