"""Tests for the batch-level and run-level rollup reports.

These mirror the acquisition side's own two reports, so the assertions here
are mostly about STRUCTURE - the columns, the attempt grouping, the review
controls, and links that actually resolve - rather than exact wording.

    pytest standalone_qc/tests/test_rollup.py
    python standalone_qc/tests/test_rollup.py
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from urllib.parse import unquote

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import pytest  # noqa: E402

from standalone_qc import review, rollup, section_facts  # noqa: E402
from standalone_qc.config import load_config, policy_fingerprint  # noqa: E402
from standalone_qc.discover import find_scorable_regions  # noqa: E402
from standalone_qc.report import write_region_report  # noqa: E402
from standalone_qc.score import score_region  # noqa: E402
from standalone_qc.tests.test_standalone_qc import (  # noqa: E402
    BATCH,
    DSAMP,
    build_fixture,
)


def _cfg(**kw):
    base = {"dsamp": DSAMP, "max_workers": 4}
    base.update(kw)
    return load_config(None, **base)


def _score_one(tmp_path: Path):
    """Fixture tree with S1R1 actually scored, so the rollups have real data."""
    build_fixture(tmp_path)
    cfg = _cfg()
    region = find_scorable_regions(tmp_path)[0]
    score = score_region(region, cfg)
    write_region_report(score, cfg, policy_fingerprint(cfg))
    return cfg, score


def _broken_links(page: Path, base: Path) -> list:
    html = page.read_text(encoding="utf-8")
    hrefs = re.findall(r'href="([^"]+)"', html)
    rel = [h for h in hrefs if not h.startswith(("http", "file:", "#"))]
    return [h for h in rel if not (base / unquote(h)).exists()]


# ---------------------------------------------------------------------------
def test_batch_report_has_a_row_per_section(tmp_path: Path) -> None:
    cfg, _ = _score_one(tmp_path)
    out = rollup.write_batch_report(tmp_path, tmp_path / BATCH, cfg)
    assert out is not None and out.name == rollup.BATCH_REPORT_NAME
    html = out.read_text(encoding="utf-8")

    # Every section in the manifest appears, not just the scored one - a
    # pending section being absent would hide work still to do.
    for name in ("S1R1", "S2R1", "S3R1"):
        assert f"<strong>{name}</strong>" in html

    # The Zen Report column set this replaces.
    for col in ("Section", "Imaging Status", "mFoV #", "FSP #", "Duration",
                "full-res imgs", "thumb imgs", "full-res coord", "thum coord",
                "metadata", "focus plane", "jitter", "scanfault",
                "mFoV overlap", "IMQA", "worst beams"):
        assert col in html, f"missing Zen column {col!r}"
    # "focus map" checked for a FocusMap.txt this pipeline never writes, so
    # it failed on every section - replaced by "focus plane".
    assert ">focus map<" not in html
    # Still not implemented, shown as explicit TBD so the gap stays visible.
    assert "ROI align" in html
    assert html.count(">TBD<") >= 1
    # Skew was removed: no honest pass/fail could be built from one image.
    assert ">skew</th>" not in html

    # Dropped on purpose: "sFoV stitch" is not wanted, and the old "QC"
    # column (Scored / Awaiting QC) only restated what an empty IMQA cell
    # already says.
    assert "sFoV stitch" not in html
    assert ">QC</th>" not in html
    assert "Awaiting QC" not in html
    assert "1 of 3 section(s) have image quality" in html
    # Acquisition status chips for the two non-scored sections.
    assert "Failed" in html and "Pending" in html

    # A page left open during a run must keep itself current.
    assert "btn-auto-refresh" in html and "location.reload()" in html


def test_batch_report_reflects_real_scores(tmp_path: Path) -> None:
    cfg, score = _score_one(tmp_path)
    out = rollup.write_batch_report(tmp_path, tmp_path / BATCH, cfg)
    html = out.read_text(encoding="utf-8")

    summary = json.loads(
        (score.region_dir / "StandaloneQC" / "standalone_qc_summary.json")
        .read_text(encoding="utf-8")
    )
    # The planted bad beam must surface in the batch overview, not only in the
    # per-section report.
    assert summary["outlier_beams"], "fixture should have an outlier beam"
    beam = summary["outlier_beams"][0]["beam_index"]
    assert str(beam) in html
    assert f'{summary["n_tiles"]}' in html


def test_focus_plane_column_shows_the_measured_slope(tmp_path: Path) -> None:
    """The plane fit has to reach the batch report, not only the dashboard.

    The numbers are cached in summary.json by score_region so the report can
    show a per-section slope without re-reading every metadata.txt on each
    rebuild - the cells read the cache, so a missing cache shows as a tick
    with no slope rather than a stale one.
    """
    cfg, score = _score_one(tmp_path)
    assert score.plane_fits, "scoring should fit the OL/stig planes"
    ol = next(f for f in score.plane_fits if f["quantity"] == "ol")

    summary = json.loads(
        (score.region_dir / "StandaloneQC" / "standalone_qc_summary.json")
        .read_text(encoding="utf-8")
    )
    assert summary["plane_fits"], "plane fits must be persisted for the rollup"

    cell = rollup._focus_plane_cell(score.facts, summary)
    if (ol.get("gradient") or {}).get("grad_um_per_mm") is not None:
        # The unit adapts: real sections fit planes near 1e-4 um/mm, and
        # "%.2f um/mm" printed 0.00 for every one of them.
        assert ("&micro;m/mm" in cell) or ("nm/mm" in cell)
        assert "um/mm" in cell          # the hover text, spelled out
        assert "of focus across the whole section" in cell
    # Without the cache the column degrades to the plain tick, never to a
    # wrong number.
    assert "m/mm" not in rollup._focus_plane_cell(score.facts, {})


def test_plane_panel_explains_a_quantity_it_cannot_fit(tmp_path: Path) -> None:
    """A missing StigX plot must be distinguishable from a broken one.

    In batches with no LiveQC CSV the stig readback is constant to the three
    decimals metadata.txt records, so there is genuinely nothing to fit. The
    panel says which quantity and why instead of omitting it silently.
    """
    from standalone_qc import dashboard

    cfg, score = _score_one(tmp_path)
    page = dashboard._inject_panels("<html><body><p>x</p></body></html>",
                                    score, cfg)
    degenerate = [f for f in score.plane_fits
                  if f["degenerate"] or not f["plane"] or not f["n"]]
    if degenerate:
        assert "Focus &amp; stigmation planes" in page
        assert degenerate[0]["label"] in page


def _script_depth_at(page: str, needle: str) -> int:
    """How many <script> elements enclose `needle`. 0 = real page content."""
    head = page[:page.index(needle)].lower()
    return head.count("<script") - head.count("</script>")


def test_stig_panel_is_renamed_and_explains_itself(tmp_path: Path) -> None:
    """"Stig anisotropy" reads like a stigmator value, and an operator read
    -24.42 expecting the +/-0.3 range StigX/StigY live in. It is a measured
    image property in percentage points. The heading must say so, and say
    what it is NOT."""
    from standalone_qc import dashboard

    cfg, score = _score_one(tmp_path)
    page = dashboard._relabel(
        "<html><body><h3>② Stig anisotropy + arrows</h3></body></html>", score
    )
    assert "Stig Sharpness Anisotropy" in page
    assert "horz − vert, %" in page
    assert "② Stig anisotropy + arrows" not in page, "old heading still present"

    # The hover text has to carry the two things that caused the confusion.
    assert 'title="' in page
    assert "NOT the stigmator setting" in page
    assert "StigX / StigY" in page
    assert "percentage POINTS" in page
    # A title attribute cannot contain a raw quote or newline.
    tip = page.split('title="', 1)[1].split('"', 1)[0]
    assert chr(10) not in tip and "&#10;" in tip

    # An upstream wording change must be reported, not silently skipped.
    score.errors.clear()
    dashboard._relabel("<html><body>nothing to match</body></html>", score)
    assert any("relabel" in e for e in score.errors)


def test_panels_are_injected_into_the_page_not_into_a_script(
    tmp_path: Path,
) -> None:
    """The panels must land in the document, not inside a JS string.

    The real dashboard's inverted-preview viewer writes a second HTML
    document from JavaScript, so "</body>" appears twice and the first one is
    inside a <script>. Injecting there rendered nothing AND broke the page's
    click handler, so clicking an mFoV opened a directory index instead of
    the images.

    Asserting the panel text is merely PRESENT does not catch this - it was
    present, just inside a string literal. This checks where it landed.
    """
    from standalone_qc import dashboard

    cfg, score = _score_one(tmp_path)
    upstream = (
        "<html><body><p>real page</p>\n"
        "<script>\n"
        "  w.document.write('<html><body><img src=x></body></html>');\n"
        "</script>\n"
        "</body></html>"
    )
    page = dashboard._inject_panels(upstream, score, cfg)
    marker = "class='grid'"
    assert marker in page, "panels should have been injected"
    assert _script_depth_at(page, marker) == 0, (
        "panels were injected inside a <script> element - they will not "
        "render, and their quotes break the surrounding JS string"
    )
    # The script the viewer depends on must come through untouched.
    assert "w.document.write('<html><body><img src=x></body></html>');" in page
    # And the panels belong before the document's own closing tag.
    assert page.rindex(marker) < page.rindex("</body>")


def test_injection_survives_a_page_with_no_body_tag(tmp_path: Path) -> None:
    from standalone_qc import dashboard

    cfg, score = _score_one(tmp_path)
    page = dashboard._inject_panels("<div>fragment</div>", score, cfg)
    assert "class='grid'" in page


# ---------------------------------------------------------------------------
# 3D plots and FSP timing
# ---------------------------------------------------------------------------
_FSP_NEW = """\
[PlaneUpdate] FIDUCIAL_FILE_DIRECT_PER_REGION: region 'S1R1' matched fiducial row xy=(-26913.0,-16150.5) z=38859.6 OL=0.100 StigX=-0.1 StigY=-0.2 - moving there, seeding, then B2F -> AF (+/-1200uA) -> AS
  [B2F] attempt 1/2 rc=0 hexfit=0.0 (ok)
