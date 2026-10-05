"""Unit tests for the SAMPLE_TOPOGRAPHY_CORRECTION wafer-fiducial drift
precheck and the hard stage-Z safety ceiling it was added alongside.

Hardware-free: same comtypes stub pattern as test_wafer_fiducial_direct.py.
Does NOT exercise precheck_and_correct_wafer_fiducial_planes() or
measure_topography_check_point() end-to-end - both drive real stage/AF/AS/
B2F hardware calls that aren't meaningfully fakeable at unit-test scope.
Covers the parts that ARE safely unit-testable: point selection, the robust
offset math, and - most importantly, since this is a hardware-safety
feature - that _safe_goto_z actually refuses a Z above the hard ceiling and
lets a normal one through.

Run:  python tests/test_topography_precheck.py      (exit 0 = pass)
Also discoverable by pytest.
"""
import os
import sys
import tempfile
import types
from unittest.mock import patch

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

try:  # pragma: no cover - trivial import guard
    import comtypes  # noqa: F401
except Exception:
    _ct = types.ModuleType("comtypes")
    _cc = types.ModuleType("comtypes.client")
    _cc.CreateObject = lambda *a, **k: None
    _ct.client = _cc
    sys.modules["comtypes"] = _ct
    sys.modules["comtypes.client"] = _cc

import focus_map as fm  # noqa: E402


def _write(text):
    tmp = tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False, newline="")
    tmp.write(text)
    tmp.close()
    return tmp.name


class _FakeConn:
    def __init__(self):
        self.calls = []

    def set_double(self, name, value):
        self.calls.append((name, value))

    def execute(self, cmd):
        self.calls.append((cmd,))
        return 0


def test_minimize_ol_refuses_to_correct_from_an_implausible_z():
    """The real incident this guards against: measure_topography_check_point's
    call into minimize_ol_lens_zen_style hit a live Z read of -ish 0 (the
    telemetry-glitch/fault pattern seen elsewhere), and with no check on that
    reading, computed a Z-nudge landing at -2.7 um - only caught by
    _safe_goto_z's LAST-RESORT hard ceiling, which aborted the entire run
    (StageZHardLimitError) instead of just failing this one measurement.
    z0's own sanity check must now catch this before any Z move is even
    attempted, returning (False, nan) like any other failed measurement."""
    conn = _FakeConn()
    # dz far outside dz_limit_um so the loop doesn't just early-exit as
    # "already converged" before ever reading z0.
    with patch.object(fm, "get_stage_z_stable", return_value=0.0), \
         patch.object(fm, "safe_get_double", return_value=500.0):
        ok, dz = fm.minimize_ol_lens_zen_style(conn)
    assert ok is False
    assert dz != dz  # NaN
    assert conn.calls == [], "must NOT have commanded any Z move or OL change"


def test_safe_goto_z_allows_normal_value():
    conn = _FakeConn()
    fm._safe_goto_z(conn, 38900.0, context="test")
    assert conn.calls == [("AP_STAGE_GOTO_Z", 38900.0)]


def test_safe_goto_z_allows_none():
    conn = _FakeConn()
    fm._safe_goto_z(conn, None, context="test")
    assert conn.calls == []


def test_safe_goto_z_refuses_above_hard_ceiling():
    conn = _FakeConn()
    try:
        fm._safe_goto_z(conn, 39101.0, context="test")
        assert False, "expected StageZHardLimitError"
    except fm.StageZHardLimitError:
        pass
    assert conn.calls == [], "must NOT have commanded the unsafe Z"


def test_safe_goto_z_allows_exactly_at_ceiling():
    conn = _FakeConn()
    fm._safe_goto_z(conn, 39100.0, context="test")
    assert conn.calls == [("AP_STAGE_GOTO_Z", 39100.0)]


def test_safe_goto_z_refuses_below_floor():
    """The real incident this guards against: minimize_ol_lens_zen_style
    applying an uncapped Z correction computed from a bogus coarse-AF dz
    and driving the stage toward Z=0 with nothing catching it beforehand."""
    conn = _FakeConn()
    try:
        fm._safe_goto_z(conn, 0.0, context="test")
        assert False, "expected StageZHardLimitError"
    except fm.StageZHardLimitError:
        pass
    assert conn.calls == [], "must NOT have commanded the unsafe Z"


def test_safe_goto_z_allows_exactly_at_floor():
    conn = _FakeConn()
    fm._safe_goto_z(conn, 1000.0, context="test")
    assert conn.calls == [("AP_STAGE_GOTO_Z", 1000.0)]


