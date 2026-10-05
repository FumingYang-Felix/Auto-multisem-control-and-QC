"""
SFN (Shuttle-and-Find) page.

Two stages, both reusing existing infra:

  A) Calibration — launches auto_stage_calib.py as a SUBPROCESS (via the same
     PipelineRunner the acquisition uses) so it can drive the scope to the 3
     L-markers, detect them, and write an affine.json {a11,a12,a21,a22,tx,ty}.
     auto_stage_calib is argparse-driven (NOT MSEM_CONFIG_JSON), so we pass an
     argv list through PipelineRunner.start(args=...). Hardware ownership is
     handed off exactly like HI-MC (pause/resume the engine).

  B) Apply — runs apply_affine_to_section_json.py --all_regions SYNCHRONOUSLY
     (pure file IO, no scope) to apply that affine to EVERY region of the old
     imaging JSON, producing a new JSON that can be pushed straight into the
     HI-MC page's imaging-JSON field.
"""
from __future__ import annotations

import os
import re
import time
from pathlib import Path

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QDoubleSpinBox, QFileDialog, QFormLayout, QFrame,
    QGroupBox, QHBoxLayout, QLabel, QLineEdit, QMessageBox, QPushButton, QScrollArea,
    QSpinBox, QVBoxLayout, QWidget,
)

from .base_page import BasePage
from ..widgets import LogPanel

# auto_stage_calib.DEFAULT_PROJ (per-marker reset projective setpoints)
_DEFAULT_PROJ = {"M1": (-585.156, 649.869), "M2": (-556.728, 662.229), "M3": (-693.381, 711.415)}

# Wafer-transfer -> holder-markers-JSON auto-lookup (see _on_load_markers_from_wafer_transfer).
_WAFER_TRANSFER_DIR = r"D:\Users\Vlad\Wafer_Transfer_files"
_HOLDER_JSON_DIR = r"D:\Users\Vlad\Holder_json_files"
_HOLDER_NUM_RE = re.compile(r"wafer\s+holder\s+number\s*=\s*(\d+)", re.IGNORECASE)
_WAFER_WATCH_POLL_MS = 15 * 60 * 1000  # 15 min -- wafer transfers happen on an hours-long cadence


