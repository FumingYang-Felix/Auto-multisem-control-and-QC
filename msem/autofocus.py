"""
Autofocus control for MultiSEM

API Reference (from 20200821_MultiSEM_API_17_V2.pdf):

Commands:
- CMD_AUTOFOCUS: Main autofocus command
- CMD_RESET_AUTOFOCUS: Reset autofocus parameters to defaults

Parameters:
- AP_AUTOFOCUS_PIXELSIZE: 1-25 nm (pixel size for autofocus)
- AP_AUTOFOCUS_RANGE: 10-10000 µA (search range for main lens current)
- DP_AUTOFOCUS_MODE: 1-2 (algorithm mode)
- DP_AUTOFOCUS_SCANRATE: 1-8 (scan rate during autofocus)
- DP_AUTOFOCUS_SUPPORTPOINTS: 3-100 (number of support points)

Confidence values (AP_AUTOFOCUS_CONFIDENCE):
- > 0: Successful, smaller is better
- -1: Not enough features on sample
- -2: Upper limit of search range reached
- -3: Lower limit of search range reached
- -4: No change made

For stigmator alignment, see autostig.py module.
"""

from __future__ import annotations
from typing import Optional
from dataclasses import dataclass
from enum import IntEnum

try:
    from .connection import MSEMConnection
except ImportError:
    from connection import MSEMConnection


class AutofocusMode(IntEnum):
    """Autofocus algorithm modes"""
    MODE_1 = 1
    MODE_2 = 2


@dataclass
class AutofocusResult:
    """Result of autofocus operation"""
    success: bool
    confidence: float
    message: str


@dataclass
class AutofocusParameters:
    """Current autofocus parameters"""
    pixelsize: float  # nm
    range: float  # µA
    mode: int
    scanrate: int
    support_points: int


