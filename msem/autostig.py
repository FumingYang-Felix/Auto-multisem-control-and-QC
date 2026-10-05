"""
Auto-stigmation control for MultiSEM

API Reference (from 20200821_MultiSEM_API_17_V2.pdf):

Commands:
- CMD_AUTOSTIG: Main stigmator alignment (affects all beams)
- CMD_AUTOMSTIG: Multi-stigmator alignment (per-beam correction)
- CMD_RESET_AUTOSTIG: Reset main stigmator parameters
- CMD_RESET_AUTOMSTIG: Reset multi-stigmator parameters

Parameters for CMD_AUTOSTIG:
- AP_AUTOSTIG_PIXELSIZE: 1-25 nm
- AP_AUTOSTIG_RANGE: 0-100
- DP_AUTOSTIG_SCANRATE: 1-8
- DP_AUTOSTIG_SUPPORTPOINTS: 5-100 (number of sampling points; called
  "sampling_points" in this module, not "support_points", to avoid confusion
  with FocusMap support points, which are x,y stage positions)

Parameters for CMD_AUTOMSTIG:
- AP_AUTOMSTIG_PIXELSIZE: 1-25 nm
- AP_AUTOMSTIG_RANGE: 0-100
- DP_AUTOMSTIG_SCANRATE: 1-8
- DP_AUTOMSTIG_SUPPORTPOINTS: 5-100 (same "sampling_points" naming as above)

Confidence values:
- > 0: Successful, smaller is better
- -1: Not enough features on sample
- -2: Upper limit reached
- -3: Lower limit reached
"""

from __future__ import annotations
from dataclasses import dataclass

try:
    from .connection import MSEMConnection
except ImportError:
    from connection import MSEMConnection


@dataclass
class AutostigResult:
    """Result of autostig operation"""
    success: bool
    confidence_x: float
    confidence_y: float
    message: str


@dataclass
class AutostigParameters:
    """Current autostig parameters"""
    pixelsize: float  # nm
    range: float
    scanrate: int
    sampling_points: int


class Autostig:
    """
    Main stigmator auto-alignment interface.
    
    This corrects stigmation that affects ALL beams equally.
    
    Usage:
        conn = MSEMConnection()
        conn.connect_and_initialize()
        
        stig = Autostig(conn)
        result = stig.run()
        if result.success:
            print("Stigmation corrected!")
    """
    
    # Parameter limits
    PIXELSIZE_MIN = 1.0
    PIXELSIZE_MAX = 25.0
    RANGE_MIN = 0.0
    RANGE_MAX = 100.0
    SCANRATE_MIN = 1
    SCANRATE_MAX = 8
    SAMPLINGPOINTS_MIN = 5
    SAMPLINGPOINTS_MAX = 100

    # Recommended defaults - range/sampling_points match ZEN's own stock AS
    # defaults (confirmed against ZEN directly)
    DEFAULT_PIXELSIZE = 4.0
    DEFAULT_RANGE = 2.5
    DEFAULT_SCANRATE = 1
    DEFAULT_SAMPLINGPOINTS = 7

    def __init__(self, connection: MSEMConnection) -> None:
        self._conn = connection

    # ========== Parameters ==========

    def get_parameters(self) -> AutostigParameters:
        """Get current autostig parameters"""
        return AutostigParameters(
            pixelsize=self._conn.get_double("AP_AUTOSTIG_PIXELSIZE"),
            range=self._conn.get_double("AP_AUTOSTIG_RANGE"),
            scanrate=int(self._conn.get_double("DP_AUTOSTIG_SCANRATE")),
            sampling_points=int(self._conn.get_double("DP_AUTOSTIG_SUPPORTPOINTS")),
        )

    def set_parameters(
        self,
        pixelsize: float = None,
        range: float = None,
        scanrate: int = None,
        sampling_points: int = None,
    ) -> None:
        """Set autostig parameters"""
        if pixelsize is not None:
            self._conn.set_double("AP_AUTOSTIG_PIXELSIZE", pixelsize)
        if range is not None:
            self._conn.set_double("AP_AUTOSTIG_RANGE", range)
        if scanrate is not None:
            self._conn.set_double("DP_AUTOSTIG_SCANRATE", float(scanrate))
        if sampling_points is not None:
            self._conn.set_double("DP_AUTOSTIG_SUPPORTPOINTS", float(sampling_points))

    def set_recommended_defaults(self) -> None:
        """Set recommended default parameters"""
        self.set_parameters(
            pixelsize=self.DEFAULT_PIXELSIZE,
            range=self.DEFAULT_RANGE,
            scanrate=self.DEFAULT_SCANRATE,
            sampling_points=self.DEFAULT_SAMPLINGPOINTS,
        )
    
    def reset_parameters(self) -> int:
        """Reset parameters to API defaults"""
        return self._conn.execute("CMD_RESET_AUTOSTIG")
    
    # ========== Run ==========
    
    def run(self) -> AutostigResult:
        """
        Run main stigmator alignment (CMD_AUTOSTIG).
        
        This corrects astigmatism affecting all beams equally.
        
        Returns:
            AutostigResult with success status and confidence
        """
        ret = self._conn.execute("CMD_AUTOSTIG")
        
        try:
            conf_x = self._conn.get_double("AP_AUTOSTIG_CONFIDENCE_X")
            conf_y = self._conn.get_double("AP_AUTOSTIG_CONFIDENCE_Y")
        except:
            conf_x = 0.0 if ret == 0 else -1.0
            conf_y = 0.0 if ret == 0 else -1.0
        
        if ret == 0:
            return AutostigResult(
                success=True,
                confidence_x=conf_x,
                confidence_y=conf_y,
                message="Autostig successful"
            )
        else:
            return AutostigResult(
                success=False,
                confidence_x=conf_x,
                confidence_y=conf_y,
                message=self._interpret_confidence(min(conf_x, conf_y))
            )
    
    def _interpret_confidence(self, confidence: float) -> str:
        if confidence > 0:
            return f"Success with confidence {confidence}"
        elif confidence == -1:
            return "Failed: Not enough features on sample"
        elif confidence == -2:
            return "Failed: Upper limit reached"
        elif confidence == -3:
            return "Failed: Lower limit reached"
        else:
            return f"Failed with confidence {confidence}"


