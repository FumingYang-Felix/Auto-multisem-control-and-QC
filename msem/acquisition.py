"""
Acquisition control for MultiSEM

API Reference (from 20200821_MultiSEM_API_17_V2.pdf):
- DP_PREPARED_WIDTH: 128 - 1048575 pixels
- DP_PREPARED_HEIGHT: 128 - 1048575 pixels  
- DP_PREPARED_SCANRATE: 1 - 20
  Pixel time: tpx = 2^(r-1) * 50ns
  Example: scanrate=1 -> 50ns, scanrate=2 -> 100ns, scanrate=8 -> 6400ns
- STR_IMAGE_FILETYPE: bmp, jpg, png, tif, etc.
- STR_IMAGE_FILENAME_PREFIX: use %ts for timestamp
"""

from __future__ import annotations
from typing import Optional
from dataclasses import dataclass

try:
    from .connection import MSEMConnection
except ImportError:
    from connection import MSEMConnection


@dataclass
class ScanParameters:
    """Scan parameters for acquisition"""
    width: int = 2048
    height: int = 2048
    scanrate: int = 1


@dataclass 
class AcquisitionConfig:
    """Configuration for image acquisition"""
    image_folder: str = ""
    thumbnail_folder: str = ""
    image_prefix: str = "%ts_acq"
    thumbnail_prefix: str = "%ts_thumb"
    image_filetype: str = "tif"
    thumbnail_filetype: str = "jpg"
    enable_thumbnails: bool = True


