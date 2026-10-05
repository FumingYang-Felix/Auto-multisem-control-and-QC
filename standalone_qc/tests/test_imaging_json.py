"""Finding the imaging JSON a batch came from, and getting it into the page.

Why this matters: the dashboard's Retake selection panel builds a partial
retake by cloning the original region's XML. If the JSON never reaches the
page, `original_region_xml` is null and the panel throws the moment someone
finishes lassoing mFoVs - which is exactly how it shipped before.

    pytest standalone_qc/tests/test_imaging_json.py
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import pytest  # noqa: E402

from standalone_qc import imaging_json  # noqa: E402


def _region(name: str) -> str:
    return (f'<Region IsShadingEnabled="false" Name="{name}" Z="38859">'
            f'<CenterPosition>1.0,2.0</CenterPosition>'
            f'<ContourSize>10.0,10.0</ContourSize>'
            f'<Contour Type="Polygon"><Points>0,0 1,0 1,1</Points></Contour>'
            f'</Region>')


def _write_json(path: Path, names) -> Path:
    path.write_text(
        json.dumps({"_id": "x", "Name": path.stem,
                    "Regions": [_region(n) for n in names]}),
        encoding="utf-8",
    )
    return path


def _batch(root: Path, names, batch_name="w1h1_dye_R3_8NM_20260101_000000"):
    b = root / "run" / batch_name
    b.mkdir(parents=True)
    (b / "region_manifest.json").write_text(
        json.dumps({"regions": [
            {"idx": i, "name": n, "folder": n, "status": "done"}
            for i, n in enumerate(names, 1)
        ]}),
        encoding="utf-8",
    )
    return b


def test_matches_on_region_names_not_on_the_filename(tmp_path: Path) -> None:
    """The real trap. On w10h31 the batch called w10h31_dye_R3_8NM_... was
    acquired from w10h31_dye_border.json; w10h31_dye_R3.json - the obvious
    name-based guess - shares ZERO regions with it. Picking by name gives a
    file whose geometry belongs to different sections, and the mistake only
    shows up later as a retake region in the wrong place.
    """
    batch = _batch(tmp_path, ["S1R2", "S2R2", "S3R2"],
                   batch_name="w1h1_dye_R3_8NM_20260101_000000")
    # Named to look right, contents unrelated.
    _write_json(tmp_path / "w1h1_dye_R3.json", ["S9R9", "S8R8"])
    # Named unhelpfully, actually the source.
    right = _write_json(tmp_path / "w1h1_dye_border.json",
                        ["S1R2", "S2R2", "S3R2", "S4R2"])

    found, info = imaging_json.find_for_batch(batch, ["S1R2", "S2R2", "S3R2"])
    assert found == right, f"picked {found}, info={info}"
    assert info["source"] == "auto"
    assert info["matched"] == "3/3"


def test_nearest_candidate_wins_a_tie(tmp_path: Path) -> None:
    """A retake JSON written into the batch folder should beat an equally
    good one further up."""
    names = ["S1R2", "S2R2"]
    batch = _batch(tmp_path, names)
    _write_json(tmp_path / "far.json", names)
    near = _write_json(batch / "near.json", names)

    found, _ = imaging_json.find_for_batch(batch, names)
    assert found == near


def test_no_match_explains_itself(tmp_path: Path) -> None:
    batch = _batch(tmp_path, ["S1R2"])
    _write_json(tmp_path / "other.json", ["QQQ"])

    found, info = imaging_json.find_for_batch(batch, ["S1R2"])
    assert found is None
    assert "none of this batch" in info["note"] or "lists any" in info["note"]
    assert info["searched"] >= 1


def test_manifest_is_not_mistaken_for_an_imaging_json(tmp_path: Path) -> None:
    """region_manifest.json sits in every batch and has no Regions list;
    reading it per section would be pure waste."""
    batch = _batch(tmp_path, ["S1R2"])
    cands = imaging_json._candidates(batch)
    assert all(c.name != "region_manifest.json" for c in cands)


def test_explicit_path_wins_but_is_still_checked(tmp_path: Path) -> None:
    batch = _batch(tmp_path, ["S1R2"])
    good = _write_json(tmp_path / "good.json", ["S1R2"])
    wrong = _write_json(tmp_path / "wrong.json", ["ZZZ"])

    found, info = imaging_json.find_for_batch(
        batch, ["S1R2"], {"imaging_json_path": str(good)})
    assert found == good and info["source"] == "configured"
    assert not info.get("note")

    # A pinned file that matches nothing is still used - the operator asked
    # for it - but says so rather than silently producing bad geometry.
    found, info = imaging_json.find_for_batch(
        batch, ["S1R2"], {"imaging_json_path": str(wrong)})
    assert found == wrong
    assert "none of which is in this batch" in info["note"]

    found, info = imaging_json.find_for_batch(
        batch, ["S1R2"], {"imaging_json_path": str(tmp_path / "nope.json")})
    assert found is None and "does not exist" in info["note"]


def test_search_can_be_turned_off(tmp_path: Path) -> None:
    batch = _batch(tmp_path, ["S1R2"])
    _write_json(tmp_path / "good.json", ["S1R2"])
    found, info = imaging_json.find_for_batch(
        batch, ["S1R2"], {"imaging_json_search_enabled": False})
    assert found is None and "disabled" in info["note"]


def test_region_xml_roundtrip(tmp_path: Path) -> None:
    p = _write_json(tmp_path / "w.json", ["S1R2", "S2R2"])
    xml = imaging_json.region_xml(p, "S1R2")
    assert xml and 'Name="S1R2"' in xml
    assert imaging_json.region_xml(p, "S9R9") is None
    # Matched the same way live_qc_support matches it, or the page would
    # find nothing even though we said we had it.
    assert f'Name="S1R2"' in xml


def test_unreadable_candidates_are_skipped_not_fatal(tmp_path: Path) -> None:
    batch = _batch(tmp_path, ["S1R2"])
    (tmp_path / "broken.json").write_text("{not json", encoding="utf-8")
    (tmp_path / "notaregionfile.json").write_text('{"foo": 1}', encoding="utf-8")
    good = _write_json(tmp_path / "good.json", ["S1R2"])
    found, _ = imaging_json.find_for_batch(batch, ["S1R2"])
    assert found == good


def test_dashboard_embeds_the_region_xml(tmp_path: Path) -> None:
    """End to end: the payload the browser reads must carry the XML.

    This is the assertion that would have caught the original defect - the
    page looked complete, but imaging_json_path and original_region_xml were
    both null in it.
    """
    from standalone_qc.config import load_config
    from standalone_qc.dashboard import (
        DASHBOARD_AVAILABLE, write_section_dashboard,
    )
    from standalone_qc.discover import find_scorable_regions
    from standalone_qc.score import score_region
    from standalone_qc.tests.test_standalone_qc import DSAMP, build_fixture

    if not DASHBOARD_AVAILABLE:
        pytest.skip("live_qc_support._write_dashboard_html unavailable")

    batch = build_fixture(tmp_path)
    region = find_scorable_regions(tmp_path)[0]
    # Name it unhelpfully, so only content matching can find it.
    _write_json(tmp_path / "unrelated_name.json", [region.name])

    cfg = load_config(None, dsamp=DSAMP, max_workers=4,
                      region_overview_enabled=False)
    score = score_region(region, cfg)
    out = write_section_dashboard(score, cfg)
    assert out and out.is_file()

    page = out.read_text(encoding="utf-8")
    m = re.search(
        r'<script id="retake-payload" type="application/json">(.*?)</script>',
        page, re.S)
    assert m, "no retake payload in the dashboard"
    payload = json.loads(m.group(1))
    assert payload["imaging_json_path"], "imaging JSON path not passed through"
    xml = payload["original_region_xml"]
    assert xml, "retake panel would throw: no original region XML"
    assert f'Name="{region.name}"' in xml


def test_no_match_names_what_the_candidates_held(tmp_path: Path) -> None:
    """"Not found" on its own sends the operator looking for a bug.

    On the run that prompted this the batch's regions were all S*R5 and every
    JSON on the share held S*R3 - nothing was broken, the R5 file simply was
    not there. Showing both sides of the mismatch is what turns the message
    into an instruction.
    """
    batch = _batch(tmp_path, ["S10R5", "S11R5", "S12R5", "S13R5"])
    _write_json(tmp_path / "w10h31_dye_R3.json", ["S10R3", "S11R3", "S12R3"])

    found, info = imaging_json.find_for_batch(
        batch, ["S10R5", "S11R5", "S12R5", "S13R5"]
    )
    assert found is None
    note = info["note"]
    assert "S10R5" in note, "say which regions were wanted"
    assert "w10h31_dye_R3.json" in note and "S10R3" in note, \
        "say what the files that WERE found contain"
    assert [c["file"] for c in info["candidates"]] == ["w10h31_dye_R3.json"]


def test_dashboard_warns_in_the_panel_when_the_xml_is_missing(
    tmp_path: Path,
) -> None:
    """The panel's own error fires at autosave, i.e. after a few hundred
    mFoVs have been lassoed and with no way to save them. The same condition
    is known before the page is written, so it is stated at the top of the
    panel instead."""
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
    # A JSON that exists but is for another series - the real failure mode.
    _write_json(tmp_path / "wrong_series.json", ["ZZ9R9"])

    cfg = load_config(None, dsamp=DSAMP, max_workers=4,
                      region_overview_enabled=False)
    score = score_region(region, cfg)
    out = write_section_dashboard(score, cfg)
    assert out and out.is_file()
    page = out.read_text(encoding="utf-8")

    assert "Retakes cannot be saved for this section" in page
    assert "Imaging JSON" in page, "the banner must name the setting to change"
    assert "ZZ9R9" in page, "and show what the candidate actually held"
    # Immediately after the panel heading, not buried at the end of the page.
    h3 = page.find("Retake selection</h3>")
    assert 0 < h3 < page.find("Retakes cannot be saved") < h3 + 1200
    assert any("no imaging JSON found" in n for n in score.notes)


def test_no_banner_when_the_xml_is_present(tmp_path: Path) -> None:
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
    _write_json(tmp_path / "right.json", [region.name])
    cfg = load_config(None, dsamp=DSAMP, max_workers=4,
                      region_overview_enabled=False)
    out = write_section_dashboard(score_region(region, cfg), cfg)
    assert out and "Retakes cannot be saved" not in out.read_text(
        encoding="utf-8"
    )
