"""
Test script for autofocus operations.
Run this on a machine connected to MultiSEM.

SAFETY WARNING: 
- Autofocus modifies the main lens current
- Ensure sample is roughly in focus before running
- Start with conservative range settings
"""

import sys
from pathlib import Path

# Add parent directory to path so we can import msem
sys.path.insert(0, str(Path(__file__).parent.parent))

from msem import MSEMConnection, Autofocus


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
        print("WARNING: Beam is off. Autofocus requires beam to be on.")
        print("Turning beam on...")
        conn.execute("CMD_BEAM_ON")
        conn.execute("CMD_WAIT_BEAM_ON")
    
    # 3. Create Autofocus controller
    af = Autofocus(conn)
    
    # 4. Read current parameters
    print("\n--- Current Autofocus Parameters ---")
    params = af.get_parameters()
    print(f"  Pixelsize: {params.pixelsize} nm")
    print(f"  Range: {params.range} µA")
    print(f"  Mode: {params.mode}")
    print(f"  Scanrate: {params.scanrate}")
    print(f"  Support points: {params.support_points}")
    
    # 5. Set recommended defaults
    print("\n--- Setting Recommended Defaults ---")
    af.set_recommended_defaults()
    params = af.get_parameters()
    print(f"  Pixelsize: {params.pixelsize} nm")
    print(f"  Range: {params.range} µA")
    print(f"  Scanrate: {params.scanrate}")
    print(f"  Support points: {params.support_points}")
    
    # 6. Run autofocus
    print("\n--- Running Autofocus ---")
    print("(This will modify the main lens current)")
    
    result = af.run()
    print(f"Success: {result.success}")
    print(f"Confidence: {result.confidence}")
    print(f"Message: {result.message}")
    
    print("\nDone.")
    print("For stigmator alignment, use test_autostig.py")


if __name__ == "__main__":
    main()

