import csv
import statistics as st
from pathlib import Path

base = Path(r"D:\mSEM_workflow_manager\WholeImageQC")

with open(base / "matlab_results_0281.csv") as f:
    matlab_rows = {r["fname"]: r for r in csv.DictReader(f)}

with open(base / "python_results_0281.csv") as f:
    python_rows = {Path(r["image_path"]).name: r for r in csv.DictReader(f)}

names = sorted(matlab_rows.keys(), key=lambda n: int(n.split("__")[1].split(".")[0]))

def f(x):
    try:
        return float(x)
    except Exception:
        return float("nan")

fields = [
    ("quality", "fixed_quality", "quality"),
    ("horz_qual", "fixed_horz", "horz_qual"),
    ("vert_qual", "fixed_vert", "vert_qual"),
    ("std_high", "fixed_stdhigh", "std_high"),
    ("range_val", "fixed_range", "range_val"),
    ("std_all", "fixed_stdall", "std_all"),
    ("percent_saturation", "fixed_sat", "percent_saturation"),
]

print(f"{'name':<22}{'status(M/P)':<16}", "  ".join(f"{lbl:>12}" for lbl,_,_ in fields))
diffs = {lbl: [] for lbl,_,_ in fields}
status_mismatch = []
big_quality_diff = []

for name in names:
    m = matlab_rows[name]
    p = python_rows.get(name)
    if p is None:
        print(f"{name:<22} MISSING FROM PYTHON RESULTS")
        continue
    mstat, pstat = m["fixed_status"], p["status"]
    row_strs = []
    for lbl, mcol, pcol in fields:
        mv, pv = f(m[mcol]), f(p[pcol])
        d = mv - pv
        diffs[lbl].append(d)
        row_strs.append(f"{mv:6.2f}/{pv:6.2f}")
    print(f"{name:<22}{mstat+'/'+pstat:<16}", "  ".join(row_strs))
    if mstat != pstat:
        status_mismatch.append((name, mstat, pstat))
    qd = f(m["fixed_quality"]) - f(p["quality"])
    if abs(qd) > 2.0:
        big_quality_diff.append((name, f(m["fixed_quality"]), f(p["quality"]), qd))

print("\n=== Summary over", len(names), "images ===")
for lbl, _, _ in fields:
    d = diffs[lbl]
    print(f"{lbl:<20} mean_diff={st.mean(d):8.4f}  max_abs_diff={max(abs(x) for x in d):8.4f}  stdev_diff={st.pstdev(d):8.4f}")

print("\nStatus mismatches (MATLAB fixed vs Python):", len(status_mismatch))
for n, ms, ps in status_mismatch:
    print(" ", n, ms, "vs", ps)

print("\nImages with |quality diff| > 2.0:", len(big_quality_diff))
for n, mv, pv, d in big_quality_diff:
    print(f"  {n}: matlab={mv:.3f} python={pv:.3f} diff={d:.3f}")
