"""
Shared utilities and definitions for process communication.
"""
from enum import Enum, auto
from dataclasses import dataclass
from typing import Any, Optional
import time

class SystemState(Enum):
    IDLE = auto()
    INITIALIZING = auto()
    MOVING = auto()
    ACQUIRING = auto()
    PROCESSING = auto()
    ERROR = auto()
    SHUTDOWN = auto()

class LogLevel(Enum):
    INFO = auto()
    WARNING = auto()
    ERROR = auto()
    DEBUG = auto()

@dataclass
class LogMessage:
    level: LogLevel
    source: str
    message: str
    timestamp: float = 0.0

    def __post_init__(self):
        if self.timestamp == 0.0:
            self.timestamp = time.time()

@dataclass
class Command:
    """Command sent from UI/Engine to Core"""
    action: str
    params: dict = None
    id: str = ""  # Unique ID for tracking

@dataclass
class ImageMetadata:
    """Metadata for an acquired image"""
    filename: str
    width: int
    height: int
    x_pos: float
    y_pos: float
    timestamp: float
    path: str

