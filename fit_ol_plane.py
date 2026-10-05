#!/usr/bin/env python3
"""Fit a tilted plane OL = a*x + b*y + c to an (x, y, z, ol) points file.

Input format (comma-separated, optional header row "x,y,z,ol"):
    x, y, z in mm; ol in microns; z is expected to be constant (not used
    in the fit - the plane is fit over x,y with ol as the third dimension).

Usage:
    python fit_ol_plane.py [path/to/points.txt]
Defaults to D:\\Users\\Vlad\\Section _detection-files\\w10h31_dye_pos.txt.
"""
import sys

DEFAULT_PATH = r"D:\Users\Vlad\Section _detection-files\w10h31_dye_pos.txt"


def parse_points(path):
    """Robust comma-split parser - tolerates stray decimal-comma typos
    (e.g. "11,2" meaning "11.2") by collapsing a 5-token line's last two
    tokens back into one field."""
    rows = []
    with open(path, "r") as f:
        for line_num, raw_line in enumerate(f, start=1):
            line = raw_line.strip()
            if not line:
                continue
            tokens = [t.strip() for t in line.split(",")]
            if tokens[0].lower() == "x":
                continue  # header row

            if len(tokens) == 5:
                # Likely a decimal comma inside the last field, e.g.
                # "...,38836.0, 11,2" -> ol field split into "11" and "2".
                tokens = tokens[:3] + [f"{tokens[3]}.{tokens[4]}"]
                print(f"[Line {line_num}] WARNING: 5 fields found, treating "
                      f"as decimal-comma typo -> ol={tokens[3]}")

            if len(tokens) != 4:
                print(f"[Line {line_num}] WARNING: expected 4 fields, got "
                      f"{len(tokens)} -> skipping: {raw_line!r}")
                continue

            try:
                x, y, z, ol = (float(t) for t in tokens)
            except ValueError:
                print(f"[Line {line_num}] WARNING: could not parse floats "
                      f"-> skipping: {raw_line!r}")
                continue

            rows.append((x, y, z, ol))
    return rows


def fit_plane(rows):
    """Least-squares fit ol = a*x + b*y + c. Returns (a, b, c, residuals)."""
    import numpy as np

    xs = np.array([r[0] for r in rows])
    ys = np.array([r[1] for r in rows])
    ols = np.array([r[3] for r in rows])

    A = np.column_stack([xs, ys, np.ones_like(xs)])
    (a, b, c), _, _, _ = np.linalg.lstsq(A, ols, rcond=None)

    predicted = a * xs + b * ys + c
    residuals = ols - predicted
    return a, b, c, residuals


def flatness_metrics(rows, residuals):
    """R^2 (fraction of OL variance the plane explains) plus a couple of
    residual-vs-spread ratios that are easier to read when R^2 alone is
    ambiguous (e.g. a small, noisy OL range)."""
    ols = [r[3] for r in rows]
    mean_ol = sum(ols) / len(ols)

    ss_res = sum(r ** 2 for r in residuals)
    ss_tot = sum((v - mean_ol) ** 2 for v in ols)
    r_squared = 1.0 - ss_res / ss_tot if ss_tot > 0 else float("nan")

    rms = (ss_res / len(residuals)) ** 0.5
    ol_range = max(ols) - min(ols)
    ol_std = (ss_tot / len(ols)) ** 0.5

    return {
        "r_squared": r_squared,
        "rms": rms,
        "rms_over_range": rms / ol_range if ol_range > 0 else float("nan"),
        "rms_over_std": rms / ol_std if ol_std > 0 else float("nan"),
        "ol_range": ol_range,
    }


def plot_fit(rows, a, b, c, out_path):
    """3D scatter of the measured points + the fitted plane surface."""
    import numpy as np
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    xs = np.array([r[0] for r in rows])
    ys = np.array([r[1] for r in rows])
    ols = np.array([r[3] for r in rows])

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

    ax.scatter(xs, ys, ols, color="tab:red", s=50, depthshade=True, label="measured")
    for x, y, ol in zip(xs, ys, ols):
        ax.plot([x, x], [y, y], [ol, a * x + b * y + c], color="gray", linewidth=0.8)

    ax.set_xlabel("x (mm)")
    ax.set_ylabel("y (mm)")
    ax.set_zlabel("OL (um)")
    ax.set_title("OL plane fit")
    ax.legend()

    fig.tight_layout()
    fig.savefig(out_path, dpi=150)
    print(f"\nSaved plot to {out_path}")


def main():
    path = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_PATH
    rows = parse_points(path)

    if len(rows) < 3:
        print(f"Need at least 3 valid points to fit a plane, got {len(rows)}.")
        return 1

    zs = {r[2] for r in rows}
    if len(zs) > 1:
        print(f"WARNING: z is not constant across points: {sorted(zs)}")

    a, b, c, residuals = fit_plane(rows)

    print(f"\nFit using {len(rows)} point(s):")
    for (x, y, z, ol), res in zip(rows, residuals):
        print(f"  x={x:>10.1f}  y={y:>10.1f}  z={z:>10.1f}  "
              f"ol={ol:>7.2f}  residual={res:+.3f}")

    print("\nPlane equation:")
    print(f"  ol(x, y) = ({a:.6e}) * x + ({b:.6e}) * y + ({c:.4f})")

    m = flatness_metrics(rows, residuals)
    print(f"\nRMS residual: {m['rms']:.4f} um")
    print(f"Max |residual|: {max(abs(r) for r in residuals):.4f} um")
    print(f"OL range across points: {m['ol_range']:.4f} um")
    print(f"R^2 (variance explained by plane): {m['r_squared']:.4f}")
    print(f"RMS residual / OL range: {m['rms_over_range']:.4f}")
    print(f"RMS residual / OL std-dev: {m['rms_over_std']:.4f}")

    import os
    out_path = os.path.join(os.path.dirname(os.path.abspath(path)), "ol_plane_fit.png")
    plot_fit(rows, a, b, c, out_path)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
