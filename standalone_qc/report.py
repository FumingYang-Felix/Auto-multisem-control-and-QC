"""Output for one scored region, written to `<region>/StandaloneQC/`.

Files written:
  standalone_qc_results.csv       fully-scored tiles, in live_qc_results.csv's
                                  own column order (so anything that already
                                  reads that format reads this too)
  standalone_qc_results.jsonl     same rows, full nested per-beam detail
  standalone_qc_probe.csv         the map pass, one row per mFoV (the
                                  filename is historical - see score.py)
  standalone_qc_summary.json      machine-readable run summary
  standalone_qc_report.html       self-contained page, no external assets

We write our own HTML rather than calling live_qc_support._write_dashboard_html:
that function is private, needs live matplotlib cmap/norm objects built inside
the viewer process, and lives in a shared file in another clone we've agreed
not to depend on beyond its public API. The page here is deliberately plain
(inline SVG, no JS libraries, no matplotlib) so it renders anywhere and can't
break on a missing dependency.
"""
from __future__ import annotations

import csv
import html
import json
import math
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from live_qc_support import CSV_COLUMNS
from qc_algorithm import quality_tier

from . import focus_planes, fsp_timing, plot3d, portable_paths, scan_checks
from . import overview_fast
from .overview_plan import DEFAULT_MAX_MEGAPIXELS, plan_overview_ds
from .score import RegionScore

# Tier colors, matching the red/amber/green convention the live dashboard
# used for the same three quality_tier() buckets.
_TIER_FILL = {"red": "#c0392b", "amber": "#d68910", "green": "#2e8b3d"}
_BLANK_FILL = "#d9d9d9"
_TISSUE_FILL = "#eaf0f6"
_ERROR_FILL = "#8e44ad"

PROBE_COLUMNS = [
    "tile_idx", "x_um", "y_um", "probe_beam", "quality",
    "pct_saturation_med", "std_all_med", "mean_intensity_med",
    "jitter_score_med", "is_blank_outside_roi", "status_category",
]


def output_dir(score: RegionScore, cfg: Dict[str, Any]) -> Path:
    return score.region_dir / str(cfg.get("output_subdir", "StandaloneQC"))


# ---------------------------------------------------------------------------
# tabular output
# ---------------------------------------------------------------------------
def _write_results_csv(path: Path, scored: Sequence[Dict[str, Any]]) -> None:
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(CSV_COLUMNS)
        for r in scored:
            w.writerow([
                r.get("tile_idx"), r.get("x_um"), r.get("y_um"), r.get("quality"),
                r.get("ol_pred"), r.get("stig_x_pred"), r.get("stig_y_pred"),
                r.get("stig_x_med"), r.get("stig_y_med"), r.get("stig_anisotropy"),
                r.get("pct_saturation_med"), r.get("pct_dark_med"),
                r.get("pct_bright_med"), r.get("std_all_med"),
                r.get("jitter_score_med"), r.get("mean_intensity_med"),
                int(bool(r.get("is_blank_outside_roi", False))),
                int(bool(r.get("is_jittery", False))),
                r.get("n_images_used"),
                json.dumps(r.get("selected_beams", []), ensure_ascii=False),
                r.get("status"), r.get("status_category"), r.get("failed_images"),
                r.get("tile_folder"),
            ])


def _write_probe_csv(path: Path, probe: Dict[int, Dict[str, Any]]) -> None:
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(PROBE_COLUMNS)
        for tile_idx in sorted(probe):
            r = probe[tile_idx]
            beams = r.get("selected_beams") or []
            w.writerow([
                tile_idx, r.get("x_um"), r.get("y_um"),
                beams[0] if beams else "",
                r.get("quality"), r.get("pct_saturation_med"),
                r.get("std_all_med"), r.get("mean_intensity_med"),
                r.get("jitter_score_med"),
                int(bool(r.get("is_blank_outside_roi", False))),
                r.get("status_category"),
            ])


