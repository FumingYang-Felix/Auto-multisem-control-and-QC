#!/usr/bin/env python3
"""Compare tilted-plane fits of OL(x,y), StigX(x,y), StigY(x,y) across
multiple fiducial-point datasets.

Each dataset's file is read via its OWN header row (column order/columns
present need not match between files) - x, y, z, ol are required; stigx/
stigy are used only for datasets whose header actually has those columns
(datasets that only recorded OL, e.g. an "outside the ROI" fiducial pass,
are simply skipped for the stig comparison, not fabricated).

For each field (OL, StigX, StigY) with >=1 dataset:
  - fits value = a*x + b*y + c per dataset
  - reports the plane equation, slope magnitude (|grad|=sqrt(a^2+b^2)),
    tilt angle (deg from horizontal), and R^2
  - draws one figure per field: a row of scatter+plane 3D subplots, one per
    dataset that has that field, sharing the same z-scale for visual
    comparison
For each field with >=2 datasets, additionally prints a pairwise comparison
table (slope ratio, tilt-angle delta, and the angle BETWEEN the two plane
normals - the single number for "how differently tilted", independent of
which direction each plane tilts).

Input format (comma-separated, header row REQUIRED - column names, any
order): x, y, z, ol[, stigx, stigy][, projective_x, projective_y, ...].
Only x/y/z/ol are required; stigx/stigy are optional per-dataset.

Usage:
    python compare_ol_plane_fiducials.py [path1 path2 ...] [--no-show]
Defaults to the three w10h31_region_fiducials*.txt files below.
--no-show: skip the interactive windows and just save the PNGs.
"""
import itertools
import math
import os
import re
import sys

# Header column names that spell out units/differ from the canonical name
# this script looks up internally (e.g. a header of "Stigmatorx(%)" instead
# of "stigx") - see _normalize_header_token().
_HEADER_ALIASES = {
    "stigmatorx": "stigx",
    "stigmatory": "stigy",
}

DEFAULT_DATASETS = [
    ("Dataset 1", r"D:\Users\Vlad\Section _detection-files\w10h31_region_fiducials.txt"),
    ("Dataset 2", r"D:\Users\Vlad\Section _detection-files\w10h31_region_fiducials_2.txt"),
    ("Dataset 3 (outside)", r"D:\Users\Vlad\Section _detection-files\w10h31_region_fiducials_3_outside.txt"),
]

FIELDS = ["OL", "StigX", "StigY"]
FIELD_COL = {"OL": "ol", "StigX": "stigx", "StigY": "stigy"}
FIELD_UNIT = {"OL": "um", "StigX": "%", "StigY": "%"}


def _normalize_header_token(token: str) -> str:
    """Canonicalize a header column name: strip a trailing '(unit)' annotation
    and whitespace, lowercase, then apply _HEADER_ALIASES - so a header can
    spell out units and use human-friendly names (e.g. 'x(um)', 'OL(um)',
    'Stigmatorx(%)') without breaking column lookup below."""
    name = re.sub(r"\(.*?\)", "", token).strip().lower()
    return _HEADER_ALIASES.get(name, name)


def parse_dataset(path):
    """Parse a fiducials file using its OWN header row to locate columns by
    name - so it doesn't matter whether stigx/stigy/projective_x/y are
    present, missing, or reordered. Returns a dict with numpy arrays for
    "x","y","z","ol" (always) and "stigx"/"stigy" (only if that dataset's
    header has them - otherwise those keys are None, not fabricated)."""
    import numpy as np

    with open(path, "r") as f:
        lines = [ln.rstrip("\n") for ln in f if ln.strip()]
    if not lines:
        raise RuntimeError(f"{path}: file is empty")

    header_tokens = [_normalize_header_token(t) for t in lines[0].split(",")]
    if header_tokens[0] != "x":
        raise RuntimeError(
            f"{path}: expected a header row starting with 'x' (got {lines[0]!r}) - "
            f"this script requires a header naming each column.")
    col = {name: i for i, name in enumerate(header_tokens)}
    for required in ("x", "y", "z", "ol"):
        if required not in col:
            raise RuntimeError(f"{path}: header is missing required column {required!r}: {lines[0]!r}")
    has_stig = "stigx" in col and "stigy" in col

    parsed = {"x": [], "y": [], "z": [], "ol": []}
    if has_stig:
        parsed["stigx"] = []
        parsed["stigy"] = []

    for line_num, line in enumerate(lines[1:], start=2):
        tokens = [t.strip() for t in line.split(",")]
        try:
            row = {k: float(tokens[i]) for k, i in col.items() if k in parsed}
        except (ValueError, IndexError):
            print(f"[{os.path.basename(path)} line {line_num}] WARNING: could not parse "
                  f"-> skipping: {line!r}")
            continue
        for k in parsed:
            parsed[k].append(row[k])

    if len(parsed["x"]) < 3:
        raise RuntimeError(f"{path}: need >=3 valid points to fit a plane, got {len(parsed['x'])}")

    out = {k: np.array(v) for k, v in parsed.items()}
    out["has_stig"] = has_stig
    out["n"] = len(out["x"])
    return out


