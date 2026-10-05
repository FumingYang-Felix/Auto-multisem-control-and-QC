"""Unit tests for the hardened wafer-fiducial CSV parser and the
WAFER_FIDUCIAL_DIRECT plane-update method's data path.

Hardware-free: focus_map's `from msem import ...` pulls in Windows-only
comtypes, so on a dev machine we stub it (only if the real one is absent)
before importing focus_map, same as tests/test_manual_focus_points.py.

Covers the actual incident that motivated this change: an 8-column header
sitting over 6-column data rows (w10h31_region_fiducials_2.txt) used to be
silently misread (projective x/y read as stig) by the old purely-positional
parser. _parse_wafer_fiducial_points is now header-aware and must reject
that file's rows outright instead.

Does NOT exercise the WAFER_FIDUCIAL_DIRECT branch inside
_run_one_region_body/run_one_region directly - that function also drives
stage moves, Z-fiducial correction, tile-route building and real tile
acquisition, none of which are meaningfully fakeable at unit-test scope.
Its behavior (skip all measurement, take a_new/b_new/c_new and
a_sx_new/b_sx_new/c_sx_new/a_sy_new/b_sy_new/c_sy_new straight from
_get_wafer_fiducial_planes(), inherit prev stig if the fiducial file has
none) was instead verified by a dedicated code read-through plus a
repo-wide grep confirming every branch-local variable it skips
(representative_m/measured_all/ms/skipped/focus_csv) is never referenced
past the dispatch. These tests cover everything upstream of that branch
that IS safely unit-testable: the fiducial file parsing + plane fitting it
reads its input from.

Run:  python tests/test_wafer_fiducial_direct.py      (exit 0 = pass)
Also discoverable by pytest.
"""
import os
import sys
import tempfile
import types

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


def test_legacy_headerless_4_column():
    path = _write("1000.0,2000.0,38800.0,5.0\n3000.0,1000.0,38800.0,7.0\n0.0,3000.0,38800.0,3.0\n")
    try:
        rows = fm._parse_wafer_fiducial_points(path)
        assert len(rows) == 3
        assert rows[0] == (1000.0, 2000.0, 38800.0, 5.0, None, None, None, None)
    finally:
        os.unlink(path)


def test_plain_8_column_header():
    path = _write(
        "x,y,z,ol,stigx,stigy,projective_x,projective_y\n"
        "1000.0,2000.0,38800.0,5.0,0.1,-0.1,-610.0,670.0\n"
    )
    try:
        rows = fm._parse_wafer_fiducial_points(path)
        assert rows == [(1000.0, 2000.0, 38800.0, 5.0, 0.1, -0.1, -610.0, 670.0)]
    finally:
        os.unlink(path)


def test_spelled_out_units_header():
    """The new human-friendly header convention adopted this session."""
    path = _write(
        "x(um),y(um),Z(um),OL(um),Stigmatorx(%),Stigmatory(%),Projectivex,ProjectiveY\n"
        "1000.0,2000.0,38800.0,5.0,0.1,-0.1,-610.0,670.0\n"
    )
    try:
        rows = fm._parse_wafer_fiducial_points(path)
        assert rows == [(1000.0, 2000.0, 38800.0, 5.0, 0.1, -0.1, -610.0, 670.0)]
    finally:
        os.unlink(path)