[FiducialDirect] xy=(-26913.0,-16150.5) z=38859.6: seeded OL=0.100 StigX=-0.1 StigY=-0.2  (stage move 2.05s)
[FiducialDirect] B2F ok=True rc=0 projective=(-610.517,685.187)  (26.18s)
[FiducialDirect] AF (fine-only, range=+/-1200uA): conf=0.0 success=True  (2.63s) - no coarse fallback
[FiducialDirect] AS: confX=0.4579 confY=0.4062 ok=True  (4.88s)
[Timing] Region 'S1R1' breakdown (8 tile(s), 75.9s total):
  focus/stigmation  :     40.3s ( 53.1%)
"""

# The older format times only AS. Note the trap that broke the first
# version of the parser: the "measured ... at xy=(...)" RESULT line looks a
# lot like the "measuring ... at xy=(...)" announcement of a new point.
_FSP_OLD = """\
[PlaneUpdate] FIDUCIAL_FOCUS_POINT_XY_REGION_OFFSET: measuring one point for 'S1R1' at xy=(2563.2,3958.1) (sp[0] offset by 110.0 um) - B2F -> AF -> AS
[TopographyPrecheck] B2F rc=0, projective=(-609.539, 678.265)
[TopographyPrecheck] AF: fine=0.0000, coarse=None, success=True
[TopographyPrecheck] AF dz=+0.015 um (AP_DZ_FROM_OLENS, post-AF residual)
[TopographyPrecheck] AS: confX=0.2926 confY=0.3049 ok=True (4.15s, measured only - not applied to any plane)
[PlaneUpdate] FIDUCIAL_FOCUS_POINT_XY_REGION_OFFSET: measured OL=1.821 uA, StigX=0.107337, StigY=0.275880 at xy=(2563.2,3958.1) - re-leveling
"""


def test_fsp_timing_parses_the_fully_timed_log_format(tmp_path: Path) -> None:
    from standalone_qc import fsp_timing

    log = tmp_path / "console.log"
    log.write_text(_FSP_NEW, encoding="utf-8")
    recs = fsp_timing.parse_console_log(log)

    assert len(recs) == 1, "one support point, announced twice"
    r = recs[0]
    assert (r["move_s"], r["b2f_s"], r["focus_s"], r["stig_s"]) == (
        2.05, 26.18, 2.63, 4.88
    )
    assert r["af_ok"] is True and r["as_ok"] is True and r["b2f_ok"] is True
    assert r["total_s"] == pytest.approx(35.74)
    assert r["method"] == "FIDUCIAL_FILE_DIRECT_PER_REGION"
    # B2F is NOT folded into "move": here it is 10x the stage move and 26x
    # the autofocus, which is the whole point of charting it separately.
    assert r["b2f_s"] > r["move_s"] + r["focus_s"] + r["stig_s"]


def test_fsp_timing_reports_untimed_steps_as_unknown(tmp_path: Path) -> None:
    """The older format times only AS. focus/move must come back None, never
    0.0 - a zero would draw a bar reading "instant" for a step that was
    simply never measured."""
    from standalone_qc import fsp_timing

    log = tmp_path / "console.log"
    log.write_text(_FSP_OLD, encoding="utf-8")
    recs = fsp_timing.parse_console_log(log)

    assert len(recs) == 1, "the 'measured ... at xy=' line is not a new FSP"
    r = recs[0]
    assert r["stig_s"] == 4.15
    assert r["focus_s"] is None and r["move_s"] is None and r["b2f_s"] is None
    assert r["af_ok"] is True          # reported, just not timed
    assert r["ol"] == 1.821 and r["stig_x"] == 0.107337
    assert any("not timed" in n for n in r["notes"])

    html = fsp_timing.bars_html(recs)
    assert "<svg" in html
    assert "not timed in this log format" in html
    for absent in ("focus (AF)", "move"):
        assert f">{absent}<" not in html, "untimed steps must draw no bar"


def test_fsp_bars_omit_nothing_when_everything_is_timed(tmp_path: Path) -> None:
    from standalone_qc import fsp_timing

    log = tmp_path / "console.log"
    log.write_text(_FSP_NEW, encoding="utf-8")
    html = fsp_timing.bars_html(fsp_timing.parse_console_log(log))
    assert "not timed" not in html
    for label in ("focus (AF)", "stig (AS)", "move", "B2F"):
        assert label in html


def test_failed_autofocus_is_reported_as_a_failure_not_a_logging_gap(
    tmp_path: Path,
) -> None:
    """When AF fails, AS is skipped and nothing is timed.

    The empty chart then has two possible explanations and they are not
    equivalent: "this log format times nothing" is a note about logging,
    while "no focus step completed" is a section-level failure the operator
    needs to see. Real case: S27R2.
    """
    from standalone_qc import fsp_timing

    log = tmp_path / "console.log"
    log.write_text(
        "[PlaneUpdate] FIDUCIAL_FOCUS_POINT_XY_REGION_OFFSET: measuring one "
        "point for 'S27R2' at xy=(-16701.5,1808.8) - B2F -> AF -> AS\n"
        "[TopographyPrecheck] B2F rc=0, projective=(-609.5, 678.2)\n"
        "[TopographyPrecheck] AF: fine=0.0000, coarse=0.0000, success=False\n"
        "[TopographyPrecheck] AS skipped: AF failed\n",
        encoding="utf-8",
    )
    recs = fsp_timing.parse_console_log(log)
    assert len(recs) == 1
    assert recs[0]["af_ok"] is False
    assert recs[0]["as_skipped"] is True
    assert recs[0]["total_s"] is None

    html = fsp_timing.bars_html(recs)
    assert "No focus step completed" in html
    assert "autofocus failed" in html and "autostig skipped" in html
    assert "log format" not in html, "must not blame logging for a failure"
    assert "#a33" in html, "a section-level failure should read as a warning"


def test_fsp_timings_reach_the_cached_facts(tmp_path: Path) -> None:
    """And a cache written by the previous schema must not be served."""
    from standalone_qc import section_facts

    sec = tmp_path / "S1R1"
    sec.mkdir()
    (sec / "console.log").write_text(_FSP_NEW, encoding="utf-8")

    facts = section_facts.load_or_collect(sec, "S1R1")
    assert facts["fsp_timings"] and facts["fsp_timings"][0]["stig_s"] == 4.88
    assert facts["aggregate"]["focus_stig_s"] == 40.3

    # An old-schema cache is ignored rather than returned without the key.
    cache = section_facts.cache_path(sec)
    cache.write_text(json.dumps({"facts": {"duration_s": 1.0}}), encoding="utf-8")
    assert section_facts.load_cached(sec) is None
    assert section_facts.load_or_collect(sec, "S1R1")["fsp_timings"]


def test_3d_scatter_is_self_contained_and_carries_its_data(
    tmp_path: Path,
) -> None:
    """No external script may be referenced: these pages are opened over
    file://, where a CDN may be unreachable."""
    from standalone_qc import plot3d

    pts = [(0.0, 0.0, 1.0), (100.0, 0.0, 2.0), (0.0, 100.0, 3.0),
           (100.0, 100.0, 4.2)]
    div = plot3d.scatter3d_html(pts, "OL", "uA", plane=(0.01, 0.02, 1.0),
                                point_labels=["a", "b", "c", "d"])
    assert "<canvas>" in div and "class='p3d'" in div
    payload = json.loads(re.search(
        r"<script type='application/json'>(.*?)</script>", div, re.S).group(1))
    assert len(payload["points"]) == 4
    assert payload["plane"] == [0.01, 0.02, 1.0]
    # Each point carries x, y, value, a colour and a tooltip.
    assert payload["points"][0][0:3] == [0.0, 0.0, 1.0]
    assert payload["points"][0][3].startswith("#")
    assert "a" in payload["points"][0][4]

    engine = plot3d.engine_html()
    assert "<script" in engine and "<style" in engine
    assert "src=" not in engine, "the renderer must not load anything remote"
    # The wheel is deliberately not bound - see plot3d's module docstring.
    assert "wheel" not in engine

    assert "no data" in plot3d.scatter3d_html([], "OL")