def _write_jsonl(path: Path, scored: Sequence[Dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8") as f:
        for r in scored:
            f.write(json.dumps(r, ensure_ascii=False, default=str) + "\n")


def _imqa(score: RegionScore) -> Dict[str, Any]:
    """Section image quality as mean ± std over every mFoV, matching the Zen
    Report's IMQA column (e.g. "17.1 ± 10.3").

    Computed over the MAP pass - every mFoV - not over the handful given the
    all-beam treatment, because the spread across a section is the point: a
    good mean with a large std means part of the section is out of focus.

    NEGATIVE mFoVs ARE EXCLUDED. Quality is a contrast ratio minus one, so a
    tile with no tissue in it - and these sections are imaged deliberately
    wide, out past the tissue, so the surface can be seen - scores below
    zero on empty resin. On S12R5 the blank detector caught 6 of 399 tiles
    while 49 scored negative, and including them moved the section from
    27.0 +/- 13.0 to 21.4 +/- 19.2: the mean dropped and the spread nearly
    doubled, which reads as "part of this section is out of focus" when what
    it really means is "part of this section is not tissue".

    The cost of this is real and is why the count is kept: a BADLY defocused
    tile also scores negative, so those are dropped here too. They are rare
    (the operator's judgement, and consistent with 49 negatives forming a
    border ring rather than a scatter), but `imqa_n_negative` is reported so
    a section where that number jumps is still visible, and the
    all-inclusive figures are kept beside it rather than thrown away.
    """
    import numpy as np
    vals = [
        float(r.get("quality", 0.0))
        for r in score.results_by_tile.values()
        if r.get("status") == "ok" and not r.get("is_blank_outside_roi")
    ]
    if not vals:
        return {"imqa_mean": None, "imqa_std": None, "imqa_n": 0,
                "imqa_n_negative": 0, "imqa_mean_all": None,
                "imqa_std_all": None, "imqa_n_all": 0}
    arr = np.asarray(vals, dtype=np.float64)
    pos = arr[arr >= 0.0]

    def _mean_std(a):
        if not a.size:
            return None, None
        return float(a.mean()), (float(a.std(ddof=1)) if a.size > 1 else 0.0)

    m, sd = _mean_std(pos)
    m_all, sd_all = _mean_std(arr)
    return {
        "imqa_mean": m,
        "imqa_std": sd,
        "imqa_n": int(pos.size),
        "imqa_n_negative": int(arr.size - pos.size),
        "imqa_mean_all": m_all,
        "imqa_std_all": sd_all,
        "imqa_n_all": int(arr.size),
    }


def build_summary(
    score: RegionScore, cfg: Dict[str, Any], fingerprint: str,
) -> Dict[str, Any]:
    scored_q = [
        float(r.get("quality", -100.0)) for r in score.scored
        if r.get("status") == "ok"
    ]
    return {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "producer": "standalone_qc",
        "policy_fingerprint": fingerprint,
        "block": score.block_label,
        "region": score.region_name,
        "region_folder": score.region_folder,
        "region_dir": str(score.region_dir),
        # Operator vocabulary alongside the internal keys above, which stay
        # for compatibility with anything already reading this file.
        "sample": score.sample_id,
        "run": score.run_label,
        "batch": score.block_label,
        "section": score.region_name,
        "image_dir": str(score.region_dir.parent),
        "layout_source": score.layout_source,
        "n_tiles": score.n_tiles,
        "n_blank_tiles": score.n_blank,
        "n_tissue_tiles": len(score.tissue_tiles),
        # Counted over every mFoV (the map pass), so these are section-wide
        # rather than a property of the few all-beam mFoVs.
        "n_jittery": sum(
            1 for r in score.results_by_tile.values() if r.get("is_jittery")
        ),
        "n_failed_mfovs": sum(
            1 for r in score.results_by_tile.values()
            if str(r.get("status_category", "ok")) not in ("ok", "outside_roi")
        ),
        "tile_selection": score.selection_reason,
        "scored_tile_idxs": [r.get("tile_idx") for r in score.scored],
        "n_images_read": score.n_images_read,
        "elapsed_s": round(score.elapsed_s, 2),
        "quality_median": (
            float(sorted(scored_q)[len(scored_q) // 2]) if scored_q else None
        ),
        "outlier_beams": [b for b in score.beam_summary if b.get("is_outlier")],
        "n_beams_seen": len(score.beam_summary),
        "overview": score.overview,
        # Passed through from the acquisition side's live_qc_timing.json, not
        # measured here. qc_catchup_* is the regression canary for "is
        # acquisition still waiting on its own QC subprocesses".
        "acq_timing": {
            k: score.acq_timing.get(k) for k in (
                "wall_s", "focus_s", "move_s", "scan_s", "storage_s", "n_tiles",
                "qc_catchup_s", "qc_catchup_worker_s", "qc_catchup_viewer_s",
            ) if score.acq_timing.get(k) is not None
        },
        "acq_live_qc_mode": score.live_qc_mode,
        **_imqa(score),
        # One sFoV per mFoV checked for scan faults; skew is measured but
        # deliberately not given a verdict - see scan_checks.py.
        "scan_checks": scan_checks.summarize(score.scan_checks),
        "overlap": score.overlap,
        "plane_fits": score.plane_fits,
        "facts": score.facts,
        "errors": score.errors,
        "notes": score.notes,
        "config": {
            k: cfg.get(k) for k in (
                "dsamp", "aggregate_mode", "beam_selection_mode", "tile_sampling",
                "tile_sampling_every_n", "tissue_probe_enabled", "tissue_probe_beam",
                "low_quality_threshold", "high_quality_threshold",
            )
        },
    }


# ---------------------------------------------------------------------------
# region overview (optional, expensive)
# ---------------------------------------------------------------------------
def render_region_overview(
    score: RegionScore, cfg: Dict[str, Any],
    on_progress: Optional[Any] = None,
) -> Optional[Dict[str, Any]]:
    """Build a downsampled overview of the whole region by PLACING each
    beam image at its recorded coordinate.

    Not stitching: nothing is registered, correlated or aligned. Each
    image is pasted where image_coordinates.txt says it belongs, scaled
    by overview_ds, and overlapping pixels keep the darker value. A beam
    recorded at the wrong coordinate is placed confidently in the wrong
    place, and the result still looks plausible.

    Expensive and deliberately opt-in: RegionOverviewAccumulator.add_mfov()
    reads EVERY beam image of every mfov it's given, so this costs the full
    region read (~75 GB for a 236-tile region) regardless of how few tiles
    were scored. That cost is the reason it's a separate flag rather than
    part of every run.

    Best-effort: returns None (never raises) if qc_overview or PIL can't do
    it, so a failed overview can't lose us a completed scoring pass.
    """
    try:
        from qc_overview import RegionOverviewAccumulator
    except Exception as exc:
        score.errors.append(f"overview: qc_overview unavailable ({exc})")
        return None

    out_dir = output_dir(score, cfg)
    out_dir.mkdir(parents=True, exist_ok=True)
    layout = [
        {"tile_idx": t.tile_idx, "x_um": t.x_um, "y_um": t.y_um}
        for t in score.tiles
    ]
    # Thumbnails first. Acquisition writes a 500x437 .png beside every
    # beam, and at the downsample an overview uses they carry every pixel
    # the result keeps - 8.8x faster and 20x less data off the share, for a
    # byte-identical-resolution output. See overview_fast.py.
    if bool(cfg.get("overview_use_thumbnails", True)):
        try:
            info = overview_fast.build(score.tiles, out_dir, cfg, on_progress)
        except Exception as exc:
            score.errors.append(
                f"fast overview failed ({type(exc).__name__}: {exc}); "
                f"falling back to full-resolution images")
            info = None
        if info:
            score.notes.append(
                f"overview from {info['source']} at ds={info['ds']} "
                f"-> {info['width']}x{info['height']} px")
            return info
        score.notes.append(
            "no usable thumbnails; building the overview from the "
            "full-resolution images instead")

    # overview_ds is a fixed divisor, so the canvas grows with the SECTION.
    # At the configured 16 a 397-mFoV section stitches to 671 Mpx, which the
    # browser refuses to decode - the operator then sees a blurry, misaligned
    # background under a correctly-drawn hex overlay. Raise ds when needed.
    ds, ds_info = plan_overview_ds(score.tiles, cfg)
    if ds_info.get("note"):
        score.notes.append(ds_info["note"])
    cfg = dict(cfg, overview_ds=ds)

    # Report progress per mFoV. This loop reads every beam of every mFoV -
    # minutes on a large section - and used to run completely silently, so
    # the longest phase of an analysis was the one with nothing on screen.
    progress = on_progress or (lambda done, total, label: None)
    total = len(score.tiles)
    try:
        acc = RegionOverviewAccumulator(layout, out_dir, cfg)
        for i, tile in enumerate(score.tiles, start=1):
            progress(i, total, f"overview: reading mFoV {tile.tile_idx}")
            acc.add_mfov(tile.tile_idx, tile.tile_folder)
        # render() composites the whole canvas in one call inside the shared
        # module, so there is no progress to report from within it - say so
        # rather than going quiet again at the end.
        progress(total, total, "overview: compositing the placed images")
        info = acc.render()
    except Exception as exc:
        score.errors.append(f"overview: {type(exc).__name__}: {exc}")
        return None

    # Belt and braces: if the stitch still came out over budget (an extent
    # bigger than the coordinate files implied), say so rather than shipping
    # a page whose overview silently will not load.
    if info:
        w, h = int(info.get("width") or 0), int(info.get("height") or 0)
        cap = float(cfg.get("overview_max_megapixels",
                            DEFAULT_MAX_MEGAPIXELS)) * 1e6
        if cap > 0 and w * h > cap * 1.5:
            score.errors.append(
                f"overview is {w}x{h} px ({w * h / 1e6:.0f} Mpx) even at "
                f"ds={ds}; browsers stop decoding around 268 Mpx, so this "
                f"one may not display. Raise overview_ds."
            )
    return info


# ---------------------------------------------------------------------------
# HTML
# ---------------------------------------------------------------------------
def _estimate_radius(pts: Sequence[Tuple[float, float]]) -> float:
    """Half the median nearest-neighbour spacing - a marker size that just
    touches its neighbours at the real tile pitch."""
    if len(pts) < 2:
        return 50.0
    nearest: List[float] = []
    for i, (x0, y0) in enumerate(pts):
        best = None
        for j, (x1, y1) in enumerate(pts):
            if i == j:
                continue
            d = math.hypot(x1 - x0, y1 - y0)
            if d > 1e-9 and (best is None or d < best):
                best = d
        if best is not None:
            nearest.append(best)
    if not nearest:
        return 50.0
    return 0.5 * sorted(nearest)[len(nearest) // 2]


def _tile_map_svg(score: RegionScore, cfg: Dict[str, Any], width: int = 860) -> str:
    """Inline SVG of the region's tiles in stage coordinates.

    Y is negated so the picture matches the orientation matplotlib gave the
    live dashboard (SVG's y grows downward, stage y does not). This is stage
    micron space throughout - deliberately NOT image_coordinates.txt pixel
    space, which is a different frame (X inverted relative to stage, per the
    handoff's coordinate-system note).
    """
    if not score.tiles:
        return "<p class='muted'>No mFoVs to plot.</p>"

    low = float(cfg.get("low_quality_threshold", 10.0))
    high = float(cfg.get("high_quality_threshold", 30.0))
    pts = [(t.x_um, t.y_um) for t in score.tiles]
    r_um = _estimate_radius(pts)

    xs = [p[0] for p in pts]
    ys = [-p[1] for p in pts]
    min_x, max_x = min(xs) - r_um, max(xs) + r_um
    min_y, max_y = min(ys) - r_um, max(ys) + r_um
    span_x = max(max_x - min_x, 1e-6)
    span_y = max(max_y - min_y, 1e-6)
    scale = width / span_x
    height = max(120, int(span_y * scale))

    scored_by_idx = {int(r["tile_idx"]): r for r in score.scored}

    def _fill(tile_idx: int) -> str:
        full = scored_by_idx.get(tile_idx)
        if full is not None:
            if full.get("status_category") == "error":
                return _ERROR_FILL
            return _TIER_FILL.get(
                quality_tier(float(full.get("quality", -100.0)), low, high), _ERROR_FILL
            )
        probe = score.probe_by_tile.get(tile_idx, {})
        if probe.get("status_category") == "error":
            return _ERROR_FILL
        if probe.get("is_blank_outside_roi"):
            return _BLANK_FILL
        return _TISSUE_FILL

    parts = [
        f"<svg viewBox='0 0 {width} {height}' width='100%' "
        f"style='max-width:{width}px' role='img' "
        f"aria-label='Section mFoV map coloured by QC status'>"
    ]
    r_px = max(2.0, r_um * scale)
    for tile in score.tiles:
        cx = (tile.x_um - min_x) * scale
        cy = (-tile.y_um - min_y) * scale
        is_scored = tile.tile_idx in scored_by_idx
        full = scored_by_idx.get(tile.tile_idx)
        probe = score.probe_by_tile.get(tile.tile_idx, {})
        q = full.get("quality") if full else probe.get("quality")
        q_txt = f"{float(q):.2f}" if isinstance(q, (int, float)) and not (
            isinstance(q, float) and math.isnan(q)
        ) else "n/a"
        tip = (
            f"mFoV {tile.tile_idx} | x={tile.x_um:.0f} y={tile.y_um:.0f} um | "
            f"quality={q_txt} | "
            f"{'ALL-BEAM ANALYZED' if is_scored else 'probe only'}"
        )
        parts.append(
            f"<circle cx='{cx:.1f}' cy='{cy:.1f}' r='{r_px:.1f}' "
            f"fill='{_fill(tile.tile_idx)}' "
            f"stroke='{'#111' if is_scored else '#b9c4d0'}' "
            f"stroke-width='{2.0 if is_scored else 0.5}'>"
            f"<title>{html.escape(tip)}</title></circle>"
        )
    parts.append("</svg>")
    return "".join(parts)


def _legend_html() -> str:
    items = [
        (_TIER_FILL["green"], "analyzed: good"),
        (_TIER_FILL["amber"], "analyzed: marginal"),
        (_TIER_FILL["red"], "analyzed: low"),
        (_TISSUE_FILL, "tissue, probe only"),
        (_BLANK_FILL, "blank / outside ROI"),
        (_ERROR_FILL, "error"),
    ]
    chips = "".join(
        f"<span class='chip'><i style='background:{c}'></i>{html.escape(t)}</span>"
        for c, t in items
    )
    return f"<div class='legend'>{chips}</div>"


def _table(headers: Sequence[str], rows: Sequence[Sequence[Any]],
           row_classes: Optional[Sequence[str]] = None) -> str:
    head = "".join(f"<th>{html.escape(str(h))}</th>" for h in headers)
    body = []
    for i, row in enumerate(rows):
        cls = f" class='{row_classes[i]}'" if row_classes and row_classes[i] else ""
        cells = "".join(f"<td>{html.escape(str(c))}</td>" for c in row)
        body.append(f"<tr{cls}>{cells}</tr>")
    return (
        f"<div class='scroll'><table><thead><tr>{head}</tr></thead>"
        f"<tbody>{''.join(body)}</tbody></table></div>"
    )


def _fmt(val: Any, nd: int = 2) -> str:
    if isinstance(val, bool):
        return "yes" if val else "no"
    if isinstance(val, (int, float)):
        f = float(val)
        if math.isnan(f):
            return "n/a"
        return f"{f:.{nd}f}"
    return "n/a" if val is None else str(val)


def _focus_planes_html(score: RegionScore, cfg: Dict[str, Any]) -> str:
    """OL / StigX / StigY against (x, y): a plane fit with its RMS and
    max-min span, plus reference-minus-measured when a fiducial file covers
    the region."""
    try:
        fits, fid = focus_planes.analyze_section(
            score.region_dir, cfg, score.sample_id
        )
    except Exception as exc:
        return f"<p class='muted'>Focus planes unavailable: {html.escape(str(exc))}</p>"
    if not any(f.n for f in fits):
        return "<p class='muted'>No per-mFoV metadata.txt to fit planes to.</p>"

    blocks = []
    for f in fits:
        if not f.n:
            blocks.append(
                f"<div class='plane'><strong>{html.escape(f.label)}</strong>"
                f"<div class='muted'>no data</div></div>"
            )
            continue
        if f.degenerate:
            blocks.append(
                f"<div class='plane'><strong>{html.escape(f.label)}</strong>"
                f"<div class='muted'>"
                f"{html.escape(focus_planes.degenerate_reason(f))}</div></div>"
            )
            continue

        # OL in microns of focus rather than the raw uA readback; every
        # scalar below is scaled with it so the table matches the plot axis.
        d = focus_planes.to_display_units(f)
        u = f" {d.unit}" if d.unit else ""
        rows = [
            ("source", f.source),
            ("n mFoVs", f.n),
            ("max − min", _fmt(f.span * d.scale, 5) + u),
            ("RMS about plane", _fmt(f.rms * d.scale, 5) + u),
            ("residual max − min", _fmt(f.residual_span * d.scale, 5) + u),
        ]
        if d.plane:
            a, b, c = d.plane
            rows.append(("plane", f"{a:.3e}·x + {b:.3e}·y + {c:.5g}"))
        if d.note:
            rows.append(("units", d.note))
        g = f.gradient or {}
        if g:
            rows += [
                ("slope", f"{g['grad_um_per_mm']:.4g} µm/mm"),
                ("max-gradient direction", f"{g['azimuth_deg']:.1f}° from +x"),
                ("plane vs xy angle", f"{g['tilt_deg']:.4g}°"),
            ]
        stats = "".join(
            f"<tr><th>{html.escape(str(k))}</th><td>{html.escape(str(v))}</td></tr>"
            for k, v in rows
        )
        note = (
            f"<div class='muted' style='font-size:11px;'>"
            f"{html.escape(f.source_note)}</div>" if f.source_note else ""
        )
        # Measured values in 3D with the fitted plane drawn through them.
        # The vertical axis is auto-ranged to the data, so a 0.0023 um span
        # still fills the axis.
        svg = plot3d.scatter3d_html(
            d.points, f.label, d.unit, plane=d.plane,
            labels=("x (µm)", "y (µm)", d.axis_label),
            point_labels=d.tips,
            plot_id=f"p3d-rep-{score.region_folder}-{f.quantity}",
        )
        ref_svg = ""
        if f.ref_available and f.ref_points:
            ref_pts = [(x, y, v * d.scale) for x, y, v in f.ref_points]
            ref_svg = plot3d.scatter3d_html(
                ref_pts, f"reference − measured {f.label}", d.unit,
                labels=("x (µm)", "y (µm)",
                        f"difference{(' (' + d.unit + ')') if d.unit else ''}"),
                point_labels=[f"mFoV {i + 1}" for i in range(len(ref_pts))],
                plot_id=f"p3d-rep-{score.region_folder}-{f.quantity}-ref",
            )
            stats += (
                f"<tr><th>ref − meas mean</th>"
                f"<td>{_fmt(f.ref_mean * d.scale, 5)}{u}</td></tr>"
                f"<tr><th>ref − meas RMS</th>"
                f"<td>{_fmt(f.ref_rms * d.scale, 5)}{u}</td></tr>"
                f"<tr><th>ref − meas max − min</th>"
                f"<td>{_fmt(f.ref_span * d.scale, 5)}{u}</td></tr>"
            )
        blocks.append(
            f"<div class='plane'><strong>{html.escape(f.label)}</strong>{note}"
            f"<table class='meta'>{stats}</table>{svg}{ref_svg}</div>"
        )

    fid_line = (
        f"<p class='muted'>Reference fiducial file: <code>"
        f"{html.escape(str(fid))}</code></p>" if fid else
        "<p class='muted'>No reference fiducial file found for this region, "
        "so no reference-minus-measured plots.</p>"
    )
    body = fid_line + f"<div class='planes'>{''.join(blocks)}</div>"
    if "class='p3d'" in body:
        body += plot3d.engine_html()
    return body


def _fsp_timing_html(score: RegionScore) -> str:
    """Region AF / AS / B2F / stage-move bars, then the per-support-point
    breakdown underneath. See fsp_timing.region_steps for why the two are
    charted separately instead of sharing one axis."""
    facts = score.facts or {}
    fsps = facts.get("fsp_timings") or []
    agg = facts.get("aggregate") or {}
    out = fsp_timing.region_bars_html(
        facts, region=score.region_name or "region", width=680, height=300
    )
    if not fsps:
        return out + ("<p class='muted'>No focus support point found in this "
                      "section's console.log.</p>")
    tot = agg.get("focus_stig_s")
    head = ""
    if tot is not None:
        acc = sum(r.get("total_s") or 0.0 for r in fsps)
        head = (
            f"<p class='muted'>Section focus/stigmation total "
            f"{float(tot):.1f}s; the steps timed below account for "
            f"{acc:.1f}s of it.</p>"
        )
    return out + head + fsp_timing.bars_html(fsps, width=680)


def _write_html(path: Path, score: RegionScore, cfg: Dict[str, Any],
                summary: Dict[str, Any]) -> None:
    low = float(cfg.get("low_quality_threshold", 10.0))
    high = float(cfg.get("high_quality_threshold", 30.0))

    # Operator-facing vocabulary, which is NOT the code's internal naming:
    #   Sample  = the wafer, e.g. "w10h31"
    #   Run     = the folder holding batches + the wafer-level QC report
    #   Batch   = one image-acquisition folder for that wafer
    #   Section = what the code calls a region
    #   mFoV    = what the code calls a tile
    # Only display strings use these words - CSV column names, JSON summary
    # keys and every internal identifier keep the code's own terms, so
    # nothing that reads our output files has to change and the shared
    # modules stay compatible.
    meta_rows = [
        ("Sample", score.sample_id or "—"),
        ("Run", score.run_label or "—"),
        ("Batch", score.block_label),
        ("Section", score.region_name),
        ("Image directory", str(score.region_dir.parent)),
        ("mFoVs in section", score.n_tiles),
        ("Blank / outside ROI", f"{score.n_blank} (probe pass)"),
        ("mFoVs fully analyzed", f"{len(score.scored)} - {score.selection_reason}"),
        ("Beam selection", cfg.get("beam_selection_mode")),
        ("Beams seen", summary.get("n_beams_seen")),
        ("Images read", score.n_images_read),
        ("Elapsed", f"{score.elapsed_s:.1f} s"),
        ("Layout source", {
            "live_qc_csv": "LiveQC/live_qc_results.csv (commanded values)",
            "metadata": "mfov*/metadata.txt (measured readback)",
            "none": "none",
        }.get(score.layout_source, score.layout_source)),
        ("Quality thresholds", f"low &lt; {low:g}, green &ge; {high:g}"),
        ("Generated", summary.get("generated_at")),
    ]
    # Deliberately NOT shown here: the policy fingerprint (an internal config
    # hash the ledger uses to decide when a re-score is needed - still in
    # standalone_qc_summary.json), and the acquisition side's own imaging /
    # QC-catch-up timings. Those last two are numbers from the ACQUISITION
    # machine, not this one; there is no catch-up on the standalone system by
    # design, and showing acquisition's copy next to our results just invited
    # the question "whose number is this?". They stay in the summary JSON, and
    # the only actionable case - acquisition still burning minutes on live QC
    # despite lightweight mode - surfaces as a warning instead.
    meta_html = "".join(
        f"<tr><th>{html.escape(str(k))}</th><td>{v if k == 'Quality thresholds' else html.escape(str(v))}</td></tr>"
        for k, v in meta_rows
    )

    warn_html = ""
    if score.errors:
        items = "".join(f"<li>{html.escape(e)}</li>" for e in score.errors)
        warn_html = f"<div class='warn'><strong>Warnings</strong><ul>{items}</ul></div>"
    if score.notes:
        items = "".join(f"<li>{html.escape(n)}</li>" for n in score.notes)
        warn_html += f"<div class='note'><strong>Notes</strong><ul>{items}</ul></div>"

    scored_rows, scored_cls = [], []
    for r in sorted(score.scored, key=lambda d: int(d.get("tile_idx", 0))):
        q = float(r.get("quality", -100.0))
        tier = quality_tier(q, low, high) if r.get("status") == "ok" else "red"
        outliers = ", ".join(
            str(o.get("beam_index")) for o in (r.get("outlier_beams") or [])
        ) or "-"
        scored_rows.append([
            r.get("tile_idx"), _fmt(q), r.get("n_images_used"),
            _fmt(r.get("stig_x_med")), _fmt(r.get("stig_y_med")),
            _fmt(r.get("stig_anisotropy")), _fmt(r.get("jitter_score_med"), 3),
            _fmt(r.get("pct_saturation_med")), _fmt(r.get("std_all_med")),
            _fmt(r.get("ol_pred"), 3), r.get("status_category"), outliers,
        ])
        scored_cls.append(f"tier-{tier}")
    scored_html = _table(
        ["mFoV", "quality", "beams", "stig X", "stig Y", "anisotropy",
         "jitter", "% sat", "std", "OL cmd", "status", "outlier beams"],
        scored_rows, scored_cls,
    ) if scored_rows else "<p class='muted'>No mFoVs were fully analyzed.</p>"

    beam_rows, beam_cls = [], []
    for b in score.beam_summary:
        beam_rows.append([
            b["beam_index"], b["n_tiles"], _fmt(b["quality_median"]),
            _fmt(b["quality_min"]), _fmt(b["quality_max"]),
            "OUTLIER" if b["is_outlier"] else "",
            _fmt(b["deviation"]) if b["is_outlier"] else "",
        ])
        beam_cls.append("outlier" if b["is_outlier"] else "")
    beam_html = _table(
        ["beam", "mFoVs", "median quality", "min", "max", "flag", "deviation"],
        beam_rows, beam_cls,
    ) if beam_rows else "<p class='muted'>No per-beam data.</p>"

    detail_blocks = []
    for r in sorted(score.scored, key=lambda d: int(d.get("tile_idx", 0))):
        rows = [
            [img.get("beam_index"), _fmt(img.get("quality")),
             _fmt(img.get("horz_qual")), _fmt(img.get("vert_qual")),
             _fmt(img.get("jitter_score"), 3), _fmt(img.get("percent_saturation")),
             _fmt(img.get("mean_intensity")), img.get("status")]
            for img in sorted(
                r.get("image_results", []),
                key=lambda d: d.get("beam_index") or 0,
            )
        ]
        detail_blocks.append(
            f"<details><summary>mFoV {r.get('tile_idx')} - "
            f"{len(rows)} beam(s)</summary>"
            + _table(["beam", "quality", "stig X", "stig Y", "jitter",
                      "% sat", "mean I", "status"], rows)
            + "</details>"
        )

    ov_html = ""
    if score.overview and score.overview.get("path"):
        ov_name = Path(str(score.overview["path"])).name
        ov_html = (
            "<h2>Section overview</h2>"
            f"<p class='muted'>{score.overview.get('width')} x "
            f"{score.overview.get('height')} px, downsample "
            f"{cfg.get('overview_ds')}x</p>"
            f"<img src='{html.escape(ov_name)}' alt='Region overview'>"
        )

    doc = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Standalone QC - {html.escape(score.region_name)}</title>
<style>
  :root {{ color-scheme: light; }}
  body {{ margin:0; padding:24px; font:14px/1.5 -apple-system,Segoe UI,Roboto,sans-serif;
         background:#f7f8fa; color:#1a1d21; }}
  .wrap {{ max-width:1100px; margin:0 auto; }}
  h1 {{ font-size:20px; margin:0 0 4px; }}
  h2 {{ font-size:16px; margin:28px 0 8px; }}
  .sub {{ color:#5b6570; margin:0 0 20px; }}
  .card {{ background:#fff; border:1px solid #e2e6ea; border-radius:8px;
           padding:16px; margin-bottom:18px; }}
  table {{ border-collapse:collapse; width:100%; font-size:13px; }}
  th,td {{ text-align:left; padding:5px 9px; border-bottom:1px solid #eceff2;
           white-space:nowrap; }}
  thead th {{ background:#f2f4f7; font-weight:600; }}
  .scroll {{ overflow-x:auto; }}
  .meta th {{ width:190px; color:#5b6570; font-weight:500; background:none; }}
  .muted {{ color:#7a828c; }}
  .warn {{ background:#fff6e5; border:1px solid #f0d090; border-radius:6px;
           padding:10px 14px; margin-bottom:18px; }}
  .warn ul,.note ul {{ margin:6px 0 0 18px; padding:0; }}
  .note {{ background:#eef4fb; border:1px solid #b8cfe6; border-radius:6px;
           padding:10px 14px; margin-bottom:18px; }}
  .legend {{ margin:10px 0 0; display:flex; flex-wrap:wrap; gap:14px;
             font-size:12px; color:#5b6570; }}
  .chip {{ display:flex; align-items:center; gap:6px; }}
  .chip i {{ width:12px; height:12px; border-radius:3px;
             border:1px solid rgba(0,0,0,.15); display:inline-block; }}
  tr.tier-red td:nth-child(2) {{ color:#c0392b; font-weight:600; }}
  tr.tier-amber td:nth-child(2) {{ color:#a56a00; font-weight:600; }}
  tr.tier-green td:nth-child(2) {{ color:#1f6f2c; font-weight:600; }}
  tr.outlier {{ background:#fdeaea; }}
  tr.outlier td:nth-child(6) {{ color:#c0392b; font-weight:700; }}
  details {{ margin:6px 0; }}
  summary {{ cursor:pointer; padding:4px 0; font-weight:500; }}
  img {{ max-width:100%; border:1px solid #e2e6ea; border-radius:6px; }}
  .planes {{ display:flex; flex-wrap:wrap; gap:18px; }}
  .plane {{ flex:1 1 440px; min-width:330px; }}
  .plane table.meta {{ margin:6px 0; }}
  .plane table.meta th {{ width:150px; font-size:11px; }}
  .plane table.meta td {{ font-size:11px; }}
</style></head><body><div class="wrap">
<h1>Standalone QC - {html.escape(score.region_name)}</h1>
<p class="sub">Sample {html.escape(score.sample_id or "—")} &middot; batch
{html.escape(score.block_label)} &middot; scored on this machine from images
on disk, independently of acquisition.</p>
{warn_html}
<div class="card"><table class="meta">{meta_html}</table></div>
<div class="card"><h2 style="margin-top:0">mFoV map</h2>
{_tile_map_svg(score, cfg)}
{_legend_html()}</div>
<div class="card"><h2 style="margin-top:0">Fully analyzed mFoVs</h2>
{scored_html}</div>
<div class="card"><h2 style="margin-top:0">Per-beam quality across analyzed mFoVs</h2>
<p class="muted">Median quality of each beam over every fully-analyzed mFoV. A
beam flagged OUTLIER is consistently unlike the others - a hardware
suspicion, not mFoV-to-mFoV noise. This is what all-beam coverage is for.</p>
{beam_html}</div>
<div class="card"><h2 style="margin-top:0">Focus &amp; stigmation planes</h2>
<p class="muted">Each mFoV's value against its stage position, with a
least-squares plane fitted and drawn through the points. Drag a plot to rotate
it; points are coloured by their residual about the plane, so a tilt common to
the whole section does not hide the local variation.</p>
{_focus_planes_html(score, cfg)}</div>
<div class="card"><h2 style="margin-top:0">Focus support point timing</h2>
<p class="muted">What establishing the focus/stigmation plane cost, split into
stage move, B2F alignment, autofocus (AF) and autostig (AS), for each support
point.</p>
{_fsp_timing_html(score)}</div>
<div class="card"><h2 style="margin-top:0">Per-beam detail</h2>
{''.join(detail_blocks) or "<p class='muted'>No detail.</p>"}</div>
{f'<div class="card">{ov_html}</div>' if ov_html else ''}
</div></body></html>
"""
    # UNC rather than this machine's drive letter, so the page is
    # usable from any computer that can reach the share.
    path.write_text(portable_paths.apply(doc, cfg), encoding="utf-8")


# ---------------------------------------------------------------------------
# entry point
# ---------------------------------------------------------------------------
def write_region_report(
    score: RegionScore, cfg: Dict[str, Any], fingerprint: str,
) -> Dict[str, Path]:
    """Write every output file for one scored region. Returns {name: path}."""
    out_dir = output_dir(score, cfg)
    out_dir.mkdir(parents=True, exist_ok=True)

    summary = build_summary(score, cfg, fingerprint)
    written: Dict[str, Path] = {}

    results_csv = out_dir / "standalone_qc_results.csv"
    _write_results_csv(results_csv, score.scored)
    written["results_csv"] = results_csv

    results_jsonl = out_dir / "standalone_qc_results.jsonl"
    _write_jsonl(results_jsonl, score.scored)
    written["results_jsonl"] = results_jsonl

    if score.probe_by_tile:
        probe_csv = out_dir / "standalone_qc_probe.csv"
        _write_probe_csv(probe_csv, score.probe_by_tile)
        written["probe_csv"] = probe_csv

    summary_json = out_dir / "standalone_qc_summary.json"
    summary_json.write_text(
        json.dumps(summary, indent=2, default=str), encoding="utf-8"
    )
    written["summary_json"] = summary_json

    report_html = out_dir / "standalone_qc_report.html"
    _write_html(report_html, score, cfg, summary)
    written["report_html"] = report_html

    return written
