r"""Marking the focus support points on the section overview.

The support points are logged in STAGE microns; the overview is a raster in
its own pixel space. Nothing records the transform between them, but every
mFoV gives a known pair - its stage position from the layout, and its
bounding box in the overview from the accumulator - so the mapping can be
fitted from the section's own data.

Fitted, not assumed. qc_overview documents only the sign relationship
(corr(stage_x, img_x) = -1, corr(stage_y, img_y) = +1), not a scale, and the
scale depends on overview_ds which we now vary per section. Measured on a
397-mFoV section the fit lands at 0.128 um per overview pixel, which is
exactly the 8 nm acquisition pixel times ds=16 - so the fit recovers the
real scale rather than approximating it, with a worst-case residual of 34 px
in a 24355 px-wide image (0.14%).

Separate axis fits (x->px, y->py) because that is what the documented
relationship supports: no rotation, no skew. If a future setup needed a full
affine this is where it would go, and the residuals printed by the caller
would show it was needed.
"""
from __future__ import annotations

import html as _h
from typing import Any, Dict, List, Optional, Sequence, Tuple


def fit_stage_to_overview(
    tiles: Sequence[Any], bboxes: Dict[Any, Sequence[float]]
) -> Optional[Dict[str, float]]:
    """Least-squares px = ax*x_um + bx, py = ay*y_um + by.

    None when there is too little spread on either axis to solve for it -
    better no markers than markers in invented places.
    """
    import numpy as np

    stage = {int(t.tile_idx): (float(t.x_um), float(t.y_um)) for t in tiles}
    xs, ys, pxs, pys = [], [], [], []
    for key, bb in (bboxes or {}).items():
        try:
            idx = int(key)
        except (TypeError, ValueError):
            continue
        if idx not in stage or bb is None or len(bb) < 4:
            continue
        x0, y0, x1, y1 = (float(v) for v in bb[:4])
        xs.append(stage[idx][0])
        ys.append(stage[idx][1])
        pxs.append((x0 + x1) / 2.0)
        pys.append((y0 + y1) / 2.0)
    if len(xs) < 3:
        return None
    X = np.asarray(xs); Y = np.asarray(ys)
    PX = np.asarray(pxs); PY = np.asarray(pys)
    # np.ptp(...), not X.ptp(): the ndarray method was removed in
    # NumPy 2.0.
    if np.ptp(X) <= 0 or np.ptp(Y) <= 0:
        return None
    ax, bx = (float(v) for v in np.polyfit(X, PX, 1))
    ay, by = (float(v) for v in np.polyfit(Y, PY, 1))
    rx = float(np.abs(PX - (ax * X + bx)).max())
    ry = float(np.abs(PY - (ay * Y + by)).max())
    return {"ax": ax, "bx": bx, "ay": ay, "by": by,
            "resid_x_px": rx, "resid_y_px": ry, "n": len(xs),
            "um_per_px": abs(1.0 / ax) if ax else float("nan")}


def markers_svg(
    points: Sequence[Dict[str, Any]],
    fit: Dict[str, float],
    width: int,
    height: int,
) -> Tuple[str, List[str]]:
    """(svg fragment, notes). Draws a crosshair and label per support point.

    The overview's own rectangles are drawn with a 180 degree rotation
    applied to their coordinates (see qc_overview's panel builder), so the
    same flip is applied here or the markers would land diagonally opposite
    where they belong.
    """
    out: List[str] = []
    notes: List[str] = []
    r = max(14.0, 0.006 * max(width, height))
    sw = max(2.0, 0.0012 * max(width, height))
    fs = max(26.0, 0.012 * max(width, height))
    for pt in points:
        x, y = pt.get("x"), pt.get("y")
        if x is None or y is None:
            continue
        px = fit["ax"] * float(x) + fit["bx"]
        py = fit["ay"] * float(y) + fit["by"]
        cx, cy = width - px, height - py          # the same 180 deg flip
        idx = pt.get("index")
        label = f"FSP {idx}" if idx is not None else "FSP"
        if not (0 <= cx <= width and 0 <= cy <= height):
            notes.append(
                f"{label} at stage ({x:.0f}, {y:.0f}) falls outside the "
                f"overview - support points are often measured off the "
                f"imaged area"
            )
            continue
        bits = [f"{label}  stage ({x:.1f}, {y:.1f}) um"]
        for key, name in (("ol_meas", "OL measured"),
                          ("ol_pred", "OL predicted"),
                          ("resid", "residual")):
            v = pt.get(key)
            if v is not None:
                bits.append(f"{name} {v:+.3f} uA")
        tip = _h.escape("\n".join(bits))
        # Magenta: nothing else on the overview uses it, so a marker cannot
        # be mistaken for a quality colour.
        out.append(
            f'<g class="fspmark" pointer-events="none">'
            f'<title>{tip}</title>'
            f'<circle cx="{cx:.1f}" cy="{cy:.1f}" r="{r:.1f}" fill="none" '
            f'stroke="#ff00ff" stroke-width="{sw:.1f}"/>'
            f'<line x1="{cx - r * 1.8:.1f}" y1="{cy:.1f}" '
            f'x2="{cx + r * 1.8:.1f}" y2="{cy:.1f}" '
            f'stroke="#ff00ff" stroke-width="{sw:.1f}"/>'
            f'<line x1="{cx:.1f}" y1="{cy - r * 1.8:.1f}" '
            f'x2="{cx:.1f}" y2="{cy + r * 1.8:.1f}" '
            f'stroke="#ff00ff" stroke-width="{sw:.1f}"/>'
            f'<text x="{cx + r * 2.1:.1f}" y="{cy - r * 0.6:.1f}" '
            f'font-size="{fs:.0f}" fill="#ff00ff" stroke="#000" '
            f'stroke-width="{sw * 0.8:.1f}" paint-order="stroke">'
            f'{_h.escape(label)}</text>'
            f'</g>'
        )
    return "".join(out), notes
