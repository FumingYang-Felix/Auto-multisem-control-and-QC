"""
Test script for ROI (Region of Interest) acquisition.
Run this on a machine connected to MultiSEM.

Full API control without ZEN dependency.

WARNING: This will actually move the stage and acquire images!
"""

import sys
from pathlib import Path

# Add parent directory to path so we can import msem
sys.path.insert(0, str(Path(__file__).parent.parent))

from msem import (
    MSEMConnection,
    ROIAcquisition,
    ROIConfig,
    TravelPattern,
    TravelDirection,
    StartPosition,
    ROIProgress,
)


def progress_callback(progress: ROIProgress) -> None:
    """Print progress updates"""
    print(
        f"  Tile {progress.current_tile}/{progress.total_tiles} "
        f"({progress.percent_complete:.1f}%) - "
        f"Position: ({progress.current_x:.1f}, {progress.current_y:.1f}) µm"
    )


def main():
    # 1. Connect
    print("Connecting to MultiSEM...")
    conn = MSEMConnection()
    ret = conn.connect_and_initialize()
    print(f"Initialize returned: {ret}")
    
    if ret != 0:
        print(f"ERROR: {conn.get_last_error()}")
        return
    
    # 2. Check beam status
    is_beam_on = conn.get_double("DP_IS_BEAM_ON")
    print(f"Beam status: {'ON' if is_beam_on == 1 else 'OFF'}")
    
    if is_beam_on != 1:
        print("Turning beam on...")
        ret = conn.execute("CMD_BEAM_ON")
        print(f"CMD_BEAM_ON returned: {ret}")
        ret = conn.execute("CMD_WAIT_BEAM_ON")
        print(f"CMD_WAIT_BEAM_ON returned: {ret}")
    
    # 3. Create ROI acquisition controller
    roi_acq = ROIAcquisition(conn)
    
    # 4. Configure with default parameters
    print("\n--- ROI Configuration ---")
    config = ROIConfig(
        # Overlap settings
        outer_mfov_overlap=0.04,      # 4% of MFOV
        inner_sfov_overlap=1.0,       # 1 µm between sub-images
        
        # Image settings
        pixelsize=10.0,               # 10 nm pixel size
        image_filetype="bmp",
        thumbnail_filetype="jpg",
        
        # Travel settings - User specified defaults
        travel_pattern=TravelPattern.COMB,
        travel_direction=TravelDirection.HORIZONTAL_UP,
        start_position=StartPosition.BOTTOM_LEFT,
        
        # Output
        output_folder=r"V:\Fuming\test",
        experiment_name="test_roi",
        sample_name="test_sample",
        operator_name="API_Test",
    )
    
    roi_acq.set_config(config)
    print(f"  Outer MFOV overlap: {config.outer_mfov_overlap * 100}%")
    print(f"  Inner SFOV overlap: {config.inner_sfov_overlap} µm")
    print(f"  Pixel size: {config.pixelsize} nm")
    print(f"  Image format: {config.image_filetype}")
    print(f"  Travel pattern: {config.travel_pattern.value}")
    print(f"  Travel direction: {config.travel_direction.value}")
    print(f"  Start position: {config.start_position.value}")
    print(f"  Output folder: {config.output_folder}")
    
    # Verify parameters were applied correctly
    print("\n--- Verify Applied Parameters ---")
    actual_hex_overlap = conn.get_double("AP_HEXAGON_OVERLAP")
    actual_overlap_x = conn.get_double("AP_OVERLAP_X")
    actual_overlap_y = conn.get_double("AP_OVERLAP_Y")
    actual_pixelsize = conn.get_double("AP_PIXELSIZE")
    print(f"  AP_HEXAGON_OVERLAP readback: {actual_hex_overlap} ({actual_hex_overlap*100:.1f}%)")
    print(f"  AP_OVERLAP_X readback: {actual_overlap_x} µm")
    print(f"  AP_OVERLAP_Y readback: {actual_overlap_y} µm")
    print(f"  AP_PIXELSIZE readback: {actual_pixelsize} nm")
    
    # Check if values match
    if abs(actual_hex_overlap - config.outer_mfov_overlap) > 0.001:
        print(f"  WARNING: MFOV overlap mismatch! Expected {config.outer_mfov_overlap}, got {actual_hex_overlap}")
    if abs(actual_overlap_x - config.inner_sfov_overlap) > 0.1:
        print(f"  WARNING: SFOV overlap X mismatch! Expected {config.inner_sfov_overlap}, got {actual_overlap_x}")
    if abs(actual_overlap_y - config.inner_sfov_overlap) > 0.1:
        print(f"  WARNING: SFOV overlap Y mismatch! Expected {config.inner_sfov_overlap}, got {actual_overlap_y}")
    if abs(actual_pixelsize - config.pixelsize) > 0.1:
        print(f"  WARNING: Pixel size mismatch! Expected {config.pixelsize}, got {actual_pixelsize}")
    
    # 5. Define test ROI - irregular polygon around current position
    print("\n--- Defining ROI ---")
    
    # Current position (you specified)
    center_x = 56352.0  # µm
    center_y = 13177.0  # µm
    
    # Read actual current position for verification
    actual_x = conn.get_double("AP_STAGE_AT_X")
    actual_y = conn.get_double("AP_STAGE_AT_Y")
    actual_z = conn.get_double("AP_STAGE_AT_Z")
    print(f"Actual stage position: X={actual_x:.1f}, Y={actual_y:.1f}, Z={actual_z:.1f} µm")
    print(f"Using center: X={center_x:.1f}, Y={center_y:.1f} µm")
    print("(Z will NOT be changed)")
    
    # Define a TRAPEZOID shape ROI
    #
    #       (3)-----------(2)     ← 上底 (200 µm)
    #      /                \
    #     /                  \
    #    /                    \
    #   (0)-------------------(1)  ← 下底 (400 µm)
    #   (4) = (0) closed
    #
    # Height: 300 µm
    
    # Trapezoid parameters
    bottom_width = 400  # µm - wider bottom
    top_width = 200     # µm - narrower top
    height = 300        # µm
    
    # Calculate corners (counterclockwise from bottom-left)
    # Closed polygon: n+1 points where last point = first point
    vertices = [
        (center_x - bottom_width / 2, center_y - height / 2),   # 0: bottom-left
        (center_x + bottom_width / 2, center_y - height / 2),   # 1: bottom-right
        (center_x + top_width / 2,    center_y + height / 2),   # 2: top-right
        (center_x - top_width / 2,    center_y + height / 2),   # 3: top-left
        (center_x - bottom_width / 2, center_y - height / 2),   # 4: = [0] closed
    ]
    
    print("\nTrapezoid ROI vertices (closed polygon, 4+1 points):")
    for i, (x, y) in enumerate(vertices):
        print(f"  [{i}] ({x:.1f}, {y:.1f}) µm")
    print(f"  ✓ Closed: [4] == [0]")
    
    roi_acq.define_roi(vertices)
    
    # Calculate bounding box
    x_coords = [v[0] for v in vertices]
    y_coords = [v[1] for v in vertices]
    print(f"\nBounding box: X=[{min(x_coords):.1f}, {max(x_coords):.1f}], "
          f"Y=[{min(y_coords):.1f}, {max(y_coords):.1f}] µm")
    print(f"Approx size: {max(x_coords)-min(x_coords):.0f} x {max(y_coords)-min(y_coords):.0f} µm")
    
    # 6. Calculate tile positions
    print("\n--- Calculating Tiles ---")
    tiles = roi_acq.calculate_tiles()
    print(f"Number of tiles: {len(tiles)}")
    
    if tiles:
        # Show travel order verification
        print("\n--- Travel Order Verification ---")
        print(f"Pattern: {config.travel_pattern.value} (Comb = same direction each row)")
        print(f"Direction: {config.travel_direction.value} (rows move bottom→top)")
        print(f"Start: {config.start_position.value} (start from left side)")
        
        # Group by row
        rows = {}
        for tile in tiles:
            if tile.row not in rows:
                rows[tile.row] = []
            rows[tile.row].append(tile)
        
        print(f"\nTile layout ({len(rows)} rows):")
        for row_idx in sorted(rows.keys()):
            row_tiles = rows[row_idx]
            # Show first tile index in this row
            first_idx = tiles.index(row_tiles[0])
            x_range = f"X: {min(t.x for t in row_tiles):.0f} → {max(t.x for t in row_tiles):.0f}"
            y_val = row_tiles[0].y
            print(f"  Row {row_idx} (Y={y_val:.0f}): {len(row_tiles)} tiles, {x_range}, starts at tile #{first_idx}")
        
        print("\nFirst 10 tiles (in acquisition order):")
        for i, tile in enumerate(tiles[:10]):
            print(f"  [{i:2d}] ({tile.x:8.1f}, {tile.y:8.1f}) µm - row={tile.row}, col={tile.col}")
        
        if len(tiles) > 10:
            print(f"  ... and {len(tiles) - 10} more tiles")
            print(f"\nLast 3 tiles:")
            for i, tile in enumerate(tiles[-3:], start=len(tiles)-3):
                print(f"  [{i:2d}] ({tile.x:8.1f}, {tile.y:8.1f}) µm - row={tile.row}, col={tile.col}")
    
    # 7. Estimate acquisition time
    estimated_time = roi_acq.estimate_time()
    print(f"\nEstimated acquisition time: {estimated_time:.1f} seconds ({estimated_time/60:.1f} minutes)")
    
    # 8. Set progress callback
    roi_acq.set_progress_callback(progress_callback)
    
    # 9. Acquire all tiles
    print("\n--- Acquisition ---")
    print("Starting acquisition...")
    ret = roi_acq.acquire_all()
    print(f"Acquisition returned: {ret}")
    
    if ret == 0:
        print("Acquisition completed successfully!")
        roi_acq.finish_experiment()
    else:
        print(f"Acquisition failed with code: {ret}")
        print(f"Error: {conn.get_last_error()}")
    
    # 10. Check for stored experiments (Janelia workflow)
    print("\n--- Stored Experiments (Tile Setups) ---")
    try:
        experiments = roi_acq.get_stored_experiments()
        if experiments:
            print(f"Found {len(experiments)} stored experiments:")
            for exp in experiments[:5]:
                print(f"  - {exp['name']} (ID: {exp['id'][:8]}..., Regions: {exp['regions']})")
        else:
            print("No stored experiments found.")
    except Exception as e:
        print(f"Could not retrieve stored experiments: {e}")
    
    print("\nDone.")


if __name__ == "__main__":
    main()