def test_select_topography_precheck_points_skips_incomplete_rows():
    path = _write(
        "x,y,z,ol,stigx,stigy,projx,projy\n"
        "0.0,0.0,38900.0,10.0,0.1,-0.1,-610.0,670.0\n"      # complete
        "1000.0,0.0,38900.0,11.0,0.2,-0.2,\n"                 # missing proj (incomplete)
        "0.0,1000.0,38900.0,12.0,0.3,-0.3,-611.0,671.0\n"    # complete
        "1000.0,1000.0,38900.0,13.0,0.4,-0.4,-612.0,672.0\n" # complete
        "500.0,500.0,38900.0,14.0,0.5,-0.5,-613.0,673.0\n"   # complete
    )
    try:
        pts = fm._select_topography_precheck_points(path, 4)
        assert len(pts) == 4
        # the incomplete row's (x, y) must never be selected
        assert (1000.0, 0.0) not in pts
    finally:
        os.unlink(path)


def test_select_topography_precheck_points_caps_at_available():
    path = _write(
        "x,y,z,ol,stigx,stigy,projx,projy\n"
        "0.0,0.0,38900.0,10.0,0.1,-0.1,-610.0,670.0\n"
        "1000.0,1000.0,38900.0,13.0,0.4,-0.4,-612.0,672.0\n"
    )
    try:
        pts = fm._select_topography_precheck_points(path, 4)
        assert len(pts) == 2
    finally:
        os.unlink(path)


def test_robust_median_offset_plain_median():
    assert fm._robust_median_offset([1.0, 2.0, 3.0]) == 2.0


def test_robust_median_offset_drops_outlier():
    # 3 tight values + 1 wild outlier - MAD filter should drop the outlier
    # and the result should stay close to the tight cluster, not be dragged
    # toward the outlier.
    vals = [10.0, 10.5, 9.5, 500.0]
    result = fm._robust_median_offset(vals)
    assert abs(result - 10.0) < 1.0


def test_safe_goto_z_backlash_disabled_by_default_is_single_move():
    conn = _FakeConn()
    assert fm.CONFIG.get("z_backlash_enabled", False) is False
    fm._safe_goto_z(conn, 38900.0, context="test")
    assert conn.calls == [("AP_STAGE_GOTO_Z", 38900.0)]


def test_safe_goto_z_backlash_overshoots_from_below_then_approaches_target():
    conn = _FakeConn()
    saved = {k: fm.CONFIG.get(k) for k in
              ("z_backlash_enabled", "z_backlash_um", "z_backlash_approach_from_below")}
    try:
        fm.CONFIG["z_backlash_enabled"] = True
        fm.CONFIG["z_backlash_um"] = 5.0
        fm.CONFIG["z_backlash_approach_from_below"] = True
        fm._safe_goto_z(conn, 38900.0, context="test")
        # overshoot BELOW the target first (final approach must be upward),
        # settle, THEN the real target - in that exact order.
        assert conn.calls == [
            ("AP_STAGE_GOTO_Z", 38895.0),
            ("CMD_WAIT_STAGE_SETTLED",),
            ("AP_STAGE_GOTO_Z", 38900.0),
        ]
    finally:
        fm.CONFIG.update(saved)


def test_safe_goto_z_backlash_overshoots_from_above_when_configured():
    conn = _FakeConn()
    saved = {k: fm.CONFIG.get(k) for k in
              ("z_backlash_enabled", "z_backlash_um", "z_backlash_approach_from_below")}
    try:
        fm.CONFIG["z_backlash_enabled"] = True
        fm.CONFIG["z_backlash_um"] = 5.0
        fm.CONFIG["z_backlash_approach_from_below"] = False
        fm._safe_goto_z(conn, 38900.0, context="test")
        assert conn.calls == [
            ("AP_STAGE_GOTO_Z", 38905.0),
            ("CMD_WAIT_STAGE_SETTLED",),
            ("AP_STAGE_GOTO_Z", 38900.0),
        ]
    finally:
        fm.CONFIG.update(saved)


def test_safe_goto_z_backlash_overshoot_still_respects_hard_ceiling():
    conn = _FakeConn()
    saved = {k: fm.CONFIG.get(k) for k in
              ("z_backlash_enabled", "z_backlash_um", "z_backlash_approach_from_below")}
    try:
        fm.CONFIG["z_backlash_enabled"] = True
        fm.CONFIG["z_backlash_um"] = 5.0
        fm.CONFIG["z_backlash_approach_from_below"] = False  # overshoot ABOVE target
        try:
            fm._safe_goto_z(conn, 39099.0, context="test")  # overshoot would be 39104 > ceiling
            assert False, "expected StageZHardLimitError"
        except fm.StageZHardLimitError:
            pass
        assert conn.calls == [], "must NOT have commanded anything, including the overshoot"
    finally:
        fm.CONFIG.update(saved)


if __name__ == "__main__":
    fails = 0
    for name, fn in sorted(list(globals().items())):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS {name}")
            except Exception as exc:
                fails += 1
                print(f"FAIL {name}: {exc}")
    sys.exit(1 if fails else 0)
