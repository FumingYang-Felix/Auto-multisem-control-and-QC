import json
from pathlib import Path

import numpy as np
import matplotlib.pyplot as plt
from mpl_toolkits.mplot3d import proj3d  # noqa: F401  (registers 3d projection)

DIAG_DIR = Path(r"D:\Users\Vlad\Section _detection-files\diagnostics\w10h31_220526")
DATA_FILE = DIAG_DIR / "focus_data_220526.json"
FIDUCIAL_FILE = DIAG_DIR / "wafer_fiducial_used.txt"

with open(DATA_FILE) as f:
    data = json.load(f)

rows = data["rows"]
plane_pred = data["plane"]  # {'a','b','c'} fit used to command OL this run

region = np.array([r["region"] for r in rows])
mfov = np.array([r["mfov"] for r in rows])
x = np.array([r["x"] for r in rows])
y = np.array([r["y"] for r in rows])
z_meas = np.array([r["z"] for r in rows])
ol_pred = np.array([r["ol_pred"] for r in rows])
ol_meas = np.array([r["ol_measured"] for r in rows])
quality = np.array([r["quality"] for r in rows])
failed = quality <= -99


def fit_plane(px, py, pz):
    """least-squares z = a*px + b*py + c"""
    A = np.column_stack([px, py, np.ones_like(px)])
    coeffs, *_ = np.linalg.lstsq(A, pz, rcond=None)
    return {"a": coeffs[0], "b": coeffs[1], "c": coeffs[2]}


def eval_plane(plane, px, py):
    return plane["a"] * px + plane["b"] * py + plane["c"]


# --- fitted plane for the *measured* OL, independent of the predicted one ---
plane_meas = fit_plane(x, y, ol_meas)

# --- expected Z: fit from the wafer-fiducial file actually used for this run ---
fid_x, fid_y, fid_z = [], [], []
with open(FIDUCIAL_FILE) as f:
    next(f)  # header
    for line in f:
        line = line.strip()
        if not line:
            continue
        parts = line.split(",")
        fid_x.append(float(parts[0]))
        fid_y.append(float(parts[1]))
        fid_z.append(float(parts[2]))
fid_x, fid_y, fid_z = map(np.array, (fid_x, fid_y, fid_z))
plane_z = fit_plane(fid_x, fid_y, fid_z)
z_expected = eval_plane(plane_z, x, y)

# --- report the headline finding to the console ---
print("=" * 70)
print(f"Expected Z (from wafer-fiducial plane): ~{z_expected.mean():.3f} um "
      f"(fiducial Z itself is flat: {fid_z.min():.3f}-{fid_z.max():.3f} um)")
print(f"Measured Z across the run: {z_meas.min():.3f} to {z_meas.max():.3f} um")
bad_z = np.unique(z_meas[failed])
good_z = np.unique(z_meas[~failed])
print(f"Failed-region Z values:  {bad_z}")
print(f"Succeeded-region Z values: {good_z}")
if bad_z.size and good_z.size:
    print(f"--> stage Z was ~{good_z[0] - bad_z[0]:.1f} um LOW during the 19 failed regions "
          f"({bad_z[0]:.1f} vs the correct/expected {good_z[0]:.1f} um)")
print("=" * 70)

fig = plt.figure(figsize=(20, 7))

# ============================================================
# Panel A (3D): expected vs measured OL, both point clouds + both fitted planes
# ============================================================
axA = fig.add_subplot(1, 3, 1, projection="3d")
axA.scatter(x, y, ol_pred, s=22, c="tab:blue", label="expected OL (plane)", depthshade=False)
axA.scatter(x, y, ol_meas, s=14, c="tab:orange", marker="^", label="measured OL (Lens)", depthshade=False)

gx = np.linspace(x.min(), x.max(), 12)
gy = np.linspace(y.min(), y.max(), 12)
GX, GY = np.meshgrid(gx, gy)
axA.plot_surface(GX, GY, eval_plane(plane_pred, GX, GY), color="tab:blue", alpha=0.15, linewidth=0)
axA.plot_surface(GX, GY, eval_plane(plane_meas, GX, GY), color="tab:orange", alpha=0.15, linewidth=0)

axA.set_xlabel("Stage X (um)")
axA.set_ylabel("Stage Y (um)")
axA.set_zlabel("OL (uA)")
axA.set_title("Expected vs measured OL\n(both planes + both point clouds)")
axA.legend(loc="upper left", fontsize=8)

# ============================================================
# Panel B (2D): expected vs measured OL identity check (unchanged, kept as-is)
# ============================================================
axB = fig.add_subplot(1, 3, 2)
ok = ~failed
axB.scatter(ol_pred[ok], ol_meas[ok], s=22, c="tab:green", label="succeeded", alpha=0.8)
axB.scatter(ol_pred[failed], ol_meas[failed], s=22, c="tab:red", label="failed (LiveQC -100)", alpha=0.6)
vmin = min(ol_pred.min(), ol_meas.min())
vmax = max(ol_pred.max(), ol_meas.max())
lims = [vmin - 1, vmax + 1]
axB.plot(lims, lims, "k--", linewidth=1, label="y = x")
axB.set_xlim(lims)
axB.set_ylim(lims)
axB.set_xlabel("Expected (predicted) OL (uA)")
axB.set_ylabel("Measured OL (uA)")
axB.set_title("Expected vs measured OL (269 tiles)")
axB.set_aspect("equal", adjustable="box")
axB.legend(loc="upper left", fontsize=9)
resid = ol_meas - ol_pred
axB.text(
    0.02, 0.02,
    f"max |residual| = {np.max(np.abs(resid)):.4f} uA\nmean residual = {np.mean(resid):.6f} uA",
    transform=axB.transAxes, fontsize=9, va="bottom",
)

