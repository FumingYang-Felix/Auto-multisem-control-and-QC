"""
msem - MultiSEM API wrapper package

Modules:
    - connection: Low-level COM connection to MultiSEM
    - stage: Stage movement control
    - acquisition: Single image acquisition
    - autofocus: Autofocus control
    - autostig: Autostigmation control  
    - roi_acquisition: Region of Interest (ROI) acquisition
    - detector: Detector control and equalization
    - optics: Objective lens and stigmation control

NOTE: live_scan (Single Live / Full Live) was removed — that feature never
worked through the API (live image only renders in ZEN), per the GUI rewrite.
"""

try:
    from .connection import MSEMConnection
    from .stage import Stage, StagePosition
    from .acquisition import Acquisition, ScanParameters
    from .autofocus import Autofocus, AutofocusResult, AutofocusParameters
    from .autostig import Autostig, Automstig, AutostigResult
    from .roi_acquisition import (
        ROIAcquisition,
        ROIConfig,
        TilePosition,
        ROIProgress,
        TravelPattern,
        TravelDirection,
        StartPosition,
    )
    from .detector import Detector, DetectorSettings, DetectorCorrection
    from .optics import Optics, StigmationValues, MultiStigmation
except ImportError:
    from connection import MSEMConnection
    from stage import Stage, StagePosition
    from acquisition import Acquisition, ScanParameters
    from autofocus import Autofocus, AutofocusResult, AutofocusParameters
    from autostig import Autostig, Automstig, AutostigResult
    from roi_acquisition import (
        ROIAcquisition,
        ROIConfig,
        TilePosition,
        ROIProgress,
        TravelPattern,
        TravelDirection,
        StartPosition,
    )
    from detector import Detector, DetectorSettings, DetectorCorrection
    from optics import Optics, StigmationValues, MultiStigmation

__all__ = [
    # Connection
    "MSEMConnection",
    # Stage
    "Stage",
    "StagePosition",
    # Acquisition
    "Acquisition",
    "ScanParameters",
    # Autofocus
    "Autofocus",
    "AutofocusResult",
    "AutofocusParameters",
    # Autostig
    "Autostig",
    "Automstig",
    "AutostigResult",
    # ROI Acquisition
    "ROIAcquisition",
    "ROIConfig",
    "TilePosition",
    "ROIProgress",
    "TravelPattern",
    "TravelDirection",
    "StartPosition",
    # Detector
    "Detector",
    "DetectorSettings",
    "DetectorCorrection",
    # Optics
    "Optics",
    "StigmationValues",
    "MultiStigmation",
]
