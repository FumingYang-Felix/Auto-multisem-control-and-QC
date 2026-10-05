"""Self-contained regression tests for standalone_qc.

Builds a miniature acquisition output tree in a temp dir - no microscope, no
V:\\ share, no real run needed - and exercises discovery, both layout sources,
the tissue probe, tile selection, outlier-beam detection, reporting and the
ledger.

The fixture deliberately mimics the acquisition side's LIGHTWEIGHT mode:
live_qc_results.csv carries real layout columns and NaN/zero placeholders
everywhere else (see ../../../STANDALONE_QC_HANDOFF.md section 3). If a
future acquisition-side change makes those columns real again, these tests
still pass - we never read them.

Run either way:
    pytest standalone_qc/tests/test_standalone_qc.py
    python standalone_qc/tests/test_standalone_qc.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
from PIL import Image

# Import the package by its real name whether run under pytest from the repo
# root or directly as a script from anywhere.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from standalone_qc import ledger  # noqa: E402
from standalone_qc.config import load_config, policy_fingerprint  # noqa: E402
from standalone_qc.discover import (  # noqa: E402
    REGION_COMPLETE_NAME,
    find_scorable_regions,
    list_regions,
    parse_sample_id,
    read_region_complete,
    read_region_manifest,
    resolve_image_dir,
    verify_region_files,
)
from standalone_qc.layout import read_region_layout  # noqa: E402
from standalone_qc.report import write_region_report  # noqa: E402
from standalone_qc.score import score_region, select_tiles  # noqa: E402

BATCH = "w9h9_test_8NM_20260907_120000"
N_BEAMS = 6
BAD_BEAM = 5
W, H = 80, 70
DSAMP = 48

CSV_HEADER = (
    "tile_idx,x_um,y_um,quality,ol_pred,stig_x_pred,stig_y_pred,stig_x_med,"
    "stig_y_med,stig_anisotropy,pct_saturation_med,pct_dark_med,pct_bright_med,"
    "std_all_med,jitter_score_med,mean_intensity_med,is_blank_outside_roi,"
    "is_jittery,n_images_used,selected_beams,status,status_category,"
    "failed_images,tile_folder"
)


def _boxcar(a: np.ndarray, k: int = 4) -> np.ndarray:
    pad = np.pad(a, k, mode="edge")
    cs = np.cumsum(np.cumsum(pad, axis=0), axis=1)
    win = 2 * k + 1
    out = (cs[win:, win:] - cs[:-win, win:] - cs[win:, :-win] + cs[:-win, :-win])
    return (out / (win * win))[: a.shape[0], : a.shape[1]]


def _tile_image(rng: np.random.Generator, tissue: bool, beam: int) -> np.ndarray:
    if not tissue:
        # Flat: std_all falls under outside_roi_std_threshold -> blank.
        return np.clip(rng.normal(130, 0.3, (H, W)), 0, 255).astype(np.uint8)
    a = rng.normal(128, 38, (H, W))
    if beam == BAD_BEAM:
        a = _boxcar(a)  # stands in for a defocused beam
    return np.clip(a, 0, 255).astype(np.uint8)


def build_fixture(root: Path, n_tiles: int = 7, tissue: set | None = None) -> Path:
    """A batch folder with one 'done' region plus failed/pending siblings."""
    tissue = {2, 3, 4, 5} if tissue is None else tissue
    rng = np.random.default_rng(11)
    batch = root / BATCH
    region = batch / "S1R1"
    rows = [CSV_HEADER]

    for t in range(1, n_tiles + 1):
        d = region / f"mfov{t}"
        d.mkdir(parents=True, exist_ok=True)
        coords = []
        for beam in range(1, N_BEAMS + 1):
            name = f"mfov{t}_178500000{t:02d}__{beam:03d}.bmp"
            Image.fromarray(_tile_image(rng, t in tissue, beam), mode="L").save(d / name)
            coords.append(f"{d / name}\t{t * 100 + beam * 9}\t{t * 40 + beam * 7}\t0")
        (d / "image_coordinates.txt").write_text("\n".join(coords) + "\n", encoding="utf-8")
        (d / "metadata.txt").write_text(
            f"Stage pos. X: {-27000.0 + t * 120.0} um\n"
            f"Stage pos. Y: {-16800.0 + t * 60.0} um\n"
            "Stage pos. Z: 12345.6 um\n"
            f"Lens: {4.45 + t * 0.001}\n"
            "Stigmator Tilt X: 0.1875\nStigmator Tilt Y: 0.3605\n",
            encoding="utf-8",
        )
        rows.append(
            f"{t},{-27000.0 + t * 120.0},{-16800.0 + t * 60.0},nan,"
            f"{4.45 + t * 0.001},0.1875,0.3605,0.0,0.0,0.0,0.0,0.0,0.0,0.0,"
            f"0.0,0.0,0,0,{N_BEAMS},[],ok,ok,0,{d}"
        )

    lq = region / "LiveQC"
    lq.mkdir(parents=True, exist_ok=True)
    (lq / "live_qc_results.csv").write_text("\n".join(rows) + "\n", encoding="utf-8")

    (batch / "region_manifest.json").write_text(
        json.dumps({
            "generated_at": "2026-09-07 12:00:00",
            "regions": [
                {"idx": 1, "name": "S1R1", "folder": "S1R1", "status": "done"},
                {"idx": 2, "name": "S2R1", "folder": "S2R1", "status": "failed",
                 "detail": "planted failure"},
                {"idx": 3, "name": "S3R1", "folder": "S3R1", "status": "pending"},
            ],
        }, indent=2),
        encoding="utf-8",
    )
    return batch


def _cfg(**kw):
    base = {"dsamp": DSAMP, "max_workers": 4}
    base.update(kw)
    return load_config(None, **base)


# ---------------------------------------------------------------------------
def test_discovery_only_returns_finished_regions(tmp_path: Path) -> None:
    build_fixture(tmp_path)
    assert len(list_regions(tmp_path)) == 3
    scorable = find_scorable_regions(tmp_path)
    # "failed" and "pending" must never be scored, and "failed" has no folder
    # on disk anyway - both reasons are load-bearing.
    assert [r.name for r in scorable] == ["S1R1"]
    assert scorable[0].key == f"{BATCH}/S1R1"


def _write_trigger(region_dir: Path, status: str, name: str = "S1R1",
                   detail=None, when: str = "2026-09-07 12:07:30") -> None:
    """The acquisition side's per-region marker, exact schema from
    ../../ACQUISITION_REPLY.md."""
    region_dir.mkdir(parents=True, exist_ok=True)
    (region_dir / REGION_COMPLETE_NAME).write_text(
        json.dumps({"name": name, "status": status, "detail": detail,
                    "completed_at": when}),
        encoding="utf-8",
    )


def test_falls_back_to_manifest_when_no_trigger_file(tmp_path: Path) -> None:
    # Every region imaged before 2026-09-07 - i.e. all historical data on the
    # share - has only the batch manifest. That path must keep working.
    build_fixture(tmp_path)
    regions = list_regions(tmp_path)
    assert all(r.status_source == "manifest" for r in regions)
    assert [r.name for r in find_scorable_regions(tmp_path)] == ["S1R1"]


def test_region_complete_trigger_is_preferred(tmp_path: Path) -> None:
    batch = build_fixture(tmp_path)
    _write_trigger(batch / "S1R1", "done")

    s1 = next(r for r in list_regions(tmp_path) if r.name == "S1R1")
    assert s1.status_source == "region_complete"
    assert s1.status == "done"
    assert s1.completed_at == "2026-09-07 12:07:30"
    assert [r.name for r in find_scorable_regions(tmp_path)] == ["S1R1"]


def test_trigger_file_overrides_a_lagging_manifest(tmp_path: Path) -> None:
    """The marker is written immediately BEFORE the manifest, so a region can
    legitimately be complete while the shared file still says "running".
    Trusting the per-region file is the whole reason to prefer it."""
    batch = build_fixture(tmp_path)
    manifest = json.loads((batch / "region_manifest.json").read_text(encoding="utf-8"))
    manifest["regions"][0]["status"] = "running"
    (batch / "region_manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

    assert find_scorable_regions(tmp_path) == []  # manifest alone says not yet
    _write_trigger(batch / "S1R1", "done")
    assert [r.name for r in find_scorable_regions(tmp_path)] == ["S1R1"]


def test_trigger_file_can_also_withhold_a_region(tmp_path: Path) -> None:
    # Skipped/failed regions get the marker too, carrying that status. It must
    # be able to say "don't score this" as well as "do".
    batch = build_fixture(tmp_path)
    _write_trigger(batch / "S1R1", "skipped", detail="unsafe stage Z")
    s1 = next(r for r in list_regions(tmp_path) if r.name == "S1R1")
    assert s1.status == "skipped" and s1.detail == "unsafe stage Z"
    assert find_scorable_regions(tmp_path) == []


def test_region_missing_from_manifest_is_still_found(tmp_path: Path) -> None:
    # A manifest whose last write was lost shouldn't make a finished region
    # invisible - region_complete.json is our own convention, so a folder
    # carrying one is unambiguously a region of this batch.
    batch = build_fixture(tmp_path)
    orphan = batch / "S9R9"
    _write_trigger(orphan, "done", name="S9R9")
    names = {r.name for r in find_scorable_regions(tmp_path)}
    assert names == {"S1R1", "S9R9"}


def test_unparseable_trigger_falls_back_not_fails(tmp_path: Path) -> None:
    batch = build_fixture(tmp_path)
    (batch / "S1R1" / REGION_COMPLETE_NAME).write_text("{ truncated", encoding="utf-8")
    assert read_region_complete(batch / "S1R1") is None
    # The manifest still says "done", so the region remains scorable.
    s1 = next(r for r in list_regions(tmp_path) if r.name == "S1R1")
    assert s1.status == "done" and s1.status_source == "manifest"


def test_image_dir_accepts_a_batch_folder(tmp_path: Path) -> None:
    """A batch folder is the expected Image Directory. Pointing there used to
    report "nothing found" while staring straight at the data, because the
    scan looks one level down and a Section folder has no manifest."""
    batch = build_fixture(tmp_path)

    # A batch folder resolves to (the run folder above it, its own name).
    root, batch_name = resolve_image_dir(batch)
    assert root == tmp_path and batch_name == BATCH

    # The run folder resolves to itself, with no batch filter.
    root, batch_name = resolve_image_dir(tmp_path)
    assert root == tmp_path and batch_name is None

    # Both spellings must find the same sections.
    from_run = find_scorable_regions(tmp_path)
    resolved, batch_name = resolve_image_dir(batch)
    from_batch = find_scorable_regions(resolved, batch_name, exact=True)
    assert [r.key for r in from_run] == [r.key for r in from_batch]


def test_batch_folder_keeps_the_ledger_at_the_run_level(tmp_path: Path) -> None:
    """Resolving to the RUN folder (rather than treating the batch folder as a
    root of its own) is what keeps the ledger and the wafer-level review files
    where the acquisition pipeline already puts them."""
    batch = build_fixture(tmp_path)
    root, _ = resolve_image_dir(batch)
    ledger.mark(root, f"{BATCH}/S1R1", "abc123")
    assert (tmp_path / ledger.LEDGER_NAME).is_file()
    assert not (batch / ledger.LEDGER_NAME).exists()


def test_section_folder_is_not_mistaken_for_a_batch(tmp_path: Path) -> None:
    # A Section folder has no manifest, so it must NOT resolve to its parent -
    # that would silently score the whole batch when one section was meant.
    batch = build_fixture(tmp_path)
    root, batch_name = resolve_image_dir(batch / "S1R1")
    assert root == batch / "S1R1" and batch_name is None
    assert find_scorable_regions(root) == []


def test_sample_is_the_wafer_not_the_batch(tmp_path: Path) -> None:
    """Sample = the wafer (w<N>h<N>); Batch = the acquisition folder."""
    assert parse_sample_id("w10h31_dye_roi_R2_focus_8NM_20260909_125113") == "w10h31"
    # build_block_name's other spelling, per the README's own example.
    assert parse_sample_id("w13_h25_Slice1_8NM_20260629_143012") == "w13_h25"
    assert parse_sample_id("W9H9_test_8NM_20260907_120000") == "W9H9"
    # A folder name carrying no wafer identity must not invent one.
    assert parse_sample_id("some_unrelated_folder") is None

    build_fixture(tmp_path)
    r = find_scorable_regions(tmp_path)[0]
    assert r.sample_id == "w9h9"          # from BATCH = "w9h9_test_8NM_..."
    assert r.batch_label == BATCH         # the whole timestamped folder
    assert r.run_label == tmp_path.name   # the folder above the batch


def test_manifest_tolerates_bom_and_missing(tmp_path: Path) -> None:
    batch = build_fixture(tmp_path)
    path = batch / "region_manifest.json"
    # A BOM is what PowerShell's Out-File and Notepad produce; utf-8 (not -sig)
    # would raise here and silently drop the whole batch from discovery.
    path.write_bytes(b"\xef\xbb\xbf" + path.read_bytes())
    assert read_region_manifest(batch) is not None
    assert read_region_manifest(tmp_path / "nope") is None


def test_verify_region_files_flags_short_tile(tmp_path: Path) -> None:
    batch = build_fixture(tmp_path)
    region = batch / "S1R1"
    assert verify_region_files(region).ok
    # Remove one beam from one tile: that tile is now short of the region's
    # modal beam count, which is the mid-write signature we care about.
    victim = next((region / "mfov3").glob("*.bmp"))
    victim.unlink()
    check = verify_region_files(region)
    assert not check.ok
    assert check.n_incomplete == 1
    assert check.modal_beam_count == N_BEAMS


def test_layout_from_csv_then_metadata_fallback(tmp_path: Path) -> None:
    batch = build_fixture(tmp_path)
    region = batch / "S1R1"

    tiles, source = read_region_layout(region)
    assert source == "live_qc_csv"
    assert [t.tile_idx for t in tiles] == list(range(1, 8))
    # ol_pred must come through as a real number, not the NaN placeholder
    # columns sitting next to it in the same row.
    assert tiles[0].ol_pred is not None and abs(tiles[0].ol_pred - 4.451) < 1e-6

    # Without the CSV we fall back to metadata.txt and still get every tile.
    (region / "LiveQC" / "live_qc_results.csv").unlink()
    tiles2, source2 = read_region_layout(region)
    assert source2 == "metadata"
    assert [t.tile_idx for t in tiles2] == [t.tile_idx for t in tiles]
    assert [round(t.x_um, 3) for t in tiles2] == [round(t.x_um, 3) for t in tiles]


def test_nan_placeholders_never_become_values(tmp_path: Path) -> None:
    batch = build_fixture(tmp_path)
    tiles, _ = read_region_layout(batch / "S1R1")
    # `quality` is literal NaN in lightweight mode. Nothing in TileRef should
    # be carrying a NaN forward into a median somewhere downstream.
    for t in tiles:
        for val in (t.ol_pred, t.stig_x_pred, t.stig_y_pred):
            assert val is None or not np.isnan(val)


def test_probe_finds_blank_tiles_and_selection_uses_them(tmp_path: Path) -> None:
    build_fixture(tmp_path, n_tiles=7, tissue={2, 3, 4, 5})
    region = find_scorable_regions(tmp_path)[0]
    score = score_region(region, _cfg())

    assert score.layout_source == "live_qc_csv"
    assert score.n_tiles == 7
    # Tiles 1, 6, 7 are flat -> blank; 2..5 are textured -> tissue.
    assert sorted(score.tissue_tiles) == [2, 3, 4, 5]
    assert score.n_blank == 3
    # first / middle / last of the tissue tiles
    assert [r["tile_idx"] for r in score.scored] == [2, 4, 5]


def test_outlier_beam_is_detected(tmp_path: Path) -> None:
    build_fixture(tmp_path)
    region = find_scorable_regions(tmp_path)[0]
    score = score_region(region, _cfg())

    assert score.beam_summary, "expected a per-beam summary"
    assert {b["beam_index"] for b in score.beam_summary} == set(range(1, N_BEAMS + 1))
    flagged = [b["beam_index"] for b in score.beam_summary if b["is_outlier"]]
    assert flagged == [BAD_BEAM], f"expected only beam {BAD_BEAM}, got {flagged}"
    # Every beam was actually scored on every selected tile - the whole point
    # of beam_selection_mode=ALL.
    for r in score.scored:
        assert r["n_images_used"] == N_BEAMS


def test_sampling_modes(tmp_path: Path) -> None:
    build_fixture(tmp_path, n_tiles=7, tissue={2, 3, 4, 5})
    region = find_scorable_regions(tmp_path)[0]

    tiles, _ = read_region_layout(region.region_dir)
    picked, reason = select_tiles(tiles, {}, _cfg(tile_sampling="all"))
    assert len(picked) == 7 and "every mFoV" in reason

    picked, _ = select_tiles(tiles, {}, _cfg(tile_sampling="every_n",
                                            tile_sampling_every_n=3))
    assert [t.tile_idx for t in picked] == [1, 4, 7]

    # No map-pass data -> must fall back to position, not crash or return
    # nothing. The reason says so, because mFoVs chosen by position can
    # land on blank ones and the report should not hide that.
    picked, reason = select_tiles(tiles, {}, _cfg())
    assert len(picked) == 3 and "map pass skipped" in reason


def test_report_files_are_written(tmp_path: Path) -> None:
    build_fixture(tmp_path)
    region = find_scorable_regions(tmp_path)[0]
    cfg = _cfg()
    score = score_region(region, cfg)
    written = write_region_report(score, cfg, policy_fingerprint(cfg))

    for key in ("results_csv", "results_jsonl", "probe_csv", "summary_json",
                "report_html"):
        assert written[key].is_file(), f"{key} not written"
        assert written[key].stat().st_size > 0

    # Our output must live under StandaloneQC/, never mixed into LiveQC/ -
    # keeping it unambiguous which system owns which files.
    assert written["report_html"].parent.name == "StandaloneQC"

    summary = json.loads(written["summary_json"].read_text(encoding="utf-8"))
    assert summary["producer"] == "standalone_qc"
    assert summary["n_tiles"] == 7
    assert [b["beam_index"] for b in summary["outlier_beams"]] == [BAD_BEAM]

    html = written["report_html"].read_text(encoding="utf-8")
    # 7 for the mFoV map, plus one point per mFoV in each focus-plane scatter.
    assert "<svg" in html and html.count("<circle") >= 7
    assert "Focus &amp; stigmation planes" in html
    assert "OUTLIER" in html
    # An unrendered f-string placeholder would mean a broken template.
    assert "{score." not in html and "{cfg." not in html


def _write_timing(region_dir: Path, catchup: float) -> None:
    (region_dir / "LiveQC").mkdir(parents=True, exist_ok=True)
    (region_dir / "LiveQC" / "live_qc_timing.json").write_text(
        json.dumps({"wall_s": 1234.5, "n_tiles": 7, "qc_catchup_s": catchup,
                    "qc_catchup_worker_s": catchup / 2,
                    "qc_catchup_viewer_s": catchup / 2}),
        encoding="utf-8",
    )


def test_catchup_is_a_warning_only_in_lightweight_mode(tmp_path: Path) -> None:
    """The fixture CSV writes NaN quality, i.e. lightweight mode - where a
    600s catch-up IS a regression and must be flagged."""
    batch = build_fixture(tmp_path)
    _write_timing(batch / "S1R1", 601.2)
    region = find_scorable_regions(tmp_path)[0]
    cfg = _cfg()
    score = score_region(region, cfg)

    assert score.live_qc_mode == "lightweight"
    assert score.acq_timing["wall_s"] == 1234.5
    assert any("qc_catchup_s" in e for e in score.errors)
    assert not score.notes

    written = write_region_report(score, cfg, policy_fingerprint(cfg))
    summary = json.loads(written["summary_json"].read_text(encoding="utf-8"))
    # Acquisition's own timings stay in the JSON for whoever wants them...
    assert summary["acq_timing"]["qc_catchup_s"] == 601.2
    assert summary["acq_live_qc_mode"] == "lightweight"

    page = written["report_html"].read_text(encoding="utf-8")
    # ...but never as a header row on the operator-facing page: they are the
    # ACQUISITION machine's numbers, and there is no catch-up on the
    # standalone system, so showing them there only raised "whose number is
    # this?". The one actionable case appears as a warning instead.
    assert "Acquisition QC catch-up" not in page
    assert "Acquisition imaging time" not in page
    assert "qc_catchup_s" in page and "Warnings" in page


def test_catchup_in_full_mode_is_a_note_not_a_warning(tmp_path: Path) -> None:
    """A pre-lightweight run legitimately spent minutes here - that's the
    baseline this split removes, not something to report back. Flagging it as
    a regression would be a false alarm (observed on real data, 2026-09-07)."""
    batch = build_fixture(tmp_path)
    _write_timing(batch / "S1R1", 600.0)
    # Make the CSV look like full mode by giving one tile a real quality.
    csv_path = batch / "S1R1" / "LiveQC" / "live_qc_results.csv"
    lines = csv_path.read_text(encoding="utf-8").splitlines()
    lines[1] = lines[1].replace(",nan,", ",-22.71,", 1)
    csv_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    cfg = _cfg()
    score = score_region(find_scorable_regions(tmp_path)[0], cfg)
    assert score.live_qc_mode == "full"
    assert not any("qc_catchup_s" in e for e in score.errors)
    assert any("qc_catchup_s" in n for n in score.notes)

    page = write_region_report(score, cfg, policy_fingerprint(cfg))["report_html"]
    body = page.read_text(encoding="utf-8")
    assert "Notes" in body and "Warnings" not in body


def test_report_uses_operator_vocabulary(tmp_path: Path) -> None:
    """Sample (the wafer) / Run / Batch / Section / mFoV - not the code's
    block / region / tile - and the internal policy fingerprint stays off the
    page (still in the summary JSON)."""
    build_fixture(tmp_path)
    cfg = _cfg()
    score = score_region(find_scorable_regions(tmp_path)[0], cfg)
    written = write_region_report(score, cfg, policy_fingerprint(cfg))
    page = written["report_html"].read_text(encoding="utf-8")

    for wanted in ("Sample", "Run", "Batch", "Section", "Image directory",
                   "mFoVs in section", "mFoVs fully analyzed", "mFoV map",
                   "Fully analyzed mFoVs"):
        assert wanted in page, f"missing {wanted!r}"
    for unwanted in ("Block", "Tiles in region", "Region folder",
                     "Policy fingerprint", "Tile map"):
        assert unwanted not in page, f"should not appear: {unwanted!r}"

    # Sample is the WAFER, not the acquisition folder - the two were the same
    # string before this distinction existed.
    assert "w9h9" in page
    assert score.sample_id == "w9h9" and score.block_label == BATCH
    # "Image directory" is the batch folder: the one CONTAINING this section.
    assert str(score.region_dir.parent) in page

    summary = json.loads(written["summary_json"].read_text(encoding="utf-8"))
    assert summary["sample"] == "w9h9"
    assert summary["batch"] == BATCH
    assert summary["run"] == tmp_path.name

    # Internal identifiers are untouched: the CSV keeps live_qc_results.csv's
    # column names and the summary keeps the code's own keys, so nothing that
    # reads these files has to change.
    assert written["results_csv"].read_text(encoding="utf-8").startswith("tile_idx,x_um")
    summary = json.loads(written["summary_json"].read_text(encoding="utf-8"))
    assert {"region", "block", "n_tiles", "policy_fingerprint"} <= set(summary)


def test_missing_acq_timing_is_not_an_error(tmp_path: Path) -> None:
    build_fixture(tmp_path)
    region = find_scorable_regions(tmp_path)[0]
    score = score_region(region, _cfg())
    assert score.acq_timing == {}
    assert not any("qc_catchup" in e for e in score.errors)


def test_ledger_roundtrip_and_policy_invalidation(tmp_path: Path) -> None:
    build_fixture(tmp_path)
    cfg = _cfg()
    fp = policy_fingerprint(cfg)
    key = f"{BATCH}/S1R1"

    assert not ledger.is_done(ledger.load(tmp_path), key, fp)
    ledger.mark(tmp_path, key, fp, detail={"n_tiles": 7})
    assert ledger.is_done(ledger.load(tmp_path), key, fp)

    # A knob that changes the numbers must invalidate; one that doesn't, mustn't.
    other = policy_fingerprint(_cfg(dsamp=DSAMP + 8))
    assert not ledger.is_done(ledger.load(tmp_path), key, other)
    same = policy_fingerprint(_cfg(max_workers=99))
    assert same == fp
    assert ledger.is_done(ledger.load(tmp_path), key, same)


def test_missing_images_do_not_raise(tmp_path: Path) -> None:
    batch = build_fixture(tmp_path)
    for p in (batch / "S1R1" / "mfov3").glob("*.bmp"):
        p.unlink()
    region = find_scorable_regions(tmp_path)[0]
    score = score_region(region, _cfg())
    # Tile 3 still appears (a hole in a region should be visible, not omitted)
    # and the run completes rather than dying on it.
    assert 3 in score.probe_by_tile
    assert score.probe_by_tile[3]["status_category"] != "ok"
    assert score.scored


# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import tempfile
    import traceback

    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for fn in tests:
        with tempfile.TemporaryDirectory() as td:
            try:
                fn(Path(td))
                print(f"  PASS  {fn.__name__}")
            except Exception:
                failed += 1
                print(f"  FAIL  {fn.__name__}")
                traceback.print_exc()
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    raise SystemExit(1 if failed else 0)


def test_map_pass_shrinks_when_every_mfov_is_rescored(tmp_path: Path) -> None:
    """With tile_sampling=all and beam_selection_mode=ALL the map pass's
    results are all overwritten, so reading five beams for it is five beams
    of share traffic for one scan check.

    On the operator's sections that was 1,995 reads of 38,304 - about 5.6 GB
    a section off a share already at its throughput ceiling.
    """
    from standalone_qc import score as _score

    build_fixture(tmp_path)
    region = find_scorable_regions(tmp_path)[0]

    base = dict(dsamp=DSAMP, max_workers=4, region_overview_enabled=False)
    cfg_all = load_config(None, tile_sampling="all",
                          beam_selection_mode="ALL", **base)
    assert _score._map_pass_is_redundant(cfg_all)
    s_all = score_region(region, cfg_all)

    n_tiles = len(s_all.results_by_tile)
    full = sum(len(r.get("selected_beams") or []) for r in s_all.scored)
    assert full // n_tiles > 1, "the fixture must have several beams"
    # the all-beam pass plus exactly ONE map read per mFoV, not five.
    assert s_all.n_images_read == full + n_tiles

    # The scanfault column still has its input - that is why the map pass is
    # reduced rather than skipped.
    assert len(s_all.scan_checks) == n_tiles

    # A sampling run still needs the full map pass: those mFoVs get no
    # all-beam reading, so their hexes come from the 5-beam sample.
    cfg_some = load_config(None, tile_sampling="every_n",
                           tile_sampling_every_n=3,
                           beam_selection_mode="ALL", **base)
    assert not _score._map_pass_is_redundant(cfg_some)
    s_some = score_region(region, cfg_some)
    mapped = sum(len(r.get("selected_beams") or [])
                 for i, r in s_some.probe_by_tile.items()
                 if i not in {x["tile_idx"] for x in s_some.scored})
    assert mapped > len(s_some.probe_by_tile), "map pass must stay multi-beam"


def test_tissue_count_uses_the_best_reading_per_mfov(tmp_path: Path) -> None:
    """n_tissue_tiles came from the map pass even when every mFoV had since
    been re-measured at 91 beams - the 5-beam guess outranking the result
    that had replaced it everywhere else on the page."""
    build_fixture(tmp_path)
    region = find_scorable_regions(tmp_path)[0]
    s = score_region(region, load_config(
        None, tile_sampling="all", beam_selection_mode="ALL",
        dsamp=DSAMP, max_workers=4, region_overview_enabled=False))

    expected = [i for i, r in sorted(s.results_by_tile.items())
                if not r.get("is_blank_outside_roi")]
    assert s.tissue_tiles == expected
