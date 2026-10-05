"""The page that goes on the ACQUISITION computer, tested here.

It cannot import anything from standalone_qc (that package does not exist on
that machine), so this suite imports it in isolation with msem_control's
BasePage and LogPanel stubbed, and then drives the REAL agent with it. That
is the only way to know the two sides agree on the protocol before the file
is copied across - a mismatch would otherwise surface as a request the QC
computer silently never answers.

    pytest standalone_qc/tests/test_acq_page.py
"""
from __future__ import annotations

import importlib.util
import os
import sys
import types
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import pytest  # noqa: E402
from PySide6.QtWidgets import QApplication, QVBoxLayout, QWidget  # noqa: E402

PAGE_FILE = (Path(__file__).resolve().parents[1]
             / "for_acquisition_pc" / "standalone_qc_page.py")


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture(scope="module")
def page_module():
    """Load the file the way msem_control will, with its two imports stubbed.

    Stubbing rather than importing the real ones proves the page needs
    nothing else from that app beyond BasePage and LogPanel - the two things
    INSTALL.md says must exist.
    """
    class BasePage(QWidget):
        def __init__(self, engine, parent=None):
            super().__init__(parent)
            self.engine = engine
            self.layout = QVBoxLayout(self)
            self.setup_ui()

        def setup_ui(self):
            pass

        def on_show(self):
            pass

    class LogPanel(QWidget):
        def __init__(self):
            super().__init__()
            self.lines = []

        def append_log(self, source, level, message):
            self.lines.append((source, level, message))

    pkg = types.ModuleType("acqstub")
    pkg.__path__ = []
    base = types.ModuleType("acqstub.base_page")
    base.BasePage = BasePage
    widgets = types.ModuleType("acqstub.widgets")
    widgets.LogPanel = LogPanel
    sys.modules.update({"acqstub": pkg, "acqstub.base_page": base,
                        "acqstub.widgets": widgets})

    src = PAGE_FILE.read_text(encoding="utf-8")
    src = src.replace("from .base_page import BasePage",
                      "from acqstub.base_page import BasePage")
    src = src.replace("from ..widgets import LogPanel",
                      "from acqstub.widgets import LogPanel")
    spec = importlib.util.spec_from_loader("acq_qc_page", loader=None)
    mod = importlib.util.module_from_spec(spec)
    mod.__dict__["__file__"] = str(PAGE_FILE)
    exec(compile(src, str(PAGE_FILE), "exec"), mod.__dict__)
    sys.modules["acq_qc_page"] = mod
    return mod


def test_the_page_imports_with_only_basepage_and_logpanel(page_module) -> None:
    assert hasattr(page_module, "StandaloneQcPage")
    # No accidental dependency on the QC package.
    src = PAGE_FILE.read_text(encoding="utf-8")
    assert "standalone_qc." not in src.replace("standalone_qc_page", "")
    assert "from standalone_qc" not in src


def test_protocol_constants_match_the_qc_side(page_module) -> None:
    """The two sides are separate clones; if these drift, requests are
    written where nobody reads them."""
    from standalone_qc import control

    assert page_module.PROTOCOL_VERSION == control.PROTOCOL_VERSION
    assert page_module.STATUS_NAME == control.STATUS_NAME
    assert page_module.REQUEST_PREFIX == control.REQUEST_PREFIX
    assert page_module.RESPONSE_PREFIX == control.RESPONSE_PREFIX
    assert page_module.OFFLINE_AFTER_S == control.OFFLINE_AFTER_S


def test_page_request_is_served_by_the_real_agent(qapp, page_module,
                                                  tmp_path) -> None:
    """The whole handshake: the page writes, the agent analyzes, the page
    reports it back - with no shared code between them but the JSON."""
    from standalone_qc.agent import Agent
    from standalone_qc.config import load_config
    from standalone_qc.tests.test_standalone_qc import (BATCH, DSAMP,
                                                        build_fixture)

    build_fixture(tmp_path / "data")
    ctrl = tmp_path / "ctrl"
    ctrl.mkdir()

    page = page_module.StandaloneQcPage(engine=None)
    page._timer.stop()                     # drive polling by hand
    page.edit_control.setText(str(ctrl))
    page.edit_image_dir.setText(str(tmp_path / "data" / BATCH))
    page.chk_overview.setChecked(False)
    page.spin_workers.setValue(4)

    # Agent offline: submitting must ask first rather than queue silently.
    from PySide6.QtWidgets import QMessageBox
    asked = {}

    def _yes(*a, **k):
        asked["t"] = a[1]
        return QMessageBox.Yes

    orig = QMessageBox.question
    QMessageBox.question = staticmethod(_yes)
    try:
        page._on_analyze()
    finally:
        QMessageBox.question = orig
    assert "online" in asked["t"].lower(), asked
    rid = page._last_request_id
    assert rid and (ctrl / f"request_{rid}.json").is_file()

    agent = Agent(ctrl, load_config(None, dsamp=DSAMP, max_workers=4,
                                    region_overview_enabled=False),
                  poll_s=1.0, log=lambda m: None)
    agent.run(once=True)

    page._poll()
    assert page._last_request_id in page._reported_done
    msgs = [m for _, _, m in page.log_panel.lines]
    assert any("S1R1" in m for m in msgs), msgs
    assert any("median quality" in m for m in msgs), msgs
    assert "analyzing" in page.lbl_status.text() or "idle" in page.lbl_status.text()


def test_offline_is_stated_plainly(qapp, page_module, tmp_path) -> None:
    page = page_module.StandaloneQcPage(engine=None)
    page._timer.stop()
    page.edit_control.setText(str(tmp_path / "empty"))
    page._poll()
    assert "OFFLINE" in page.lbl_status.text()
    assert "Service mode" in page.lbl_status.text()

    page.edit_control.clear()
    page._poll()
    assert "no control folder" in page.lbl_status.text()


def test_named_sections_are_parsed_from_free_text(qapp, page_module,
                                                  tmp_path) -> None:
    import json

    ctrl = tmp_path / "ctrl"
    ctrl.mkdir()
    (tmp_path / "batch").mkdir()
    page = page_module.StandaloneQcPage(engine=None)
    page._timer.stop()
    page.edit_control.setText(str(ctrl))
    page.edit_image_dir.setText(str(tmp_path / "batch"))
    page.edit_sections.setText(" S3R5 ,S4R5; S5R5 ")

    from PySide6.QtWidgets import QMessageBox
    orig = QMessageBox.question
    QMessageBox.question = staticmethod(lambda *a, **k: QMessageBox.Yes)
    try:
        page._on_analyze()
    finally:
        QMessageBox.question = orig

    req = json.loads((ctrl / f"request_{page._last_request_id}.json")
                     .read_text(encoding="utf-8"))
    assert req["sections"] == ["S3R5", "S4R5", "S5R5"]
    assert req["action"] == "analyze"       # named sections, not "pending"
    assert req["options"]["max_workers"] == 24
