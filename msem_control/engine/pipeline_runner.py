"""
Pipeline runner — drives the real acquisition pipeline (focus_map.py via
main_live_qc.py) as a SUBPROCESS, instead of the GUI calling msem.* inline.

Why a subprocess (not in-process import):
  - focus_map.py / main_live_qc.py are standalone scripts: each builds its own
    MSEMConnection and main_live_qc spawns its own matplotlib LiveQC subprocess.
    Importing them into the Qt event loop would fight the GUI's own engine
    connection and the matplotlib child. Running them as a child process is
    exactly how the user runs them from a terminal today — zero interference.
  - The GUI just: builds a config JSON, launches `python main_live_qc.py` with
    MSEM_CONFIG_JSON pointing at it, tails the merged stdout/stderr (the same
    text you'd see in the terminal + per-section console.log), and parses the
    [Regions]/[Region]/[AcqTile]/[LiveQC] lines into progress signals.

Config JSON shape consumed by focus_map.apply_external_config_override() and
main_live_qc._apply_live_qc_config_override():
    {"focus_map": { ...CONFIG overrides... },
     "live_qc":   { ...LIVE_QC_CONFIG overrides... }}
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Dict, Optional

from PySide6.QtCore import QObject, QProcess, QProcessEnvironment, Signal


# ---------------------------------------------------------------------------
# Pure UI -> config mapping (no Qt, unit-testable)
# ---------------------------------------------------------------------------

# Scan-rate combo text "Comb"/"Serpentine" -> focus_map route_mode token.
_TRAVEL_MAP = {
    "comb": "COMB",
    "serpentine": "SERPENTINE",
    "snake": "SERPENTINE",
}
# Direction combo text -> focus_map travel_direction token.
_DIRECTION_MAP = {
    "horizontal up": "HORIZONTAL_UP",
    "horizontal down": "HORIZONTAL_DOWN",
    "vertical left": "VERTICAL_LEFT",
    "vertical right": "VERTICAL_RIGHT",
}
# Start-position combo text -> focus_map start_position token.
_START_MAP = {
    "bottom left": "BOTTOM",
    "bottom right": "BOTTOM",
    "top left": "TOP",
    "top right": "TOP",
}


def build_run_config(ui: Dict[str, Any]) -> Dict[str, Any]:
    """Map a flat dict of UI values to the {focus_map, live_qc} config the
    pipeline consumes. Only keys the user actually set should be present in
    `ui`; everything omitted falls back to the pipeline's hardcoded defaults.

    Recognised `ui` keys (all optional):
      imaging_json_path, output_root, czi_name, prefix_base, combo_format,
      region_name_filter, process_all_regions, region_name,
      acq_pixel_size_nm, acq_scan_speed, acq_width, acq_height,
      mfov_overlap_pct, sfov_overlap_um,
      travel_pattern, travel_direction, start_position,
      prev_focusmap_by_region, plane_update_method, initial_ol_mode,
      # live_qc:
      qc_low_quality_threshold, qc_num_extra_beams, qc_beam_selection_mode,
      qc_jitter_warn_threshold, qc_outside_roi_sat_threshold, qc_save_every,
      qc_extra (dict, merged verbatim into live_qc)
    """
    fm: Dict[str, Any] = {}

    def put(key, ui_key, transform=None):
        if ui_key in ui and ui[ui_key] is not None:
            v = ui[ui_key]
            fm[key] = transform(v) if transform else v

    # imaging JSON + region selection
    put("json_file", "imaging_json_path")
    put("region_name_filter", "region_name_filter")
    put("process_all_regions", "process_all_regions")
    put("region_name", "region_name")

    # output
    put("output_root", "output_root")
    put("czi_name", "czi_name")
    put("prefix_base", "prefix_base")
    put("combo_format", "combo_format")

    # scan
    put("acq_pixel_size_nm", "acq_pixel_size_nm", float)
    put("acq_scan_speed", "acq_scan_speed", int)
    put("acq_width", "acq_width", int)
    put("acq_height", "acq_height", int)

    # overlap
    if ui.get("mfov_overlap_pct") is not None:
        # focus_map roi_hex_overlap is a 0..0.5 fraction; UI gives percent.
        fm["roi_hex_overlap"] = float(ui["mfov_overlap_pct"]) / 100.0
    put("inner_sfoverlap_um", "sfov_overlap_um", float)

    # route
    if ui.get("travel_pattern"):
        fm["route_mode"] = _TRAVEL_MAP.get(str(ui["travel_pattern"]).strip().lower(), "SERPENTINE")
        fm["route_source"] = "REBUILD"
    if ui.get("travel_direction"):
        fm["travel_direction"] = _DIRECTION_MAP.get(str(ui["travel_direction"]).strip().lower(), "VERTICAL_LEFT")
    if ui.get("start_position"):
        fm["start_position"] = _START_MAP.get(str(ui["start_position"]).strip().lower(), "TOP")

    # advanced (optional)
    put("prev_focusmap_by_region", "prev_focusmap_by_region")
    put("prev_focusmap_dir", "prev_focusmap_dir")  # folder of prev_focusmap .txt
    put("plane_update_method", "plane_update_method")
    put("initial_ol_mode", "initial_ol_mode")
    # any extra focus_map keys passed verbatim
    extra_fm = ui.get("fm_extra")
    if isinstance(extra_fm, dict):
        fm.update(extra_fm)

    # ---- live_qc section ----
    lq: Dict[str, Any] = {}

    def putq(key, ui_key, transform=None):
        if ui_key in ui and ui[ui_key] is not None:
            v = ui[ui_key]
            lq[key] = transform(v) if transform else v

    # Path to the focus_map.py the wrapper should load. Defaults in
    # main_live_qc.py to a hardcoded D:\ path; the GUI overrides it to the
    # co-located focus_map.py so the subprocess always loads the right script.
    putq("acquire_script_path", "acquire_script_path")
    putq("low_quality_threshold", "qc_low_quality_threshold", float)
    putq("num_extra_beams", "qc_num_extra_beams", int)
    putq("beam_selection_mode", "qc_beam_selection_mode")
    putq("jitter_warn_threshold", "qc_jitter_warn_threshold", float)
    putq("outside_roi_sat_threshold", "qc_outside_roi_sat_threshold", float)
    putq("save_every", "qc_save_every", int)
    extra_lq = ui.get("qc_extra")
    if isinstance(extra_lq, dict):
        lq.update(extra_lq)

    out: Dict[str, Any] = {}
    if fm:
        out["focus_map"] = fm
    if lq:
        out["live_qc"] = lq
    return out


# ---------------------------------------------------------------------------
# QProcess-based subprocess driver
# ---------------------------------------------------------------------------

class PipelineRunner(QObject):
    """Launches `python main_live_qc.py` as a child process and surfaces its
    output + progress as Qt signals. One runner drives one acquisition run."""

    log_line = Signal(str)                 # a single line of subprocess output
    started = Signal()                     # process launched OK
    region_total = Signal(int)             # total regions to process
    region_started = Signal(str, int)      # region_name, region_idx (1-based)
    tile_progress = Signal(int, int)       # order, total (within current region)
    qc_tile = Signal(int)                  # tile_idx queued to LiveQC
    region_done = Signal(str)              # region_name finished
    finished = Signal(int)                 # process exit code (0 == clean)
    error = Signal(str)                    # fatal launch / process error

    _RE_REGIONS_TOTAL = re.compile(r"\[Regions\]\s+Will process\s+(\d+)\s+region")
    _RE_REGION_START = re.compile(r"\[Region\]\s+START\s+region_name=(\S+)\s+region_idx=(\d+)")
    _RE_REGION_DONE = re.compile(r"\[Region\]\s+DONE\s+region_name=(\S+)")
    _RE_ACQ_TILE = re.compile(r"\[AcqTile\]\s+order=(\d+)/(\d+)")
    _RE_QC_TILE = re.compile(r"\[LiveQC\]\s+queued tile\s+(\d+)")

    def __init__(self, parent: Optional[QObject] = None):
        super().__init__(parent)
        self._proc: Optional[QProcess] = None
        self._buf = ""
        self._config_path = ""

    # -- state --------------------------------------------------------------
    def is_running(self) -> bool:
        return self._proc is not None and self._proc.state() != QProcess.NotRunning

    @property
    def config_path(self) -> str:
        return self._config_path

    # -- launch -------------------------------------------------------------
    def start(
        self,
        config: Dict[str, Any],
        *,
        workdir: str,
        config_path: Optional[str] = None,
        python_exe: Optional[str] = None,
        script: str = "main_live_qc.py",
    ) -> bool:
        """Write `config` to JSON and spawn the pipeline. Returns False if a run
        is already in progress or the config couldn't be written."""
        if self.is_running():
            self.error.emit("A pipeline run is already in progress.")
            return False

        workdir = str(workdir)
        if config_path is None:
            config_path = os.path.join(workdir, "msem_run_config.json")
        self._config_path = config_path
        try:
            Path(config_path).parent.mkdir(parents=True, exist_ok=True)
            with open(config_path, "w", encoding="utf-8") as f:
                json.dump(config, f, indent=2)
        except Exception as exc:
            self.error.emit(f"Failed to write config {config_path}: {exc}")
            return False

        self._buf = ""
        proc = QProcess(self)
        proc.setWorkingDirectory(workdir)
        proc.setProcessChannelMode(QProcess.MergedChannels)  # interleave stderr+stdout

        env = QProcessEnvironment.systemEnvironment()
        env.insert("MSEM_CONFIG_JSON", config_path)
        env.insert("PYTHONUNBUFFERED", "1")  # live line-buffered output
        proc.setProcessEnvironment(env)

        proc.readyReadStandardOutput.connect(self._on_stdout)
        proc.finished.connect(self._on_finished)
        proc.errorOccurred.connect(self._on_proc_error)
        proc.started.connect(self.started)

        exe = python_exe or sys.executable
        proc.setProgram(exe)
        proc.setArguments([script])
        self._proc = proc
        self.log_line.emit(
            f"[runner] launch: {exe} {script}  (cwd={workdir})  "
            f"MSEM_CONFIG_JSON={config_path}"
        )
        proc.start()
        return True

    # -- stop ---------------------------------------------------------------
    def request_stop(self) -> None:
        """Graceful stop: send SIGINT so main_live_qc.py's signal handler can
        flush the in-flight tile + shut down the LiveQC subprocesses. Falls
        back to QProcess.terminate() if SIGINT can't be delivered."""
        if not self.is_running():
            return
        self.log_line.emit("[runner] stop requested — sending interrupt (graceful)")
        pid = int(self._proc.processId())
        try:
            import signal as _sig
            os.kill(pid, _sig.SIGINT)
        except Exception:
            try:
                self._proc.terminate()
            except Exception as exc:
                self.error.emit(f"terminate failed: {exc}")

    def kill(self) -> None:
        if self.is_running():
            self._proc.kill()

    # -- internal -----------------------------------------------------------
    def _on_stdout(self) -> None:
        if not self._proc:
            return
        chunk = bytes(self._proc.readAllStandardOutput()).decode("utf-8", errors="replace")
        self._buf += chunk
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            self._handle_line(line.rstrip("\r"))

    def _handle_line(self, line: str) -> None:
        if line == "":
            return
        self.log_line.emit(line)
        m = self._RE_REGIONS_TOTAL.search(line)
        if m:
            self.region_total.emit(int(m.group(1)))
            return
        m = self._RE_REGION_START.search(line)
        if m:
            self.region_started.emit(m.group(1), int(m.group(2)))
            return
        m = self._RE_REGION_DONE.search(line)
        if m:
            self.region_done.emit(m.group(1))
            return
        m = self._RE_ACQ_TILE.search(line)
        if m:
            self.tile_progress.emit(int(m.group(1)), int(m.group(2)))
            return
        m = self._RE_QC_TILE.search(line)
        if m:
            self.qc_tile.emit(int(m.group(1)))
            return

    def _on_finished(self, code: int, _status) -> None:
        if self._buf:
            self._handle_line(self._buf)
            self._buf = ""
        self.finished.emit(int(code))
        self._proc = None

    def _on_proc_error(self, err) -> None:
        # err is a QProcess.ProcessError enum; stringify defensively
        self.error.emit(f"QProcess error: {err}")