def test_ol_is_plotted_in_microns_and_the_tilt_is_preserved() -> None:
    """OL displays as microns of focus, not raw OL microamps.

    Two things must hold: the conversion divides by OL_UA_PER_UM, and the
    fitted plane's TILT is unchanged by the reference-to-mean shift - the
    slope in um/mm is reported elsewhere from the uA plane, so the two must
    agree or the axis and the number would disagree.
    """
    from standalone_qc import focus_planes
    from standalone_qc.focus_planes import OL_UA_PER_UM, PlaneFit

    # A plane tilted purely in x: 119.262 uA over 1000 um = 1 um per mm.
    pts = [(0.0, 0.0, 2_084_877.0),
           (1000.0, 0.0, 2_084_877.0 + OL_UA_PER_UM),
           (0.0, 1000.0, 2_084_877.0),
           (1000.0, 1000.0, 2_084_877.0 + OL_UA_PER_UM)]
    fit = PlaneFit(quantity="ol", label="OL", unit="uA", n=len(pts),
                   points=pts, plane=(OL_UA_PER_UM / 1000.0, 0.0, 2_084_877.0),
                   rms=0.0, span=OL_UA_PER_UM)

    d = focus_planes.to_display_units(fit)
    assert d.unit == "µm"
    assert "µm" in d.axis_label and "OL" in d.axis_label

    # Values: microns, centred on the section mean.
    zs = [z for _, _, z in d.points]
    assert max(zs) - min(zs) == pytest.approx(1.0)      # 119.262 uA = 1 um
    assert sum(zs) / len(zs) == pytest.approx(0.0, abs=1e-9)
    assert max(abs(z) for z in zs) < 1.0, "should be referenced to the mean"

    # The plane still passes through the converted points.
    a, b, c = d.plane
    for x, y, z in d.points:
        assert a * x + b * y + c == pytest.approx(z, abs=1e-9)

    # And the gradient of the CONVERTED plane matches the um/mm that
    # gradient_stats derives from the uA plane.
    import math

    reported = focus_planes.gradient_stats(fit.plane)["grad_um_per_mm"]
    assert reported == pytest.approx(1.0)
    assert math.hypot(a, b) * 1000.0 == pytest.approx(reported)

    # Scale converts the stats that sit beside the plot.
    assert fit.span * d.scale == pytest.approx(1.0)
    # The raw reading stays reachable in the tooltip, as a literal µ - the
    # plotter HTML-escapes these strings, so "&micro;" would reach the
    # tooltip as the text "&micro;A".
    assert any("µA" in t for t in d.tips)
    assert not any("&" in t for t in d.tips)


def test_stig_is_not_unit_converted() -> None:
    """StigX/StigY are dimensionless - the OL calibration must not touch
    them."""
    from standalone_qc import focus_planes
    from standalone_qc.focus_planes import PlaneFit

    pts = [(0.0, 0.0, 0.10), (100.0, 0.0, 0.12)]
    fit = PlaneFit(quantity="stig_x", label="StigX", n=2, points=pts,
                   plane=(2e-4, 0.0, 0.10), span=0.02, rms=0.0)
    d = focus_planes.to_display_units(fit)
    assert d.scale == 1.0
    assert d.points == pts and d.plane == fit.plane
    assert d.note == ""


def test_timing_bars_label_the_vertical_axis() -> None:
    from standalone_qc import plot3d

    svg = plot3d.grouped_bars_svg(
        ["S1", "S2"], [("focus (AF)", "#2e6fb7", [1.0, 2.0])]
    )
    assert "Time (s)" in svg
    assert "rotate(-90" in svg, "the axis label should run up the left edge"


def test_plots_are_at_least_as_large_as_the_coverage_map() -> None:
    """The operator asked for these to match the mFoV coverage map's size;
    a later tweak to one default should not silently shrink them again."""
    import inspect

    from standalone_qc import mfov_overlap, plot3d

    cov_w = inspect.signature(mfov_overlap.coverage_svg).parameters["width"].default
    p3d_h = inspect.signature(plot3d.scatter3d_html).parameters["height"].default
    bar_h = inspect.signature(plot3d.grouped_bars_svg).parameters["height"].default
    assert p3d_h >= cov_w
    assert bar_h >= 340


def test_dashboard_uses_3d_plots_and_loads_the_engine_once(
    tmp_path: Path,
) -> None:
    from standalone_qc import dashboard

    cfg, score = _score_one(tmp_path)
    page = dashboard._inject_panels("<html><body><p>x</p></body></html>",
                                    score, cfg)
    fittable = [f for f in score.plane_fits
                if f["plane"] and f["n"] and not f["degenerate"]]
    if not fittable:
        pytest.skip("fixture has no non-degenerate plane to plot")
    assert "class='p3d'" in page
    # One engine per page, however many plots.
    assert page.count("data-p3d-engine=\"1\"") == 2      # the <style> and <script>
    assert _script_depth_at(page, "class='p3d'") == 0

    # No caller may smuggle an HTML entity into a plot title or tooltip:
    # the plotter escapes them, so "&micro;A" would display as that text.
    for payload in re.findall(
        r"<script type='application/json'>(.*?)</script>", page, re.S
    ):
        j = json.loads(payload)
        if "points" not in j:
            continue
        for p in j["points"]:
            assert "&amp;" not in p[4], f"double-escaped entity: {p[4]}"
        for lab in j["labels"]:
            assert "&amp;" not in lab, f"double-escaped entity: {lab}"


def test_analysis_version_invalidates_older_results(tmp_path: Path) -> None:
    """A new per-section output must make existing results read as stale.

    Without this the ledger insists a section is finished even though its
    summary predates the check that was just added, so "Analyze All Pending"
    does nothing and the new column stays empty - which is how the mFoV
    overlap column came up blank on 23 of 24 sections.
    """
    from standalone_qc import config as cfgmod

    cfg = _cfg()
    before = policy_fingerprint(cfg)
    original = cfgmod.ANALYSIS_VERSION
    try:
        cfgmod.ANALYSIS_VERSION = original + 1
        assert policy_fingerprint(cfg) != before
    finally:
        cfgmod.ANALYSIS_VERSION = original
    assert policy_fingerprint(cfg) == before

    # Turning a check off is also a policy change, not a free switch.
    assert policy_fingerprint(_cfg(overlap_check_enabled=False)) != before


def test_global_report_shows_one_row_per_section(tmp_path: Path) -> None:
    """A section imaged in several batches collapses to a single row showing
    the newest batch - the Attempt column that used to distinguish them is
    gone, so duplicate rows would be indistinguishable."""
    cfg, _ = _score_one(tmp_path)
    # A second, later batch that re-images S1R1.
    other = tmp_path / "w9h9_retry_8NM_20260910_090000"
    other.mkdir()
    (other / "region_manifest.json").write_text(
        json.dumps({"regions": [
            {"idx": 1, "name": "S1R1", "folder": "S1R1", "status": "done"}
        ]}),
        encoding="utf-8",
    )
    (other / "S1R1").mkdir()

    out = rollup.write_global_report(tmp_path, cfg)
    assert out is not None and out.name == rollup.GLOBAL_REPORT_NAME
    html = out.read_text(encoding="utf-8")

    assert html.count("<strong>S1R1</strong>") == 1
    # Collapsing must not silently hide that it was imaged twice.
    assert "newest of 2 batches" in html
    assert 'rowspan="2"' not in html

    # Both dropped columns are gone from the SECTIONS table. Scoped to it
    # deliberately: the Batches table keeps its own "Analyzed" column, which
    # is a count of analyzed sections per batch and was not in scope.
    sections_table = html.split("<h2>Batches")[0]
    assert "Attempt" not in sections_table
    assert "attempt 1/2" not in sections_table
    assert ">Analyzed</th>" not in sections_table
    # Both tables still present. The Batches heading carries the acquisition
    # report's own "(* = see tooltip...)" note, so match only its prefix.
    assert "<h2>Sections (3)</h2>" in html
    assert "<h2>Batches (2)" in html


def test_global_report_progress_and_awaiting(tmp_path: Path) -> None:
    cfg, _ = _score_one(tmp_path)
    out = rollup.write_global_report(tmp_path, cfg)
    html = out.read_text(encoding="utf-8")
    # One of three sections scored.
    assert "1/3 section(s) analyzed (33%)" in html
    assert 'class="progress"' in html
    # S2R1 failed and S3R1 is pending, so neither is "awaiting QC" - only a
    # section that finished imaging but has no QC yet belongs on that list.
    assert "Awaiting analysis" not in html.split("<main>")[0] or "S2R1" not in html.split("<main>")[0]


def test_reports_link_to_each_other_and_to_sections(tmp_path: Path) -> None:
    cfg, _ = _score_one(tmp_path)
    written = rollup.write_all_reports(tmp_path, cfg)
    assert "__global__" in written and BATCH in written

    # Links are relative, so copying the tree or remapping the drive letter
    # (V:\ on the acquisition PC vs Z:\ here) does not break them.
    assert _broken_links(written["__global__"], tmp_path) == []
    assert _broken_links(written[BATCH], tmp_path / BATCH) == []

    batch_html = written[BATCH].read_text(encoding="utf-8")
    assert "whole run ↑" in batch_html          # batch -> run
    global_html = written["__global__"].read_text(encoding="utf-8")
    assert rollup.BATCH_REPORT_NAME in global_html   # run -> batch


def test_review_controls_are_present_and_shared(tmp_path: Path) -> None:
    cfg, _ = _score_one(tmp_path)
    written = rollup.write_all_reports(tmp_path, cfg)

    for page in (written["__global__"], written[BATCH]):
        html = page.read_text(encoding="utf-8")
        assert 'class="review-group"' in html
        assert html.count('class="review-group"') == 3   # one per section
        for status in ("not_verified", "accepted", "retake"):
            assert f'data-status="{status}"' in html
        # The autosave machinery comes from live_qc_support, so both sides
        # write the same region_review_status.json.
        if review.REVIEW_UI_AVAILABLE:
            assert "showSaveFilePicker" in html
            assert "region_review_status.json" in html
            # Keyed on the RUN root, so our report and acquisition's reconnect
            # to the same remembered file handle.
            assert str(tmp_path) in html


