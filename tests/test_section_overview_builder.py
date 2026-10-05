"""
End-to-end test for section_overview_builder.py against synthetic mFOV data.

Builds a small fake section: a few mfov*/ folders, each with a handful of
beam tiles at DELIBERATELY DIFFERENT overall brightness levels (simulating
real beam-to-beam / mFOV-to-mFOV intensity variation in a MultiSEM section),
each containing a small bright square (a stand-in "feature") on a mid-gray
background, plus an image_coordinates.txt placing the mFOVs side by side in
one shared global coordinate frame. Confirms:

  1. discover_mfov_dirs finds every mFOV.
  2. build_section_overview runs end-to-end and produces a correctly-sized,
     saved uint8 PNG.
  3. Per-mFOV gain normalization actually equalizes brightness across mFOVs
     that started at very different levels -- the central point of choosing
     per-mFOV normalization over a single whole-section stretch.

Run standalone (no pytest needed):   python tests/test_section_overview_builder.py
"""
import os
import sys
import tempfile

import cv2
import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, REPO)

from section_overview_builder import (  # noqa: E402
    OverviewConfig, discover_mfov_dirs, build_section_overview,
    load_and_prepare_mfov_tiles, normalize_mfov_gain,
    find_thumbnail_for_beam, discover_actual_thumbnail_ds,
)

TILE_W, TILE_H = 200, 175  # small stand-in for the real 2000x1748 raw tile
THUMB_SCALE = 20           # stand-in scope-side DP_THUMBNAIL_SCALE


def _make_beam_tile(base_level: int, feature_level: int) -> np.ndarray:
    img = np.full((TILE_H, TILE_W), base_level, dtype=np.uint8)
    cv2.rectangle(img, (60, 50), (140, 120), feature_level, -1)
    return img


def _write_mfov(mfov_dir: str, origin_xy, base_level: int, feature_level: int, n_beams: int = 4,
                with_thumbnails=False, thumbnail_ext="png", thumbnails_for_beams=None):
    """with_thumbnails=True writes a real (separately downsampled) thumbnail
    alongside each full-res beam, same stem, mirroring what the scope does
    per configure_output_for_tile(). thumbnails_for_beams restricts which
    beam numbers get one (default: all), to test partial availability."""
    os.makedirs(mfov_dir, exist_ok=True)
    ox, oy = origin_xy
    lines = []
    for b in range(1, n_beams + 1):
        name = f"mfov_{b:03d}.bmp"
        x = ox + (b - 1) * TILE_W
        y = oy
        full = _make_beam_tile(base_level, feature_level)
        cv2.imwrite(os.path.join(mfov_dir, name), full)
        lines.append(f"{name}\t{x}\t{y}\t0")
        wants_thumb = with_thumbnails and (thumbnails_for_beams is None or b in thumbnails_for_beams)
        if wants_thumb:
            # match _downsample_area's own round() (not floor division), so a
            # thumbnail-sourced tile and a full-res-fallback tile at the same
            # effective scale come out the same size, as they should.
            thumb = cv2.resize(full, (max(1, round(TILE_W / THUMB_SCALE)), max(1, round(TILE_H / THUMB_SCALE))),
                               interpolation=cv2.INTER_AREA)
            cv2.imwrite(os.path.join(mfov_dir, f"mfov_{b:03d}.{thumbnail_ext}"), thumb)
    with open(os.path.join(mfov_dir, "image_coordinates.txt"), "w") as f:
        f.write("\n".join(lines) + "\n")


def _make_synthetic_section(root: str):
    # 3 mFOVs at very different overall brightness (simulating beam/detector
    # variation across the section), each placed at a different global
    # position so they don't overlap once stitched.
    _write_mfov(os.path.join(root, "mfov1"), (0, 0), base_level=40, feature_level=180)
    _write_mfov(os.path.join(root, "mfov2"), (TILE_W * 5, 0), base_level=120, feature_level=220)
    _write_mfov(os.path.join(root, "mfov3"), (TILE_W * 10, 0), base_level=80, feature_level=90)  # low-contrast mFOV


