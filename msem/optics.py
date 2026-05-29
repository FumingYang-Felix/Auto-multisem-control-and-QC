"""
Optics Control Module

Controls objective lens, stigmation, and multi-stigmation.
"""
from dataclasses import dataclass
from typing import List, Optional

try:
    from .connection import MSEMConnection
except ImportError:
    from connection import MSEMConnection


@dataclass
class StigmationValues:
    """Stigmation X and Y values"""
    x: float = 0.0
    y: float = 0.0


@dataclass
class MultiStigmation:
    """Per-beam stigmation values"""
    beam_index: int
    x: float = 0.0
    y: float = 0.0


class Optics:
    """
    Optics control interface.
    
    Controls:
    - Objective Lens current
    - Main stigmator (X, Y)
    - Multi-stigmator (per-beam correction)
    """
    
    NUM_BEAMS = 91
    
    def __init__(self, connection: MSEMConnection):
        self._conn = connection
        
    # === Objective Lens ===
    
    def get_ol_current(self) -> float:
        """
        Get current objective lens current.
        
        Returns:
            OL current in mA
        """
        return self._conn.get_double("AP_OLENS_CURRENT")
    
    def set_ol_current(self, current: float) -> bool:
        """
        Set objective lens current.
        
        Note: Changes to lens current may require time to stabilize.
        The focus will not be immediate after this command.
        
        Args:
            current: OL current in mA
            
        Returns:
            True on success
        """
        return self._conn.set_double("AP_OLENS_CURRENT", current)
    
    def get_dz_from_ol(self) -> float:
        """
        Get delta Z from objective lens.
        
        Returns:
            Delta Z in µm
        """
        return self._conn.get_double("AP_DZ_FROM_OLENS")
    
    # === Main Stigmator ===
    
    def get_stigmation(self) -> StigmationValues:
        """Get main stigmator values"""
        x = self._conn.get_double("AP_STIG_X")
        y = self._conn.get_double("AP_STIG_Y")
        return StigmationValues(x=x, y=y)
    
    def set_stigmation(self, x: float, y: float) -> bool:
        """
        Set main stigmator values.
        
        Note: After changing stigmation, the beam alignment may be affected.
        
        Args:
            x: Stigmation X value
            y: Stigmation Y value
            
        Returns:
            True if both succeeded
        """
        ret_x = self._conn.set_double("AP_STIG_X", x)
        ret_y = self._conn.set_double("AP_STIG_Y", y)
        return ret_x and ret_y
    
    def set_stig_x(self, x: float) -> bool:
        """Set main stigmator X"""
        return self._conn.set_double("AP_STIG_X", x)
    
    def set_stig_y(self, y: float) -> bool:
        """Set main stigmator Y"""
        return self._conn.set_double("AP_STIG_Y", y)
    
    # === Multi-Stigmator (Per-Beam) ===
    
    def get_multi_stigmation(self, beam_index: int) -> MultiStigmation:
        """
        Get stigmation correction for a specific beam.
        
        Args:
            beam_index: Beam index (0-90)
            
        Returns:
            MultiStigmation values for the beam
        """
        if beam_index < 0 or beam_index >= self.NUM_BEAMS:
            raise ValueError(f"Beam index must be 0-{self.NUM_BEAMS-1}")
            
        x = self._conn.get_double_from_list("AP_MSTIG_STIGMATION_X", beam_index)
        y = self._conn.get_double_from_list("AP_MSTIG_STIGMATION_Y", beam_index)
        
        return MultiStigmation(beam_index=beam_index, x=x, y=y)
    
    def set_multi_stigmation(self, beam_index: int, x: float, y: float) -> bool:
        """
        Set stigmation correction for a specific beam.
        
        Args:
            beam_index: Beam index (0-90)
            x: Stigmation X correction
            y: Stigmation Y correction
            
        Returns:
            True if both succeeded
        """
        if beam_index < 0 or beam_index >= self.NUM_BEAMS:
            raise ValueError(f"Beam index must be 0-{self.NUM_BEAMS-1}")
            
        ret_x = self._conn.set_double_from_list("AP_MSTIG_STIGMATION_X", beam_index, x)
        ret_y = self._conn.set_double_from_list("AP_MSTIG_STIGMATION_Y", beam_index, y)
        return ret_x and ret_y
    
    def get_all_multi_stigmation(self) -> List[MultiStigmation]:
        """Get stigmation values for all beams"""
        values = []
        for i in range(self.NUM_BEAMS):
            try:
                val = self.get_multi_stigmation(i)
                values.append(val)
            except:
                pass
        return values
    
    # === Convenience Methods ===
    
    def adjust_focus(self, delta: float) -> bool:
        """
        Adjust focus by changing OL current.
        
        Args:
            delta: Amount to adjust (positive = more current)
            
        Returns:
            True on success
        """
        current = self.get_ol_current()
        return self.set_ol_current(current + delta)
    
    def adjust_stigmation(self, dx: float, dy: float) -> bool:
        """
        Adjust stigmation by relative amounts.
        
        Args:
            dx: Amount to adjust X
            dy: Amount to adjust Y
            
        Returns:
            True on success
        """
        current = self.get_stigmation()
        return self.set_stigmation(current.x + dx, current.y + dy)

