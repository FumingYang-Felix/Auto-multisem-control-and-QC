#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Apply 2D affine transform (new = A*old + t) to section JSON whose "Regions" field contains XML strings.

It updates (for selected region names):
- SupportPoints/SupportPoint/X, Y
- Contour/Points text: "x,y x,y ..."
- CenterPosition text: "x,y"

It does NOT modify:
- Z
- OL
- ProjectiveX / ProjectiveY

Usage:
  python apply_affine_to_section_json.py --in 3min_stainh25.json --out 3min_stainh25_reload1.json \
      --affine_json V:\\Fuming\\msem_calib\\outputs\\affine_reload_*.json --region S2
"""

import argparse
import json
import xml.etree.ElementTree as ET
from typing import Tuple, Optional, List, Dict, Any


def apply_affine_xy(x: float, y: float, a11: float, a12: float, a21: float, a22: float, tx: float, ty: float) -> Tuple[float, float]:
    return (a11 * x + a12 * y + tx, a21 * x + a22 * y + ty)


def parse_points_string(points_str: str) -> List[Tuple[float, float]]:
    pts = []
    for token in (points_str or "").strip().split():
        if "," not in token:
            continue
        xs, ys = token.split(",", 1)
        try:
            pts.append((float(xs), float(ys)))
        except ValueError:
            pass
    return pts


def format_points_string(pts: List[Tuple[float, float]]) -> str:
    return " ".join([f"{x:.6f},{y:.6f}" for (x, y) in pts])


def parse_xy_pair(text: str) -> Optional[Tuple[float, float]]:
    if not text:
        return None
    s = text.strip()
    if "," not in s:
        return None
    xs, ys = s.split(",", 1)
    try:
        return float(xs), float(ys)
    except ValueError:
        return None


def format_xy_pair(x: float, y: float) -> str:
    return f"{x:.6f},{y:.6f}"


def transform_region_xml(
    region_xml: str,
    target_region: str,
    a11: float, a12: float, a21: float, a22: float, tx: float, ty: float
) -> Tuple[str, bool]:
    root = ET.fromstring(region_xml)
    # target_region=None means "transform EVERY region" (--all_regions mode)
    if target_region is not None and (root.get("Name") or "") != target_region:
        return region_xml, False

    changed = False

    # CenterPosition
    cp = root.find("CenterPosition")
    if cp is not None and cp.text:
        xy = parse_xy_pair(cp.text)
        if xy is not None:
            x, y = xy
            x2, y2 = apply_affine_xy(x, y, a11, a12, a21, a22, tx, ty)
            cp.text = format_xy_pair(x2, y2)
            changed = True

    # Contour/Points
    contour = root.find("Contour")
    if contour is not None:
        pts_node = contour.find("Points")
        if pts_node is not None and pts_node.text:
            pts = parse_points_string(pts_node.text)
            if pts:
                pts2 = [apply_affine_xy(x, y, a11, a12, a21, a22, tx, ty) for (x, y) in pts]
                pts_node.text = format_points_string(pts2)
                changed = True

    # SupportPoints
    sp_root = root.find("SupportPoints")
    if sp_root is not None:
        for sp in sp_root.findall("SupportPoint"):
            x_node = sp.find("X")
            y_node = sp.find("Y")
            if x_node is None or y_node is None:
                continue
            try:
                x = float((x_node.text or "").strip())
                y = float((y_node.text or "").strip())
            except ValueError:
                continue
            x2, y2 = apply_affine_xy(x, y, a11, a12, a21, a22, tx, ty)
            x_node.text = f"{x2:.6f}"
            y_node.text = f"{y2:.6f}"
            changed = True

    new_xml = ET.tostring(root, encoding="unicode")
    return new_xml, changed


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="in_path", required=True)
    ap.add_argument("--out", dest="out_path", required=True)
    ap.add_argument("--region", default=None, help="Region Name to transform (single-region mode).")
    ap.add_argument("--all_regions", action="store_true", help="Transform EVERY region in the JSON at once.")

    ap.add_argument("--affine_json", required=True, help="JSON with a11,a12,a21,a22,tx,ty (new=A*old+t).")
    ap.add_argument("--force", action="store_true", help="Apply even if AffineApplied exists (avoid double-apply).")

    args = ap.parse_args()

    if bool(args.region) == bool(args.all_regions):
        raise SystemExit("Specify exactly one of --region <name> or --all_regions.")

    with open(args.affine_json, "r", encoding="utf-8") as f:
        A = json.load(f)

    a11 = float(A["a11"]); a12 = float(A["a12"])
    a21 = float(A["a21"]); a22 = float(A["a22"])
    tx  = float(A["tx"]);  ty  = float(A["ty"])

    with open(args.in_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    if (not args.force) and ("AffineApplied" in data):
        raise SystemExit(
            "This JSON already contains 'AffineApplied'. Refusing to apply again. Use --force if needed."
        )

    regions = data.get("Regions", [])
    if not isinstance(regions, list) or not regions:
        raise SystemExit("JSON has no 'Regions' list.")

    target = None if args.all_regions else args.region
    changed_any = False
    changed_names = []
    new_regions = []
    for rxml in regions:
        if not isinstance(rxml, str):
            new_regions.append(rxml)
            continue
        new_xml, changed = transform_region_xml(
            rxml, target_region=target,
            a11=a11, a12=a12, a21=a21, a22=a22, tx=tx, ty=ty
        )
        new_regions.append(new_xml)
        if changed:
            changed_any = True
            try:
                changed_names.append(ET.fromstring(new_xml).get("Name") or "?")
            except Exception:
                changed_names.append("?")

    data["Regions"] = new_regions
    data["AffineApplied"] = {
        "region": "ALL" if args.all_regions else args.region,
        "regions_changed": changed_names,
        "n_changed": len(changed_names),
        "a11": a11, "a12": a12, "a21": a21, "a22": a22, "tx": tx, "ty": ty,
        "equation": "new = A*old + t",
    }

    with open(args.out_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)

    print(f"[OK] wrote: {args.out_path} changed={changed_any} "
          f"n_regions={len(changed_names)} regions={changed_names}")
    print(f"     affine: [[{a11},{a12}],[{a21},{a22}]] + ({tx},{ty})")


if __name__ == "__main__":
    main()