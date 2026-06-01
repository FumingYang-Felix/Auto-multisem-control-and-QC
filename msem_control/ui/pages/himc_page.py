"""
HI-MC page — a lightweight launcher for the PROVEN acquisition + Live-QC
pipeline (focus_map.py via main_live_qc.py).

The full Acquisition page is "the whole workflow" (manual stage control + the
big interactive tile map). For day-to-day runs the user used to hand-edit
focus_map.CONFIG, then `python main_live_qc.py`. This page surfaces the knobs
they actually tune as a form, points at the imaging JSON + prev-focusmap folder,
and drives the SAME subprocess via the SAME PipelineRunner — no microscope code
runs in the GUI process, and there is deliberately no heavy map widget here, so
this page stays snappy.

Everything maps through pipeline_runner.build_run_config(); anything not exposed
as a widget can be set in the two "advanced overrides" boxes (key = value, one
per line, value JSON-decoded) which flow through fm_extra / qc_extra verbatim.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDoubleSpinBox, QFileDialog, QFormLayout, QFrame,
    QGroupBox, QHBoxLayout, QLabel, QLineEdit, QPlainTextEdit, QProgressBar,
    QPushButton, QScrollArea, QSpinBox, QVBoxLayout, QWidget,
)

from .base_page import BasePage
from ..widgets import LogPanel


class HiMcPage(BasePage):
    """Form-driven launcher for the acquisition + Live-QC pipeline."""

    # -- construction -------------------------------------------------------
    def setup_ui(self):
        # runtime state (BasePage calls setup_ui from __init__, so init here)
        self._pipeline_runner = None
        self._last_output_dir = None   # block folder / QC results dir for "Open"

        title = QLabel("HI-MC — Acquisition + Live QC")
        title.setStyleSheet("font-size: 18px; font-weight: 600; padding: 0 0 6px 0;")
        self.layout.addWidget(title)

        hint = QLabel(
            "Load a Zeiss imaging JSON, point at the prev-focusmap folder, tune "
            "the parameters, then Start. Runs the same main_live_qc.py pipeline "
            "you run from the terminal.")
        hint.setWordWrap(True)
        hint.setStyleSheet("color: #9aa0a6; padding-bottom: 8px;")
        self.layout.addWidget(hint)

        # --- scrollable parameter form ------------------------------------
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        form_host = QWidget()
        form_col = QVBoxLayout(form_host)
        form_col.setContentsMargins(0, 0, 0, 0)
        form_col.setSpacing(10)

        form_col.addWidget(self._build_io_group())
        form_col.addWidget(self._build_region_group())
        form_col.addWidget(self._build_scan_group())
        form_col.addWidget(self._build_route_group())
        form_col.addWidget(self._build_plane_group())
        form_col.addWidget(self._build_qc_group())
        form_col.addWidget(self._build_advanced_group())
        form_col.addStretch(1)

        scroll.setWidget(form_host)
        self.layout.addWidget(scroll, stretch=3)

        # --- action row ---------------------------------------------------
        self.layout.addLayout(self._build_action_row())

        # --- log ----------------------------------------------------------
        self.log_panel = LogPanel()
        self.layout.addWidget(self.log_panel, stretch=2)

    # -- group builders -----------------------------------------------------
    def _group(self, title: str) -> tuple[QGroupBox, QFormLayout]:
        box = QGroupBox(title)
        form = QFormLayout(box)
        form.setLabelAlignment(Qt.AlignRight)
        form.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)
        return box, form

    def _file_row(self, edit: QLineEdit, handler) -> QWidget:
        row = QWidget()
        h = QHBoxLayout(row)
        h.setContentsMargins(0, 0, 0, 0)
        h.addWidget(edit, stretch=1)
        btn = QPushButton("Browse…")
        btn.clicked.connect(handler)
        h.addWidget(btn)
        return row

    def _build_io_group(self) -> QGroupBox:
        box, form = self._group("Input / Output")
        self.edit_json = QLineEdit()
        self.edit_json.setPlaceholderText("Zeiss imaging JSON (regions + support points)")
        form.addRow("Imaging JSON:", self._file_row(self.edit_json, self._browse_json))

        self.edit_pfm = QLineEdit()
        self.edit_pfm.setPlaceholderText("folder of prev_focusmap *.txt (matched per section)")
        form.addRow("Prev focus-map dir:", self._file_row(self.edit_pfm, self._browse_pfm))

        self.edit_output = QLineEdit()
        self.edit_output.setPlaceholderText(r"output root, e.g. V:\HI-MC\...\API")
        form.addRow("Output root:", self._file_row(self.edit_output, self._browse_output))

        self.edit_czi = QLineEdit("MyBlock")
        form.addRow("Block (czi) name:", self.edit_czi)
        self.edit_prefix = QLineEdit("tile")
        form.addRow("Tile prefix:", self.edit_prefix)
        self.combo_format = QComboBox()
        self.combo_format.addItems(["bmp", "tif", "png"])
        form.addRow("Image format:", self.combo_format)
        return box

    def _build_region_group(self) -> QGroupBox:
        box, form = self._group("Region selection")
        self.chk_process_all = QCheckBox("Process ALL regions in the JSON")
        self.chk_process_all.toggled.connect(self._on_process_all_toggled)
        form.addRow(self.chk_process_all)

        self.edit_region_name = QLineEdit()
        self.edit_region_name.setPlaceholderText("e.g. S25R1 (single-region mode)")
        form.addRow("Region name:", self.edit_region_name)

        self.edit_region_filter = QLineEdit()
        self.edit_region_filter.setPlaceholderText("e.g. R1 — only when 'all regions' is on (empty = every region)")
        self.edit_region_filter.setEnabled(False)
        form.addRow("Region filter:", self.edit_region_filter)
        return box

    def _build_scan_group(self) -> QGroupBox:
        box, form = self._group("Scan settings")
        self.spin_pixel = QDoubleSpinBox()
        self.spin_pixel.setRange(0.1, 10000.0)
        self.spin_pixel.setDecimals(2)
        self.spin_pixel.setValue(8.0)
        self.spin_pixel.setSuffix(" nm")
        form.addRow("Pixel size:", self.spin_pixel)

        self.spin_scanrate = QSpinBox()
        self.spin_scanrate.setRange(1, 30)
        self.spin_scanrate.setValue(4)
        form.addRow("Scan speed (rate):", self.spin_scanrate)

        self.spin_width = QSpinBox()
        self.spin_width.setRange(1, 100000)
        self.spin_width.setValue(2000)
        form.addRow("Tile width (px):", self.spin_width)

        self.spin_height = QSpinBox()
        self.spin_height.setRange(1, 100000)
        self.spin_height.setValue(1748)
        form.addRow("Tile height (px):", self.spin_height)

        self.spin_mfov_overlap = QDoubleSpinBox()
        self.spin_mfov_overlap.setRange(0.0, 50.0)
        self.spin_mfov_overlap.setDecimals(2)
        self.spin_mfov_overlap.setValue(5.0)
        self.spin_mfov_overlap.setSuffix(" %")
        form.addRow("MFOV (hex) overlap:", self.spin_mfov_overlap)

        self.spin_sfov_overlap = QDoubleSpinBox()
        self.spin_sfov_overlap.setRange(0.0, 100.0)
        self.spin_sfov_overlap.setDecimals(2)
        self.spin_sfov_overlap.setValue(1.0)
        self.spin_sfov_overlap.setSuffix(" µm")
        form.addRow("SFOV (inner) overlap:", self.spin_sfov_overlap)
        return box

    def _build_route_group(self) -> QGroupBox:
        box, form = self._group("Route")
        self.combo_travel = QComboBox()
        self.combo_travel.addItems(["Serpentine", "Comb"])
        form.addRow("Travel pattern:", self.combo_travel)

        self.combo_direction = QComboBox()
        self.combo_direction.addItems(
            ["Vertical Left", "Vertical Right", "Horizontal Up", "Horizontal Down"])
        form.addRow("Travel direction:", self.combo_direction)

        self.combo_start = QComboBox()
        self.combo_start.addItems(["Top", "Bottom"])
        form.addRow("Start position:", self.combo_start)
        return box

    def _build_plane_group(self) -> QGroupBox:
        box, form = self._group("Focus / plane update")
        self.combo_plane = QComboBox()
        self.combo_plane.addItems(
            ["ONE_POINT_OFFSET", "THREE_POINT_DELTA", "CURRENT_ALL_POINTS_PLANE"])
        form.addRow("Plane update method:", self.combo_plane)

        self.combo_init_ol = QComboBox()
        self.combo_init_ol.addItems(["PREV_PLANE_AT_FIRST_SP", "READ_CURRENT", "FIXED"])
        form.addRow("Initial OL mode:", self.combo_init_ol)

        self.combo_best_point = QComboBox()
        self.combo_best_point.addItems(["AF", "AS_SUM", "AF_AS"])
        form.addRow("Best-point metric:", self.combo_best_point)
        return box

    def _build_qc_group(self) -> QGroupBox:
        box, form = self._group("Live QC")
        self.spin_lowq = QDoubleSpinBox()
        self.spin_lowq.setRange(0.0, 1000.0)
        self.spin_lowq.setDecimals(1)
        self.spin_lowq.setValue(10.0)
        form.addRow("Low-quality threshold:", self.spin_lowq)

        self.spin_extra_beams = QSpinBox()
        self.spin_extra_beams.setRange(0, 91)
        self.spin_extra_beams.setValue(4)
        form.addRow("Extra QC beams:", self.spin_extra_beams)

        self.combo_beam_mode = QComboBox()
        self.combo_beam_mode.addItems(
            ["FIXED_PER_RUN_RANDOM", "FIXED_SET", "PER_MFOV_RANDOM"])
        form.addRow("Beam selection:", self.combo_beam_mode)

        self.spin_jitter = QDoubleSpinBox()
        self.spin_jitter.setRange(1.0, 10.0)
        self.spin_jitter.setDecimals(2)
        self.spin_jitter.setValue(1.5)
        form.addRow("Jitter warn threshold:", self.spin_jitter)

        self.spin_sat = QDoubleSpinBox()
        self.spin_sat.setRange(0.0, 100.0)
        self.spin_sat.setDecimals(1)
        self.spin_sat.setValue(30.0)
        self.spin_sat.setSuffix(" %")
        form.addRow("Outside-ROI saturation:", self.spin_sat)

        self.spin_save_every = QSpinBox()
        self.spin_save_every.setRange(1, 1000)
        self.spin_save_every.setValue(5)
        form.addRow("Save dashboard every:", self.spin_save_every)
        return box

    def _build_advanced_group(self) -> QGroupBox:
        box = QGroupBox("Advanced overrides  (key = value, one per line; value is JSON)")
        box.setCheckable(True)
        box.setChecked(False)
        v = QVBoxLayout(box)
        v.addWidget(QLabel("focus_map overrides — any CONFIG key, e.g.  manual_sp_id = 5"))
        self.txt_fm_extra = QPlainTextEdit()
        self.txt_fm_extra.setPlaceholderText('manual_sp_id = 5\nb2f_mode = "OFF"\nplane_update_sp_ids = [8, 9, 10]')
        self.txt_fm_extra.setFixedHeight(80)
        v.addWidget(self.txt_fm_extra)
        v.addWidget(QLabel("live_qc overrides — any LIVE_QC_CONFIG key, e.g.  stig_display_max = 40"))
        self.txt_qc_extra = QPlainTextEdit()
        self.txt_qc_extra.setPlaceholderText('fixed_beams = [1, 10, 28, 55, 82]\ntop_fraction = 0.5')
        self.txt_qc_extra.setFixedHeight(80)
        v.addWidget(self.txt_qc_extra)
        return box

    def _build_action_row(self) -> QHBoxLayout:
        row = QHBoxLayout()
        self.btn_start = QPushButton("▶  Start")
        self.btn_start.clicked.connect(self._on_start)
        self.btn_stop = QPushButton("■  Stop")
        self.btn_stop.clicked.connect(self._on_stop)
        self.btn_stop.setEnabled(False)
        self.btn_open = QPushButton("Open QC output…")
        self.btn_open.clicked.connect(self._open_output)
        self.btn_open.setEnabled(False)

        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.lbl_progress = QLabel("idle")
        self.lbl_progress.setMinimumWidth(220)

        row.addWidget(self.btn_start)
        row.addWidget(self.btn_stop)
        row.addWidget(self.btn_open)
        row.addSpacing(12)
        row.addWidget(self.progress_bar, stretch=1)
        row.addWidget(self.lbl_progress)
        return row

    # -- external wiring ----------------------------------------------------
    def set_imaging_json_path(self, path: str):
        """Auto-fill the imaging-JSON field when the shared Parameters dock
        loads a Zeiss experiment JSON (convenience; the field is still editable)."""
        if path:
            self.edit_json.setText(path)
            try:
                self.log_panel.append_log("HI-MC", "INFO", f"Imaging JSON set: {path}")
            except Exception:
                pass

    # -- small handlers -----------------------------------------------------
    def _on_process_all_toggled(self, on: bool):
        self.edit_region_filter.setEnabled(on)
        self.edit_region_name.setEnabled(not on)

    def _browse_json(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Select imaging JSON", "", "JSON (*.json);;All files (*)")
        if path:
            self.edit_json.setText(path)

    def _browse_pfm(self):
        path = QFileDialog.getExistingDirectory(self, "Select prev-focusmap folder")
        if path:
            self.edit_pfm.setText(path)

    def _browse_output(self):
        path = QFileDialog.getExistingDirectory(self, "Select output root")
        if path:
            self.edit_output.setText(path)

    # -- override parsing ---------------------------------------------------
    def _parse_overrides(self, text: str, where: str) -> dict:
        """Parse 'key = value' lines; value is JSON-decoded (fallback: string).
        Bad lines are logged + skipped (never block a run)."""
        out: dict = {}
        for raw in text.splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                self.log_panel.append_log("HI-MC", "WARNING",
                                          f"{where}: ignoring line without '=': {line}")
                continue
            key, _, val = line.partition("=")
            key = key.strip()
            val = val.strip()
            if not key:
                continue
            try:
                out[key] = json.loads(val)
            except Exception:
                out[key] = val  # treat as a plain string
        return out

    # -- build config from the form ----------------------------------------
    def _build_ui_dict(self, repo_root: Path) -> dict:
        ui = {
            "imaging_json_path": self.edit_json.text().strip(),
            "acquire_script_path": str(repo_root / "focus_map.py"),
            "output_root": self.edit_output.text().strip(),
            "czi_name": self.edit_czi.text().strip() or "acquisition",
            "prefix_base": self.edit_prefix.text().strip().rstrip("_") or "tile",
            "combo_format": self.combo_format.currentText().strip(),
            "acq_pixel_size_nm": self.spin_pixel.value(),
            "acq_scan_speed": self.spin_scanrate.value(),
            "acq_width": self.spin_width.value(),
            "acq_height": self.spin_height.value(),
            "mfov_overlap_pct": self.spin_mfov_overlap.value(),
            "sfov_overlap_um": self.spin_sfov_overlap.value(),
            "travel_pattern": self.combo_travel.currentText(),
            "travel_direction": self.combo_direction.currentText(),
            "start_position": self.combo_start.currentText(),
            "plane_update_method": self.combo_plane.currentText(),
            "initial_ol_mode": self.combo_init_ol.currentText(),
            "best_point_metric": self.combo_best_point.currentText(),
            # live_qc
            "qc_low_quality_threshold": self.spin_lowq.value(),
            "qc_num_extra_beams": self.spin_extra_beams.value(),
            "qc_beam_selection_mode": self.combo_beam_mode.currentText(),
            "qc_jitter_warn_threshold": self.spin_jitter.value(),
            "qc_outside_roi_sat_threshold": self.spin_sat.value(),
            "qc_save_every": self.spin_save_every.value(),
        }

        if self.chk_process_all.isChecked():
            ui["process_all_regions"] = True
            filt = self.edit_region_filter.text().strip()
            if filt:
                ui["region_name_filter"] = filt
        else:
            ui["process_all_regions"] = False
            rn = self.edit_region_name.text().strip()
            if rn:
                ui["region_name"] = rn

        pfm = self.edit_pfm.text().strip()
        if pfm:
            ui["prev_focusmap_dir"] = pfm

        fm_extra = self._parse_overrides(self.txt_fm_extra.toPlainText(), "focus_map override")
        if fm_extra:
            ui["fm_extra"] = fm_extra
        qc_extra = self._parse_overrides(self.txt_qc_extra.toPlainText(), "live_qc override")
        if qc_extra:
            ui["qc_extra"] = qc_extra
        return ui

    # -- start / stop -------------------------------------------------------
    def _on_start(self):
        from msem_control.engine.pipeline_runner import PipelineRunner, build_run_config

        # don't double-run; the engine pause flag is shared with the Acquisition page
        if self._pipeline_runner is not None and self._pipeline_runner.is_running():
            self.log_panel.append_log("HI-MC", "WARNING", "A run is already in progress.")
            return
        if self.engine is not None and self.engine.is_paused():
            self.log_panel.append_log(
                "HI-MC", "WARNING",
                "Another acquisition already owns the scope (engine paused). Stop it first.")
            return

        json_path = self.edit_json.text().strip()
        if not json_path or not os.path.isfile(json_path):
            self.log_panel.append_log(
                "HI-MC", "ERROR", "Pick a valid imaging JSON first.")
            return
        if not self.chk_process_all.isChecked() and not self.edit_region_name.text().strip():
            self.log_panel.append_log(
                "HI-MC", "ERROR",
                "Single-region mode: enter a Region name (or tick 'Process ALL regions').")
            return

        repo_root = Path(__file__).resolve().parents[3]
        main_script = repo_root / "main_live_qc.py"
        if not main_script.is_file():
            self.log_panel.append_log(
                "HI-MC", "ERROR",
                f"main_live_qc.py not found at {main_script}. Deploy the pipeline "
                f"scripts alongside the GUI.")
            return

        ui = self._build_ui_dict(repo_root)
        config = build_run_config(ui)

        if self._pipeline_runner is None:
            self._pipeline_runner = PipelineRunner(self)
            r = self._pipeline_runner
            r.log_line.connect(self._on_log)
            r.region_total.connect(self._on_region_total)
            r.region_started.connect(self._on_region_started)
            r.tile_progress.connect(self._on_tile_progress)
            r.region_done.connect(self._on_region_done)
            r.finished.connect(self._on_finished)
            r.error.connect(self._on_error)

        if self.engine is not None:
            try:
                self.engine.pause_hardware()
            except Exception:
                pass

        config_path = str(repo_root / "msem_run_config.json")
        self.progress_bar.setValue(0)
        self.lbl_progress.setText("launching…")
        self.btn_start.setEnabled(False)
        self.btn_stop.setEnabled(True)
        self.log_panel.append_log("HI-MC", "INFO", f"Launching {main_script.name} (cwd={repo_root})")
        ok = self._pipeline_runner.start(
            config, workdir=str(repo_root), config_path=config_path,
            script="main_live_qc.py")
        if not ok:
            self._reset_after_run()

    def _on_stop(self):
        if self._pipeline_runner is not None and self._pipeline_runner.is_running():
            self.log_panel.append_log("HI-MC", "INFO", "Stop requested (graceful)…")
            self._pipeline_runner.request_stop()
        else:
            self._reset_after_run()

    def _reset_after_run(self):
        if self.engine is not None:
            try:
                self.engine.resume_hardware()
            except Exception:
                pass
        self.btn_start.setEnabled(True)
        self.btn_stop.setEnabled(False)

    # -- pipeline signal handlers ------------------------------------------
    def _on_log(self, line: str):
        tag = "Pipeline"
        if line.startswith("[") and "]" in line:
            tag = line[1:line.index("]")]
        low = line.lower()
        if "error" in low or "traceback" in low or "failed" in low or "crashed" in low:
            level = "ERROR"
        elif "warn" in low or "skip" in low:
            level = "WARNING"
        else:
            level = "INFO"
        self.log_panel.append_log(tag, level, line)

        # capture output locations so "Open QC output" works
        marker = "QC outputs will be written to:"
        if marker in line:
            self._last_output_dir = line.split(marker, 1)[1].strip()
            self.btn_open.setEnabled(True)
        elif "[Output] Block folder:" in line and not self._last_output_dir:
            self._last_output_dir = line.split("Block folder:", 1)[1].strip()
            self.btn_open.setEnabled(True)

    def _on_region_total(self, n: int):
        self.lbl_progress.setText(f"0 / {n} region(s)")

    def _on_region_started(self, region_name: str, region_idx: int):
        self.progress_bar.setValue(0)
        self.lbl_progress.setText(f"Region {region_idx}: {region_name}")
        self.log_panel.append_log("HI-MC", "INFO", f"Region {region_idx} START: {region_name}")

    def _on_tile_progress(self, order: int, total: int):
        pct = int((order / total) * 100) if total > 0 else 0
        self.progress_bar.setValue(pct)
        self.lbl_progress.setText(f"tile {order}/{total}")

    def _on_region_done(self, region_name: str):
        self.log_panel.append_log("HI-MC", "SUCCESS", f"Region DONE: {region_name}")

    def _on_finished(self, code: int):
        if code == 0:
            self.progress_bar.setValue(100)
            self.lbl_progress.setText("done")
            self.log_panel.append_log("HI-MC", "SUCCESS", "Pipeline finished cleanly")
        else:
            self.lbl_progress.setText(f"exit {code}")
            self.log_panel.append_log("HI-MC", "ERROR", f"Pipeline exited with code {code}")
        self._reset_after_run()

    def _on_error(self, msg: str):
        self.log_panel.append_log("HI-MC", "ERROR", msg)

    # -- misc ---------------------------------------------------------------
    def _open_output(self):
        if not self._last_output_dir:
            return
        path = self._last_output_dir
        # the QC results dir is <block>/<region>/LiveQC; open its parent if the
        # exact dir doesn't exist yet (e.g. only the block folder was captured)
        target = path if os.path.exists(path) else os.path.dirname(path)
        QDesktopServices.openUrl(QUrl.fromLocalFile(target))
