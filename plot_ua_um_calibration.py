"""
Fit and plot the OL-current (uA) <-> focus (um) calibration collected by
calibrate_ua_um.py.

Flags consecutive rows whose ol_current_uA reading is essentially identical
(no real stage/OL change between them) as likely mistake-then-correction
pairs - the smallest real step in this dataset is ~0.01 um, which the
~119 uA/um slope turns into >=1 uA of real change, so two readings within
0.5 uA of each other with different labeled um values means nothing actually
moved between them.

Usage:  python plot_ua_um_calibration.py
Reads:  D:\\Users\\Vlad\\Section _detection-files\\diagnostics\\ua_um_calibration.csv
Writes: ua_um_calibration.html + ua_um_calibration_fit.json next to it.
"""
import csv
import json
import os

import numpy as np
import plotly.graph_objects as go
from plotly.subplots import make_subplots

DIAG_DIR = r"D:\Users\Vlad\Section _detection-files\diagnostics"
CSV_PATH = os.path.join(DIAG_DIR, "ua_um_calibration.csv")
DUPLICATE_UA_THRESHOLD = 0.5  # uA - well below the smallest real expected step


def load_rows(path):
    rows = []
    with open(path, newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            rows.append({
                "timestamp": r["timestamp"],
                "um": float(r["um_entered"]),
                "ua": float(r["ol_current_uA"]),
                "z": float(r["stage_z_um"]),
            })
    return rows


def fit_line(um, ua):
    A = np.column_stack([um, np.ones_like(um)])
    (slope, intercept), *_ = np.linalg.lstsq(A, ua, rcond=None)
    pred = slope * um + intercept
    resid = ua - pred
    ss_res = float(np.sum(resid ** 2))
    ss_tot = float(np.sum((ua - ua.mean()) ** 2))
    r2 = 1.0 - ss_res / ss_tot if ss_tot > 0 else float("nan")
    return float(slope), float(intercept), r2, resid


def main():
    rows = load_rows(CSV_PATH)
    um = np.array([r["um"] for r in rows])
    ua = np.array([r["ua"] for r in rows])

    # Flag the EARLIER row of any pair whose uA reading barely changed
    # despite a different labeled um - the classic "typed the wrong value,
    # caught it, re-entered" signature (the stage/OL hadn't actually moved
    # between the two entries).
    flagged = np.zeros(len(rows), dtype=bool)
    for i in range(1, len(rows)):
        if abs(ua[i] - ua[i - 1]) < DUPLICATE_UA_THRESHOLD and um[i] != um[i - 1]:
            flagged[i - 1] = True

    slope_all, intercept_all, r2_all, resid_all = fit_line(um, ua)
    clean = ~flagged
    slope_clean, intercept_clean, r2_clean, resid_clean_sub = fit_line(um[clean], ua[clean])
    resid_clean_full = ua - (slope_clean * um + intercept_clean)

    print("=" * 70)
    print(f"All {len(rows)} points:      uA = {slope_all:.4f} * um + {intercept_all:.4f}   R^2={r2_all:.8f}")
    print(f"Excl. {int(flagged.sum())} flagged pt(s): uA = {slope_clean:.4f} * um + {intercept_clean:.4f}   R^2={r2_clean:.8f}")
    print("=" * 70)
    for i in np.where(flagged)[0]:
        print(f"  FLAGGED: {rows[i]['timestamp']}  um={um[i]}  uA={ua[i]:.4f}  "
              f"(next row um={um[i+1]} uA={ua[i+1]:.4f} - essentially the same reading)")

    # um>=10 sanity check the user specifically asked about, against the clean fit
    hi = um >= 10.0
    print(f"\num >= 10 region ({hi.sum()} pts, clean fit): "
          f"max |residual| = {np.max(np.abs(resid_clean_full[hi])):.4f} uA, "
          f"mean = {np.mean(resid_clean_full[hi]):+.4f} uA")
    lo = (um < 10.0) & clean
    print(f"um < 10 region, excl. flagged ({lo.sum()} pts): "
          f"max |residual| = {np.max(np.abs(resid_clean_full[lo])):.4f} uA, "
          f"mean = {np.mean(resid_clean_full[lo]):+.4f} uA")

    hover = [f"{r['timestamp']}<br>um={r['um']}<br>uA={r['ua']:.4f}<br>stage Z={r['z']:.4f} um"
             for r in rows]

    fig = make_subplots(
        rows=2, cols=1, shared_xaxes=True, vertical_spacing=0.10,
        row_heights=[0.68, 0.32],
        subplot_titles=(
            "OL current (uA) vs. focus (um) calibration",
            "Residuals vs. the excl.-flagged fit (green = um >= 10, the region you flagged as suspect)",
        ),
    )

    fig.add_trace(go.Scatter(
        x=um[clean & ~hi], y=ua[clean & ~hi], mode="markers", name="um < 10",
        marker=dict(size=7, color="#2563eb"),
        text=[hover[i] for i in np.where(clean & ~hi)[0]], hovertemplate="%{text}<extra></extra>",
    ), row=1, col=1)
    fig.add_trace(go.Scatter(
        x=um[clean & hi], y=ua[clean & hi], mode="markers", name="um >= 10 (flagged region of concern)",
        marker=dict(size=7, color="#16a34a"),
        text=[hover[i] for i in np.where(clean & hi)[0]], hovertemplate="%{text}<extra></extra>",
    ), row=1, col=1)
    fig.add_trace(go.Scatter(
        x=um[flagged], y=ua[flagged], mode="markers", name="likely mistake (duplicate reading)",
        marker=dict(size=11, color="#dc2626", symbol="x"),
        text=[hover[i] for i in np.where(flagged)[0]], hovertemplate="%{text}<extra></extra>",
    ), row=1, col=1)
    xs_line = np.array([um.min(), um.max()])
    fig.add_trace(go.Scatter(
        x=xs_line, y=slope_clean * xs_line + intercept_clean, mode="lines",
        name=f"fit (excl. flagged): {slope_clean:.3f}*um + {intercept_clean:.3f}, R2={r2_clean:.6f}",
        line=dict(color="black", width=2),
    ), row=1, col=1)
    fig.add_trace(go.Scatter(
        x=xs_line, y=slope_all * xs_line + intercept_all, mode="lines",
        name=f"fit (all points): {slope_all:.3f}*um + {intercept_all:.3f}, R2={r2_all:.6f}",
        line=dict(color="gray", width=1, dash="dash"),
    ), row=1, col=1)

    fig.add_trace(go.Scatter(
        x=um[clean], y=resid_clean_full[clean], mode="markers", name="residual (excl. flagged)",
        marker=dict(size=7, color=["#16a34a" if h else "#2563eb" for h in hi[clean]]),
        text=[hover[i] for i in np.where(clean)[0]], hovertemplate="%{text}<extra></extra>",
        showlegend=False,
    ), row=2, col=1)
    fig.add_trace(go.Scatter(
        x=um[flagged], y=resid_clean_full[flagged], mode="markers", name="flagged (mistake)",
        marker=dict(size=11, color="#dc2626", symbol="x"),
        text=[hover[i] for i in np.where(flagged)[0]], hovertemplate="%{text}<extra></extra>",
        showlegend=False,
    ), row=2, col=1)
    fig.add_hline(y=0, line=dict(color="black", dash="dot"), row=2, col=1)

    fig.update_xaxes(title_text="Focus entered (um)", row=2, col=1)
    fig.update_yaxes(title_text="OL current, AP_OLENS_CURRENT (uA)", row=1, col=1)
    fig.update_yaxes(title_text="Residual (uA)", row=2, col=1)
    fig.update_layout(
        height=950, margin=dict(l=60, r=30, t=80, b=60),
        legend=dict(orientation="h", yanchor="bottom", y=1.06, x=0),
    )

    html_path = os.path.join(DIAG_DIR, "ua_um_calibration.html")
    with open(html_path, "w", encoding="utf-8") as f:
        f.write(fig.to_html(include_plotlyjs="inline", full_html=True))

    fit_path = os.path.join(DIAG_DIR, "ua_um_calibration_fit.json")
    with open(fit_path, "w", encoding="utf-8") as f:
        json.dump({
            "slope_uA_per_um": slope_clean, "intercept_uA": intercept_clean, "r_squared": r2_clean,
            "n_points_total": len(rows), "n_points_excluded": int(flagged.sum()),
            "excluded_rows": [{"timestamp": rows[i]["timestamp"], "um": um[i], "ua": ua[i]}
                               for i in np.where(flagged)[0]],
            "slope_all_points_uA_per_um": slope_all, "intercept_all_points_uA": intercept_all,
        }, f, indent=2)

    print(f"\nWrote {html_path}")
    print(f"Wrote {fit_path}")


if __name__ == "__main__":
    main()
