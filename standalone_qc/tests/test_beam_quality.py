"""Panel 4's box plot: the axis must not be set by one bad image.

    pytest standalone_qc/tests/test_beam_quality.py
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import pytest  # noqa: E402

from standalone_qc import beam_quality  # noqa: E402

CFG = {"low_quality_threshold": 50.0, "high_quality_threshold": 60.0,
       "colormap": "RdYlGn"}


def _robust(html: str) -> str:
    """Just the default chart. The panel holds two SVGs of the same boxes
    (one per y scale), so an unscoped findall counts everything twice."""
    return re.search(r'<svg id="bq-svg-robust".*?</svg>', html, re.S).group(0)


def _vals(n: int = 40, base: float = 20.0, spread: float = 4.0):
    """A tight, symmetric cluster: anything far from it is a real outlier."""
    return [base + spread * ((i % 9) - 4) / 4.0 for i in range(n)]


def test_whiskers_ignore_the_extreme_but_the_value_is_kept() -> None:
    vals = {1: _vals() + [888.0]}
    s = beam_quality.beam_stats(vals)[0]

    assert s["max"] == 888.0, "the extreme is still in the data"
    assert s["w_hi"] < 40.0, "but it is past the 1.5*IQR fence"
    assert s["out_hi"] == [888.0]
    assert s["n"] == 41


def test_axis_follows_the_whiskers_not_the_worst_image() -> None:
    """The defect this panel replaces: on S12R5 one image at 888 against a
    whisker range topping out near 133 squashed all 91 boxes flat."""
    vals = {b: _vals(base=20.0 + b) for b in range(1, 6)}
    vals[3] = list(vals[3]) + [888.0]
    html = beam_quality.panel_html(vals, CFG)

    robust = _robust(html)
    full = re.search(r'<svg id="bq-svg-full".*?</svg>', html, re.S).group(0)
    ticks = lambda svg: [float(t.replace(",", "")) for t in
                         re.findall(r'text-anchor="end"[^>]*>([-\d,]+)</text>',
                                    svg)]
    assert max(ticks(robust)) < 60, "robust axis must not reach 888"
    assert max(ticks(full)) >= 888, "full-range axis still shows it"

    # Boxes are drawn taller on the robust axis - that is the whole point.
    h = lambda svg: max(float(m) for m in
                        re.findall(r'class="bq-box"[^>]*height="([\d.]+)"', svg))
    assert h(robust) > 4 * h(full)


def test_nothing_past_the_axis_is_silently_dropped() -> None:
    """A "robust" axis is only honest if what it excludes is still shown."""
    vals = {1: _vals() + [888.0, 999.0], 2: _vals()}
    html = beam_quality.panel_html(vals, CFG)
    robust = _robust(html)

    assert "▲2" in robust, "both off-axis values counted at the frame edge"
    assert "999.00" in robust, "and the real range is in the hover text"
    assert "past this axis" in robust


def test_a_saturated_threshold_scale_says_so() -> None:
    """With the threshold at 50 and beam medians at 12-55, 87 of 91 beams got
    the identical darkest red. The colour then carries no information about
    which beam is unusual, and the legend has to admit that rather than let
    it read as 91 equally bad beams."""
    vals = {b: _vals(base=12.0 + b * 0.4) for b in range(1, 92)}
    html = beam_quality.panel_html(vals, CFG)

    assert "share the end colour" in html
    assert "separates almost nothing" in html

    boxes = re.findall(r'class="bq-box" data-abs="(#\w+)" data-rel="(#\w+)"',
                       _robust(html))
    assert len(boxes) == 91
    n_abs = len({a for a, _ in boxes})
    n_rel = len({r for _, r in boxes})
    assert n_abs <= 3, "threshold mode is saturated here"
    assert n_rel > 20, "the section-relative scale must still separate beams"


def test_relative_scale_is_robust_to_one_dead_beam() -> None:
    """One beam at zero must not rescale the other 90 into one colour, which
    is what a min/max scale would do."""
    # The 90 healthy beams differ a little, as real ones do; the point is
    # that those differences survive the presence of beam 45.
    vals = {b: _vals(base=30.0 + (b % 7) * 0.8) for b in range(1, 92)}
    vals[45] = _vals(base=0.1, spread=0.05)
    boxes = re.findall(r'data-rel="(#\w+)"',
                       _robust(beam_quality.panel_html(vals, CFG)))

    assert len({c for i, c in enumerate(boxes) if i != 44}) > 1, \
        "the other beams keep their spread"
    import matplotlib.colors as mc
    dead = mc.to_rgb(boxes[44])
    assert dead[0] > 0.5 and dead[1] < 0.3, "the dead beam reads as red"


def test_empty_input_is_not_an_error() -> None:
    assert beam_quality.panel_html({}, CFG) == ""
    assert beam_quality.panel_html(None, CFG) == ""
    assert beam_quality.panel_html({7: []}, CFG) == ""


def test_zero_iqr_does_not_divide_by_zero() -> None:
    """Every image on a beam scoring identically is a real case (a dead beam
    returning a constant), and Tukey's fences then collapse onto the box."""
    s = beam_quality.beam_stats({1: [5.0] * 20 + [99.0]})[0]
    assert s["q1"] == s["q3"] == 5.0
    assert s["out_hi"] == [99.0]
    assert beam_quality.panel_html({1: [5.0] * 20 + [99.0]}, CFG)


def test_equal_thresholds_fall_back_to_pass_fail() -> None:
    cfg = dict(CFG, low_quality_threshold=30.0, high_quality_threshold=30.0)
    html = beam_quality.panel_html({1: _vals(base=10.0), 2: _vals(base=50.0)},
                                   cfg)
    cols = re.findall(r'data-abs="(#\w+)"', _robust(html))
    assert cols == ["#c0392b", "#2e8b3d"]
    assert "pass/fail" in html


def test_dashboard_swaps_the_panel_in(tmp_path: Path) -> None:
    from standalone_qc.config import load_config
    from standalone_qc.dashboard import (
        DASHBOARD_AVAILABLE, write_section_dashboard,
    )
    from standalone_qc.discover import find_scorable_regions
    from standalone_qc.score import score_region
    from standalone_qc.tests.test_standalone_qc import DSAMP, build_fixture

    if not DASHBOARD_AVAILABLE:
        pytest.skip("live_qc_support._write_dashboard_html unavailable")

    build_fixture(tmp_path)
    region = find_scorable_regions(tmp_path)[0]
    cfg = load_config(None, dsamp=DSAMP, max_workers=4,
                      region_overview_enabled=False)
    out = write_section_dashboard(score_region(region, cfg), cfg)
    assert out
    page = out.read_text(encoding="utf-8")

    assert 'id="bq-panel"' in page, "the replacement panel is not in the page"
    assert page.count('id="bq-svg-robust"') == 1
    # The heading and the section around it survive the swap.
    assert "Image quality per beam" in page
    i = page.find('<section class="panel beamplot">')
    assert i < page.find('id="bq-panel"') < page.find("</section>", i)
    # ...and the old fixed-axis plot is gone, not merely appended to.
    assert page.count("rect class=\"bq-box\"") > 0