def test_discover_mfov_dirs_finds_all_three():
    with tempfile.TemporaryDirectory() as td:
        _make_synthetic_section(td)
        found = discover_mfov_dirs(td)
        assert len(found) == 3, f"expected 3 mFOV dirs, got {found}"


def test_build_section_overview_end_to_end():
    with tempfile.TemporaryDirectory() as td:
        _make_synthetic_section(td)
        cfg = OverviewConfig(
            target_um_per_px=0.5, raw_pixel_nm=8.0,
            tile_w_full=TILE_W, tile_h_full=TILE_H, padding_full=8,
        )
        out_path = os.path.join(td, "overview.png")
        result = build_section_overview(td, out_path, cfg)
        assert result == out_path
        assert os.path.isfile(out_path)
        img = cv2.imread(out_path, cv2.IMREAD_GRAYSCALE)
        assert img is not None
        assert img.shape[0] > 0 and img.shape[1] > 0
        # something was actually drawn, not a blank canvas
        assert img.max() > img.min()


def test_per_mfov_gain_normalization_equalizes_brightness():
    # The core design choice this tool made: two mFOVs whose (already
    # background-subtracted, downsampled) signal sits at very different
    # absolute levels must land at the SAME target level after per-mFOV gain
    # correction, rather than one whole-section stretch leaving the dimmer
    # mFOV looking dim relative to the rest. Tested directly on
    # normalize_mfov_gain (not routed through background subtraction first,
    # which -- by design -- removes flat/uniform level differences and would
    # confound a fixture built from uniform backgrounds).
    cfg = OverviewConfig()
    dim_tiles = {
        "a": np.full((50, 50), 20.0, dtype=np.float32),
        "b": np.full((50, 50), 30.0, dtype=np.float32),
    }
    bright_tiles = {
        "a": np.full((50, 50), 150.0, dtype=np.float32),
        "b": np.full((50, 50), 160.0, dtype=np.float32),
    }

    dim_norm = normalize_mfov_gain(dim_tiles, cfg)
    bright_norm = normalize_mfov_gain(bright_tiles, cfg)

    dim_after = np.concatenate([t.reshape(-1) for t in dim_norm.values()])
    bright_after = np.concatenate([t.reshape(-1) for t in bright_norm.values()])
    dim_p = float(np.percentile(dim_after, cfg.gain_percentile))
    bright_p = float(np.percentile(bright_after, cfg.gain_percentile))

    assert abs(dim_p - cfg.gain_target_level) < 1.0, f"dim mFOV's {cfg.gain_percentile}th pct = {dim_p:.1f}"
    assert abs(bright_p - cfg.gain_target_level) < 1.0, f"bright mFOV's {cfg.gain_percentile}th pct = {bright_p:.1f}"
    assert abs(dim_p - bright_p) < 1.0, (
        f"per-mFOV normalization should equalize the two mFOVs near the common target "
        f"({cfg.gain_target_level}), got dim={dim_p:.1f} bright={bright_p:.1f}"
    )


def test_find_thumbnail_for_beam_matches_by_stem():
    with tempfile.TemporaryDirectory() as td:
        _write_mfov(td, (0, 0), base_level=50, feature_level=200, n_beams=2, with_thumbnails=True)
        cfg = OverviewConfig(tile_w_full=TILE_W, tile_h_full=TILE_H)
        beam_path = os.path.join(td, "mfov_001.bmp")
        found = find_thumbnail_for_beam(beam_path, cfg)
        assert found is not None
        assert found == os.path.join(td, "mfov_001.png")


def test_find_thumbnail_does_not_match_itself_when_same_extension():
    # Regression test: if thumbnail_extensions includes "bmp" (STR_THUMBNAIL_FILETYPE
    # could legitimately be set to match the full-res format) and no separate
    # thumbnail file actually exists, the full-res beam must NOT be returned
    # as its own "thumbnail" -- that would make the pipeline treat a full-res
    # image as already-downsampled.
    with tempfile.TemporaryDirectory() as td:
        _write_mfov(td, (0, 0), base_level=50, feature_level=200, n_beams=1, with_thumbnails=False)
        cfg = OverviewConfig(tile_w_full=TILE_W, tile_h_full=TILE_H,
                             thumbnail_extensions=("bmp", "png"))
        beam_path = os.path.join(td, "mfov_001.bmp")
        assert find_thumbnail_for_beam(beam_path, cfg) is None


