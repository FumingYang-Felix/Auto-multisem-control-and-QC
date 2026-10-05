"""Unit tests for focus_diagnostics.py - the automatic end-of-run
expected-vs-measured OL/Z diagnostic generator.

Hardware-free: focus_diagnostics.py never touches the scope, so these build
small fake run directory trees (console.log + mfovN/metadata.txt +
LiveQC/live_qc_results.csv) under tempfile.mkdtemp() and check the parsing,
plane-fitting, and end-to-end HTML/JSON generation against them.

Run:  python tests/test_focus_diagnostics.py      (exit 0 = pass)
Also discoverable by pytest.
"""
import json
import os
import shutil
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import focus_diagnostics as fd  # noqa: E402


def _write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as f:
        f.write(text)


_METADATA_TEMPLATE = """Operator:\t\tAPI
Stage pos. X:\t\t{x}um
Stage pos. Y:\t\t{y}um
Stage pos. Z:\t\t{z}um
Stigmator Shift X:\t0.016
Stigmator Shift Y:\t-0.360
Stigmator Tilt X:\t0.147
Stigmator Tilt Y:\t0.428
Projective X:\t\t-610.575
Projective Y:\t\t684.382
Lens:\t\t\t{lens}
Focus Offset:\t\t-1.190um
"""

_CONSOLE_LOG_TEMPLATE = """
=== focus_map process @ 2026-09-01 22:10:38 (region {region}, idx 1) ===
[Region] START region_name={region} region_idx=1
[NewPlane] OL    = {a:.6e}*x + {b:.6e}*y + {c:.3f} (wafer fiducial)
[NewPlane] StigX = 0.000000e+00*x + 0.000000e+00*y + 0.100000
"""


def _make_region(run_dir, region, plane, tiles, quality_by_mfov=None):
    """tiles: list of (mfov_name, x, y, z, lens). quality_by_mfov: optional
    {mfov_name: quality} - if given, writes a LiveQC results CSV."""
    region_dir = os.path.join(run_dir, region)
    a, b, c = plane
    _write(os.path.join(region_dir, "console.log"),
           _CONSOLE_LOG_TEMPLATE.format(region=region, a=a, b=b, c=c))
    for mfov, x, y, z, lens in tiles:
        _write(os.path.join(region_dir, mfov, "metadata.txt"),
               _METADATA_TEMPLATE.format(x=x, y=y, z=z, lens=lens))
    if quality_by_mfov:
        rows = ["tile_idx,x_um,y_um,quality,tile_folder"]
        for i, (mfov, q) in enumerate(quality_by_mfov.items(), start=1):
            rows.append(f"{i},0,0,{q},{region_dir}\\{mfov}")
        _write(os.path.join(region_dir, "LiveQC", "live_qc_results.csv"), "\n".join(rows) + "\n")
    return region_dir


def test_to_float_strips_units():
    assert fd._to_float("38859.697um") == 38859.697
    assert fd._to_float("-141.4") == -141.4
    assert fd._to_float("not a number") is None


def test_read_metadata_parses_known_fields():
    tmp = tempfile.mkdtemp()
    try:
        path = os.path.join(tmp, "metadata.txt")
        _write(path, _METADATA_TEMPLATE.format(x=1.5, y=-2.5, z=38900.0, lens=2000000.0))
        meta = fd._read_metadata(path)
        assert meta["x"] == 1.5 and meta["y"] == -2.5 and meta["z"] == 38900.0
        assert meta["lens"] == 2000000.0
        assert meta["stigx"] == 0.147 and meta["stigy"] == 0.428
    finally:
        shutil.rmtree(tmp)


def test_read_metadata_missing_xyz_returns_none():
    tmp = tempfile.mkdtemp()
    try:
        path = os.path.join(tmp, "metadata.txt")
        _write(path, "Lens:\t\t\t123.0\n")
        assert fd._read_metadata(path) is None
    finally:
        shutil.rmtree(tmp)


def test_read_metadata_missing_file_returns_none():
    assert fd._read_metadata(os.path.join(tempfile.gettempdir(), "does_not_exist_xyz.txt")) is None


def test_parse_region_planes_extracts_ol_coefficients():
    tmp = tempfile.mkdtemp()
    try:
        path = os.path.join(tmp, "console.log")
        _write(path, _CONSOLE_LOG_TEMPLATE.format(region="S1R1", a=-8.6e-4, b=1.5e-4, c=-162.71))
        planes = fd._parse_region_planes(path)
        assert planes["ol"] is not None
        a, b, c = planes["ol"]
        assert abs(a - -8.6e-4) < 1e-9
        assert abs(b - 1.5e-4) < 1e-9
        assert abs(c - -162.71) < 1e-6
    finally:
        shutil.rmtree(tmp)


