"""
Detector Control Module

Controls detector equalization, offset, and sensitivity.
"""
from dataclasses import dataclass
from typing import List, Optional

try:
    from .connection import MSEMConnection
except ImportError:
    from connection import MSEMConnection


@dataclass
class DetectorSettings:
    """Global detector settings"""
    offset: float = 0.0
    sensitivity: float = 0.0


@dataclass
class DetectorCorrection:
    """Per-detector correction values"""
    beam_index: int
    offset_correction: float = 0.0
    sensitivity_correction: float = 0.0


class Detector:
    """
    Detector control interface.
    
    Controls:
    - Global detector offset and sensitivity
    - Per-detector corrections
    - Detector equalization
    """
    
    # Number of beams (91 for MultiSEM 91)
    NUM_BEAMS = 91
    
    def __init__(self, connection: MSEMConnection):
        self._conn = connection
        
    # === Global Settings ===
    
    def get_global_settings(self) -> DetectorSettings:
        """Get global detector offset and sensitivity"""
        offset = self._conn.get_double("AP_DETECTOR_OFFSET")
        sensitivity = self._conn.get_double("AP_DETECTOR_SENSITIVITY")
        return DetectorSettings(offset=offset, sensitivity=sensitivity)
    
    def set_global_offset(self, offset: float) -> bool:
        """Set global detector offset (brightness)"""
        return self._conn.set_double("AP_DETECTOR_OFFSET", offset)
    
    def set_global_sensitivity(self, sensitivity: float) -> bool:
        """Set global detector sensitivity (APD gain)"""
        return self._conn.set_double("AP_DETECTOR_SENSITIVITY", sensitivity)
    
    # === Per-Detector Corrections ===
    
    def get_detector_correction(self, beam_index: int) -> DetectorCorrection:
        """Get correction values for a specific detector (beam)"""
        if beam_index < 0 or beam_index >= self.NUM_BEAMS:
            raise ValueError(f"Beam index must be 0-{self.NUM_BEAMS-1}")
            
        offset_corr = self._conn.get_double_from_list("AP_DETECTOR_OFFSET_CORRECTION", beam_index)
        sens_corr = self._conn.get_double_from_list("AP_DETECTOR_SENSITIVITY_CORRECTION", beam_index)
        
        return DetectorCorrection(
            beam_index=beam_index,
            offset_correction=offset_corr,
            sensitivity_correction=sens_corr
        )
    
    def set_detector_offset_correction(self, beam_index: int, correction: float) -> bool:
        """Set offset correction for a specific detector"""
        if beam_index < 0 or beam_index >= self.NUM_BEAMS:
            raise ValueError(f"Beam index must be 0-{self.NUM_BEAMS-1}")
        return self._conn.set_double_from_list("AP_DETECTOR_OFFSET_CORRECTION", beam_index, correction)
    
    def set_detector_sensitivity_correction(self, beam_index: int, correction: float) -> bool:
        """Set sensitivity correction for a specific detector"""
        if beam_index < 0 or beam_index >= self.NUM_BEAMS:
            raise ValueError(f"Beam index must be 0-{self.NUM_BEAMS-1}")
        return self._conn.set_double_from_list("AP_DETECTOR_SENSITIVITY_CORRECTION", beam_index, correction)
    
    # === Equalization ===
    
    def equalize_detectors(self) -> int:
        """
        Run detector equalization.
        
        This command equalizes all detector responses.
        Should be run on a uniform sample area.
        
        Returns:
            0 on success
        """
        return self._conn.execute("CMD_EQUALIZE_DETECTORS")
    
    # === Utility ===
    
    def get_all_corrections(self) -> List[DetectorCorrection]:
        """Get corrections for all detectors"""
        corrections = []
        for i in range(self.NUM_BEAMS):
            try:
                corr = self.get_detector_correction(i)
                corrections.append(corr)
            except:
                pass
        return corrections

