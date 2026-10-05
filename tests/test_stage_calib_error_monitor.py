"""
Tests for stage_calib_error_monitor -- verified off-hardware against small
committed fixtures (tests/fixtures/sample_service_log.xml and
sample_error_handler_log.xml) distilled from a real mSEM service log pulled
off the microscope control PC.

Run standalone (no pytest needed):   python tests/test_stage_calib_error_monitor.py
"""
import os
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, REPO)

from stage_calib_error_monitor import _matches, _SourceTail, scan_log_file  # noqa: E402

SERVICE_LOG = os.path.join(HERE, "fixtures", "sample_service_log.xml")
ERROR_HANDLER_LOG = os.path.join(HERE, "fixtures", "sample_error_handler_log.xml")


def test_service_log_matches_only_stage_errors_not_warn_noise_or_unrelated():
    events = scan_log_file(SERVICE_LOG, source="mSEMService")
    messages = [e.message for e in events]
    assert len(events) == 3, f"expected 3 stage ERROR events, got {len(events)}: {messages}"
    assert all(e.level == "ERROR" for e in events)
    # the WARN "Failed to set variable Stage_ZPosition_Monitor..." motor-noise line must NOT appear
    assert not any("Failed to set variable" in m for m in messages)
    # the unrelated AutoFocus ERROR must NOT appear
    assert not any("AutoFocus" in m for m in messages)


def test_error_handler_log_matches_only_stage_calib_user_messages():
    events = scan_log_file(ERROR_HANDLER_LOG, source="ErrorHandler")
    assert len(events) == 1, f"expected exactly 1 match, got {len(events)}"
    assert "Stage Calibration" in events[0].message
    # the unrelated Vacboard user message and the non-"create user message" WARN must NOT appear
    assert not any("Vacboard" in e.message for e in events)


def test_matches_helper_source_semantics():
    # mSEMService: ERROR + keyword -> match; WARN + keyword -> no match (motor noise)
    assert _matches("mSEMService", "ERROR", "Failed to get stage position.")
    assert not _matches("mSEMService", "WARN", "Failed to set variable Stage_ZPosition_Monitor to value 1")
    assert not _matches("mSEMService", "ERROR", "Failed to execute matlab highlevel command AutoFocus")
    # ErrorHandler: only "create user message" lines count, regardless of level, and still keyword-gated
    assert _matches("ErrorHandler", "DEBUG", "create user message: SystemFailure | 31050: Stage Calibration")
    assert not _matches("ErrorHandler", "DEBUG", "create user message: SystemFailure | 40069: Vacboard error")
    assert not _matches("ErrorHandler", "WARN", "no methods for this error defined: SystemFailure | 31050")


def _write_event(path, ts_ms, level, message, mode="w"):
    with open(path, mode, encoding="utf-8") as f:
        f.write(
            f'<log4j:event logger="X" timestamp="{ts_ms}" level="{level}" thread="1">'
            f'<log4j:message>{message}</log4j:message></log4j:event>\n'
        )


def test_source_tail_filters_stale_content_seen_via_a_cached_file_size():
    # Reproduces the false-positive seen in the field: a network share can
    # report a stale (too-small) cached file size right when the tail starts
    # watching, which -- without the timestamp gate -- makes pre-existing old
    # log content look like it "just happened."
    with tempfile.TemporaryDirectory() as td:
        path = os.path.join(td, "mSEMService.log.xml")
        old_ts = int(time.time() * 1000) - 10 * 60 * 1000   # 10 minutes ago
        _write_event(path, old_ts, "ERROR", "Failed to get stage position.")

        tail = _SourceTail(log_dir=td, base_name="mSEMService", from_start=False)
        assert tail.poll() == []          # normal selection: pos == current size, nothing "new"

        # simulate the stale-cache bug: the size at selection time was
        # (incorrectly) smaller than the file's true current size
        tail._pos = 0
        events = tail.poll()
        assert events == [], f"a stale pre-existing event leaked through as new: {events}"

        # a genuinely fresh event appended after the tail started must still come through
        new_ts = int(time.time() * 1000)
        _write_event(path, new_ts, "ERROR", "Failed to get stage position (fresh).", mode="a")
        events = tail.poll()
        assert len(events) == 1 and "fresh" in events[0][2], f"fresh event was wrongly dropped: {events}"


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"PASS  {name}")
