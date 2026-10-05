r"""Building the overview from thumbnails instead of full-resolution beams.

Acquisition writes a 500x437 .png beside every 2000x1748 .bmp, with its own
coordinate file at exactly 1/4 scale. At the downsample an overview uses,
the thumbnail already carries every pixel the result keeps - so reading the
full-resolution set threw away 99.96% of what it read.

Measured on real data:

    one mFoV, full-res ds=52   318.2 MB   1.14 s
    one mFoV, thumbs   ds=13    15.2 MB   0.13 s
    399-mFoV section overview: 13+ min -> 66 s

The geometry has to match the old path exactly, because the bboxes it
returns are what position the quality overlay and the focus-support-point
markers. An early version was 17x faster and 128 px out: the origin must be
the minimum TOP-LEFT, not the minimum centre, and the white padding has to
be applied too.

    pytest standalone_qc/tests/test_overview_fast.py
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import pytest  # noqa: E402

from standalone_qc import overview_fast  # noqa: E402
from standalone_qc.config import load_config  # noqa: E402

TW, TH = 100, 80          # a small stand-in for the 500x437 thumbnail


def _section(tmp_path: Path, n_x=3, n_y=2, pitch=90, thumbs=True):
    """mFoVs on a grid, each with 2 beams and both coordinate files."""
    from PIL import Image

    tiles = []
    idx = 0
    for iy in range(n_y):
        for ix in range(n_x):
            idx += 1
            d = tmp_path / f"mfov{idx}"
            d.mkdir()
            trows, irows = [], []
            for b in range(2):
                cx = ix * pitch * 2 + b * TW + TW / 2.0
                cy = iy * pitch * 2 + TH / 2.0
                if thumbs:
                    name = f"m{idx}_{b:03d}.png"
                    Image.new("L", (TW, TH), 40 + 10 * b).save(d / name)
                    trows.append(f"{d / name}\t{cx}\t{cy}\t0")
                big = f"m{idx}_{b:03d}.bmp"
                Image.new("L", (TW * 4, TH * 4), 40 + 10 * b).save(d / big)
                irows.append(f"{d / big}\t{cx * 4}\t{cy * 4}\t0")
            if thumbs:
                (d / "thumbnail_coordinates.txt").write_text(
                    "\n".join(trows) + "\n", encoding="utf-8")
            (d / "image_coordinates.txt").write_text(
                "\n".join(irows) + "\n", encoding="utf-8")
            tiles.append(SimpleNamespace(tile_idx=idx, tile_folder=d,
                                         x_um=float(ix), y_um=float(iy)))
    return tiles


def _cfg(**kw):
    base = {"overview_ds": 4, "overview_padding": 0, "max_workers": 4}
    base.update(kw)
    return load_config(None, **base)


def test_prefers_thumbnails_and_measures_their_scale(tmp_path: Path) -> None:
    tiles = _section(tmp_path)
    pl = overview_fast.plan(tiles, _cfg())
    assert pl["source"] == "thumbnails"
    # Measured from the two coordinate files, not assumed to be 4.
    assert pl["scale"] == pytest.approx(4.0, rel=1e-6)
    assert pl["tile_w"] == TW and pl["tile_h"] == TH
    # overview_ds is quoted in FULL-RES pixels, so it has to be converted
    # into the thumbnail's own units or the result is 4x too coarse.
    assert pl["ds"] == 1


def test_falls_back_to_full_resolution_without_thumbnails(tmp_path: Path) -> None:
    tiles = _section(tmp_path, thumbs=False)
    pl = overview_fast.plan(tiles, _cfg())
    assert pl["source"] == "full resolution"
    assert pl["scale"] == 1.0
    assert pl["tile_w"] == TW * 4


def test_origin_is_the_top_left_not_the_centre(tmp_path: Path) -> None:
    """The bug that made an early version 128 px out. The leftmost, topmost
    beam must land at the padding offset - not half a tile before it."""
    tiles = _section(tmp_path)
    info = overview_fast.build(tiles, tmp_path / "out", _cfg())
    assert info is not None
    first = info["bboxes"][1]
    assert first[0] == 0 and first[1] == 0, info["bboxes"]
    # And nothing is clipped off the far edge.
    last = max(b[2] for b in info["bboxes"].values())
    assert last <= info["width"]


def test_padding_matches_the_full_resolution_builder(tmp_path: Path) -> None:
    tiles = _section(tmp_path)
    plain = overview_fast.build(tiles, tmp_path / "a", _cfg())
    # overview_padding is in FULL-RES px; at scale 4 and ds 1 that is /4.
    padded = overview_fast.build(tiles, tmp_path / "b",
                                 _cfg(overview_padding=40))
    assert padded["width"] == plain["width"] + 2 * 10
    assert padded["bboxes"][1][0] == plain["bboxes"][1][0] + 10


def test_output_shape_matches_what_the_dashboard_expects(tmp_path: Path) -> None:
    """The overlay and the FSP markers read these keys."""
    from PIL import Image

    tiles = _section(tmp_path)
    info = overview_fast.build(tiles, tmp_path / "out", _cfg())
    for key in ("path", "width", "height", "bboxes"):
        assert key in info, key
    assert len(info["bboxes"]) == len(tiles)
    with Image.open(info["path"]) as im:
        assert im.size == (info["width"], info["height"])
    assert Path(info["path"]).name == "standalone_qc_region_overview.png"


def test_a_section_with_no_coordinates_returns_none(tmp_path: Path) -> None:
    """None, so the caller falls back rather than writing a blank image."""
    d = tmp_path / "mfov1"
    d.mkdir()
    tiles = [SimpleNamespace(tile_idx=1, tile_folder=d, x_um=0.0, y_um=0.0)]
    assert overview_fast.plan(tiles, _cfg()) is None
    assert overview_fast.build(tiles, tmp_path / "out", _cfg()) is None


def test_overlapping_beams_keep_the_darker_pixel(tmp_path: Path) -> None:
    """Same compositing rule as qc_overview, so an overview built either
    way means the same thing."""
    import numpy as np
    from PIL import Image

    d = tmp_path / "mfov1"
    d.mkdir()
    rows = []
    for b, grey in enumerate((200, 60)):
        name = f"m1_{b:03d}.png"
        Image.new("L", (TW, TH), grey).save(d / name)
        rows.append(f"{d / name}\t{TW / 2.0}\t{TH / 2.0}\t0")   # same place
    (d / "thumbnail_coordinates.txt").write_text("\n".join(rows) + "\n",
                                                 encoding="utf-8")
    tiles = [SimpleNamespace(tile_idx=1, tile_folder=d, x_um=0.0, y_um=0.0)]
    info = overview_fast.build(tiles, tmp_path / "out",
                               _cfg(overview_ds=1, overview_padding=0))
    arr = np.asarray(Image.open(info["path"]).convert("L"))
    assert arr.min() == 60, "the darker of two overlapping beams must win"
    assert 200 not in set(np.unique(arr)), "the lighter beam should be covered"