def test_existing_review_status_is_loaded_not_reset(tmp_path: Path) -> None:
    cfg, _ = _score_one(tmp_path)
    review.review_status_path(tmp_path).write_text(
        json.dumps({"S1R1": {"status": "retake", "reason": "looks defocused"}}),
        encoding="utf-8",
    )
    out = rollup.write_global_report(tmp_path, cfg)
    html = out.read_text(encoding="utf-8")
    # The saved decision must come back pre-selected, and its reason with it -
    # regenerating a report must never silently discard a human's verdict.
    assert "looks defocused" in html
    payload = re.search(
        r'<script id="review-payload"[^>]*>(.*?)</script>', html, re.S
    )
    assert payload
    assert json.loads(payload.group(1))["S1R1"]["status"] == "retake"


def test_retake_list_is_synced_from_review(tmp_path: Path) -> None:
    cfg, _ = _score_one(tmp_path)
    review.review_status_path(tmp_path).write_text(
        json.dumps({"S1R1": {"status": "retake", "reason": "defocused"}}),
        encoding="utf-8",
    )
    rollup.write_global_report(tmp_path, cfg)
    retake = tmp_path / review.RETAKE_LIST_FILENAME
    # This is the file the acquisition pipeline reads for "skip already
    # imaged", so the loop back to re-imaging depends on it being written.
    if retake.is_file():
        assert "S1R1" in retake.read_text(encoding="utf-8")
    else:
        pytest.skip("sync_retake_list unavailable in this clone")


def test_unscored_sections_still_get_their_acquisition_columns(tmp_path: Path) -> None:
    """The point of the post-acquisition workflow: pointing at a batch shows
    a useful table straight away. Duration, FSP #, mFoV # and the file checks
    all come from acquisition's own artefacts, so they must NOT wait on image
    scoring."""
    batch = build_fixture(tmp_path)
    cfg = _cfg()
    (batch / "S1R1" / "console.log").write_text(
        "[Region] START region_name=S1R1 region_idx=1\n"
        "[PlaneUpdate] ONE_POINT_OFFSET: measuring one point for 'S1R1' at xy=(1,2)\n"
        "[PlaneUpdate] ONE_POINT_OFFSET: measured OL=2655.1 uA, StigX=0.07, StigY=0.27\n"
        "[Timing] Region 'S1R1' breakdown (7 tile(s), 123.4s total):\n"
        "[Region] DONE region_name=S1R1\n",
        encoding="utf-8",
    )

    # Nothing scored at all.
    assert rollup.scan_sections(tmp_path, cfg) == 1
    out = rollup.write_batch_report(tmp_path, batch, cfg)
    html = out.read_text(encoding="utf-8")

    assert "1/1" in html          # FSP succeeded/attempted
    assert "2m 3.4s" in html      # duration from the [Timing] line
    assert "&#10003;" in html     # file-presence checkmarks

    rows = rollup.collect_rows(tmp_path, cfg)
    s1 = next(r for r in rows if r.region.name == "S1R1")
    assert s1.summary is None                       # never scored
    assert s1.facts["fsp_succeeded"] == 1           # ...but facts are there
    assert s1.facts["plane_method"] == "ONE_POINT_OFFSET"
    assert s1.facts["files"]["n_mfovs"] == 7


def test_scan_is_cached_and_skipped_second_time(tmp_path: Path) -> None:
    batch = build_fixture(tmp_path)
    cfg = _cfg()
    assert rollup.scan_sections(tmp_path, cfg) == 1
    cache = section_facts.cache_path(batch / "S1R1")
    assert cache.is_file()

    # only_missing (the default) must not redo the work - a ~2.3s-per-section
    # pass repeated on every refresh is exactly what the cache exists to stop.
    assert rollup.scan_sections(tmp_path, cfg) == 0
    assert rollup.scan_sections(tmp_path, cfg, only_missing=False) == 1


def test_scoring_reuses_a_cached_scan(tmp_path: Path) -> None:
    build_fixture(tmp_path)
    cfg = _cfg()
    rollup.scan_sections(tmp_path, cfg)
    score = score_region(find_scorable_regions(tmp_path)[0], cfg)
    # Facts come back on the score without re-listing every mFoV folder.
    assert score.facts and score.facts["files"]["n_mfovs"] == 7


def test_corrupt_facts_cache_falls_back(tmp_path: Path) -> None:
    batch = build_fixture(tmp_path)
    cfg = _cfg()
    rollup.scan_sections(tmp_path, cfg)
    section_facts.cache_path(batch / "S1R1").write_text("{ truncated",
                                                        encoding="utf-8")
    assert section_facts.load_cached(batch / "S1R1") is None
    # A bad cache must not break the report, and must be re-gatherable.
    assert rollup.write_batch_report(tmp_path, batch, cfg) is not None
    assert rollup.scan_sections(tmp_path, cfg) == 1


def _coord_section(root: Path, offsets, beams=(0, 1800, 3600)) -> Path:
    """A synthetic section: each mFoV a 3x3 grid of overlapping beams, placed
    at the given global offsets. Beam images are 2000x1748 like the real ones,
    written tiny-but-correctly-sized so the geometry is real."""
    from PIL import Image
    sec = root / "SEC"
    for i, (ox, oy) in enumerate(offsets, start=1):
        d = sec / f"mfov{i}"
        d.mkdir(parents=True, exist_ok=True)
        lines = []
        n = 0
        for bx in beams:
            for by in beams:
                n += 1
                name = f"mfov{i}__{n:03d}.bmp"
                Image.new("L", (2000, 1748), 128).save(d / name)
                lines.append(f"{d / name}\t{ox + bx}\t{oy + by}\t0")
        (d / "image_coordinates.txt").write_text(
            "\n".join(lines) + "\n", encoding="utf-8"
        )
    return sec


def test_overlap_check_passes_when_mfovs_interlock(tmp_path: Path) -> None:
    from standalone_qc import mfov_overlap
    # Two mFoVs 3400px apart: each spans 3600+2000 = 5600px, so they overlap.
    sec = _coord_section(tmp_path, [(0, 0), (3400, 0)])
    r = mfov_overlap.analyze_section(sec, {"pixel_size_nm": 8.0})
    assert r.n_mfovs == 2
    assert r.n_gap_cells == 0
    assert r.weak_pairs == []
    assert r.passed is True
    # 5.5%-ish sFoV overlap: beams 1800 apart, 2000 wide -> 10%.
    assert 0.05 < r.sfov_overlap_frac < 0.15


def test_overlap_check_detects_a_gap(tmp_path: Path) -> None:
    """A checker that never fails is worthless.

    A RING of mFoVs: each overlaps its two ring neighbours, but the centre is
    left uncovered. That is the only shape that produces a genuinely enclosed
    hole - an L or a cross leaves every uncovered cell connected to the
    outside, which the flood fill correctly declines to call a gap.
    """
    import math
    from standalone_qc import mfov_overlap
    # Each mFoV footprint is 3600 + 2000 = 5600 px across, so half-width
    # 2800. On a circle of radius 6000 the ring neighbours are 4592 px apart
    # and overlap, while the centre sits 3200 px beyond any coverage.
    offsets = [
        (int(6000 * math.cos(2 * math.pi * k / 8)),
         int(6000 * math.sin(2 * math.pi * k / 8)))
        for k in range(8)
    ]
    sec = _coord_section(tmp_path, offsets)
    r = mfov_overlap.analyze_section(sec, {"pixel_size_nm": 8.0})
    assert r.n_mfovs == 8
    assert r.n_gap_cells > 0, "an enclosed gap between mFoVs was not detected"
    assert r.largest_gap_cells > 1
    assert r.passed is False
    assert "gap" in r.note
    # Reported in real units when a pixel size is supplied.
    assert r.gap_area_um2 and r.gap_area_um2 > 0
    assert r.gap_points, "gap locations must be available for rendering"


def test_overlap_edge_is_not_a_gap(tmp_path: Path) -> None:
    """The ragged outside of a section is uncovered but is not a fault - only
    an enclosed hole is. Without the flood fill every section would fail."""
    from standalone_qc import mfov_overlap
    sec = _coord_section(tmp_path, [(0, 0)])
    r = mfov_overlap.analyze_section(sec, {})
    assert r.n_gap_cells == 0
    assert r.passed is True


def test_nothing_scored_yields_no_report(tmp_path: Path) -> None:
    build_fixture(tmp_path)
    cfg = _cfg()
    # Reports are still written (sections exist, just unscored) - what must
    # NOT happen is a crash or a bogus 100% progress bar.
    out = rollup.write_global_report(tmp_path, cfg)
    assert out is not None
    html = out.read_text(encoding="utf-8")
    assert "0/3 section(s) analyzed (0%)" in html
    assert "Awaiting analysis (1)" in html      # S1R1 is done but unscored


def test_missing_run_root_is_not_an_error(tmp_path: Path) -> None:
    assert rollup.write_global_report(tmp_path / "nope", _cfg()) is None
    assert rollup.collect_rows(tmp_path / "nope", _cfg()) == []


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))