class Automstig:
    """
    Multi-stigmator auto-alignment interface.
    
    This corrects stigmation for EACH BEAM individually.
    Takes longer than Autostig but provides per-beam correction.
    
    Usage:
        conn = MSEMConnection()
        conn.connect_and_initialize()
        
        mstig = Automstig(conn)
        ret = mstig.run()
        if ret == 0:
            print("Multi-stigmation corrected!")
    """
    
    # Parameter limits
    PIXELSIZE_MIN = 1.0
    PIXELSIZE_MAX = 25.0
    RANGE_MIN = 0.0
    RANGE_MAX = 100.0
    SCANRATE_MIN = 1
    SCANRATE_MAX = 8
    SAMPLINGPOINTS_MIN = 5
    SAMPLINGPOINTS_MAX = 100

    # Recommended defaults
    DEFAULT_PIXELSIZE = 5.0
    DEFAULT_RANGE = 10.0
    DEFAULT_SCANRATE = 1
    DEFAULT_SAMPLINGPOINTS = 15

    def __init__(self, connection: MSEMConnection) -> None:
        self._conn = connection

    # ========== Parameters ==========

    def get_parameters(self) -> AutostigParameters:
        """Get current automstig parameters"""
        return AutostigParameters(
            pixelsize=self._conn.get_double("AP_AUTOMSTIG_PIXELSIZE"),
            range=self._conn.get_double("AP_AUTOMSTIG_RANGE"),
            scanrate=int(self._conn.get_double("DP_AUTOMSTIG_SCANRATE")),
            sampling_points=int(self._conn.get_double("DP_AUTOMSTIG_SUPPORTPOINTS")),
        )

    def set_parameters(
        self,
        pixelsize: float = None,
        range: float = None,
        scanrate: int = None,
        sampling_points: int = None,
    ) -> None:
        """Set automstig parameters"""
        if pixelsize is not None:
            self._conn.set_double("AP_AUTOMSTIG_PIXELSIZE", pixelsize)
        if range is not None:
            self._conn.set_double("AP_AUTOMSTIG_RANGE", range)
        if scanrate is not None:
            self._conn.set_double("DP_AUTOMSTIG_SCANRATE", float(scanrate))
        if sampling_points is not None:
            self._conn.set_double("DP_AUTOMSTIG_SUPPORTPOINTS", float(sampling_points))

    def set_recommended_defaults(self) -> None:
        """Set recommended default parameters"""
        self.set_parameters(
            pixelsize=self.DEFAULT_PIXELSIZE,
            range=self.DEFAULT_RANGE,
            scanrate=self.DEFAULT_SCANRATE,
            sampling_points=self.DEFAULT_SAMPLINGPOINTS,
        )
    
    def reset_parameters(self) -> int:
        """Reset parameters to API defaults"""
        return self._conn.execute("CMD_RESET_AUTOMSTIG")
    
    # ========== Run ==========
    
    def run(self) -> int:
        """
        Run multi-stigmator alignment (CMD_AUTOMSTIG).
        
        This corrects astigmatism for each beam individually.
        Takes longer than CMD_AUTOSTIG.
        
        Returns:
            0 on success, -1 on failure
        """
        return self._conn.execute("CMD_AUTOMSTIG")

