"""Ad-hoc batch runner: compute_single_image_quality() over every .bmp in a
folder, written for the MATLAB-vs-Python IMQA verification exercise.
Not part of the pipeline; safe to delete after use.
"""
import csv
import sys
from pathlib import Path

from qc_algorithm import compute_single_image_quality

folder = Path(sys.argv[1])
out_csv = Path(sys.argv[2]) if len(sys.argv) > 2 else Path("python_results.csv")

files = sorted(folder.glob("*.bmp"))
rows = []
for i, f in enumerate(files, 1):
    print(f"[{i}/{len(files)}] {f.name}")
    r = compute_single_image_quality(f)
    rows.append(r.to_dict())

with open(out_csv, "w", newline="") as fh:
    writer = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
    writer.writeheader()
    writer.writerows(rows)

print(f"\nWrote {len(rows)} rows to {out_csv}")