# ============================================================
# Panel C (3D): actual (measured) x,y,z vs. expected x,y,z position
# ============================================================
axC = fig.add_subplot(1, 3, 3, projection="3d")
axC.scatter(x[ok], y[ok], z_meas[ok], s=26, c="tab:green", label="measured, succeeded", depthshade=False)
axC.scatter(x[failed], y[failed], z_meas[failed], s=26, c="tab:red", label="measured, FAILED", depthshade=False)
axC.scatter(x, y, z_expected, s=10, c="black", marker="x", label="expected (fiducial plane)", depthshade=False)
axC.plot_surface(GX, GY, eval_plane(plane_z, GX, GY), color="gray", alpha=0.12, linewidth=0)

axC.set_xlabel("Stage X (um)")
axC.set_ylabel("Stage Y (um)")
axC.set_zlabel("Z (um)")
axC.set_title("Actual (measured) vs. expected stage Z")
axC.legend(loc="upper left", fontsize=8)

fig.suptitle(
    "w10h31_dye_roi_R2_2_focus_8NM_20260901_220526 — expected vs. measured (269 mFOVs, 23 regions)\n"
    "drag to rotate 3D panels · hover a point for details",
    fontsize=11,
)
fig.tight_layout(rect=[0, 0, 1, 0.90])


# ============================================================
# Shared hover tooltip across 2D and 3D panels
# ============================================================
def tile_text(i, extra=""):
    z_line = f"measured Z={z_meas[i]:.1f} um  |  expected Z={z_expected[i]:.1f} um\n" if extra == "z" else ""
    return (
        f"{region[i]} / {mfov[i]}\n"
        f"x={x[i]:.0f}, y={y[i]:.0f} um\n"
        f"{z_line}"
        f"expected OL={ol_pred[i]:.3f} uA\n"
        f"measured OL={ol_meas[i]:.3f} uA\n"
        f"quality={'FAILED' if failed[i] else f'{quality[i]:.1f}'}"
    )


annots = {}
for ax in (axA, axB, axC):
    a = ax.annotate(
        "", xy=(0, 0), xytext=(15, 15), textcoords="offset points",
        bbox=dict(boxstyle="round", fc="lightyellow", ec="gray"), fontsize=8,
        arrowprops=dict(arrowstyle="->"),
    )
    a.set_visible(False)
    annots[ax] = a


def screen_xy(ax, xs, ys, zs=None):
    if zs is None:
        return ax.transData.transform(np.column_stack([xs, ys]))
    txs, tys, _ = proj3d.proj_transform(xs, ys, zs, ax.get_proj())
    return ax.transData.transform(np.column_stack([txs, tys]))


def on_move(event):
    for ax, a in annots.items():
        if event.inaxes is not ax:
            if a.get_visible():
                a.set_visible(False)
                fig.canvas.draw_idle()
            continue

        if ax is axA:
            disp1 = screen_xy(ax, x, y, ol_pred)
            disp2 = screen_xy(ax, x, y, ol_meas)
            d1 = np.hypot(disp1[:, 0] - event.x, disp1[:, 1] - event.y)
            d2 = np.hypot(disp2[:, 0] - event.x, disp2[:, 1] - event.y)
            i1, i2 = int(np.argmin(d1)), int(np.argmin(d2))
            i, dmin = (i1, d1[i1]) if d1[i1] < d2[i2] else (i2, d2[i2])
            if dmin < 15:
                a.xy = (event.xdata, event.ydata) if event.xdata is not None else (x[i], y[i])
                a.set_text(tile_text(i))
                a.set_visible(True)
            else:
                a.set_visible(False)

        elif ax is axB:
            dispp = ax.transData.transform(np.column_stack([ol_pred, ol_meas]))
            d = np.hypot(dispp[:, 0] - event.x, dispp[:, 1] - event.y)
            i = int(np.argmin(d))
            if d[i] < 15:
                a.xy = (ol_pred[i], ol_meas[i])
                a.set_text(tile_text(i))
                a.set_visible(True)
            else:
                a.set_visible(False)

        elif ax is axC:
            dispm = screen_xy(ax, x, y, z_meas)
            dispe = screen_xy(ax, x, y, z_expected)
            dm = np.hypot(dispm[:, 0] - event.x, dispm[:, 1] - event.y)
            de = np.hypot(dispe[:, 0] - event.x, dispe[:, 1] - event.y)
            im, ie = int(np.argmin(dm)), int(np.argmin(de))
            i, dmin = (im, dm[im]) if dm[im] < de[ie] else (ie, de[ie])
            if dmin < 15:
                a.xy = (event.xdata, event.ydata) if event.xdata is not None else (x[i], y[i])
                a.set_text(tile_text(i, extra="z"))
                a.set_visible(True)
            else:
                a.set_visible(False)

        fig.canvas.draw_idle()


fig.canvas.mpl_connect("motion_notify_event", on_move)

plt.show()
