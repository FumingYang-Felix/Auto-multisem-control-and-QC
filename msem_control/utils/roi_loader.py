import csv
import json
from typing import List, Tuple

def load_rois_from_csv(filepath: str) -> List[List[Tuple[float, float]]]:
    """
    Load ROI polygons from aligned_polygons.csv format.
    
    Expected CSV format:
        id,type,contour_coordinates,center_coordinates,rotation
        section_1,new_mask,"[[x1, y1], [x2, y2], ...]","[cx, cy]",120.65
    
    The contour_coordinates column contains a JSON array of [x, y] pairs.
    
    Returns:
        List of polygons, where each polygon is a list of (x, y) tuples.
    """
    polygons = []
    
    with open(filepath, 'r', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        
        for row in reader:
            try:
                # Parse the contour_coordinates JSON string
                contour_str = row.get('contour_coordinates', '[]')
                
                # The CSV may have the coordinates as a JSON string
                contour_points = json.loads(contour_str)
                
                # Convert to list of tuples
                points = [(float(pt[0]), float(pt[1])) for pt in contour_points]
                
                if len(points) >= 3:
                    polygons.append(points)
                    
            except (json.JSONDecodeError, ValueError, TypeError, KeyError) as e:
                # Skip malformed rows
                row_id = row.get('id', 'unknown')
                print(f"Warning: Skipping row '{row_id}': {e}")
                continue
    
    return polygons


def load_rois_with_metadata(filepath: str) -> List[dict]:
    """
    Load ROI polygons with all metadata from CSV.
    
    Returns:
        List of dicts with keys: id, type, points, center, rotation
    """
    rois = []
    
    with open(filepath, 'r', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        
        for row in reader:
            try:
                contour_str = row.get('contour_coordinates', '[]')
                contour_points = json.loads(contour_str)
                points = [(float(pt[0]), float(pt[1])) for pt in contour_points]
                
                center_str = row.get('center_coordinates', '[0, 0]')
                center = json.loads(center_str)
                
                roi = {
                    'id': row.get('id', ''),
                    'type': row.get('type', ''),
                    'points': points,
                    'center': (float(center[0]), float(center[1])),
                    'rotation': float(row.get('rotation', 0))
                }
                
                if len(points) >= 3:
                    rois.append(roi)
                    
            except (json.JSONDecodeError, ValueError, TypeError) as e:
                row_id = row.get('id', 'unknown')
                print(f"Warning: Skipping row '{row_id}': {e}")
                continue
    
    return rois
