r"""Locating the focus support points on the stitched overview.

The support points are logged in STAGE microns; the overview is a raster in
its own pixel space, and nothing records the transform between them. Every
mFoV supplies a known pair (stage position from the layout, bounding box
from the accumulator), so the mapping is FITTED from the section's own data
rather than assumed - qc_overview documents only the sign relationship, not
a scale, and the scale moves with overview_ds, which we now vary per section.

Checked against a real 397-mFoV section: the fit recovers 0.128 um per
overview pixel, exactly the 8 nm acquisition pixel times ds=16, with a
worst-case residual of 35 px in a 24355 px image, and all seven support
points land inside the imaged area.

    pytest standalone_qc/tests/test_fsp_overlay.py
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import pytest  # noqa: E402

from standalone_qc import fsp_overlay, fsp_timing  # noqa: E402

# One beam image is 2000 px at 8 nm, so 16 um; ds=16 makes it 125 overview px.
UM_PER_PX = 0.008 * 16


def _grid(n_x=4, n_y=3, pitch_um=160.0):
    """mFoVs on a regular grid, with the bboxes the accumulator would give.

    px = -x/UM_PER_PX is the documented sign relationship: image x runs
    opposite to stage x.
    """
    tiles, bboxes = [], {}
    idx = 0
    for iy in range(n_y):
        for ix in range(n_x):
            idx += 1
            x_um, y_um = ix * pitch_um, iy * pitch_um
            tiles.append(SimpleNamespace(tile_idx=idx, x_um=x_um, y_um=y_um))
            px = -x_um / UM_PER_PX + 100000.0
            py = y_um / UM_PER_PX + 50000.0
            half = (pitch_um / 2.0) / UM_PER_PX
            bboxes[idx] = [px - half, py - half, px + half, py + half]
    return tiles, bboxes


def test_fit_recovers_the_real_scale() -> None:
    tiles, bboxes = _grid()
    fit = fsp_overlay.fit_stage_to_overview(tiles, bboxes)
    assert fit is not None
    assert fit["um_per_px"] == pytest.approx(UM_PER_PX, rel=1e-6)
    assert fit["ax"] < 0, "image x must run opposite to stage x"
    assert fit["ay"] > 0
    assert fit["resid_x_px"] < 1e-6 and fit["resid_y_px"] < 1e-6
    assert fit["n"] == len(tiles)


def test_fit_refuses_rather_than_guesses() -> None:
    """Without spread on both axes the mapping is unsolvable. Better no
    markers than markers in invented places."""
    tiles, bboxes = _grid(n_x=4, n_y=1)      # no y spread
    assert fsp_overlay.fit_stage_to_overview(tiles, bboxes) is None
    # Too few points at all.
    tiles, bboxes = _grid(n_x=1, n_y=1)
    assert fsp_overlay.fit_stage_to_overview(tiles, bboxes) is None
    assert fsp_overlay.fit_stage_to_overview([], {}) is None


def test_markers_land_where_the_mfovs_are() -> None:
    """A support point at an mFoV's stage position must mark that mFoV."""
    tiles, bboxes = _grid()
    fit = fsp_overlay.fit_stage_to_overview(tiles, bboxes)
    W = H = 120000
    target = tiles[5]
    pts = [{"index": 0, "x": target.x_um, "y": target.y_um,
            "ol_meas": -170.866, "ol_pred": -166.638, "resid": -4.228}]
    frag, notes = fsp_overlay.markers_svg(pts, fit, W, H)
    assert notes == []

    import re
    m = re.search(r'<circle cx="([\d.]+)" cy="([\d.]+)"', frag)
    assert m
    cx, cy = float(m.group(1)), float(m.group(2))
    # The overview's rectangles are drawn with a 180 degree flip, so the
    # markers get the same flip or they land diagonally opposite.
    bb = bboxes[target.tile_idx]
    want_x = W - (bb[0] + bb[2]) / 2.0
    want_y = H - (bb[1] + bb[3]) / 2.0
    assert cx == pytest.approx(want_x, abs=1.0)
    assert cy == pytest.approx(want_y, abs=1.0)

    # The readings are on the tooltip, not just the position.
    assert "OL measured -170.866" in frag
    assert "residual -4.228" in frag
    assert "FSP 0" in frag


def test_a_point_outside_the_overview_is_reported_not_drawn() -> None:
    """Support points are often measured off the imaged area; a marker at a
    clamped or wrapped position would be a lie."""
    tiles, bboxes = _grid()
    fit = fsp_overlay.fit_stage_to_overview(tiles, bboxes)
    pts = [{"index": 3, "x": 1.0e6, "y": 1.0e6}]
    frag, notes = fsp_overlay.markers_svg(pts, fit, 1000, 1000)
    assert frag == ""
    assert notes and "outside the overview" in notes[0]
    assert "FSP 3" in notes[0]


def test_support_points_parse_with_their_readings(tmp_path: Path) -> None:
    log = tmp_path / "console.log"
    log.write_text(
        "[PlaneUpdate] Measuring 2 support points using method=AF_MINIMIZE_OL\n"
        "[NewPlanePt] sp[0] xy=(-15550.3,-19769.1) ol_meas=-170.866 "
        "ol_pred=-166.638 resid=-4.228\n"
        "[NewPlanePt] sp[1] xy=(-15633.3,-18489.7) ol_meas=-117.389 "
        "ol_pred=-175.054 resid=57.665\n"
        "[NewPlaneFit] a=-2.371654e-02, b=-8.116514e-03, c=-695.894\n",
        encoding="utf-8")
    d = fsp_timing.parse_support_points(log)
    assert d["method"] == "AF_MINIMIZE_OL"
    assert d["n_declared"] == 2
    assert d["plane"] == [-0.02371654, -0.008116514, -695.894]
    assert len(d["points"]) == 2
    p0 = d["points"][0]
    assert (p0["x"], p0["y"]) == (-15550.3, -19769.1)
    # These used to come back None: an optional group after a lazy ".*?" is
    # skipped by preference, so one combined pattern parsed the position and
    # dropped every number after it.
    assert p0["ol_meas"] == -170.866
    assert p0["ol_pred"] == -166.638
    assert p0["resid"] == -4.228


def test_a_retried_point_keeps_its_later_reading(tmp_path: Path) -> None:
    """focus_map logs a retried point twice; the plane was fitted to the
    later reading."""
    log = tmp_path / "console.log"
    log.write_text(
        "[NewPlanePt] sp[0] xy=(1.0,2.0) ol_meas=-1.0\n"
        "[NewPlanePt] sp[0] xy=(1.0,2.0) ol_meas=-9.0\n",
        encoding="utf-8")
    pts = fsp_timing.parse_support_points(log)["points"]
    assert len(pts) == 1 and pts[0]["ol_meas"] == -9.0


def test_single_point_strategies_report_no_support_points(tmp_path: Path) -> None:
    """The fiducial modes log nothing of this shape, and that is normal."""
    log = tmp_path / "console.log"
    log.write_text(
        "[PlaneUpdate] FIDUCIAL_FOCUS_POINT_XY_REGION_OFFSET: measuring one "
        "point for 'S3R2' at xy=(2563.2,3958.1)\n", encoding="utf-8")
    d = fsp_timing.parse_support_points(log)
    assert d["points"] == [] and d["method"] is None
    assert fsp_timing.parse_support_points(tmp_path / "nope.log")["points"] == []
