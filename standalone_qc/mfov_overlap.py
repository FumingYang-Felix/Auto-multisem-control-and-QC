"""mFoV coverage: are there gaps between mFoVs, or do they interlock?

Works from `image_coordinates.txt`, which gives every beam of every mFoV in
ONE global pixel frame for the whole section. That is a stronger starting
point than the MATLAB CheckOverlap tool has: it reconstructs relative mFoV
positions by FFT cross-correlation first
(utils_estimate_mfov_displacement.m), then tests coverage using
`utils_analysis_mfov_overlap.m`'s neat trick - for each mFoV, take the
CONCAVE corners of its sFoV footprint and ask whether a neighbouring mFoV's
sFoV covers each one; an uncovered notch means a gap in that direction.

Here the same question is answered by rasterising coverage directly:

  * every beam contributes a rectangle (beam centre +/- half the image size)
  * coverage is accumulated on a coarse grid, a fraction of a beam wide
  * an uncovered cell that cannot be reached from outside the section is an
    INTERIOR HOLE - a gap between mFoVs. Uncovered cells connected to the
    outside are just the ragged edge of the section and are not faults.
  * for each adjacent mFoV pair, the shared covered area says whether they
    actually interlock

WHAT THIS DOES AND DOES NOT CATCH. It checks the geometry ZEN recorded: given
where the beams say they are, is the area covered? It does not verify that
the recorded positions match the image content - a stage that moved wrongly
but reported the right coordinates would pass. Catching that needs the
content-based displacement estimate the MATLAB tool does by cross-correlating
overlapping strips, which is the natural follow-on to this.
"""
from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np

# Grid resolution as a fraction of a beam width. 1/8 of a beam keeps the
# raster small (a 9-mFoV section is ~140x200 cells) while still resolving a
# gap far narrower than one beam.
GRID_FRACTION = 0.125
# Adjacency: mFoV centres closer than this multiple of the modal centre
# spacing count as neighbours. 1.3 admits the six hex neighbours without
# reaching the next ring.
ADJACENT_FACTOR = 1.3
# An adjacent pair sharing less than this fraction of one mFoV's area is not
# interlocking, even if no interior hole opened up.
MIN_PAIR_OVERLAP_FRAC = 0.01


@dataclass
class OverlapResult:
    n_mfovs: int = 0
    beam_w: int = 0
    beam_h: int = 0
    grid_px: float = 0.0
    sfov_overlap_frac: Optional[float] = None
    mfov_pitch_px: Optional[float] = None
    n_adjacent_pairs: int = 0
    pair_overlap_min_frac: Optional[float] = None
    pair_overlap_median_frac: Optional[float] = None
    weak_pairs: List[Tuple[str, str, float]] = field(default_factory=list)
    n_gap_cells: int = 0
    gap_area_px2: float = 0.0
    gap_area_um2: Optional[float] = None
    largest_gap_cells: int = 0
    passed: Optional[bool] = None
    note: str = ""
    # For rendering: per-mFoV footprint rectangles and the gap cell centres,
    # both in global pixel coordinates.
    footprints: Dict[str, Tuple[float, float, float, float]] = field(default_factory=dict)
    gap_points: List[Tuple[float, float]] = field(default_factory=list)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "n_mfovs": self.n_mfovs,
            "beam_w": self.beam_w, "beam_h": self.beam_h,
            "grid_px": self.grid_px,
            "sfov_overlap_frac": self.sfov_overlap_frac,
            "mfov_pitch_px": self.mfov_pitch_px,
            "n_adjacent_pairs": self.n_adjacent_pairs,
            "pair_overlap_min_frac": self.pair_overlap_min_frac,
            "pair_overlap_median_frac": self.pair_overlap_median_frac,
            "weak_pairs": [list(p) for p in self.weak_pairs],
            "n_gap_cells": self.n_gap_cells,
            "gap_area_px2": self.gap_area_px2,
            "gap_area_um2": self.gap_area_um2,
            "largest_gap_cells": self.largest_gap_cells,
            "passed": self.passed,
            "note": self.note,
        }


def _read_section_coords(section_dir: Path) -> Dict[str, Dict[str, Tuple[float, float]]]:
    from qc_overview import parse_coords_file
    out: Dict[str, Dict[str, Tuple[float, float]]] = {}
    try:
        subs = sorted(
            (p for p in section_dir.iterdir()
             if p.is_dir() and p.name.lower().startswith("mfov")),
            key=lambda p: p.name,
        )
    except OSError:
        return out
    for sub in subs:
        coords = parse_coords_file(str(sub / "image_coordinates.txt"))
        if coords:
            out[sub.name] = coords
    return out