def test_discover_actual_thumbnail_ds_computes_correct_scale():
    with tempfile.TemporaryDirectory() as td:
        mfov_dir = os.path.join(td, "mfov1")
        _write_mfov(mfov_dir, (0, 0), base_level=50, feature_level=200, n_beams=2, with_thumbnails=True)
        cfg = OverviewConfig(tile_w_full=TILE_W, tile_h_full=TILE_H)
        ds = discover_actual_thumbnail_ds([mfov_dir], cfg)
        assert ds == THUMB_SCALE, f"expected discovered ds={THUMB_SCALE}, got {ds}"


def test_discover_actual_thumbnail_ds_returns_none_when_absent():
    with tempfile.TemporaryDirectory() as td:
        mfov_dir = os.path.join(td, "mfov1")
        _write_mfov(mfov_dir, (0, 0), base_level=50, feature_level=200, n_beams=2, with_thumbnails=False)
        cfg = OverviewConfig(tile_w_full=TILE_W, tile_h_full=TILE_H)
        assert discover_actual_thumbnail_ds([mfov_dir], cfg) is None


def test_load_and_prepare_mfov_tiles_prefers_thumbnail_and_falls_back_per_beam():
    # Partial availability: beam 1 has a thumbnail, beam 2 doesn't -- both
    # must still end up in the result, one via each path, sized consistently
    # with the resolved cfg.ds (thumbnails are used as-is; full-res is
    # downsampled to match).
    with tempfile.TemporaryDirectory() as td:
        mfov_dir = os.path.join(td, "mfov1")
        _write_mfov(mfov_dir, (0, 0), base_level=50, feature_level=200, n_beams=2,
                   with_thumbnails=True, thumbnails_for_beams={1})
        cfg = OverviewConfig(tile_w_full=TILE_W, tile_h_full=TILE_H, ds=THUMB_SCALE)

        tiles, coords, n_thumb, n_fullres = load_and_prepare_mfov_tiles(mfov_dir, cfg)
        assert n_thumb == 1 and n_fullres == 1
        assert set(tiles.keys()) == {"mfov_001.bmp", "mfov_002.bmp"}
        # matches _downsample_area's own round() (not floor division) of tile_full/ds
        expected_shape = (max(1, round(TILE_H / THUMB_SCALE)), max(1, round(TILE_W / THUMB_SCALE)))
        assert tiles["mfov_001.bmp"].shape == expected_shape, \
            f"thumbnail-sourced tile shape {tiles['mfov_001.bmp'].shape} != {expected_shape}"
        assert tiles["mfov_002.bmp"].shape == expected_shape, \
            f"full-res-fallback tile shape {tiles['mfov_002.bmp'].shape} != {expected_shape}"


def test_build_section_overview_end_to_end_with_thumbnails():
    with tempfile.TemporaryDirectory() as td:
        _write_mfov(os.path.join(td, "mfov1"), (0, 0), base_level=40, feature_level=180,
                   with_thumbnails=True)
        _write_mfov(os.path.join(td, "mfov2"), (TILE_W * 5, 0), base_level=120, feature_level=220,
                   with_thumbnails=True)
        cfg = OverviewConfig(tile_w_full=TILE_W, tile_h_full=TILE_H, padding_full=4)
        out_path = os.path.join(td, "overview.png")
        build_section_overview(td, out_path, cfg)
        assert os.path.isfile(out_path)
        assert cfg.ds == THUMB_SCALE, "ds should be resolved from the discovered thumbnail scale"
        img = cv2.imread(out_path, cv2.IMREAD_GRAYSCALE)
        assert img is not None and img.max() > img.min()


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"PASS  {name}")