class Autofocus:
    """
    Autofocus and auto-alignment interface.
    
    SAFETY NOTE: Autofocus modifies the main lens current.
    Ensure the sample is roughly in focus before running autofocus
    to avoid large current changes.
    
    Usage:
        conn = MSEMConnection()
        conn.connect_and_initialize()
        
        af = Autofocus(conn)
        
        # Get current parameters
        params = af.get_parameters()
        print(f"Current range: {params.range} µA")
        
        # Run autofocus with safety check
        result = af.run()
        if result.success:
            print(f"Focus found! Confidence: {result.confidence}")
        else:
            print(f"Autofocus failed: {result.message}")
    """
    
    # Parameter limits from API documentation
    PIXELSIZE_MIN = 1.0
    PIXELSIZE_MAX = 25.0
    RANGE_MIN = 10.0
    RANGE_MAX = 10000.0
    SCANRATE_MIN = 1
    SCANRATE_MAX = 8
    SUPPORTPOINTS_MIN = 3
    SUPPORTPOINTS_MAX = 100
    
    # Recommended defaults
    DEFAULT_PIXELSIZE = 4.0    # nm
    DEFAULT_RANGE = 150.0      # µA (conservative)
    DEFAULT_SCANRATE = 1       # slowest/best quality
    DEFAULT_SUPPORTPOINTS = 15 # good balance of speed/accuracy
    
    def __init__(self, connection: MSEMConnection) -> None:
        self._conn = connection
    
    # ========== Parameters ==========
    
    def get_parameters(self) -> AutofocusParameters:
        """Get current autofocus parameters"""
        return AutofocusParameters(
            pixelsize=self._conn.get_double("AP_AUTOFOCUS_PIXELSIZE"),
            range=self._conn.get_double("AP_AUTOFOCUS_RANGE"),
            mode=int(self._conn.get_double("DP_AUTOFOCUS_MODE")),
            scanrate=int(self._conn.get_double("DP_AUTOFOCUS_SCANRATE")),
            support_points=int(self._conn.get_double("DP_AUTOFOCUS_SUPPORTPOINTS")),
        )
    
    def set_parameters(
        self,
        pixelsize: Optional[float] = None,
        range: Optional[float] = None,
        mode: Optional[int] = None,
        scanrate: Optional[int] = None,
        support_points: Optional[int] = None,
    ) -> None:
        """
        Set autofocus parameters.
        
        SAFETY: Be careful with 'range' parameter - large values may cause
        significant lens current changes.
        
        Args:
            pixelsize: Pixel size in nm (1-25)
            range: Search range in µA (10-10000). CAUTION: Large values risky!
            mode: Algorithm mode (1 or 2)
            scanrate: Scan rate (1-8, 1=slowest/best quality)
            support_points: Number of support points (3-100)
        """
        if pixelsize is not None:
            self._validate_range("pixelsize", pixelsize, self.PIXELSIZE_MIN, self.PIXELSIZE_MAX)
            self._conn.set_double("AP_AUTOFOCUS_PIXELSIZE", pixelsize)
        
        if range is not None:
            self._validate_range("range", range, self.RANGE_MIN, self.RANGE_MAX)
            # Safety warning for large range
            if range > 2000:
                print(f"WARNING: Large autofocus range ({range} µA) may cause significant lens current changes!")
            self._conn.set_double("AP_AUTOFOCUS_RANGE", range)
        
        if mode is not None:
            if mode not in (1, 2):
                raise ValueError("mode must be 1 or 2")
            self._conn.set_double("DP_AUTOFOCUS_MODE", float(mode))
        
        if scanrate is not None:
            self._validate_range("scanrate", scanrate, self.SCANRATE_MIN, self.SCANRATE_MAX)
            self._conn.set_double("DP_AUTOFOCUS_SCANRATE", float(scanrate))
        
        if support_points is not None:
            self._validate_range("support_points", support_points, 
                                self.SUPPORTPOINTS_MIN, self.SUPPORTPOINTS_MAX)
            self._conn.set_double("DP_AUTOFOCUS_SUPPORTPOINTS", float(support_points))
    
    def reset_parameters(self) -> int:
        """
        Reset autofocus parameters to API default values.
        
        Returns:
            0 on success
        """
        return self._conn.execute("CMD_RESET_AUTOFOCUS")
    
    def set_recommended_defaults(self) -> None:
        """
        Set recommended default parameters for autofocus.
        
        Defaults:
        - pixelsize: 4 nm
        - range: 150 µA (conservative)
        - scanrate: 1 (slowest/best quality)
        - support_points: 15
        """
        self.set_parameters(
            pixelsize=self.DEFAULT_PIXELSIZE,
            range=self.DEFAULT_RANGE,
            scanrate=self.DEFAULT_SCANRATE,
            support_points=self.DEFAULT_SUPPORTPOINTS,
        )
    
    # ========== Autofocus Operations ==========
    
    def run(self, max_range: Optional[float] = None) -> AutofocusResult:
        """
        Run autofocus.
        
        SAFETY: This command modifies the main lens current.
        
        Args:
            max_range: Optional maximum search range in µA. 
                      If current range exceeds this, it will be reduced.
                      Default: None (use current setting)
        
        Returns:
            AutofocusResult with success status, confidence, and message
        """
        # Safety check: limit range if requested
        if max_range is not None:
            current_range = self._conn.get_double("AP_AUTOFOCUS_RANGE")
            if current_range > max_range:
                print(f"SAFETY: Reducing autofocus range from {current_range} to {max_range} µA")
                self._conn.set_double("AP_AUTOFOCUS_RANGE", max_range)
        
        # Execute autofocus
        ret = self._conn.execute("CMD_AUTOFOCUS")
        
        # Get confidence
        confidence = self.get_confidence()
        
        # Interpret result
        if ret == 0:
            return AutofocusResult(
                success=True,
                confidence=confidence,
                message="Autofocus successful"
            )
        else:
            return AutofocusResult(
                success=False,
                confidence=confidence,
                message=self._interpret_confidence(confidence)
            )
    
    def get_confidence(self) -> float:
        """
        Get confidence of last autofocus.
        
        Returns:
            > 0: Successful (smaller is better)
            -1: Not enough features on sample
            -2: Upper limit of search range reached
            -3: Lower limit of search range reached
            -4: No change made
        """
        return self._conn.get_double("AP_AUTOFOCUS_CONFIDENCE")
    
    # ========== Helper Methods ==========
    
    def _validate_range(self, name: str, value: float, min_val: float, max_val: float) -> None:
        if not (min_val <= value <= max_val):
            raise ValueError(f"{name}={value} out of range [{min_val}, {max_val}]")
    
    def _interpret_confidence(self, confidence: float) -> str:
        """Interpret confidence value as human-readable message"""
        if confidence > 0:
            return f"Success with confidence {confidence}"
        elif confidence == -1:
            return "Failed: Not enough features on sample"
        elif confidence == -2:
            return "Failed: Upper limit of search range reached"
        elif confidence == -3:
            return "Failed: Lower limit of search range reached"
        elif confidence == -4:
            return "No change made"
        else:
            return f"Unknown confidence value: {confidence}"