def test_parse_region_planes_no_newplane_line_returns_none_for_ol():
    tmp = tempfile.mkdtemp()
    try:
        path = os.path.join(tmp, "console.log")
        _write(path, "[Region] START region_name=S1R1\n")
        planes = fd._parse_region_planes(path)
        assert planes["ol"] is None
    finally:
        shutil.rmtree(tmp)


def test_parse_region_planes_extracts_all_five_quantities():
    tmp = tempfile.mkdtemp()
    try:
        path = os.path.join(tmp, "console.log")
        _write(path,
               "[NewPlane] OL    = -8.600000e-04*x + 1.500000e-04*y + -162.710\n"
               "[NewPlane] StigX = 0.000000e+00*x + 0.000000e+00*y + 0.100000\n"
               "[NewPlane] StigY = 0.000000e+00*x + 0.000000e+00*y + 0.300000\n"
               "[NewPlane] ProjX = -9.700000e-05*x + 6.200000e-05*y + -611.000 (wafer fiducial, recorded only)\n"
               "[NewPlane] ProjY = -7.300000e-05*x + 4.700000e-05*y + 684.000 (wafer fiducial, recorded only)\n")
        planes = fd._parse_region_planes(path)
        assert planes["ol"] is not None
        assert planes["stigx"] == (0.0, 0.0, 0.1)
        assert planes["stigy"] == (0.0, 0.0, 0.3)
        assert planes["projx"] is not None and abs(planes["projx"][2] - -611.0) < 1e-6
        assert planes["projy"] is not None and abs(planes["projy"][2] - 684.0) < 1e-6
    finally:
        shutil.rmtree(tmp)


def test_parse_region_planes_missing_optional_quantities_are_none():
    tmp = tempfile.mkdtemp()
    try:
        path = os.path.join(tmp, "console.log")
        _write(path, "[NewPlane] OL    = -8.600000e-04*x + 1.500000e-04*y + -162.710\n")
        planes = fd._parse_region_planes(path)
        assert planes["ol"] is not None
        assert planes["stigx"] is None and planes["stigy"] is None
        assert planes["projx"] is None and planes["projy"] is None
    finally:
        shutil.rmtree(tmp)


def test_fit_plane_recovers_known_coefficients():
    a, b, c = 0.001, -0.002, 5.0
    pts = [(x, y, a * x + b * y + c) for x, y in
           [(0, 0), (10, 0), (0, 10), (10, 10), (5, 5), (-10, 3)]]
    fit = fd._fit_plane(pts)
    assert fit is not None
    fa, fb, fc = fit
    assert abs(fa - a) < 1e-9 and abs(fb - b) < 1e-9 and abs(fc - c) < 1e-6


def test_fit_plane_too_few_points_returns_none():
    assert fd._fit_plane([(0, 0, 0), (1, 1, 1)]) is None


def test_parse_fiducial_z_plane_flat_file():
    tmp = tempfile.mkdtemp()
    try:
        path = os.path.join(tmp, "fiducial.txt")
        rows = ["x(um),y(um),Z(um),OL(um)"]
        for x, y in [(-100, -100), (100, -100), (0, 200), (50, 50)]:
            rows.append(f"{x},{y},38859.7,0.0")
        _write(path, "\n".join(rows) + "\n")
        plane = fd._parse_fiducial_z_plane(path)
        assert plane is not None
        a, b, c = plane
        assert abs(a) < 1e-6 and abs(b) < 1e-6 and abs(c - 38859.7) < 1e-3
    finally:
        shutil.rmtree(tmp)


def test_parse_fiducial_z_plane_missing_file_returns_none():
    assert fd._parse_fiducial_z_plane(None) is None
    assert fd._parse_fiducial_z_plane(os.path.join(tempfile.gettempdir(), "nope.txt")) is None


def test_load_live_qc_quality_matches_by_folder_basename():
    tmp = tempfile.mkdtemp()
    try:
        region_dir = os.path.join(tmp, "S1R1")
        rows = [
            "tile_idx,x_um,y_um,quality,tile_folder",
            f"1,0,0,71.65,{region_dir}\\mfov1",
            f"2,0,0,-100.0,{region_dir}/mfov2",
        ]
        _write(os.path.join(region_dir, "LiveQC", "live_qc_results.csv"), "\n".join(rows) + "\n")
        q = fd._load_live_qc_quality(region_dir)
        assert q == {"mfov1": 71.65, "mfov2": -100.0}
    finally:
        shutil.rmtree(tmp)


