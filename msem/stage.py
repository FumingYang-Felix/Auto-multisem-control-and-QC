"""
Stage control for MultiSEM
"""

from __future__ import annotations
from typing import Tuple, Optional
from dataclasses import dataclass

try:
    from .connection import MSEMConnection
except ImportError:
    from connection import MSEMConnection


@dataclass
class StagePosition:
    """Stage position in micrometers"""
    x: float
    y: float
    z: Optional[float] = None


class Stage:
    """
    Stage control interface.
    
    Usage:
        conn = MSEMConnection()
        conn.connect_and_initialize()
        
        stage = Stage(conn)
        pos = stage.get_position()
        print(f"Current: X={pos.x}, Y={pos.y}")
        
        stage.move_xy(1000.0, 2000.0, wait=True)
    """
    
    # Stage limits in micrometers
    X_MIN = -65000.0
    X_MAX = 65000.0
    Y_MIN = -65000.0
    Y_MAX = 65000.0
    Z_MIN = 0.0
    Z_MAX = 50000.0
    
    # Default working distance (Z position for imaging)
    DEFAULT_WORKING_Z = 38500.0  # µm - standard working distance
    
    def __init__(self, connection: MSEMConnection) -> None:
        self._conn = connection
    
    def get_position(self) -> StagePosition:
        """Get current stage position (X, Y, Z)"""
        x = self._conn.get_double("AP_STAGE_AT_X")
        y = self._conn.get_double("AP_STAGE_AT_Y")
        try:
            z = self._conn.get_double("AP_STAGE_AT_Z")
        except Exception:
            z = None
        return StagePosition(x=x, y=y, z=z)
    
    def get_xy(self) -> Tuple[float, float]:
        """Get current X, Y position"""
        x = self._conn.get_double("AP_STAGE_AT_X")
        y = self._conn.get_double("AP_STAGE_AT_Y")
        return x, y
    
    def move_xy(self, x: float, y: float, wait: bool = True) -> int:
        """
        Move stage to target X, Y position.
        
        Args:
            x: Target X in micrometers
            y: Target Y in micrometers
            wait: If True, wait for stage to settle
            
        Returns:
            0 on success (from CMD_WAIT_STAGE_SETTLED if wait=True)
        """
        self._validate_xy(x, y)
        
        self._conn.set_double("AP_STAGE_GOTO_X", x)
        self._conn.set_double("AP_STAGE_GOTO_Y", y)
        
        if wait:
            return self._conn.execute("CMD_WAIT_STAGE_SETTLED")
        return 0
    
    def move_x(self, x: float, wait: bool = True) -> int:
        """Move stage X only"""
        self._validate_x(x)
        self._conn.set_double("AP_STAGE_GOTO_X", x)
        if wait:
            return self._conn.execute("CMD_WAIT_STAGE_SETTLED")
        return 0
    
    def move_y(self, y: float, wait: bool = True) -> int:
        """Move stage Y only"""
        self._validate_y(y)
        self._conn.set_double("AP_STAGE_GOTO_Y", y)
        if wait:
            return self._conn.execute("CMD_WAIT_STAGE_SETTLED")
        return 0
    
    def move_z(self, z: float, wait: bool = True) -> int:
        """Move stage Z"""
        self._validate_z(z)
        self._conn.set_double("AP_STAGE_GOTO_Z", z)
        if wait:
            return self._conn.execute("CMD_WAIT_STAGE_SETTLED")
        return 0
    
    def move_to_working_z(self, wait: bool = True) -> int:
        """
        Move stage Z to default working distance (38500 µm).
        
        SAFETY: This will move the stage! Ensure clearance before calling.
        
        Returns:
            0 on success
        """
        return self.move_z(self.DEFAULT_WORKING_Z, wait=wait)
    
    # ========== Validation ==========
    
    def _validate_x(self, x: float) -> None:
        if not (self.X_MIN <= x <= self.X_MAX):
            raise ValueError(f"X={x} out of range [{self.X_MIN}, {self.X_MAX}]")
    
    def _validate_y(self, y: float) -> None:
        if not (self.Y_MIN <= y <= self.Y_MAX):
            raise ValueError(f"Y={y} out of range [{self.Y_MIN}, {self.Y_MAX}]")
    
    def _validate_z(self, z: float) -> None:
        if not (self.Z_MIN <= z <= self.Z_MAX):
            raise ValueError(f"Z={z} out of range [{self.Z_MIN}, {self.Z_MAX}]")
    
    def _validate_xy(self, x: float, y: float) -> None:
        self._validate_x(x)
        self._validate_y(y)