class SfnPage(BasePage):

    # -- construction -------------------------------------------------------
    def setup_ui(self):
        self._stage_init_runner = None
        self._calib_runner = None
        self._apply_runner = None
        self._affine_path = None
        self._himc_page = None
        self._new_json_path = None
        self._stage_err_monitor = None
        self._log_err_seen = False
        self._stage_init_retry_used = False
        self._stage_init_exit_code = 0
        self._stage_init_user_stopped = False
        self._status_panel = None

        # -- new-wafer-transfer detection (see _poll_wafer_transfer_dir) --
        self._wafer_watch_initialized = False
        self._wafer_watch_known_files = set()
        self._wafer_watch_pending_path = None
        self._new_wafer_procedure_active = False

        title = QLabel("SFN — Shuttle-and-Find (stage re-registration)")
        title.setStyleSheet("font-size: 18px; font-weight: 600; padding: 0 0 6px 0;")
        self.layout.addWidget(title)
        hint = QLabel(
            "Stage A re-finds the 3 L-markers on the scope and computes the affine "
            "(new = A·old + t). Stage B applies it to ALL regions of the old imaging "
            "JSON, producing a re-registered JSON to feed HI-MC.")
        hint.setWordWrap(True)
        hint.setStyleSheet("color: #9aa0a6; padding-bottom: 8px;")
        self.layout.addWidget(hint)

        self.lbl_sim_banner = QLabel()
        self.lbl_sim_banner.setWordWrap(True)
        self.lbl_sim_banner.setStyleSheet(
            "color: #ffd54a; background-color: rgba(204,167,0,0.18); font-weight: 600; "
            "padding: 6px 10px; border: 1px solid #cca700; border-radius: 4px;")
        self.lbl_sim_banner.setVisible(False)
        self.layout.addWidget(self.lbl_sim_banner)
        self._sim_banner_timer = QTimer(self)
        self._sim_banner_timer.timeout.connect(self._refresh_sim_banner)
        self._sim_banner_timer.start(1000)
        self._refresh_sim_banner()

        self._wafer_watch_timer = QTimer(self)
        self._wafer_watch_timer.timeout.connect(self._poll_wafer_transfer_dir)
        self._wafer_watch_timer.start(_WAFER_WATCH_POLL_MS)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        host = QWidget()
        col = QVBoxLayout(host)
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(10)
        col.addWidget(self._build_stage_init_group())
        col.addWidget(self._build_calib_group())
        col.addWidget(self._build_proj_group())
        col.addWidget(self._build_apply_group())
        col.addStretch(1)
        scroll.setWidget(host)
        self.layout.addWidget(scroll, stretch=3)

        self.layout.addLayout(self._build_action_row())
        self.log_panel = LogPanel()
        self.layout.addWidget(self.log_panel, stretch=2)

    # -- builders -----------------------------------------------------------
    def _group(self, title):
        box = QGroupBox(title)
        form = QFormLayout(box)
        form.setLabelAlignment(Qt.AlignRight)
        form.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)
        return box, form

    def _file_row(self, edit, handler, directory=False):
        row = QWidget()
        h = QHBoxLayout(row)
        h.setContentsMargins(0, 0, 0, 0)
        h.addWidget(edit, stretch=1)
        btn = QPushButton("Browse…")
        btn.clicked.connect(handler)
        h.addWidget(btn)
        return row

    def _build_stage_init_group(self):
        box, form = self._group("Stage 0 — Calibrate Stage (CMD_STAGE_INIT)")
        hint = QLabel(
            "Re-homes the stage: moves to the lower limits, re-calibrates its "
            "coordinate system, then moves to (0,0,0). Run this once before "
            "Stage A if the stage coordinate system needs re-init.")
        hint.setWordWrap(True)
        hint.setStyleSheet("color: #9aa0a6;")
        form.addRow(hint)

        self.edit_log_dir = QLineEdit(r"X:\ProgramData\Carl Zeiss\mSEM")
        self.edit_log_dir.setPlaceholderText(
            r"mSEM service log folder (mapped/UNC path to the scope PC), e.g. X:\ProgramData\Carl Zeiss\mSEM")
        form.addRow("Service log dir:", self.edit_log_dir)
        log_hint = QLabel(
            "While Stage Init / Calibration is running, this folder's mSEMService.log / ErrorHandler.log "
            "are tailed for stage/calibration ERRORs, printed live to the console.")
        log_hint.setWordWrap(True)
        log_hint.setStyleSheet("color: #9aa0a6;")
        form.addRow(log_hint)
        return box

    def _build_calib_group(self):
        box, form = self._group("Stage A — Calibration (drives the scope)")
        self.edit_markers = QLineEdit()
        self.edit_markers.setPlaceholderText("markers_prev.json from a prior round (required for a real run)")
        form.addRow("Markers JSON:", self._file_row(self.edit_markers, self._browse_markers))
        btn_wafer_transfer = QPushButton("↻  Load from Wafer Transfer file")
        btn_wafer_transfer.setToolTip(
            f"Reads the newest wafer_transfer_*.txt in {_WAFER_TRANSFER_DIR}, extracts the "
            f"holder number, and looks up h<N>_markers_prev.json under {_HOLDER_JSON_DIR}."
        )
        btn_wafer_transfer.clicked.connect(self._on_load_markers_from_wafer_transfer)
        form.addRow("", btn_wafer_transfer)
        self.edit_remote = QLineEdit(r"V:\Fuming\msem_calib")
        form.addRow("Remote folder:", self.edit_remote)
        self.edit_outdir = QLineEdit(r"V:\Fuming\msem_calib\outputs")
        form.addRow("Output dir:", self.edit_outdir)

        self.spin_scanrate = QSpinBox(); self.spin_scanrate.setRange(1, 30); self.spin_scanrate.setValue(1)
        form.addRow("Scan rate:", self.spin_scanrate)
        self.spin_pixel = QDoubleSpinBox(); self.spin_pixel.setRange(0.1, 10000.0); self.spin_pixel.setDecimals(2)
        self.spin_pixel.setValue(8.0); self.spin_pixel.setSuffix(" nm")
        form.addRow("Pixel size:", self.spin_pixel)
        self.spin_width = QSpinBox(); self.spin_width.setRange(1, 100000); self.spin_width.setValue(2000)
        form.addRow("Tile width:", self.spin_width)
        self.spin_height = QSpinBox(); self.spin_height.setRange(1, 100000); self.spin_height.setValue(1748)
        form.addRow("Tile height:", self.spin_height)
        self.spin_overlap = QDoubleSpinBox(); self.spin_overlap.setRange(-1.0, 0.5); self.spin_overlap.setDecimals(3)
        self.spin_overlap.setValue(0.05)
        form.addRow("Overlap frac (<0 = off):", self.spin_overlap)

        self.spin_ds = QSpinBox(); self.spin_ds.setRange(1, 64); self.spin_ds.setValue(8)
        form.addRow("Downsample (ds):", self.spin_ds)
        self.spin_padding = QSpinBox(); self.spin_padding.setRange(0, 100000); self.spin_padding.setValue(80)
        form.addRow("Stitch padding (px):", self.spin_padding)
        self.spin_threshold = QSpinBox(); self.spin_threshold.setRange(0, 255); self.spin_threshold.setValue(70)
        form.addRow("Detect threshold:", self.spin_threshold)
        self.spin_morph = QSpinBox(); self.spin_morph.setRange(1, 199); self.spin_morph.setValue(11)
        form.addRow("Morph kernel:", self.spin_morph)
        self.spin_minarea = QDoubleSpinBox(); self.spin_minarea.setRange(0.0, 1e9); self.spin_minarea.setDecimals(0)
        self.spin_minarea.setValue(150000.0)
        form.addRow("Min contour area:", self.spin_minarea)

        self.chk_y_flip = QCheckBox("y_flip"); form.addRow(self.chk_y_flip)
        self.chk_no_b2f = QCheckBox("no_b2f (skip beams-to-fibers)"); form.addRow(self.chk_no_b2f)
        self.chk_no_af = QCheckBox("no_af (skip autofocus)"); form.addRow(self.chk_no_af)
        self.spin_b2f_bound = QDoubleSpinBox(); self.spin_b2f_bound.setRange(0.0, 10000.0); self.spin_b2f_bound.setValue(100.0)
        form.addRow("B2F bound:", self.spin_b2f_bound)
        return box

    def _build_proj_group(self):
        box, form = self._group("Per-marker default projective (B2F reset fallback)")
        self.proj_spins = {}
        for m in ("M1", "M2", "M3"):
            px0, py0 = _DEFAULT_PROJ[m]
            row = QWidget(); h = QHBoxLayout(row); h.setContentsMargins(0, 0, 0, 0)
            sx = QDoubleSpinBox(); sx.setRange(-100000.0, 100000.0); sx.setDecimals(3); sx.setValue(px0)
            sy = QDoubleSpinBox(); sy.setRange(-100000.0, 100000.0); sy.setDecimals(3); sy.setValue(py0)
            h.addWidget(QLabel("X")); h.addWidget(sx, 1); h.addWidget(QLabel("Y")); h.addWidget(sy, 1)
            self.proj_spins[m] = (sx, sy)
            form.addRow(f"{m}:", row)
        return box

    def _build_apply_group(self):
        box, form = self._group("Stage B — Apply affine to ALL regions (no scope)")
        self.edit_affine_in = QLineEdit()
        self.edit_affine_in.setPlaceholderText("affine.json {a11..ty} (auto-filled after Stage A)")
        form.addRow("Affine JSON:", self._file_row(self.edit_affine_in, self._browse_affine))
        self.edit_old_json = QLineEdit()
        self.edit_old_json.setPlaceholderText("old imaging JSON (auto-filled from Load JSON)")
        form.addRow("Old imaging JSON:", self._file_row(self.edit_old_json, self._browse_old_json))
        self.edit_new_json = QLineEdit()
        self.edit_new_json.setPlaceholderText("output JSON (default: <old>_sfn.json)")
        form.addRow("New imaging JSON:", self._file_row(self.edit_new_json, self._browse_new_json))
        self.chk_force = QCheckBox("force (re-apply even if AffineApplied present)")
        form.addRow(self.chk_force)
        return box

    def _build_action_row(self):
        row = QHBoxLayout()
        self.btn_new_wafer = QPushButton("🆕  Start New Wafer Procedure")
        self.btn_new_wafer.setToolTip(
            "Enabled once a new wafer_transfer_*.txt file is detected. Runs: unselect Simulation Mode, "
            "connect to the scope, turn the beam on, load the holder's markers JSON, calibrate the stage, "
            "then automatically run Shuttle-and-Find calibration.")
        self.btn_new_wafer.setEnabled(False)
        self.btn_new_wafer.clicked.connect(self._on_start_new_wafer_procedure)
        row.addWidget(self.btn_new_wafer)
        row.addSpacing(12)
        self.btn_beam_on = QPushButton("⚡  Beam ON")
        self.btn_beam_on.setToolTip("CMD_BEAM_ON: turn on the electron beam (blocks until DP_IS_BEAM_ON confirms).")
        self.btn_beam_on.clicked.connect(self._on_beam_on)
        self.btn_stage_init = QPushButton("⟲  Calibrate Stage")
        self.btn_stage_init.clicked.connect(self._on_stage_init)
        self.btn_calibrate = QPushButton("▶  Run Shuttle and Find Calibration")
        self.btn_calibrate.clicked.connect(self._on_calibrate)
        self.btn_stop = QPushButton("■  Stop")
        self.btn_stop.clicked.connect(self._on_stop)
        self.btn_stop.setEnabled(False)
        self.btn_apply = QPushButton("Apply affine → all regions")
        self.btn_apply.clicked.connect(self._on_apply)
        self.btn_send = QPushButton("Send new JSON → HI-MC")
        self.btn_send.clicked.connect(self._on_send_himc)
        self.btn_send.setEnabled(False)
        row.addWidget(self.btn_beam_on)
        row.addSpacing(12)
        row.addWidget(self.btn_stage_init)
        row.addSpacing(12)
        row.addWidget(self.btn_calibrate)
        row.addWidget(self.btn_stop)
        row.addSpacing(12)
        row.addWidget(self.btn_apply)
        row.addWidget(self.btn_send)
        row.addStretch(1)
        return row

    # -- external wiring ----------------------------------------------------
    def set_imaging_json_path(self, path: str):
        if path:
            self.edit_old_json.setText(path)
            if not self.edit_new_json.text().strip():
                stem, ext = os.path.splitext(path)
                self.edit_new_json.setText(f"{stem}_sfn{ext or '.json'}")

    def set_himc_page(self, page):
        self._himc_page = page

    def set_status_panel(self, status_panel):
        self._status_panel = status_panel

    # -- browse handlers ----------------------------------------------------
    def _browse_markers(self):
        p, _ = QFileDialog.getOpenFileName(self, "markers_prev.json", "", "JSON (*.json);;All (*)")
        if p: self.edit_markers.setText(p)

    def _find_latest_wafer_transfer_file(self):
        """Returns (path, error). Picks the most-recently-modified wafer_transfer_*.txt."""
        try:
            names = [f for f in os.listdir(_WAFER_TRANSFER_DIR)
                      if f.lower().startswith("wafer_transfer") and f.lower().endswith(".txt")]
        except OSError as e:
            return None, f"Could not list {_WAFER_TRANSFER_DIR}: {e}"
        if not names:
            return None, f"No wafer_transfer_*.txt files found in {_WAFER_TRANSFER_DIR}"
        names.sort(key=lambda f: os.path.getmtime(os.path.join(_WAFER_TRANSFER_DIR, f)), reverse=True)
        return os.path.join(_WAFER_TRANSFER_DIR, names[0]), None

    def _find_holder_markers_json(self, holder_num: int):
        """Searches _HOLDER_JSON_DIR (recursively) for h<holder_num>_markers_prev.json."""
        root = Path(_HOLDER_JSON_DIR)
        if not root.is_dir():
            return None
        target = f"h{holder_num}_markers_prev.json".lower()
        for p in root.rglob("*.json"):
            if p.name.lower() == target:
                return str(p)
        # Looser fallback in case the naming convention drifts (extra suffix, zero padding, etc.).
        loose = re.compile(rf"^h0*{holder_num}[_\-].*markers.*prev.*\.json$", re.IGNORECASE)
        for p in root.rglob("*.json"):
            if loose.match(p.name):
                return str(p)
        return None

    def _on_load_markers_from_wafer_transfer(self):
        path, err = self._find_latest_wafer_transfer_file()
        if err:
            self.log_panel.append_log("SFN", "ERROR", err); return
        self._load_markers_from_wafer_transfer_file(path)

    def _load_markers_from_wafer_transfer_file(self, path: str) -> bool:
        """Parses 'wafer holder number = N' out of `path` and, if found, locates
        h<N>_markers_prev.json and fills self.edit_markers. Returns success."""
        try:
            text = Path(path).read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            self.log_panel.append_log("SFN", "ERROR", f"Could not read {path}: {e}"); return False
        m = _HOLDER_NUM_RE.search(text)
        if not m:
            self.log_panel.append_log(
                "SFN", "ERROR", f"Could not find 'wafer holder number = N' in {path}")
            return False
        holder_num = int(m.group(1))
        self.log_panel.append_log("SFN", "INFO", f"Wafer transfer file: {path} -> holder {holder_num}")
        json_path = self._find_holder_markers_json(holder_num)
        if not json_path:
            self.log_panel.append_log(
                "SFN", "ERROR",
                f"No h{holder_num}_markers_prev.json found under {_HOLDER_JSON_DIR}")
            return False
        self.edit_markers.setText(json_path)
        self.log_panel.append_log(
            "SFN", "SUCCESS", f"Loaded markers JSON for holder {holder_num}: {json_path}")
        return True

    # -- new-wafer-transfer detection + one-click procedure ------------------
    def _show_alert(self, title: str, message: str, critical: bool = False):
        (QMessageBox.critical if critical else QMessageBox.information)(self, title, message)

    def _poll_wafer_transfer_dir(self):
        try:
            names = [f for f in os.listdir(_WAFER_TRANSFER_DIR)
                      if f.lower().startswith("wafer_transfer") and f.lower().endswith(".txt")]
        except OSError:
            return  # folder unreachable (e.g. network drive hiccup) -- try again next poll

        if not self._wafer_watch_initialized:
            # Baseline on first poll so files already present at app-start don't
            # fire a stale alert -- only genuinely NEW arrivals should.
            self._wafer_watch_known_files = set(names)
            self._wafer_watch_initialized = True
            return

        new_names = [n for n in names if n not in self._wafer_watch_known_files]
        if not new_names:
            return
        self._wafer_watch_known_files.update(names)
        new_names.sort(key=lambda f: os.path.getmtime(os.path.join(_WAFER_TRANSFER_DIR, f)), reverse=True)
        path = os.path.join(_WAFER_TRANSFER_DIR, new_names[0])
        self._wafer_watch_pending_path = path
        self.btn_new_wafer.setEnabled(True)
        msg = f"New wafer transfer file detected: {path}"
        self.log_panel.append_log("SFN", "INFO", msg)
        self._show_alert(
            "New Wafer Detected",
            f"{msg}\n\nClick 'Start New Wafer Procedure' to unselect Simulation Mode, connect to the "
            f"scope, turn the beam on, load this holder's markers JSON, and calibrate the stage.")

    def _fail_new_wafer_procedure(self, reason: str):
        err = f"New wafer procedure stopped: {reason}"
        print(f"[SFN] ERROR: {err}", flush=True)
        self.log_panel.append_log("SFN", "ERROR", err)
        self.btn_new_wafer.setEnabled(self._wafer_watch_pending_path is not None)
        self._new_wafer_procedure_active = False
        self._show_alert("New Wafer Procedure Failed", err + "\n\nInvestigate manually.", critical=True)

    def _finish_new_wafer_procedure(self, success: bool, detail: str):
        # Called once the async chain launched by _on_start_new_wafer_procedure
        # settles: either a stage-init failure, or -- on stage-init success, which
        # auto-chains into Run Shuttle and Find Calibration -- that calibration's
        # own completion/error handlers.
        if not self._new_wafer_procedure_active:
            return
        self._new_wafer_procedure_active = False
        if success:
            self._show_alert("New Wafer Procedure Complete",
                              f"Stage calibration succeeded.\n{detail}")
        else:
            self._show_alert("New Wafer Procedure Failed",
                              f"Stage calibration failed.\n{detail}\n\nInvestigate manually.",
                              critical=True)

    def _on_start_new_wafer_procedure(self):
        path = self._wafer_watch_pending_path
        if not path:
            self.log_panel.append_log("SFN", "WARNING", "No new wafer transfer file pending."); return
        if self.engine is None:
            self._fail_new_wafer_procedure("No engine connected."); return
        if self.engine.is_paused():
            self._fail_new_wafer_procedure(
                "Another run owns the scope (engine paused). Stop it first."); return
        if ((self._stage_init_runner is not None and self._stage_init_runner.is_running())
                or (self._calib_runner is not None and self._calib_runner.is_running())):
            self._fail_new_wafer_procedure("Stage init or calibration is already running."); return

        self.btn_new_wafer.setEnabled(False)
        self.log_panel.append_log("SFN", "INFO", f"Starting new wafer procedure for {path}…")

        # 1) unselect Simulation Mode
        self.engine.simulation_mode = False
        if self._status_panel is not None:
            try:
                self._status_panel.chk_simulation.setChecked(False)
            except Exception:
                pass
        self._refresh_sim_banner()
        self.log_panel.append_log("SFN", "INFO", "Simulation Mode turned OFF.")

        # 2) connect to the scope if not already connected
        try:
            if not self.engine.is_connected():
                self.log_panel.append_log("SFN", "INFO", "Connecting to the scope…")
                QApplication.processEvents()
                if not self.engine.connect():
                    self._fail_new_wafer_procedure("Connect to the scope failed."); return
            self.log_panel.append_log("SFN", "SUCCESS", "Connected to the scope.")
        except Exception as e:
            self._fail_new_wafer_procedure(f"Connect to the scope raised: {e}"); return

        # 3) beam on
        QApplication.processEvents()
        if not self._ensure_beam_on():
            self._fail_new_wafer_procedure("Beam ON failed."); return

        # 4) load this holder's markers JSON
        if not self._load_markers_from_wafer_transfer_file(path):
            self._fail_new_wafer_procedure("Could not load holder markers JSON."); return

        # 5) calibrate stage (async). On success this chains straight into
        #    Run Shuttle and Find Calibration (see _finalize_stage_init_run's
        #    success branch); the overall outcome is reported by
        #    _finish_new_wafer_procedure once THAT settles.
        self._wafer_watch_pending_path = None
        self._new_wafer_procedure_active = True
        self.log_panel.append_log("SFN", "INFO", "Launching stage calibration (CMD_STAGE_INIT)…")
        self._stage_init_retry_used = False
        self._launch_stage_init_process()

    def _browse_affine(self):
        p, _ = QFileDialog.getOpenFileName(self, "affine.json", "", "JSON (*.json);;All (*)")
        if p: self.edit_affine_in.setText(p)

    def _browse_old_json(self):
        p, _ = QFileDialog.getOpenFileName(self, "old imaging JSON", "", "JSON (*.json);;All (*)")
        if p: self.set_imaging_json_path(p)

    def _browse_new_json(self):
        p, _ = QFileDialog.getSaveFileName(self, "output JSON", "", "JSON (*.json);;All (*)")
        if p: self.edit_new_json.setText(p)

    # -- Simulation-mode guard ----------------------------------------------
    def _in_simulation(self) -> bool:
        return bool(self.engine is not None and getattr(self.engine, "simulation_mode", False))

    def _refresh_sim_banner(self):
        self.lbl_sim_banner.setVisible(self._in_simulation())
        if self._in_simulation():
            self.lbl_sim_banner.setText(
                "⚠ SIMULATION MODE — Beam ON, Calibrate Stage, and Run Shuttle and Find Calibration are "
                "BLOCKED on this page. No commands are sent to the microscope.")

    def _alert_simulation_blocked(self, action: str) -> None:
        msg = f"SIMULATION MODE is ON — {action} was NOT sent to the microscope."
        print(f"[SFN] SIMULATION: {msg}", flush=True)
        self.log_panel.append_log("SIMULATION", "WARNING", msg)

    # -- Stage/calibration error log monitor (console only) -----------------
    def _start_stage_err_monitor(self):
        if self._stage_err_monitor is not None:
            return
        log_dir = self.edit_log_dir.text().strip()
        if not log_dir:
            self.log_panel.append_log("SFN", "WARNING", "No service log dir set — skipping error log monitor.")
            return
        try:
            from stage_calib_error_monitor import StageCalibErrorMonitor

            def _on_error(evt):
                self._log_err_seen = True
                print(f"[StageCalibErrorMonitor] {evt.describe()}", flush=True)

            self._stage_err_monitor = StageCalibErrorMonitor(on_error=_on_error, log_dir=log_dir)
            self._stage_err_monitor.start()
            self.log_panel.append_log("SFN", "INFO", f"Watching mSEM service log for stage/calib errors: {log_dir}")
        except Exception as e:
            self.log_panel.append_log("SFN", "WARNING", f"Could not start service-log error monitor: {e}")
            self._stage_err_monitor = None

    def _stop_stage_err_monitor(self):
        if self._stage_err_monitor is not None:
            self._stage_err_monitor.stop()
            self._stage_err_monitor = None

    # -- Beam ON (CMD_BEAM_ON) ----------------------------------------------
    def _read_beam_on(self, conn) -> bool:
        # DP_IS_BEAM_ON: 1 = on, 0 = off, 0.5 = in-progress/transitioning (per
        # beam_arc_recovery.py) -- 0.5 is NOT "on", so this must be >= 0.99,
        # not >= 0.5 (a stuck-at-0.5 beam was previously misread as "already
        # ON" and silently skipped, when it never actually was).
        try:
            val = float(conn.get_double("DP_IS_BEAM_ON"))
            self.log_panel.append_log("SFN", "INFO", f"DP_IS_BEAM_ON = {val}")
            return val >= 0.99
        except Exception as e:
            self.log_panel.append_log("SFN", "WARNING", f"Could not read DP_IS_BEAM_ON: {e}")
            return False

    def _ensure_beam_on(self) -> bool:
        """Check DP_IS_BEAM_ON and turn the beam on if it's off.

        Tries a plain CMD_BEAM_ON first. If DP_IS_BEAM_ON still reads OFF
        afterward -- for any reason, most commonly an HV/beam-clearance
        interlock tripped by an arc event -- automatically escalates to the
        full Chamber-reset-EHT-equivalent recovery
        (beam_arc_recovery.recover_from_arc: ramp beam+HV off -> wait for
        clearance -> beam on -> re-align beams-to-fibers). Escalating on
        observed failure (rather than pre-guessing the cause from a live
        status variable) means this doesn't depend on correctly predicting
        why the beam is off. That recovery sequence can take up to several
        minutes in the worst case, during which this (synchronous) call will
        make the UI look unresponsive -- that's expected, not a hang.

        Returns True once the beam is confirmed on, False on failure (already
        logged). Shared by the manual "Beam ON" button and the pre-flight
        check before Run Shuttle and Find Calibration."""
        if self.engine is None:
            self.log_panel.append_log("SFN", "ERROR", "No engine connected."); return False
        if self.engine.is_paused():
            self.log_panel.append_log("SFN", "WARNING",
                                      "Another run owns the scope (engine paused). Stop it first."); return False
        conn = self.engine.get_msem_connection()
        if conn is None:
            self.log_panel.append_log("SFN", "ERROR", "Not connected to the scope."); return False

        try:
            if self._read_beam_on(conn):
                self.log_panel.append_log("SFN", "INFO", "Beam already ON.")
                return True

            self.log_panel.append_log("SFN", "INFO", "Beam is OFF — turning it ON (CMD_BEAM_ON)…")
            conn.execute("CMD_BEAM_ON")
            conn.execute("CMD_WAIT_BEAM_ON")

            if self._read_beam_on(conn):
                self.log_panel.append_log("SFN", "SUCCESS", "Beam is ON.")
                return True

            self.log_panel.append_log(
                "SFN", "WARNING",
                "CMD_BEAM_ON did not bring the beam on — this usually means an HV/beam-clearance "
                "interlock is tripped (e.g. an arc event). Escalating to automated recovery "
                "(ramp HV off -> wait for clearance -> beam on -> re-align)…")
            return self._recover_beam_from_arc(conn)
        except Exception as e:
            self.log_panel.append_log("SFN", "ERROR", f"Beam ON failed: {e}")
            return False

    def _recover_beam_from_arc(self, conn) -> bool:
        self.log_panel.append_log(
            "SFN", "INFO",
            "Running automated beam recovery. This can take a few minutes; "
            "the UI may look unresponsive meanwhile.")
        try:
            from beam_arc_recovery import recover_from_arc
        except Exception as e:
            self.log_panel.append_log("SFN", "ERROR", f"Could not load beam_arc_recovery: {e}")
            return False

        def _log_cb(msg: str) -> None:
            print(f"[SFN] {msg}", flush=True)
            self.log_panel.append_log("SFN", "INFO", msg)

        try:
            result = recover_from_arc(conn, log=_log_cb)
        except Exception as e:
            self.log_panel.append_log("SFN", "ERROR", f"Automated beam recovery raised: {e}")
            return False

        if not result.ok:
            self.log_panel.append_log("SFN", "ERROR", f"Automated beam recovery failed: {result.detail}")
            return False
        self.log_panel.append_log("SFN", "SUCCESS", f"Beam recovered: {result.detail}")
        return True

    def _on_beam_on(self):
        if self._in_simulation():
            self._alert_simulation_blocked("Beam ON (CMD_BEAM_ON)"); return
        self.btn_beam_on.setEnabled(False)
        QApplication.processEvents()
        try:
            self._ensure_beam_on()
        finally:
            self.btn_beam_on.setEnabled(True)

    # -- Stage 0: stage init (CMD_STAGE_INIT) -------------------------------
    def _on_stage_init(self):
        if self._in_simulation():
            self._alert_simulation_blocked("Stage Init (CMD_STAGE_INIT)"); return
        if self._stage_init_runner is not None and self._stage_init_runner.is_running():
            self.log_panel.append_log("SFN", "WARNING", "Stage init already running."); return
        if self._calib_runner is not None and self._calib_runner.is_running():
            self.log_panel.append_log("SFN", "WARNING", "Calibration is running — stop it first."); return
        if self.engine is not None and self.engine.is_paused():
            self.log_panel.append_log("SFN", "WARNING",
                                      "Another run owns the scope (engine paused). Stop it first."); return
        repo_root = Path(__file__).resolve().parents[3]
        if not (repo_root / "stage_init.py").is_file():
            self.log_panel.append_log("SFN", "ERROR",
                                      f"stage_init.py not found at {repo_root}."); return

        # a fresh manual click gets a fresh one-retry budget
        self._stage_init_retry_used = False
        self._launch_stage_init_process()

    def _launch_stage_init_process(self):
        from msem_control.engine.pipeline_runner import PipelineRunner
        repo_root = Path(__file__).resolve().parents[3]

        if self._stage_init_runner is None:
            self._stage_init_runner = PipelineRunner(self)
            self._stage_init_runner.log_line.connect(self._on_stage_init_log)
            self._stage_init_runner.finished.connect(self._on_stage_init_finished)
            self._stage_init_runner.error.connect(self._on_stage_init_error)

        if self.engine is not None:
            try: self.engine.pause_hardware()
            except Exception: pass
        self.btn_beam_on.setEnabled(False)
        self.btn_stage_init.setEnabled(False)
        self.btn_calibrate.setEnabled(False)
        self.btn_stop.setEnabled(True)
        self._log_err_seen = False
        self._stage_init_user_stopped = False
        self._start_stage_err_monitor()
        attempt = "retry" if self._stage_init_retry_used else "attempt"
        self.log_panel.append_log("SFN", "INFO", f"Launching stage init (CMD_STAGE_INIT) [{attempt}]…")
        ok = self._stage_init_runner.start(
            {}, workdir=str(repo_root),
            config_path=str(repo_root / "msem_sfn_stage_init_config.json"),
            script="stage_init.py", args=["stage_init.py"])
        if not ok:
            self._reset_after_calib()

    def _on_stage_init_log(self, line: str):
        low = line.lower()
        level = ("ERROR" if ("error" in low or "traceback" in low or "failed" in low)
                 else "WARNING" if ("warn" in low or "skip" in low) else "INFO")
        self.log_panel.append_log("StageInit", level, line)

    def _on_stage_init_finished(self, code: int):
        self._stage_init_exit_code = code
        # give the service-log monitor one more poll cycle so console output
        # for a trailing log line isn't cut off a fraction of a second early
        # (informational only -- the pass/fail decision below is exit-code-only).
        QTimer.singleShot(1500, self._finalize_stage_init_run)

    def _finalize_stage_init_run(self):
        code = self._stage_init_exit_code
        user_stopped = self._stage_init_user_stopped
        log_err_seen = self._log_err_seen
        had_error = (code != 0)
        self._reset_after_calib()

        if user_stopped:
            self.log_panel.append_log("SFN", "INFO", "Stage init stopped by operator.")
            self._new_wafer_procedure_active = False
            return
        if not had_error:
            self.log_panel.append_log("SFN", "SUCCESS", "Stage init complete.")
            if log_err_seen:
                # CMD_STAGE_INIT homes by deliberately driving into the lower
                # limits, which the mSEM service logs as level="ERROR" limit-
                # switch/timeout lines even on a fully successful run -- that's
                # routine telemetry, not a failure, so it doesn't gate retry.
                self.log_panel.append_log(
                    "SFN", "INFO",
                    "Note: the service log reported stage-related ERROR line(s) during this run "
                    "(commonly limit-switch/homing telemetry), but CMD_STAGE_INIT itself succeeded.")
            if self._new_wafer_procedure_active:
                self.log_panel.append_log(
                    "SFN", "INFO",
                    "Stage init succeeded — automatically starting Run Shuttle and Find Calibration…")
                self._on_calibrate()
            return

        reason = f"exit code {code}"
        if not self._stage_init_retry_used:
            self._stage_init_retry_used = True
            msg = f"Stage init failed ({reason}) — retrying once…"
            print(f"[SFN] {msg}", flush=True)
            self.log_panel.append_log("SFN", "WARNING", msg)
            self._launch_stage_init_process()
        else:
            err = f"Stage init failed again on retry ({reason}). Giving up."
            print(f"[SFN] ERROR: {err}", flush=True)
            self.log_panel.append_log("SFN", "ERROR", err)
            self._finish_new_wafer_procedure(False, err)

    def _on_stage_init_error(self, msg: str):
        # A QProcess error fires the error signal INSTEAD of finished, so the
        # stage-init path must resume hardware here too or the scope stays paused.
        user_stopped = self._stage_init_user_stopped
        self._reset_after_calib()
        if user_stopped:
            self.log_panel.append_log("SFN", "INFO", "Stage init stopped by operator.")
            self._new_wafer_procedure_active = False
            return
        if not self._stage_init_retry_used:
            self._stage_init_retry_used = True
            note = f"Stage init failed to run ({msg}) — retrying once…"
            print(f"[SFN] {note}", flush=True)
            self.log_panel.append_log("SFN", "WARNING", note)
            self._launch_stage_init_process()
        else:
            err = f"Stage init failed again on retry: {msg}. Giving up."
            print(f"[SFN] ERROR: {err}", flush=True)
            self.log_panel.append_log("SFN", "ERROR", err)
            self._finish_new_wafer_procedure(False, err)

    # -- Stage A: calibration ----------------------------------------------
    def _build_calib_argv(self, repo_root: Path):
        out_dir = self.edit_outdir.text().strip() or r"V:\Fuming\msem_calib\outputs"
        ts = time.strftime("%Y%m%d_%H%M%S")
        # force an explicit affine_out so we deterministically know the path
        self._affine_path = os.path.join(out_dir, f"affine_sfn_{ts}.json")
        argv = ["auto_stage_calib.py",
                "--remote_folder", self.edit_remote.text().strip() or r"V:\Fuming\msem_calib",
                "--out_dir", out_dir,
                "--affine_out", self._affine_path,
                "--scanrate", str(self.spin_scanrate.value()),
                "--width", str(self.spin_width.value()),
                "--height", str(self.spin_height.value()),
                "--pixel_nm", str(self.spin_pixel.value()),
                "--overlap_frac", str(self.spin_overlap.value()),
                "--ds", str(self.spin_ds.value()),
                "--padding", str(self.spin_padding.value()),
                "--threshold", str(self.spin_threshold.value()),
                "--morph_kernel", str(self.spin_morph.value()),
                "--min_area", str(self.spin_minarea.value()),
                "--b2f_bound", str(self.spin_b2f_bound.value())]
        markers = self.edit_markers.text().strip()
        if markers:
            argv += ["--markers", markers]
        if self.chk_y_flip.isChecked():
            argv.append("--y_flip")
        if self.chk_no_b2f.isChecked():
            argv.append("--no_b2f")
        if self.chk_no_af.isChecked():
            argv.append("--no_af")
        for m in ("M1", "M2", "M3"):
            sx, sy = self.proj_spins[m]
            argv += [f"--proj_{m.lower()}", str(sx.value()), str(sy.value())]
        return argv

    def _on_calibrate(self):
        if self._in_simulation():
            self._alert_simulation_blocked("Run Shuttle and Find Calibration"); return
        from msem_control.engine.pipeline_runner import PipelineRunner

        if self._calib_runner is not None and self._calib_runner.is_running():
            self.log_panel.append_log("SFN", "WARNING", "Calibration already running."); return
        if self._stage_init_runner is not None and self._stage_init_runner.is_running():
            self.log_panel.append_log("SFN", "WARNING", "Stage init is running — stop it first."); return
        if self.engine is not None and self.engine.is_paused():
            self.log_panel.append_log("SFN", "WARNING",
                                      "Another run owns the scope (engine paused). Stop it first."); return

        # pre-flight: don't burn a whole run against a beam that's off
        if self.engine is None:
            self.log_panel.append_log("SFN", "WARNING",
                                      "No engine connected — cannot verify beam state before calibration.")
        elif not self._ensure_beam_on():
            self.log_panel.append_log("SFN", "ERROR",
                                      "Aborting: could not confirm the beam is ON before "
                                      "Run Shuttle and Find Calibration."); return

        repo_root = Path(__file__).resolve().parents[3]
        if not (repo_root / "auto_stage_calib.py").is_file():
            self.log_panel.append_log("SFN", "ERROR",
                                      f"auto_stage_calib.py not found at {repo_root}."); return
        if not self.edit_markers.text().strip():
            self.log_panel.append_log("SFN", "WARNING",
                                      "No markers JSON set — the script will create a PLACEHOLDER with "
                                      "example coords that will NOT match your wafer. Supply a real one.")
        argv = self._build_calib_argv(repo_root)

        if self._calib_runner is None:
            self._calib_runner = PipelineRunner(self)
            self._calib_runner.log_line.connect(self._on_log)
            self._calib_runner.finished.connect(self._on_calib_finished)
            self._calib_runner.error.connect(self._on_error)

        if self.engine is not None:
            try: self.engine.pause_hardware()
            except Exception: pass
        self.btn_beam_on.setEnabled(False)
        self.btn_calibrate.setEnabled(False)
        self.btn_stage_init.setEnabled(False)
        self.btn_stop.setEnabled(True)
        self._start_stage_err_monitor()
        self.log_panel.append_log("SFN", "INFO", f"Launching calibration → affine: {self._affine_path}")
        ok = self._calib_runner.start(
            {}, workdir=str(repo_root),
            config_path=str(repo_root / "msem_sfn_calib_config.json"),
            script="auto_stage_calib.py", args=argv)
        if not ok:
            self._reset_after_calib()

    def _on_stop(self):
        if self._calib_runner is not None and self._calib_runner.is_running():
            self.log_panel.append_log("SFN", "INFO", "Stop requested (graceful)…")
            self._calib_runner.request_stop()
        elif self._stage_init_runner is not None and self._stage_init_runner.is_running():
            self.log_panel.append_log("SFN", "INFO", "Stop requested (graceful)…")
            # an operator-requested stop is not a failure -- don't auto-retry it
            self._stage_init_user_stopped = True
            self._stage_init_runner.request_stop()
        else:
            self._reset_after_calib()

    def _reset_after_calib(self):
        if self.engine is not None:
            try: self.engine.resume_hardware()
            except Exception: pass
        self._stop_stage_err_monitor()
        self.btn_beam_on.setEnabled(True)
        self.btn_calibrate.setEnabled(True)
        self.btn_stage_init.setEnabled(True)
        self.btn_stop.setEnabled(False)

    def _on_log(self, line: str):
        tag = "Calib"
        if line.startswith("[") and "]" in line:
            tag = line[1:line.index("]")]
        low = line.lower()
        level = ("ERROR" if ("error" in low or "traceback" in low or "failed" in low)
                 else "WARNING" if ("warn" in low or "skip" in low) else "INFO")
        self.log_panel.append_log(tag, level, line)
        marker = "Affine written to:"
        if marker in line:
            self._affine_path = line.split(marker, 1)[1].strip()

    def _on_calib_finished(self, code: int):
        self._reset_after_calib()
        if code == 0 and self._affine_path:
            self.edit_affine_in.setText(self._affine_path)
            self.log_panel.append_log("SFN", "SUCCESS", f"Calibration done. Affine: {self._affine_path}")
            self._finish_new_wafer_procedure(True, f"Affine: {self._affine_path}")
        else:
            err = f"Calibration exited with code {code}"
            self.log_panel.append_log("SFN", "ERROR", err)
            self._finish_new_wafer_procedure(False, err)

    def _on_error(self, msg: str):
        # A QProcess error fires the error signal INSTEAD of finished, so the
        # calib path must resume hardware here too or the scope stays paused.
        self._reset_after_calib()
        self.log_panel.append_log("SFN", "ERROR", msg)
        self._finish_new_wafer_procedure(False, f"Calibration failed to run: {msg}")

    # -- Stage B: apply (async, no scope) -----------------------------------
    def _on_apply(self):
        from msem_control.engine.pipeline_runner import PipelineRunner
        repo_root = Path(__file__).resolve().parents[3]
        script = repo_root / "apply_affine_to_section_json.py"
        affine = self.edit_affine_in.text().strip()
        old = self.edit_old_json.text().strip()
        new = self.edit_new_json.text().strip()
        if not new and old:
            stem, ext = os.path.splitext(old)
            new = f"{stem}_sfn{ext or '.json'}"
            self.edit_new_json.setText(new)
        if self._apply_runner is not None and self._apply_runner.is_running():
            self.log_panel.append_log("SFN", "WARNING", "Apply already running."); return
        if not (affine and os.path.isfile(affine)):
            self.log_panel.append_log("SFN", "ERROR", "Pick a valid affine.json first."); return
        if not (old and os.path.isfile(old)):
            self.log_panel.append_log("SFN", "ERROR", "Pick a valid old imaging JSON first."); return
        if not script.is_file():
            self.log_panel.append_log("SFN", "ERROR", f"apply_affine_to_section_json.py not found at {repo_root}."); return

        argv = ["apply_affine_to_section_json.py", "--in", old, "--out", new,
                "--affine_json", affine, "--all_regions"]
        if self.chk_force.isChecked():
            argv.append("--force")
        # Run via PipelineRunner (QProcess) so a slow network path can't freeze
        # the UI thread; it does NOT touch the scope, so no pause_hardware.
        if self._apply_runner is None:
            self._apply_runner = PipelineRunner(self)
            self._apply_runner.log_line.connect(self._on_apply_log)
            self._apply_runner.finished.connect(self._on_apply_finished)
            self._apply_runner.error.connect(self._on_apply_error)
        self._new_json_path = new
        self.btn_apply.setEnabled(False)
        self.btn_send.setEnabled(False)
        self.log_panel.append_log("SFN", "INFO", "Applying affine to ALL regions…")
        ok = self._apply_runner.start(
            {}, workdir=str(repo_root),
            config_path=str(repo_root / "msem_sfn_apply_config.json"),
            script="apply_affine_to_section_json.py", args=argv)
        if not ok:
            self.btn_apply.setEnabled(True)

    def _on_apply_log(self, line: str):
        low = line.lower()
        lvl = "ERROR" if ("error" in low or "traceback" in low or "failed" in low) else "INFO"
        self.log_panel.append_log("Apply", lvl, line)

    def _on_apply_finished(self, code: int):
        self.btn_apply.setEnabled(True)
        if code == 0:
            self.btn_send.setEnabled(True)
            self.log_panel.append_log("SFN", "SUCCESS", f"Wrote re-registered JSON: {self._new_json_path}")
        else:
            self.log_panel.append_log("SFN", "ERROR", f"apply exited with code {code}")

    def _on_apply_error(self, msg: str):
        self.btn_apply.setEnabled(True)
        self.log_panel.append_log("SFN", "ERROR", msg)

    def _on_send_himc(self):
        if self._himc_page is not None and self._new_json_path:
            self._himc_page.set_imaging_json_path(self._new_json_path)
            self.log_panel.append_log("SFN", "SUCCESS", "Sent new JSON to HI-MC page.")
        else:
            self.log_panel.append_log("SFN", "WARNING", "No new JSON yet, or HI-MC page not linked.")
