#!/usr/bin/env python3
"""Fit tilted planes OL(x,y), StigX(x,y), StigY(x,y) to a per-region fiducial
points file, one row per region: x, y, z, ol, stigx, stigy.

z is expected to be constant across rows (not used in any of the three fits -
each plane is fit over x,y with ol/stigx/stigy as the third dimension) - a
warning is printed if it isn't.

Input format (comma-separated, header row "x,y,z,ol,stigx,stigy" optional):
    8407.3,23141.4,38877.3,-8.0,0.0,-0.1
    ...

Usage:
    python fit_region_fiducial_planes.py [path/to/points.txt] [--no-show]
Defaults to D:\\Users\\Vlad\\Section _detection-files\\w10h31_region_fiducials.txt.
--no-show: skip the interactive windows and just save the PNGs (e.g. for a
headless/automated run) - shown by default otherwise.

For each of OL / StigX / StigY this prints the fitted plane's equation and
residual/flatness stats, saves a 3D scatter-plus-plane-surface PNG
(<field>_plane_fit.png) next to the input file, AND opens it in an
interactive matplotlib window (drag to rotate, scroll/right-drag to zoom) -
all three windows open together at the end so each can be rotated/zoomed
independently; close them (or Ctrl+C the terminal) to let the script exit.
"""
import os
import sys

DEFAULT_PATH = r"D:\Users\Vlad\Section _detection-files\w10h31_region_fiducials_2.txt"

# Per the user: neither StigX nor StigY should ever legitimately exceed this
# on this instrument - a row with a bigger one is a fault (microscope or
# sample), not real data, and the whole point (x,y,z,ol,stigx,stigy) must be
# dropped from every fit, not just the stig ones. Same threshold as
# focus_map.py's _STIG_FAULT_THRESHOLD_PCT (kept as a separate constant -
# this script has no dependency on focus_map.py).
STIG_FAULT_THRESHOLD_PCT = 2.0

# (column index in each parsed row, output field name, z-axis label, output filename)
FIELDS = [
    (3, "OL", "OL (uA)", "ol_plane_fit.png"),
    (4, "StigX", "StigX", "stigx_plane_fit.png"),
    (5, "StigY", "StigY", "stigy_plane_fit.png"),
]


def parse_points(path):
    """Parse 'x,y,z,ol,stigx,stigy[,projective_x,projective_y,...]' rows -
    only the first 6 fields are used, any trailing columns (e.g. projective
    x/y) are ignored. Tolerates/skips a header row and malformed lines
    (warns, doesn't abort)."""
    rows = []
    with open(path, "r") as f:
        for line_num, raw_line in enumerate(f, start=1):
            line = raw_line.strip()
            if not line:
                continue
            tokens = [t.strip() for t in line.split(",")]
            if tokens[0].lower() == "x":
                continue  # header row

            if len(tokens) < 6:
                print(f"[Line {line_num}] WARNING: expected >=6 fields, got "
                      f"{len(tokens)} -> skipping: {raw_line!r}")
                continue
            tokens = tokens[:6]

            try:
                x, y, z, ol, stigx, stigy = (float(t) for t in tokens)
            except ValueError:
                print(f"[Line {line_num}] WARNING: could not parse floats "
                      f"-> skipping: {raw_line!r}")
                continue

            if abs(stigx) > STIG_FAULT_THRESHOLD_PCT or abs(stigy) > STIG_FAULT_THRESHOLD_PCT:
                print(f"[Line {line_num}] WARNING: StigX={stigx} StigY={stigy} exceeds the "
                      f"{STIG_FAULT_THRESHOLD_PCT:.0f}% fault threshold - treating as a bad "
                      f"point (microscope/sample fault) and dropping it entirely: {raw_line!r}")
                continue

            rows.append((x, y, z, ol, stigx, stigy))
    return rows


def fit_plane(xs, ys, values):
    """Least-squares fit value = a*x + b*y + c. Returns (a, b, c, residuals)."""
    import numpy as np

    A = np.column_stack([xs, ys, np.ones_like(xs)])
    (a, b, c), _, _, _ = np.linalg.lstsq(A, values, rcond=None)

    predicted = a * xs + b * ys + c
    residuals = values - predicted
    return a, b, c, residuals


