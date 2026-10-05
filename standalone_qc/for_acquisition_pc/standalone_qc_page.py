"""Standalone QC page for msem_control — RUNS ON THE ACQUISITION COMPUTER.

    COPY THIS FILE TO:  msem_control/ui/pages/standalone_qc_page.py
    on the acquisition computer, then register it (see INSTALL.md beside
    this file).

What it does, and just as importantly what it does not:

It does NOT analyze anything. Putting QC work back on the acquisition
computer is the exact problem the standalone QC exists to solve - it made
acquisition unacceptably slow (one run spent 3000s of 5502s waiting on its
own QC). This page only WRITES A REQUEST FILE onto the share and then reads
a status file back. Both are a few hundred bytes. The analysis happens on
the QC computer.

    this page ──request_<id>.json──▶  <control folder> ──▶ QC computer
              ◀──status.json / response_<id>.json ────────  agent

DEPENDENCIES: PySide6 and the standard library, plus msem_control's own
BasePage and LogPanel. Deliberately NOTHING from the standalone_qc package,
which does not exist on this machine - the ~40 lines of JSON below are the
whole protocol.

PATHS MUST BE UNC. This computer mounts the share as V: and the QC computer
as Z:, so a drive-letter path written here means nothing over there. The
fields below warn about it and _to_unc() converts automatically where it can.
"""
from __future__ import annotations

import json
import os
import socket
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import (
    QCheckBox, QFileDialog, QFormLayout, QGroupBox, QHBoxLayout, QLabel,
    QLineEdit, QMessageBox, QPushButton, QSpinBox, QVBoxLayout, QWidget,
)

from .base_page import BasePage
from ..widgets import LogPanel

# --- the protocol. Must match standalone_qc/control.py on the QC computer --
PROTOCOL_VERSION = 1
STATUS_NAME = "status.json"
REQUEST_PREFIX = "request_"
RESPONSE_PREFIX = "response_"
# The agent refreshes status.json every 5s; treat it as offline past this.
OFFLINE_AFTER_S = 30.0


def _to_unc(path: str) -> str:
    r"""A mapped-drive path rewritten to UNC, so the QC computer can read it.

    The two machines use different letters for the same share, so sending
    "V:\Richard\..." would name nothing on the other side. Returns the
    input unchanged if it is already UNC, or if the drive is local (in which
    case the QC computer genuinely cannot reach it, and saying so beats
    inventing a path).
    """
    p = str(path).strip().replace("/", os.sep)
    if p.startswith("\\\\") or len(p) < 2 or p[1] != ":":
        return p
    try:
        import ctypes
        import ctypes.wintypes as wt
        buf = ctypes.create_unicode_buffer(1024)
        size = wt.DWORD(len(buf))
        rc = ctypes.windll.mpr.WNetGetConnectionW(p[:2], buf, ctypes.byref(size))
        if rc == 0 and buf.value.startswith("\\\\"):
            return buf.value.rstrip("\\") + p[2:]
    except Exception:
        pass
    return p


def _write_json_atomic(path: Path, payload: Dict[str, Any]) -> None:
    """tmp + os.replace, so the QC computer never reads a half-written file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp%d" % os.getpid())
    tmp.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    os.replace(tmp, path)


def _read_json(path: Path) -> Optional[Dict[str, Any]]:
    """None rather than raising: polling a folder on a share will sometimes
    catch a file mid-write or not yet there, which is normal."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


