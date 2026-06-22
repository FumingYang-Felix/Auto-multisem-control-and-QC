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
import time
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox, QDoubleSpinBox, QFileDialog, QFormLayout, QFrame, QGroupBox,
    QHBoxLayout, QLabel, QLineEdit, QPushButton, QScrollArea, QSpinBox,
    QVBoxLayout, QWidget,
)

from .base_page import BasePage
from ..widgets import LogPanel

# auto_stage_calib.DEFAULT_PROJ (per-marker reset projective setpoints)
_DEFAULT_PROJ = {"M1": (-585.156, 649.869), "M2": (-556.728, 662.229), "M3": (-693.381, 711.415)}


class SfnPage(BasePage):

    # -- construction -------------------------------------------------------
    def setup_ui(self):
        self._calib_runner = None
        self._apply_runner = None
        self._affine_path = None
        self._himc_page = None
        self._new_json_path = None

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

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        host = QWidget()
        col = QVBoxLayout(host)
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(10)
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

    def _build_calib_group(self):
        box, form = self._group("Stage A — Calibration (drives the scope)")
        self.edit_markers = QLineEdit()
        self.edit_markers.setPlaceholderText("markers_prev.json from a prior round (required for a real run)")
        form.addRow("Markers JSON:", self._file_row(self.edit_markers, self._browse_markers))
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
        self.btn_calibrate = QPushButton("▶  Run calibration")
        self.btn_calibrate.clicked.connect(self._on_calibrate)
        self.btn_stop = QPushButton("■  Stop")
        self.btn_stop.clicked.connect(self._on_stop)
        self.btn_stop.setEnabled(False)
        self.btn_apply = QPushButton("Apply affine → all regions")
        self.btn_apply.clicked.connect(self._on_apply)
        self.btn_send = QPushButton("Send new JSON → HI-MC")
        self.btn_send.clicked.connect(self._on_send_himc)
        self.btn_send.setEnabled(False)
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

    # -- browse handlers ----------------------------------------------------
    def _browse_markers(self):
        p, _ = QFileDialog.getOpenFileName(self, "markers_prev.json", "", "JSON (*.json);;All (*)")
        if p: self.edit_markers.setText(p)

    def _browse_affine(self):
        p, _ = QFileDialog.getOpenFileName(self, "affine.json", "", "JSON (*.json);;All (*)")
        if p: self.edit_affine_in.setText(p)

    def _browse_old_json(self):
        p, _ = QFileDialog.getOpenFileName(self, "old imaging JSON", "", "JSON (*.json);;All (*)")
        if p: self.set_imaging_json_path(p)

    def _browse_new_json(self):
        p, _ = QFileDialog.getSaveFileName(self, "output JSON", "", "JSON (*.json);;All (*)")
        if p: self.edit_new_json.setText(p)

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
        from msem_control.engine.pipeline_runner import PipelineRunner

        if self._calib_runner is not None and self._calib_runner.is_running():
            self.log_panel.append_log("SFN", "WARNING", "Calibration already running."); return
        if self.engine is not None and self.engine.is_paused():
            self.log_panel.append_log("SFN", "WARNING",
                                      "Another run owns the scope (engine paused). Stop it first."); return
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
        self.btn_calibrate.setEnabled(False)
        self.btn_stop.setEnabled(True)
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
        else:
            self._reset_after_calib()

    def _reset_after_calib(self):
        if self.engine is not None:
            try: self.engine.resume_hardware()
            except Exception: pass
        self.btn_calibrate.setEnabled(True)
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
        else:
            self.log_panel.append_log("SFN", "ERROR", f"Calibration exited with code {code}")

    def _on_error(self, msg: str):
        # A QProcess error fires the error signal INSTEAD of finished, so the
        # calib path must resume hardware here too or the scope stays paused.
        self._reset_after_calib()
        self.log_panel.append_log("SFN", "ERROR", msg)

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
