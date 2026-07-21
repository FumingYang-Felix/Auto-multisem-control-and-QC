"""Unit tests for the MANUAL_POINTS plane update (issue #8).

Hardware-free: focus_map's `from msem import ...` pulls in Windows-only comtypes,
so on a dev machine we stub it (only if the real one is absent) before importing
focus_map. Then we drive the manual-focus functions with a fake connection and
assert: file parsing + region matching, SKIP_AF applies OL/stig verbatim with NO
CMD_AUTOFOCUS/CMD_AUTOSTIG, the plane fit reproduces a known plane, a single point
falls back to a flat plane, and placement-only points route to the AF path.

Run:  python tests/test_manual_focus_points.py      (exit 0 = pass)
Also discoverable by pytest.
"""
import json
import os
import sys
import tempfile
import types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

# Stub comtypes ONLY when it isn't installed (i.e. not on the Windows scope PC),
# so we never shadow the real COM stack during on-hardware test runs.
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


class FakeConn:
    """Records set_double calls + executed commands; get_double echoes sets."""

    def __init__(self):
        self.sets = {}
        self.executed = []

    def set_double(self, key, value):
        self.sets[key] = value

    def get_double(self, key):
        return self.sets.get(key, 0.0)

    def execute(self, key):
        self.executed.append(key)
        return 0


def _ol_plane(x, y):
    """Known plane so a least-squares fit is exactly recoverable."""
    return 2.0 * x + 3.0 * y + 100.0


def _write_manual_file(region="S25R1"):
    pts = [(1000.0, 500.0), (4000.0, 500.0), (1000.0, 3500.0), (4000.0, 3500.0)]
    doc = {"regions": {region: {"points": [
        {"x": x, "y": y, "z": 38500.0, "ol_uA": _ol_plane(x, y),
         "stig_x": 0.02, "stig_y": -0.01}
        for (x, y) in pts]}}}
    tmp = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
    json.dump(doc, tmp)
    tmp.close()
    return tmp.name


def test_defaults_off_by_default():
    assert fm.CONFIG["plane_update_method"] == "ONE_POINT_OFFSET"
    assert fm.CONFIG["manual_focus_points_file"] is None
    assert fm.CONFIG["manual_focus_mode"] == "SKIP_AF"
    assert fm.CONFIG["manual_focus_seed_range_uA"] == 300


def test_parse_and_region_matching():
    path = _write_manual_file()
    try:
        pts = fm.load_manual_focus_points(path, "S25R1")
        assert len(pts) == 4
        assert pts[0]["x"] == 1000.0 and pts[0]["y"] == 500.0
        assert abs(pts[0]["ol_uA"] - _ol_plane(1000.0, 500.0)) < 1e-9
        try:
            fm.load_manual_focus_points(path, "NOPE")
            assert False, "missing region should raise"
        except RuntimeError:
            pass
    finally:
        os.unlink(path)


def test_skip_af_applies_verbatim_no_af_as():
    path = _write_manual_file()
    saved_mode = fm.CONFIG["manual_focus_mode"]
    try:
        pts = fm.load_manual_focus_points(path, "S25R1")
        sps = fm.build_manual_support_points(pts)
        fm.CONFIG["manual_focus_mode"] = "SKIP_AF"
        conn = FakeConn()
        ms, skipped = fm.measure_manual_focus_points(conn, sps, pts)
        assert len(ms) == 4 and len(skipped) == 0
        assert abs(conn.sets["AP_OLENS_CURRENT"] - _ol_plane(4000.0, 3500.0)) < 1e-9
        assert conn.sets["AP_STIG_X"] == 0.02 and conn.sets["AP_STIG_Y"] == -0.01
        assert "CMD_AUTOFOCUS" not in conn.executed
        assert "CMD_AUTOSTIG" not in conn.executed
        assert "CMD_WAIT_STAGE_SETTLED" in conn.executed
    finally:
        fm.CONFIG["manual_focus_mode"] = saved_mode
        os.unlink(path)


def test_plane_fit_and_flat_fallback():
    path = _write_manual_file()
    try:
        pts = fm.load_manual_focus_points(path, "S25R1")
        sps = fm.build_manual_support_points(pts)
        ms, _ = fm.measure_manual_focus_points(FakeConn(), sps, pts)
        a, b, c = fm._fit_focus_plane_or_flat([(m.x, m.y, m.ol_uA) for m in ms], "OL")
        assert abs(a - 2.0) < 1e-6 and abs(b - 3.0) < 1e-6 and abs(c - 100.0) < 1e-3
        tx, ty = 2500.0, 2000.0
        assert abs((a * tx + b * ty + c) - _ol_plane(tx, ty)) < 1e-3
        # single point -> flat plane at that OL
        a2, b2, c2 = fm._fit_focus_plane_or_flat([(1234.0, 5678.0, 3120.0)], "OL")
        assert a2 == 0.0 and b2 == 0.0 and abs(c2 - 3120.0) < 1e-9
    finally:
        os.unlink(path)


def test_placement_only_routes_to_af():
    orig = fm.measure_one_focus_point
    calls = {"n": 0}

    def fake_measure(conn, sp, skip_minimize_ol=False):
        calls["n"] += 1
        return fm.FocusMeasurement(
            sp_index=sp.index, x=sp.x, y=sp.y, z_stage=0.0, ol_uA=999.0, dz_um=0.0,
            af_conf_fine=1.0, af_conf_coarse=float("nan"), af_success=True,
            minimize_ol_success=True, dz_after_minimize=0.0,
            as_conf_x=1.0, as_conf_y=1.0, as_success=True, stig_x=0.0, stig_y=0.0)

    fm.measure_one_focus_point = fake_measure
    try:
        pts = [{"x": 100.0, "y": 200.0, "z": None, "ol_uA": None,
                "stig_x": None, "stig_y": None}]
        sps = fm.build_manual_support_points(pts)
        ms, _ = fm.measure_manual_focus_points(FakeConn(), sps, pts)
        assert calls["n"] == 1 and len(ms) == 1
    finally:
        fm.measure_one_focus_point = orig


if __name__ == "__main__":
    tests = [
        test_defaults_off_by_default,
        test_parse_and_region_matching,
        test_skip_af_applies_verbatim_no_af_as,
        test_plane_fit_and_flat_fallback,
        test_placement_only_routes_to_af,
    ]
    for t in tests:
        t()
        print(f"OK: {t.__name__}")
    print(f"\nAll {len(tests)} MANUAL_POINTS tests passed.")