def fit_plane(xs, ys, values):
    """Least-squares fit value = a*x + b*y + c. Returns (a, b, c, residuals)."""
    import numpy as np

    A = np.column_stack([xs, ys, np.ones_like(xs)])
    (a, b, c), _, _, _ = np.linalg.lstsq(A, values, rcond=None)
    residuals = values - (a * xs + b * ys + c)
    return a, b, c, residuals


def plane_slope_and_tilt(a, b):
    """slope_magnitude = |gradient| of the plane (value-units per um).
    tilt_deg = angle between the plane and horizontal (0 = flat, 90 = vertical).
    azimuth_deg = compass direction (0=+x, 90=+y, ...) the plane tilts UP toward."""
    slope_magnitude = math.hypot(a, b)
    tilt_deg = math.degrees(math.atan(slope_magnitude))
    azimuth_deg = math.degrees(math.atan2(b, a))
    return slope_magnitude, tilt_deg, azimuth_deg


def flatness_metrics(values, residuals):
    import numpy as np

    ss_res = float(np.sum(residuals ** 2))
    ss_tot = float(np.sum((values - float(np.mean(values))) ** 2))
    r_squared = 1.0 - ss_res / ss_tot if ss_tot > 0 else float("nan")
    rms = (ss_res / len(residuals)) ** 0.5
    return r_squared, rms


def angle_between_planes(a1, b1, a2, b2):
    """Angle (deg) between the two planes' normal vectors - 0 = identically
    tilted (same magnitude AND direction), regardless of the sign convention
    of either individual tilt/azimuth number."""

    def normal(a, b):
        n = (-a, -b, 1.0)
        mag = math.sqrt(sum(v * v for v in n))
        return tuple(v / mag for v in n)

    n1, n2 = normal(a1, b1), normal(a2, b2)
    dot = max(-1.0, min(1.0, sum(v1 * v2 for v1, v2 in zip(n1, n2))))
    return math.degrees(math.acos(dot))


def fit_field(field_name, label, dataset):
    """Fit one (field, dataset) pair; None if that dataset doesn't have this
    field (e.g. StigX/StigY on an OL-only fiducial file)."""
    col = FIELD_COL[field_name]
    if col not in dataset or dataset[col] is None:
        return None

    xs, ys, values = dataset["x"], dataset["y"], dataset[col]
    a, b, c, residuals = fit_plane(xs, ys, values)
    slope_mag, tilt_deg, azimuth_deg = plane_slope_and_tilt(a, b)
    r_squared, rms = flatness_metrics(values, residuals)

    zs = set(dataset["z"].tolist())
    if len(zs) > 1:
        print(f"[{label}] WARNING: z is not constant across points: {sorted(zs)}")

    unit = FIELD_UNIT[field_name]
    unit_suffix = f" {unit}" if unit else ""
    slope_mag_per_mm = slope_mag * 1000.0
    print(f"\n--- {field_name} / {label} ({dataset['n']} point(s)) ---")
    print(f"  {field_name}(x, y) = ({a:.6e}) * x + ({b:.6e}) * y + ({c:.4f})")
    print(f"  slope magnitude |grad| = {slope_mag:.6e}{unit_suffix}/um "
          f"({slope_mag_per_mm:.6e}{unit_suffix}/mm)")
    print(f"  tilt angle from horizontal = {tilt_deg:.4f} deg   "
          f"(azimuth = {azimuth_deg:.2f} deg)")
    print(f"  R^2 = {r_squared:.4f}   RMS residual = {rms:.4f}{unit_suffix}")

    return {
        "field": field_name, "label": label, "xs": xs, "ys": ys, "values": values,
        "a": a, "b": b, "c": c, "residuals": residuals,
        "slope_mag": slope_mag, "slope_mag_per_mm": slope_mag_per_mm,
        "tilt_deg": tilt_deg, "azimuth_deg": azimuth_deg,
        "r_squared": r_squared, "rms": rms,
    }


def compare_field(field_name, fits):
    """Pairwise comparison across every dataset that has this field."""
    if len(fits) < 2:
        if fits:
            print(f"\n{field_name}: only 1 dataset has this field ({fits[0]['label']}) "
                  f"- nothing to compare it against.")
        else:
            print(f"\n{field_name}: no dataset has this field.")
        return

    unit = FIELD_UNIT[field_name]
    unit_suffix = f" {unit}" if unit else ""
    print("\n" + "=" * 78)
    print(f"{field_name} COMPARISON across {len(fits)} dataset(s)")
    print("=" * 78)
    for d1, d2 in itertools.combinations(fits, 2):
        ratio = (d2["slope_mag"] / d1["slope_mag"]) if d1["slope_mag"] > 0 else float("nan")
        angle_between = angle_between_planes(d1["a"], d1["b"], d2["a"], d2["b"])
        print(f"\n  {d1['label']}  vs  {d2['label']}")
        print(f"    slope magnitude: {d1['slope_mag']:.6e}{unit_suffix}/um  vs  "
              f"{d2['slope_mag']:.6e}{unit_suffix}/um   (ratio = {ratio:.3f})")
        print(f"    slope magnitude: {d1['slope_mag_per_mm']:.6e}{unit_suffix}/mm  vs  "
              f"{d2['slope_mag_per_mm']:.6e}{unit_suffix}/mm")
        print(f"    tilt angle:      {d1['tilt_deg']:.4f}  vs  {d2['tilt_deg']:.4f}  deg"
              f"   (delta = {d2['tilt_deg'] - d1['tilt_deg']:+.4f} deg)")
        print(f"    tilt azimuth:    {d1['azimuth_deg']:.2f}  vs  {d2['azimuth_deg']:.2f}  deg")
        print(f"    angle BETWEEN the two plane normals: {angle_between:.4f} deg "
              f"(0 = identically tilted)")
        print(f"    intercept (c):   {d1['c']:.4f}  vs  {d2['c']:.4f}"
              f"   (delta = {d2['c'] - d1['c']:+.4f})")


