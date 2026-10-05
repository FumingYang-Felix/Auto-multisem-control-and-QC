"""Keeping the stitched overview inside what a browser will decode.

Measured on the real S5R5 overview (24355 x 27539 = 671 Mpx, a 251 MB PNG),
loaded into Chromium:

    671 Mpx -> image ends in the "broken" state, drawImage throws
    168 Mpx -> loads, drawImage ok
     75 Mpx -> loads, drawImage ok
     42 Mpx -> loads, drawImage ok

The hex overlay is SVG and kept drawing correctly over the failed image,
which is why the operator saw a blurry, misaligned background rather than an
obviously missing one.

    pytest standalone_qc/tests/test_overview_plan.py
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import pytest  # noqa: E402

from standalone_qc.config import load_config  # noqa: E402
from standalone_qc.overview_plan import (  # noqa: E402
    full_pixel_extent,
    plan_overview_ds,
)

SFOV_W, SFOV_H = 2000, 1748


def _make_tiles(tmp_path: Path, n_x: int, n_y: int, pitch: int = 16000):
    """A grid of mFoVs with real image_coordinates.txt files.

    Coordinates are GLOBAL image pixels, as the acquisition writes them -
    that is what makes the footprint computable without any stage-to-pixel
    calibration.
    """
    from PIL import Image

    tiles = []
    idx = 0
    for iy in range(n_y):
        for ix in range(n_x):
            idx += 1
            folder = tmp_path / f"mfov{idx}"
            folder.mkdir()
            x0, y0 = ix * pitch, iy * pitch
            rows = []
            for b in range(4):                       # 4 beams is enough
                bx = x0 + (b % 2) * SFOV_W
                by = y0 + (b // 2) * SFOV_H
                name = f"mfov{idx}_b{b}.bmp"
                Image.new("L", (SFOV_W, SFOV_H), 128).save(folder / name)
                rows.append(f"{folder / name}\t{bx}\t{by}\t0")
            (folder / "image_coordinates.txt").write_text(
                "\n".join(rows) + "\n", encoding="utf-8")
            tiles.append(SimpleNamespace(tile_idx=idx, tile_folder=folder,
                                         x_um=float(x0), y_um=float(y0)))
    return tiles


def _cfg(**kw):
    return load_config(None, **kw)


def test_small_section_keeps_the_operators_setting(tmp_path: Path) -> None:
    tiles = _make_tiles(tmp_path, 2, 2)
    ds, info = plan_overview_ds(tiles, _cfg())
    assert ds == 16, "a small section must not be second-guessed"
    assert info["requested_ds"] == 16
    assert not info.get("note")


def test_large_section_is_brought_under_the_cap(tmp_path: Path) -> None:
    """The real case: enough mFoVs that ds=16 blows past what Chrome decodes."""
    tiles = _make_tiles(tmp_path, 10, 10, pitch=40000)
    cfg = _cfg(overview_max_megapixels=64, overview_max_dim_px=12000)

    at_16_w = info_w = None
    ds, info = plan_overview_ds(tiles, cfg)
    assert ds > 16, "ds should have been raised"

    # The result actually satisfies the budget it was given.
    assert info["width"] * info["height"] <= 64e6
    assert max(info["width"], info["height"]) <= 12000
    # And it is only just big enough - not wildly over-shrunk.
    assert (info["width"] * info["height"]) > 64e6 * 0.25
    assert "will not decode" in info["note"]
    assert str(ds) in info["note"] and "16" in info["note"]


def test_the_dimension_cap_can_bind_before_the_pixel_cap(tmp_path: Path) -> None:
    """A long thin section can be under the megapixel budget and still be
    too wide - Chrome also has a per-dimension limit."""
    tiles = _make_tiles(tmp_path, 40, 1, pitch=40000)
    cfg = _cfg(overview_max_megapixels=0, overview_max_dim_px=8000)
    ds, info = plan_overview_ds(tiles, cfg)
    assert max(info["width"], info["height"]) <= 8000


def test_caps_can_be_switched_off(tmp_path: Path) -> None:
    tiles = _make_tiles(tmp_path, 10, 10, pitch=40000)
    ds, info = plan_overview_ds(
        tiles, _cfg(overview_max_megapixels=0, overview_max_dim_px=0))
    assert ds == 16
    assert "no overview size cap" in info["note"]


def test_unreadable_coordinates_leave_ds_alone(tmp_path: Path) -> None:
    """Never guess. Without coordinates there is no footprint to reason
    from, so the configured factor stands and the reason is recorded."""
    folder = tmp_path / "mfov1"
    folder.mkdir()
    tiles = [SimpleNamespace(tile_idx=1, tile_folder=folder, x_um=0.0, y_um=0.0)]
    ds, info = plan_overview_ds(tiles, _cfg())
    assert ds == 16
    assert "no image_coordinates.txt readable" in info["note"]


def test_extent_is_measured_from_the_coordinate_files(tmp_path: Path) -> None:
    tiles = _make_tiles(tmp_path, 3, 2, pitch=16000)
    span_x, span_y, n = full_pixel_extent(tiles)
    assert n == 6
    # 3 columns at 16000 apart, plus the second beam column.
    assert span_x == pytest.approx(2 * 16000 + SFOV_W)
    assert span_y == pytest.approx(1 * 16000 + SFOV_H)


def test_ds_is_never_lowered(tmp_path: Path) -> None:
    """A deliberately coarse setting must be respected - the planner exists
    to prevent unrenderable images, not to second-guess the operator."""
    tiles = _make_tiles(tmp_path, 2, 2)
    ds, _ = plan_overview_ds(tiles, _cfg(overview_ds=64))
    assert ds == 64