class Acquisition:
    """
    Single image acquisition interface.
    
    Usage:
        conn = MSEMConnection()
        conn.connect_and_initialize()
        
        acq = Acquisition(conn)
        
        # Configure output
        acq.set_output_folder(r"\\acq-pc\data\test")
        
        # Configure scan
        acq.set_scan_parameters(width=2048, height=2048, scanrate=1)
        
        # Take one image
        acq.acquire_single()
    """
    
    # Parameter limits from API documentation
    WIDTH_MIN = 128
    WIDTH_MAX = 1048575
    HEIGHT_MIN = 128
    HEIGHT_MAX = 1048575
    SCANRATE_MIN = 1
    SCANRATE_MAX = 20
    
    # Supported file types
    SUPPORTED_FILETYPES = ("bmp", "jpg", "jpeg", "png", "tif", "tiff")
    
    def __init__(self, connection: MSEMConnection) -> None:
        self._conn = connection
    
    # ========== Output Configuration ==========
    
    def set_output_folder(
        self,
        folder: str,
        image_prefix: str = "%ts_acq",
        image_filetype: str = "tif",
        thumbnail_prefix: Optional[str] = None,
        thumbnail_filetype: str = "jpg",
        enable_thumbnails: bool = True,
    ) -> None:
        """
        Configure output folder and file naming.
        
        Args:
            folder: Remote folder path (must be accessible by acquisition PC)
            image_prefix: Prefix for image files (%ts = timestamp)
            image_filetype: Image format (bmp, jpg, png, tif)
            thumbnail_prefix: Prefix for thumbnails (defaults to image_prefix)
            thumbnail_filetype: Thumbnail format
            enable_thumbnails: Whether to generate thumbnails
        """
        if thumbnail_prefix is None:
            thumbnail_prefix = image_prefix
        
        # Enable image saving (DP_IMAGE_LOCATION = 1)
        self._conn.set_double("DP_IMAGE_LOCATION", 1.0)
        
        # Set image folder and naming
        self._conn.set_string("STR_REMOTE_IMAGE_FOLDER", folder)
        self._conn.set_string("STR_IMAGE_FILENAME_PREFIX", image_prefix)
        self._conn.set_string("STR_IMAGE_FILETYPE", image_filetype)
        
        # Set thumbnail folder and naming
        if enable_thumbnails:
            self._conn.set_double("DP_THUMBNAIL_LOCATION", 1.0)
            self._conn.set_string("STR_REMOTE_THUMBNAIL_FOLDER", folder)
            self._conn.set_string("STR_THUMBNAIL_FILENAME_PREFIX", thumbnail_prefix)
            self._conn.set_string("STR_THUMBNAIL_FILETYPE", thumbnail_filetype)
        else:
            self._conn.set_double("DP_THUMBNAIL_LOCATION", 0.0)
    
    # ========== Scan Parameters ==========
    
    def set_scan_parameters(
        self,
        width: int = 2048,
        height: int = 2048,
        scanrate: int = 1,
    ) -> int:
        """
        Set scan parameters and apply them.
        
        Args:
            width: Image width in pixels (128 - 1048575)
            height: Image height in pixels (128 - 1048575)
            scanrate: Scan rate 1-20 (pixel time = 2^(r-1) * 50ns)
                     1 = 50ns (fastest), 8 = 6.4us, 20 = 26.2ms (slowest)
            
        Returns:
            0 on success from CMD_APPLY_SCAN_PARAMETERS
        """
        self._validate_scan_parameters(width, height, scanrate)
        
        self._conn.set_double("DP_PREPARED_WIDTH", float(width))
        self._conn.set_double("DP_PREPARED_HEIGHT", float(height))
        self._conn.set_double("DP_PREPARED_SCANRATE", float(scanrate))
        
        return self._conn.execute("CMD_APPLY_SCAN_PARAMETERS")
    
    def get_scan_parameters(self) -> ScanParameters:
        """Get current scan parameters"""
        width = int(self._conn.get_double("DP_PREPARED_WIDTH"))
        height = int(self._conn.get_double("DP_PREPARED_HEIGHT"))
        scanrate = int(self._conn.get_double("DP_PREPARED_SCANRATE"))
        return ScanParameters(width=width, height=height, scanrate=scanrate)
    
    def _validate_scan_parameters(self, width: int, height: int, scanrate: int) -> None:
        """Validate scan parameters against API limits"""
        if not (self.WIDTH_MIN <= width <= self.WIDTH_MAX):
            raise ValueError(
                f"width={width} out of range [{self.WIDTH_MIN}, {self.WIDTH_MAX}]"
            )
        if not (self.HEIGHT_MIN <= height <= self.HEIGHT_MAX):
            raise ValueError(
                f"height={height} out of range [{self.HEIGHT_MIN}, {self.HEIGHT_MAX}]"
            )
        if not (self.SCANRATE_MIN <= scanrate <= self.SCANRATE_MAX):
            raise ValueError(
                f"scanrate={scanrate} out of range [{self.SCANRATE_MIN}, {self.SCANRATE_MAX}]"
            )
    
    @staticmethod
    def scanrate_to_dwell_time_ns(scanrate: int) -> float:
        """
        Convert scanrate to pixel dwell time in nanoseconds.
        Formula: tpx = 2^(r-1) * 50ns
        """
        return (2 ** (scanrate - 1)) * 50.0
    
    # ========== Acquisition ==========
    
    def trigger(self) -> int:
        """
        Trigger acquisition (async).
        Call wait_complete() after to wait for completion.
        
        Returns:
            0 on success from CMD_TRIGGER_ACQUISITION
        """
        return self._conn.execute("CMD_TRIGGER_ACQUISITION")
    
    def wait_complete(self) -> int:
        """
        Wait for acquisition to complete.
        
        Returns:
            0 on success from CMD_WAIT_ACQUISITION_COMPLETE
        """
        return self._conn.execute("CMD_WAIT_ACQUISITION_COMPLETE")
    
    def wait_scan_complete(self) -> int:
        """
        Wait for scan to complete (image in buffer, not yet written to file).
        After this, stage movement is safe.
        
        Returns:
            0 on success from CMD_WAIT_SCAN_COMPLETE
        """
        return self._conn.execute("CMD_WAIT_SCAN_COMPLETE")
    
    def acquire_single(self) -> int:
        """
        Acquire a single image (trigger + wait).
        
        Returns:
            0 on success
        """
        ret = self.trigger()
        if ret != 0:
            return ret
        return self.wait_complete()