def _beam_size(section_dir: Path, coords: Dict[str, Dict]) -> Tuple[int, int]:
    from PIL import Image
    for name in coords:
        for p in sorted((section_dir / name).glob("*.bmp")):
            try:
                with Image.open(p) as im:
                    return im.width, im.height
            except Exception:
                continue
    return 0, 0


def _interior_holes(covered: np.ndarray) -> np.ndarray:
    """Uncovered cells NOT reachable from the border.

    The flood fill is the whole point: a section is a ragged blob, so most
    uncovered cells are simply outside it. Only an uncovered region enclosed
    by coverage is a gap between mFoVs.
    """
    h, w = covered.shape
    outside = np.zeros_like(covered, dtype=bool)
    q: deque = deque()
    for x in range(w):
        for y in (0, h - 1):
            if not covered[y, x] and not outside[y, x]:
                outside[y, x] = True
                q.append((y, x))
    for y in range(h):
        for x in (0, w - 1):
            if not covered[y, x] and not outside[y, x]:
                outside[y, x] = True
                q.append((y, x))
    while q:
        y, x = q.popleft()
        for dy, dx in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            ny, nx = y + dy, x + dx
            if 0 <= ny < h and 0 <= nx < w and not covered[ny, nx] \
                    and not outside[ny, nx]:
                outside[ny, nx] = True
                q.append((ny, nx))
    return (~covered) & (~outside)


def _largest_component(mask: np.ndarray) -> int:
    """Size of the biggest 4-connected blob - one 12-cell hole matters more
    than twelve scattered single cells."""
    h, w = mask.shape
    seen = np.zeros_like(mask, dtype=bool)
    best = 0
    for y0 in range(h):
        for x0 in range(w):
            if not mask[y0, x0] or seen[y0, x0]:
                continue
            n = 0
            q: deque = deque([(y0, x0)])
            seen[y0, x0] = True
            while q:
                y, x = q.popleft()
                n += 1
                for dy, dx in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                    ny, nx = y + dy, x + dx
                    if 0 <= ny < h and 0 <= nx < w and mask[ny, nx] \
                            and not seen[ny, nx]:
                        seen[ny, nx] = True
                        q.append((ny, nx))
            best = max(best, n)
    return best


