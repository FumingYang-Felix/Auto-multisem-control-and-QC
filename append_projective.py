import pandas as pd

file_path = r"D:\Users\Vlad\Section _detection-files\w10h31_region_fiducials.txt"

projective_x = [
    -609.9, -605.9, -609.9, -608.2, -609.7, -607.2,
    -612.4, -606.2, -616.4, -604.6, -611.2, -607.4,
    -608.3, -606.6, -609.5, -606.1, -604.6, -609.2,
    -604.9, -606.2, -607.6, -608.7, -608.2, -605.1,
    -610.9, -605.2
]

projective_y = [
    669.8, 667.7, 670.1, 669.8, 669.2, 671.4,
    668.3, 670.8, 683.9, 669.5, 667.5, 669.1,
    669.8, 671.5, 669.6, 670.6, 673.3, 669.2,
    670.8, 670.4, 670.1, 669.1, 669.4, 699.4,
    668.4, 670.4
]

df = pd.read_csv(file_path)

if len(df) != len(projective_x):
    raise ValueError(
        f"Row count mismatch: CSV has {len(df)} rows, "
        f"but {len(projective_x)} projective-value rows were provided."
    )

df["projective_x"] = projective_x
df["projective_y"] = projective_y

df.to_csv(file_path, index=False)

print(f"Updated successfully: {file_path}")