def test_load_live_qc_quality_missing_csv_returns_empty():
    tmp = tempfile.mkdtemp()
    try:
        assert fd._load_live_qc_quality(tmp) == {}
    finally:
        shutil.rmtree(tmp)


def test_collect_run_tiles_end_to_end():
    tmp = tempfile.mkdtemp()
    try:
        plane = (-8.6e-4, 1.5e-4, -162.71)

        def ol_at(x, y):
            a, b, c = plane
            return a * x + b * y + c

        _make_region(
            tmp, "S1R1", plane,
            tiles=[("mfov1", 100.0, 200.0, 38859.7, 2000000.0 + ol_at(100.0, 200.0)),
                   ("mfov2", 150.0, 250.0, 38859.7, 2000000.0 + ol_at(150.0, 250.0))],
            quality_by_mfov={"mfov1": 71.65, "mfov2": -100.0},
        )
        tiles = fd.collect_run_tiles(tmp)
        assert len(tiles) == 2
        by_mfov = {t["mfov"]: t for t in tiles}
        assert by_mfov["mfov1"]["region"] == "S1R1"
        assert by_mfov["mfov1"]["quality"] == 71.65
        assert by_mfov["mfov2"]["quality"] == -100.0
        assert abs(by_mfov["mfov1"]["ol_pred"] - ol_at(100.0, 200.0)) < 1e-6
    finally:
        shutil.rmtree(tmp)


def test_collect_run_tiles_carries_stig_pred_and_meas_when_planed():
    tmp = tempfile.mkdtemp()
    try:
        plane = (-8.6e-4, 1.5e-4, -162.71)
        # _CONSOLE_LOG_TEMPLATE logs OL and a flat StigX plane (0.1) but no
        # StigY/ProjX/ProjY - exercises the "some quantities present, others
        # not" case a real WAFER_FIDUCIAL_DIRECT-style region hits.
        _make_region(tmp, "S1R1", plane, tiles=[("mfov1", 100.0, 200.0, 38859.7, 2000000.0)])
        tiles = fd.collect_run_tiles(tmp)
        assert len(tiles) == 1
        t = tiles[0]
        assert t["stigx_pred"] == 0.1  # flat plane, any x/y
        assert t["stigx_meas"] == 0.147  # from _METADATA_TEMPLATE's fixed "Stigmator Tilt X"
        assert t["stigy_pred"] is None  # no [NewPlane] StigY line in the template
        assert t["projx_pred"] is None and t["projy_pred"] is None
    finally:
        shutil.rmtree(tmp)


def test_collect_run_tiles_skips_region_without_newplane_line():
    tmp = tempfile.mkdtemp()
    try:
        region_dir = os.path.join(tmp, "S1R1")
        _write(os.path.join(region_dir, "console.log"), "[Region] START region_name=S1R1\n")
        _write(os.path.join(region_dir, "mfov1", "metadata.txt"),
               _METADATA_TEMPLATE.format(x=1.0, y=2.0, z=38900.0, lens=100.0))
        assert fd.collect_run_tiles(tmp) == []
    finally:
        shutil.rmtree(tmp)


def test_collect_run_tiles_ignores_non_region_entries():
    tmp = tempfile.mkdtemp()
    try:
        _write(os.path.join(tmp, "timing_summary.txt"), "not a region\n")
        os.makedirs(os.path.join(tmp, "empty_dir"), exist_ok=True)
        assert fd.collect_run_tiles(tmp) == []
    finally:
        shutil.rmtree(tmp)


def test_calibration_offset_recovers_known_constant():
    tiles = [{"ol_pred": 1.0, "lens": 1001.0}, {"ol_pred": 2.0, "lens": 1002.0},
              {"ol_pred": 3.0, "lens": 1003.0}]
    assert abs(fd._calibration_offset(tiles) - 1000.0) < 1e-9


def test_calibration_offset_no_lens_data_returns_none():
    assert fd._calibration_offset([{"ol_pred": 1.0, "lens": None}]) is None