def test_hex_map_panels_are_removed(tmp_path: Path) -> None:
    """Panels 1-3 restate what the overview overlay and the fitted planes
    already show, so the operator asked for them gone. Keyed on the circled
    digit, not the heading text, so our own rename of panel 2 cannot break
    the match."""
    from standalone_qc import dashboard

    cfg, score = _score_one(tmp_path)
    page = (
        '<section class="panel overview"><h3>①① keep me</h3>x</section>'
        '<section class="panel"><h3>① Focus quality</h3><svg/></section>'
        '<section class="panel"><h3 title="t">② Renamed Stig</h3><svg/></section>'
        '<section class="panel"><h3>③ Status</h3><svg/></section>'
        '<section class="panel"><h3>④ Image quality per beam</h3><svg/></section>'
        '<section class="panel"><h3>⑤ Retake selection</h3><svg/></section>'
    )
    out = dashboard._strip_panels(page, cfg)
    for gone in ("Focus quality", "Renamed Stig", "③ Status"):
        assert gone not in out, gone
    for kept in ("Image quality per beam", "Retake selection"):
        assert kept in out, kept
    # The overview panel must survive: it carries the only click-through to
    # the beam images once the hex maps are gone.
    assert 'class="panel overview"' in out

    # Idempotent, so the retrofit tool can run over mixed pages.
    assert dashboard._strip_panels(out, cfg) == out

    # Configurable: an operator who wants one back can say so.
    kept_all = dashboard._strip_panels(page, dict(cfg, dashboard_hidden_panels=[]))
    assert kept_all == page


def test_3d_plot_carries_a_colour_legend() -> None:
    """The colour is a residual on an auto-scaled symmetric ramp, which is
    unreadable without a key. The bar is drawn in JS from these payload
    values so it cannot drift from the colours the points actually got."""
    import json as _json
    import re as _re

    from standalone_qc import plot3d
    from standalone_qc.focus_planes import _DIVERGING

    pts = [(0.0, 0.0, 1.0), (100.0, 0.0, 2.0), (0.0, 100.0, 1.5)]
    div = plot3d.scatter3d_html(pts, "OL", "um", plane=(0.005, 0.004, 1.0))
    pay = _json.loads(_re.search(
        r"<script type='application/json'>(.*?)</script>", div, _re.S).group(1))

    assert pay["resid_max"] > 0
    assert [t for t, _ in pay["ramp"]] == [t for t, _ in _DIVERGING]
    assert pay["ramp"][0][1] == "#053061" and pay["ramp"][-1][1] == "#67001f"
    assert "residual" in pay["color_label"] and "um" in pay["color_label"]

    # Without a plane the colour means the value itself, and says so.
    pay2 = _json.loads(_re.search(
        r"<script type='application/json'>(.*?)</script>",
        plot3d.scatter3d_html(pts, "diff", ""), _re.S).group(1))
    assert pay2["color_label"].startswith("value")

    engine = plot3d.engine_html()
    assert "drawColorBar" in engine
    assert "createLinearGradient" in engine
    # The drop lines to the floor are gone.
    assert "foot" not in engine


def test_dashboard_gets_a_live_threshold_control(tmp_path: Path) -> None:
    """The overlay's colours are baked at analysis time, so changing the
    threshold used to mean re-analyzing. The page carries each mFoV's
    quality in its own label, so the control recolours in the browser."""
    from standalone_qc import dashboard

    cfg, _ = _score_one(tmp_path)
    page = (
        '<section class="panel overview">'
        '<label><input id="toggle-overview-quality">'
        'Show focus-quality overlay</label>'
        '<svg><rect class="qtile" fill="#111"/><text class="qlabel">5.0</text>'
        '<rect class="qtile" fill="#222"/><text class="qlabel">40.0</text>'
        '</svg></section>'
    )
    out = dashboard._inject_quality_threshold(page, dict(cfg, low_quality_threshold=12.0))
    assert 'id="qc-threshold"' in out
    assert 'value="12.0"' in out, "should start at the configured threshold"
    assert "qc-threshold-reset" in out

    # It must wait for the document: the rects it recolours are emitted
    # AFTER it, so running at parse time finds none and does nothing.
    assert "DOMContentLoaded" in out
    assert out.index('id="qc-threshold"') < out.index('class="qtile"')

    # A page with no overview has nothing to recolour and is left alone.
    assert dashboard._inject_quality_threshold("<html></html>", cfg) == "<html></html>"


def _project(x, y, z, yaw=-0.62, pitch=0.42, base=268.8, cx=204.0, cy=218.0,
             zoom=1.0):
    """The engine's projection, mirrored in Python.

    Duplicated on purpose: it lets the tick-placement claim be checked
    deterministically, without a browser and without reading pixels. If the
    JS projection changes, this drifts and the test below stops meaning
    anything - so it asserts the SEPARATION, which any sane projection of
    three distinct 3D points preserves, rather than exact coordinates.
    """
    import math

    ca, sa = math.cos(yaw), math.sin(yaw)
    xr = x * ca - y * sa
    yr = x * sa + y * ca
    ce, se = math.cos(pitch), math.sin(pitch)
    s = base * zoom
    return (cx + xr * s, cy - (z * ce - yr * se) * s)


def test_the_three_origin_ticks_do_not_overlap() -> None:
    """All three axes start at the same box corner, so their minimum values
    used to print on top of one another and none could be read - reported
    from a real section. Each tick is now pushed outward along its OWN axis,
    which separates them for any rotation.
    """
    import math
    import re

    from standalone_qc import plot3d

    src = plot3d.engine_html()
    m = re.search(r"var PAD = ([\d.]+);", src)
    assert m, "the outward tick offset is gone - ticks will overlap again"
    pad = float(m.group(1))
    assert pad > 0.05, f"PAD={pad} is too small to separate the labels"

    # Where the three minimum-value labels land.
    pts = [(-0.5 - pad, -0.5, -0.5),      # x axis minimum
           (-0.5, -0.5 - pad, -0.5),      # y axis minimum
           (-0.5, -0.5, -0.5 - pad)]      # z axis minimum
    screen = [_project(*p) for p in pts]
    for i in range(3):
        for j in range(i + 1, 3):
            d = math.dist(screen[i], screen[j])
            assert d > 14, (
                f"axis {i} and {j} minimum labels are {d:.1f}px apart - "
                f"they will still collide"
            )

    # Without the offset they are the same point, which is the bug.
    same = [_project(-0.5, -0.5, -0.5)] * 3
    assert math.dist(same[0], same[1]) == 0


def test_plot_text_is_large_enough_and_zoom_is_offered() -> None:
    from standalone_qc import plot3d

    src = plot3d.engine_html()
    sizes = [int(n) for n in re.findall(r"(\d+)px ui-monospace", src)]
    assert sizes, "no font sizes found"
    assert min(sizes) >= 11, f"text as small as {min(sizes)}px is unreadable"
    # Ticks get a white halo so one landing on the wireframe stays legible.
    assert "strokeText" in src and "'#fff'" in src

    div = plot3d.scatter3d_html([(0, 0, 1), (1, 0, 2), (0, 1, 3)], "OL", "um")
    for label in ("zoom &#43;", "zoom &minus;", "top view", "reset"):
        assert label in div, f"missing control: {label}"


def test_overlay_checkbox_clears_the_mfov_outlines(tmp_path: Path) -> None:
    """Unticking "Show focus-quality overlay" cleared only the fill
    upstream, so a black outline stayed around every mFoV and the stitched
    image was still covered in a grid."""
    from standalone_qc import dashboard

    cfg, _ = _score_one(tmp_path)
    page = (
        '<section class="panel overview">'
        '<label><input id="toggle-overview-quality" checked>'
        'Show focus-quality overlay</label>'
        '<svg><rect class="qtile" fill="#111" stroke="#000" '
        'stroke-opacity="0.25"/><text class="qlabel">5.0</text></svg>'
        '</section>'
    )
    out = dashboard._inject_quality_threshold(page, cfg)
    assert "stroke-opacity" in out
    assert "setOutlines" in out
    # It reads the ORIGINAL opacity rather than assuming 0.25, so restoring
    # cannot invent a value upstream never used.
    assert "getAttribute('stroke-opacity')" in out
    assert "toggle-overview-quality" in out


def test_batch_report_shows_the_focus_strategy(tmp_path: Path) -> None:
    """Which focus strategy imaged a section is the first thing to check
    when comparing one batch against another."""
    cfg, score = _score_one(tmp_path)
    out = rollup.write_batch_report(tmp_path, tmp_path / BATCH, cfg)
    html = out.read_text(encoding="utf-8")
    assert "focus strategy" in html

    # The long constants get a short label, with the original on hover.
    cell = rollup._focus_strategy_cell(
        {"plane_method": "FIDUCIAL_FILE_DIRECT_PER_REGION"})
    assert "fiducial file, direct" in cell
    assert 'title="FIDUCIAL_FILE_DIRECT_PER_REGION"' in cell

    cell = rollup._focus_strategy_cell(
        {"plane_method": "FIDUCIAL_FOCUS_POINT_XY_REGION_OFFSET"})
    assert "fiducial point + offset" in cell

    # An unmapped strategy shows verbatim rather than vanishing: a new one
    # on the acquisition side must be visible the first time it is used.
    cell = rollup._focus_strategy_cell({"plane_method": "SOME_NEW_MODE"})
    assert "some_new_mode" in cell and 'title="SOME_NEW_MODE"' in cell

    # No console.log parsed yet is a dash, not a guess.
    assert "&mdash;" in rollup._focus_strategy_cell({})
    assert "&mdash;" in rollup._focus_strategy_cell({"plane_method": None})


