"""
ZEISS MultiSEM Experiment JSON Parser

Parses the experiment JSON files exported from ZEN MultiSEM software.
"""
import json
import re
from typing import List, Tuple, Dict, Any, Optional
from dataclasses import dataclass


@dataclass
class SupportPoint:
    """Autofocus support point"""
    x: float
    y: float
    z: float
    ol: float = 0.0  # Objective lens focus
    ol_tilt_x: float = 0.0
    ol_tilt_y: float = 0.0


@dataclass 
class Region:
    """ROI Region from ZEISS experiment"""
    name: str
    z: float
    center_x: float
    center_y: float
    contour_width: float
    contour_height: float
    polygon_points: List[Tuple[float, float]]
    support_points: List[SupportPoint]
    is_used_for_acquisition: bool = False
    ol_focus: float = 0.0
    beam2scan_a: float = 0.0
    beam2scan_b: float = 0.0
    beam2scan_c: float = 0.0
    beam2scan_d: float = 0.0


@dataclass
class ZeissExperiment:
    """Parsed ZEISS experiment data"""
    id: str
    name: str
    experiment_path: str
    operator: str
    description: str
    sample: str
    optical_images: List[str]
    stage_settling_time: float
    beam_off_at_end: bool
    z_lowering_distance: float
    max_distance_without_lowering: float
    regions: List[Region]
    datetime: str


def parse_zeiss_json(filepath: str) -> Optional[ZeissExperiment]:
    """
    Parse a ZEISS MultiSEM experiment JSON file.
    
    Args:
        filepath: Path to the JSON file
        
    Returns:
        ZeissExperiment object or None if parsing fails
    """
    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            data = json.load(f)
            
        return parse_zeiss_dict(data)
        
    except Exception as e:
        print(f"Error parsing ZEISS JSON: {e}")
        return None


def parse_zeiss_dict(data: dict) -> ZeissExperiment:
    """Parse ZEISS experiment from dictionary"""
    exp = data.get("ExperimentParameter", {})
    
    # Parse optical image paths
    images = []
    for img_str in exp.get("Images", []):
        # Format: "path=Z:\...\file.czi;sceneindex=0"
        if "path=" in img_str:
            path = img_str.split("path=")[1].split(";")[0]
            images.append(path)
    
    # Parse regions
    regions = []
    for region_xml in data.get("Regions", []):
        region = parse_region_xml(region_xml)
        if region:
            regions.append(region)
    
    return ZeissExperiment(
        id=data.get("_id", ""),
        name=data.get("Name", exp.get("ExperimentName", "")),
        experiment_path=exp.get("ExperimentPath", ""),
        operator=exp.get("ExpOperator", ""),
        description=exp.get("Description", ""),
        sample=exp.get("Sample", ""),
        optical_images=images,
        stage_settling_time=exp.get("StageSettlingTime", 0.8),
        beam_off_at_end=exp.get("BeamOffAtEnd", False),
        z_lowering_distance=exp.get("ZLoweringDistance", 2000.0),
        max_distance_without_lowering=exp.get("MaxDistanceWithoutLowering", 1000.0),
        regions=regions,
        datetime=data.get("DateTime", "")
    )