def test_generate_focus_diagnostics_end_to_end_with_z_panel():
    tmp = tempfile.mkdtemp()
    out = tempfile.mkdtemp()
    try:
        plane = (-8.6e-4, 1.5e-4, -162.71)

        def ol_at(x, y):
            a, b, c = plane
            return a * x + b * y + c

        run_dir = os.path.join(tmp, "run_20260101_000000")
        _make_region(run_dir, "S1R1", plane,
                     tiles=[("mfov1", 100.0, 200.0, 38859.7, 2000000.0 + ol_at(100.0, 200.0)),
                            ("mfov2", 150.0, 250.0, 38859.7, 2000000.0 + ol_at(150.0, 250.0)),
                            ("mfov3", 120.0, 220.0, 38859.7, 2000000.0 + ol_at(120.0, 220.0))],
                     quality_by_mfov={"mfov1": 71.65, "mfov2": -100.0, "mfov3": 60.0})

        fiducial_path = os.path.join(tmp, "fiducial.txt")
        rows = ["x(um),y(um),Z(um),OL(um)"]
        for x, y in [(-100, -100), (100, -100), (0, 200), (50, 50)]:
            rows.append(f"{x},{y},38859.7,0.0")
        _write(fiducial_path, "\n".join(rows) + "\n")

        html_path = fd.generate_focus_diagnostics(
            run_dir, wafer_fiducial_file=fiducial_path, diagnostics_dir=out,
        )
        assert html_path is not None
        assert os.path.isfile(html_path)

        json_path = os.path.join(out, "run_20260101_000000", "focus_diagnostics.json")
        assert os.path.isfile(json_path)
        with open(json_path) as f:
            data = json.load(f)
        assert len(data["tiles"]) == 3
        assert data["z_plane"] is not None

        with open(html_path, encoding="utf-8") as f:
            html = f.read()
        assert "Focus diagnostics" in html
        assert "scatter3d" in html
        # _CONSOLE_LOG_TEMPLATE (via _make_region) logs a flat StigX plane
        # but no StigY/ProjX/ProjY - the StigX identity panel should appear,
        # the others shouldn't (partial per-quantity availability).
        assert "Expected vs measured StigX" in html
        assert "Expected vs measured StigY" not in html
        assert "Expected vs measured ProjX" not in html
        assert "Expected vs measured ProjY" not in html
    finally:
        shutil.rmtree(tmp)
        shutil.rmtree(out)


def test_generate_focus_diagnostics_all_quantities_available_includes_all_panels():
    tmp = tempfile.mkdtemp()
    out = tempfile.mkdtemp()
    try:
        run_dir = os.path.join(tmp, "run_full_planes")
        region_dir = os.path.join(run_dir, "S1R1")
        _write(os.path.join(region_dir, "console.log"),
               "[NewPlane] OL    = 0.000000e+00*x + 0.000000e+00*y + -10.000\n"
               "[NewPlane] StigX = 0.000000e+00*x + 0.000000e+00*y + 0.100000\n"
               "[NewPlane] StigY = 0.000000e+00*x + 0.000000e+00*y + 0.300000\n"
               "[NewPlane] ProjX = 0.000000e+00*x + 0.000000e+00*y + -611.000 (wafer fiducial, recorded only)\n"
               "[NewPlane] ProjY = 0.000000e+00*x + 0.000000e+00*y + 684.000 (wafer fiducial, recorded only)\n")
        for mfov, x, y in [("mfov1", 1.0, 1.0), ("mfov2", 2.0, 2.0), ("mfov3", 3.0, 3.0)]:
            _write(os.path.join(region_dir, mfov, "metadata.txt"),
                   _METADATA_TEMPLATE.format(x=x, y=y, z=38900.0, lens=2000000.0 - 10.0))
        rows = ["1,0,0,50.0," + region_dir + "\\mfov1",
                "2,0,0,55.0," + region_dir + "\\mfov2",
                "3,0,0,60.0," + region_dir + "\\mfov3"]
        _write(os.path.join(region_dir, "LiveQC", "live_qc_results.csv"),
               "tile_idx,x_um,y_um,quality,tile_folder\n" + "\n".join(rows) + "\n")

        html_path = fd.generate_focus_diagnostics(run_dir, wafer_fiducial_file=None, diagnostics_dir=out)
        assert html_path is not None
        with open(html_path, encoding="utf-8") as f:
            html = f.read()
        for label in ("StigX", "StigY", "ProjX", "ProjY"):
            assert f"Expected vs measured {label}" in html
    finally:
        shutil.rmtree(tmp)
        shutil.rmtree(out)


