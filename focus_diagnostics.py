"""
Automatic, best-effort post-run diagnostics for focus_map.py.

Generates a single self-contained interactive HTML page (pan/zoom/hover,
opens in any browser, no Python needed to view it) comparing, for every
tile actually imaged in a run:

  - expected OL (the per-region plane focus_map.py actually commanded,
    parsed from that region's own console.log) vs. measured OL (the
    hardware 'Lens' readback in each tile's metadata.txt)
  - expected stage Z (from a fresh, uncorrected fit of the wafer-fiducial
    file - deliberately NOT anything a drift-precheck may have shifted,
    same principle as estimate_known_good_z_from_wafer_fiducial in
    focus_map.py) vs. measured stage Z

This module is standalone (no import of focus_map.py) and every public
entry point swallows its own exceptions - it must never be able to break
or block a real acquisition run. It exists because the w10h31_220526
Z-fault investigation had to be reconstructed by hand, region by region,
after the fact; this makes that same picture available immediately after
every run finishes, without anyone having to ask for it.
"""
import csv
import glob
import json
import os
import re
from typing import Dict, List, Optional, Tuple

# OL-current-to-focus conversion, for DISPLAY only - the underlying
# ol_pred/lens/ol_measured values in the tiles list and the saved JSON dump
# stay in the hardware's native uA; only the HTML plot's axes/hover text are
# converted to microns, a physically meaningful unit. Kept in sync by
# comment only with focus_map.py's _OL_UA_PER_UM and live_qc_support.py's
# copy of the same constant (same empirical fit - see calibrate_ua_um.py /
# ua_um_calibration_fit.json); this module deliberately never imports
# focus_map (comtypes/hardware deps), so the value is duplicated, not shared.
_OL_UA_PER_UM = 119.262

def _new_plane_re(quantity: str) -> re.Pattern:
    return re.compile(
        rf"\[NewPlane\]\s+{re.escape(quantity)}\s*=\s*([-+0-9.eE]+)\*x\s*\+\s*([-+0-9.eE]+)\*y\s*\+\s*([-+0-9.eE]+)"
    )


# Every quantity focus_map.py's console.log can print a "[NewPlane] <name> ="
# line for, and the metadata.txt field each one is compared against. OL is
# handled separately (needs a calibration-offset fit first, see
# _calibration_offset) - these four are directly comparable as-is.
_PLANE_QUANTITIES = {
    "stigx": ("StigX", "stigx"),
    "stigy": ("StigY", "stigy"),
    "projx": ("ProjX", "projx"),
    "projy": ("ProjY", "projy"),
}
_NEW_PLANE_OL_RE = _new_plane_re("OL")
_NEW_PLANE_RES = {key: _new_plane_re(label) for key, (label, _field) in _PLANE_QUANTITIES.items()}

_METADATA_FIELD_RE = re.compile(r"^([^:]+):\s*(.+)$")

_METADATA_KEYS = {
    "Stage pos. X": "x",
    "Stage pos. Y": "y",
    "Stage pos. Z": "z",
    "Lens": "lens",
    "Stigmator Tilt X": "stigx",
    "Stigmator Tilt Y": "stigy",
    "Projective X": "projx",
    "Projective Y": "projy",
}


def _to_float(raw: str) -> Optional[float]:
    m = re.match(r"^\s*(-?[0-9.]+(?:[eE][-+]?[0-9]+)?)", raw)
    return float(m.group(1)) if m else None


def _read_metadata(path: str) -> Optional[Dict[str, float]]:
    out: Dict[str, float] = {}
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                m = _METADATA_FIELD_RE.match(line.strip())
                if not m:
                    continue
                key, raw = m.group(1).strip(), m.group(2).strip()
                field = _METADATA_KEYS.get(key)
                if field is None:
                    continue
                val = _to_float(raw)
                if val is not None:
                    out[field] = val
    except OSError:
        return None
    return out if "x" in out and "y" in out and "z" in out else None


