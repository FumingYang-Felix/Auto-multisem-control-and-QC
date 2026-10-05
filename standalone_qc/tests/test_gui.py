"""Headless tests for the standalone QC window.

Runs under Qt's "offscreen" platform plugin, so there is no display and
nothing pops up - but the real widgets are constructed and the real
discovery -> table path executes against a real output tree.

What this can and cannot cover: everything up to the point of launching a
subprocess is exercised here (folder scan, table population, row tinting,
button enable/disable, and the exact argv the CLI would be handed). Actually
running a scoring subprocess is left to the CLI's own tests - see
test_standalone_qc.py - so these stay fast and never touch image data.

    pytest standalone_qc/tests/test_gui.py
    python standalone_qc/tests/test_gui.py
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

# Must be set before QApplication is created, or Qt tries to open a display.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import pytest  # noqa: E402
from PySide6.QtCore import QSettings  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from standalone_qc.gui.runner import (  # noqa: E402
    _RE_OUTLIERS,
    _RE_PROGRESS,
    _RE_RESULT,
    _RE_SECTION,
)
from standalone_qc.gui.window import (  # noqa: E402
    COL_ACQ,
    COL_QC,
    COL_SAMPLE,
    COL_SECTION,
    COL_SIGNAL,
    QCWindow,
)
from standalone_qc.tests.test_standalone_qc import BATCH, build_fixture  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture
def window(qapp, tmp_path_factory):
    """A window backed by a throwaway ini file.

    Injected rather than redirected: on Windows QSettings defaults to the
    registry, so nudging Qt's global Ini path leaves the window still reading
    - and on close overwriting - the operator's real Image Directory, and
    refreshing against real data on the share.
    """
    scratch = tmp_path_factory.mktemp("qsettings")
    settings = QSettings(str(scratch / "test.ini"), QSettings.IniFormat)
    w = QCWindow(settings=settings)
    yield w
    w._runner.stop()
    w.deleteLater()


def test_window_builds_empty(window) -> None:
    assert window.windowTitle() == "MultiSEM Standalone QC"
    assert window._table.rowCount() == 0
    # Nothing selected and no root: every action that needs a target is off.
    assert not window._score_sel_btn.isEnabled()
    assert not window._open_report_btn.isEnabled()
    assert not window._stop_btn.isEnabled()


def test_refresh_populates_table_from_real_tree(window, tmp_path) -> None:
    build_fixture(tmp_path)
    window._root_edit.setText(str(tmp_path))
    window.refresh()

    # The fixture manifest has one done + one failed + one pending section.
    assert window._table.rowCount() == 3
    sections = [window._table.item(r, COL_SECTION).text()
                for r in range(window._table.rowCount())]
    assert sections == ["S1R1", "S2R1", "S3R1"]

    assert window._table.item(0, COL_SAMPLE).text() == BATCH
    assert window._table.item(0, COL_ACQ).text() == "done"
    assert window._table.item(0, COL_QC).text() == "awaiting analysis"
    assert window._table.item(0, COL_SIGNAL).text() == "manifest"
    # A failed section is not awaiting QC - there is nothing to score.
    assert window._table.item(1, COL_ACQ).text() == "failed"
    assert window._table.item(1, COL_QC).text() == "—"

    assert "1 awaiting analysis" in window.statusBar().currentMessage()
    # Left dock counters agree with the table.
    assert window._ind_total.value_label.text() == "3"
    assert window._ind_pending.value_label.text() == "1"
    assert window._ind_unavailable.value_label.text() == "2"


def test_batch_folder_is_the_expected_input(window, tmp_path) -> None:
    """A batch folder is what an operator points at, so it must load cleanly
    with nothing flagged - it is not a mistake to be corrected."""
    batch = build_fixture(tmp_path)
    window._root_edit.setText(str(batch))
    window.refresh()

    assert window._table.rowCount() == 3
    assert window._resolved_root == tmp_path
    assert window._sample_filter == BATCH
    assert window._notice == "", "a batch folder must not raise a diagnostic"
    # The status bar shows the plain summary, with nothing appended.
    assert "awaiting analysis" in window.statusBar().currentMessage()
    # The CLI gets the RESOLVED run folder, so the ledger lands at run level.
    args = window._base_args()
    assert args[args.index("--output-root") + 1] == str(tmp_path)


def test_run_folder_with_several_batches_says_so(window, tmp_path) -> None:
    build_fixture(tmp_path)
    # A second batch under the same run.
    other = tmp_path / "w9h9_second_8NM_20260908_090000"
    other.mkdir()
    (other / "region_manifest.json").write_text(
        json.dumps({"regions": [
            {"idx": 1, "name": "S1R1", "folder": "S1R1", "status": "pending"}
        ]}),
        encoding="utf-8",
    )
    window._root_edit.setText(str(tmp_path))   # the run folder
    window.refresh()

    assert window._sample_filter is None
    # A run folder still loads, so this notice has to survive the summary
    # the status bar shows at the end of a successful refresh.
    msg = window.statusBar().currentMessage()
    assert "run folder" in msg and "2 batch" in msg
    assert "awaiting analysis" in msg, "the summary must still be there too"


def test_empty_folder_gets_a_diagnostic_not_a_shrug(window, tmp_path) -> None:
    """"No Sample folders found here" while looking at a screen full of data
    is not a useful thing to be told - it must say what it did see."""
    section_like = tmp_path / "looks_like_a_section"
    (section_like / "mfov1").mkdir(parents=True)
    window._root_edit.setText(str(section_like))
    window.refresh()
    assert "one level up" in window.statusBar().currentMessage()

    stray = tmp_path / "unrelated"
    (stray / "something_else").mkdir(parents=True)
    window._root_edit.setText(str(stray))
    window.refresh()
    text = window.statusBar().currentMessage()
    assert "region_manifest.json" in text and "something_else" in text

    bare = tmp_path / "bare"
    bare.mkdir()
    window._root_edit.setText(str(bare))
    window.refresh()
    assert "no subfolders at all" in window.statusBar().currentMessage()


def test_bad_root_clears_table_without_raising(window, tmp_path) -> None:
    build_fixture(tmp_path)
    window._root_edit.setText(str(tmp_path))
    window.refresh()
    assert window._table.rowCount() == 3

    window._root_edit.setText(str(tmp_path / "nope"))
    window.refresh()
    assert window._table.rowCount() == 0
    assert "Not a folder" in window.statusBar().currentMessage()


def test_only_scorable_sections_can_be_scored(window, tmp_path) -> None:
    build_fixture(tmp_path)
    window._root_edit.setText(str(tmp_path))
    window.refresh()

    window._table.selectRow(0)          # S1R1, done
    assert window._score_sel_btn.isEnabled()
    assert window._open_report_btn.isEnabled()   # enabled; warns if absent

    window._table.selectRow(1)          # S2R1, failed
    assert not window._score_sel_btn.isEnabled()
    assert "not finished imaging" in window.statusBar().currentMessage()


def test_argv_handed_to_the_cli(window, tmp_path) -> None:
    build_fixture(tmp_path)
    window._root_edit.setText(str(tmp_path))
    window.refresh()
    window._workers.setValue(12)

    args = window._base_args()
    assert args[:2] == ["--output-root", str(tmp_path)]
    assert "--workers" in args and args[args.index("--workers") + 1] == "12"
    # --exact is non-negotiable: the GUI names concrete rows, and substring
    # matching would let a pick of "S2R1" also score "S2R10".
    assert "--exact" in args
    # The overview is always on - there is no GUI opt-out - so the only
    # overview argument ever passed is its downsample factor.
    assert "--no-overview" not in args
    window._overview_ds.setValue(32)
    args = window._base_args()
    assert args[args.index("--overview-ds") + 1] == "32"

    # The map pass has no GUI opt-out: turning it off removes most of the
    # report (per-mFoV quality, IMQA, scanfault, jitter, the retake lasso),
    # and it is not where the time goes. --no-map-pass still exists for
    # command-line troubleshooting.
    assert not hasattr(window, "_probe")
    assert "--no-probe" not in args and "--no-map-pass" not in args

    assert "--force" not in args
    window._rescore.setChecked(True)
    assert "--force" in window._base_args()


def test_every_n_only_passed_in_that_mode(window) -> None:
    window._sampling.setCurrentIndex(window._sampling.findData("first_mid_last_tissue"))
    assert "--every-n" not in window._base_args()
    assert not window._every_n.isVisible()

    window._sampling.setCurrentIndex(window._sampling.findData("every_n"))
    window._every_n.setValue(40)
    args = window._base_args()
    assert args[args.index("--every-n") + 1] == "40"


def test_queue_targets_each_section_exactly(window, tmp_path) -> None:
    build_fixture(tmp_path)
    window._root_edit.setText(str(tmp_path))
    window.refresh()

    # Build the queue without letting it launch anything.
    window._next_job = lambda: None  # type: ignore[method-assign]
    window._start_queue(window._pending_regions())

    assert len(window._queue) == 1
    job = window._queue[0]
    assert job["key"] == f"{BATCH}/S1R1"
    args = job["args"]
    assert args[args.index("--sample") + 1] == BATCH
    assert args[args.index("--section") + 1] == "S1R1"


def test_retake_manager_exists_before_any_scoring(window, tmp_path) -> None:
    """The whole point of the post-acquisition flow: pointing at a wafer or
    batch gives a working retake manager immediately. It comes from the
    acquisition manifests, so it must not wait on image scoring."""
    build_fixture(tmp_path)
    window._root_edit.setText(str(tmp_path))
    window.refresh()

    from standalone_qc import rollup
    page = tmp_path / rollup.GLOBAL_REPORT_NAME
    assert page.is_file(), "Refresh must build the retake manager"
    html = page.read_text(encoding="utf-8")
    # Review controls live and usable with nothing scored yet.
    assert html.count('class="review-group"') == 3
    assert "0/3 section(s) analyzed (0%)" in html
    assert window._retake_btn.isEnabled()


def test_mode_selector_swaps_the_actions(window, tmp_path) -> None:
    build_fixture(tmp_path)
    window._root_edit.setText(str(tmp_path))
    window.refresh()

    window._mode.setCurrentIndex(window._mode.findData("post"))
    assert not window._watch_btn.isVisible() or window._watch_btn.isHidden()
    assert not window._score_all_btn.isHidden()
    assert window._watch_interval.isHidden()

    window._mode.setCurrentIndex(window._mode.findData("realtime"))
    assert not window._watch_btn.isHidden()
    assert not window._watch_interval.isHidden()
    # Realtime ADDS the watch controls; it does not take the manual ones
    # away. An operator watching a run must still be able to analyze on
    # demand - see test_manual_analysis_works_while_watching.
    assert not window._score_all_btn.isHidden()


def test_manual_analysis_works_while_watching(window, tmp_path) -> None:
    """Watching must not disable the analysis buttons.

    Reported from a live run: two sections had failed and a third was still
    imaging, and every analysis button was greyed out with only "Stop
    Watching" live - so the only way to analyze anything by hand was to stop
    watching first, which is precisely when an operator does not want to.

    There is no conflict to prevent: _on_watch_tick returns early while a
    run or queue is outstanding, so a manual run just makes the next poll a
    no-op.
    """
    build_fixture(tmp_path)
    window._root_edit.setText(str(tmp_path))
    window.refresh()
    window._mode.setCurrentIndex(window._mode.findData("realtime"))
    window._next_job = lambda: None  # type: ignore[method-assign]

    window._watching = True
    window._sync_buttons()

    assert window._score_all_btn.isEnabled(), "Analyze All Pending is dead"
    assert not window._score_all_btn.isHidden()
    window._table.selectRow(0)                       # S1R1, finished imaging
    window._sync_buttons()
    assert window._score_sel_btn.isEnabled(), "Analyze Selected is dead"

    # And pressing one actually queues work while watching.
    window._on_score_selected()
    assert len(window._queue) == 1

    # A watch poll during that queue is a no-op rather than a second run.
    before = list(window._queue)
    window._on_watch_tick()
    assert window._queue == before


def test_a_queued_section_is_never_dropped(window, tmp_path) -> None:
    """Two paths used to lose work: a manual queue REPLACED the watcher's,
    and _next_job popped a job before launching it, so a failed launch
    discarded that section."""
    build_fixture(tmp_path)
    window._root_edit.setText(str(tmp_path))
    window.refresh()
    window._next_job = lambda: None  # type: ignore[method-assign]

    window._start_queue(window._pending_regions(), announce=False)
    first = list(window._queue)
    assert first

    # A second queue adds to the first instead of replacing it.
    window._table.selectRow(0)
    window._on_score_selected()
    assert len(window._queue) == len(first) + 1
    assert first[0] in window._queue


def test_disabled_buttons_explain_themselves(window, tmp_path) -> None:
    build_fixture(tmp_path)
    window._root_edit.setText(str(tmp_path))
    window.refresh()

    window._table.selectRow(1)          # S2R1 - failed, still not imaged
    window._sync_buttons()
    assert not window._score_sel_btn.isEnabled()
    assert "finished imaging" in window._score_sel_btn.toolTip()


def test_watching_starts_stops_and_queues(window, tmp_path, monkeypatch) -> None:
    build_fixture(tmp_path)
    window._root_edit.setText(str(tmp_path))
    window.refresh()
    window._mode.setCurrentIndex(window._mode.findData("realtime"))

    started: list = []
    monkeypatch.setattr(window._runner, "start", lambda args: started.append(args))

    window._start_watching()
    assert window._watching
    assert window._watch_timer.isActive()
    # The first tick happens immediately, so a section that finished before
    # watching began is picked up rather than waiting a whole interval.
    assert len(started) == 1
    assert "--section" in started[0]

    window._stop_watching()
    assert not window._watching
    assert not window._watch_timer.isActive()


def test_watch_tick_does_not_pile_up_runs(window, tmp_path, monkeypatch) -> None:
    build_fixture(tmp_path)
    window._root_edit.setText(str(tmp_path))
    window.refresh()

    monkeypatch.setattr(window._runner, "is_running", lambda: True)
    started: list = []
    monkeypatch.setattr(window._runner, "start", lambda args: started.append(args))
    window._watching = True
    window._on_watch_tick()
    # A slow section must never let ticks stack up overlapping runs.
    assert started == []


def test_action_row_order(window, tmp_path) -> None:
    """Left to right: Analyze All Pending, Analyze Selected, Open Retake
    Manager, Stop - then the per-section openers."""
    build_fixture(tmp_path)
    window._root_edit.setText(str(tmp_path))
    window.refresh()

    # Found by content, not by index: this used to be itemAt(4), which
    # silently became the wrong row the moment a widget above it was
    # removed. The action row is the one holding the Analyze buttons.
    widgets = []
    for i in range(window.layout_page.count()):
        row = window.layout_page.itemAt(i).layout()
        if row is None:
            continue
        found = [row.itemAt(j).widget() for j in range(row.count())]
        found = [w for w in found if w is not None]
        if window._score_all_btn in found:
            widgets = found
            break
    assert widgets, "no layout row contains the Analyze All Pending button"

    order = [widgets.index(b) for b in (
        window._score_all_btn, window._score_sel_btn,
        window._retake_btn, window._stop_btn,
    )]
    assert order == sorted(order), "buttons are out of the requested order"
    assert widgets.index(window._score_all_btn) == 0, "Analyze All Pending must be first"
    assert (widgets.index(window._retake_btn)
            < widgets.index(window._stop_btn)), "Retake Manager must precede Stop"

    # No Scan Sections button any more - analysis populates those columns.
    assert not hasattr(window, "_scan_btn")
    assert not hasattr(window, "_on_scan")


def test_retake_menu_offers_global_and_each_batch(window, tmp_path) -> None:
    build_fixture(tmp_path)
    other = tmp_path / "w9h9_second_8NM_20260908_090000"
    other.mkdir()
    (other / "region_manifest.json").write_text(
        json.dumps({"regions": [
            {"idx": 1, "name": "S1R1", "folder": "S1R1", "status": "done"}
        ]}),
        encoding="utf-8",
    )
    window._root_edit.setText(str(tmp_path))
    window.refresh()

    window._populate_retake_menu()
    labels = [a.text() for a in window._retake_menu.actions() if not a.isSeparator()]
    assert labels[0] == "Global — whole run"
    assert any(BATCH in t for t in labels)
    assert any("w9h9_second" in t for t in labels)

    # Each entry must open its OWN batch - a bare lambda over the loop
    # variable would make every entry open the last one.
    opened: list = []
    window._open_batch_report = lambda d: opened.append(d.name)
    window._populate_retake_menu()
    for a in window._retake_menu.actions():
        if a.text().startswith("Batch — "):
            a.trigger()
    assert sorted(opened) == sorted([BATCH, "w9h9_second_8NM_20260908_090000"])


def test_retake_menu_lists_every_batch_even_when_pointed_at_one(
    window, tmp_path
) -> None:
    """The reported bug: the menu was built from self._regions, which is
    filtered to whichever batch the Image Directory names. Pointing at one
    batch - the normal way to analyze - left the menu offering only that
    batch, with no way to reach any other attempt at the same sections.
    """
    build_fixture(tmp_path)
    other = tmp_path / "w9h9_second_8NM_20260908_090000"
    other.mkdir()
    (other / "region_manifest.json").write_text(
        json.dumps({"regions": [
            {"idx": 1, "name": "S1R1", "folder": "S1R1", "status": "done"}
        ]}),
        encoding="utf-8",
    )

    # Point at ONE batch, not the run folder.
    window._root_edit.setText(str(tmp_path / BATCH))
    window.refresh()
    assert window._sample_filter == BATCH, "fixture should resolve to one batch"
    assert {r.block_dir.name for r in window._regions} == {BATCH},         "the table is filtered to one batch - that is correct"

    window._populate_retake_menu()
    labels = [a.text() for a in window._retake_menu.actions()
              if not a.isSeparator()]
    assert labels[0] == "Global — whole run"
    # BOTH batches, including the other one the table never mentions.
    assert any(BATCH in t for t in labels), labels
    assert any("w9h9_second" in t for t in labels), labels

    # The batch currently pointed at is marked, so it is identifiable among
    # several similar timestamps.
    assert any(BATCH in t and "Image Directory" in t for t in labels), labels

    # A batch with no report on disk is still listed, and says so, rather
    # than being hidden - hiding it would hide the batch being analyzed
    # right now, which is the one most likely to be wanted. (refresh()
    # builds reports from the acquisition manifests, so remove one to get
    # a batch into that state.)
    from standalone_qc import rollup as _rollup

    (other / _rollup.BATCH_REPORT_NAME).unlink(missing_ok=True)
    window._populate_retake_menu()
    labels = [a.text() for a in window._retake_menu.actions()
              if not a.isSeparator()]
    assert any("w9h9_second" in t and "no report yet" in t for t in labels), labels
    assert not any(BATCH in t and "no report yet" in t for t in labels), labels

    opened: list = []
    window._open_batch_report = lambda d: opened.append(d.name)
    window._populate_retake_menu()
    for a in window._retake_menu.actions():
        if a.text().startswith("Batch — "):
            a.trigger()
    assert sorted(opened) == sorted([BATCH, "w9h9_second_8NM_20260908_090000"])


def test_failed_section_is_not_retried_forever(window, tmp_path) -> None:
    """A section that fails to score gets an "error" ledger entry, not
    "scored". Without skip_failed the watcher would re-queue it on every
    poll - hundreds of re-reads across a long unattended run."""
    from standalone_qc import ledger
    from standalone_qc.config import policy_fingerprint

    build_fixture(tmp_path)
    window._root_edit.setText(str(tmp_path))
    window.refresh()
    key = f"{BATCH}/S1R1"
    assert [r.key for r in window._pending_regions()] == [key]

    # Simulate the CLI recording a failure for it.
    ledger.mark(tmp_path, key, policy_fingerprint(window._build_cfg()),
                status="error", detail={"error": "boom"})

    # Manual scoring still offers it...
    assert [r.key for r in window._pending_regions()] == [key]
    # ...but automatic queueing does not.
    assert window._pending_regions(skip_failed=True) == []
    assert window._failed_keys() == [key]

    started: list = []
    monkeypatch_start = lambda args: started.append(args)  # noqa: E731
    window._runner.start = monkeypatch_start
    window._watching = True
    window._on_watch_tick()
    assert started == [], "watcher must not re-queue a failed section"

    # And it says so once, not on every poll.
    window._on_watch_tick()
    window._on_watch_tick()
    assert len(window._reported_failures) == 1


def test_scored_section_is_not_requeued(window, tmp_path) -> None:
    from standalone_qc import ledger
    from standalone_qc.config import policy_fingerprint

    build_fixture(tmp_path)
    window._root_edit.setText(str(tmp_path))
    window.refresh()
    ledger.mark(tmp_path, f"{BATCH}/S1R1",
                policy_fingerprint(window._build_cfg()), status="scored")
    assert window._pending_regions() == []
    assert window._pending_regions(skip_failed=True) == []


def test_analyze_selected_forces_even_when_already_analyzed(
    window, tmp_path
) -> None:
    """Naming rows and pressing Analyze Selected must analyze THOSE rows.

    Regression: the button built its arguments without --force, so the CLI's
    ledger filter silently dropped every already-analyzed section. Selecting
    all 24 rows of a batch to pick up a newly added check re-analyzed only
    the sections that happened to be new, and the operator's only clue was
    that the batch report still showed the old numbers.
    """
    from standalone_qc import ledger
    from standalone_qc.config import policy_fingerprint

    build_fixture(tmp_path)
    window._root_edit.setText(str(tmp_path))
    window.refresh()
    ledger.mark(tmp_path, f"{BATCH}/S1R1",
                policy_fingerprint(window._build_cfg()), status="scored")

    window._next_job = lambda: None  # type: ignore[method-assign]
    window._table.selectRow(0)                      # S1R1, done + analyzed
    assert window._selected_regions(), "row 0 should resolve to a section"
    window._on_score_selected()

    assert len(window._queue) == 1
    assert "--force" in window._queue[0]["args"]

    # "Analyze All Pending" keeps the filter - that is what pending means.
    # announce=False: with nothing pending, _start_queue puts up a modal
    # "Nothing to do" box that would hang the suite.
    window._queue = []
    window._start_queue(window._pending_regions(), announce=False)
    assert window._queue == []


def test_runner_parses_real_cli_output() -> None:
    """The regexes must match the CLI's actual wording - these lines are
    copied verbatim from a real run against Z:\\, so a change to the CLI's
    output format fails here instead of silently killing the progress bar."""
    m = _RE_PROGRESS.match("    [ 43.0%] 96/223  probe tile 94")
    assert m and (int(m.group(2)), int(m.group(3))) == (96, 223)

    m = _RE_SECTION.match("[StandaloneQC] === w10h31_dye_R4_8NM_20260907_085700/S2R3 ===")
    assert m and m.group(1) == "w10h31_dye_R4_8NM_20260907_085700/S2R3"

    m = _RE_RESULT.match(
        "    mfovs=223 blank=0 analyzed=3 images_read=496 median_q=19.33 in 11.9s"
    )
    assert m and m.group(1) == "223" and m.group(5) == "19.33"

    m = _RE_OUTLIERS.match("    OUTLIER BEAM(S): [13, 14, 28, 64]")
    assert m and m.group(1) == "[13, 14, 28, 64]"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))


# ---------------------------------------------------------------------------
# service mode: the acquisition computer asks, this computer analyzes
# ---------------------------------------------------------------------------
def test_service_mode_shows_its_own_controls(window, tmp_path) -> None:
    build_fixture(tmp_path)
    window._root_edit.setText(str(tmp_path))
    window.refresh()

    window._mode.setCurrentIndex(window._mode.findData("post"))
    assert window._service_btn.isHidden()
    assert window._control_row.isHidden()

    window._mode.setCurrentIndex(window._mode.findData("service"))
    assert not window._service_btn.isHidden()
    assert not window._control_row.isHidden()
    # The watch controls belong to realtime, not here.
    assert window._watch_interval.isHidden()


def test_service_needs_a_control_folder(window, tmp_path, monkeypatch) -> None:
    """Starting with no folder must explain itself rather than launching an
    agent that watches nothing."""
    from PySide6.QtWidgets import QMessageBox

    shown = {}
    monkeypatch.setattr(QMessageBox, "information",
                        lambda *a, **k: shown.update(title=a[1], text=a[2]))
    window._mode.setCurrentIndex(window._mode.findData("service"))
    window._control_root.clear()
    window._on_toggle_service()

    assert "control folder" in shown.get("title", "").lower()
    assert "UNC" in shown.get("text", "")
    assert not window._agent.is_running()


def test_service_launches_the_agent_not_the_scorer(window, tmp_path) -> None:
    """The agent is a separate long-lived process: Stop (which kills a
    scoring run) must not kill it, and it must be the agent module."""
    launched = {}

    def fake_start(args, module="standalone_qc"):
        launched["args"] = list(args)
        launched["module"] = module

    window._mode.setCurrentIndex(window._mode.findData("service"))
    window._control_root.setText(str(tmp_path / "ctrl"))
    window._agent.start = fake_start  # type: ignore[method-assign]
    window._on_toggle_service()

    assert launched["module"] == "standalone_qc.agent"
    assert launched["args"][0] == "--control-root"
    assert launched["args"][1] == str(tmp_path / "ctrl")
    # Two distinct runners, so Stop cannot take the service down with it.
    assert window._agent is not window._runner


def test_leaving_service_mode_stops_the_service(window, tmp_path) -> None:
    """A running agent while the window says 'Post-acquisition' would be
    serving requests with nothing on screen saying so."""
    stopped = {"n": 0}
    window._mode.setCurrentIndex(window._mode.findData("service"))
    window._control_root.setText(str(tmp_path / "ctrl"))
    window._agent.is_running = lambda: True  # type: ignore[method-assign]
    window._agent.stop = lambda: stopped.update(n=stopped["n"] + 1)  # type: ignore[method-assign]

    window._mode.setCurrentIndex(window._mode.findData("post"))
    assert stopped["n"] == 1


def test_quality_threshold_reaches_the_cli_and_the_config(window, tmp_path) -> None:
    """The threshold sets colours and tiers, so it has to reach both the
    subprocess that writes the pages and the config the QC column is judged
    against."""
    build_fixture(tmp_path)
    window._root_edit.setText(str(tmp_path))
    window.refresh()
    window._q_low.setValue(12.5)
    window._q_high.setValue(45.0)

    args = window._base_args()
    assert args[args.index("--quality-threshold") + 1] == "12.5"
    assert args[args.index("--high-quality-threshold") + 1] == "45.0"

    cfg = window._build_cfg()
    assert cfg["low_quality_threshold"] == 12.5
    assert cfg["high_quality_threshold"] == 45.0


def test_threshold_does_not_invalidate_analyzed_sections(window) -> None:
    """Moving the threshold must NOT mark work as needing redoing: it
    changes no measurement, only how results are coloured. If it entered the
    policy fingerprint, nudging it would grey out every analyzed section."""
    from standalone_qc.config import policy_fingerprint

    window._q_low.setValue(10.0)
    before = policy_fingerprint(window._build_cfg())
    window._q_low.setValue(25.0)
    window._q_high.setValue(60.0)
    assert policy_fingerprint(window._build_cfg()) == before