def test_overview_click_can_be_narrowed_to_one_beam(tmp_path: Path) -> None:
    """A click opens all 91 beams of the mFoV by default, which is why it
    was unclear what it selects - nothing is. The picker narrows it."""
    from standalone_qc import dashboard

    cfg, _ = _score_one(tmp_path)
    page = (
        '<section class="panel overview">'
        '<label><input id="toggle-overview-quality" checked>'
        'Show focus-quality overlay</label>'
        '<svg><a data-tile="7"><rect class="qtile" fill="#111"/></a>'
        '<text class="qlabel">5.0</text></svg></section>'
        '<script id="tile-bmps-payload" type="application/json">{}</script>'
    )
    out = dashboard._inject_quality_threshold(page, cfg)

    assert '<select id="qc-beam"' in out, "must be a dropdown, not a field"
    # A SELECT, not a number box with an "all" placeholder: greyed
    # placeholder text reads as a value, so an operator tried to clear a
    # field that was already empty and concluded it was broken.
    assert 'placeholder="all"' not in out
    assert '<option value="">' in out, "the default option must mean all beams"
    # The beam list is built in the browser from the images actually
    # present, rather than assuming 91 - a section may carry a different
    # beam set, and offering a beam that is not there would open nothing.
    assert "beam ' + n + ' only" in out
    assert "bmps[k].length > most" in out
    # Beam is matched by FILENAME where possible, so a short or gappy list
    # cannot quietly show a different beam than the one asked for.
    assert "indexOf(want)" in out
    # Capture phase, or the page's own handler opens all 91 as well.
    assert "}, true);" in out
    # The modifier clicks upstream defines must still work.
    assert "ev.shiftKey" in out and "ev.metaKey" in out
    # Single-beam view keeps the same display inversion as the all-beams one.
    assert "filter:invert(1)" in out


def test_single_beam_viewer_can_zoom(tmp_path: Path) -> None:
    """Opening one beam must pan and zoom like the all-beams viewer.

    The first version wrote a plain <img>, so picking a beam silently
    traded away zoom - which is most of what an sFoV viewer is for.
    """
    from standalone_qc import dashboard

    cfg, _ = _score_one(tmp_path)
    page = (
        '<section class="panel overview">'
        '<label><input id="toggle-overview-quality" checked>'
        'Show focus-quality overlay</label>'
        '<svg><a data-tile="1"><rect class="qtile" fill="#111"/></a>'
        '<text class="qlabel">5.0</text></svg></section>'
    )
    out = dashboard._inject_quality_threshold(page, cfg)

    for needed in ("zoombox", "zoom-badge", "'wheel'", "dblclick",
                   "getBoundingClientRect", "transform-origin:0 0"):
        assert needed in out, f"single-beam viewer lost {needed}"
    # Cursor-anchored zoom, matching the all-beams viewer rather than
    # scaling about the corner.
    assert "mx - (mx - st2.tx) * ratio" in out
    # Panning only once zoomed in, as upstream does.
    assert "if (st2.scale <= MIN) { return; }" in out


def test_injected_script_is_not_cut_short(tmp_path: Path) -> None:
    """A closing script tag ANYWHERE in the injected block - including
    inside a JS comment, which is how this broke - ends the script element
    early and silently discards everything after it. The page still looks
    complete; the controls simply do nothing.
    """
    import re as _re

    from standalone_qc import dashboard

    cfg, _ = _score_one(tmp_path)
    page = (
        '<section class="panel overview">'
        '<label><input id="toggle-overview-quality" checked>'
        'Show focus-quality overlay</label>'
        '<svg><a data-tile="1"><rect class="qtile" fill="#111"/></a>'
        '<text class="qlabel">5.0</text></svg></section>'
    )
    out = dashboard._inject_quality_threshold(page, cfg)

    blocks = _re.findall(r"<script>(.*?)</script>", out, _re.S)
    assert blocks, "no plain script block was injected"
    js = blocks[-1]
    # Balanced, i.e. not truncated part-way through.
    assert js.count("{") == js.count("}"), "unbalanced braces - script truncated"
    assert js.count("(") == js.count(")"), "unbalanced parens - script truncated"
    # And nothing in it can terminate the element early.
    assert not _re.search(r"</\s*script", js, _re.I), \
        "the injected script contains a closing script tag"
    # Both control handlers survive to the end of the block.
    assert "qc-threshold-reset" in js and "openOneBeam" in js
    assert js.rstrip().endswith("})();")


def test_equal_thresholds_mean_a_hard_cutoff() -> None:
    """Setting both ends of the scale to the same value is a reasonable way
    to ask for pass/fail. It used to produce Normalize(vmin=30, vmax=30) - a
    degenerate scale that painted EVERY mFoV one colour whatever its
    quality, so the overlay looked like it ignored the threshold.
    """
    from standalone_qc.config import load_config
    from standalone_qc.dashboard import build_colormaps

    def colour(cm, q):
        rgba = cm["cmap_focus"](cm["norm_focus"](q))
        return "#%02x%02x%02x" % tuple(int(255 * c) for c in rgba[:3])

    cm = build_colormaps(load_config(
        None, low_quality_threshold=30.0, high_quality_threshold=30.0))
    below = [colour(cm, q) for q in (-100, 0, 20, 29.9)]
    above = [colour(cm, q) for q in (30, 30.1, 90, 1000)]
    assert len(set(below)) == 1 and len(set(above)) == 1
    assert below[0] != above[0], "every quality painted the same colour"
    assert below[0] == "#c0392b" and above[0] == "#2e8b3d"

    # An inverted pair is treated the same way rather than inverting the
    # meaning of the colours.
    cm = build_colormaps(load_config(
        None, low_quality_threshold=30.0, high_quality_threshold=20.0))
    assert colour(cm, 29.9) == "#c0392b" and colour(cm, 30.0) == "#2e8b3d"

    # The normal case still ramps.
    cm = build_colormaps(load_config(
        None, low_quality_threshold=10.0, high_quality_threshold=30.0))
    assert len({colour(cm, q) for q in (0, 15, 20, 25, 30)}) > 2


def test_the_page_control_starts_at_the_configured_threshold() -> None:
    """The in-page control must open at the value the page was generated
    with, or the number on screen contradicts the colours under it."""
    from standalone_qc import dashboard
    from standalone_qc.config import load_config

    page = ('<section class="panel overview"><label>'
            '<input id="toggle-overview-quality" checked>'
            'Show focus-quality overlay</label>'
            '<svg><rect class="qtile" fill="#111"/>'
            '<text class="qlabel">5.0</text></svg></section>')
    for want in (10.0, 25.0, 30.0, 42.5):
        out = dashboard._inject_quality_threshold(
            page, load_config(None, low_quality_threshold=want))
        assert f'value="{want:.1f}"' in out, want


def test_overview_can_be_zoomed_and_panned(tmp_path: Path) -> None:
    """The overview is an <svg> with a viewBox, so zoom was always possible
    - it simply was not wired. Everything in the panel (mFoV rects, quality
    labels, FSP markers) is in that same SVG, so it scales with it."""
    from standalone_qc import dashboard

    cfg, score = _score_one(tmp_path)
    page = ('<section class="panel overview"><label>'
            '<input id="toggle-overview-quality" checked>'
            'Show focus-quality overlay</label>'
            '<svg viewBox="0 0 100 200"><a data-tile="1">'
            '<rect class="qtile" fill="#111"/></a>'
            '<text class="qlabel">5.0</text></svg></section>')
    out = dashboard._inject_overview_tools(page, score, cfg)

    for needed in ("qc-zoom-in", "qc-zoom-out", "qc-zoom-reset",
                   "qc-zoom-badge", "'wheel'", "setAttribute('viewBox'"):
        assert needed in out, f"missing {needed}"
    # The wheel must not scroll the page as well.
    assert "e.preventDefault();" in out and "passive: false" in out
    # A drag must not also open an mFoV, or panning sprays popups.
    assert "moved > 3" in out
    # A bad number in the viewBox blanks the image until reload, so both
    # the point lookup and the zoom guard against it.
    assert "r.width > 0 && r.height > 0" in out
    assert "cur.every(isFinite)" in out
    # Zooming out past the whole image, or off it, is clamped.
    assert "cur[2] > home[2]" in out


def test_middle_click_mfov_viewer_uses_a_shared_beam_table(
    tmp_path: Path,
) -> None:
    """One offset table for the section, not 36,000 absolute coordinates:
    the page is already large, and the 91 beams sit identically in every
    mFoV."""
    import json as _json
    import re as _re

    from standalone_qc import dashboard

    cfg, score = _score_one(tmp_path)
    page = ('<section class="panel overview"><label>'
            '<input id="toggle-overview-quality" checked>'
            'Show focus-quality overlay</label>'
            '<svg viewBox="0 0 100 200"><a data-tile="1">'
            '<rect class="qtile" fill="#111"/></a>'
            '<text class="qlabel">5.0</text></svg></section>')
    out = dashboard._inject_overview_tools(page, score, cfg)

    m = _re.search(
        r'<script id="qc-beam-layout" type="application/json">(.*?)</script>',
        out, _re.S)
    if m:
        payload = _json.loads(m.group(1))
        assert payload["beam_w"] > 0 and payload["beam_h"] > 0
        assert payload["offsets"], "no beam offsets"
        # Compact: a table, not a coordinate per beam per mFoV.
        assert len(m.group(1)) < 20000, "beam table is too big to ship"

    assert "auxclick" in out and "e.button !== 1" in out
    # Middle mousedown must be swallowed, or the browser starts autoscroll.
    assert "e.button === 1" in out
    # Beams are placed at their true positions, and lead to full resolution.
    assert "window.__qcOpenBeam" in out
    assert "img.bm" in out