def _parse_region_planes(console_log_path: str) -> Dict[str, Optional[Tuple[float, float, float]]]:
    """One pass over a region's console.log, returning whichever
    '[NewPlane] <name> = ...' planes it actually printed: 'ol' (required by
    the caller) plus any of 'stigx'/'stigy'/'projx'/'projy' that were also
    logged - a region using WAFER_FIDUCIAL_DIRECT prints only OL/StigX/StigY,
    for instance, so the latter two are frequently absent and that's fine."""
    out: Dict[str, Optional[Tuple[float, float, float]]] = {"ol": None}
    out.update({k: None for k in _NEW_PLANE_RES})
    remaining = {"ol": _NEW_PLANE_OL_RE, **_NEW_PLANE_RES}
    try:
        with open(console_log_path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                if not remaining:
                    break
                for key, rx in list(remaining.items()):
                    m = rx.search(line)
                    if m:
                        out[key] = (float(m.group(1)), float(m.group(2)), float(m.group(3)))
                        del remaining[key]
    except OSError:
        return out
    return out


def _fit_plane(points: List[Tuple[float, float, float]]):
    import numpy as np

    if len(points) < 3:
        return None
    xs = np.array([p[0] for p in points])
    ys = np.array([p[1] for p in points])
    zs = np.array([p[2] for p in points])
    A = np.column_stack([xs, ys, np.ones_like(xs)])
    coeffs, *_ = np.linalg.lstsq(A, zs, rcond=None)
    return float(coeffs[0]), float(coeffs[1]), float(coeffs[2])


def _parse_fiducial_z_plane(path: str) -> Optional[Tuple[float, float, float]]:
    """Fresh fit of Z = a*x + b*y + c straight from the wafer-fiducial file,
    independent of anything a live run may have derived/corrected from it -
    this is the 'known good' reference the diagnostic plots against."""
    if not path or not os.path.isfile(path):
        return None
    points: List[Tuple[float, float, float]] = []
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            reader = csv.reader(f)
            rows = list(reader)
    except OSError:
        return None
    if not rows:
        return None
    for row in rows[1:]:  # skip header
        if len(row) < 3:
            continue
        try:
            x, y, z = float(row[0]), float(row[1]), float(row[2])
        except ValueError:
            continue
        points.append((x, y, z))
    return _fit_plane(points)


def _load_live_qc_quality(region_dir: str) -> Dict[str, float]:
    """folder-basename -> quality, from <region_dir>/LiveQC/live_qc_results.csv."""
    out: Dict[str, float] = {}
    csv_path = os.path.join(region_dir, "LiveQC", "live_qc_results.csv")
    if not os.path.isfile(csv_path):
        return out
    try:
        with open(csv_path, "r", encoding="utf-8", errors="replace", newline="") as f:
            for row in csv.DictReader(f):
                folder = (row.get("tile_folder") or "").replace("\\", "/").rstrip("/")
                if not folder:
                    continue
                basename = folder.rsplit("/", 1)[-1]
                try:
                    out[basename] = float(row["quality"])
                except (KeyError, ValueError):
                    continue
    except OSError:
        pass
    return out


def collect_run_tiles(block_folder: str) -> List[Dict]:
    """Walk every region subfolder of a run's output folder and return one
    dict per imaged tile: region, mfov, x, y, z, lens, ol_pred, quality
    (quality is None if LiveQC has no row for that tile), plus
    <quantity>_pred/<quantity>_meas for each of stigx/stigy/projx/projy that
    this region's console.log logged a plane for (None otherwise - see
    _parse_region_planes)."""
    tiles: List[Dict] = []
    for entry in sorted(os.listdir(block_folder)):
        region_dir = os.path.join(block_folder, entry)
        console_log = os.path.join(region_dir, "console.log")
        if not os.path.isdir(region_dir) or not os.path.isfile(console_log):
            continue

        planes = _parse_region_planes(console_log)
        if planes["ol"] is None:
            print(f"[FocusDiagnostics] {entry}: no [NewPlane] OL line in console.log - skipping region")
            continue
        a_ol, b_ol, c_ol = planes["ol"]
        quality_by_folder = _load_live_qc_quality(region_dir)

        for mfov_name in sorted(os.listdir(region_dir)):
            mfov_dir = os.path.join(region_dir, mfov_name)
            meta_path = os.path.join(mfov_dir, "metadata.txt")
            if not os.path.isdir(mfov_dir) or not os.path.isfile(meta_path):
                continue
            meta = _read_metadata(meta_path)
            if meta is None:
                continue
            tile = {
                "region": entry,
                "mfov": mfov_name,
                "x": meta["x"], "y": meta["y"], "z": meta["z"],
                "lens": meta.get("lens"),
                "ol_pred": a_ol * meta["x"] + b_ol * meta["y"] + c_ol,
                "quality": quality_by_folder.get(mfov_name),
            }
            for key, (_label, field) in _PLANE_QUANTITIES.items():
                plane = planes.get(key)
                if plane is not None:
                    a, b, c = plane
                    tile[f"{key}_pred"] = a * meta["x"] + b * meta["y"] + c
                else:
                    tile[f"{key}_pred"] = None
                tile[f"{key}_meas"] = meta.get(field)
            tiles.append(tile)
    return tiles


def _calibration_offset(tiles: List[Dict]) -> Optional[float]:
    have = [t for t in tiles if t.get("lens") is not None]
    if not have:
        return None
    return sum(t["lens"] - t["ol_pred"] for t in have) / len(have)


_IDENTITY_PANEL_SPECS = [
    # (quantity key, display label, unit)
    ("stigx", "StigX", ""),
    ("stigy", "StigY", ""),
    ("projx", "ProjX", ""),
    ("projy", "ProjY", ""),
]


def _build_html(
    tiles: List[Dict], run_name: str,
    z_plane: Optional[Tuple[float, float, float]],
) -> str:
    import plotly.graph_objects as go

    offset = _calibration_offset(tiles)
    for t in tiles:
        t["ol_measured"] = (t["lens"] - offset) if (offset is not None and t.get("lens") is not None) else None
        if z_plane is not None:
            az, bz, cz = z_plane
            t["z_expected"] = az * t["x"] + bz * t["y"] + cz
        else:
            t["z_expected"] = None

    xs = [t["x"] for t in tiles]
    ys = [t["y"] for t in tiles]
    # Displayed in microns (physically meaningful), converted from the raw
    # uA the tiles list/JSON keep - see _OL_UA_PER_UM above.
    ol_pred = [t["ol_pred"] / _OL_UA_PER_UM for t in tiles]
    have_meas = all(t["ol_measured"] is not None for t in tiles) and bool(tiles)
    ol_meas = [t["ol_measured"] / _OL_UA_PER_UM for t in tiles] if have_meas else None
    failed = [(q is not None and q <= -99) for q in [t["quality"] for t in tiles]]
    # Each point below is ONE IMAGED MFOV TILE: "expected"/"predicted" =
    # this region's plane (see [NewPlane] in that region's console.log)
    # evaluated at this tile's (x, y); "measured" = the hardware readback
    # from that tile's own metadata.txt. Spelled out in the hover text since
    # that's the one thing carried onto every single marker.
    hover = [f"{t['region']} / {t['mfov']} (one imaged mfov tile)<br>x={t['x']:.0f}, y={t['y']:.0f} um"
             for t in tiles]
    has_z_expected = z_plane is not None

    # Panels are laid out by hand (not via make_subplots) - mixing 'scene'
    # (3D) and 'xy' (2D) subplots in one make_subplots row gave each type a
    # different vertical domain/title placement (the 2D panel sat visibly
    # higher than the two 3D scenes) and left too little horizontal gap for
    # a scene's z-axis label to render without being cut off by its
    # neighbor. Explicit, identical domains for every panel in a row - scene
    # and xy alike - sidesteps both.
    row1: List[Dict] = []
    if have_meas:
        row1.append({"kind": "scene", "title": "Expected vs measured OL (3D)"})
    if has_z_expected:
        row1.append({"kind": "scene", "title": "Expected vs measured stage Z (3D)"})
    if have_meas:
        row1.append({"kind": "xy", "title": "Expected vs measured OL (identity check)",
                      "label": "OL", "pred": ol_pred, "meas": ol_meas, "unit": "um"})

    row2: List[Dict] = []
    for key, label, unit in _IDENTITY_PANEL_SPECS:
        pred = [t[f"{key}_pred"] for t in tiles]
        meas = [t[f"{key}_meas"] for t in tiles]
        if tiles and all(p is not None for p in pred) and all(m is not None for m in meas):
            row2.append({"kind": "xy", "title": f"Expected vs measured {label}",
                          "label": label, "pred": pred, "meas": meas, "unit": unit})

    rows = [r for r in (row1, row2) if r]
    n_rows = len(rows)
    row_gap = 0.10
    row_height = (0.88 - row_gap * (n_rows - 1)) / n_rows if n_rows else 0.88

    fig = go.Figure()
    annotations = []
    scene_count = 0
    axis_count = 0

    def _surface(a, b, c, color):
        import numpy as np
        gx = np.linspace(min(xs), max(xs), 12)
        gy = np.linspace(min(ys), max(ys), 12)
        GX, GY = np.meshgrid(gx, gy)
        GZ = a * GX + b * GY + c
        return go.Surface(x=GX, y=GY, z=GZ, showscale=False, opacity=0.25,
                           colorscale=[[0, color], [1, color]])

    for row_idx, row_panels in enumerate(rows):
        row_top = 0.88 - row_idx * (row_height + row_gap)
        row_bottom = row_top - row_height
        y_domain = [row_bottom, row_top]
        title_y = row_top + 0.025

        n = len(row_panels)
        gap = 0.06
        width = (1.0 - gap * (n - 1)) / n if n else 1.0

        for i, panel in enumerate(row_panels):
            x0 = i * (width + gap)
            x_domain = [x0, x0 + width]
            annotations.append(dict(
                text=panel["title"], showarrow=False, xref="paper", yref="paper",
                x=x0 + width / 2, y=title_y, xanchor="center", yanchor="bottom",
                font=dict(size=13),
            ))

            if panel["kind"] == "scene":
                scene_count += 1
                scene_key = "scene" if scene_count == 1 else f"scene{scene_count}"
                scene_ref = scene_key
                is_ol_scene = panel["title"].startswith("Expected vs measured OL")

                zaxis_kwargs: Dict[str, object] = dict(
                    # Plotly's default SI-prefix tick format (e.g. "38.860k")
                    # rounds away exactly the precision that matters when the
                    # range is tiny (a well-behaved run can have sub-micron Z
                    # spread around ~38900 um) - every tick then rounds to
                    # the same displayed text and they overlap. Plain
                    # fixed-point, fewer ticks, sidesteps that regardless of
                    # the data's range.
                    tickformat=".3f", exponentformat="none", nticks=6,
                )
                if not is_ol_scene:
                    # This wafer's real stage-Z is often near-constant
                    # (sub-micron spread across the whole run, sometimes
                    # literally identical to machine precision) - Plotly's
                    # autorange on a near-zero-span axis produced an empty
                    # panel (just overlapping tick labels, no visible scatter
                    # or surface at all). An explicit, generously-padded
                    # range keeps the axis well-defined regardless of how
                    # flat the data actually is.
                    z_all_vals = [t["z"] for t in tiles] + [t["z_expected"] for t in tiles]
                    z_lo, z_hi = min(z_all_vals), max(z_all_vals)
                    z_span = z_hi - z_lo
                    z_pad = z_span * 0.2 if z_span > 1e-6 else 1.0
                    zaxis_kwargs["range"] = [z_lo - z_pad, z_hi + z_pad]

                fig.update_layout(**{scene_key: dict(
                    domain=dict(x=x_domain, y=y_domain),
                    xaxis_title="X (um)", yaxis_title="Y (um)",
                    # aspectmode="cube" forces X/Y/Z into equal PHYSICAL cube
                    # proportions regardless of each axis's own data range -
                    # fine for OL (a healthy fraction of X/Y's span), but
                    # breaks the Z scene: forcing a near-zero-span Z axis
                    # into the same cube as a tens-of-thousands-of-um X/Y
                    # span left the scene degenerate (empty/unreadable).
                    # "auto" sizes each axis sensibly instead.
                    aspectmode="cube" if is_ol_scene else "auto",
                    zaxis=zaxis_kwargs,
                )})

                if is_ol_scene:
                    fig.add_trace(go.Scatter3d(
                        x=xs, y=ys, z=ol_pred, mode="markers", name="expected OL", scene=scene_ref,
                        marker=dict(size=3, color="#3b82f6"), text=hover,
                        hovertemplate="%{text}<br>expected OL=%{z:.3f} um<extra></extra>",
                    ))
                    fig.add_trace(go.Scatter3d(
                        x=xs, y=ys, z=ol_meas, mode="markers", name="measured OL", scene=scene_ref,
                        marker=dict(size=3, color="#f59e0b", symbol="diamond"), text=hover,
                        hovertemplate="%{text}<br>measured OL=%{z:.3f} um<extra></extra>",
                    ))
                    fig.update_layout(**{f"{scene_key}_zaxis_title": "OL (um)"})
                else:
                    z_meas = [t["z"] for t in tiles]
                    z_exp = [t["z_expected"] for t in tiles]
                    ok_idx = [j for j, f in enumerate(failed) if not f]
                    bad_idx = [j for j, f in enumerate(failed) if f]
                    fig.add_trace(go.Scatter3d(
                        x=[xs[j] for j in ok_idx], y=[ys[j] for j in ok_idx], z=[z_meas[j] for j in ok_idx],
                        mode="markers", name="measured Z, succeeded", scene=scene_ref,
                        marker=dict(size=3, color="#16a34a"),
                        text=[hover[j] for j in ok_idx], hovertemplate="%{text}<br>measured Z=%{z:.1f} um<extra></extra>",
                    ))
                    fig.add_trace(go.Scatter3d(
                        x=[xs[j] for j in bad_idx], y=[ys[j] for j in bad_idx], z=[z_meas[j] for j in bad_idx],
                        mode="markers", name="measured Z, FAILED", scene=scene_ref,
                        marker=dict(size=3, color="#dc2626"),
                        text=[hover[j] for j in bad_idx], hovertemplate="%{text}<br>measured Z=%{z:.1f} um (FAILED)<extra></extra>",
                    ))
                    fig.add_trace(go.Scatter3d(
                        x=xs, y=ys, z=z_exp, mode="markers", name="expected Z (fiducial)", scene=scene_ref,
                        marker=dict(size=2, color="black", symbol="cross"),
                        text=hover, hovertemplate="%{text}<br>expected Z=%{z:.1f} um<extra></extra>",
                    ))
                    az, bz, cz = z_plane
                    surf = _surface(az, bz, cz, "#888888")
                    surf.update(scene=scene_ref)
                    fig.add_trace(surf)
                    fig.update_layout(**{f"{scene_key}_zaxis_title": "Z (um)"})

            else:  # 2D identity check
                axis_count += 1
                xaxis_key = "xaxis" if axis_count == 1 else f"xaxis{axis_count}"
                yaxis_key = "yaxis" if axis_count == 1 else f"yaxis{axis_count}"
                xaxis_ref = "x" if axis_count == 1 else f"x{axis_count}"
                yaxis_ref = "y" if axis_count == 1 else f"y{axis_count}"
                unit_sfx = f" ({panel['unit']})" if panel["unit"] else ""
                pred, meas = panel["pred"], panel["meas"]
                fig.update_layout(**{
                    xaxis_key: dict(domain=x_domain, anchor=yaxis_ref, title=f"Expected {panel['label']}{unit_sfx}"),
                    yaxis_key: dict(domain=y_domain, anchor=xaxis_ref, title=f"Measured {panel['label']}{unit_sfx}",
                                     scaleanchor=xaxis_ref),
                })
                vmin, vmax = min(min(pred), min(meas)), max(max(pred), max(meas))
                ok_idx = [j for j, f in enumerate(failed) if not f]
                bad_idx = [j for j, f in enumerate(failed) if f]
                fig.add_trace(go.Scatter(
                    x=[pred[j] for j in ok_idx], y=[meas[j] for j in ok_idx], mode="markers",
                    name="succeeded", marker=dict(size=6, color="#16a34a"), showlegend=(axis_count == 1),
                    xaxis=xaxis_ref, yaxis=yaxis_ref,
                    text=[hover[j] for j in ok_idx], hovertemplate="%{text}<br>expected=%{x:.3f}<br>measured=%{y:.3f}<extra></extra>",
                ))
                fig.add_trace(go.Scatter(
                    x=[pred[j] for j in bad_idx], y=[meas[j] for j in bad_idx], mode="markers",
                    name="failed (LiveQC -100)", marker=dict(size=6, color="#dc2626"), showlegend=(axis_count == 1),
                    xaxis=xaxis_ref, yaxis=yaxis_ref,
                    text=[hover[j] for j in bad_idx], hovertemplate="%{text}<br>expected=%{x:.3f}<br>measured=%{y:.3f}<extra></extra>",
                ))
                fig.add_trace(go.Scatter(
                    x=[vmin, vmax], y=[vmin, vmax], mode="lines", name="y = x",
                    line=dict(color="black", dash="dash"), showlegend=(axis_count == 1),
                    xaxis=xaxis_ref, yaxis=yaxis_ref,
                ))

    n_failed = sum(failed)
    fig.update_layout(
        title=dict(text=f"Focus diagnostics — {run_name}  ({len(tiles)} tiles, {n_failed} failed)", y=0.995),
        annotations=annotations,
        height=650 if n_rows <= 1 else 1050,
        margin=dict(l=20, r=20, t=90, b=20),
    )
    return fig.to_html(include_plotlyjs="inline", full_html=True)


_PRECHECK_QUANTITIES = [
    # (pred key, meas key, display label, unit)
    ("ol_pred", "ol_meas", "OL", "uA"),
    ("stigx_pred", "stigx_meas", "StigX", ""),
    ("stigy_pred", "stigy_meas", "StigY", ""),
    ("projx_pred", "projx_meas", "ProjX", ""),
    ("projy_pred", "projy_meas", "ProjY", ""),
]


def _build_precheck_html(points: List[Dict], run_name: str) -> str:
    """One identity-check scatter (fiducial-predicted vs. freshly-measured,
    y=x reference line) per quantity in _PRECHECK_QUANTITIES that has usable
    data for every point - all panels are the same 'xy' subplot type (unlike
    generate_focus_diagnostics, which mixes in 3D scenes), so a plain
    make_subplots row works without the domain-alignment issues that needed
    hand-rolled domains there."""
    import plotly.graph_objects as go
    from plotly.subplots import make_subplots

    hover = [f"x={p['x']:.0f}, y={p['y']:.0f} um" for p in points]

    panels = []
    for pred_key, meas_key, label, unit in _PRECHECK_QUANTITIES:
        pred = [p.get(pred_key) for p in points]
        meas = [p.get(meas_key) for p in points]
        if points and all(v is not None and v == v for v in pred) and all(v is not None and v == v for v in meas):
            panels.append((label, unit, pred, meas))

    if not panels:
        fig = go.Figure()
        fig.update_layout(title=f"Precheck diagnostics — {run_name} (no usable data)")
        return fig.to_html(include_plotlyjs="inline", full_html=True)

    fig = make_subplots(
        rows=1, cols=len(panels),
        subplot_titles=[f"Fiducial vs. fresh {label} (n={len(points)})" for label, *_ in panels],
        horizontal_spacing=0.06,
    )

    for i, (label, unit, pred, meas) in enumerate(panels, start=1):
        unit_sfx = f" ({unit})" if unit else ""
        vmin, vmax = min(min(pred), min(meas)), max(max(pred), max(meas))
        pad = (vmax - vmin) * 0.05 or 1.0
        lims = [vmin - pad, vmax + pad]
        fig.add_trace(go.Scatter(
            x=pred, y=meas, mode="markers", name=label, showlegend=False,
            marker=dict(size=9, color="#2563eb"),
            text=hover, hovertemplate="%{text}<br>fiducial=%{x:.4f}<br>measured=%{y:.4f}<extra></extra>",
        ), row=1, col=i)
        fig.add_trace(go.Scatter(
            x=lims, y=lims, mode="lines", name="y = x", showlegend=False,
            line=dict(color="black", dash="dash"),
        ), row=1, col=i)
        fig.update_xaxes(title_text=f"Fiducial-predicted {label}{unit_sfx}", row=1, col=i)
        fig.update_yaxes(title_text=f"Freshly measured {label}{unit_sfx}", row=1, col=i,
                          scaleanchor=f"x{i}" if i > 1 else "x")

    fig.update_layout(
        title=f"Precheck diagnostics — {run_name}: fiducial vs. freshly-measured at every point "
              f"(only OL feeds the plane actually used for imaging - the rest are comparison-only)",
        height=480, margin=dict(l=60, r=30, t=110, b=60),
    )
    return fig.to_html(include_plotlyjs="inline", full_html=True)


def generate_precheck_diagnostics(
    points: List[Dict],
    run_name: str,
    diagnostics_dir: Optional[str] = None,
) -> Optional[str]:
    """Best-effort: write the SAMPLE_TOPOGRAPHY_CORRECTION precheck's own
    fiducial-vs-freshly-measured scatter plots (OL, StigX, StigY, ProjX,
    ProjY) to <diagnostics_dir>/<run_name>/precheck_diagnostics.html, plus a
    JSON dump of the raw per-point data. `points` is the list of dicts
    precheck_and_correct_wafer_fiducial_planes() builds (x, y, and
    <quantity>_pred/<quantity>_meas for ol/stigx/stigy/projx/projy). Returns
    the HTML path, or None if nothing could be written (never raises)."""
    try:
        if not points:
            print("[FocusDiagnostics] precheck: no usable measurements - skipping diagnostics")
            return None

        out_dir = diagnostics_dir or r"D:\Users\Vlad\Section _detection-files\diagnostics"
        run_dir = os.path.join(out_dir, run_name)
        os.makedirs(run_dir, exist_ok=True)

        html = _build_precheck_html(points, run_name)
        html_path = os.path.join(run_dir, "precheck_diagnostics.html")
        with open(html_path, "w", encoding="utf-8") as f:
            f.write(html)

        json_path = os.path.join(run_dir, "precheck_diagnostics.json")
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump({"run": run_name, "points": points}, f, indent=1)

        print(f"[FocusDiagnostics] wrote {html_path} ({len(points)} precheck point(s))")
        return html_path
    except Exception as exc:
        print(f"[FocusDiagnostics] WARNING: precheck diagnostics generation failed: {exc}")
        return None


def generate_focus_diagnostics(
    block_folder: str,
    wafer_fiducial_file: Optional[str] = None,
    diagnostics_dir: Optional[str] = None,
) -> Optional[str]:
    """Best-effort: parse everything this run actually wrote to disk and
    generate an interactive HTML + a JSON dump of the underlying data under
    <diagnostics_dir>/<run folder name>/. Returns the HTML path, or None if
    diagnostics couldn't be generated for any reason (never raises)."""
    try:
        run_name = os.path.basename(os.path.normpath(block_folder))
        tiles = collect_run_tiles(block_folder)
        if not tiles:
            print("[FocusDiagnostics] no tiles with usable metadata.txt found - skipping")
            return None

        z_plane = _parse_fiducial_z_plane(wafer_fiducial_file) if wafer_fiducial_file else None
        if wafer_fiducial_file and z_plane is None:
            print(f"[FocusDiagnostics] could not fit a Z plane from {wafer_fiducial_file} "
                  f"- expected-Z panel will be omitted")

        out_dir = diagnostics_dir or r"D:\Users\Vlad\Section _detection-files\diagnostics"
        run_dir = os.path.join(out_dir, run_name)
        os.makedirs(run_dir, exist_ok=True)

        html = _build_html(tiles, run_name, z_plane)
        html_path = os.path.join(run_dir, "focus_diagnostics.html")
        with open(html_path, "w", encoding="utf-8") as f:
            f.write(html)

        json_path = os.path.join(run_dir, "focus_diagnostics.json")
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump({"run": run_name, "tiles": tiles, "z_plane": z_plane}, f, indent=1)

        print(f"[FocusDiagnostics] wrote {html_path} ({len(tiles)} tiles)")
        return html_path
    except Exception as exc:
        print(f"[FocusDiagnostics] WARNING: diagnostics generation failed: {exc}")
        return None


if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        print("usage: python focus_diagnostics.py <block_folder> [wafer_fiducial_file] [diagnostics_dir]")
        sys.exit(1)
    generate_focus_diagnostics(
        sys.argv[1],
        wafer_fiducial_file=sys.argv[2] if len(sys.argv) > 2 else None,
        diagnostics_dir=sys.argv[3] if len(sys.argv) > 3 else None,
    )