def test_stig_over_threshold_drops_the_whole_row():
    """Per the user: neither StigX nor StigY should ever legitimately exceed
    2% on this instrument - a row with a bigger one is a fault (microscope
    or sample), and the WHOLE point (x,y,z,ol,stigx,stigy,projx,projy) must
    be dropped, not just excluded from the stig fits. A real w10h31_dye_R3
    fiducial point (StigY=4.6%) dominated the OL plane's residuals before
    this filter existed."""
    path = _write(
        "x(um),y(um),Z(um),OL(um),Stigmatorx(%),Stigmatory(%),Projectivex,ProjectiveY\n"
        "1000.0,2000.0,38800.0,5.0,0.1,-0.1,-610.0,670.0\n"
        "3000.0,1000.0,38800.0,7.0,-0.1,4.6,-611.0,671.0\n"  # StigY=4.6% - must be dropped entirely
        "0.0,3000.0,38800.0,3.0,2.1,0.1,-612.0,672.0\n"      # StigX=2.1% - also over threshold
        "500.0,500.0,38800.0,6.0,0.2,0.2,-613.0,673.0\n"
    )
    try:
        rows = fm._parse_wafer_fiducial_points(path)
        assert rows == [
            (1000.0, 2000.0, 38800.0, 5.0, 0.1, -0.1, -610.0, 670.0),
            (500.0, 500.0, 38800.0, 6.0, 0.2, 0.2, -613.0, 673.0),
        ]
    finally:
        os.unlink(path)


def test_stig_exactly_at_threshold_is_kept():
    path = _write(
        "x(um),y(um),Z(um),OL(um),Stigmatorx(%),Stigmatory(%),Projectivex,ProjectiveY\n"
        "1000.0,2000.0,38800.0,5.0,2.0,-2.0,-610.0,670.0\n"  # exactly at the 2.0% threshold
    )
    try:
        rows = fm._parse_wafer_fiducial_points(path)
        assert rows == [(1000.0, 2000.0, 38800.0, 5.0, 2.0, -2.0, -610.0, 670.0)]
    finally:
        os.unlink(path)


def test_header_declares_more_columns_than_rows_have_is_rejected():
    """The actual incident: an 8-column header over 6-column rows must be
    rejected outright, not silently read with projective values as stig."""
    path = _write(
        "x(um),y(um),Z(um),OL(um),Stigmatorx(%),Stigmatory(%),Projectivex,ProjectiveY\n"
        "1000.0,2000.0,38800.0,5.0,-610.0,670.0\n"  # only 6 fields, header wants 8
    )
    try:
        rows = fm._parse_wafer_fiducial_points(path)
        assert rows == [], "6-field row under an 8-column header must be skipped, not misread"
    finally:
        os.unlink(path)


def test_six_column_header_no_stig():
    path = _write(
        "x,y,z,ol,projective_x,projective_y\n"
        "1000.0,2000.0,38800.0,5.0,-610.0,670.0\n"
    )
    try:
        rows = fm._parse_wafer_fiducial_points(path)
        assert rows == [(1000.0, 2000.0, 38800.0, 5.0, None, None, -610.0, 670.0)]
    finally:
        os.unlink(path)


def _ol_plane(x, y):
    return 0.5 * x + -0.25 * y + 10.0


def _stig_plane(x, y, k):
    return 0.001 * k * x + 0.0005 * k * y


def test_get_wafer_fiducial_planes_fits_ol_and_stig():
    pts = [(0.0, 0.0), (1000.0, 0.0), (0.0, 1000.0), (1000.0, 1000.0)]
    lines = ["x(um),y(um),Z(um),OL(um),Stigmatorx(%),Stigmatory(%)"]
    for x, y in pts:
        lines.append(f"{x},{y},38800.0,{_ol_plane(x, y)},{_stig_plane(x, y, 1)},{_stig_plane(x, y, -1)}")
    path = _write("\n".join(lines) + "\n")
    saved_path = fm.CONFIG.get("wafer_fiducial_ol_file")
    fm._wafer_fiducial_cache.clear()
    try:
        fm.CONFIG["wafer_fiducial_ol_file"] = path
        planes = fm._get_wafer_fiducial_planes()
        a, b, c = planes["ol"]
        assert abs(a - 0.5) < 1e-6 and abs(b - (-0.25)) < 1e-6 and abs(c - 10.0) < 1e-3
        assert planes["stigx"] is not None and planes["stigy"] is not None
        asx, bsx, csx = planes["stigx"]
        assert abs(asx - 0.001) < 1e-9 and abs(bsx - 0.0005) < 1e-9
    finally:
        fm.CONFIG["wafer_fiducial_ol_file"] = saved_path
        fm._wafer_fiducial_cache.clear()
        os.unlink(path)