def flatness_metrics(values, residuals):
    """R^2 (fraction of variance the plane explains) plus a couple of
    residual-vs-spread ratios that are easier to read when R^2 alone is
    ambiguous (e.g. a small, noisy value range)."""
    import numpy as np

    mean_v = float(np.mean(values))
    ss_res = float(np.sum(residuals ** 2))
    ss_tot = float(np.sum((values - mean_v) ** 2))
    r_squared = 1.0 - ss_res / ss_tot if ss_tot > 0 else float("nan")

    rms = (ss_res / len(residuals)) ** 0.5
    v_range = float(values.max() - values.min())
    v_std = (ss_tot / len(values)) ** 0.5

    return {
        "r_squared": r_squared,
        "rms": rms,
        "rms_over_range": rms / v_range if v_range > 0 else float("nan"),
        "rms_over_std": rms / v_std if v_std > 0 else float("nan"),
        "range": v_range,
    }


def plot_fit(xs, ys, values, a, b, c, zlabel, title, out_path):
    """3D scatter of the measured points + the fitted plane surface. Returns
    the Figure (left open - NOT closed here) so the caller can plt.show() it
    interactively (rotate/zoom) after saving the static PNG."""
    import numpy as np
    import matplotlib.pyplot as plt

    fig = plt.figure(figsize=(9, 7))
    ax = fig.add_subplot(111, projection="3d")

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

    ax.set_xlabel("x (um)")
    ax.set_ylabel("y (um)")
    ax.set_zlabel(zlabel)
    ax.set_title(title)
    ax.legend()

    fig.tight_layout()
    fig.savefig(out_path, dpi=150)
    print(f"Saved plot to {out_path}")
    return fig


def main():
    import numpy as np

    args = [a for a in sys.argv[1:] if a != "--no-show"]
    show = "--no-show" not in sys.argv[1:]
    path = args[0] if args else DEFAULT_PATH
    rows = parse_points(path)

    if len(rows) < 3:
        print(f"Need at least 3 valid points to fit a plane, got {len(rows)}.")
        return 1

    zs = {r[2] for r in rows}
    if len(zs) > 1:
        print(f"WARNING: z is not constant across points: {sorted(zs)}")

    xs = np.array([r[0] for r in rows])
    ys = np.array([r[1] for r in rows])
    out_dir = os.path.dirname(os.path.abspath(path))
    figures = []

    for col_idx, name, zlabel, out_name in FIELDS:
        values = np.array([r[col_idx] for r in rows])

        print("\n" + "=" * 78)
        print(f"{name}: fit using {len(rows)} point(s)")
        print("=" * 78)
        a, b, c, residuals = fit_plane(xs, ys, values)
        for x, y, v, res in zip(xs, ys, values, residuals):
            print(f"  x={x:>10.1f}  y={y:>10.1f}  {name}={v:>9.3f}  residual={res:+.4f}")

        print(f"\n{name} plane equation:")
        print(f"  {name}(x, y) = ({a:.6e}) * x + ({b:.6e}) * y + ({c:.4f})")

        m = flatness_metrics(values, residuals)
        print(f"\nRMS residual: {m['rms']:.4f}")
        print(f"Max |residual|: {max(abs(r) for r in residuals):.4f}")
        print(f"{name} range across points: {m['range']:.4f}")
        print(f"R^2 (variance explained by plane): {m['r_squared']:.4f}")
        print(f"RMS residual / range: {m['rms_over_range']:.4f}")
        print(f"RMS residual / std-dev: {m['rms_over_std']:.4f}")

        out_path = os.path.join(out_dir, out_name)
        figures.append(plot_fit(xs, ys, values, a, b, c, zlabel, f"{name} plane fit", out_path))

    if show:
        import matplotlib.pyplot as plt
        print(f"\nOpening {len(figures)} interactive plot window(s) - drag to rotate, "
              f"scroll/right-drag to zoom. Close all windows (or Ctrl+C) to exit.")
        plt.show()
    else:
        import matplotlib.pyplot as plt
        for fig in figures:
            plt.close(fig)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
