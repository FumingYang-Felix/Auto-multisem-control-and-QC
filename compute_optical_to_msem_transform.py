#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Compute the transform between optical-image pixel coordinates (a Zeiss ZEN
Shuttle & Find optical overview .czi) and MSEM stage coordinates (electron
beam), by composing two pieces:

  A) optical_image_pixel -> optical_stage_xy
     Read directly from the CZI's own metadata (pixel size, scene center
     position, stage orientation sign) - no fitting involved.

  B) optical_stage_xy -> msem_stage_xy
     Fit from the 3 physical L-markers' positions in BOTH stage frames:
     the optical-stage positions are in the CZI's ShuttleAndFindData
     metadata; the msem-stage positions are in a markers_prev.json file
     (see auto_stage_calib.py's LMarkerPosition / load_markers_from_json).

The composed transform (A then B) maps optical_image_pixel -> msem_stage_xy
directly, plus its inverse for the reverse direction. Output uses the same
"new = A*old + t" Transform2D JSON convention as auto_stage_calib.py, so it's
compatible with apply_affine_to_section_json.py's flat a11..ty fields.

Usage:
  python compute_optical_to_msem_transform.py \
      --czi "D:\\Users\\Vlad\\Section _detection-files\\w10_dye_h31.czi" \
      --markers "D:\\Users\\Vlad\\Holder_json_files\\h31_markers_prev.json" \
      --out affine_optical_to_msem_h31.json
"""

from __future__ import annotations

import argparse
import math
import time
import xml.etree.ElementTree as ET
from typing import Dict, List, Tuple

import numpy as np
import czifile

from auto_stage_calib import (
    Transform2D,
    LMarkerPosition,
    load_markers_from_json,
    compute_affine_from_3_points,
    save_affine_json,
)


def parse_czi_shuttle_and_find(czi_path: str) -> Dict:
    """Extract everything needed for Transform A + the optical-stage marker
    positions, from CZI metadata only (never reads pixel data)."""
    czi = czifile.CziFile(czi_path)
    try:
        meta = czi.metadata()
    finally:
        czi.close()
    if not isinstance(meta, str):
        meta = ET.tostring(meta, encoding="unicode")
    root = ET.fromstring(meta)

    size_x = int(root.find(".//Information/Image/SizeX").text)
    size_y = int(root.find(".//Information/Image/SizeY").text)

    dist_x = root.find(".//Scaling/Items/Distance[@Id='X']/Value")
    dist_y = root.find(".//Scaling/Items/Distance[@Id='Y']/Value")
    pixel_size_x_um = float(dist_x.text) * 1e6
    pixel_size_y_um = float(dist_y.text) * 1e6

    scene = root.find(".//Information/Image/Dimensions/S/Scenes/Scene")
    center_x, center_y = (float(v) for v in scene.find("CenterPosition").text.split(","))

    calib = root.find(".//Appliances/Appliance/Data/ShuttleAndFindData/Calibration")
    orient = calib.find("StageOrientation")
    orientation_x = float(orient.get("X"))
    orientation_y = float(orient.get("Y"))

    markers: List[LMarkerPosition] = []
    for m in calib.findall("Markers/Marker"):
        marker_id = m.get("Id")  # e.g. "Marker:1"
        n = marker_id.split(":")[-1]
        markers.append(LMarkerPosition(
            x=float(m.get("StageXPosition")),
            y=float(m.get("StageYPosition")),
            z=float(m.get("FocusPosition", 0.0)),
            name=f"M{n}",
        ))

    return dict(
        size_x=size_x, size_y=size_y,
        pixel_size_x_um=pixel_size_x_um, pixel_size_y_um=pixel_size_y_um,
        center_x=center_x, center_y=center_y,
        orientation_x=orientation_x, orientation_y=orientation_y,
        markers=markers,
    )


def build_transform_a(info: Dict) -> Transform2D:
    """optical_image_pixel -> optical_stage_xy, straight from CZI metadata."""
    cx_px, cy_px = info["size_x"] / 2.0, info["size_y"] / 2.0
    sx = info["orientation_x"] * info["pixel_size_x_um"]
    sy = info["orientation_y"] * info["pixel_size_y_um"]
    return Transform2D(
        a11=sx, a12=0.0,
        a21=0.0, a22=sy,
        tx=info["center_x"] - sx * cx_px,
        ty=info["center_y"] - sy * cy_px,
    )


def compose(t2: Transform2D, t1: Transform2D) -> Transform2D:
    """Return the transform equivalent to applying t1 then t2 (t2 after t1)."""
    m1 = np.array([[t1.a11, t1.a12], [t1.a21, t1.a22]])
    m2 = np.array([[t2.a11, t2.a12], [t2.a21, t2.a22]])
    v1 = np.array([t1.tx, t1.ty])
    v2 = np.array([t2.tx, t2.ty])
    m_c = m2 @ m1
    v_c = m2 @ v1 + v2
    return Transform2D(
        a11=float(m_c[0, 0]), a12=float(m_c[0, 1]),
        a21=float(m_c[1, 0]), a22=float(m_c[1, 1]),
        tx=float(v_c[0]), ty=float(v_c[1]),
    )


def invert(t: Transform2D) -> Transform2D:
    m = np.array([[t.a11, t.a12], [t.a21, t.a22]])
    v = np.array([t.tx, t.ty])
    m_inv = np.linalg.inv(m)
    v_inv = -m_inv @ v
    return Transform2D(
        a11=float(m_inv[0, 0]), a12=float(m_inv[0, 1]),
        a21=float(m_inv[1, 0]), a22=float(m_inv[1, 1]),
        tx=float(v_inv[0]), ty=float(v_inv[1]),
    )


def decompose_scale_rotation(t: Transform2D) -> Tuple[float, float, float]:
    """Rough scale_x, scale_y, rotation_deg for a plausibility sanity check
    (assumes no significant shear)."""
    scale_x = math.hypot(t.a11, t.a21)
    scale_y = math.hypot(t.a12, t.a22)
    rotation_deg = math.degrees(math.atan2(t.a21, t.a11))
    return scale_x, scale_y, rotation_deg


def transform_to_dict(t: Transform2D) -> Dict[str, float]:
    return {"a11": t.a11, "a12": t.a12, "a21": t.a21, "a22": t.a22, "tx": t.tx, "ty": t.ty}


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--czi", required=True, help="Optical Shuttle & Find overview .czi file")
    ap.add_argument("--markers", required=True, help="MSEM-stage markers_prev.json (l_markers: M1/M2/M3)")
    ap.add_argument("--out", default=None, help="Output affine JSON path (default: timestamped)")
    args = ap.parse_args()

    info = parse_czi_shuttle_and_find(args.czi)
    print("[CZI metadata]")
    print(f"  Image size: {info['size_x']} x {info['size_y']} px")
    print(f"  Pixel size: {info['pixel_size_x_um']:.8f} x {info['pixel_size_y_um']:.8f} um/px")
    print(f"  Scene center (optical stage): ({info['center_x']:.3f}, {info['center_y']:.3f}) um")
    print(f"  StageOrientation: X={info['orientation_x']:.0f} Y={info['orientation_y']:.0f}")

    transform_a = build_transform_a(info)
    print("\n[Transform A] optical_pixel -> optical_stage")
    print(f"  {transform_to_dict(transform_a)}")

    msem_markers = load_markers_from_json(args.markers)
    msem_by_name = {m.name: m for m in msem_markers}
    optical_by_name = {m.name: m for m in info["markers"]}

    common_names = sorted(set(msem_by_name) & set(optical_by_name))
    if len(common_names) != 3:
        raise SystemExit(
            f"Expected 3 matching marker names between CZI ({sorted(optical_by_name)}) "
            f"and markers file ({sorted(msem_by_name)}), got common={common_names}"
        )

    print("\n[Marker correspondence] (name: optical_stage_xy -> msem_stage_xy)")
    old_pts, new_pts = [], []
    for name in common_names:
        om = optical_by_name[name]
        mm = msem_by_name[name]
        print(f"  {name}: ({om.x:.3f}, {om.y:.3f})  ->  ({mm.x:.3f}, {mm.y:.3f})")
        old_pts.append((om.x, om.y))
        new_pts.append((mm.x, mm.y))

    transform_b = compute_affine_from_3_points(old_pts, new_pts)
    print("\n[Transform B] optical_stage -> msem_stage (fitted from 3 markers)")
    print(f"  {transform_to_dict(transform_b)}")

    forward = compose(transform_b, transform_a)  # pixel -> optical_stage -> msem_stage
    backward = invert(forward)

    scale_x, scale_y, rotation_deg = decompose_scale_rotation(forward)
    print("\n[Composed] optical_image_pixel -> msem_stage_xy")
    print(f"  {transform_to_dict(forward)}")
    print(f"  Effective scale: {scale_x:.4f} x {scale_y:.4f} um(msem)/px   Rotation: {rotation_deg:.3f} deg")
    if abs(rotation_deg) > 20 or abs(scale_x - scale_y) / max(scale_x, scale_y) > 0.1:
        print("  WARNING: large rotation or very unequal X/Y scale - double-check marker correspondence/orientation assumptions.")

    out_path = args.out
    if out_path is None:
        ts = time.strftime("%Y%m%d_%H%M%S")
        out_path = f"affine_optical_to_msem_{ts}.json"

    save_affine_json(
        forward,
        out_path,
        extra={
            "from_frame": "optical_image_pixel",
            "to_frame": "msem_stage_xy",
            "inverse": transform_to_dict(backward),
            "czi_file": args.czi,
            "markers_file": args.markers,
            "marker_correspondence": [
                {"name": name, "optical_stage_xy": list(old_pts[i]), "msem_stage_xy": list(new_pts[i])}
                for i, name in enumerate(common_names)
            ],
            "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        },
    )
    print(f"\nSaved: {out_path}")


if __name__ == "__main__":
    main()