class StandaloneQcPage(BasePage):
    """Ask the QC computer to analyze a batch, and watch it happen."""

    def setup_ui(self):
        self._last_request_id: Optional[str] = None
        self._reported_done: set = set()

        title = QLabel("Standalone QC")
        title.setStyleSheet("font-size:15px;font-weight:600;")
        self.layout.addWidget(title)

        hint = QLabel(
            "Analysis runs on the QC computer, not this one. This page writes "
            "a request into a shared folder; the QC computer picks it up, "
            "analyzes, and reports progress back here."
        )
        hint.setWordWrap(True)
        hint.setStyleSheet("color:#8a929b;")
        self.layout.addWidget(hint)

        # --- where ---------------------------------------------------------
        box = QGroupBox("Shared folders")
        form = QFormLayout(box)

        self.edit_control = QLineEdit()
        self.edit_control.setPlaceholderText(
            r"\server\share\StandaloneQC_control   — must match the QC computer")
        self.edit_control.setToolTip(
            "The folder the QC computer's Standalone QC window is watching "
            "in Service mode. Use a UNC path, not V: - the QC computer maps "
            "the same share to a different letter.")
        form.addRow("Control folder:", self._row(self.edit_control,
                                                 self._browse_control))

        self.edit_image_dir = QLineEdit()
        self.edit_image_dir.setPlaceholderText(
            r"\server\share\...\<batch>   — the timestamped acquisition folder")
        self.edit_image_dir.setToolTip(
            "The batch to analyze: the timestamped folder holding this "
            "acquisition's section folders. The run folder above it also "
            "works and covers every batch in it.")
        form.addRow("Image directory:", self._row(self.edit_image_dir,
                                                  self._browse_image_dir))

        self.btn_from_himc = QPushButton("Use the HI-MC storage location")
        self.btn_from_himc.setToolTip(
            "Copy the path from the HI-MC page's 'Image storage location', "
            "converted to UNC so the QC computer can read it.")
        self.btn_from_himc.clicked.connect(self._on_from_himc)
        form.addRow("", self.btn_from_himc)
        self.layout.addWidget(box)

        # --- what ----------------------------------------------------------
        box2 = QGroupBox("What to analyze")
        form2 = QFormLayout(box2)

        self.edit_sections = QLineEdit()
        self.edit_sections.setPlaceholderText(
            "(optional) e.g. S3R5, S4R5 — blank means every section that has "
            "finished imaging and is not yet analyzed")
        self.edit_sections.setToolTip(
            "Naming sections analyzes exactly those, whether they have been "
            "analyzed before or not. Leave blank for everything pending.")
        form2.addRow("Sections:", self.edit_sections)

        self.spin_workers = QSpinBox()
        self.spin_workers.setRange(1, 64)
        self.spin_workers.setValue(24)
        self.spin_workers.setToolTip(
            "How many sFoVs the QC computer reads at once. It is waiting on "
            "the share, not on its CPU, so a number above its core count is "
            "normal. Lower it if QC reads are slowing this acquisition down.")
        form2.addRow("# sFoVs Processed Concurrently:", self.spin_workers)

        self.chk_overview = QCheckBox(
            "Stitch the section overview image (slow on large sections)")
        self.chk_overview.setChecked(True)
        self.chk_overview.setToolTip(
            "The overview reads every beam of every mFoV on the QC computer "
            "- about 36,000 images for a 400-mFoV section. Those reads come "
            "off the same share this acquisition is writing to, so consider "
            "leaving it off until the run finishes.")
        form2.addRow("Overview:", self.chk_overview)
        self.layout.addWidget(box2)

        # --- go ------------------------------------------------------------
        actions = QHBoxLayout()
        self.btn_analyze = QPushButton("▶  Analyze on the QC computer")
        self.btn_analyze.setObjectName("PrimaryButton")
        self.btn_analyze.clicked.connect(self._on_analyze)
        actions.addWidget(self.btn_analyze)

        self.btn_refresh = QPushButton("Refresh status")
        self.btn_refresh.clicked.connect(self._poll)
        actions.addWidget(self.btn_refresh)
        actions.addStretch()
        self.layout.addLayout(actions)

        self.lbl_status = QLabel("QC computer: unknown")
        self.lbl_status.setWordWrap(True)
        self.lbl_status.setStyleSheet("font-weight:600;")
        self.layout.addWidget(self.lbl_status)

        self.log_panel = LogPanel()
        self.layout.addWidget(self.log_panel, stretch=2)

        # Polling, not pushing: there is no way for the QC computer to call
        # into this process, and a 3s read of one small JSON costs nothing.
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._poll)
        self._timer.start(3000)

    # -- small helpers ------------------------------------------------------
    def _row(self, edit: QLineEdit, handler) -> QWidget:
        w = QWidget()
        h = QHBoxLayout(w)
        h.setContentsMargins(0, 0, 0, 0)
        h.addWidget(edit, stretch=1)
        b = QPushButton("Browse…")
        b.clicked.connect(handler)
        h.addWidget(b)
        return w

    def _browse_control(self):
        d = QFileDialog.getExistingDirectory(
            self, "Select the shared control folder",
            self.edit_control.text().strip())
        if d:
            self.edit_control.setText(_to_unc(d))

    def _browse_image_dir(self):
        d = QFileDialog.getExistingDirectory(
            self, "Select the batch folder to analyze",
            self.edit_image_dir.text().strip())
        if d:
            self.edit_image_dir.setText(_to_unc(d))

    def _on_from_himc(self):
        """Pull the output path straight off the HI-MC page, so the operator
        does not retype it (and cannot mistype it)."""
        win = self.window()
        page = getattr(win, "page_himc", None)
        field = getattr(page, "edit_output", None) if page is not None else None
        if field is None:
            QMessageBox.information(
                self, "HI-MC page not found",
                "Could not read the HI-MC page's storage location from here. "
                "Paste the batch path into 'Image directory' instead.")
            return
        val = field.text().strip()
        if not val:
            QMessageBox.information(
                self, "Nothing to copy",
                "The HI-MC page's 'Image storage location' is empty.")
            return
        self.edit_image_dir.setText(_to_unc(val))
        self.log_panel.append_log("QC", "INFO",
                                  f"Image directory set from HI-MC: {_to_unc(val)}")

    # -- submitting ---------------------------------------------------------
    def _on_analyze(self):
        ctrl = _to_unc(self.edit_control.text().strip())
        image_dir = _to_unc(self.edit_image_dir.text().strip())
        if not ctrl:
            QMessageBox.warning(
                self, "No control folder",
                "Set the Control folder first - the shared folder the QC "
                "computer is watching in Service mode.")
            return
        if not image_dir:
            QMessageBox.warning(
                self, "No image directory",
                "Set the batch folder to analyze, or press 'Use the HI-MC "
                "storage location'.")
            return

        # Refuse to submit into the void: an offline agent means the request
        # sits unread, and a button that looked like it worked is worse than
        # one that said no.
        status = _read_json(Path(ctrl) / STATUS_NAME)
        if not self._is_online(status):
            seen = (status or {}).get("heartbeat_at") or "never"
            answer = QMessageBox.question(
                self, "QC computer does not look online",
                f"No recent heartbeat in:\n{ctrl}\n\nLast seen: {seen}\n\n"
                f"On the QC computer, open the Standalone QC window, choose "
                f"Service mode, set the same control folder and press Start "
                f"Service.\n\nQueue the request anyway? It will be picked up "
                f"whenever that service starts.",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
            if answer != QMessageBox.Yes:
                return

        sections = [s.strip() for s in
                    self.edit_sections.text().replace(";", ",").split(",")
                    if s.strip()]
        rid = time.strftime("%Y%m%d_%H%M%S") + "_%04x" % (os.getpid() & 0xffff)
        payload = {
            "protocol": PROTOCOL_VERSION,
            "id": rid,
            "action": "analyze" if sections else "analyze_pending",
            "image_dir": image_dir,
            "sections": sections,
            "options": {
                "max_workers": int(self.spin_workers.value()),
                "region_overview_enabled": bool(self.chk_overview.isChecked()),
            },
            "requested_by": socket.gethostname(),
            "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "created_epoch": time.time(),
        }
        try:
            _write_json_atomic(Path(ctrl) / f"{REQUEST_PREFIX}{rid}.json", payload)
        except OSError as exc:
            QMessageBox.warning(self, "Could not write the request",
                                f"{ctrl}\n\n{exc}")
            return

        self._last_request_id = rid
        what = ", ".join(sections) if sections else "every pending section"
        self.log_panel.append_log(
            "QC", "INFO",
            f"requested {what} in {image_dir} (id {rid}) - the QC computer "
            f"will pick this up within a few seconds")
        self._poll()

    # -- status -------------------------------------------------------------
    @staticmethod
    def _is_online(status: Optional[Dict[str, Any]]) -> bool:
        if not status:
            return False
        try:
            age = time.time() - float(status.get("heartbeat_epoch") or 0.0)
        except (TypeError, ValueError):
            return False
        return 0 <= age <= OFFLINE_AFTER_S

    def _poll(self):
        ctrl = self.edit_control.text().strip()
        if not ctrl:
            self.lbl_status.setText("QC computer: no control folder set")
            self.lbl_status.setStyleSheet("font-weight:600;color:#8a929b;")
            return
        root = Path(_to_unc(ctrl))
        status = _read_json(root / STATUS_NAME)

        if not self._is_online(status):
            seen = (status or {}).get("heartbeat_at") or "never"
            self.lbl_status.setText(
                f"QC computer: OFFLINE (last seen {seen}) - start Service "
                f"mode in its Standalone QC window")
            self.lbl_status.setStyleSheet("font-weight:600;color:#f44747;")
        else:
            state = str(status.get("state") or "?")
            cur = status.get("current") or {}
            if state == "analyzing" and cur:
                done, total = cur.get("done") or 0, cur.get("total") or 0
                pct = f" {100.0 * done / total:.0f}%" if total else ""
                self.lbl_status.setText(
                    f"QC computer ({status.get('host', '?')}): analyzing "
                    f"{cur.get('section') or '?'}{pct} - "
                    f"{cur.get('phase') or ''}")
                self.lbl_status.setStyleSheet("font-weight:600;color:#cca700;")
            else:
                queued = int(status.get("queue") or 0)
                extra = f", {queued} queued" if queued else ""
                self.lbl_status.setText(
                    f"QC computer ({status.get('host', '?')}): {state}{extra}")
                self.lbl_status.setStyleSheet("font-weight:600;color:#4ec9b0;")

        # Report each finished request once.
        if self._last_request_id and self._last_request_id not in self._reported_done:
            resp = _read_json(
                root / f"{RESPONSE_PREFIX}{self._last_request_id}.json")
            if resp is not None:
                self._reported_done.add(self._last_request_id)
                self._report(resp)

    def _report(self, resp: Dict[str, Any]):
        if not resp.get("ok"):
            self.log_panel.append_log(
                "QC", "ERROR",
                f"request {resp.get('id')} failed: "
                f"{resp.get('error') or resp.get('note') or 'unknown reason'}")
            return
        sections = resp.get("sections") or []
        if not sections:
            self.log_panel.append_log(
                "QC", "INFO",
                f"request {resp.get('id')}: {resp.get('note') or 'done'}")
            return
        for s in sections:
            if not s.get("ok"):
                self.log_panel.append_log(
                    "QC", "ERROR",
                    f"{s.get('section')}: {s.get('error') or 'failed'}")
                continue
            bits = [f"{s.get('n_tiles')} mFoVs",
                    f"median quality {s.get('median_q')}",
                    f"{s.get('elapsed_s')}s"]
            outliers = s.get("outlier_beams")
            if outliers:
                bits.append(f"outlier beam(s) {outliers}")
            self.log_panel.append_log(
                "QC", "WARNING" if outliers else "INFO",
                f"{s.get('section')}: " + ", ".join(bits))
            if s.get("report"):
                self.log_panel.append_log("QC", "INFO",
                                          f"  report: {s['report']}")

    def on_show(self):
        self._poll()