def test_tile_is_called_mfov_everywhere_an_operator_reads_it(
    tmp_path: Path,
) -> None:
    """"Tile" is the acquisition code's word; mFoV is the operator's, and
    the rest of these reports already say mFoV. The remaining uses come
    from live_qc_support, so they are corrected on the generated HTML.

    The risk is a blanket replace: data-tile, tile-bmps-payload, .qtile,
    tileIdx and tile_data are identifiers, and rewriting them breaks the
    page silently.
    """
    from standalone_qc import dashboard

    cfg, score = _score_one(tmp_path)
    page = (
        "<h3>x</h3>"
        "<small>Cmd/Ctrl+click opens the tile folder. Shift+click copies.</small>"
        "'<title>Tile ' + tileIdx + ' \u2014 inverted preview</title>'"
        "'<h1>Tile ' + tileIdx + ' \u2014 ' + urls.length + ' BMP(s)</h1>'"
        '<rect class="qtile"><title>tile 7 xy=(1,2)</title></rect>'
        '<a data-tile="7">x</a>'
        '<script id="tile-bmps-payload" type="application/json">{}</script>'
    )
    out = dashboard._relabel_tiles(page, score)

    # Prose is corrected...
    assert "'<title>mFoV ' + tileIdx" in out
    assert "'<h1>mFoV ' + tileIdx" in out
    assert "<title>mFoV 7 " in out
    assert "opens the mFoV folder" in out
    assert "<title>tile " not in out
    assert "Tile ' + tileIdx" not in out

    # ...and identifiers are untouched.
    for ident in ('data-tile="7"', "tile-bmps-payload", 'class="qtile"',
                  "tileIdx"):
        assert ident in out, f"rewrote the identifier {ident}"


# A third console.log shape: a per-point header with NO tag on the step lines
# at all, as the offset-focus-point strategy writes it. AF and AS carry real
# durations here; B2F and the stage move do not.
_FSP_UNTAGGED = """\
[Offset FocusPoint 0] Stage -> (B2F) -> AF -> AS (no minimize-OL)
[B2F] attempt 1/2 rc=0 hexfit=0.0 (ok)
AF: fine=0.0000, coarse=None, success=True  (6.18s)
AS: rc=0, confX=0.2369, confY=0.1699, ok=True  (4.08s)
[Offset FocusPoint 1] Stage -> (B2F) -> AF -> AS (no minimize-OL)
[B2F] attempt 1/2 rc=0 hexfit=0.0 (ok)
AF: fine=0.0000, coarse=None, success=True  (5.02s)
AS: rc=0, confX=0.2411, confY=0.1702, ok=True  (3.91s)
[Timing] Region 'S18R5' breakdown (399 tile(s), 942.7s total):
  focus/stigmation  :    140.9s ( 14.9%)
  stage move+settle :    168.0s ( 17.8%)
  beam scan         :    598.9s ( 63.5%)
  storage overhead  :      1.1s (  0.1%)
"""


def test_untagged_focus_point_steps_are_timed(tmp_path: Path) -> None:
    """The offset-focus-point format puts no [Tag] on its AF/AS lines. Before
    the parser allowed that, every one of these sections charted as "no
    per-FSP timings" even though each point had a real AF and AS time."""
    from standalone_qc import fsp_timing

    log = tmp_path / "console.log"
    log.write_text(_FSP_UNTAGGED, encoding="utf-8")
    recs = fsp_timing.parse_console_log(log)

    assert len(recs) == 2, "one record per [Offset FocusPoint] header"
    assert [r["focus_s"] for r in recs] == [6.18, 5.02]
    assert [r["stig_s"] for r in recs] == [4.08, 3.91]
    # The B2F line in this format carries no duration, so it stays unknown
    # rather than becoming a zero-height bar.
    assert all(r["b2f_s"] is None and r["move_s"] is None for r in recs)


def test_region_steps_cover_af_as_b2f_and_stage_move(tmp_path: Path) -> None:
    """The operator's question is "where did this region's time go", and the
    per-FSP chart cannot answer it: its stage-move figure is only the moves to
    the support points, while the region also moves between all 399 mFoVs."""
    from standalone_qc import fsp_timing, section_facts

    log = tmp_path / "console.log"
    log.write_text(_FSP_UNTAGGED, encoding="utf-8")
    facts = {
        "aggregate": section_facts.parse_console_log(log)["aggregate"],
        "fsp_timings": fsp_timing.parse_console_log(log),
    }
    steps = {s["key"]: s for s in fsp_timing.region_steps(facts)}
    assert set(steps) == {"af", "as", "b2f", "move"}

    # AF and AS summed from the points; stage move straight off the breakdown.
    assert steps["af"]["seconds"] == pytest.approx(11.20)
    assert steps["as"]["seconds"] == pytest.approx(7.99)
    assert steps["move"]["seconds"] == 168.0 and not steps["move"]["derived"]
    # B2F is the focus/stigmation leftover here, and must say so.
    assert steps["b2f"]["derived"] is True
    assert steps["b2f"]["seconds"] == pytest.approx(140.9 - 11.20 - 7.99)

    html = fsp_timing.region_bars_html(facts, "S18R5")
    assert "<svg" in html and "Time (s)" in html
    for label in ("focus (AF)", "stig (AS)", "B2F", "stage move+settle"):
        assert label in html
    assert "derived, not timed directly" in html
    # Beam scan dwarfs every charted step; naming it keeps the chart from
    # reading as the region's whole time budget.
    assert "beam scan 598.9s" in html


def test_region_b2f_prefers_a_measured_time_to_the_leftover(
    tmp_path: Path,
) -> None:
    """The leftover is the bigger number, which makes preferring the measured
    one look like throwing information away. It is not: the extra seconds are
    overhead the log never attributed to B2F, and they are reported on their
    own line instead of inflating the alignment's cost."""
    from standalone_qc import fsp_timing, section_facts

    log = tmp_path / "console.log"
    log.write_text(
        _FSP_NEW + "[Timing] Region 'S1R1' breakdown (4 tile(s), 90.0s total):"
        "\n  focus/stigmation  :     40.3s ( 44.8%)"
        "\n  stage move+settle :      3.0s (  3.3%)\n",
        encoding="utf-8",
    )
    facts = {
        "aggregate": section_facts.parse_console_log(log)["aggregate"],
        "fsp_timings": fsp_timing.parse_console_log(log),
    }
    steps = {s["key"]: s for s in fsp_timing.region_steps(facts)}
    assert steps["b2f"]["seconds"] == 26.18, "the measured B2F, not 40.3-AF-AS"
    rest = fsp_timing.region_unattributed_s(facts)
    assert rest == pytest.approx(40.3 - 2.63 - 4.88 - 26.18)
    assert "not attributed to any focus step" in fsp_timing.region_bars_html(
        facts, "S1R1"
    )


def test_region_bars_absent_when_the_log_has_no_breakdown(
    tmp_path: Path,
) -> None:
    from standalone_qc import fsp_timing

    log = tmp_path / "console.log"
    log.write_text(_FSP_OLD, encoding="utf-8")
    facts = {"aggregate": {}, "fsp_timings": fsp_timing.parse_console_log(log)}
    # Only AS is known and there is no region total. A single bar labelled
    # with the region's name would claim to be the region's time budget when
    # it is one support point's autostig, so no region chart is drawn and the
    # per-FSP chart carries the number on its own.
    assert fsp_timing.region_bars_html(facts, "S1R1") == ""
    assert [s["key"] for s in fsp_timing.region_steps(facts)] == ["as"]


from standalone_qc import fsp_timing  # noqa: E402

_LQ_TIMING = """{"focus_s": 30.1, "focus_af_s": 0.0, "focus_as_s": 0.0,
 "focus_move_s": 0.0, "wall_s": 800.7, "move_s": 166.7, "scan_s": 598.0,
 "storage_s": 0.95, "n_tiles": 399, "n_tiles_z_glitch": 0,
 "qc_catchup_worker_s": 0.0, "qc_catchup_viewer_s": 0.0, "qc_catchup_s": 0.0}"""


def _section_with_timing(tmp_path: Path) -> Path:
    d = tmp_path / "S12R5"
    (d / "LiveQC").mkdir(parents=True)
    (d / "LiveQC" / "live_qc_timing.json").write_text(_LQ_TIMING,
                                                      encoding="utf-8")
    (d / "console.log").write_text(
        _FSP_UNTAGGED.replace("'S18R5'", "'S12R5'").replace(
            "(399 tile(s), 942.7s total)", "(399 tile(s), 849.1s total)")
        .replace("140.9s ( 14.9%)", " 30.1s (  3.5%)")
        .replace("168.0s ( 17.8%)", "166.7s ( 19.6%)")
        .replace("598.9s ( 63.5%)", "598.0s ( 70.4%)")
        .replace("1.1s (  0.1%)", "0.9s (  0.1%)"),
        encoding="utf-8")
    return d


def _facts_for(d: Path) -> dict:
    from standalone_qc import section_facts
    log = section_facts.parse_console_log(d / "console.log")
    return {
        "aggregate": log["aggregate"],
        "duration_s": log.get("duration_s"),
        "fsp_timings": fsp_timing.parse_console_log(d / "console.log"),
        "live_qc_timing": fsp_timing.read_live_qc_timing(d),
    }


def test_live_qc_timing_is_read_and_is_optional(tmp_path: Path) -> None:
    from standalone_qc import fsp_timing

    d = _section_with_timing(tmp_path)
    t = fsp_timing.read_live_qc_timing(d)
    assert t["wall_s"] == 800.7 and t["n_tiles"] == 399.0

    # Sections acquired before the file existed must still chart.
    assert fsp_timing.read_live_qc_timing(tmp_path / "nope") == {}
    (d / "LiveQC" / "live_qc_timing.json").write_text("{not json",
                                                      encoding="utf-8")
    assert fsp_timing.read_live_qc_timing(d) == {}


