"""
Test script for single image acquisition.
Run this on a machine connected to MultiSEM.
"""

import sys
from pathlib import Path

# Add parent directory to path so we can import msem
sys.path.insert(0, str(Path(__file__).parent.parent))

from msem import MSEMConnection, Acquisition


def main():
    # 1. Connect
    print("Connecting to MultiSEM...")
    conn = MSEMConnection()
    ret = conn.connect_and_initialize()
    print(f"Initialize returned: {ret}")
    
    if ret != 0:
        print(f"ERROR: {conn.get_last_error()}")
        return
    
    # 2. Check system status
    acq_timeout = conn.get_double("AP_ACQUISITION_TIMEOUT")
    scan_timeout = conn.get_double("AP_SCAN_TIMEOUT")
    print(f"Acquisition timeout: {acq_timeout} ms")
    print(f"Scan timeout: {scan_timeout} ms")
    
    # Check if this is simulation or real hardware
    try:
        hw_connected = conn.get_double("DP_IS_HARDWARE_CONNECTED")
        print(f"Hardware connected: {hw_connected}")
    except:
        print("Hardware connected: (unable to read)")
    
    # Check vacuum status
    try:
        vac_status = conn.get_double("DP_VACSTATUS")
        print(f"Vacuum status: {vac_status}")
    except:
        print("Vacuum status: (unable to read)")
    
    # Check and enable data records
    try:
        data_records = conn.get_double("DP_DATA_RECORDS_ENABLED")
        print(f"Data records enabled: {data_records}")
        if data_records < 0.5:
            print("  -> Enabling data records...")
            conn.set_double("DP_DATA_RECORDS_ENABLED", 1.0)
    except:
        pass
    
    # Increase timeout if needed (max 30000 ms)
    # conn.set_double("AP_ACQUISITION_TIMEOUT", 30000.0)
    # conn.set_double("AP_SCAN_TIMEOUT", 30000.0)
    
    # 3. Check beam status
    is_beam_on = conn.get_double("DP_IS_BEAM_ON")
    print(f"Beam on: {is_beam_on}")
    
    if is_beam_on < 0.5:
        print("Beam is off, turning on...")
        ret = conn.execute("CMD_BEAM_ON")
        print(f"CMD_BEAM_ON returned: {ret}")
        ret = conn.execute("CMD_WAIT_BEAM_ON")
        print(f"CMD_WAIT_BEAM_ON returned: {ret}")
        
        # Verify beam is now on
        is_beam_on = conn.get_double("DP_IS_BEAM_ON")
        print(f"Beam on after CMD_BEAM_ON: {is_beam_on}")
    
    # 4. Create Acquisition controller
    acq = Acquisition(conn)
    
    # 5. Configure output folder
    # IMPORTANT: This folder must exist on ALL acquisition PCs!
    output_folder = r"V:\Fuming\test"
    print(f"Setting output folder: {output_folder}")
    
    # Check if folder is accessible from this PC (optional, acq PC is what matters)
    import os
    if os.path.isdir(output_folder):
        print(f"  -> Folder accessible from this PC: YES")
    else:
        print(f"  -> Folder accessible from this PC: NO (may still work if acq PC can access)")
    
    acq.set_output_folder(
        folder=output_folder,
        image_prefix="%ts_test",
        image_filetype="tif",
        enable_thumbnails=True,
    )
    
    # Read back to verify
    folder_readback = conn.get_string("STR_REMOTE_IMAGE_FOLDER")
    print(f"  -> STR_REMOTE_IMAGE_FOLDER readback: {folder_readback}")
    
    # Check and set image location (0=not saved, 1=saved to storage)
    try:
        img_location = conn.get_double("DP_IMAGE_LOCATION")
        print(f"  -> DP_IMAGE_LOCATION: {img_location} (0=not saved, 1=saved)")
        if img_location < 0.5:
            print("  -> Setting DP_IMAGE_LOCATION to 1 (save images)")
            conn.set_double("DP_IMAGE_LOCATION", 1.0)
    except Exception as e:
        print(f"  -> DP_IMAGE_LOCATION error: {e}")
    
    # Check and set thumbnail location
    try:
        thumb_location = conn.get_double("DP_THUMBNAIL_LOCATION")
        print(f"  -> DP_THUMBNAIL_LOCATION: {thumb_location} (0=not saved, 1=saved)")
        if thumb_location < 0.5:
            print("  -> Setting DP_THUMBNAIL_LOCATION to 1 (save thumbnails)")
            conn.set_double("DP_THUMBNAIL_LOCATION", 1.0)
    except Exception as e:
        print(f"  -> DP_THUMBNAIL_LOCATION error: {e}")
    
    # Read back all image settings to verify
    try:
        prefix = conn.get_string("STR_IMAGE_FILENAME_PREFIX")
        filetype = conn.get_string("STR_IMAGE_FILETYPE")
        print(f"  -> STR_IMAGE_FILENAME_PREFIX: {prefix}")
        print(f"  -> STR_IMAGE_FILETYPE: {filetype}")
    except Exception as e:
        print(f"  -> Error reading image settings: {e}")
    
    # 6. Configure scan parameters
    width = 1024
    height = 1024
    scanrate = 1
    print(f"Setting scan parameters: {width}x{height}, scanrate={scanrate}")
    ret = acq.set_scan_parameters(width=width, height=height, scanrate=scanrate)
    print(f"CMD_APPLY_SCAN_PARAMETERS returned: {ret}")
    
    # 7. Read back current parameters
    params = acq.get_scan_parameters()
    print(f"Current scan params: {params.width}x{params.height}, rate={params.scanrate}")
    
    # 8. Acquire single image (step by step for debugging)
    print("Triggering acquisition...")
    ret = acq.trigger()
    print(f"CMD_TRIGGER_ACQUISITION returned: {ret}")
    
    if ret != 0:
        print(f"Trigger failed! error={conn.get_last_error()}")
        return
    
    print("Waiting for acquisition complete...")
    ret = acq.wait_complete()
    print(f"CMD_WAIT_ACQUISITION_COMPLETE returned: {ret}")
    
    if ret == 0:
        print("SUCCESS: Image acquired!")
        print(f"Images saved to: {conn.get_string('STR_REMOTE_IMAGE_FOLDER')}")
    else:
        print(f"FAILED: ret={ret}, error={conn.get_last_error()}")
    
    print("\nDone.")


if __name__ == "__main__":
    main()
