"""
Simple test script for Stage operations.
Run this on a machine connected to MultiSEM.
"""

import sys
from pathlib import Path

# Add parent directory to path so we can import msem
sys.path.insert(0, str(Path(__file__).parent.parent))

from msem import MSEMConnection, Stage


def main():
    # 1. Connect
    print("Connecting to MultiSEM...")
    conn = MSEMConnection()
    ret = conn.connect_and_initialize()
    print(f"Initialize returned: {ret}")
    
    if ret != 0:
        print(f"ERROR: {conn.get_last_error()}")
        return
    
    # 2. Create Stage controller
    stage = Stage(conn)
    
    # 3. Read current position
    pos = stage.get_position()
    print(f"Current position: X={pos.x:.2f}, Y={pos.y:.2f}, Z={pos.z}")
    
    # 4. Test Z move (move down 100 µm = increase Z)
    if pos.z is not None:
        new_z = pos.z + 100  # move down 100 µm
        print(f"\n--- Z Movement Test ---")
        print(f"Current Z: {pos.z:.2f} µm")
        print(f"Target Z: {new_z:.2f} µm (down 100 µm)")
        
        # Safety check
        if new_z > Stage.Z_MAX:
            print(f"WARNING: Target Z exceeds maximum ({Stage.Z_MAX} µm)")
        else:
            print("Moving Z...")
            ret = stage.move_z(new_z, wait=True)
            print(f"Move returned: {ret}")
            
            # Read new position
            pos2 = stage.get_position()
            print(f"New Z position: {pos2.z:.2f} µm")
            
            # Move back
            print(f"\nMoving back to original Z: {pos.z:.2f} µm...")
            ret = stage.move_z(pos.z, wait=True)
            print(f"Move returned: {ret}")
            
            pos3 = stage.get_position()
            print(f"Final Z position: {pos3.z:.2f} µm")
    else:
        print("Z position not available")
    
    print("\nDone.")


if __name__ == "__main__":
    main()