def parse_region_xml(xml_str: str) -> Optional[Region]:
    """Parse a single region from XML string"""
    try:
        # Extract attributes from <Region> tag
        name = _extract_attr(xml_str, "Name", "Unknown")
        z = float(_extract_attr(xml_str, "Z", "0"))
        ol_focus = float(_extract_attr(xml_str, "OLFocus", "0"))
        beam2scan_a = float(_extract_attr(xml_str, "Beam2ScanA", "0"))
        beam2scan_b = float(_extract_attr(xml_str, "Beam2ScanB", "0"))
        beam2scan_c = float(_extract_attr(xml_str, "Beam2ScanC", "0"))
        beam2scan_d = float(_extract_attr(xml_str, "Beam2ScanD", "0"))
        
        # Extract CenterPosition
        center_match = re.search(r'<CenterPosition>([^,]+),([^<]+)</CenterPosition>', xml_str)
        center_x = float(center_match.group(1)) if center_match else 0
        center_y = float(center_match.group(2)) if center_match else 0
        
        # Extract ContourSize
        size_match = re.search(r'<ContourSize>([^,]+),([^<]+)</ContourSize>', xml_str)
        contour_w = float(size_match.group(1)) if size_match else 0
        contour_h = float(size_match.group(2)) if size_match else 0
        
        # Extract IsUsedForAcquisition
        used_match = re.search(r'<IsUsedForAcquisition>(\w+)</IsUsedForAcquisition>', xml_str)
        is_used = used_match.group(1).lower() == "true" if used_match else False
        
        # Extract polygon points
        points_match = re.search(r'<Points>([^<]+)</Points>', xml_str)
        polygon_points = []
        if points_match:
            points_str = points_match.group(1)
            # Format: "x1,y1 x2,y2 x3,y3 ..."
            for point in points_str.strip().split():
                coords = point.split(',')
                if len(coords) == 2:
                    polygon_points.append((float(coords[0]), float(coords[1])))
        
        # Extract support points
        support_points = []
        sp_pattern = r'<SupportPoint[^>]*>.*?<X>([^<]+)</X>.*?<Y>([^<]+)</Y>.*?<Z>([^<]+)</Z>.*?<OL>([^<]+)</OL>.*?</SupportPoint>'
        for match in re.finditer(sp_pattern, xml_str, re.DOTALL):
            sp = SupportPoint(
                x=float(match.group(1)),
                y=float(match.group(2)),
                z=float(match.group(3)),
                ol=float(match.group(4))
            )
            support_points.append(sp)
        
        return Region(
            name=name,
            z=z,
            center_x=center_x,
            center_y=center_y,
            contour_width=contour_w,
            contour_height=contour_h,
            polygon_points=polygon_points,
            support_points=support_points,
            is_used_for_acquisition=is_used,
            ol_focus=ol_focus,
            beam2scan_a=beam2scan_a,
            beam2scan_b=beam2scan_b,
            beam2scan_c=beam2scan_c,
            beam2scan_d=beam2scan_d
        )
        
    except Exception as e:
        print(f"Error parsing region XML: {e}")
        return None


def _extract_attr(xml_str: str, attr_name: str, default: str = "") -> str:
    """Extract an attribute value from XML string"""
    pattern = f'{attr_name}="([^"]*)"'
    match = re.search(pattern, xml_str)
    return match.group(1) if match else default


def get_all_polygons(experiment: ZeissExperiment) -> List[List[Tuple[float, float]]]:
    """Get all polygon ROIs from experiment for display"""
    polygons = []
    for region in experiment.regions:
        if region.polygon_points:
            polygons.append(region.polygon_points)
    return polygons


def get_all_support_points(experiment: ZeissExperiment) -> List[Tuple[float, float, float]]:
    """Get all support points (autofocus points) from experiment"""
    points = []
    for region in experiment.regions:
        for sp in region.support_points:
            points.append((sp.x, sp.y, sp.z))
    return points


# === Test ===
if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1:
        exp = parse_zeiss_json(sys.argv[1])
        if exp:
            print(f"Experiment: {exp.name}")
            print(f"Operator: {exp.operator}")
            print(f"Path: {exp.experiment_path}")
            print(f"Optical Images: {len(exp.optical_images)}")
            print(f"Regions: {len(exp.regions)}")
            
            for i, region in enumerate(exp.regions[:3]):
                print(f"\n  Region {i+1}: {region.name}")
                print(f"    Center: ({region.center_x:.1f}, {region.center_y:.1f})")
                print(f"    Z: {region.z:.1f} µm")
                print(f"    Polygon points: {len(region.polygon_points)}")
                print(f"    Support points: {len(region.support_points)}")