def test_generate_focus_diagnostics_no_wafer_fiducial_omits_z_panel():
    tmp = tempfile.mkdtemp()
    out = tempfile.mkdtemp()
    try:
        plane = (0.0, 0.0, -10.0)
        run_dir = os.path.join(tmp, "run_no_fiducial")
        _make_region(run_dir, "S1R1", plane,
                     tiles=[("mfov1", 1.0, 1.0, 38900.0, 2000000.0 - 10.0),
                            ("mfov2", 2.0, 2.0, 38900.0, 2000000.0 - 10.0),
                            ("mfov3", 3.0, 3.0, 38900.0, 2000000.0 - 10.0)],
                     quality_by_mfov={"mfov1": 50.0, "mfov2": 55.0, "mfov3": 60.0})

        html_path = fd.generate_focus_diagnostics(
            run_dir, wafer_fiducial_file=None, diagnostics_dir=out,
        )
        assert html_path is not None
        with open(html_path, encoding="utf-8") as f:
            html = f.read()
        assert "expected Z" not in html
    finally:
        shutil.rmtree(tmp)
        shutil.rmtree(out)


def test_generate_focus_diagnostics_no_tiles_returns_none():
    tmp = tempfile.mkdtemp()
    out = tempfile.mkdtemp()
    try:
        empty_run = os.path.join(tmp, "empty_run")
        os.makedirs(empty_run, exist_ok=True)
        assert fd.generate_focus_diagnostics(empty_run, diagnostics_dir=out) is None
        assert os.listdir(out) == []
    finally:
        shutil.rmtree(tmp)
        shutil.rmtree(out)


def test_generate_focus_diagnostics_missing_run_dir_does_not_raise():
    out = tempfile.mkdtemp()
    try:
        result = fd.generate_focus_diagnostics(
            os.path.join(tempfile.gettempdir(), "definitely_does_not_exist_run_dir"),
            diagnostics_dir=out,
        )
        assert result is None
    finally:
        shutil.rmtree(out)


def _precheck_point(x, y, ol_pred, ol_meas, stig_pred=0.2, stig_meas=0.2,
                     proj_pred=-610.0, proj_meas=-610.0):
    return {
        "x": x, "y": y,
        "ol_pred": ol_pred, "ol_meas": ol_meas,
        "stigx_pred": stig_pred, "stigx_meas": stig_meas,
        "stigy_pred": stig_pred, "stigy_meas": stig_meas,
        "projx_pred": proj_pred, "projx_meas": proj_meas,
        "projy_pred": proj_pred, "projy_meas": proj_meas,
    }


def test_generate_precheck_diagnostics_writes_html_and_json():
    out = tempfile.mkdtemp()
    try:
        points = [_precheck_point(x, x * 2, -10 + 0.001 * x, -10 + 0.001 * x + 0.5)
                  for x in range(-5000, 5001, 1000)]
        html_path = fd.generate_precheck_diagnostics(points, "run_precheck_1", diagnostics_dir=out)
        assert html_path is not None
        assert os.path.isfile(html_path)

        json_path = os.path.join(out, "run_precheck_1", "precheck_diagnostics.json")
        assert os.path.isfile(json_path)
        with open(json_path) as f:
            data = json.load(f)
        assert len(data["points"]) == len(points)

        with open(html_path, encoding="utf-8") as f:
            html = f.read()
        for label in ("OL", "StigX", "StigY", "ProjX", "ProjY"):
            assert f"fresh {label}" in html
    finally:
        shutil.rmtree(out)


def test_generate_precheck_diagnostics_no_points_returns_none():
    out = tempfile.mkdtemp()
    try:
        assert fd.generate_precheck_diagnostics([], "empty_run", diagnostics_dir=out) is None
        assert os.listdir(out) == []
    finally:
        shutil.rmtree(out)


def test_build_precheck_html_omits_panel_missing_data():
    points = [
        {"x": 0.0, "y": 0.0, "ol_pred": -10.0, "ol_meas": -9.5,
         "stigx_pred": None, "stigx_meas": None,
         "stigy_pred": 0.3, "stigy_meas": 0.31,
         "projx_pred": -610.0, "projx_meas": -610.2,
         "projy_pred": 682.0, "projy_meas": 682.1},
        {"x": 100.0, "y": 50.0, "ol_pred": -9.9, "ol_meas": -9.4,
         "stigx_pred": None, "stigx_meas": None,
         "stigy_pred": 0.31, "stigy_meas": 0.30,
         "projx_pred": -609.9, "projx_meas": -610.1,
         "projy_pred": 682.1, "projy_meas": 682.0},
    ]
    html = fd._build_precheck_html(points, "partial_run")
    assert "fresh OL" in html
    assert "fresh StigY" in html
    assert "fresh ProjX" in html
    assert "fresh StigX" not in html  # stigx_pred/meas are None for every point


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
