"""
Tests for focus_map.diagnose_and_recover_beam_issue — the B2F "black image"
triage: Z sanity -> beam-on status -> service log arc check -> recovery.

Verified off-hardware with mocks + the same committed fixture
(tests/fixtures/sample_arc_log.xml) test_beam_arc.py uses.

Run standalone (no pytest needed):   python tests/test_beam_issue_recovery.py
"""
import os
import sys
from unittest.mock import MagicMock, patch

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, REPO)

import focus_map as fm  # noqa: E402
from beam_arc_recovery import RecoveryResult  # noqa: E402

SAMPLE_LOG = os.path.join(HERE, "fixtures", "sample_arc_log.xml")
ARC_FIRST_TS_S = 1782395056.060  # ARC_FIRST_TS from test_beam_arc.py, in seconds


def _sp():
    return fm.SupportPoint(index=0, x=100.0, y=200.0, z=1000.0, ol=0.0,
                            projective_x=0.0, projective_y=0.0)


def test_confirmed_arc_triggers_recovery_and_returns_true():
    def fake_get_double(conn, key, default=float("nan")):
        return {"AP_STAGE_AT_Z": 1000.0, "DP_IS_BEAM_ON": 0.0}.get(key, default)

    with patch.object(fm, "estimate_z_from_wafer_fiducial", return_value=None), \
         patch.object(fm, "safe_get_double", side_effect=fake_get_double), \
         patch("beam_arc_monitor.find_active_log", return_value=SAMPLE_LOG), \
         patch("time.time", return_value=ARC_FIRST_TS_S + 10), \
         patch("beam_arc_recovery.wait_for_beam_restored",
               return_value=RecoveryResult(True, "test-stub")) as mock_recover:
        assert fm.diagnose_and_recover_beam_issue(MagicMock(), _sp()) is True
        assert mock_recover.called


def test_beam_already_on_short_circuits_without_checking_log():
    with patch.object(fm, "estimate_z_from_wafer_fiducial", return_value=None), \
         patch.object(fm, "safe_get_double",
                      side_effect=lambda c, k, default=float("nan"):
                      1.0 if k == "DP_IS_BEAM_ON" else default), \
         patch("beam_arc_monitor.find_active_log") as mock_find:
        assert fm.diagnose_and_recover_beam_issue(MagicMock(), _sp()) is False
        assert not mock_find.called


def test_z_far_off_fiducial_bails_before_checking_beam():
    with patch.object(fm, "estimate_z_from_wafer_fiducial", return_value=1_000_000.0), \
         patch.object(fm, "safe_get_double",
                      side_effect=lambda c, k, default=float("nan"):
                      1000.0 if k == "AP_STAGE_AT_Z" else default) as mock_get:
        assert fm.diagnose_and_recover_beam_issue(MagicMock(), _sp()) is False
        # DP_IS_BEAM_ON should never even be queried once Z is disqualified.
        assert all(call.args[1] != "DP_IS_BEAM_ON" for call in mock_get.call_args_list)


def test_beam_off_but_no_recent_arc_in_log_does_not_recover():
    def fake_get_double(conn, key, default=float("nan")):
        return {"AP_STAGE_AT_Z": 1000.0, "DP_IS_BEAM_ON": 0.0}.get(key, default)

    with patch.object(fm, "estimate_z_from_wafer_fiducial", return_value=None), \
         patch.object(fm, "safe_get_double", side_effect=fake_get_double), \
         patch("beam_arc_monitor.find_active_log", return_value=SAMPLE_LOG), \
         patch("time.time", return_value=ARC_FIRST_TS_S + 99999), \
         patch("beam_arc_recovery.wait_for_beam_restored") as mock_recover:
        assert fm.diagnose_and_recover_beam_issue(MagicMock(), _sp()) is False
        assert not mock_recover.called


def test_beam_issue_recovery_disabled_short_circuits_immediately():
    with patch.dict(fm.CONFIG, {"beam_issue_recovery_enabled": False}), \
         patch.object(fm, "safe_get_double") as mock_get:
        assert fm.diagnose_and_recover_beam_issue(MagicMock(), _sp()) is False
        assert not mock_get.called


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"PASS  {name}")