def analyze_section(
    section_dir: str | Path, cfg: Optional[Dict[str, Any]] = None
) -> OverlapResult:
    cfg = cfg or {}
    section_dir = Path(section_dir)
    out = OverlapResult()

    coords = _read_section_coords(section_dir)
    if len(coords) < 1:
        out.note = "no image_coordinates.txt found - cannot check coverage"
        return out
    W, H = _beam_size(section_dir, coords)
    if not W or not H:
        out.note = "could not read a beam image to get its size"
        return out

    out.n_mfovs = len(coords)
    out.beam_w, out.beam_h = W, H
    grid = max(1.0, GRID_FRACTION * min(W, H))
    out.grid_px = grid

    # sFoV overlap within an mFoV, from nearest-neighbour beam spacing.
    first = np.array(list(next(iter(coords.values())).values()), dtype=np.float64)
    if len(first) > 1:
        nn = []
        for i in range(len(first)):
            d = np.hypot(first[:, 0] - first[i, 0], first[:, 1] - first[i, 1])
            d[i] = np.inf
            nn.append(d.min())
        out.sfov_overlap_frac = float(1.0 - np.median(nn) / W)

    # Global raster bounds, padded by half a beam so edge beams fit.
    allpts = np.array(
        [p for c in coords.values() for p in c.values()], dtype=np.float64
    )
    x0 = allpts[:, 0].min() - W
    y0 = allpts[:, 1].min() - H
    x1 = allpts[:, 0].max() + W
    y1 = allpts[:, 1].max() + H
    nx = int(math.ceil((x1 - x0) / grid))
    ny = int(math.ceil((y1 - y0) / grid))
    if nx <= 0 or ny <= 0 or nx * ny > 40_000_000:
        out.note = f"raster would be {nx}x{ny} cells - refusing"
        return out

    per_mfov: Dict[str, np.ndarray] = {}
    covered = np.zeros((ny, nx), dtype=bool)
    for name, c in coords.items():
        m = np.zeros((ny, nx), dtype=bool)
        for (bx, by) in c.values():
            cx0 = int((bx - W / 2.0 - x0) / grid)
            cx1 = int(math.ceil((bx + W / 2.0 - x0) / grid))
            cy0 = int((by - H / 2.0 - y0) / grid)
            cy1 = int(math.ceil((by + H / 2.0 - y0) / grid))
            m[max(0, cy0):max(0, cy1), max(0, cx0):max(0, cx1)] = True
        per_mfov[name] = m
        covered |= m
        xs = [p[0] for p in c.values()]
        ys = [p[1] for p in c.values()]
        out.footprints[name] = (min(xs) - W / 2.0, min(ys) - H / 2.0,
                                max(xs) + W / 2.0, max(ys) + H / 2.0)

    # --- gaps ---
    holes = _interior_holes(covered)
    out.n_gap_cells = int(holes.sum())
    out.gap_area_px2 = float(out.n_gap_cells * grid * grid)
    px_nm = cfg.get("pixel_size_nm")
    if px_nm:
        out.gap_area_um2 = out.gap_area_px2 * (float(px_nm) / 1000.0) ** 2
    if out.n_gap_cells:
        out.largest_gap_cells = _largest_component(holes)
        ys, xs = np.nonzero(holes)
        step = max(1, len(xs) // 400)   # cap the render payload
        out.gap_points = [
            (x0 + (x + 0.5) * grid, y0 + (y + 0.5) * grid)
            for x, y in zip(xs[::step], ys[::step])
        ]

    # --- adjacent-pair overlap ---
    names = list(coords)
    cent = {
        n: (float(np.mean([p[0] for p in coords[n].values()])),
            float(np.mean([p[1] for p in coords[n].values()])))
        for n in names
    }
    dists = []
    for i, a in enumerate(names):
        for b in names[i + 1:]:
            dists.append((math.hypot(cent[a][0] - cent[b][0],
                                     cent[a][1] - cent[b][1]), a, b))
    if dists:
        dists.sort()
        pitch = dists[0][0]
        out.mfov_pitch_px = pitch
        fracs = []
        for d, a, b in dists:
            if d > ADJACENT_FACTOR * pitch:
                continue
            inter = int((per_mfov[a] & per_mfov[b]).sum())
            base = int(min(per_mfov[a].sum(), per_mfov[b].sum())) or 1
            frac = inter / base
            fracs.append(frac)
            if frac < MIN_PAIR_OVERLAP_FRAC:
                out.weak_pairs.append((a, b, float(frac)))
        out.n_adjacent_pairs = len(fracs)
        if fracs:
            out.pair_overlap_min_frac = float(min(fracs))
            out.pair_overlap_median_frac = float(np.median(fracs))

    out.passed = bool(out.n_gap_cells == 0 and not out.weak_pairs)
    if not out.passed:
        bits = []
        if out.n_gap_cells:
            bits.append(
                f"{out.n_gap_cells} interior gap cell(s), largest blob "
                f"{out.largest_gap_cells}"
            )
        if out.weak_pairs:
            bits.append(f"{len(out.weak_pairs)} adjacent pair(s) barely overlap")
        out.note = "; ".join(bits)
    return out


# ---------------------------------------------------------------------------
# rendering
# ---------------------------------------------------------------------------
def coverage_svg(res: OverlapResult, width: int = 420) -> str:
    """mFoV footprints with any detected gaps marked."""
    import html as _h
    if not res.footprints:
        return "<p class='muted'>No coverage geometry available.</p>"

    xs0 = min(f[0] for f in res.footprints.values())
    ys0 = min(f[1] for f in res.footprints.values())
    xs1 = max(f[2] for f in res.footprints.values())
    ys1 = max(f[3] for f in res.footprints.values())
    spanx = (xs1 - xs0) or 1.0
    spany = (ys1 - ys0) or 1.0
    pad = 8
    plot = width - 2 * pad
    height = int(plot * spany / spanx) + 2 * pad
    height = max(140, min(height, 620))
    ploth = height - 2 * pad

    def sx(x: float) -> float:
        return pad + (x - xs0) / spanx * plot

    def sy(y: float) -> float:
        # Negate y: SVG grows downward, the coordinate frame does not.
        return pad + (ys1 - y) / spany * ploth

    parts = [
        f"<svg viewBox='0 0 {width} {height}' width='100%' "
        f"style='max-width:{width}px' role='img' "
        f"aria-label='mFoV coverage map'>"
    ]
    for name, (fx0, fy0, fx1, fy1) in sorted(res.footprints.items()):
        x, y = sx(fx0), sy(fy1)
        w = max(1.0, sx(fx1) - sx(fx0))
        h = max(1.0, sy(fy0) - sy(fy1))
        parts.append(
            f"<rect x='{x:.1f}' y='{y:.1f}' width='{w:.1f}' height='{h:.1f}' "
            f"fill='#1d6fb8' fill-opacity='0.13' stroke='#1d6fb8' "
            f"stroke-width='1'><title>{_h.escape(name)}</title></rect>"
        )
    for (gx, gy) in res.gap_points:
        parts.append(
            f"<circle cx='{sx(gx):.1f}' cy='{sy(gy):.1f}' r='2.2' "
            f"fill='#c0392b'><title>gap</title></circle>"
        )
    parts.append("</svg>")
    return "".join(parts)
