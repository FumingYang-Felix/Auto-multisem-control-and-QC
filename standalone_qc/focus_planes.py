"""Focus / stigmation planes across a section: OL, StigX, StigY vs (x, y).

For every mFoV in a section this reads the hardware READBACK from that
mFoV's own `metadata.txt` - the value the column actually settled at, not the
value the pipeline commanded - and for each of OL, StigX and StigY:

  * fits a least-squares plane  v = a*x + b*y + c
  * reports the RMS of the residuals about that plane
  * reports the max-min span, both of the raw values and of the residuals
  * if a reference fiducial file covers this region, evaluates the
    reference plane at each mFoV and reports (reference - measured)

Why the readback and not the commanded value: the commanded numbers lie on
the commanded plane by construction, so fitting a plane to them and quoting
an RMS would always return ~0 and measure nothing. The readback is what can
actually deviate.

The fiducial-file parser here is a reimplementation of
focus_map._parse_wafer_fiducial_points rather than an import - focus_map
pulls in comtypes and must never be imported on this machine. It keeps that
function's one hard-won behaviour: when a header row is present, columns are
looked up BY NAME, never by position. An 8-column header once sat over
6-column rows in a real fiducial file and a positional parser silently read
projective_x/y as stigx/stigy.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np

from .layout import read_metadata

# Quantities plotted, in report order: (key, label, unit-if-commanded).
#
# No unit is claimed for a readback: metadata.txt's "Lens" field reads around
# 2,087,224 on the same instrument whose commanded OL is about 2,655 uA, so
# the two are on different scales and labelling the readback "uA" would be
# wrong. Spans and RMS are still meaningful as relative deviations.
QUANTITIES = [
    ("ol", "OL", "µA"),
    ("stig_x", "StigX", ""),
    ("stig_y", "StigY", ""),
]

# metadata.txt field -> fiducial-file column for the same quantity.
_FIDUCIAL_COL = {"ol": "ol", "stig_x": "stigx", "stig_y": "stigy"}

_HEADER_ALIASES = {
    "x": "x", "xum": "x", "x(um)": "x",
    "y": "y", "yum": "y", "y(um)": "y",
    "z": "z", "zum": "z", "z(um)": "z",
    "ol": "ol", "olum": "ol", "ol(um)": "ol", "olua": "ol",
    "stigmatorx": "stigx", "stigx": "stigx", "stigmatorx(%)": "stigx",
    "stigmatory": "stigy", "stigy": "stigy", "stigmatory(%)": "stigy",
    "projectivex": "projx", "projx": "projx",
    "projectivey": "projy", "projy": "projy",
}


def _norm_token(tok: str) -> str:
    t = re.sub(r"\s+", "", tok.strip().lower())
    return _HEADER_ALIASES.get(t, _HEADER_ALIASES.get(re.sub(r"\(.*?\)", "", t), t))


@dataclass
class PlaneFit:
    quantity: str
    label: str
    unit: str = ""
    # "readback" (metadata.txt) or "commanded" (LiveQC CSV) - see
    # _choose_source for why it is not always the readback.
    source: str = "readback"
    source_note: str = ""
    n: int = 0
    plane: Optional[Tuple[float, float, float]] = None
    rms: Optional[float] = None
    span: Optional[float] = None           # max-min of the measured values
    residual_span: Optional[float] = None  # max-min of the residuals
    points: List[Tuple[float, float, float]] = field(default_factory=list)
    # reference - measured, when a fiducial file covers this region
    ref_available: bool = False
    ref_rms: Optional[float] = None
    ref_span: Optional[float] = None
    ref_mean: Optional[float] = None
    ref_points: List[Tuple[float, float, float]] = field(default_factory=list)
    # Slope magnitude and the two angles - OL only, since the conversion to
    # microns is an OL calibration and stig has no such unit.
    gradient: Dict[str, float] = field(default_factory=dict)

    @property
    def degenerate(self) -> bool:
        """True when the values carry no variation, so the fitted plane
        describes rounding rather than the instrument. Reporting an RMS of
        6e-18 as though it were a measurement would be worse than saying the
        data cannot support one."""
        return bool(self.n) and (self.span is None or self.span <= _FLAT_SPAN_EPS)

    @property
    def extent_um(self) -> Dict[str, float]:
        """Bounding box of the fitted points, in stage microns.

        A slope alone cannot be judged: 1.3e-4 um/mm is negligible across a
        3 mm section and would matter across a wafer. Carrying the extent
        lets a reader turn the slope into focus change without going back to
        the points.
        """
        if not self.points:
            return {}
        xs = [p[0] for p in self.points]
        ys = [p[1] for p in self.points]
        return {"x": max(xs) - min(xs), "y": max(ys) - min(ys)}

    def as_dict(self) -> Dict[str, Any]:
        return {
            "quantity": self.quantity, "label": self.label, "unit": self.unit,
            "source": self.source, "source_note": self.source_note,
            "degenerate": self.degenerate,
            "n": self.n,
            "plane": list(self.plane) if self.plane else None,
            "rms": self.rms, "span": self.span,
            "residual_span": self.residual_span,
            "ref_available": self.ref_available,
            "ref_rms": self.ref_rms, "ref_span": self.ref_span,
            "ref_mean": self.ref_mean,
            "extent_um": self.extent_um,
            "gradient": self.gradient,
        }


# ---------------------------------------------------------------------------
# measured values
# ---------------------------------------------------------------------------
def gather_measured(section_dir: str | Path) -> List[Dict[str, float]]:
    """One record per mFoV: {x, y, ol, stig_x, stig_y} from metadata.txt.

    One small text read per mFoV - about a second for a 230-mFoV section, and
    no image data.
    """
    section_dir = Path(section_dir)
    out: List[Dict[str, float]] = []
    try:
        subs = sorted(
            p for p in section_dir.iterdir()
            if p.is_dir() and p.name.lower().startswith("mfov")
        )
    except OSError:
        return out
    for sub in subs:
        meta = read_metadata(sub / "metadata.txt")
        if "x" not in meta or "y" not in meta:
            continue
        rec: Dict[str, float] = {"x": meta["x"], "y": meta["y"]}
        for key in ("ol", "stig_x", "stig_y"):
            if key in meta:
                rec[key] = meta[key]
        out.append(rec)
    return out


# ---------------------------------------------------------------------------
# plane fitting
# ---------------------------------------------------------------------------
def fit_plane(
    xs: Sequence[float], ys: Sequence[float], vs: Sequence[float]
) -> Optional[Tuple[float, float, float]]:
    """Least-squares v = a*x + b*y + c. None if under-determined."""
    if len(xs) < 3:
        return None
    A = np.column_stack([np.asarray(xs, float), np.asarray(ys, float),
                         np.ones(len(xs))])
    try:
        coef, *_ = np.linalg.lstsq(A, np.asarray(vs, float), rcond=None)
    except np.linalg.LinAlgError:
        return None
    if not np.all(np.isfinite(coef)):
        return None
    return float(coef[0]), float(coef[1]), float(coef[2])


def _eval_plane(plane: Tuple[float, float, float], x: float, y: float) -> float:
    a, b, c = plane
    return a * x + b * y + c


# OL current to focus distance. Same empirical fit as focus_map._OL_UA_PER_UM,
# live_qc_support and focus_diagnostics (see calibrate_ua_um.py /
# ua_um_calibration_fit.json). Duplicated rather than imported because
# focus_map pulls in comtypes; kept in sync by comment, as those three
# already are.
OL_UA_PER_UM = 119.262


def gradient_stats(
    plane: Tuple[float, float, float], ua_per_um: float = OL_UA_PER_UM
) -> Dict[str, float]:
    """Decompose an OL plane into slope magnitude and two angles.

    The plane is v = a*x + b*y + c with x, y in stage microns and v in OL
    microamps, so (a, b) is the gradient in uA per micron. Verified against
    the pipeline's own commanded plane: fitting the metadata.txt Lens
    readback reproduces focus_map's logged `[NewPlane] OL = a*x + b*y + c`
    coefficients to within 0.5% across three batches, differing only by a
    constant offset of about +2,084,877 in c. So the readback is uA with an
    offset, and its gradient needs no rescaling.

    Returns:
      grad_ua_per_um   |(a, b)|, the raw fitted gradient
      grad_um_per_mm   the same converted to microns of focus per mm of
                       lateral travel - the operator-facing slope
      azimuth_deg      direction of steepest ascent in the xy plane,
                       measured from +x counter-clockwise, in [0, 360)
      tilt_deg         dihedral angle between the fitted plane and the xy
                       plane. Computed from the DIMENSIONLESS micron-per-
                       micron gradient, because an angle is only geometric
                       once both axes are the same unit.
    """
    a, b, _ = plane
    grad_ua = math.hypot(a, b)
    grad_um_per_um = grad_ua / ua_per_um if ua_per_um else float("nan")
    return {
        "grad_ua_per_um": grad_ua,
        "grad_um_per_mm": grad_um_per_um * 1000.0,
        "azimuth_deg": math.degrees(math.atan2(b, a)) % 360.0,
        "tilt_deg": math.degrees(math.atan(grad_um_per_um)),
    }


# ---------------------------------------------------------------------------
# reference fiducial file
# ---------------------------------------------------------------------------
def parse_fiducial_file(path: str | Path) -> List[Dict[str, Optional[float]]]:
    """x,y,z,ol[,stigx,stigy,projx,projy] points. Header row optional; when
    present, columns are matched BY NAME - see the module docstring."""
    path = Path(path)
    try:
        lines = [ln.strip() for ln in path.read_text(
            encoding="utf-8-sig", errors="replace").splitlines() if ln.strip()]
    except OSError:
        return []
    if not lines:
        return []

    tokens0 = [_norm_token(t) for t in lines[0].split(",")]
    if tokens0 and tokens0[0] == "x":
        col = {name: i for i, name in enumerate(tokens0)}
        if any(k not in col for k in ("x", "y")):
            return []
        data = lines[1:]
    else:
        # Legacy positional layout.
        col = {"x": 0, "y": 1, "z": 2, "ol": 3}
        data = lines

    out: List[Dict[str, Optional[float]]] = []
    need = max(col.values())
    for line in data:
        parts = [p.strip() for p in line.split(",")]
        if len(parts) <= need:
            # Declared a column the row does not have - skip rather than
            # shift every field left and misread the quantities.
            continue
        rec: Dict[str, Optional[float]] = {}
        ok = True
        for name, idx in col.items():
            try:
                rec[name] = float(parts[idx])
            except (ValueError, IndexError):
                if name in ("x", "y"):
                    ok = False
                    break
                rec[name] = None
        if ok:
            out.append(rec)
    return out


def find_fiducial_file(
    section_dir: Path, sample_id: Optional[str], cfg: Dict[str, Any]
) -> Optional[Path]:
    """Locate a reference fiducial file for this region, or None.

    Precedence: an explicit `fiducial_file` in config; then any
    `fiducial_dir` searched for a name mentioning this sample; then a
    `fiducials.txt` sitting beside the section or its batch. Returning None
    is normal - the difference plots are simply omitted.
    """
    explicit = cfg.get("fiducial_file")
    if explicit and Path(explicit).is_file():
        return Path(explicit)

    d = cfg.get("fiducial_dir")
    if d and Path(d).is_dir() and sample_id:
        cands = [p for p in Path(d).rglob("*.txt")
                 if sample_id.lower() in str(p).lower()]
        if cands:
            return sorted(cands)[0]

    for base in (section_dir, section_dir.parent, section_dir.parent.parent):
        for name in ("fiducials.txt", "region_fiducials.txt"):
            p = base / name
            if p.is_file():
                return p
    return None


# ---------------------------------------------------------------------------
# the whole job for one section
# ---------------------------------------------------------------------------
def gather_commanded(section_dir: str | Path) -> List[Dict[str, float]]:
    """Per-mFoV COMMANDED values from LiveQC/live_qc_results.csv.

    Needed because the readback is not always usable: metadata.txt quantises
    the stigmator to three decimals, while the real per-mFoV variation is
    around 1e-5 - so a readback-based stig plane is pure rounding noise. The
    commanded values carry that variation.
    """
    from .layout import from_live_qc_csv
    out: List[Dict[str, float]] = []
    for t in from_live_qc_csv(Path(section_dir)):
        rec: Dict[str, float] = {"x": t.x_um, "y": t.y_um}
        if t.ol_pred is not None:
            rec["ol"] = t.ol_pred
        if t.stig_x_pred is not None:
            rec["stig_x"] = t.stig_x_pred
        if t.stig_y_pred is not None:
            rec["stig_y"] = t.stig_y_pred
        out.append(rec)
    return out


# A readback whose full span is below this is flat to the file's own
# precision, so a plane fitted to it describes rounding, not the instrument.
_FLAT_SPAN_EPS = 1e-9


def _choose_source(
    key: str,
    readback: List[Dict[str, float]],
    commanded: List[Dict[str, float]],
) -> Tuple[List[Tuple[float, float, float]], str, str]:
    """(points, source, note) for one quantity.

    Prefers the readback - it is what the column actually settled at, and the
    only source that can deviate from the commanded plane. Falls back to the
    commanded values when the readback carries no variation at all, with a
    note, because a plane fitted to commanded values lies on them by
    construction: its RMS is ~0 by definition and measures nothing about the
    instrument.
    """
    rb = [(r["x"], r["y"], r[key]) for r in readback if key in r]
    if rb:
        vs = [p[2] for p in rb]
        if (max(vs) - min(vs)) > _FLAT_SPAN_EPS:
            return rb, "readback", ""
    cmd = [(r["x"], r["y"], r[key]) for r in commanded if key in r]
    if cmd:
        note = (
            "readback is flat to the precision metadata.txt records, so the "
            "commanded values are shown; a plane fitted to commanded values "
            "lies on them by construction, so its RMS is not a measure of "
            "instrument deviation"
        )
        return cmd, "commanded", note
    if rb:
        return rb, "readback", (
            "constant across every mFoV at the precision metadata.txt "
            "records, and no commanded values are available (this batch has "
            "no LiveQC output) - so no plane can be fitted to it"
        )
    return [], "none", "no data"


@dataclass
class DisplayForm:
    """One fit expressed in the units the plots should show."""
    points: List[Tuple[float, float, float]]
    plane: Optional[Tuple[float, float, float]]
    unit: str
    axis_label: str
    scale: float = 1.0          # multiply a fit's rms/span by this
    note: str = ""
    tips: List[str] = field(default_factory=list)


def to_display_units(fit: "PlaneFit") -> DisplayForm:
    """Convert a fit for plotting. OL becomes MICRONS OF FOCUS.

    The readback is in OL microamps (119.262 uA per micron of focus), and its
    absolute value carries a large instrument offset - around 2.08e6 uA,
    which converted naively would read as ~17,500 um and is not a focus
    height at all. So OL is converted to microns AND referenced to this
    section's mean: what the plot is for is the tilt and the spread, both of
    which survive the shift unchanged, while the meaningless offset does not
    get printed on an axis as though it meant something.

    Subtracting a constant from every value shifts the fitted plane by the
    same constant, so the converted plane still passes through the converted
    points - only its intercept changes.

    StigX / StigY are dimensionless and pass through untouched.
    """
    label = fit.label + (f" ({fit.unit})" if fit.unit else "")
    if fit.quantity != "ol" or not fit.points:
        return DisplayForm(list(fit.points), fit.plane, fit.unit, label,
                           1.0, "",
                           [f"mFoV {i + 1}" for i in range(len(fit.points))])

    k = 1.0 / OL_UA_PER_UM
    mean_ua = sum(v for _, _, v in fit.points) / len(fit.points)
    points = [(x, y, (v - mean_ua) * k) for x, y, v in fit.points]
    plane = None
    if fit.plane:
        a, b, c = fit.plane
        plane = (a * k, b * k, (c - mean_ua) * k)
    # A literal µ, not "&micro;": these strings are HTML-escaped by the
    # plotter before they reach the tooltip, so an entity would show up as
    # the text "&micro;A".
    tips = [f"mFoV {i + 1} · {v:.3f} µA raw"
            for i, (_, _, v) in enumerate(fit.points)]
    return DisplayForm(
        points, plane, "µm", "OL (µm of focus, from section mean)", k,
        f"converted at {OL_UA_PER_UM:g} µA per µm and referenced to this "
        f"section's mean OL of {mean_ua:.3f} µA, which is mostly a fixed "
        f"instrument offset",
        tips,
    )


def degenerate_reason(fit: "PlaneFit") -> str:
    """Why this quantity has no plane, in words, or "" if it has one.

    Shared by the section report and the dashboard so both say the same
    thing. `source_note` alone is not enough: when the readback is flat it
    reads "so the commanded values are shown", which is actively misleading
    in a batch where the commanded values are constant too (OL held fixed) -
    it suggests a plot should exist.
    """
    if not fit.n:
        return "no per-mFoV values found"
    if fit.degenerate:
        why = (f"identical across all {fit.n} mFoVs in the {fit.source} "
               f"values, so no plane can be fitted")
        if fit.source == "readback":
            return why + " at the precision metadata.txt records"
        if fit.source == "commanded":
            return (why + " - the metadata.txt readback was flat, and the "
                    "LiveQC commanded values it fell back to are constant "
                    "as well")
        return why
    if not fit.plane:
        return "the least-squares fit did not resolve a plane"
    return ""


def analyze_section(
    section_dir: str | Path,
    cfg: Dict[str, Any],
    sample_id: Optional[str] = None,
) -> Tuple[List[PlaneFit], Optional[Path]]:
    """Plane fits for OL / StigX / StigY, plus reference differences when a
    fiducial file is available. Returns (fits, fiducial_path_or_None)."""
    section_dir = Path(section_dir)
    readback = gather_measured(section_dir)
    commanded = gather_commanded(section_dir)

    fid_path = find_fiducial_file(section_dir, sample_id, cfg)
    fid_points = parse_fiducial_file(fid_path) if fid_path else []
    ref_planes: Dict[str, Optional[Tuple[float, float, float]]] = {}
    if fid_points:
        fx = [p["x"] for p in fid_points]
        fy = [p["y"] for p in fid_points]
        for key, fcol in _FIDUCIAL_COL.items():
            vals = [p.get(fcol) for p in fid_points]
            if all(v is not None for v in vals) and len(vals) >= 3:
                ref_planes[key] = fit_plane(fx, fy, [float(v) for v in vals])
            else:
                ref_planes[key] = None

    fits: List[PlaneFit] = []
    for key, label, unit in QUANTITIES:
        pts, source, note = _choose_source(key, readback, commanded)
        fit = PlaneFit(quantity=key, label=label,
                       unit=unit if source == "commanded" else "",
                       source=source, source_note=note,
                       n=len(pts), points=pts)
        if pts:
            xs = [p[0] for p in pts]
            ys = [p[1] for p in pts]
            vs = np.asarray([p[2] for p in pts], dtype=np.float64)
            fit.span = float(vs.max() - vs.min())
            plane = fit_plane(xs, ys, vs)
            if plane is not None:
                fit.plane = plane
                resid = vs - np.asarray(
                    [_eval_plane(plane, x, y) for x, y in zip(xs, ys)]
                )
                fit.rms = float(np.sqrt(np.mean(resid ** 2)))
                fit.residual_span = float(resid.max() - resid.min())
                if key == "ol":
                    fit.gradient = gradient_stats(plane)

            ref_plane = ref_planes.get(key)
            if ref_plane is not None:
                diffs = np.asarray([
                    _eval_plane(ref_plane, x, y) - v for x, y, v in pts
                ], dtype=np.float64)
                fit.ref_available = True
                fit.ref_rms = float(np.sqrt(np.mean(diffs ** 2)))
                fit.ref_span = float(diffs.max() - diffs.min())
                fit.ref_mean = float(diffs.mean())
                fit.ref_points = [
                    (x, y, float(d)) for (x, y, _), d in zip(pts, diffs)
                ]
        fits.append(fit)
    return fits, fid_path


# ---------------------------------------------------------------------------
# rendering
# ---------------------------------------------------------------------------
_DIVERGING = [
    (0.00, (5, 48, 97)), (0.25, (67, 147, 195)), (0.50, (247, 247, 247)),
    (0.75, (214, 96, 77)), (1.00, (103, 0, 31)),
]


def _color(t: float) -> str:
    """Blue-white-red ramp. Diverging because every quantity plotted here is
    read against a fitted plane or a reference, so the meaningful question is
    which side of zero a point falls on, not its absolute magnitude."""
    t = 0.0 if math.isnan(t) else min(1.0, max(0.0, t))
    for (t0, c0), (t1, c1) in zip(_DIVERGING[:-1], _DIVERGING[1:]):
        if t <= t1:
            f = 0.0 if t1 == t0 else (t - t0) / (t1 - t0)
            rgb = [int(round(a + (b - a) * f)) for a, b in zip(c0, c1)]
            return f"#{rgb[0]:02x}{rgb[1]:02x}{rgb[2]:02x}"
    return "#67001f"


def scatter_svg(
    points: Sequence[Tuple[float, float, float]],
    title: str,
    unit: str = "",
    width: int = 330,
    symmetric: bool = True,
    azimuth_deg: Optional[float] = None,
) -> str:
    """One (x, y, value) scatter as inline SVG, coloured by value.

    Y is negated so the picture matches the orientation the dashboard's hex
    maps use - SVG's y grows downward, stage y does not.
    """
    import html as _h
    if not points:
        return f'<div class="muted">{_h.escape(title)}: no data</div>'

    xs = [p[0] for p in points]
    ys = [-p[1] for p in points]
    vs = np.asarray([p[2] for p in points], dtype=np.float64)
    lo, hi = float(vs.min()), float(vs.max())
    if symmetric:
        m = max(abs(lo), abs(hi), 1e-12)
        lo, hi = -m, m
    rng = (hi - lo) or 1.0

    pad = 26
    spanx = (max(xs) - min(xs)) or 1.0
    spany = (max(ys) - min(ys)) or 1.0
    plot = width - 2 * pad
    height = int(plot * (spany / spanx)) + 2 * pad
    height = max(150, min(height, 460))
    ploth = height - 2 * pad

    parts = [
        f"<svg viewBox='0 0 {width} {height + 30}' width='100%' "
        f"style='max-width:{width}px' role='img' "
        f"aria-label='{_h.escape(title)}'>",
        f"<text x='{pad}' y='12' font-size='11' fill='#333'>"
        f"{_h.escape(title)}</text>",
    ]
    r = max(2.0, min(7.0, 0.45 * plot / max(1.0, math.sqrt(len(points)))))
    for (x, y, v) in points:
        cx = pad + (x - min(xs)) / spanx * plot
        cy = pad + (-y - min(ys)) / spany * ploth
        t = (v - lo) / rng
        parts.append(
            f"<circle cx='{cx:.1f}' cy='{cy:.1f}' r='{r:.1f}' "
            f"fill='{_color(t)}' stroke='#555' stroke-width='0.4'>"
            f"<title>x={x:.0f} y={y:.0f} → {v:.4g} {_h.escape(unit)}</title>"
            f"</circle>"
        )
    # Steepest-ascent arrow, so the azimuth number has something to read
    # against. Drawn with -sin for the y component because SVG's y grows
    # downward while the angle is defined in stage coordinates.
    if azimuth_deg is not None:
        th = math.radians(azimuth_deg)
        cx0, cy0 = pad + plot / 2.0, pad + ploth / 2.0
        L = 0.30 * min(plot, ploth)
        x1 = cx0 + L * math.cos(th)
        y1 = cy0 - L * math.sin(th)
        parts.append(
            f"<line x1='{cx0:.1f}' y1='{cy0:.1f}' x2='{x1:.1f}' y2='{y1:.1f}' "
            f"stroke='#111' stroke-width='2' opacity='0.75'/>"
        )
        for sgn in (+1, -1):
            ah = th + sgn * math.radians(150)
            parts.append(
                f"<line x1='{x1:.1f}' y1='{y1:.1f}' "
                f"x2='{x1 + 9 * math.cos(ah):.1f}' "
                f"y2='{y1 - 9 * math.sin(ah):.1f}' "
                f"stroke='#111' stroke-width='2' opacity='0.75'/>"
            )
        parts.append(
            f"<title>steepest ascent: {azimuth_deg:.1f}° from +x</title>"
        )

    # Colour bar
    bw = plot
    for i in range(40):
        parts.append(
            f"<rect x='{pad + i * bw / 40:.1f}' y='{height + 4}' "
            f"width='{bw / 40 + 0.6:.1f}' height='7' "
            f"fill='{_color(i / 39.0)}'/>"
        )
    parts.append(
        f"<text x='{pad}' y='{height + 24}' font-size='9' fill='#555'>"
        f"{lo:.4g}</text>"
        f"<text x='{pad + bw}' y='{height + 24}' font-size='9' fill='#555' "
        f"text-anchor='end'>{hi:.4g}</text></svg>"
    )
    return "".join(parts)
