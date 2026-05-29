"""
Test script for auto-stigmation operations.
Run this on a machine connected to MultiSEM.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from msem import MSEMConnection, Autostig, Automstig


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
    print(f"Beam on: {is_beam_on}")
    
    if is_beam_on < 0.5:
        print("WARNING: Beam is off. Autostig requires beam to be on.")
        return
    
    # 3. Main stigmator (Autostig)
    print("\n--- Main Stigmator (Autostig) ---")
    stig = Autostig(conn)
    
    params = stig.get_parameters()
    print(f"Current parameters:")
    print(f"  Pixelsize: {params.pixelsize} nm")
    print(f"  Range: {params.range}")
    print(f"  Scanrate: {params.scanrate}")
    print(f"  Support points: {params.support_points}")
    
    # Set recommended defaults
    print("\nSetting recommended defaults...")
    stig.set_recommended_defaults()
    
    print("\nRunning CMD_AUTOSTIG...")
    result = stig.run()
    print(f"Success: {result.success}")
    print(f"Confidence X: {result.confidence_x}")
    print(f"Confidence Y: {result.confidence_y}")
    print(f"Message: {result.message}")
    
    # 4. Multi-stigmator (Automstig)
    print("\n--- Multi-Stigmator (Automstig) ---")
    mstig = Automstig(conn)
    
    params = mstig.get_parameters()
    print(f"Current parameters:")
    print(f"  Pixelsize: {params.pixelsize} nm")
    print(f"  Range: {params.range}")
    print(f"  Scanrate: {params.scanrate}")
    print(f"  Support points: {params.support_points}")
    
    print("\nRunning CMD_AUTOMSTIG (this takes longer)...")
    ret = mstig.run()
    print(f"Returned: {ret}")
    
    print("\nDone.")


if __name__ == "__main__":
    main()