def plot_field(field_name, fits, out_path):
    """One figure per field: a row of scatter+plane 3D subplots, one per
    dataset that has this field, sharing the same z-scale. Returns the
    Figure (left open for interactive display)."""
    import numpy as np
    import matplotlib.pyplot as plt

    all_values = np.concatenate([d["values"] for d in fits])
    zmin, zmax = float(all_values.min()), float(all_values.max())
    zpad = (zmax - zmin) * 0.1 or 1.0
    zmin, zmax = zmin - zpad, zmax + zpad

    fig = plt.figure(figsize=(7.5 * len(fits), 7))
    unit = FIELD_UNIT[field_name]
    unit_suffix = f" {unit}" if unit else ""

    for i, d in enumerate(fits, start=1):
        ax = fig.add_subplot(1, len(fits), i, projection="3d")
        xs, ys, values, a, b, c = d["xs"], d["ys"], d["values"], d["a"], d["b"], d["c"]

        pad_x = (xs.max() - xs.min()) * 0.1 or 1.0
        pad_y = (ys.max() - ys.min()) * 0.1 or 1.0
        xx, yy = np.meshgrid(
            np.linspace(xs.min() - pad_x, xs.max() + pad_x, 10),
            np.linspace(ys.min() - pad_y, ys.max() + pad_y, 10),
        )
        zz = a * xx + b * yy + c
        ax.plot_surface(xx, yy, zz, alpha=0.4, color="tab:blue", edgecolor="none")

        ax.scatter(xs, ys, values, color="tab:red", s=50, depthshade=True, label="measured")
        for x, y, v in zip(xs, ys, values):
            ax.plot([x, x], [y, y], [v, a * x + b * y + c], color="gray", linewidth=0.8)

        ax.set_zlim(zmin, zmax)
        ax.set_xlabel("x (um)")
        ax.set_ylabel("y (um)")
        ax.set_zlabel(f"{field_name}{unit_suffix}")
        ax.set_title(
            f"{d['label']}\ntilt={d['tilt_deg']:.3f} deg  "
            f"|grad|={d['slope_mag_per_mm']:.2e}{unit_suffix}/mm  R^2={d['r_squared']:.3f}"
        )
        ax.legend()

    fig.suptitle(f"{field_name} plane fit comparison", fontsize=14)
    fig.tight_layout()
    fig.savefig(out_path, dpi=150)
    print(f"\nSaved plot to {out_path}")
    return fig


def main():
    args = [a for a in sys.argv[1:] if a != "--no-show"]
    show = "--no-show" not in sys.argv[1:]
    paths = args if args else [p for _, p in DEFAULT_DATASETS]
    labels = [lbl for lbl, _ in DEFAULT_DATASETS] if not args else [f"Dataset {i+1}" for i in range(len(paths))]

    print("=" * 78)
    print("Loading datasets")
    print("=" * 78)
    datasets = []
    for label, path in zip(labels, paths):
        d = parse_dataset(path)
        stig_note = "has stigx/stigy" if d["has_stig"] else "OL only (no stigx/stigy)"
        print(f"  {label}: {os.path.basename(path)} - {d['n']} point(s), {stig_note}")
        datasets.append((label, path, d))

    out_dir = os.path.dirname(os.path.abspath(paths[0]))
    figures = []

    for field_name in FIELDS:
        print("\n" + "#" * 78)
        print(f"# {field_name}")
        print("#" * 78)
        fits = []
        for label, _path, d in datasets:
            fit = fit_field(field_name, label, d)
            if fit is not None:
                fits.append(fit)

        compare_field(field_name, fits)

        if fits:
            out_path = os.path.join(out_dir, f"{field_name.lower()}_plane_fit_comparison.png")
            figures.append(plot_field(field_name, fits, out_path))

    import matplotlib.pyplot as plt
    if show:
        print(f"\nOpening {len(figures)} interactive plot window(s) - drag a subplot to "
              f"rotate it, scroll/right-drag to zoom. Close all windows (or Ctrl+C) to exit.")
        plt.show()
    else:
        for fig in figures:
            plt.close(fig)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