def test_beam_scan_is_not_called_unattributed(tmp_path: Path) -> None:
    """The remainder must be computed against EVERY known cost, not just the
    charted ones.

    Subtracting only the four charted steps from the region total put the
    598s beam scan - the thing the microscope is supposed to be doing - into
    a bucket labelled "unattributed", turning an ordinary region into an
    apparent 650s mystery.
    """
    ov = fsp_timing.region_overview(_facts_for(_section_with_timing(tmp_path)))

    assert ov["total_s"] == 849.1
    # 598.0 scan + 0.9 storage, from the console log: where both sources
    # carry a value the log wins, so one rounding is used throughout rather
    # than two mixed together.
    assert ov["scan_storage_s"] == pytest.approx(598.9)
    assert ov["unattributed_s"] == pytest.approx(
        849.1 - ov["named_s"] - 598.9)
    assert 20.0 < ov["unattributed_s"] < 80.0, \
        "a real region's slack is tens of seconds, not hundreds"
    assert ov["s_per_mfov"] == pytest.approx(849.1 / 399)


def test_wall_clock_is_not_presented_as_the_region_total(
    tmp_path: Path,
) -> None:
    """wall_s covers the tile loop and excludes focus/stigmation, so on some
    sections the steps sum to MORE than it. Printed as "the region took
    801s" next to bars totalling 897s it reads as a bug in the chart."""
    facts = _facts_for(_section_with_timing(tmp_path))
    html = fsp_timing.region_bars_html(facts, "S12R5")

    assert "849s</strong> for the whole region" in html
    assert "Tile loop (LiveQC wall clock) 801s" in html
    assert "not the focus/stigmation pass" in html
    assert "399 mFoV at 2.13s each" in html
    # and the four charted bars are reconciled against the total in words
    assert "beam scan and storage" in html and "unattributed" in html


def test_zero_focus_split_fields_are_not_read_as_measurements(
    tmp_path: Path,
) -> None:
    """focus_af_s/focus_as_s/focus_move_s exist in the schema and are 0.0 in
    all 43 files on the share. A real autofocus cannot take 0.000s, so a
    zero there means "not filled in" and must not replace the value derived
    from the per-point lines."""
    facts = _facts_for(_section_with_timing(tmp_path))
    steps = {s["key"]: s for s in fsp_timing.region_steps(facts)}
    assert steps["af"]["seconds"] > 0.0, "the derived AF time must survive"

    # When the acquisition side does start filling them in, they win.
    facts["live_qc_timing"] = dict(facts["live_qc_timing"],
                                   focus_af_s=7.5, focus_as_s=3.25)
    steps = {s["key"]: s for s in fsp_timing.region_steps(facts)}
    assert steps["af"]["seconds"] == 7.5 and not steps["af"]["derived"]
    assert steps["as"]["seconds"] == 3.25


# focus_map's itemised focus phase, for runs after 2026-10-02. Documented
# shape: focus_move_s = focus_b2f_s + focus_stage_s, focus_minimize_s is a
# PART of focus_af_s, and what is left of focus_s after af/as/move is
# unitemized setup.
_LQ_ITEMISED = {
    "focus_s": 48.0, "focus_af_s": 18.0, "focus_minimize_s": 6.5,
    "focus_as_s": 4.0, "focus_b2f_s": 9.0, "focus_stage_s": 12.0,
    "focus_move_s": 21.0, "move_s": 166.7, "scan_s": 598.0, "storage_s": 0.9,
    "wall_s": 800.7, "n_tiles": 399, "n_tiles_z_glitch": 0,
    "qc_catchup_s": 0.0,
}


def _itemised_facts(**over) -> dict:
    lq = dict(_LQ_ITEMISED, **over)
    return {
        "aggregate": {"focus_stig_s": lq["focus_s"], "move_s": lq["move_s"],
                      "scan_s": lq["scan_s"], "storage_s": lq["storage_s"]},
        "duration_s": 849.1, "fsp_timings": [], "live_qc_timing": lq,
    }


def test_itemised_focus_phase_is_used_verbatim() -> None:
    steps = {s["key"]: s for s in fsp_timing.region_steps(_itemised_facts())}

    assert set(steps) == {"af", "as", "b2f", "fstage", "setup", "move"}
    assert steps["af"]["seconds"] == 18.0
    assert steps["b2f"]["seconds"] == 9.0
    assert steps["fstage"]["seconds"] == 12.0
    # Nothing here is inferred any more except the setup leftover.
    for k in ("af", "as", "b2f", "fstage", "move"):
        assert steps[k]["derived"] is False, k
    assert steps["setup"]["derived"] is True

    # The focus-phase parts must add back up to focus_s exactly.
    parts = sum(steps[k]["seconds"]
                for k in ("af", "as", "b2f", "fstage", "setup"))
    assert parts == pytest.approx(48.0)
    assert "focus_split_gap_s" not in fsp_timing.region_overview(
        _itemised_facts()
    )


def test_focus_move_is_never_charted_beside_its_own_parts() -> None:
    """focus_move_s = focus_b2f_s + focus_stage_s. Charting it as well as
    the two would count those 21s twice, and the bars would overrun focus_s
    by nearly half."""
    steps = fsp_timing.region_steps(_itemised_facts())
    assert sum(s["seconds"] for s in steps if s["key"] != "move") \
        == pytest.approx(48.0)
    assert not any(s["seconds"] == 21.0 for s in steps)

    # With only the sum recorded, it stands in for both - once.
    lq = dict(_LQ_ITEMISED)
    del lq["focus_b2f_s"], lq["focus_stage_s"]
    facts = _itemised_facts()
    facts["live_qc_timing"] = lq
    steps = {s["key"]: s for s in fsp_timing.region_steps(facts)}
    assert "fstage" not in steps
    assert steps["b2f"]["seconds"] == 21.0
    assert steps["b2f"]["label"] == "B2F + focus stage"


def test_minimize_ol_is_a_note_not_a_bar() -> None:
    """focus_minimize_s is a component OF focus_af_s. A sixth bar for it
    would be double counting, and it is non-zero only in a batch's first
    region, so a missing bar elsewhere would look like missing data."""
    steps = {s["key"]: s for s in fsp_timing.region_steps(_itemised_facts())}
    assert "minimize" not in steps
    assert "6.5s of it is minimize-OL" in steps["af"]["note"]
    assert "first region" in steps["af"]["note"]

    # Later regions: the key is zero, so the breakdown sentence drops. The
    # label still says AF includes minimize-OL, because focus_af_s always
    # does - there was simply none to do.
    steps = {s["key"]: s
             for s in fsp_timing.region_steps(_itemised_facts(
                 focus_minimize_s=0.0))}
    assert "of it is minimize-OL" not in steps["af"]["note"]
    assert "autofocus plus minimize-OL" in steps["af"]["note"]


def test_old_runs_fall_back_rather_than_charting_zeros(tmp_path: Path) -> None:
    """In pre-2026-10-02 runs the old focus fields are 0.0 meaning "not
    recorded". Reading those as measurements would chart a region whose AF,
    AS and B2F all took no time."""
    facts = _facts_for(_section_with_timing(tmp_path))
    assert facts["live_qc_timing"]["focus_af_s"] == 0.0
    steps = {s["key"]: s for s in fsp_timing.region_steps(facts)}

    assert "setup" not in steps, "no itemised path without real numbers"
    assert steps["af"]["seconds"] > 0.0 and steps["af"]["derived"], \
        "AF comes from the per-point log lines instead"


def test_the_two_kinds_of_stage_movement_stay_apart() -> None:
    """focus_stage_s is stage travel during the focus phase; move_s is the
    move to each of 399 mFoVs. They differ by more than 10x here, and one
    'stage' bar would be meaningless."""
    steps = {s["key"]: s for s in fsp_timing.region_steps(_itemised_facts())}
    assert steps["fstage"]["seconds"] == 12.0
    assert steps["move"]["seconds"] == 166.7
    assert "tiles" in steps["move"]["label"]
    assert "focus" in steps["fstage"]["label"]
    assert "telemetry glitch" in steps["fstage"]["note"]


def test_not_verified_is_red_until_a_choice_is_made() -> None:
    """The three review states must be visually distinct AND the unreviewed
    one must be the one that catches the eye, so a part-reviewed batch can
    be scanned rather than read.

    The styling is keyed on .active, which the review script puts on exactly
    one button per region (defaulting to not_verified at load), so nothing
    extra is needed to clear the red once Accept or Retake is clicked.
    """
    import re as _re
    from standalone_qc import rollup as _r

    def style(status: str) -> str:
        m = _re.search(
            r'\.rev-btn\.active\[data-status="' + status + r'"\]\s*\{([^}]*)\}',
            _r._CSS)
        assert m, f"no active style for {status}"
        return m.group(1)

    nv, acc, ret = (style(s) for s in
                    ("not_verified", "accepted", "retake"))
    assert "#c0392b" in nv and "color:#fff" in nv
    # Distinct from both decided states, or it defeats the purpose.
    assert "#c0392b" not in acc and "#c0392b" not in ret
    # The inactive base style must NOT be red, or every row shows three
    # red buttons and the signal is gone.
    base = _re.search(r'\.rev-btn\s*\{([^}]*)\}', _r._CSS).group(1)
    assert "#c0392b" not in base