def _proj_plane(x, y, k):
    return -600.0 + 0.002 * k * x + 0.001 * k * y


def test_get_wafer_fiducial_planes_fits_projective():
    """SAMPLE_TOPOGRAPHY_CORRECTION requires projx/projy planes on top of
    ol/stigx/stigy - added this session alongside that new plane_update_method."""
    pts = [(0.0, 0.0), (1000.0, 0.0), (0.0, 1000.0), (1000.0, 1000.0)]
    lines = ["x(um),y(um),Z(um),OL(um),Stigmatorx(%),Stigmatory(%),Projectivex,ProjectiveY"]
    for x, y in pts:
        lines.append(
            f"{x},{y},38800.0,{_ol_plane(x, y)},{_stig_plane(x, y, 1)},{_stig_plane(x, y, -1)},"
            f"{_proj_plane(x, y, 1)},{_proj_plane(x, y, -1)}")
    path = _write("\n".join(lines) + "\n")
    saved_path = fm.CONFIG.get("wafer_fiducial_ol_file")
    fm._wafer_fiducial_cache.clear()
    try:
        fm.CONFIG["wafer_fiducial_ol_file"] = path
        planes = fm._get_wafer_fiducial_planes()
        assert planes["projx"] is not None and planes["projy"] is not None
        apx, bpx, cpx = planes["projx"]
        apy, bpy, cpy = planes["projy"]
        assert abs(apx - 0.002) < 1e-9 and abs(bpx - 0.001) < 1e-9 and abs(cpx - (-600.0)) < 1e-6
        assert abs(apy - (-0.002)) < 1e-9 and abs(bpy - (-0.001)) < 1e-9 and abs(cpy - (-600.0)) < 1e-6
    finally:
        fm.CONFIG["wafer_fiducial_ol_file"] = saved_path
        fm._wafer_fiducial_cache.clear()
        os.unlink(path)


def test_get_wafer_fiducial_planes_no_projective_columns():
    """The pre-existing 6-column format (no stig/proj) must still leave
    projx/projy as None, not raise."""
    lines = ["x,y,z,ol"]
    for x, y in [(0.0, 0.0), (1000.0, 0.0), (0.0, 1000.0)]:
        lines.append(f"{x},{y},38800.0,{_ol_plane(x, y)}")
    path = _write("\n".join(lines) + "\n")
    saved_path = fm.CONFIG.get("wafer_fiducial_ol_file")
    fm._wafer_fiducial_cache.clear()
    try:
        fm.CONFIG["wafer_fiducial_ol_file"] = path
        planes = fm._get_wafer_fiducial_planes()
        assert planes["projx"] is None and planes["projy"] is None
    finally:
        fm.CONFIG["wafer_fiducial_ol_file"] = saved_path
        fm._wafer_fiducial_cache.clear()
        os.unlink(path)


if __name__ == "__main__":
    tests = [
        test_legacy_headerless_4_column,
        test_plain_8_column_header,
        test_spelled_out_units_header,
        test_stig_over_threshold_drops_the_whole_row,
        test_stig_exactly_at_threshold_is_kept,
        test_header_declares_more_columns_than_rows_have_is_rejected,
        test_six_column_header_no_stig,
        test_get_wafer_fiducial_planes_fits_ol_and_stig,
        test_get_wafer_fiducial_planes_fits_projective,
        test_get_wafer_fiducial_planes_no_projective_columns,
    ]
    for t in tests:
        t()
        print(f"OK: {t.__name__}")
    print(f"\nAll {len(tests)} wafer-fiducial tests passed.")
